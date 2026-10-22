"""Health snapshots must be accurate, read-only, private and independent of HTTP."""
import copy
import threading
import unittest
from unittest.mock import Mock, patch

from support import ApplicationFixture, Clock, WALLET, OTHER
from walletwatcher.domain.models import Account
from walletwatcher.hyperliquid.client import HyperliquidClient
from walletwatcher.hyperliquid.errors import DataFetchError
from walletwatcher.hyperliquid.rate_limit import RequestBudget
from walletwatcher.infrastructure.runtime import WorkerSupervisor
from walletwatcher.presentation.health import WORKER_NAMES


class BudgetDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.budget = RequestBudget(clock=self.clock)

    def test_actual_weights_and_expiry_are_reported_without_mutating_reservations(self):
        old = self.budget.reserve('meta')
        self.clock.advance(59)
        page = self.budget.reserve('userFills')
        self.budget.record_response(page, 'userFills', [None])
        self.assertEqual(self.budget.diagnostics()['used'], 41)
        self.clock.advance(1)
        before = copy.deepcopy(self.budget.calls)
        self.assertEqual(self.budget.diagnostics()['used'], 21)
        self.assertEqual(self.budget.calls, before)
        self.assertIn(old, self.budget.calls)
        self.clock.advance(60)
        self.assertEqual(self.budget.diagnostics()['used'], 0)

    def test_reports_real_admission_constraints_without_reserving_weight(self):
        self.budget.limit = 200
        for _ in range(10):
            self.budget.reserve('meta')
        self.assertEqual(self.budget.diagnostics()['monitor_pause'], 'request budget is busy')
        self.assertEqual(self.budget.diagnostics()['monitor_pause_reason'], 'budget')
        self.budget.set_trading_priority(True)
        self.budget.set_trading_rule('rule')
        with self.assertRaises(DataFetchError):
            self.budget.reserve('userFills')
        demand = {owner: dict(values) for owner, values in self.budget.trading_waiters.items()}
        self.assertEqual(self.budget.diagnostics()['pending'], 120)
        self.assertEqual(self.budget.trading_waiters, demand)
        self.assertEqual(sum(call[1] for call in self.budget.calls), 200)

    def test_scheduled_capacity_exchange_cooldown_and_exclusive_priority(self):
        for _ in range(44):
            self.budget.reserve('meta')
        self.budget.schedule_trading_check(124, 5)
        result = self.budget.diagnostics()
        self.assertTrue(result['scheduled'])
        self.assertIn('scheduled autoclose', result['monitor_pause'])
        self.assertEqual(result['monitor_pause_reason'], 'trading_capacity')
        self.budget.defer(20)
        self.assertEqual(self.budget.diagnostics()['cooldown'], 20)
        self.assertEqual(self.budget.diagnostics()['monitor_pause'], 'exchange cooldown')
        self.assertEqual(self.budget.diagnostics()['monitor_pause_reason'], 'rate_limit')
        self.budget.set_close_priority('rule', True)
        self.assertEqual(self.budget.diagnostics()['monitor_pause'], 'automatic closing has priority')
        self.assertEqual(self.budget.diagnostics()['monitor_pause_reason'], 'trading_priority')
        self.budget.set_close_priority('rule', False)
        self.budget.reserve_for_trading(False)
        self.clock.advance(60)
        result = self.budget.diagnostics()
        self.assertIsNone(result['monitor_pause'])
        self.assertIsNone(result['monitor_pause_reason'])
        self.assertFalse(result['scheduled'])
        self.assertEqual(result['cooldown'], 0)


class WorkerDiagnosticsTests(unittest.TestCase):
    def test_reports_running_stalled_stopped_and_restart_wait_without_taking_action(self):
        stop, ready = threading.Event(), threading.Event()
        clock = Clock()
        supervisor = WorkerSupervisor(stop, timeout=10, clock=clock)
        self.assertEqual(supervisor.diagnostics(), {})
        def worker(beat):
            beat()
            ready.set()
            stop.wait()
        supervisor.add('monitor', worker)
        try:
            self.assertTrue(ready.wait(2))
            self.assertEqual(supervisor.diagnostics()['monitor']['state'], 'running')
            clock.advance(11)
            snapshot = supervisor.diagnostics()
            self.assertEqual(snapshot['monitor'], dict(state='stalled', heartbeat_age=11, restart_in=0))
            snapshot['monitor']['heartbeat_age'] = 999
            self.assertEqual(supervisor.diagnostics()['monitor']['heartbeat_age'], 11)
        finally:
            stop.set()
            self.assertTrue(supervisor.join(2))
        self.assertEqual(supervisor.diagnostics()['monitor']['state'], 'stopped')
        stop.clear()
        supervisor.workers['monitor']['restart_at'] = clock() + 3
        with patch.object(supervisor, '_start') as restart:
            result = supervisor.diagnostics()['monitor']
        self.assertEqual(result['state'], 'awaiting restart')
        self.assertEqual(result['restart_in'], 3)
        restart.assert_not_called()
        supervisor.workers['monitor']['thread'] = None
        self.assertEqual(supervisor.diagnostics()['monitor']['state'], 'starting')


