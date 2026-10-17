"""Offline fixtures shared by behavioral tests; no real credentials are used."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from bot import Application
from config import Config
from delivery import DeliveryQueue
from models import Position, Trade

WALLET = '0x' + 'a' * 40
OTHER = '0x' + 'b' * 40


def position(size=1, ticker='BTC', **changes):
    values = dict(ticker=ticker, direction='Long' if size > 0 else 'Short',
                  leverage=5, leverage_type='cross', size=size, entry_price='100',
                  volume=abs(size) * 100, pnl=2.5)
    return Position(**(values | changes))


def trade(timestamp=1000, ticker='BTC', action='Open Long'):
    return Trade(ticker, '100', '1', action, timestamp)


class Clock:
    def __init__(self):
        self.now = 10000.0

    def __call__(self):
        return self.now

    def advance(self, seconds=16):
        self.now += seconds


class ApplicationFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = Config('12345:test', (1, 2), 1, 'PROD',
                             wallets_file=self.root / 'wallets.txt',
                             database_file=self.root / 'state.sqlite3')
        self.sender = Mock()
        self.sender.send_message.return_value = SimpleNamespace(message_id=7)
        self.client = Mock()
        self.clock = Clock()
        self.queue = DeliveryQueue(self.sender, self.config.database_file, clock=self.clock)
        self.addCleanup(self.queue.close)
        self.app = Application(self.config, bot=self.sender, client=self.client, outbox=self.queue)

    def jobs(self):
        return self.queue.db.execute('SELECT * FROM outbox ORDER BY rowid').fetchall()

    def deliver_pending(self):
        # Advance only a fake clock; unexpected retry loops fail promptly.
        for _ in range(100):
            if not self.queue.pending_count():
                return
            self.clock.advance()
            self.assertTrue(self.queue.deliver_once())
        self.fail('Pending notifications did not drain')

    def message(self, text, chat=1, user=99):
        return SimpleNamespace(text=text, chat=SimpleNamespace(id=chat),
                               from_user=SimpleNamespace(id=user))
