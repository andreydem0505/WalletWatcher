"""Activation retries only read-only requests, with durable, safe diagnostics."""
from unittest.mock import Mock, patch
import requests

from support import ApplicationFixture, WALLET, OTHER
from walletwatcher.domain.models import Account
from walletwatcher.hyperliquid.client import DataFetchError, HyperliquidClient
from walletwatcher.storage.autoclose import AutocloseStore
from walletwatcher.trading.worker import AutocloseWorker
from test_autoclose import Exchange, KEY


class ActivationRetryTests(ApplicationFixture):
    def setUp(self):
        super().setUp()
        self.app.accounts[WALLET] = Account()
        self.app.credentials.save(1, OTHER, KEY)
        self.store = self.app.autoclose_store
        self.exchange = Exchange(self.clock)
        self.worker = AutocloseWorker(self.app, self.store, self.exchange, self.clock)
        self.rule = self.store.create(1, WALLET, 'BTC', self.app.credentials.version(1))

    def state(self):
        return self.store.active()[0]

    def test_transient_error_retries_and_then_arms_without_submitting_an_order(self):
        with patch.object(self.exchange, 'authority', side_effect=DataFetchError(KEY)), \
                self.assertLogs('walletwatcher.trading.worker', level='WARNING') as logs:
            self.worker.cycle()
        self.assertNotIn(KEY, ''.join(logs.output))
        self.assertEqual(self.state()['state'], 'queued')
        self.assertEqual(self.state()['activation_failures'], 1)
        self.assertIn('Retrying automatically', self.jobs()[0]['text'])
        self.assertNotIn(KEY, self.jobs()[0]['text'])
        with patch.object(self.exchange, 'authority', side_effect=AssertionError('not due yet')):
            self.worker.cycle()
        self.clock.advance(5)
        self.worker.cycle()
        self.assertEqual(self.state()['state'], 'armed')
        self.assertEqual(self.state()['attempts'], 0)
        self.assertFalse(self.exchange.orders)

    def test_rate_limit_retry_deadline_survives_store_and_worker_reopen(self):
        error = DataFetchError('private details', reason='rate_limit', status_code=429, retry_after=61)
        with patch.object(self.exchange, 'authority', side_effect=error):
            self.worker.cycle()
        self.assertEqual(self.state()['activation_retry_at'], self.clock() + 61)
        store = AutocloseStore(self.queue, '12345')
        worker = AutocloseWorker(self.app, store, self.exchange, self.clock)
        self.clock.advance(60)
        with patch.object(self.exchange, 'authority') as query:
            worker.cycle()
            query.assert_not_called()
        self.clock.advance(1)
        worker.cycle()
        self.assertEqual(self.state()['state'], 'armed')
        self.assertIn('HTTP 429', self.jobs()[0]['text'])

    def test_actual_shared_budget_exhaustion_waits_without_any_exchange_request(self):
        session = Mock()
        client = HyperliquidClient(session=session, clock=self.clock, weight_budget=60)
        client._reserve_weight('userRole')
        def authority(credentials):
            client._request({'type': 'userRole', 'user': credentials.address})
        with patch.object(self.exchange, 'authority', side_effect=authority):
            self.worker.cycle()
        session.post.assert_not_called()
        self.assertEqual(self.state()['activation_retry_at'], self.clock() + 60)
        self.assertIn('bot request budget is busy', self.jobs()[0]['text'])
        self.clock.advance(60)
        self.worker.cycle()
        self.assertEqual(self.state()['state'], 'armed')
        self.assertFalse(self.exchange.orders)

    def test_three_network_failures_finish_registration_not_trade_attempts(self):
        with patch.object(self.exchange, 'authority', side_effect=DataFetchError('timeout')):
            for delay in (0, 5, 10):
                self.clock.advance(delay)
                self.worker.cycle()
        self.assertFalse(self.store.active())
        self.assertEqual(len(self.jobs()), 3)  # Every failed activation attempt is explained.
        self.assertIn('No rule was armed', self.jobs()[-1]['text'])
        self.assertFalse(self.exchange.orders)

    def test_long_cooldown_has_bounded_activation_lifetime(self):
        error = DataFetchError('rate limited', reason='rate_limit', retry_after=7200)
        with patch.object(self.exchange, 'authority', side_effect=error):
            self.worker.cycle()
        self.clock.advance(300)
        with patch.object(self.exchange, 'authority') as query:
            self.worker.cycle()
            query.assert_not_called()
        self.assertFalse(self.store.active())
        self.assertIn('Activation timed out', self.jobs()[-1]['text'])

    def test_cancel_during_cooldown_prevents_activation(self):
        with patch.object(self.exchange, 'authority', side_effect=DataFetchError('offline', retry_after=60)):
            self.worker.cycle()
        self.store.cancel(1, WALLET, 'BTC')
        with patch.object(self.exchange, 'authority') as query:
            self.worker.cycle()
            query.assert_not_called()
        self.assertFalse(self.store.active())
        self.assertIn('cancelled', self.jobs()[-1]['text'])

    def test_credentials_replacement_during_cooldown_cancels(self):
        with patch.object(self.exchange, 'authority', side_effect=DataFetchError('offline')):
            self.worker.cycle()
        self.app.credentials.save(1, OTHER, KEY)
        self.clock.advance(5)
        self.worker.cycle()
        self.assertFalse(self.store.active())
        self.assertFalse(self.exchange.orders)

    def test_non_retryable_http_error_reports_stage_and_status_without_body(self):
        error = DataFetchError(KEY, operation='meta', reason='http', status_code=422, retryable=False)
        with patch.object(self.exchange, 'market', side_effect=error):
            self.worker.cycle()
        self.assertFalse(self.store.active())
        self.assertIn('loading market metadata', self.jobs()[-1]['text'])
        self.assertIn('HTTP 422', self.jobs()[-1]['text'])
        self.assertNotIn(KEY, self.jobs()[-1]['text'])

    def test_fill_history_failure_repeats_complete_validation_before_baseline(self):
        with patch.object(self.exchange, 'fills', side_effect=DataFetchError('timeout')):
            self.worker.cycle()
        self.assertIn('fill history', self.jobs()[-1]['text'])
        self.exchange.source = 200
        self.clock.advance(5)
        self.worker.cycle()
        self.assertEqual(self.state()['source_size'], '200')
        self.assertEqual(self.state()['state'], 'armed')


    def test_initial_deadline_persists_before_network_and_expiry_does_not_arm(self):
        original = self.exchange.authority
        def slow(credentials):
            self.assertEqual(self.state()['activation_deadline'], self.clock() + 300)
            self.clock.advance(301)
            return original(credentials)
        with patch.object(self.exchange, 'authority', side_effect=slow):
            self.worker.cycle()
        self.assertFalse(self.store.active())
        self.assertIn('Activation timed out', self.jobs()[-1]['text'])
        self.assertFalse(self.exchange.orders)

    def test_recovered_queued_request_deadline_without_prior_error_is_safe(self):
        self.rule['activation_deadline'] = self.clock() - 1
        self.store.save(self.rule)
        self.worker.cycle()
        self.assertFalse(self.store.active())
        self.assertIn('Activation timed out', self.jobs()[-1]['text'])


