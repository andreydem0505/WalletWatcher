"""Upcoming autoclose checks retain capacity without a fixed quota partition."""
import threading
import unittest
from unittest.mock import Mock, patch

from support import ApplicationFixture, Clock, WALLET, OTHER
from test_autoclose import Exchange, KEY
from walletwatcher.domain.models import Account
from walletwatcher.hyperliquid.errors import DataFetchError
from walletwatcher.hyperliquid.rate_limit import RequestBudget
from walletwatcher.storage.autoclose import AutocloseStore
from walletwatcher.trading.worker import AutocloseWorker


class ScheduledBudgetTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.budget = RequestBudget(clock=self.clock)

    def ordinary(self, operation='meta'):
        # The forecast belongs to the autoclose thread, traffic comes from others.
        results = []
        def reserve():
            try:
                results.append(self.budget.reserve(operation))
            except DataFetchError as error:
                results.append(error)
        thread = threading.Thread(target=reserve)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        return results[0]

    def test_full_check_fits_after_monitoring_uses_all_spare_capacity(self):
        self.budget.schedule_trading_check(124, 5)
        for _ in range(43):
            self.assertIsInstance(self.ordinary(), list)
        for _ in range(8):
            self.assertIsInstance(self.ordinary('clearinghouseState'), list)
        before = list(self.budget.calls)
        error = self.ordinary('clearinghouseState')
        self.assertEqual(error.reason, 'trading_capacity')
        self.assertFalse(error.submitted)
        self.assertEqual(list(self.budget.calls), before)
        self.clock.advance(5)
        self.budget.set_trading_priority(True)
        for operation in ('clearinghouseState', 'clearinghouseState', 'userFillsByTime'):
            self.budget.reserve(operation)
        self.assertEqual(sum(call[1] for call in self.budget.calls), 1000)
        self.assertEqual(self.budget.blocked_until, 0)

    def test_expiring_capacity_is_available_to_monitoring_before_deadline(self):
        for _ in range(40):
            self.budget.reserve('meta')  # 800 units expire at the check deadline.
        self.clock.advance(55)
        self.budget.schedule_trading_check(124, 5)
        for _ in range(10):
            self.assertIsInstance(self.ordinary(), list)
        # The real current total is still enforced, even when forecast fits.
        self.assertEqual(self.ordinary().reason, 'budget')
        self.clock.advance(5)
        self.budget.set_trading_priority(True)
        self.budget.reserve('userFillsByTime')
        self.assertEqual(sum(call[1] for call in self.budget.calls), 320)

    def test_future_and_overdue_deadlines(self):
        for _ in range(45):
            self.budget.reserve('meta')
        self.budget.schedule_trading_check(124, 60)
        self.assertIsInstance(self.ordinary(), list)  # New call expires in time.
        self.budget.schedule_trading_check(124, 5)
        self.clock.advance(6)
        self.assertEqual(self.ordinary().reason, 'trading_capacity')
        self.clock.advance(54)
        self.assertIsInstance(self.ordinary(), list)

    def test_cancellation_and_worker_exit_release_forecast(self):
        for release in (lambda: self.budget.schedule_trading_check(0, 0),
                        lambda: self.budget.reserve_for_trading(False),
                        lambda: self.budget.set_trading_priority(False)):
            with self.subTest(release=release):
                self.budget.schedule_trading_check(124, 5)
                self.assertTrue(self.budget.scheduled_checks)
                release()
                self.assertFalse(self.budget.scheduled_checks)

    def test_forecast_does_not_bypass_exchange_cooldown_or_close_priority(self):
        self.budget.schedule_trading_check(124, 5)
        self.budget.set_close_priority('closing', True)
        self.assertEqual(self.ordinary().reason, 'trading_priority')
        self.budget.set_close_priority('closing', False)
        self.budget.defer(30)
        self.assertEqual(self.ordinary().reason, 'rate_limit')
        self.budget.set_trading_priority(True)
        with self.assertRaises(DataFetchError) as error:
            self.budget.reserve('userFillsByTime')
        self.assertEqual(error.exception.reason, 'rate_limit')

    def test_heavy_monitoring_cannot_starve_five_second_checks(self):
        self.budget.set_trading_priority(True)
        pauses = 0
        for _ in range(48):  # Four minutes, much more traffic than can fit.
            self.budget.begin_trading_cycle()
            self.budget.schedule_trading_check(124, 5, interval=5, recurring=25)
            for _ in range(12):  # Twelve wallets compete for fill/state requests.
                for operation in ('clearinghouseState', 'userFills'):
                    result = self.ordinary(operation)
                    if isinstance(result, DataFetchError):
                        # Expiry can free future capacity while today's total
                        # still prevents admitting this ordinary request now.
                        self.assertIn(result.reason, ('trading_capacity', 'budget'))
                        pauses += 1
                    elif operation == 'userFills':
                        self.budget.record_response(result, operation, [None] * 20)
            self.clock.advance(5)
            for operation in ('clearinghouseState', 'clearinghouseState'):
                self.budget.reserve(operation)
            reservation = self.budget.reserve('userFillsByTime')
            self.budget.record_response(reservation, 'userFillsByTime', [None])
            self.assertLessEqual(sum(call[1] for call in self.budget.calls), 1000)
        self.assertGreater(pauses, 0)

    def test_forecast_tracks_actual_response_weight_and_releases_unused_capacity(self):
        self.budget.set_trading_priority(True)
        self.budget.begin_trading_cycle()
        self.budget.reserve('clearinghouseState')
        self.budget.reserve('clearinghouseState')
        page = self.budget.reserve('userFillsByTime')
        self.budget.record_response(page, 'userFillsByTime', [None] * 400)
        self.budget.schedule_trading_check(124, 5, interval=5, recurring=25)
        # 44 observed units plus a 99-unit fill reservation margin.
        self.assertEqual(next(iter(self.budget.scheduled_checks.values()))[1:], (143, 5, 44))
        self.budget.begin_trading_cycle()
        self.budget.schedule_trading_check(124, 0, interval=5, recurring=25)
        self.assertEqual(next(iter(self.budget.scheduled_checks.values()))[1:], (143, 5, 44))
        self.clock.advance(5)
        self.budget.reserve('clearinghouseState')
        self.budget.reserve('clearinghouseState')
        page = self.budget.reserve('userFillsByTime')
        self.budget.record_response(page, 'userFillsByTime', [None])
        self.budget.schedule_trading_check(124, 5, interval=5, recurring=25)
        self.assertEqual(next(iter(self.budget.scheduled_checks.values()))[1:], (124, 5, 25))


