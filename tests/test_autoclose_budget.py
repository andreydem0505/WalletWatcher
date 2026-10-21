"""Regressions for an armed rule starved by monitoring requests."""
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from unittest.mock import Mock, patch
import requests

from support import ApplicationFixture, WALLET, OTHER
from test_autoclose import Exchange, KEY, fill
from walletwatcher.domain.models import Account
from walletwatcher.hyperliquid.client import DataFetchError, HyperliquidClient
from walletwatcher.trading.api import TradingAPI
from walletwatcher.trading.worker import AutocloseWorker


class BudgetTests(ApplicationFixture):
    def make_client(self, budget=1000):
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {}
        client = HyperliquidClient(session=session, clock=self.clock, weight_budget=budget)
        self.addCleanup(client.close)
        return client, session

    def monitor_request(self, client, session, operation):
        results = []
        def request():
            try:
                results.append(client._request({'type': operation}, session=session))
            except Exception as error:
                results.append(error)
        thread = threading.Thread(target=request)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        if isinstance(results[0], Exception):
            raise results[0]
        return results[0]

    def test_local_expensive_refusal_does_not_block_cheap_requests(self):
        client, session = self.make_client(100)
        for _ in range(4):
            client._reserve_weight('meta')
        with self.assertRaises(DataFetchError) as error:
            client._request({'type': 'userFills'})
        self.assertEqual(error.exception.reason, 'budget')
        self.assertEqual(client.blocked_until, 0)
        client._request({'type': 'clearinghouseState'})
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(client.calls[-1][1], 2)

    def test_monitor_can_use_the_entire_idle_budget_with_active_rules(self):
        client, session = self.make_client()
        client.reserve_for_trading(True)
        for _ in range(50):
            client._request({'type': 'meta'})
        self.assertEqual(sum(c[1] for c in client.calls), 1000)
        with self.assertRaises(DataFetchError):
            client._request({'type': 'clearinghouseState'})
        self.assertEqual(session.post.call_count, 50)
        self.clock.advance(60)
        client._request({'type': 'clearinghouseState'})
        self.assertEqual(session.post.call_count, 51)

    def test_both_priorities_share_the_full_budget_without_a_fixed_split(self):
        client, _ = self.make_client()
        client.reserve_for_trading(True)
        client.set_trading_priority(True)
        for _ in range(30):
            client._reserve_weight('meta')
        client.set_trading_priority(False)
        for _ in range(20):
            client._reserve_weight('meta')
        self.assertEqual(sum(c[1] for c in client.calls), 1000)
        client.set_trading_priority(True)
        with self.assertRaises(DataFetchError):
            client._request({'type': 'exchange'})

    def test_pending_trading_request_keeps_priority_and_monitor_uses_only_spare_weight(self):
        client, session = self.make_client(200)
        for _ in range(2):
            client._reserve_weight('meta')
        self.clock.advance(10)
        for _ in range(3):
            client._reserve_weight('meta')
        client.set_trading_priority(True)
        with self.assertRaises(DataFetchError) as error:
            client._request({'type': 'userFills'})
        self.assertEqual(error.exception.retry_after, 50)
        with self.assertRaises(DataFetchError):
            self.monitor_request(client, session, 'clearinghouseState')
        self.clock.advance(50)
        self.monitor_request(client, session, 'meta')  # 20 spare units, 120 protected.
        with self.assertRaises(DataFetchError):
            self.monitor_request(client, session, 'clearinghouseState')
        session.post.return_value.json.return_value = []
        client._request({'type': 'userFills'})
        self.assertFalse(client.trading_waiters)
        self.assertEqual(sum(c[1] for c in client.calls), 100)
        self.monitor_request(client, session, 'userRole')
        self.assertEqual(sum(c[1] for c in client.calls), 160)

    def test_smaller_trading_checks_do_not_clear_pending_expensive_request(self):
        client, session = self.make_client(200)
        for _ in range(5):
            client._reserve_weight('meta')
        client.set_trading_priority(True)
        client.set_trading_rule('first-rule')
        with self.assertRaises(DataFetchError):
            client._request({'type': 'userFills'})
        client.finish_trading_cycle({'first-rule', 'second-rule'})
        # No retry is due yet: the pending rule still has priority.
        client.set_trading_priority(True)
        client.finish_trading_cycle({'first-rule', 'second-rule'})
        with self.assertRaises(DataFetchError):
            self.monitor_request(client, session, 'clearinghouseState')
        client.set_trading_priority(True)
        client.set_trading_rule('first-rule')
        client._request({'type': 'clearinghouseState'})
        with self.assertRaises(DataFetchError):
            client._request({'type': 'userFills'})
        client.finish_trading_cycle({'first-rule', 'second-rule'})
        with self.assertRaises(DataFetchError):
            self.monitor_request(client, session, 'clearinghouseState')
        # The rule no longer needs history (for example it was cancelled).
        client.set_trading_priority(True)
        client.set_trading_rule('second-rule')
        client._request({'type': 'clearinghouseState'})
        client.finish_trading_cycle({'second-rule'})
        self.assertFalse(client.trading_waiters)
        self.monitor_request(client, session, 'meta')

    def test_different_pending_operations_reserve_largest_weight_not_the_sum(self):
        client, session = self.make_client(200)
        for _ in range(8):
            client._reserve_weight('meta')
        client.set_trading_priority(True)
        for operation in ('userRole', 'userFillsByTime'):
            with self.assertRaises(DataFetchError):
                client._request({'type': operation})
        self.clock.advance(60)
        for _ in range(4):
            self.monitor_request(client, session, 'meta')
        with self.assertRaises(DataFetchError):
            self.monitor_request(client, session, 'clearinghouseState')
        session.post.return_value.json.return_value = []
        client._request({'type': 'userFillsByTime'})
        client._request({'type': 'userRole'})
        self.assertFalse(client.trading_waiters)
        self.assertEqual(sum(c[1] for c in client.calls), 160)

    def test_impossible_trading_request_does_not_starve_monitoring(self):
        client, session = self.make_client(100)
        client.set_trading_priority(True)
        with self.assertRaises(DataFetchError):
            client._request({'type': 'userFills'})
        self.assertFalse(client.trading_waiters)
        self.monitor_request(client, session, 'meta')

    def test_another_rules_success_does_not_release_pending_demand(self):
        client, session = self.make_client(200)
        for _ in range(5):
            client._reserve_weight('meta')
        client.set_trading_priority(True)
        client.set_trading_rule('first-rule')
        with self.assertRaises(DataFetchError):
            client._request({'type': 'userFills'})
        self.clock.advance(60)
        client.set_trading_rule('second-rule')
        session.post.return_value.json.return_value = []
        client._request({'type': 'userFills'})  # Actual weight is 20.
        client.finish_trading_cycle({'first-rule', 'second-rule'})
        for _ in range(3):
            self.monitor_request(client, session, 'meta')
        with self.assertRaises(DataFetchError):
            self.monitor_request(client, session, 'clearinghouseState')
        client.set_trading_rule('first-rule')
        client._request({'type': 'userFills'})
        self.assertFalse(client.trading_waiters)

    def test_parallel_monitor_requests_cannot_consume_pending_trading_capacity(self):
        client, session = self.make_client(200)
        for _ in range(5):
            client._reserve_weight('meta')
        client.set_trading_priority(True)
        with self.assertRaises(DataFetchError):
            client._request({'type': 'userFills'})
        self.clock.advance(60)
        barrier = threading.Barrier(8)
        def monitor(_):
            barrier.wait(2)
            try:
                client._request({'type': 'meta'}, session=session)
                return True
            except DataFetchError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(monitor, range(8))), 4)
        self.assertEqual(sum(c[1] for c in client.calls), 80)
        session.post.return_value.json.return_value = [None] * 2000
        client._request({'type': 'userFills'})
        self.assertEqual(sum(c[1] for c in client.calls), 200)
        self.assertFalse(client.trading_waiters)

    def test_retry_after_waits_until_sufficient_weight_expires(self):
        client, _ = self.make_client(100)
        client._reserve_weight('meta')
        self.clock.advance(10)
        for _ in range(4):
            client._reserve_weight('meta')
        with self.assertRaises(DataFetchError) as error:
            client._request({'type': 'userRole'})
        self.assertEqual(error.exception.retry_after, 60)
        self.clock.advance(50)
        with self.assertRaises(DataFetchError):
            client._request({'type': 'userRole'})
        self.clock.advance(10)
        client._request({'type': 'userRole'})

    def test_real_http_429_still_blocks_both_priorities(self):
        client, session = self.make_client()
        session.post.return_value.status_code = 429
        session.post.return_value.headers = {'Retry-After': '60'}
        session.post.return_value.raise_for_status.side_effect = requests.HTTPError()
        with self.assertRaises(DataFetchError):
            client._request({'type': 'meta'})
        client.set_trading_priority(True)
        with self.assertRaises(DataFetchError) as error:
            client._request({'type': 'clearinghouseState'})
        self.assertEqual(error.exception.reason, 'rate_limit')
        self.assertEqual(session.post.call_count, 1)

    def test_reserve_is_released_when_there_are_no_rules(self):
        client, session = self.make_client(200)
        client.reserve_for_trading(True)
        for _ in range(5):
            client._reserve_weight('meta')
        client.set_trading_priority(True)
        with self.assertRaises(DataFetchError):
            client._request({'type': 'userFills'})
        with self.assertRaises(DataFetchError):
            self.monitor_request(client, session, 'meta')
        client.reserve_for_trading(False)
        self.monitor_request(client, session, 'meta')
        self.assertFalse(client.trading_waiters)
        self.assertEqual(sum(c[1] for c in client.calls), 120)

    def test_disabling_priority_releases_that_workers_pending_demand(self):
        client, session = self.make_client(200)
        for _ in range(5):
            client._reserve_weight('meta')
        client.set_trading_priority(True)
        with self.assertRaises(DataFetchError):
            client._request({'type': 'userFills'})
        client.set_trading_priority(False)
        self.assertFalse(client.trading_waiters)
        client._request({'type': 'meta'})
        self.assertEqual(sum(c[1] for c in client.calls), 120)


