"""Shared ticker command: exact layout, snapshot scope and access boundaries."""
import copy
import unittest

from support import ApplicationFixture, WALLET, OTHER, position
from walletwatcher.domain.models import Account, PositionSnapshotValue
from walletwatcher.presentation.intersections import intersection_messages


THIRD = '0x' + 'c' * 40


class IntersectionFormattingTests(unittest.TestCase):
    def test_groups_by_ticker_and_keeps_four_line_position_format(self):
        accounts = {
            WALLET: Account('Whale', positions=[position(ticker='ETH'), position(volume=50000, pnl=12.5)]),
            OTHER: Account('Short trader', positions=[position(-1, volume=20000, pnl=-2),
                                                      position(-2, ticker='ETH')]),
        }
        self.assertEqual(intersection_messages(accounts), [
            f'<code>Whale</code>\n<code>{WALLET}</code>\n'
            '<b>BTC Long 5 cross</b>\nVolume: $50K\nEntry Price: 100\nP&amp;L: +$12.5\n\n'
            f'<code>Short trader</code>\n<code>{OTHER}</code>\n'
            '<b>BTC Short 5 cross</b>\nVolume: $20K\nEntry Price: 100\nP&amp;L: -$2\n\n'
            f'<code>Whale</code>\n<code>{WALLET}</code>\n'
            '<b>ETH Long 5 cross</b>\nVolume: $100\nEntry Price: 100\nP&amp;L: +$2.5\n\n'
            f'<code>Short trader</code>\n<code>{OTHER}</code>\n'
            '<b>ETH Short 5 cross</b>\nVolume: $200\nEntry Price: 100\nP&amp;L: +$2.5\n'
        ])

    def test_hip3_names_are_exact_and_single_wallet_tickers_are_excluded(self):
        accounts = {
            WALLET: Account(positions=[position(ticker='xyz:SPCX'), position(ticker='BTC')]),
            OTHER: Account(positions=[position(-1, ticker='xyz:SPCX'), position(ticker='SPCX')]),
            THIRD: Account(positions=[position(2, ticker='xyz:SPCX')]),
        }
        text = '\n'.join(intersection_messages(accounts))
        self.assertEqual(text.count('<b>xyz:SPCX '), 3)
        self.assertNotIn('<b>BTC ', text)
        self.assertNotIn('<b>SPCX ', text)
        self.assertTrue(text.startswith(f'<code>{WALLET}</code>\n'))
        self.assertLess(text.index(WALLET), text.index(OTHER))
        self.assertLess(text.index(OTHER), text.index(THIRD))

    def test_counts_distinct_wallets_and_ignores_zero_positions(self):
        accounts = {
            WALLET: Account(positions=[position(), position(-1)]),
            OTHER: Account(positions=[position(0)]),
        }
        text = '\n'.join(intersection_messages(accounts))
        self.assertIn('No tickers have open positions', text)
        self.assertNotIn('<b>', text)
        accounts[OTHER].positions = [position()]
        text = '\n'.join(intersection_messages(accounts))
        self.assertEqual(text.count(f'<code>{WALLET}</code>'), 1)
        self.assertEqual(text.count(f'<code>{OTHER}</code>'), 1)

    def test_empty_and_unavailable_snapshots_are_explained(self):
        self.assertEqual(intersection_messages({}), ['No wallets are being tracked.'])
        for positions, missing in (([], False), (None, True)):
            with self.subTest(positions=positions):
                text = '\n'.join(intersection_messages({WALLET: Account(positions=positions)}))
                self.assertIn('No tickers have open positions', text)
                self.assertEqual('not available yet' in text, missing)
        accounts = {WALLET: Account(positions=[position()]), OTHER: Account(positions=[position()]),
                    THIRD: Account()}
        text = '\n'.join(intersection_messages(accounts))
        self.assertEqual(text.count('<b>BTC '), 2)
        self.assertIn('1 tracked wallet(s)', text)
        self.assertNotIn(THIRD, text)

    def test_escapes_tags_and_retains_existing_position_annotations_without_mutation(self):
        btc = position(leverage=10, max_leverage=10, delta=-2060, entry_price=None)
        account = Account('<b>Whale & friends</b>', positions=[btc])
        account.snapshot[btc.key] = PositionSnapshotValue(2, 2160, 5)
        accounts = {WALLET: account, OTHER: Account(positions=[position(is_new=True, max_leverage=5)])}
        before = copy.deepcopy(accounts)
        text = '\n'.join(intersection_messages(accounts))
        self.assertIn('<code>&lt;b&gt;Whale &amp; friends&lt;/b&gt;</code>', text)
        self.assertIn('5× → 10× cross', text)
        self.assertIn('Volume: $100 👑 (-$2.06K) 👈', text)
        self.assertIn('Volume: $100 🆕👑', text)
        self.assertIn('Entry Price: N/A', text)
        self.assertEqual(accounts, before)
        self.assertEqual(accounts[WALLET].positions[0].delta, before[WALLET].positions[0].delta)

    def test_large_report_keeps_every_wallet_position_and_splits_complete_blocks(self):
        accounts = {f'0x{index:040x}': Account(f'Whale {index}', positions=[position()])
                    for index in range(60)}
        messages = intersection_messages(accounts)
        self.assertGreater(len(messages), 1)
        text = '\n'.join(messages)
        self.assertEqual(text.count('<b>BTC '), 60)
        for wallet in accounts:
            self.assertEqual(text.count(f'<code>{wallet}</code>'), 1)
        for message in messages:
            self.assertLessEqual(len(message), 1500)
            self.assertEqual(message.count('<b>'), message.count('</b>'))
            self.assertEqual(message.count('<code>'), message.count('</code>'))
            for block in message.strip().split('\n\n'):
                self.assertEqual(len(block.splitlines()), 6)


