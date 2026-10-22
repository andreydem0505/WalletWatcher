"""Concurrency guarantees for /my, independent of network speed and scheduling."""
from concurrent.futures import ThreadPoolExecutor
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import requests

from walletwatcher.hyperliquid.client import DataFetchError, HyperliquidClient
from walletwatcher.domain.models import Account
from support import ApplicationFixture, Clock, WALLET, OTHER
from test_personal_accounts import KEY, state, contexts, response, snapshot


class PersonalWorkerTests(ApplicationFixture):
    def my(self, user=1):
        self.app.handle_message(SimpleNamespace(text='/my', from_user=SimpleNamespace(id=user),
            chat=SimpleNamespace(id=user, type='private')))

    def test_my_is_queued_and_repeated_commands_share_one_fresh_result(self):
        self.app.credentials.save(1, WALLET, KEY)
        self.client.fetch_portfolio.return_value = snapshot()
        self.my()
        self.my()
        self.client.fetch_portfolio.assert_not_called()
        self.assertEqual(self.app.personal_queue.qsize(), 1)
        self.assertTrue(self.app.process_personal_request())
        self.client.fetch_portfolio.assert_called_once_with(WALLET)
        self.assertEqual(len(self.jobs()), 1)
        self.assertFalse(self.app.personal_pending)
        self.my()
        self.assertTrue(self.app.process_personal_request())
        self.assertEqual(self.client.fetch_portfolio.call_count, 2)

    def test_slow_my_runs_in_separate_thread_while_monitor_and_commands_progress(self):
        self.app.credentials.save(1, WALLET, KEY)
        started, release, result_ready = threading.Event(), threading.Event(), threading.Event()
        thread_ids = []

        def fetch(address):
            thread_ids.append(threading.get_ident())
            started.set()
            if not release.wait(3):
                raise DataFetchError('test timed out')
            return snapshot()

        enqueue = self.queue.enqueue
        def capture(*args, **kwargs):
            enqueue(*args, **kwargs)
            if 'Balance:' in args[1]:
                result_ready.set()

        self.queue.enqueue = capture
        self.client.fetch_portfolio.side_effect = fetch
        worker = threading.Thread(target=self.app.personal_worker, args=(Mock(),), daemon=True)
        def finish():
            self.app.stop.set()
            release.set()
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.addCleanup(finish)
        worker.start()
        self.my()
        self.assertTrue(started.wait(2))
        self.assertNotEqual(thread_ids[0], threading.get_ident())
        self.app.handle_message(self.message('/status'))
        self.assertIn('Last monitoring cycle:', self.jobs()[0]['text'])
        account = Account(positions=[])
        self.app.accounts[WALLET] = account
        self.client.fetch_open_positions.return_value = []
        self.app.poll_wallet(WALLET, account)
        self.assertIsNotNone(account.last_updated)
        self.assertFalse(result_ready.is_set())
        release.set()
        self.assertTrue(result_ready.wait(2))
        self.assertEqual([job['chat'] for job in self.jobs()], [1, 1])

    def test_full_queue_rejects_request_without_blocking_or_marking_it_pending(self):
        self.app.personal_queue = queue.Queue(maxsize=1)
        for user in (1, 2):
            self.app.credentials.save(user, WALLET, KEY)
            self.my(user)
        self.assertEqual(self.app.personal_pending, {1})
        self.assertEqual(self.jobs()[0]['chat'], 2)
        self.assertIn('Очередь', self.jobs()[0]['text'])
        self.client.fetch_portfolio.assert_not_called()

    def test_failed_request_does_not_stop_next_user_or_log_secret(self):
        for user in (1, 2):
            self.app.credentials.save(user, WALLET if user == 1 else OTHER, KEY)
            self.my(user)
        self.client.fetch_portfolio.side_effect = [RuntimeError(KEY), snapshot()]
        with self.assertLogs('walletwatcher.services.portfolios', level='ERROR') as logs:
            self.assertTrue(self.app.process_personal_request())
        self.assertTrue(self.app.process_personal_request())
        self.assertFalse(self.app.process_personal_request())
        self.assertFalse(self.app.personal_pending)
        self.assertNotIn(KEY, '\n'.join(logs.output))
        self.assertEqual([job['chat'] for job in self.jobs()], [1, 2])
        self.assertIn('Balance:', self.jobs()[1]['text'])

    def test_account_change_during_request_does_not_publish_previous_accounts_positions(self):
        self.app.credentials.save(1, WALLET, KEY)
        self.my()
        def changed(address):
            self.app.credentials.save(1, OTHER, KEY)
            return snapshot()
        self.client.fetch_portfolio.side_effect = changed
        self.app.process_personal_request()
        self.assertIn('Данные аккаунта изменились', self.jobs()[0]['text'])
        self.assertNotIn('Balance:', self.jobs()[0]['text'])

    def test_shutdown_during_request_does_not_enqueue_result(self):
        self.app.credentials.save(1, WALLET, KEY)
        self.my()
        def stopping(address):
            self.app.stop.set()
            return snapshot()
        self.client.fetch_portfolio.side_effect = stopping
        self.app.process_personal_request()
        self.assertFalse(self.app.personal_pending)
        self.assertEqual(self.queue.pending_count(), 0)

    def test_shutdown_before_request_does_not_start_network_work(self):
        self.app.credentials.save(1, WALLET, KEY)
        self.my()
        self.app.stop.set()
        self.app.process_personal_request()
        self.client.fetch_portfolio.assert_not_called()
        self.assertFalse(self.app.personal_pending)