class WaitingBudgetNoticeTests(ApplicationFixture):
    def setUp(self):
        super().setUp()
        self.app.accounts[WALLET] = Account()
        self.app.credentials.save(1, OTHER, KEY)
        self.store = self.app.autoclose_store
        self.exchange = Exchange(self.clock)
        self.worker = AutocloseWorker(self.app, self.store, self.exchange, self.clock)
        self.store.create(1, WALLET, 'BTC', self.app.credentials.version(1))
        self.worker.cycle()

    def fail_history(self, reason='budget'):
        error = DataFetchError('safe test failure', reason=reason, operation='userFillsByTime')
        with patch.object(self.exchange, 'fills', side_effect=error):
            self.worker.cycle()

    def test_successful_poll_does_not_rearm_same_budget_warning(self):
        before = len(self.jobs())
        for _ in range(4):
            self.fail_history()
            self.clock.advance(5)
            self.worker.cycle()
            self.clock.advance(5)
        self.assertEqual(len(self.jobs()), before + 1)
        self.assertIn('No close order has been submitted', self.jobs()[-1]['text'])
        self.assertTrue(self.store.active())
        self.assertFalse(self.exchange.orders)
        self.clock.advance(60)
        self.fail_history()
        self.assertEqual(len(self.jobs()), before + 2)

    def test_budget_incident_survives_restart_but_other_failures_notify(self):
        self.fail_history()
        before = len(self.jobs())
        self.clock.advance(5)
        self.worker.cycle()
        store = AutocloseStore(self.queue, '12345')
        self.worker = AutocloseWorker(self.app, store, self.exchange, self.clock)
        self.fail_history()
        self.assertEqual(len(self.jobs()), before)
        self.fail_history('timeout')
        self.assertEqual(len(self.jobs()), before + 1)
        self.assertIn('timed out', self.jobs()[-1]['text'])

    def test_source_trigger_escalates_repeated_budget_error_immediately(self):
        self.fail_history()
        before = len(self.jobs())
        self.clock.advance(5)
        self.worker.cycle()
        self.exchange.source = 0
        self.fail_history()
        self.assertEqual(len(self.jobs()), before + 1)
        self.assertIn('Position closure is not confirmed', self.jobs()[-1]['text'])
        self.assertTrue(self.store.active()[0]['close_priority'])
        self.assertFalse(self.exchange.orders)

    def test_new_check_stage_is_reported_immediately(self):
        self.fail_history()
        before = len(self.jobs())
        with patch.object(self.exchange, 'position', side_effect=DataFetchError('busy', reason='budget')):
            self.worker.cycle()
        self.assertEqual(len(self.jobs()), before + 1)
        self.assertIn('reading your trading account position', self.jobs()[-1]['text'])

    def test_closing_errors_are_not_suppressed_by_the_waiting_budget_window(self):
        self.exchange.source = 0
        before = len(self.jobs())
        self.fail_history()
        # A successful check reopens notifications, without submitting an order.
        with patch.object(self.worker, 'step'):
            self.worker.process_rule(self.store.active()[0])
        self.fail_history()
        self.assertEqual(len(self.jobs()), before + 2)

    def test_budget_notice_transaction_failure_leaves_no_suppression_marker(self):
        with patch.object(self.queue, 'enqueue_transaction', side_effect=RuntimeError('full')):
            with self.assertRaises(RuntimeError):
                self.fail_history()
        self.assertNotIn('budget_notice', self.store.active()[0])
        before = len(self.jobs())
        self.fail_history()
        self.assertEqual(len(self.jobs()), before + 1)

    def test_worker_forecasts_complete_checks_with_and_without_anchor(self):
        rule = self.store.active()[0]
        self.app.client.schedule_trading_check.reset_mock()
        self.worker.schedule_next_check([rule], 5)
        self.app.client.schedule_trading_check.assert_called_once_with(244, 5, interval=5, recurring=44)
        rule['history_anchor'] = {'time': 1, 'coin': 'BTC', 'tid': '1'}
        self.worker.schedule_next_check([rule], 5)
        self.app.client.schedule_trading_check.assert_called_with(124, 5, interval=5, recurring=25)
        rule['state'] = 'closing'
        self.worker.schedule_next_check([rule], 5)
        self.app.client.schedule_trading_check.assert_called_with(0, 5, interval=5, recurring=0)
        rule['state'] = 'armed'
        self.store.cancel(1)
        self.worker.schedule_next_check([rule], 5)
        self.app.client.schedule_trading_check.assert_called_with(0, 5, interval=5, recurring=0)

    def test_monitor_capacity_pause_does_not_increment_failures_or_backoff(self):
        account = self.app.accounts[WALLET]
        account.failures = 4
        account.next_poll = 0
        self.app.client.fetch_open_positions.side_effect = DataFetchError('reserved', reason='trading_capacity')
        def wait(delay):
            self.app.stop.set()
            return True
        with patch.object(self.app.stop, 'wait', side_effect=wait):
            self.app.monitor(Mock())
        self.assertEqual(account.failures, 4)
        self.assertEqual(account.next_poll, 0)