class ArmedRuleTests(ApplicationFixture):
    def setUp(self):
        super().setUp()
        self.app.accounts[WALLET] = Account()
        self.app.credentials.save(1, OTHER, KEY)
        self.store = self.app.autoclose_store
        self.exchange = Exchange(self.clock)
        self.worker = AutocloseWorker(self.app, self.store, self.exchange, self.clock)
        self.rule = self.store.create(1, WALLET, 'xyz:SPCX', self.app.credentials.version(1))
        self.exchange.market_value = self.exchange.market('xyz:SPCX')
        self.worker.cycle()

    def test_source_and_original_lifecycle_are_checked_every_poll(self):
        checkpoint = self.store.active()[0]['checkpoint']
        with patch.object(self.exchange, 'fills', wraps=self.exchange.fills) as history:
            for _ in range(5):
                self.clock.advance(5)
                self.worker.cycle()
            self.assertEqual(history.call_count, 5)
        self.assertGreater(self.store.active()[0]['checkpoint'], checkpoint)

    def test_full_close_triggers_history_check_immediately_before_30_seconds(self):
        self.exchange.source = Decimal(0)
        self.clock.advance(5)
        with patch.object(self.exchange, 'fills', wraps=self.exchange.fills) as history:
            self.worker.cycle()
        self.assertEqual(len(self.exchange.orders), 1)
        self.assertEqual(history.call_count, 2)
        self.assertTrue(history.call_args.kwargs['fresh'])

    def test_same_size_close_reopen_is_rejected_on_source_trigger(self):
        self.clock.advance(1)
        self.exchange.trade(10)
        self.exchange.trade(10, 'B')
        self.exchange.source = Decimal(0)
        self.clock.advance(1)
        self.worker.cycle()
        self.assertFalse(self.exchange.orders)
        self.assertFalse(self.store.active())
        self.assertIn('reopened position', self.jobs()[-1]['text'])

    def test_same_size_close_reopen_is_found_on_next_poll_without_source_trigger(self):
        self.clock.advance(1)
        self.exchange.trade(10)
        self.exchange.trade(10, 'B')
        self.clock.advance(1)
        self.worker.cycle()
        self.assertFalse(self.store.active())
        self.assertFalse(self.exchange.orders)

    def test_source_failure_is_persisted_and_notifies_immediately_without_repeats(self):
        self.exchange.source = Decimal(0)
        original = self.exchange.position
        error = DataFetchError(KEY, reason='budget', operation='clearinghouseState', retry_after=60)
        def position(address, market, fresh=False):
            if address == WALLET:
                raise error
            return original(address, market, fresh)
        before = len(self.jobs())
        with patch.object(self.exchange, 'position', side_effect=position), \
                self.assertLogs('walletwatcher.trading.worker', level='WARNING') as logs:
            for index in range(8):
                self.clock.advance(5)
                self.worker.cycle()
                if index == 0:
                    self.assertEqual(len(self.jobs()), before + 1)
        self.assertNotIn(KEY, ''.join(logs.output))
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(len(self.jobs()), before + 1)
        self.assertIn('checks are delayed', self.jobs()[-1]['text'])
        self.assertIn('tracked wallet', self.jobs()[-1]['text'])
        rule = self.store.active()[0]
        self.assertEqual(rule['attempts'], 0)
        self.assertIn('bot request budget', rule['last_check_error']['detail'])
        self.assertNotIn(KEY, json.dumps(rule))
        self.clock.advance(1)
        self.exchange.trade(10)
        self.worker.cycle()
        self.assertFalse(self.store.active())
        self.assertIn('your original position was closed or reversed', self.jobs()[-1]['text'])

    def test_recovery_closes_without_widening_price_or_duplicate_submission(self):
        self.exchange.source = Decimal(0)
        error = DataFetchError('network', reason='timeout')
        with patch.object(self.exchange, 'authority', side_effect=error):
            self.clock.advance(5)
            self.worker.cycle()
        rule = self.store.active()[0]
        self.assertEqual(rule['last_check_error']['stage'], 'checking trading permissions')
        self.clock.advance(5)
        self.worker.cycle()
        self.assertEqual(len(self.exchange.orders), 1)
        self.assertNotIn('check_error_since', self.store.active()[0])
        self.clock.advance(5)
        self.worker.cycle()
        self.assertFalse(self.store.active())
        self.assertEqual(len(self.exchange.orders), 1)
        self.assertIn('market close confirmed', self.jobs()[-1]['text'])

    def test_immediate_notification_and_state_commit_atomically(self):
        error = DataFetchError('network')
        rule = self.store.active()[0]
        with patch.object(self.queue, 'enqueue_transaction', side_effect=RuntimeError('full')):
            with self.assertRaises(RuntimeError):
                self.worker.check_failed(rule, error)
        self.assertNotIn('check_delay_notified', self.store.active()[0])
        self.worker.check_failed(self.store.active()[0], error)
        self.assertTrue(self.store.active()[0]['check_delay_notified'])


    def test_preflight_refusal_does_not_create_unknown_order_and_waits_for_budget(self):
        self.exchange.source = Decimal(0)
        self.clock.advance(1)
        error = DataFetchError('busy', reason='budget', retry_after=60, submitted=False)
        with patch.object(self.exchange, 'send', side_effect=error):
            self.worker.cycle()
        rule = self.store.active()[0]
        self.assertEqual(rule['attempts'], 1)
        self.assertNotIn('pending', rule)
        self.assertFalse(self.store.unresolved())
        attempt = json.loads(self.queue.db.execute('SELECT data FROM autoclose_attempts').fetchone()[0])
        self.assertTrue(attempt['not_submitted'])
        self.clock.advance(5)
        with patch.object(self.exchange, 'authority') as auth:
            self.worker.cycle()
            auth.assert_not_called()
        self.assertFalse(self.exchange.orders)
        self.clock.advance(55)
        self.worker.cycle()
        self.assertEqual(len(self.exchange.orders), 1)
        self.clock.advance(5)
        self.worker.cycle()
        self.assertFalse(self.store.active())
        self.assertIn('market close confirmed', self.jobs()[-1]['text'])