class ParallelPortfolioTests(unittest.TestCase):
    def client(self, post, **kwargs):
        sessions, lock = [], threading.Lock()
        def factory():
            session = Mock()
            session.post.side_effect = post
            with lock:
                sessions.append(session)
            return session
        monitor_session = Mock()
        client = HyperliquidClient(monitor_session, personal_session_factory=factory, **kwargs)
        client._fetch_dex_states = Mock(return_value=[('', state())])
        return client, monitor_session, sessions

    def test_account_mode_and_snapshot_start_together(self):
        together = threading.Barrier(2)
        def post(*args, **kwargs):
            self.assertEqual(kwargs['json']['type'], 'userAbstraction')
            together.wait(2)
            return response('disabled')
        client, _, sessions = self.client(post)
        def snapshot_states(wallet):
            together.wait(2)
            return [('', {'marginSummary': {'accountValue': '77.43'}, 'assetPositions': []})]
        client._fetch_dex_states.side_effect = snapshot_states
        self.assertEqual(client.fetch_portfolio(WALLET).balance, 77.43)
        for session in sessions:
            session.close.assert_called_once()

    def test_four_prices_and_orders_overlap_with_separate_sessions(self):
        together, lock = threading.Barrier(4), threading.Lock()
        active, peak, owners = 0, 0, {}
        sessions = []
        def factory():
            session = Mock()
            def post(*args, **kwargs):
                nonlocal active, peak
                ident = threading.get_ident()
                with lock:
                    self.assertEqual(owners.setdefault(id(session), ident), ident)
                payload = kwargs['json']
                if payload['type'] == 'userAbstraction':
                    return response('disabled')
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    together.wait(2)
                    return response(contexts('VVV' if payload['dex'] == '' else 'EWZ')
                                    if payload['type'] == 'metaAndAssetCtxs' else [])
                finally:
                    with lock:
                        active -= 1
            session.post.side_effect = post
            with lock:
                sessions.append(session)
            return session
        client = HyperliquidClient(Mock(), personal_session_factory=factory)
        client._fetch_dex_states = Mock(return_value=[('', state()), ('xyz', state('xyz:EWZ', '2', '10'))])
        result = client.fetch_portfolio(WALLET)
        self.assertEqual(peak, 4)
        self.assertEqual(len(sessions), 4)
        self.assertEqual(result.balance, 87.43)
        self.assertEqual([item.position.ticker for item in result.positions], ['VVV', 'xyz:EWZ'])
        for session in sessions:
            session.close.assert_called_once()

    def test_slow_monitor_http_does_not_hold_personal_requests(self):
        monitor_started, release_monitor = threading.Event(), threading.Event()
        personal_finished = threading.Event()
        errors = []
        replies = {'userAbstraction': 'disabled', 'metaAndAssetCtxs': contexts(), 'frontendOpenOrders': []}
        client, monitor_session, sessions = self.client(
            lambda *args, **kwargs: response(replies[kwargs['json']['type']]))
        def monitor_post(*args, **kwargs):
            monitor_started.set()
            if not release_monitor.wait(3):
                raise requests.Timeout()
            return response(state())
        monitor_session.post.side_effect = monitor_post
        def monitor():
            try:
                client.fetch_open_positions(WALLET)
            except Exception as error:
                errors.append(error)
        def personal():
            try:
                client.fetch_portfolio(OTHER)
                personal_finished.set()
            except Exception as error:
                errors.append(error)
        workers = [threading.Thread(target=monitor), threading.Thread(target=personal)]
        workers[0].start()
        try:
            self.assertTrue(monitor_started.wait(2))
            workers[1].start()
            self.assertTrue(personal_finished.wait(2))
            self.assertTrue(workers[0].is_alive())
            monitor_session.close.assert_not_called()
        finally:
            release_monitor.set()
            for worker in workers:
                if worker.ident is not None:
                    worker.join(3)
        self.assertFalse(errors)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        for session in sessions:
            session.close.assert_called_once()

    def test_parallel_requests_share_monitor_weight_budget_atomically(self):
        clock, barrier = Clock(), threading.Barrier(4)
        monitor_session, personal_session = Mock(), Mock()
        monitor_session.post.return_value = response({'assetPositions': []})
        personal_session.post.return_value = response({})
        client = HyperliquidClient(monitor_session, clock=clock, weight_budget=42)
        client.fetch_open_positions(WALLET)  # 2 units, leaving room for two 20-unit requests.
        def fetch():
            barrier.wait(2)
            try:
                client._request({'type': 'metaAndAssetCtxs'}, session=personal_session)
                return True
            except DataFetchError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            successes = list(pool.map(lambda _: fetch(), range(4)))
        self.assertEqual(sum(successes), 2)
        self.assertEqual(personal_session.post.call_count, 2)
        self.assertEqual(sum(call[1] for call in client.calls), 42)
        clock.advance(60)
        client.fetch_open_positions(WALLET)
        self.assertEqual(monitor_session.post.call_count, 2)

    def test_personal_429_sets_shared_monitor_cooldown(self):
        clock = Clock()
        client, monitor_session, sessions = self.client(Mock(), clock=clock)
        failed = response(None)
        failed.status_code, failed.headers = 429, {'Retry-After': '30'}
        failed.raise_for_status.side_effect = requests.HTTPError()
        personal_session = Mock()
        personal_session.post.return_value = failed
        with self.assertRaises(DataFetchError):
            client._request({'type': 'userAbstraction'}, session=personal_session)
        with self.assertRaises(DataFetchError):
            client.fetch_open_positions(WALLET)
        monitor_session.post.assert_not_called()
        self.assertEqual(client.blocked_until, clock() + 30)

    def test_personal_threads_only_update_personal_heartbeat_and_close_sessions_on_error(self):
        def failed(*args, **kwargs):
            raise requests.Timeout()
        client, _, sessions = self.client(failed)
        monitor_beat, personal_beat = Mock(), Mock()
        client.set_heartbeat(monitor_beat)
        errors = []
        def fetch():
            client.set_heartbeat(personal_beat)
            try:
                client.fetch_portfolio(WALLET)
            except DataFetchError as error:
                errors.append(error)
        worker = threading.Thread(target=fetch)
        worker.start()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        monitor_beat.assert_not_called()
        self.assertGreaterEqual(personal_beat.call_count, 3)
        for session in sessions:
            session.close.assert_called_once()
