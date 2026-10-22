"""Composition root and supervised process lifecycle."""
import logging
import os
import signal
import threading
import time

import telebot
from telebot import apihelper

from walletwatcher.config import Config, ConfigError
from walletwatcher.hyperliquid.client import HyperliquidClient
from walletwatcher.infrastructure.runtime import InstanceLock, WorkerSupervisor, configure_logging, in_docker
from walletwatcher.presentation.telegram import TelegramController
from walletwatcher.services.access import AutocloseAccess
from walletwatcher.services.health import HealthService
from walletwatcher.services.messaging import Replies
from walletwatcher.services.monitoring import MonitoringService
from walletwatcher.services.portfolios import PortfolioRequest, PortfolioService
from walletwatcher.services.settings import Settings
from walletwatcher.storage.autoclose import AutocloseStore
from walletwatcher.storage.credentials import CredentialsStore
from walletwatcher.storage.delivery import DeliveryQueue
from walletwatcher.trading.api import TradingAPI
from walletwatcher.trading.worker import AutocloseWorker

logger = logging.getLogger(__name__)


class Application:
    """Wire components and retain entry points that delegate to their owners."""

    def __init__(self, config, bot=None, client=None, outbox=None):
        self.settings = Settings(config)
        self.bot = bot or telebot.TeleBot(config.token, threaded=False)
        self._client = client or HyperliquidClient(perp_dexs=config.perp_dexs)
        self.outbox = outbox or DeliveryQueue(
            self.bot, config.database_file, config.edit_interval,
            config.chat_interval, config.global_interval)
        self.stop = threading.Event()
        started_at = time.monotonic()
        bot_id = config.token.split(':', 1)[0]
        self.outbox.bind_bot(bot_id)
        self.credentials = CredentialsStore(
            self.outbox.db, self.outbox.lock, bot_id,
            config.database_file.with_name('credentials.key'))
        self.autoclose_store = AutocloseStore(self.outbox, bot_id)
        self.replies = Replies(self.settings, self.outbox)
        self.monitoring = MonitoringService(self.settings, self.client, self.outbox,
                                            self.stop, self.autoclose_store.cancel_source)
        self.portfolios = PortfolioService(self.settings, self.client, self.credentials,
                                          self.outbox, self.stop, self.replies.reply)
        self.diagnostics = HealthService(self.settings, self.monitoring, self.portfolios,
                                         self.client, self.outbox, self.autoclose_store, started_at)
        self.autoclose_access = AutocloseAccess(self.settings, self.monitoring.is_tracked)
        self.autoclose = AutocloseWorker(
            self.autoclose_store, TradingAPI(self.client), credentials=self.credentials,
            access=self.autoclose_access, requests=self.client, stop=self.stop)
        self.commands = TelegramController(
            self.settings, self.bot, self.client, self.outbox, self.stop, self.credentials,
            self.autoclose_store, self.monitoring, self.portfolios, self.diagnostics, self.replies)
        self.outbox.prune(self.accounts, config.recipients, config.chat_ids)

    @property
    def client(self):
        return self._client

    @client.setter
    def client(self, value):
        # Keep the facade's existing client injection seam consistent across services.
        self._client = value
        self.monitoring.client = self.portfolios.client = self.diagnostics.client = value
        self.commands.client = self.autoclose.requests = value

    @property
    def config(self):
        return self.settings.config

    @config.setter
    def config(self, value):
        self.settings.config = value

    @property
    def accounts(self):
        return self.monitoring.accounts

    @accounts.setter
    def accounts(self, value):
        self.monitoring.accounts = value

    @property
    def lock(self):
        return self.monitoring.lock

    @property
    def last_cycle(self):
        return self.monitoring.last_cycle

    @last_cycle.setter
    def last_cycle(self, value):
        self.monitoring.last_cycle = value

    @property
    def personal_queue(self):
        return self.portfolios.personal_queue

    @personal_queue.setter
    def personal_queue(self, value):
        self.portfolios.personal_queue = value

    @property
    def personal_pending(self):
        return self.portfolios.personal_pending

    @property
    def wallet_pending(self):
        return self.portfolios.wallet_pending

    @property
    def personal_lock(self):
        return self.portfolios.personal_lock

    @property
    def started_at(self):
        return self.diagnostics.started_at

    @started_at.setter
    def started_at(self, value):
        self.diagnostics.started_at = value

    @property
    def supervisor(self):
        return self.diagnostics.supervisor

    @supervisor.setter
    def supervisor(self, value):
        self.diagnostics.supervisor = value

    @property
    def offset(self):
        return self.commands.offset

    @offset.setter
    def offset(self, value):
        self.commands.offset = value

    def reply(self, chat, text):
        return self.replies.reply(chat, text)

    def broadcast(self, text):
        return self.replies.broadcast(text)

    def handle_message(self, message):
        return self.commands.handle_message(message)

    def _command(self, chat, text, user=None):
        return self.commands._command(chat, text, user)

    def poll_wallet(self, wallet, account):
        return self.monitoring.poll_wallet(wallet, account)

    def monitor(self, heartbeat):
        return self.monitoring.run(heartbeat)

    def polling(self, heartbeat):
        return self.commands.polling(heartbeat)

    def _queue_portfolio(self, request):
        return self.portfolios.enqueue(request)

    def process_personal_request(self, timeout=0):
        return self.portfolios.process(timeout)

    def personal_worker(self, heartbeat):
        return self.portfolios.run(heartbeat)

    def run(self):
        supervisor = WorkerSupervisor(self.stop, self.config.watchdog_timeout)
        self.supervisor = supervisor
        supervisor.add('monitor', self.monitor)
        supervisor.add('delivery', lambda beat: self.outbox.run(self.stop, beat))
        supervisor.add('polling', self.polling)
        supervisor.add('personal', self.personal_worker)
        supervisor.add('autoclose', self.autoclose.run)
        heartbeat_path = os.getenv('WALLETWATCHER_HEARTBEAT')
        try:
            self.broadcast('Bot restarted')
            while not self.stop.wait(1):
                supervisor.check()
                parent_pid = os.getenv('WALLETWATCHER_PARENT_PID')
                if parent_pid and os.getppid() != int(parent_pid):
                    raise RuntimeError('Supervisor disappeared; exiting orphaned bot')
                if heartbeat_path:
                    os.utime(heartbeat_path, None)
        finally:
            self.stop.set()
            if not supervisor.join(5):
                # Do not close SQLite underneath a worker or wait forever on an
                # unresponsive HTTP call; supervisor will create a fresh process.
                logger.warning('Workers did not stop within the shutdown deadline')
            else:
                self.client.close()
                self.outbox.close()


def main():
    configure_logging()
    if not in_docker():
        logger.error('WalletWatcher must run in Docker: docker compose up -d --build')
        return 78
    try:
        config = Config.from_env()
    except ConfigError as error:
        logger.error('Configuration error: %s', error)
        return 78
    # Timeout bounds also cover TeleBot's initial getMe request.
    apihelper.CONNECT_TIMEOUT = 5
    apihelper.READ_TIMEOUT = 30
    apihelper.RETRY_ON_ERROR = False
    config.wallets_file.parent.mkdir(parents=True, exist_ok=True)
    with InstanceLock(config.wallets_file.parent / 'bot.lock'):
        app = Application(config)

        def shutdown(signum, frame):
            logger.info('Shutdown requested (signal %s)', signum)
            app.stop.set()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        app.run()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        logger.exception('Bot process failed; supervisor can restart it')
        raise SystemExit(1)
