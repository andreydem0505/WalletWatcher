"""Command replies, authorization, status dates and durable wallet management."""
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from walletwatcher.domain.models import Account
from walletwatcher.storage.wallets import load_accounts, save_wallets
from support import ApplicationFixture, WALLET, OTHER


class CommandBehaviorTests(ApplicationFixture):
    def test_help_explains_all_commands_and_replies_only_to_requesting_chat(self):
        self.app.config = replace(self.config, mode='TEST')
        for command in ('/help', '/HELP@wallet_bot'):
            before = len(self.jobs())
            self.app.handle_message(self.message(command, chat=2))
            jobs = self.jobs()[before:]
            text = ''.join(job['text'] for job in jobs)
            self.assertEqual(len(jobs), 1)
            self.assertTrue(all(job['chat'] == 2 for job in jobs))
            self.assertTrue(all(not job['html'] for job in jobs))  # Literal argument placeholders.
            for syntax in ('/addwallet <address>', '/removewallet <address>',
                           '/settag <address> <tag>', '/creds <address> <private_key>', '/my',
                           '/autoclose <wallet> <ticker>', '/cancelautoclose <wallet> <ticker>',
                           '/autocloses', '/intersections', '/health', '/wallet <address>'):
                self.assertIn(syntax, text)
            self.assertNotIn('/help', text)
            self.assertNotRegex(text, '[А-Яа-яЁё]')
            self.assertIn('Only available in a private chat', text)
            self.assertIn('up to 100 characters', text)
            self.assertIn('Any other message', text)
            self.assertTrue(all(len(job['text']) <= 1500 for job in jobs))
        self.assertFalse(self.config.wallets_file.exists())
        self.assertTrue(self.app.personal_queue.empty())
        self.client.fetch_portfolio.assert_not_called()

    def test_help_rejects_arguments_and_respects_chat_allowlist(self):
        self.app.handle_message(self.message('/help', chat=999))
        self.assertEqual(self.queue.pending_count(), 0)
        self.app.handle_message(self.message('/help unexpected'))
        self.assertEqual(self.jobs()[0]['text'], 'Usage: /help')

    def test_status_dates_use_moscow_seconds_without_timezone(self):
        timestamp = datetime(2026, 10, 17, 22, 34, 56, 987654, tzinfo=timezone.utc).timestamp()
        self.app.last_cycle = timestamp
        self.app.accounts = {WALLET: Account(last_updated=timestamp, failures=3)}
        self.app.handle_message(self.message('anything'))
        text = self.jobs()[0]['text']
        self.assertIn('Last monitoring cycle: 2026-10-18 01:34:56\n', text)
        self.assertIn('Last success: 2026-10-18 01:34:56; failures: 3', text)
        self.assertNotIn('+03:00', text)
        self.assertNotIn('.987654', text)

    def test_empty_status_contains_no_spurious_wallet(self):
        self.app.handle_message(self.message(''))
        self.assertEqual(self.jobs()[0]['text'],
                         'Wallets: 0\n❗ Last monitoring cycle: not yet\nPending notifications: 0\n\nTracked wallets:')

    def test_status_follows_current_file_order_and_keeps_monitoring_state(self):
        third = '0x' + '1' * 40
        self.app.accounts = {third: Account(), WALLET: Account(), OTHER: Account('live tag', failures=3)}
        original = self.app.accounts[OTHER]
        for order in ((OTHER, third, WALLET), (WALLET, OTHER, third)):
            self.config.wallets_file.write_text('# Wallet order\n' + '\n'.join(f'{wallet};' for wallet in order))
            self.app.handle_message(self.message('status'))
            text = self.jobs()[-1]['text']
            self.assertTrue(text.startswith('Wallets: 3\n'))
            self.assertEqual(sorted(order, key=lambda wallet: text.index(f'<code>{wallet}</code>')), list(order))
            self.assertIn(f'<code>{OTHER}</code>\nlive tag\n❗ Last success: not yet; failures: 3', text)
            self.assertIs(self.app.accounts[OTHER], original)

    def test_wallet_commands_preserve_file_order_and_append_new_wallets(self):
        third = '0x' + '1' * 40
        self.app.accounts = {WALLET: Account(), OTHER: Account()}
        self.config.wallets_file.write_text(f'{OTHER};\n{WALLET};\n')
        for command, order in ((f'/settag {WALLET} whale', [OTHER, WALLET]),
                               (f'/addwallet {third}', [OTHER, WALLET, third]),
                               (f'/removewallet {OTHER}', [WALLET, third]),
                               (f'/addwallet {OTHER}', [WALLET, third, OTHER])):
            with self.subTest(command=command):
                self.app.handle_message(self.message(command))
                self.assertEqual(list(load_accounts(self.config.wallets_file)), order)
                self.assertEqual(list(self.app.accounts), order)
                self.app.handle_message(self.message('status'))
                text = self.jobs()[-1]['text']
                self.assertTrue(text.startswith(f'Wallets: {len(order)}\n'))
                self.assertEqual(sorted(order, key=lambda wallet: text.index(f'<code>{wallet}</code>')), order)

    def test_unreadable_wallet_order_keeps_status_available(self):
        self.app.accounts = {WALLET: Account()}
        with patch('walletwatcher.services.monitoring.load_accounts', side_effect=PermissionError()), \
                self.assertLogs('walletwatcher.services.monitoring', level='WARNING'):
            self.app.handle_message(self.message('status'))
        self.assertTrue(self.jobs()[0]['text'].startswith('Wallets: 1\n'))
        self.assertIn(f'<code>{WALLET}</code>', self.jobs()[0]['text'])

    def test_group_member_can_manage_wallets_in_authorized_chat(self):
        self.app.handle_message(self.message(f'/ADDWALLET@wallet_bot {WALLET.upper()}', user=999))
        self.assertIn(WALLET, load_accounts(self.config.wallets_file))
        self.assertEqual({job['chat'] for job in self.jobs()}, {1, 2})

    def test_duplicate_add_and_missing_remove_reply_only_to_requester(self):
        self.app.accounts = {WALLET: Account()}
        for text, expected in ((f'/addwallet {WALLET}', 'already being tracked'),
                               (f'/removewallet {OTHER}', 'not being tracked'),
                               (f'/settag {OTHER} whale', 'not being tracked')):
            self.app.handle_message(self.message(text, chat=2))
            self.assertEqual(self.jobs()[-1]['chat'], 2)
            self.assertIn(expected, self.jobs()[-1]['text'])
        self.assertFalse(self.config.wallets_file.exists())

    def test_invalid_commands_do_not_change_saved_wallets(self):
        self.app.accounts = {WALLET: Account('original')}
        save_wallets(self.app.accounts, self.config.wallets_file)
        commands = ('/addwallet', f'/addwallet {OTHER} extra', '/removewallet bad',
                    f'/settag {WALLET}', f'/settag {WALLET} a;b',
                    f'/settag {WALLET} ' + 'x' * 101)
        for command in commands:
            with self.subTest(command=command):
                self.app.handle_message(self.message(command))
                self.assertEqual(load_accounts(self.config.wallets_file)[WALLET].tag, 'original')
                self.assertEqual(set(self.app.accounts), {WALLET})
                self.assertEqual(self.jobs()[-1]['chat'], 1)

    def test_failed_tag_save_preserves_live_and_persistent_tag(self):
        account = Account('original')
        self.app.accounts = {WALLET: account}
        save_wallets(self.app.accounts, self.config.wallets_file)
        with patch('walletwatcher.services.monitoring.save_wallets', side_effect=OSError('disk full')), self.assertLogs('walletwatcher.presentation.telegram', level='ERROR'):
            self.app.handle_message(self.message(f'/settag {WALLET} changed'))
        self.assertIs(self.app.accounts[WALLET], account)
        self.assertEqual(account.tag, 'original')
        self.assertEqual(load_accounts(self.config.wallets_file)[WALLET].tag, 'original')
        self.assertIn('no success was confirmed', self.jobs()[0]['text'])

    def test_remove_cancels_queued_trade_alerts(self):
        self.app.accounts = {WALLET: Account()}
        self.queue.enqueue(1, 'trade alert', key=f'wallet:{WALLET}:1', generation='one')
        self.app.handle_message(self.message(f'/removewallet {WALLET}'))
        self.assertFalse(load_accounts(self.config.wallets_file))
        self.assertTrue(all(not job['key'].startswith('wallet:') for job in self.jobs()))
        self.assertTrue(all('removed' in job['text'] for job in self.jobs()))

    def test_test_mode_broadcasts_only_to_admin_but_replies_to_requester(self):
        self.app.config = replace(self.config, mode='TEST')
        self.app.handle_message(self.message(f'/addwallet {WALLET}', chat=2))
        self.assertEqual([job['chat'] for job in self.jobs()], [1])
        self.app.handle_message(self.message('status', chat=2))
        self.assertEqual(self.jobs()[-1]['chat'], 2)
        self.assertTrue(self.jobs()[-1]['html'])

    def test_plain_replies_split_without_enabling_html(self):
        self.app.reply(1, '<>&' * 1001)
        jobs = self.jobs()
        self.assertEqual(''.join(job['text'] for job in jobs), '<>&' * 1001)
        self.assertEqual([len(job['text']) for job in jobs], [1500, 1500, 3])
        self.assertTrue(all(not job['html'] for job in jobs))


