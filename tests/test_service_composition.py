"""Services share live settings and autoclose operates without an Application."""
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import queue
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock

from support import ApplicationFixture, Clock, OTHER, WALLET
from test_autoclose import Exchange, KEY
from walletwatcher.domain.credentials import Credentials
from walletwatcher.domain.models import Account
from walletwatcher.services.portfolios import PortfolioRequest
from walletwatcher.storage.autoclose import AutocloseStore
from walletwatcher.storage.delivery import DeliveryQueue
from walletwatcher.trading.ports import CredentialReader, RuleAccess, TradingRequests
from walletwatcher.trading.worker import AutocloseWorker


class ServiceCompositionTests(ApplicationFixture):
    def test_compatibility_facade_does_not_duplicate_mutable_service_state(self):
        self.app.accounts = {WALLET: Account()}
        self.assertIs(self.app.accounts, self.app.monitoring.accounts)
        self.assertIs(self.app.lock, self.app.monitoring.lock)
        self.app.last_cycle = 1000
        self.assertEqual(self.app.monitoring.last_cycle, 1000)
        self.assertEqual(self.app.last_cycle, 1000)
        self.app.started_at = 42
        self.assertEqual(self.app.started_at, self.app.diagnostics.started_at)
        self.assertEqual(self.app.started_at, 42)
        self.app.supervisor = Mock()
        self.assertIs(self.app.supervisor, self.app.diagnostics.supervisor)
        self.app.offset = 17
        self.assertEqual(self.app.commands.offset, 17)
        self.assertEqual(self.app.offset, 17)
        self.app.personal_queue = queue.Queue(maxsize=1)
        self.assertIs(self.app.personal_queue, self.app.portfolios.personal_queue)
        self.assertIs(self.app.personal_lock, self.app.portfolios.personal_lock)
        self.app._queue_portfolio(PortfolioRequest(1, WALLET))
        self.assertEqual(self.app.portfolios.pending_count(), 1)

    def test_new_access_configuration_and_wallet_collection_are_observed_live(self):
        access = self.app.autoclose_access
        self.app.accounts = {WALLET: Account()}
        self.assertTrue(access.tracks_wallet(WALLET))
        self.assertTrue(access.allows_user(1))
        self.app.config = replace(self.config, chat_ids=(2,), poll_interval=8)
        self.assertFalse(access.allows_user(1))
        self.assertEqual(access.poll_interval, 8)
        for service in (self.app.monitoring, self.app.portfolios, self.app.commands, self.app.diagnostics):
            self.assertIs(service.config, self.app.config)
        self.app.accounts = {}
        self.assertFalse(access.tracks_wallet(WALLET))

    def test_client_injection_remains_shared_by_request_workers(self):
        replacement = Mock()
        self.app.client = replacement
        self.assertIs(self.app.client, replacement)
        for service in (self.app.monitoring, self.app.portfolios, self.app.commands, self.app.diagnostics):
            self.assertIs(service.client, replacement)
        self.assertIs(self.app.autoclose.requests, replacement)


class StandaloneAutocloseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.clock = Clock()
        self.outbox = DeliveryQueue(Mock(), Path(directory.name) / 'state.sqlite3', clock=self.clock)
        self.addCleanup(self.outbox.close)
        self.store = AutocloseStore(self.outbox, 'test')
        self.exchange = Exchange(self.clock)
        self.credentials = Mock(spec=CredentialReader)
        self.credentials.version.return_value = 'version'
        self.credentials.load.return_value = Credentials(OTHER, KEY)
        self.access = Mock(spec=RuleAccess)
        self.access.poll_interval = 5
        self.access.allows_user.return_value = True
        self.access.tracks_wallet.return_value = True
        self.requests = Mock(spec=TradingRequests)
        self.worker = AutocloseWorker(
            self.store, self.exchange, credentials=self.credentials, access=self.access,
            requests=self.requests, stop=Event(), clock=self.clock)
        self.rule = self.store.create(1, WALLET, 'BTC', 'version')

    def state(self):
        row = self.outbox.db.execute('SELECT data FROM autoclose_rules WHERE id=?', (self.rule['id'],)).fetchone()
        return json.loads(row[0])

    def test_full_close_is_confirmed_and_notified_once_without_application(self):
        self.worker.cycle()
        self.assertEqual(self.state()['state'], 'armed')
        self.exchange.source = Decimal(9)
        self.clock.advance(5)
        self.worker.cycle()
        self.assertEqual(self.state()['state'], 'closing')
        order = self.exchange.orders[0]['order']
        self.assertEqual((order['b'], order['s'], order['r'], order['t']),
                         (False, '10', True, {'limit': {'tif': 'Ioc'}}))
        self.clock.advance(5)
        self.worker.cycle()
        self.assertEqual(self.state()['state'], 'done')
        self.worker.cycle()
        self.assertEqual(len(self.exchange.orders), 1)
        notifications = self.outbox.db.execute(
            "SELECT chat, text FROM outbox WHERE key LIKE 'autoclose:%'").fetchall()
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0][0], 1)
        self.assertIn('market close confirmed', notifications[0][1])

    def test_live_access_revocation_prevents_submission_and_notification(self):
        self.worker.cycle()
        before = self.outbox.pending_count()
        self.access.allows_user.return_value = False
        self.exchange.source = Decimal(0)
        self.clock.advance(5)
        self.worker.cycle()
        self.assertEqual(self.state()['state'], 'cancelled')
        self.assertFalse(self.exchange.orders)
        self.assertEqual(self.outbox.pending_count(), before)
