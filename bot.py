"""Application entry point; all outbound Telegram traffic uses DeliveryQueue."""
from datetime import datetime, timezone, timedelta
from html import escape
import copy
import logging
import os
import signal
import threading
import time

import telebot
from telebot import apihelper

from config import Config, ConfigError
from data_fetcher import DataFetchError, HyperliquidClient
from delivery import DeliveryQueue
from format import on_change_message
from models import Account, TradeAction
from runtime import InstanceLock, WorkerSupervisor, configure_logging, in_docker
from serialization import load_accounts, normalize_wallet, save_wallets, validate_tag

logger = logging.getLogger(__name__)
MOSCOW = timezone(timedelta(hours=3))


class Application:
    def __init__(self, config, bot=None, client=None, outbox=None):
        self.config = config
        self.bot = bot or telebot.TeleBot(config.token, threaded=False)
        self.client = client or HyperliquidClient(perp_dexs=config.perp_dexs)
        self.outbox = outbox or DeliveryQueue(
            self.bot, config.database_file, config.edit_interval,
            config.chat_interval, config.global_interval)
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.accounts = load_accounts(config.wallets_file)
        self.last_cycle = None
        self.outbox.bind_bot(config.token.split(':', 1)[0])
        self.offset = int(self.outbox.get_metadata('telegram_offset', 0))
        self.outbox.prune(self.accounts, config.recipients, config.chat_ids)

    def reply(self, chat, text):
        # Plain text avoids user-supplied Markdown/HTML breaking command replies.
        for start in range(0, len(text), 1500):
            self.outbox.enqueue(chat, text[start:start + 1500])

    def broadcast(self, text):
        for chat in self.config.recipients:
            self.reply(chat, text)

    def handle_message(self, message):
        chat = message.chat.id
        if chat not in self.config.chat_ids or not isinstance(message.text, str):
            return
        try:
            self._command(chat, message.text)
        except ValueError as error:
            self.reply(chat, str(error))
        except Exception:
            logger.exception('Command failed in chat %s', chat)
            self.reply(chat, 'Command failed; no success was confirmed. Please retry.')

    def _command(self, chat, text):
        args = text.split()
        command = args[0].split('@', 1)[0].lower() if args else ''
        if command in ('/addwallet', '/removewallet', '/settag'):
            expected = 3 if command == '/settag' else 2
            if len(args) < expected or (expected == 2 and len(args) != 2):
                raise ValueError(f'Usage: {command} <wallet>' + (' <tag>' if expected == 3 else ''))
            wallet = normalize_wallet(args[1])
            tag = validate_tag(' '.join(args[2:])) if command == '/settag' else None
            with self.lock:
                proposed = self.accounts.copy()
                if command == '/addwallet':
                    if wallet in proposed:
                        self.reply(chat, f'Wallet {wallet} is already being tracked')
                        return
                    proposed[wallet] = Account()
                    response = f'Wallet {wallet} added'
                else:
                    if wallet not in proposed:
                        self.reply(chat, f'Wallet {wallet} is not being tracked')
                        return
                    if command == '/removewallet':
                        del proposed[wallet]
                        response = f'Wallet {wallet} removed'
                    else:
                        # Validate/save without changing live state on write failure.
                        proposed[wallet] = copy.copy(proposed[wallet])
                        proposed[wallet].tag = tag
                        response = f'Wallet {wallet} tag set to {tag}'
                save_wallets(proposed, self.config.wallets_file)
                self.accounts = proposed
                if command == '/removewallet':
                    self.outbox.forget_wallet(wallet)
            self.broadcast(response)
            return
        with self.lock:
            updated = datetime.fromtimestamp(self.last_cycle, MOSCOW).strftime('%Y-%m-%d %H:%M:%S') if self.last_cycle else 'not yet'
            lines = [f'Last monitoring cycle: {updated}', f'Pending notifications: {self.outbox.pending_count()}', '', 'Tracked wallets:']
            for wallet, account in self.accounts.items():
                last = datetime.fromtimestamp(account.last_updated, MOSCOW).strftime('%Y-%m-%d %H:%M:%S') if account.last_updated else 'not yet'
                tag = f' ({escape(account.tag)})' if account.tag else ''
                lines.append(f'<code>{escape(wallet)}</code>{tag}\n  Last success: {last}; failures: {account.failures}')
        # Keep each wallet and its HTML tags intact when splitting long lists.
        message = ''
        for line in lines:
            candidate = message + ('\n' if message else '') + line
            if len(candidate) > 1500:
                self.outbox.enqueue(chat, message, html=True)
                message = line
            else:
                message = candidate
        self.outbox.enqueue(chat, message, html=True)

    def poll_wallet(self, wallet, account):
        positions = self.client.fetch_open_positions(wallet)
        with self.lock:
            if self.accounts.get(wallet) is not account:
                return
            before = {position.ticker: position for position in account.positions or []}
            after = {position.ticker: position for position in positions}
            changed_coins = {coin for coin in before.keys() | after.keys() if before.get(coin) != after.get(coin)}
            changed = account.positions is not None and bool(changed_coins)
        trade = self.client.fetch_last_trade(wallet, coins=changed_coins) if changed else None
        if changed and trade is None:
            raise DataFetchError('Positions changed but no matching perpetual fill was found; retrying')
        with self.lock:
            if self.accounts.get(wallet) is not account:
                return
            if account.positions is None:
                account.positions = positions
            elif changed:
                if trade.timestamp < account.last_actions.get(TradeAction(trade.ticker, trade.action), 0):
                    raise DataFetchError('Received an older fill; retrying')
                account.update(trade, positions)
            # Requeue current state even if the API positions have not changed.
            # A failed enqueue must not turn a detected trade into a lost alert.
            if account.last_trade is not None:
                text = on_change_message(wallet, account)
                for chat in self.config.recipients:
                    self.outbox.enqueue(chat, text, key=f'wallet:{wallet}:{chat}',
                                        generation=account.generation, html=True)
            account.last_updated = time.time()
            account.failures = 0
            account.next_poll = time.monotonic() + self.config.poll_interval

    def monitor(self, heartbeat):
        self.client.heartbeat = heartbeat
        while not self.stop.is_set():
            heartbeat()
            with self.lock:
                accounts = list(self.accounts.items())
            for wallet, account in accounts:
                if self.stop.is_set():
                    return
                heartbeat()
                if time.monotonic() < account.next_poll:
                    continue
                try:
                    self.poll_wallet(wallet, account)
                except Exception as error:
                    with self.lock:
                        account.failures = min(account.failures + 1, 10)
                        account.next_poll = time.monotonic() + min(300, 2 ** account.failures)
                    logger.warning('Wallet %s check failed (%s): %s; retry scheduled',
                                   wallet, type(error).__name__, error)
                self.stop.wait(0.2)
            with self.lock:
                self.last_cycle = time.time()
            self.stop.wait(1)  # Also wait when there are no wallets.

    def polling(self, heartbeat):
        failures = 0
        while not self.stop.is_set():
            heartbeat()
            flood_until = float(self.outbox.get_metadata('telegram_flood_until', 0))
            if time.time() < flood_until:
                self.stop.wait(max(0, min(1, flood_until - time.time())))
                continue
            try:
                updates = self.bot.get_updates(offset=self.offset, timeout=30,
                                               long_polling_timeout=20, allowed_updates=['message'])
                for update in updates:
                    if self.stop.is_set():
                        return
                    heartbeat()
                    if update.message is not None:
                        self.handle_message(update.message)
                    self.offset = update.update_id + 1
                    self.outbox.set_metadata('telegram_offset', self.offset)
                failures = 0
            except Exception as error:
                failures = min(failures + 1, 8)
                result = getattr(error, 'result_json', None) or {}
                if not isinstance(result, dict):
                    result = {}
                try:
                    parameters = result.get('parameters')
                    delay = float(parameters.get('retry_after', min(60, 2 ** failures))
                                  if isinstance(parameters, dict) else min(60, 2 ** failures))
                    if not 0 < delay < float('inf'):
                        raise ValueError()
                except (TypeError, ValueError):
                    delay = 30
                if result.get('error_code') in (401, 409):
                    delay = max(delay, 60)
                if result.get('error_code') == 429:
                    self.outbox.defer_global(delay + 1)
                logger.warning('Telegram polling failed (%s, code=%s); retry in %.1fs',
                               type(error).__name__, result.get('error_code'), delay)
                # Long flood waits are healthy inactivity; keep heartbeats alive.
                deadline = time.monotonic() + delay + 1
                while not self.stop.is_set() and time.monotonic() < deadline:
                    heartbeat()
                    self.stop.wait(min(1, max(0, deadline - time.monotonic())))
            self.stop.wait(0.1)

    def run(self):
        supervisor = WorkerSupervisor(self.stop, self.config.watchdog_timeout)
        supervisor.add('monitor', self.monitor)
        supervisor.add('delivery', lambda beat: self.outbox.run(self.stop, beat))
        supervisor.add('polling', self.polling)
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