class StatusAttentionTests(ApplicationFixture):
    def setUp(self):
        super().setUp()
        self.app.accounts = {WALLET: Account(tag='Whale <BTC>', last_updated=self.clock() - 10)}
        self.app.last_cycle = self.clock()

    def report(self):
        before = len(self.jobs())
        with patch('time.time', self.clock):
            self.app.handle_message(self.message('any text'))
        return '\n'.join(job['text'] for job in self.jobs()[before:])

    def test_healthy_status_and_routine_pending_messages_have_no_warning_marker(self):
        self.queue.enqueue(1, 'private queued reply')
        self.queue.db.execute('INSERT INTO chats VALUES (?, ?)', (1, self.clock() + 3.1))
        self.queue.db.commit()
        text = self.report()
        self.assertNotIn('❗', text)
        self.assertIn('Pending notifications: 1', text)
        self.assertIn(f'<code>{WALLET}</code>\nWhale &lt;BTC&gt;\nLast success:', text)
        self.assertNotIn('private queued reply', text)
        self.client.fetch_open_positions.assert_not_called()
        self.client.fetch_portfolio.assert_not_called()

    def test_only_failing_or_unchecked_wallet_lines_are_marked(self):
        self.app.accounts[WALLET].failures = 2
        self.app.accounts[OTHER] = Account()
        text = self.report()
        self.assertEqual(text.count('❗'), 2)
        self.assertIn('❗ Last success: 1970-01-01 05:46:30; failures: 2', text)
        self.assertIn(f'<code>{OTHER}</code>\n❗ Last success: not yet; failures: 0', text)
        self.assertTrue(text.startswith('Wallets: 2\nLast monitoring cycle:'))

    def test_stale_data_uses_the_same_configured_threshold_as_health(self):
        self.app.config = replace(self.config, poll_interval=30)
        self.app.last_cycle = self.clock() - 90
        self.app.accounts[WALLET].last_updated = self.clock() - 90
        self.assertNotIn('❗', self.report())
        self.clock.advance(0.1)
        text = self.report()
        self.assertIn('❗ Last monitoring cycle:', text)
        self.assertIn('❗ Last success:', text)
        self.assertNotIn('❗ Pending notifications:', text)

    def test_delivery_retries_cooldown_wait_and_overdue_queue_are_marked(self):
        cases = (dict(retrying=1), dict(cooldown=1), dict(reply_wait=11), dict(overdue=31))
        for changes in cases:
            with self.subTest(changes=changes):
                diagnostics = dict(pending=2, retrying=0, cooldown=0, reply_wait=0, overdue=0) | changes
                with patch.object(self.queue, 'diagnostics', return_value=diagnostics):
                    text = self.report()
                self.assertIn('❗ Pending notifications: 2', text)
                self.assertNotIn('❗ Last success:', text)

    def test_delivery_grace_boundaries_are_not_marked(self):
        diagnostics = dict(pending=2, retrying=0, cooldown=0, reply_wait=10, overdue=30)
        with patch.object(self.queue, 'diagnostics', return_value=diagnostics):
            self.assertNotIn('❗', self.report())