class HealthCommandTests(ApplicationFixture):
    def setUp(self):
        super().setUp()
        self.http = HyperliquidClient(session=Mock(), clock=self.clock)
        self.addCleanup(self.http.close)
        self.app.client = self.http
        self.app.started_at = self.clock() - 120

    def command(self, text='/health', user=1, chat=1, kind='private'):
        message = self.message(text, chat=chat, user=user)
        message.chat.type = kind
        with patch('time.time', self.clock), patch('time.monotonic', self.clock):
            self.app.handle_message(message)

    def report(self, start=0):
        return '\n'.join(job['text'] for job in self.jobs()[start:])

    def test_empty_startup_is_explicit_and_reads_do_not_use_http_or_credentials(self):
        with patch.object(self.app.credentials, 'load') as decrypt:
            self.command()
        text = self.report()
        self.assertIn('Uptime: 120s', text)
        self.assertIn('Wallets: 0; never checked: 0', text)
        self.assertIn('Last cycle: not yet', text)
        self.assertIn('Oldest wallet success: not yet', text)
        self.assertIn('API: 0/1000 | Pending notifications: 0', text)
        self.assertIn('Next position request: available', text)
        self.assertIn('Not started.', text)
        self.assertIn('Health: issues or delays', text)
        self.assertNotIn('<b>Telegram</b>', text)
        self.assertNotIn('<b>Hyperliquid', text)
        self.assertNotIn('<b>Your autoclose rules</b>', text)
        self.assertTrue(all(job['chat'] == 1 and job['html'] for job in self.jobs()))
        self.http.session.post.assert_not_called()
        decrypt.assert_not_called()

    def test_distinguishes_cycle_time_from_old_wallet_data_and_current_failures(self):
        self.app.accounts = {WALLET: Account(last_updated=self.clock() - 65, failures=3),
                             OTHER: Account(), 'third': Account(last_updated=self.clock() - 10)}
        self.app.last_cycle = self.clock()
        self.app.personal_pending.add(1)
        self.app.supervisor = Mock()
        self.app.supervisor.diagnostics.return_value = {
            'monitor': dict(state='running', heartbeat_age=2, restart_in=0),
            'personal': dict(state='stalled', heartbeat_age=181, restart_in=0),
            'delivery': dict(state='awaiting restart', heartbeat_age=4, restart_in=1.2),
        }
        self.command('/HEALTH@wallet_bot')
        text = self.report()
        self.assertIn('Wallets: 3; never checked: 1', text)
        self.assertIn('Wallets with failures: 1', text)
        self.assertIn('Last cycle: 1970-01-01 05:46:40', text)
        self.assertIn('Oldest wallet success: 1970-01-01 05:45:35 (65s ago)', text)
        self.assertNotIn('monitor: running; heartbeat', text)
        self.assertIn('personal: stalled; heartbeat 181s ago', text)
        self.assertIn('delivery: awaiting restart; heartbeat 4s ago; retry in 2s', text)
        self.assertIn('Portfolio requests queued or running: 1', text)
        self.assertEqual(self.app.accounts[WALLET].failures, 3)
        self.assertEqual(self.app.accounts[OTHER].last_updated, None)

    def test_queue_retry_counts_and_waits_do_not_change_delivery_state(self):
        self.queue.enqueue(1, 'private data')
        self.queue.enqueue(2, 'another user data')
        self.queue.db.execute('UPDATE outbox SET attempts=2 WHERE chat=1')
        self.queue.db.execute('INSERT INTO chats VALUES (?, ?)', (1, self.clock() + 30))
        self.queue.db.commit()
        self.queue.defer_global(20)
        before = [tuple(row) for row in self.queue.db.execute('SELECT * FROM outbox')]
        self.command()
        text = self.report(2)
        self.assertIn('Pending notifications: 2; retrying: 1', text)
        self.assertIn('Delivery cooldown: 20s; this chat wait: 30s', text)
        self.assertNotIn('private data', text)
        self.assertNotIn('another user data', text)
        self.assertEqual([tuple(row) for row in self.queue.db.execute('SELECT * FROM outbox LIMIT 2')], before)

    def test_health_works_when_normal_requests_are_blocked_and_http_is_busy(self):
        self.http.budget.defer(60)
        self.http.set_close_priority('rule', True)
        self.http.request_lock.acquire()
        try:
            thread = threading.Thread(target=self.command)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        finally:
            self.http.request_lock.release()
        text = self.report()
        self.assertIn('Next position request: paused — automatic closing has priority', text)
        self.assertIn('Exchange cooldown: 60s', text)
        self.assertEqual(self.http.budget.closing_rules, {'rule'})
        self.http.session.post.assert_not_called()

    def test_private_autoclose_errors_belong_only_to_authenticated_owner(self):
        store = self.app.autoclose_store
        own = store.create(1, WALLET, 'BTC', 'v')
        own.update(state='armed', check_error_since=self.clock(),
                   last_check_error=dict(stage='checking your position fill history', detail='the bot request budget is busy'))
        store.save(own)
        queued = store.create(1, WALLET, 'ETH', 'v')
        queued['activation_error'] = 'activation failed: Hyperliquid rejected the data request (HTTP 422).'
        store.save(queued)
        other = store.create(2, OTHER, 'SECRET-OTHER-TICKER', 'v')
        other.update(state='armed', check_error_since=self.clock(),
                     last_check_error=dict(stage='private stage', detail='another owner error'))
        store.save(other)
        self.command()
        text = self.report()
        self.assertIn('Queued: 1; armed: 1; closing: 0', text)
        self.assertIn('Delayed checks: 2', text)
        self.assertIn('checking your position fill history: the bot request budget is busy', text)
        self.assertIn('HTTP 422', text)
        self.assertNotIn('SECRET-OTHER-TICKER', text)
        self.assertNotIn('another owner error', text)
        before = len(self.jobs())
        own.pop('check_error_since')
        store.save(own)
        self.command()
        self.assertNotIn('bot request budget is busy', self.report(before))

    def test_no_private_diagnostics_in_groups_or_mismatched_private_identity(self):
        for kind, user in (('group', 1), ('private', 2), ('private', True), ('private', None)):
            with self.subTest(kind=kind, user=user):
                before = len(self.jobs())
                with patch.object(self.app.autoclose_store, 'for_user') as private, \
                        patch.object(self.app.autoclose_store, 'unresolved') as orders:
                    self.command(kind=kind, user=user)
                self.assertNotIn('Your autoclose', self.report(before))
                private.assert_not_called()
                orders.assert_not_called()

    def test_pending_orders_include_cancelled_or_failed_own_rules_and_do_not_change_them(self):
        store = self.app.autoclose_store
        own = store.create(1, WALLET, 'BTC', 'v')
        own['pending'] = {'cloid': 'test'}
        store.save(own)
        store.finish(own, 'failed', 'unknown outcome', notify=False)
        self.command()
        self.assertIn('unconfirmed orders: 1', self.report())
        self.assertEqual(store.unresolved(), [(own, False)])

    def test_long_error_report_splits_safely_and_redacts_secrets(self):
        secret = '0x' + '1' * 64
        for index in range(12):
            rule = self.app.autoclose_store.create(1, WALLET, f'xyz:COIN{index}', 'v')
            rule.update(state='armed', check_error_since=self.clock(),
                        last_check_error=dict(stage='checking <history>', detail='x' * 150 + secret + ' 12345:test'))
            self.app.autoclose_store.save(rule)
        self.command()
        text = self.report()
        self.assertNotIn(secret, text)
        self.assertNotIn('12345:test', text)
        self.assertIn('&lt;PRIVATE_KEY&gt;', text)
        self.assertIn('&lt;BOT_TOKEN&gt;', text)
        self.assertIn('checking &lt;history&gt;', text)
        self.assertGreater(len(self.jobs()), 1)
        for job in self.jobs():
            self.assertLessEqual(len(job['text']), 1500)
            self.assertEqual(job['text'].count('<b>'), job['text'].count('</b>'))
            self.assertEqual(job['text'].count('<code>'), job['text'].count('</code>'))

    def test_authorization_arguments_and_help_still_fit_one_message(self):
        self.command(chat=999)
        self.assertFalse(self.jobs())
        self.command('/health unexpected')
        self.assertEqual(self.jobs()[0]['text'], 'Usage: /health')
        self.command('/help')
        self.assertEqual(len(self.jobs()), 2)
        self.assertIn('/health', self.jobs()[-1]['text'])
        self.assertLessEqual(len(self.jobs()[-1]['text']), 1500)

    def ready(self, age=10):
        self.app.accounts = {WALLET: Account(last_updated=self.clock() - age),
                             OTHER: Account(last_updated=self.clock() - age)}
        self.app.last_cycle = self.clock()
        self.app.supervisor = Mock()
        self.app.supervisor.diagnostics.return_value = {
            name: dict(state='running', heartbeat_age=1, restart_in=0) for name in WORKER_NAMES}

    def test_healthy_reply_is_four_lines_without_verbose_sections(self):
        self.ready()
        rule = self.app.autoclose_store.create(1, WALLET, 'BTC', 'v')
        rule['state'] = 'armed'
        self.app.autoclose_store.save(rule)
        for _ in range(17):
            self.http._reserve_weight('meta')
        for _ in range(5):
            self.http._reserve_weight('clearinghouseState')
        self.command()
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.report(), '<b>✅ Health: OK</b>\n'
                         'Wallets: 2 | Workers: 5/5\n'
                         'API: 350/1000 | Pending notifications: 0\n'
                         'Your autoclose rules: 1\n')
        self.http.session.post.assert_not_called()

    def test_routine_queue_and_personal_activity_do_not_expand_report(self):
        self.ready()
        self.queue.enqueue(1, 'queued')
        self.app.personal_pending.add(1)
        self.queue.db.execute('INSERT INTO chats VALUES (?, ?)', (1, self.clock() + 3.1))
        self.queue.db.commit()
        self.command()
        text = self.report(1)
        self.assertIn('Health: OK', text)
        self.assertIn('Pending notifications: 1', text)
        self.assertIn('Portfolio requests queued or running: 1', text)
        self.assertNotIn('<b>Telegram</b>', text)
        self.assertNotIn('Uptime:', text)

    def test_normal_autoclose_priority_is_short_and_does_not_signal_an_error(self):
        self.ready()
        for _ in range(44):
            self.http._reserve_weight('meta')
        self.http.schedule_trading_check(124, 5)
        for closing in (False, True):
            with self.subTest(closing=closing):
                self.http.set_close_priority('rule', closing)
                before = len(self.jobs())
                self.command()
                text = self.report(before)
                self.assertIn('Health: OK', text)
                self.assertIn('Monitoring: autoclose priority', text)
                self.assertNotIn('<b>Hyperliquid', text)
                self.assertNotIn('<b>Monitoring</b>', text)

    def test_staleness_boundary_and_only_affected_section_is_shown(self):
        self.ready(age=60)
        self.command()
        self.assertIn('Health: OK', self.report())
        self.clock.advance(0.1)
        before = len(self.jobs())
        self.command()
        text = self.report(before)
        self.assertIn('Health: issues or delays', text)
        self.assertIn('<b>Monitoring</b>', text)
        self.assertIn('Oldest wallet success:', text)
        for healthy_section in ('<b>Workers</b>', '<b>Telegram</b>', '<b>Hyperliquid', '<b>Your autoclose rules</b>'):
            self.assertNotIn(healthy_section, text)
        self.app.accounts[WALLET].last_updated = self.clock()
        self.app.accounts[OTHER].last_updated = self.clock()
        before = len(self.jobs())
        self.command()
        self.assertIn('Health: OK', self.report(before))
        self.assertNotIn('<b>Monitoring</b>', self.report(before))

    def test_long_configured_poll_interval_gets_appropriate_freshness_window(self):
        from dataclasses import replace
        self.app.config = replace(self.config, poll_interval=30)
        self.ready(age=80)
        self.command()
        self.assertIn('Health: OK', self.report())
        self.clock.advance(11)
        before = len(self.jobs())
        self.command()
        self.assertIn('<b>Monitoring</b>', self.report(before))

    def test_stale_cycle_and_never_checked_wallet_cannot_report_healthy(self):
        self.ready()
        self.app.last_cycle = self.clock() - 61
        self.command()
        self.assertIn('<b>Monitoring</b>', self.report())
        self.app.last_cycle = self.clock()
        self.app.accounts[WALLET].last_updated = None
        before = len(self.jobs())
        self.command()
        self.assertIn('never checked: 1', self.report(before))
        self.assertNotIn('Health: OK', self.report(before))

    def test_missing_and_failed_workers_expand_only_worker_details(self):
        self.ready()
        workers = self.app.supervisor.diagnostics.return_value
        del workers['autoclose']
        workers['personal']['state'] = 'stalled'
        self.command()
        text = self.report()
        self.assertIn('Workers: 3/5', text)
        self.assertIn('autoclose: not started', text)
        self.assertIn('personal: stalled', text)
        self.assertNotIn('monitor: running', text)
        self.assertNotIn('<b>Monitoring</b>', text)

    def test_budget_failure_shows_only_budget_details_and_disappears_after_recovery(self):
        self.ready()
        for _ in range(50):
            self.http._reserve_weight('meta')
        self.command()
        text = self.report()
        self.assertIn('Health: issues or delays', text)
        self.assertIn('<b>Hyperliquid', text)
        self.assertIn('Weight: 1000/1000 (100%)', text)
        self.assertNotIn('<b>Monitoring</b>', text)
        self.clock.advance(60)
        self.deliver_pending()
        self.ready()
        before = len(self.jobs())
        self.command()
        self.assertIn('Health: OK', self.report(before))
        self.assertNotIn('<b>Hyperliquid', self.report(before))

    def test_overdue_queue_and_chat_wait_have_grace_periods(self):
        self.ready()
        self.queue.enqueue(1, 'queued')
        self.clock.advance(30)
        self.queue.db.execute('INSERT INTO chats VALUES (?, ?)', (1, self.clock() + 10))
        self.queue.db.commit()
        self.command()
        self.assertIn('Health: OK', self.report(1))
        self.clock.advance(1)
        before = len(self.jobs())
        self.command()
        text = self.report(before)
        self.assertIn('<b>Telegram</b>', text)
        self.assertIn('Oldest overdue notification: 31s', text)
        self.assertNotIn('<b>Monitoring</b>', text)
        self.queue.db.execute('UPDATE outbox SET due=?', (self.clock() + 60,))
        self.queue.db.execute('UPDATE chats SET due=? WHERE chat=1', (self.clock() + 11,))
        self.queue.db.commit()
        before = len(self.jobs())
        self.command()
        self.assertIn('this chat wait: 11s', self.report(before))

    def test_healthy_group_reply_omits_private_diagnostics_entirely(self):
        self.ready()
        rule = self.app.autoclose_store.create(2, OTHER, 'PRIVATE-TICKER', 'v')
        rule.update(state='armed', check_error_since=self.clock(),
                    last_check_error=dict(stage='private', detail='private problem'))
        self.app.autoclose_store.save(rule)
        self.command(kind='group')
        self.assertEqual(self.report(), '<b>✅ Health: OK</b>\n'
                         'Wallets: 2 | Workers: 5/5\nAPI: 0/1000 | Pending notifications: 0\n')

    def test_health_classification_is_independent_of_diagnostic_wording(self):
        self.ready()
        for cause in ('budget', 'trading_capacity', 'trading_priority'):
            with self.subTest(cause=cause):
                self.http.budget = RequestBudget(clock=self.clock)
                if cause == 'trading_priority':
                    self.http.set_close_priority('rule', True)
                else:
                    for _ in range(50 if cause == 'budget' else 44):
                        self.http._reserve_weight('meta')
                    if cause == 'trading_capacity':
                        self.http.schedule_trading_check(124, 5)
                data = self.http.request_status()
                self.assertEqual(data['monitor_pause_reason'], cause)
                data['monitor_pause'] = 'Different diagnostic wording'
                before = len(self.jobs())
                with patch.object(self.http, 'request_status', return_value=data):
                    self.command()
                text = self.report(before)
                if cause == 'budget':
                    self.assertIn('Health: issues or delays', text)
                    self.assertIn('<b>Hyperliquid', text)
                else:
                    self.assertIn('Health: OK', text)
                    self.assertIn('Monitoring: autoclose priority', text)
                    self.assertNotIn('<b>Hyperliquid', text)
