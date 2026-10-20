"""Private, read-only listing of durable autoclose rules."""
from dataclasses import replace
from unittest.mock import patch

from support import ApplicationFixture, WALLET, OTHER
from walletwatcher.storage.autoclose import AutocloseStore


class AutocloseListTests(ApplicationFixture):
    def command(self, text='/autocloses', user=1, chat=None, kind='private'):
        message = self.message(text, chat=user if chat is None else chat, user=user)
        message.chat.type = kind
        self.app.handle_message(message)

    def create(self, user=1, wallet=WALLET, ticker='BTC', state='armed'):
        rule = self.app.autoclose_store.create(user, wallet, ticker, 'test-version')
        rule['state'] = state
        self.app.autoclose_store.save(rule)
        return rule

    def test_exact_copyable_format_and_creation_order_for_own_rules_only(self):
        self.create(wallet=OTHER)
        self.create(wallet=WALLET, ticker='xyz:SPCX', state='closing')
        self.create(user=2, ticker='ETH')
        other_bot = AutocloseStore(self.queue, 'another-bot')
        other_bot.create(1, WALLET, 'SOL', 'test-version')
        self.app.config = replace(self.config, mode='TEST', admin_id=2)
        with patch.object(self.app.credentials, 'load') as decrypt, \
                patch.object(self.app.credentials, 'version') as credentials:
            self.command()
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['chat'], 1)
        self.assertTrue(jobs[0]['html'])
        self.assertEqual(jobs[0]['text'],
                         f'<code>{OTHER}</code> BTC\n<code>{WALLET}</code> xyz:SPCX')
        decrypt.assert_not_called()
        credentials.assert_not_called()
        self.assertEqual(self.client.mock_calls, [])

    def test_empty_without_credentials_and_case_insensitive_bot_mention(self):
        self.command('/AUTOCLOSES@wallet_bot')
        self.assertEqual(self.jobs()[0]['text'], 'You have no active autoclose rules.')
        self.assertFalse(self.jobs()[0]['html'])

    def test_cancelled_and_terminal_rules_are_excluded_immediately(self):
        store = self.app.autoclose_store
        for index, state in enumerate(('done', 'failed', 'cancelled')):
            rule = self.create(ticker=f'COIN{index}')
            store.finish(rule, state, 'finished', notify=False)
        self.create(ticker='ETH')
        store.cancel(1, WALLET, 'ETH')  # Worker has not yet finalized the rule.
        self.create(wallet=OTHER, ticker='SOL')
        store.cancel_source(OTHER)
        self.create(ticker='BTC', state='queued')
        self.command()
        self.assertEqual(self.jobs()[0]['text'], f'<code>{WALLET}</code> BTC')
        self.assertEqual(len(store.active()), 3)  # Listing does not mutate the worker journal.

    def test_private_authenticated_owner_and_allowlist_are_required(self):
        self.create()
        with patch.object(self.app.autoclose_store, 'for_user') as query:
            for user, chat, kind in ((1, 1, 'group'), (2, 1, 'private'),
                                     (None, 1, 'private'), (True, 1, 'private')):
                self.command(user=user, chat=chat, kind=kind)
                self.assertIn('private chat', self.jobs()[-1]['text'])
            count = len(self.jobs())
            self.command(user=99)
            self.assertEqual(len(self.jobs()), count)
            query.assert_not_called()

    def test_arguments_are_rejected_without_listing_rules(self):
        self.create()
        self.command('/autocloses unexpected')
        self.assertEqual(self.jobs()[0]['text'], 'Usage: /autocloses')

    def test_long_list_splits_only_between_complete_rows(self):
        lines = []
        for index in range(60):
            ticker = f'xyz:COIN{index}'
            self.create(ticker=ticker)
            lines.append(f'<code>{WALLET}</code> {ticker}')
        self.command()
        jobs = self.jobs()
        self.assertGreater(len(jobs), 1)
        self.assertEqual('\n'.join(job['text'] for job in jobs), '\n'.join(lines))
        for job in jobs:
            self.assertLessEqual(len(job['text']), 1500)
            self.assertEqual(job['chat'], 1)
            self.assertTrue(job['html'])
            for row in job['text'].splitlines():
                self.assertRegex(row, r'^<code>0x[a-f0-9]{40}</code> xyz:COIN\d+$')

    def test_help_documents_the_command(self):
        self.app._command(1, '/help')
        self.assertIn('/autocloses', '\n'.join(job['text'] for job in self.jobs()))