class IntersectionCommandTests(ApplicationFixture):
    def setUp(self):
        super().setUp()
        self.app.accounts = {WALLET: Account('First', positions=[position()]),
                             OTHER: Account('Second', positions=[position(-1)])}

    def test_command_replies_only_to_requester_without_network_or_credentials(self):
        before = copy.deepcopy(self.app.accounts)
        for command in ('/intersections', '/INTERSECTIONS@wallet_bot'):
            self.app.handle_message(self.message(command, chat=2))
            job = self.jobs()[-1]
            self.assertEqual(job['chat'], 2)
            self.assertTrue(job['html'])
            self.assertEqual(job['text'].count('<b>BTC '), 2)
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(self.app.accounts, before)
        self.client.fetch_open_positions.assert_not_called()
        self.client.fetch_last_trade.assert_not_called()
        self.client.fetch_portfolio.assert_not_called()
        self.assertTrue(self.app.personal_queue.empty())
        self.assertFalse(self.app.autoclose_store.active())

    def test_wallet_order_follows_file_and_removed_wallet_is_excluded(self):
        self.config.wallets_file.write_text(f'{OTHER};\n{WALLET};\n{THIRD};\n')
        self.app.handle_message(self.message('/intersections'))
        text = self.jobs()[-1]['text']
        self.assertLess(text.index(OTHER), text.index(WALLET))
        self.assertNotIn(THIRD, text)
        del self.app.accounts[OTHER]
        self.app.handle_message(self.message('/intersections'))
        self.assertIn('No tickers have open positions', self.jobs()[-1]['text'])

    def test_only_authorized_chats_and_no_arguments(self):
        self.app.handle_message(self.message('/intersections', chat=999))
        self.assertFalse(self.jobs())
        self.app.handle_message(self.message('/intersections BTC'))
        self.assertEqual(self.jobs()[-1]['text'], 'Usage: /intersections')
        self.assertFalse(self.jobs()[-1]['html'])
        group = self.message('/intersections', chat=2, user=999)
        group.chat.type = 'group'
        self.app.handle_message(group)
        self.assertEqual(self.jobs()[-1]['text'].count('<b>BTC '), 2)
