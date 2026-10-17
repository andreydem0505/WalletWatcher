"""Command replies, authorization, status dates and durable wallet management."""
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from models import Account
from serialization import load_accounts, save_wallets
from support import ApplicationFixture, WALLET, OTHER


class CommandBehaviorTests(ApplicationFixture):
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
                         'Last monitoring cycle: not yet\nPending notifications: 0\n\nTracked wallets:')

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
        with patch('bot.save_wallets', side_effect=OSError('disk full')), self.assertLogs('bot', level='ERROR'):
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