class RequestDiagnosticsTests(ApplicationFixture):
    def request_error(self, exception, code=None, headers=None):
        session = Mock()
        response = session.post.return_value
        response.status_code = code
        response.headers = headers or {}
        if code is None:
            session.post.side_effect = exception
        elif isinstance(exception, ValueError) and not isinstance(exception, requests.HTTPError):
            response.json.side_effect = exception
        else:
            response.raise_for_status.side_effect = exception
        client = HyperliquidClient(session=session, clock=self.clock)
        with self.assertRaises(DataFetchError) as caught:
            client._request({'type': 'userRole', 'user': OTHER})
        return client, caught.exception

    def test_timeout_network_5xx_and_429_are_retryable_and_sanitized(self):
        for exception, code, reason in ((requests.Timeout(KEY), None, 'timeout'),
                                        (requests.ConnectionError(KEY), None, 'network'),
                                        (requests.HTTPError(KEY), 503, 'http'),
                                        (requests.HTTPError(KEY), 408, 'http'),
                                        (requests.HTTPError(KEY), 429, 'rate_limit')):
            with self.subTest(code=code, reason=reason):
                _, error = self.request_error(exception, code)
                self.assertTrue(error.retryable)
                self.assertEqual(error.reason, reason)
                self.assertEqual(error.operation, 'userRole')
                self.assertEqual(error.status_code, code)
                self.assertNotIn(KEY, str(error))
                self.assertNotIn(KEY, error.diagnostic())

    def test_4xx_and_invalid_json_fail_without_automatic_retry(self):
        for exception, code, reason in ((requests.HTTPError(KEY), 403, 'http'),
                                        (requests.HTTPError(KEY), 422, 'http'),
                                        (ValueError(KEY), 200, 'invalid_response')):
            _, error = self.request_error(exception, code)
            self.assertFalse(error.retryable)
            self.assertEqual(error.reason, reason)
            self.assertNotIn(KEY, error.diagnostic())

    def test_429_cooldown_preserves_reason_delay_and_prevents_post(self):
        client, error = self.request_error(requests.HTTPError(), 429, {'Retry-After': '73'})
        self.assertEqual(error.retry_after, 73)
        with self.assertRaises(DataFetchError) as caught:
            client._request({'type': 'meta'})
        self.assertEqual(caught.exception.reason, 'rate_limit')
        self.assertEqual(caught.exception.retry_after, 73)
        self.assertEqual(client.session.post.call_count, 1)

    def test_diagnostic_unknown_reason_does_not_render_untrusted_details(self):
        error = DataFetchError(KEY, reason=KEY, status_code=KEY)
        self.assertEqual(error.diagnostic(), 'the data request failed')

    def test_two_unit_info_weights_follow_current_api_contract(self):
        client = HyperliquidClient(clock=self.clock)
        self.addCleanup(client.close)
        for operation in ('allMids', 'orderStatus', 'clearinghouseState'):
            self.assertEqual(client._reserve_weight(operation)[1], 2)
