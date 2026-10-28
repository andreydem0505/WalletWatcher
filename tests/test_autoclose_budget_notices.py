"""One durable warning per recurring budget failure, including close preflight."""
from unittest.mock import patch

from support import ApplicationFixture, OTHER, WALLET
from test_autoclose import Exchange, KEY
from walletwatcher.domain.models import Account
from walletwatcher.hyperliquid.errors import DataFetchError
from walletwatcher.storage.autoclose import AutocloseStore
from walletwatcher.trading.worker import AutocloseWorker


class BudgetNoticeTests(ApplicationFixture):
    def setUp(self):
        super().setUp()
        self.app.accounts[WALLET] = Account()
        self.app.credentials.save(1, OTHER, KEY)
        self.store = self.app.autoclose_store
        self.exchange = Exchange(self.clock)
        self.worker = self.make_worker()
        self.rule = self.store.create(1, WALLET, 'BTC', self.app.credentials.version(1))
        self.worker.cycle()

    def make_worker(self):
        return AutocloseWorker(
            self.store, self.exchange, credentials=self.app.credentials,
            access=self.app.autoclose_access, requests=self.app.client,
            stop=self.app.stop, clock=self.clock)

    def fail(self, stage='verifying fills before submission', *, closing=True, reason='budget'):
        rule = self.store.active()[0]
        rule.update(check_stage=stage, close_priority=closing)
        self.worker.check_failed(rule, DataFetchError(
            KEY, reason=reason, operation='userFillsByTime', retry_after=30))

    def notices(self):
        return [job for job in self.jobs() if 'checks are delayed' in job['text']]

    def test_alternating_stages_report_each_stage_once_during_a_continuous_shortage(self):
        for _ in range(12):
            self.fail('checking your position fill history')
            self.fail()
            self.clock.advance(5)
        self.assertEqual(len(self.notices()), 2)
        self.assertEqual(self.notices()[0]['chat'], 1)
        self.assertNotIn(KEY, str([dict(job) for job in self.notices()]))
        self.assertTrue(self.store.active())
        self.assertFalse(self.exchange.orders)

    def test_waiting_stage_alternation_also_does_not_repeat_old_warnings(self):
        for _ in range(4):
            self.fail('reading your trading account position', closing=False)
            self.fail(closing=False)
        self.assertEqual(len(self.notices()), 2)

    def test_other_error_is_immediate_but_does_not_rearm_the_previous_budget_warning(self):
        self.fail()
        self.fail(reason='timeout')
        self.assertEqual(len(self.notices()), 2)
        self.assertIn('timed out', self.notices()[-1]['text'])
        self.fail()
        self.assertEqual(len(self.notices()), 2)

    def test_trigger_escalation_is_immediate_even_when_that_stage_was_reported_while_waiting(self):
        self.fail(closing=False)
        self.fail(closing=True)
        self.assertEqual(len(self.notices()), 2)
        self.assertIn('No close order has been submitted', self.notices()[0]['text'])
        self.assertIn('Position closure is not confirmed', self.notices()[1]['text'])

    def test_continuing_failures_extend_the_quiet_window_and_a_new_incident_notifies(self):
        self.fail()
        for _ in range(4):
            self.clock.advance(30)
            self.fail()
        self.assertEqual(len(self.notices()), 1)
        self.clock.advance(59)
        self.fail()
        self.assertEqual(len(self.notices()), 1)
        self.clock.advance(60)
        self.fail()
        self.assertEqual(len(self.notices()), 2)

    def test_restart_preserves_both_alternating_stage_markers(self):
        self.fail()
        self.fail('checking your position fill history')
        self.store = AutocloseStore(self.queue, '12345')
        self.worker = self.make_worker()
        self.clock.advance(5)
        self.fail()
        self.fail('checking your position fill history')
        self.assertEqual(len(self.notices()), 2)

    def test_new_order_attempt_and_unknown_outcome_each_notify_immediately(self):
        self.fail()
        rule = self.store.active()[0]
        self.store.prepare(rule, 'test-signer', self.exchange.market_value.order(10, 99), self.clock())
        self.fail()
        self.assertEqual(len(self.notices()), 2)
        self.assertIn('order outcome is unknown', self.notices()[-1]['text'])
        # The same attempt must not repeat when another stage fails in between.
        self.fail('checking your position fill history')
        self.fail()
        self.assertEqual(len(self.notices()), 3)
        rule = self.store.active()[0]
        self.store.prepare(rule, 'test-signer', self.exchange.market_value.order(10, 99), self.clock())
        self.fail()
        self.assertEqual(len(self.notices()), 4)

    def test_failed_notification_transaction_does_not_hide_the_next_warning(self):
        with patch.object(self.queue, 'enqueue_transaction', side_effect=RuntimeError('full')):
            with self.assertRaises(RuntimeError):
                self.fail()
        self.assertFalse(self.notices())
        self.fail()
        self.assertEqual(len(self.notices()), 1)

    def test_legacy_waiting_marker_is_respected_after_upgrade(self):
        rule = self.store.active()[0]
        detail = DataFetchError('busy', reason='budget', operation='userFillsByTime').diagnostic()
        rule['budget_notice'] = dict(stage='verifying fills before submission', detail=detail,
                                     quiet_until=self.clock() + 60)
        self.store.save(rule)
        self.fail(closing=False)
        self.assertFalse(self.notices())
        self.fail(closing=True)
        self.assertEqual(len(self.notices()), 1)

    def test_preflight_failures_then_recovery_send_one_order_and_one_success(self):
        self.exchange.source = 0
        fills = self.exchange.fills
        failed_stage = [0]

        def history(*args, **kwargs):
            # Alternate the early history check and the final fresh check.
            if (failed_stage[0] % 2 == 0 and kwargs.get('fresh')) or failed_stage[0] % 2 == 1:
                failed_stage[0] += 1
                raise DataFetchError('busy', reason='budget', operation='userFillsByTime')
            return fills(*args, **kwargs)

        with patch.object(self.exchange, 'fills', side_effect=history):
            for _ in range(12):
                self.clock.advance(5)
                self.worker.cycle()
        self.assertEqual(len(self.notices()), 2)
        self.assertFalse(self.exchange.orders)
        self.assertEqual(self.store.active()[0]['attempts'], 0)
        self.assertTrue(self.store.active()[0]['close_priority'])
        self.clock.advance(5)
        self.worker.cycle()
        self.assertEqual(len(self.exchange.orders), 1)
        self.clock.advance(5)
        self.worker.cycle()
        self.worker.cycle()
        self.assertFalse(self.store.active())
        successes = [job for job in self.jobs() if 'market close confirmed' in job['text']]
        self.assertEqual(len(successes), 1)
        self.assertEqual(successes[0]['chat'], 1)
        self.assertEqual(len(self.exchange.orders), 1)
