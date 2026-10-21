"""Closing preempts ordinary requests; failures are durably explained to owners."""
from decimal import Decimal
import json
import threading
from unittest.mock import Mock, patch

from support import ApplicationFixture, WALLET, OTHER
from test_autoclose import Exchange, KEY
from walletwatcher.domain.models import Account
from walletwatcher.hyperliquid.client import HyperliquidClient
from walletwatcher.hyperliquid.errors import DataFetchError
from walletwatcher.storage.autoclose import AutocloseStore
from walletwatcher.trading.policy import TradingError
from walletwatcher.trading.worker import AutocloseWorker


class ClosingTests(ApplicationFixture):
    def setUp(self):
        super().setUp()
        self.app.accounts[WALLET] = Account()
        self.app.credentials.save(1, OTHER, KEY)
        self.store = self.app.autoclose_store
        self.exchange = Exchange(self.clock)
        self.worker = AutocloseWorker(self.app, self.store, self.exchange, self.clock)
        self.rule = self.store.create(1, WALLET, 'BTC', self.app.credentials.version(1))
        self.worker.cycle()
        self.session = Mock()
        self.session.post.return_value.status_code = 200
        self.session.post.return_value.json.return_value = {'assetPositions': []}
        self.http = HyperliquidClient(self.session, clock=self.clock)
        self.addCleanup(self.http.close)
        self.app.client = self.http

    def rule_state(self):
        return json.loads(self.queue.db.execute('SELECT data FROM autoclose_rules WHERE id=?',
                                               (self.rule['id'],)).fetchone()[0])

    def normal_request(self):
        result = []
        def request():
            try:
                self.http._request({'type': 'clearinghouseState'}, session=self.session)
                result.append('sent')
            except DataFetchError as error:
                result.append(error.reason)
        thread = threading.Thread(target=request)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        return result[0]

    def trigger(self):
        self.exchange.source = Decimal(0)
        self.clock.advance(5)
        self.worker.cycle()

    def test_monitor_pauses_before_history_and_send_and_resumes_only_after_confirmation(self):
        observations = []
        original = self.exchange.fills
        def history(*args, **kwargs):
            observations.append(self.normal_request())
            return original(*args, **kwargs)
        with patch.object(self.exchange, 'fills', side_effect=history):
            self.trigger()
        self.assertEqual(observations, ['trading_priority', 'trading_priority'])
        self.assertEqual(len(self.exchange.orders), 1)
        self.assertEqual(self.normal_request(), 'trading_priority')
        self.session.post.assert_not_called()
        self.clock.advance(5)
        self.worker.cycle()
        self.assertFalse(self.store.active())
        self.assertEqual(self.normal_request(), 'sent')
        self.assertEqual(len(self.exchange.orders), 1)

    def test_monitor_pause_does_not_increment_failures_or_backoff(self):
        self.trigger()
        account = self.app.accounts[WALLET]
        account.failures = 4
        account.next_poll = 0
        waits = []
        def wait(delay):
            waits.append(delay)
            self.app.stop.set()
            return True
        with patch.object(self.app.stop, 'wait', side_effect=wait):
            self.app.monitor(Mock())
        self.assertEqual(account.failures, 4)
        self.assertEqual(account.next_poll, 0)
        self.assertEqual(waits, [1])
        self.session.post.assert_not_called()

    def test_priority_is_restored_before_any_api_work_after_restart(self):
        with patch.object(self.exchange, 'fills', side_effect=DataFetchError('offline')):
            self.trigger()
        self.assertEqual(self.rule_state()['attempts'], 0)
        self.assertTrue(self.rule_state()['close_priority'])
        self.http.sync_close_priorities(set())  # Simulate a fresh process's empty limiter.
        replacement = AutocloseStore(self.queue, '12345')
        self.worker = AutocloseWorker(self.app, replacement, self.exchange, self.clock)
        observed = []
        self.exchange.begin_cycle.side_effect = lambda: observed.append(self.normal_request())
        self.worker.cycle()
        self.assertTrue(observed)
        self.assertEqual(set(observed), {'trading_priority'})
        self.assertEqual(len(self.exchange.orders), 1)

    def test_source_recovery_releases_priority_without_latching_a_trade(self):
        with patch.object(self.exchange, 'fills', side_effect=DataFetchError('offline')):
            self.trigger()
        self.exchange.source = Decimal(100)
        self.clock.advance(5)
        self.worker.cycle()
        self.assertFalse(self.exchange.orders)
        self.assertFalse(self.rule_state()['close_priority'])
        self.assertEqual(self.normal_request(), 'sent')

    def test_worker_crash_notifies_owner_and_keeps_monitor_paused_until_supervised_recovery(self):
        self.trigger()
        with patch.object(self.worker, 'cycle', side_effect=RuntimeError(KEY)):
            with self.assertRaises(RuntimeError):
                self.worker.run(Mock())
        self.assertIn('worker was interrupted', self.jobs()[-1]['text'])
        self.assertNotIn(KEY, self.jobs()[-1]['text'])
        self.assertEqual(self.normal_request(), 'trading_priority')
        self.worker = AutocloseWorker(self.app, self.store, self.exchange, self.clock)
        self.worker.cycle()
        self.assertEqual(self.normal_request(), 'sent')
        self.assertEqual(len(self.exchange.orders), 1)

    def test_application_shutdown_releases_priority_and_closes_the_api(self):
        self.trigger()
        self.app.stop.set()
        self.worker.run(Mock())
        self.assertFalse(self.http.budget.closing_rules)
        self.exchange.close.assert_called_once()

    def test_cancel_before_submission_releases_monitor(self):
        with patch.object(self.exchange, 'fills', side_effect=DataFetchError('offline')):
            self.trigger()
        self.store.cancel(1)
        self.worker.cycle()
        self.assertFalse(self.store.active())
        self.assertFalse(self.exchange.orders)
        self.assertEqual(self.normal_request(), 'sent')

    def test_closing_is_processed_before_an_older_queued_rule(self):
        self.trigger()
        self.app.credentials.save(2, OTHER, KEY)
        queued = self.store.create(2, WALLET, 'ETH', self.app.credentials.version(2))
        self.exchange.unknown = True
        original = self.store.active
        def reverse_order():
            return sorted(original(), key=lambda rule: rule['id'] != queued['id'])
        processed = []
        process = self.worker.process_rule
        def tracked(rule):
            processed.append(rule['id'])
            process(rule)
        with patch.object(self.store, 'active', side_effect=reverse_order), \
                patch.object(self.worker, 'process_rule', side_effect=tracked):
            self.worker.cycle()
        self.assertEqual(processed, [self.rule['id']])
        self.assertEqual(next(r for r in original() if r['id'] == queued['id'])['state'], 'queued')

    def test_an_unknown_order_blocks_monitor_until_the_rule_stops_but_never_resends(self):
        self.exchange.unknown = True
        self.trigger()
        self.worker.cycle()
        self.assertEqual(self.normal_request(), 'trading_priority')
        unverified = [job for job in self.jobs() if 'not yet verified' in job['text']]
        self.assertEqual(len(unverified), 1)
        self.worker = AutocloseWorker(self.app, self.store, self.exchange, self.clock)
        self.worker.cycle()
        self.assertEqual(len([job for job in self.jobs() if 'not yet verified' in job['text']]), 1)
        self.clock.advance(30)
        self.worker.cycle()
        self.assertFalse(self.store.active())
        self.assertTrue(self.store.unresolved())
        self.assertEqual(self.normal_request(), 'sent')
        self.assertEqual(len(self.exchange.orders), 1)

    def test_each_network_failure_stage_notifies_immediately_with_a_safe_explanation(self):
        # All injections fail before sending a real request or a simulated order.
        scenarios = [('fills', 'checking your position fill history'),
                     ('authority', 'checking trading permissions'),
                     ('mid', 'reading the market quote')]
        for method, stage in scenarios:
            with self.subTest(method=method):
                before = len(self.jobs())
                with patch.object(self.exchange, method, side_effect=DataFetchError(KEY, reason='timeout')):
                    self.trigger()
                self.assertEqual(len(self.jobs()), before + 1)
                notice = self.jobs()[-1]
                self.assertEqual(notice['chat'], 1)
                self.assertIn(stage, notice['text'])
                self.assertIn('timed out', notice['text'])
                self.assertNotIn(KEY, notice['text'])
                self.assertFalse(self.exchange.orders)

    def test_repeated_error_is_deduplicated_but_changed_cause_is_explained(self):
        self.exchange.source = Decimal(0)
        before = len(self.jobs())
        with patch.object(self.exchange, 'authority', side_effect=DataFetchError('offline')):
            for _ in range(3):
                self.worker.cycle()
        self.assertEqual(len(self.jobs()), before + 1)
        with patch.object(self.exchange, 'authority', side_effect=DataFetchError('busy', reason='rate_limit', status_code=429)):
            self.worker.cycle()
        self.assertEqual(len(self.jobs()), before + 2)
        self.assertIn('HTTP 429', self.jobs()[-1]['text'])
        self.worker = AutocloseWorker(self.app, self.store, self.exchange, self.clock)
        with patch.object(self.exchange, 'authority', side_effect=DataFetchError('busy', reason='rate_limit', status_code=429)):
            self.worker.cycle()
        self.assertEqual(len(self.jobs()), before + 2)

    def test_submission_timeout_is_reported_before_reconciliation_and_never_retried_blindly(self):
        self.exchange.fail = DataFetchError(KEY, reason='timeout', operation='exchange')
        self.trigger()
        notice = self.jobs()[-1]
        self.assertIn('submitting the reduce-only order', notice['text'])
        self.assertIn('outcome is unknown', notice['text'])
        self.assertNotIn(KEY, notice['text'])
        self.assertEqual(len(self.exchange.orders), 1)
        self.worker.cycle()
        self.assertEqual(len(self.exchange.orders), 1)
        self.assertIn('market close confirmed', self.jobs()[-1]['text'])

    def test_every_rejected_ioc_attempt_is_reported_and_notices_survive_restart(self):
        self.exchange.reject = True
        self.trigger()
        self.assertIn('IOC order was rejected', self.jobs()[-1]['text'])
        self.worker = AutocloseWorker(self.app, self.store, self.exchange, self.clock)
        self.worker.cycle()  # Reconcile only: minimum retry interval has not elapsed.
        self.assertEqual(len([job for job in self.jobs() if 'IOC order was rejected' in job['text']]), 1)
        for _ in range(3):
            self.clock.advance(5)
            self.worker.cycle()
        self.assertEqual(len(self.exchange.orders), 3)
        notices = [job for job in self.jobs() if 'IOC order was rejected' in job['text']]
        self.assertEqual(len(notices), 3)
        self.assertEqual({job['chat'] for job in notices}, {1})
        self.assertIn('Remaining position: 10', self.jobs()[-1]['text'])
        self.assertEqual(self.normal_request(), 'sent')

    def test_partial_fill_is_explained_without_claiming_a_complete_close(self):
        self.exchange.fraction = Decimal('.5')
        self.trigger()
        self.assertIn('partial fill: 5.0 of 10', self.jobs()[-1]['text'])
        self.assertNotIn('market close confirmed', self.jobs()[-1]['text'])
        self.assertEqual(self.normal_request(), 'trading_priority')

    def test_unexpected_ioc_response_is_explained_immediately_without_blind_retry(self):
        self.exchange.unknown = True
        with patch.object(self.exchange, 'send', return_value={
                'status': 'ok', 'response': {'data': {'statuses': [{'resting': {'oid': 99}}]}}}):
            self.trigger()
        self.assertIn('did not report a completed IOC execution', self.jobs()[-1]['text'])
        before = len(self.jobs())
        self.worker.cycle()
        self.assertEqual(len(self.jobs()), before)
        self.assertEqual(self.rule_state()['attempts'], 1)
        self.assertEqual(self.normal_request(), 'trading_priority')

    def test_final_refresh_errors_are_explained_before_any_submission(self):
        original_position = self.exchange.position
        original_fills = self.exchange.fills
        for method, stage in [('position', 'refreshing your position before submission'),
                              ('fills', 'verifying fills before submission')]:
            with self.subTest(method=method):
                def position(address, market, fresh=False):
                    if fresh:
                        raise DataFetchError(KEY, reason='timeout')
                    return original_position(address, market, fresh)
                def fills(*args, **kwargs):
                    if kwargs.get('fresh'):
                        raise DataFetchError(KEY, reason='timeout')
                    return original_fills(*args, **kwargs)
                with patch.object(self.exchange, method, side_effect=position if method == 'position' else fills):
                    self.trigger()
                self.assertIn(stage, self.jobs()[-1]['text'])
                self.assertIn('timed out', self.jobs()[-1]['text'])
                self.assertNotIn(KEY, self.jobs()[-1]['text'])
                self.assertFalse(self.exchange.orders)

    def test_attempt_notice_and_journal_commit_atomically(self):
        self.trigger()
        rule = self.store.active()[0]
        attempt = rule['pending']
        with patch.object(self.queue, 'enqueue_transaction', side_effect=RuntimeError('full')):
            with self.assertRaises(RuntimeError):
                self.worker.notify_attempt_issue(rule, attempt, 'test', 'A safe explanation.')
        self.assertNotIn('notices', self.rule_state()['pending'])
        saved_attempt = json.loads(self.queue.db.execute('SELECT data FROM autoclose_attempts WHERE rule_id=?',
                                                        (rule['id'],)).fetchone()[0])
        self.assertNotIn('notices', saved_attempt)

    def test_late_reconciliation_error_is_explained_once_and_preserves_the_fence(self):
        self.exchange.unknown = True
        self.trigger()
        self.clock.advance(30)
        self.worker.cycle()
        with patch.object(self.exchange, 'status', side_effect=DataFetchError(KEY, reason='timeout')):
            self.worker.cycle()
            self.worker.cycle()
        notices = [job for job in self.jobs() if 'late order reconciliation failed' in job['text']]
        self.assertEqual(len(notices), 1)
        self.assertIn('timed out', notices[0]['text'])
        self.assertNotIn(KEY, notices[0]['text'])
        self.assertTrue(self.store.unresolved())
        self.assertEqual(len(self.exchange.orders), 1)

    def test_final_data_error_releases_monitor_and_explains_the_stop(self):
        with patch.object(self.exchange, 'fills', side_effect=TradingError('Position history is incomplete.')):
            self.trigger()
        self.assertIn('Position history is incomplete', self.jobs()[-1]['text'])
        self.assertFalse(self.store.active())
        self.assertEqual(self.normal_request(), 'sent')

    def test_non_retryable_key_failure_stops_instead_of_pausing_monitor_forever(self):
        error = DataFetchError(KEY, reason='http', status_code=403, operation='userRole', retryable=False)
        with patch.object(self.exchange, 'authority', side_effect=error):
            self.trigger()
        self.assertFalse(self.store.active())
        self.assertFalse(self.exchange.orders)
        self.assertIn('HTTP 403', self.jobs()[-1]['text'])
        self.assertIn('not retryable', self.jobs()[-1]['text'])
        self.assertNotIn(KEY, self.jobs()[-1]['text'])
        self.assertEqual(self.normal_request(), 'sent')

    def test_lower_priority_trading_checks_cannot_take_closing_capacity(self):
        self.http.set_trading_priority(True)
        self.http.sync_close_priorities({'urgent'})
        self.http.set_trading_rule('waiting')
        with self.assertRaises(DataFetchError) as caught:
            self.http._request({'type': 'userRole'}, session=self.session)
        self.assertEqual(caught.exception.reason, 'trading_priority')
        self.session.post.assert_not_called()
        self.http.set_trading_rule('urgent')
        self.http._request({'type': 'userRole'}, session=self.session)
        self.session.post.assert_called_once()

    def test_closing_does_not_wait_for_an_already_inflight_monitor_http_request(self):
        entered, release = threading.Event(), threading.Event()
        response = self.session.post.return_value
        def slow_post(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError('Test monitor was not released')
            return response
        self.session.post.side_effect = slow_post
        result = []
        def monitor():
            try:
                result.append(self.http.fetch_open_positions(WALLET))
            except Exception as error:
                result.append(error)
        thread = threading.Thread(target=monitor)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.http.set_trading_priority(True)
            self.http.set_trading_rule('urgent')
            self.http.set_close_priority('urgent', True)
            trading = Mock()
            trading.post.return_value.status_code = 200
            trading.post.return_value.json.return_value = {}
            self.http._request({'type': 'userRole'}, session=trading)
            trading.post.assert_called_once()
            self.assertTrue(thread.is_alive())
            self.assertEqual(self.normal_request(), 'trading_priority')
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [[]])

    def test_error_notifications_are_delivered_only_to_the_rule_owner(self):
        with patch.object(self.exchange, 'mid', side_effect=DataFetchError('offline')):
            self.trigger()
        self.deliver_pending()
        self.assertTrue(self.sender.send_message.called)
        self.assertEqual({call.kwargs['chat_id'] for call in self.sender.send_message.call_args_list}, {1})
        self.assertTrue(any('reading the market quote' in call.kwargs['text']
                            for call in self.sender.send_message.call_args_list))

    def test_multiple_closing_rules_keep_the_pause_until_all_release_it(self):
        self.http.sync_close_priorities({'first', 'second'})
        self.assertEqual(self.normal_request(), 'trading_priority')
        self.http.set_close_priority('first', False)
        self.assertEqual(self.normal_request(), 'trading_priority')
        self.http.finish_trading_cycle({'second'})
        self.assertEqual(self.normal_request(), 'trading_priority')
        self.http.set_close_priority('second', False)
        self.assertEqual(self.normal_request(), 'sent')

    def test_full_budget_cannot_be_stolen_from_a_closing_rule_when_it_expires(self):
        for _ in range(50):
            self.http._reserve_weight('meta')
        self.http.set_trading_priority(True)
        self.http.set_trading_rule('urgent')
        self.http.set_close_priority('urgent', True)
        with self.assertRaises(DataFetchError) as caught:
            self.http._request({'type': 'userRole'}, session=self.session)
        self.assertEqual(caught.exception.reason, 'budget')
        self.clock.advance(60)
        self.assertEqual(self.normal_request(), 'trading_priority')
        self.http._request({'type': 'userRole'}, session=self.session)
        self.assertEqual(self.session.post.call_count, 1)