class HistorySharingTests(ApplicationFixture):
    def test_saved_fill_replaces_full_recent_history_with_checked_incremental_reads(self):
        client = Mock()
        api = TradingAPI(client)
        self.addCleanup(api.close)
        old = fill(start='0', size='10', side='B', stamp=1, tid=10)
        client._request.side_effect = [[old], [old], [old], [old]]
        initial = api.fills(OTHER, 10, 10)
        rule = dict(own_size='10', checkpoint=10, ticker='BTC')
        from walletwatcher.trading.api import checkpoint
        checkpoint(rule, Decimal(10), 10, initial)
        self.assertEqual(rule['history_anchor'], {'time': 1, 'coin': 'BTC', 'tid': '10'})
        for end in (20, 30):
            api.begin_cycle()
            values = api.fills(OTHER, rule['checkpoint'], end, anchor=rule['history_anchor'])
            checkpoint(rule, Decimal(10), end, values)
        types = [call.args[0]['type'] for call in client._request.call_args_list]
        self.assertEqual(types.count('userFills'), 1)
        self.assertEqual(types.count('userFillsByTime'), 3)
        self.assertEqual(client._request.call_args.args[0]['startTime'], 1)

    def test_pruned_anchor_is_rejected_even_if_net_position_size_is_unchanged(self):
        from walletwatcher.trading.api import TradingError
        client = Mock()
        api = TradingAPI(client)
        self.addCleanup(api.close)
        client._request.return_value = []
        anchor = dict(time=1, coin='BTC', tid='10')
        with self.assertRaisesRegex(TradingError, 'saved fill is missing'):
            api.fills(OTHER, 10, 20, anchor=anchor)
        self.assertEqual(client._request.call_count, 1)
        self.assertEqual(client._request.call_args.args[0]['type'], 'userFillsByTime')
        with self.assertRaisesRegex(TradingError, 'newer than'):
            api.fills(OTHER, 0, 20, anchor=anchor)
        client._request.return_value = [fill(stamp=2, tid=10)]
        with self.assertRaisesRegex(TradingError, 'missing or changed'):
            api.fills(OTHER, 10, 20, anchor=anchor)

    def test_history_anchor_survives_database_reopen_without_bootstrap_reload(self):
        from walletwatcher.storage.autoclose import AutocloseStore
        from walletwatcher.trading.api import FillHistory, checkpoint
        anchor = dict(time=1, coin='BTC', tid='10')
        rule = self.app.autoclose_store.create(1, WALLET, 'BTC', 'test-version')
        rule['ticker'] = 'BTC'
        checkpoint(rule, Decimal(10), 10, FillHistory([], anchor))
        self.app.autoclose_store.save(rule)
        restored = AutocloseStore(self.queue, '12345').active()[0]
        client = Mock()
        client._request.return_value = [fill(stamp=1, tid=10)]
        api = TradingAPI(client)
        self.addCleanup(api.close)
        api.fills(OTHER, 10, 20, anchor=restored['history_anchor'])
        self.assertEqual(client._request.call_count, 1)
        self.assertEqual(client._request.call_args.args[0]['type'], 'userFillsByTime')

    def test_final_same_millisecond_history_refresh_catches_close_reopen(self):
        from walletwatcher.trading.api import replay
        old = fill(stamp=1, tid=10, coin='ETH')
        closed = fill(stamp=10, tid=11)
        reopened = fill(start='0', side='B', stamp=10, tid=12)
        anchor = dict(time=1, coin='ETH', tid='10')
        client = Mock()
        client._request.side_effect = [[old], [old, closed, reopened]]
        api = TradingAPI(client)
        self.addCleanup(api.close)
        rule = dict(ticker='BTC', own_size='10', checkpoint=10, boundary=[])
        first = api.fills(OTHER, 10, 10, anchor=anchor)
        self.assertFalse(replay(rule, Decimal(10), 10, first))
        cached = api.fills(OTHER, 10, 10, anchor=anchor)
        self.assertFalse(replay(rule, Decimal(10), 10, cached))
        fresh = api.fills(OTHER, 10, 10, anchor=anchor, fresh=True)
        self.assertTrue(replay(rule, Decimal(10), 10, fresh))
        self.assertEqual(client._request.call_count, 2)

    def test_same_snapshot_is_shared_but_fresh_validation_bypasses_cache(self):
        client = Mock()
        api = TradingAPI(client)
        self.addCleanup(api.close)
        client._request.return_value = []
        self.assertEqual(api.fills(OTHER, 1, 2), [])
        self.assertEqual(client._request.call_count, 2)
        api.fills(OTHER, 1, 2)
        self.assertEqual(client._request.call_count, 2)
        api.fills(OTHER, 1, 2, fresh=True)
        self.assertEqual(client._request.call_count, 4)
        api.begin_cycle()
        api.fills(OTHER, 1, 2)
        self.assertEqual(client._request.call_count, 6)
