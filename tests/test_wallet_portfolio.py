"""Explicit-address portfolios reuse /my formatting without credentials or monitoring."""
from dataclasses import replace
import queue
import threading
from unittest.mock import Mock, patch

from support import ApplicationFixture, WALLET, OTHER, position
from test_personal_accounts import KEY, snapshot
from walletwatcher.domain.models import Account
from walletwatcher.hyperliquid.errors import DataFetchError
from walletwatcher.presentation.portfolio import Portfolio, PersonalPosition


class WalletPortfolioTests(ApplicationFixture):
    def command(self, text=None, chat=1, user=1, kind='private'):
        message = self.message(text if text is not None else f'/wallet {WALLET}', chat=chat, user=user)
        message.chat.type = kind
        self.app.handle_message(message)

    def test_any_address_is_fetched_fresh_without_reading_or_writing_credentials(self):
        self.app.accounts[WALLET] = Account(positions=[position(ticker='CACHED')])
        self.client.fetch_portfolio.return_value = snapshot()
        with patch.object(self.app.credentials, 'load', side_effect=AssertionError('no decrypt')), \
                patch.object(self.app.credentials, 'get_address', side_effect=AssertionError('no saved address')), \
                patch.object(self.app.credentials, 'save', side_effect=AssertionError('no mutation')):
            self.command(f'/WALLET@wallet_bot {OTHER.upper()}')
            self.client.fetch_portfolio.assert_not_called()
            self.assertTrue(self.app.process_personal_request())
        self.client.fetch_portfolio.assert_called_once_with(OTHER)
        self.assertEqual(self.jobs()[0]['text'],
                         '<b>Balance: $77.43</b>\n\n<b>VVV Short 3 cross</b>\nMargin: $23.05\n'
                         'Entry Price: 22.1841\nCur Price: 23.2312\nP&amp;L: -$0.16 (-0.8%)\n'
                         'Funding: -$0.23\n🟢 TP: 20.0\n🔴 SL: 25.2')
        self.assertTrue(self.jobs()[0]['html'])
        self.assertEqual(list(self.app.accounts), [WALLET])
        self.assertEqual(self.app.accounts[WALLET].positions[0].ticker, 'CACHED')
        self.assertFalse(self.app.wallet_pending)
        self.assertFalse(self.app.personal_pending)
        self.assertFalse(self.app.autoclose_store.active())

    def test_output_is_identical_to_my_for_the_same_account(self):
        self.app.credentials.save(1, WALLET, KEY)
        self.client.fetch_portfolio.return_value = snapshot()
        self.command('/my')
        self.app.process_personal_request()
        self.command()
        self.app.process_personal_request()
        self.assertEqual(self.jobs()[0]['text'], self.jobs()[1]['text'])
        self.assertEqual(self.jobs()[0]['html'], self.jobs()[1]['html'])
        self.assertEqual([call.args[0] for call in self.client.fetch_portfolio.call_args_list], [WALLET, WALLET])

    def test_public_wallet_replies_only_to_requesting_chat_including_groups_and_test_mode(self):
        self.app.config = replace(self.config, mode='TEST')
        self.client.fetch_portfolio.return_value = snapshot()
        self.command(chat=2, user=999, kind='group')
        self.assertTrue(self.app.process_personal_request())
        self.assertEqual([job['chat'] for job in self.jobs()], [2])
        self.assertIsNone(self.app.credentials.get_address(999))

    def test_invalid_arguments_and_unauthorized_chats_never_queue_work_or_echo_secrets(self):
        self.command(chat=999)
        self.assertFalse(self.jobs())
        for text in ('/wallet', '/wallet bad', f'/wallet {WALLET} extra', f'/wallet {KEY}',
                     f'/wallet {WALLET} {KEY}'):
            with self.subTest(text=text.split()[0]):
                self.command(text)
                self.assertNotIn(KEY, self.jobs()[-1]['text'])
                self.assertEqual(self.jobs()[-1]['chat'], 1)
        self.assertTrue(self.app.personal_queue.empty())
        self.assertFalse(self.app.wallet_pending)
        self.client.fetch_portfolio.assert_not_called()

    def test_duplicates_share_pending_result_but_new_commands_fetch_again_after_completion(self):
        self.client.fetch_portfolio.return_value = snapshot()
        self.command()
        self.command(f'/wallet {WALLET.upper()}')
        self.assertEqual(self.app.personal_queue.qsize(), 1)
        self.assertTrue(self.app.process_personal_request())
        self.assertFalse(self.app.wallet_pending)
        self.command()
        self.app.process_personal_request()
        self.assertEqual(self.client.fetch_portfolio.call_count, 2)
        self.assertEqual(len(self.jobs()), 2)

    def test_different_addresses_and_chats_have_independent_pending_results(self):
        self.client.fetch_portfolio.return_value = snapshot()
        for address, chat in ((WALLET, 1), (OTHER, 1), (WALLET, 2)):
            self.command(f'/wallet {address}', chat=chat)
        self.assertEqual(self.app.personal_queue.qsize(), 3)
        for _ in range(3):
            self.app.process_personal_request()
        self.assertEqual([call.args[0] for call in self.client.fetch_portfolio.call_args_list], [WALLET, OTHER, WALLET])
        self.assertEqual([job['chat'] for job in self.jobs()], [1, 1, 2])
        self.assertFalse(self.app.wallet_pending)

    def test_full_shared_queue_preserves_pending_sets_and_can_retry_after_completion(self):
        self.app.personal_queue = queue.Queue(maxsize=1)
        self.app.credentials.save(1, WALLET, KEY)
        self.client.fetch_portfolio.return_value = snapshot()
        self.command('/my')
        self.command(f'/wallet {OTHER}')
        self.assertEqual(self.app.personal_pending, {1})
        self.assertFalse(self.app.wallet_pending)
        self.assertIn('Очередь запросов /wallet заполнена', self.jobs()[0]['text'])
        self.app.process_personal_request()
        self.command(f'/wallet {OTHER}')
        self.app.process_personal_request()
        self.assertFalse(self.app.wallet_pending)
        self.command()
        self.command('/my')
        self.assertIn('Очередь запросов /my заполнена', self.jobs()[-1]['text'])
        self.assertFalse(self.app.personal_pending)
        self.app.process_personal_request()

    def test_credentials_change_still_rejects_old_my_result_but_not_explicit_wallet(self):
        self.app.credentials.save(1, OTHER, KEY)
        self.command('/my')
        self.command()
        def fetch(address):
            self.app.credentials.save(1, WALLET, KEY)
            return snapshot()
        self.client.fetch_portfolio.side_effect = fetch
        self.app.process_personal_request()
        self.app.process_personal_request()
        self.assertIn('Данные аккаунта изменились', self.jobs()[0]['text'])
        self.assertIn('Balance:', self.jobs()[1]['text'])
        self.assertEqual(self.app.credentials.get_address(1), WALLET)

    def test_failure_is_safe_releases_pending_and_does_not_prevent_next_request(self):
        for error, level in ((DataFetchError('busy', reason='trading_priority'), 'WARNING'),
                             (RuntimeError(KEY), 'ERROR')):
            with self.subTest(error=type(error).__name__):
                self.client.fetch_portfolio.side_effect = error
                self.command()
                before = len(self.jobs())
                with self.assertLogs('walletwatcher.services.portfolios', level=level) as logs:
                    self.app.process_personal_request()
                self.assertFalse(self.app.wallet_pending)
                self.assertNotIn(KEY, '\n'.join(logs.output))
                self.assertNotIn(KEY, self.jobs()[before]['text'])
                self.assertNotIn('Balance:', self.jobs()[before]['text'])
                if isinstance(error, DataFetchError):
                    self.assertIn(f'/wallet {WALLET}', self.jobs()[before]['text'])
                self.client.fetch_portfolio.side_effect = None
                self.client.fetch_portfolio.return_value = snapshot()
                self.command()
                self.app.process_personal_request()
                self.assertIn('Balance:', self.jobs()[-1]['text'])

    def test_shutdown_before_and_during_request_drops_work_and_clears_pending(self):
        self.command()
        self.app.stop.set()
        self.app.process_personal_request()
        self.client.fetch_portfolio.assert_not_called()
        self.assertFalse(self.app.wallet_pending)
        self.app.stop.clear()
        self.command()
        def stopping(address):
            self.app.stop.set()
            return snapshot()
        self.client.fetch_portfolio.side_effect = stopping
        self.app.process_personal_request()
        self.assertFalse(self.jobs())
        self.assertFalse(self.app.wallet_pending)

    def test_access_revoked_before_or_during_fetch_discards_result_and_error(self):
        self.command(chat=2)
        self.app.config = replace(self.config, chat_ids=(1,))
        self.app.process_personal_request()
        self.client.fetch_portfolio.assert_not_called()
        self.assertFalse(self.app.wallet_pending)
        for error in (None, DataFetchError('unavailable'), RuntimeError(KEY)):
            self.app.config = self.config
            self.command(chat=2)
            def revoked(address):
                self.app.config = replace(self.config, chat_ids=(1,))
                if error is not None:
                    raise error
                return snapshot()
            self.client.fetch_portfolio.side_effect = revoked
            self.app.process_personal_request()
            self.assertFalse(self.app.wallet_pending)
        self.assertFalse(self.jobs())

    def test_slow_wallet_fetch_does_not_block_commands_or_monitoring_and_deduplicates_in_flight(self):
        started, release = threading.Event(), threading.Event()
        def fetch(address):
            started.set()
            self.assertTrue(release.wait(3))
            return snapshot()
        self.client.fetch_portfolio.side_effect = fetch
        self.command()
        worker = threading.Thread(target=self.app.process_personal_request)
        worker.start()
        try:
            self.assertTrue(started.wait(2))
            self.command()
            self.assertTrue(self.app.personal_queue.empty())
            self.command('/help')
            self.assertIn('/wallet <address>', self.jobs()[0]['text'])
            self.app.accounts[OTHER] = Account()
            self.client.fetch_open_positions.return_value = []
            self.app.poll_wallet(OTHER, self.app.accounts[OTHER])
            self.assertIsNotNone(self.app.accounts[OTHER].last_updated)
            self.assertNotIn('Balance:', '\n'.join(job['text'] for job in self.jobs()))
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertIn('Balance:', self.jobs()[-1]['text'])
        self.assertFalse(self.app.wallet_pending)

    def test_empty_and_large_portfolios_use_same_delivery_format_without_omitting_positions(self):
        self.client.fetch_portfolio.return_value = Portfolio(0, [])
        self.command()
        self.app.process_personal_request()
        self.assertEqual(self.jobs()[0]['text'], '<b>Balance: $0.00</b>\n\nNo open positions.')
        positions = [PersonalPosition(position(ticker=f'COIN{index}'), 23.05, '100', 0.1, 0)
                     for index in range(100)]
        self.client.fetch_portfolio.return_value = Portfolio(77.43, positions)
        self.command(chat=2)
        self.app.process_personal_request()
        jobs = self.jobs()[1:]
        self.assertGreater(len(jobs), 1)
        self.assertTrue(all(job['chat'] == 2 and job['html'] for job in jobs))
        text = '\n'.join(job['text'] for job in jobs)
        for index in range(100):
            self.assertEqual(text.count(f'COIN{index} Long'), 1)
