"""Bounded portfolio requests, deduplication and background processing."""
from dataclasses import dataclass
import logging
import queue
import threading

from walletwatcher.hyperliquid.errors import DataFetchError
from walletwatcher.presentation.portfolio import portfolio_messages

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PortfolioRequest:
    """An explicit public address, or a /my request bound to saved credentials."""
    chat: int
    address: str
    user_id: int | None = None

    @property
    def command(self):
        return '/my' if self.user_id is not None else '/wallet'

    @property
    def pending_key(self):
        return self.user_id if self.user_id is not None else (self.chat, self.address)


class PortfolioService:
    def __init__(self, settings, client, credentials, outbox, stop, reply):
        self.settings, self.client = settings, client
        self.credentials, self.outbox = credentials, outbox
        self.stop, self.reply = stop, reply
        self.personal_queue = queue.Queue(maxsize=32)
        self.personal_pending = set()
        self.wallet_pending = set()
        self.personal_lock = threading.Lock()

    @property
    def config(self):
        return self.settings.config

    def pending_count(self):
        with self.personal_lock:
            return len(self.personal_pending) + len(self.wallet_pending)

    def enqueue(self, request):
        with self.personal_lock:
            pending = self.personal_pending if request.user_id is not None else self.wallet_pending
            if request.pending_key in pending:
                return  # Repeated requests share the queued or in-flight result.
            try:
                self.personal_queue.put_nowait(request)
            except queue.Full:
                self.reply(request.chat, f'Очередь запросов {request.command} заполнена. Повторите попытку позже.')
                return
            pending.add(request.pending_key)

    def process(self, timeout=0):
        try:
            request = self.personal_queue.get(timeout=timeout)
        except queue.Empty:
            return False
        try:
            if self.stop.is_set() or request.chat not in self.config.chat_ids:
                return True
            portfolio = self.client.fetch_portfolio(request.address)
            if self.stop.is_set() or request.chat not in self.config.chat_ids:
                return True
            if request.user_id is not None and self.credentials.get_address(request.user_id) != request.address:
                self.reply(request.chat, 'Данные аккаунта изменились. Повторите /my.')
            else:
                for text in portfolio_messages(portfolio):
                    self.outbox.enqueue(request.chat, text, html=True)
        except DataFetchError:
            logger.warning('Portfolio request %s failed for chat %s', request.command, request.chat)
            retry = '/my' if request.user_id is not None else f'/wallet {request.address}'
            if not self.stop.is_set() and request.chat in self.config.chat_ids:
                self.reply(request.chat, f'Не удалось получить свежие данные Hyperliquid. Повторите {retry} позже.')
        except Exception as error:
            logger.error('Portfolio request %s failed for chat %s (%s)', request.command, request.chat, type(error).__name__)
            if not self.stop.is_set() and request.chat in self.config.chat_ids:
                self.reply(request.chat, 'Команда не выполнена. Повторите попытку позже.')
        finally:
            with self.personal_lock:
                pending = self.personal_pending if request.user_id is not None else self.wallet_pending
                pending.discard(request.pending_key)
            self.personal_queue.task_done()
        return True

    def run(self, heartbeat):
        self.client.set_heartbeat(heartbeat)
        while not self.stop.is_set():
            heartbeat()
            self.process(timeout=0.2)
