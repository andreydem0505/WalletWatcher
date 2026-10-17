"""Observable monitoring behavior, from fetched snapshots to Telegram calls."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from data_fetcher import DataFetchError
from models import Account
from support import ApplicationFixture, WALLET, position, trade


class MonitoringTests(ApplicationFixture):
    def track(self, positions=None):
        account = Account(positions=positions)
        self.app.accounts[WALLET] = account
        return account

    def poll(self, account, positions, latest=None):
        self.client.fetch_open_positions.return_value = positions
        self.client.fetch_last_trade.return_value = latest
        self.app.poll_wallet(WALLET, account)

    def test_first_snapshot_establishes_baseline_without_alert(self):
        account = self.track()
        self.poll(account, [position()])
        self.assertEqual(account.positions, [position()])
        self.assertIsNotNone(account.last_updated)
        self.client.fetch_last_trade.assert_not_called()
        self.assertEqual(self.queue.pending_count(), 0)

    def test_opening_position_sends_one_alert_to_each_recipient(self):
        account = self.track([])
        self.poll(account, [position()], trade())
        self.client.fetch_last_trade.assert_called_once_with(WALLET, coins={'BTC'})
        self.deliver_pending()
        self.assertEqual(self.sender.send_message.call_count, 2)
        self.assertEqual({call.kwargs['chat_id'] for call in self.sender.send_message.call_args_list}, {1, 2})
        self.assertIn('BTC Open Long', self.sender.send_message.call_args.kwargs['text'])
        self.sender.edit_message_text.assert_not_called()

    def test_repeat_action_edits_existing_alert_and_idle_polls_do_nothing(self):
        account = self.track([])
        self.poll(account, [position()], trade())
        generation = account.generation
        self.deliver_pending()
        self.poll(account, [position(2)], trade(2000))
        self.assertEqual(account.generation, generation)
        self.deliver_pending()
        self.assertEqual(self.sender.send_message.call_count, 2)
        self.assertEqual(self.sender.edit_message_text.call_count, 2)
        self.client.fetch_last_trade.reset_mock()
        for _ in range(3):
            self.poll(account, [position(2)])
            self.clock.advance()
            self.assertFalse(self.queue.deliver_once())
        self.client.fetch_last_trade.assert_not_called()
        self.assertEqual(self.sender.edit_message_text.call_count, 2)

    def test_different_action_sends_new_alert_with_closed_position(self):
        account = self.track([])
        self.poll(account, [position()], trade())
        generation = account.generation
        self.deliver_pending()
        self.poll(account, [], trade(2000, action='Close Long'))
        self.assertNotEqual(account.generation, generation)
        self.deliver_pending()
        self.assertEqual(self.sender.send_message.call_count, 4)
        self.assertIn('BTC Position Closed', self.sender.send_message.call_args.kwargs['text'])
        self.sender.edit_message_text.assert_not_called()

    def test_price_and_pnl_only_do_not_refresh_or_notify(self):
        account = self.track([])
        self.poll(account, [position()], trade())
        self.deliver_pending()
        self.sender.reset_mock()
        self.client.fetch_last_trade.reset_mock()
        self.poll(account, [position(volume=200, pnl=102.5)])
        self.assertEqual(account.positions[0].volume, 100)
        self.assertEqual(account.positions[0].pnl, 2.5)
        self.client.fetch_last_trade.assert_not_called()
        self.assertFalse(self.queue.deliver_once())
        self.sender.send_message.assert_not_called()
        self.sender.edit_message_text.assert_not_called()

    def test_change_in_one_position_refreshes_all_other_positions(self):
        for change in (dict(size=2, volume=200), dict(leverage=10),
                       dict(leverage_type='isolated'), dict(entry_price='110'),
                       dict(direction='Short', size=-1)):
            with self.subTest(change=change):
                account = self.track([position(), position(ticker='ETH')])
                fresh_eth = position(ticker='ETH', volume=175, pnl=77)
                self.poll(account, [replace(position(), **change), fresh_eth], trade())
                self.client.fetch_last_trade.assert_called_with(WALLET, coins={'BTC'})
                eth = next(pos for pos in account.positions if pos.ticker == 'ETH')
                self.assertEqual((eth.volume, eth.pnl, eth.entry_price), (175, 77, '100'))
                self.assertIn('Entry Price: 100\nP&amp;L: +$77', self.jobs()[0]['text'])

    def test_entry_price_change_is_detected_and_rendered(self):
        account = self.track([position(), position(ticker='ETH')])
        latest = trade(ticker='ETH')
        self.poll(account, [position(pnl=50), position(ticker='ETH', entry_price='120')], latest)
        self.client.fetch_last_trade.assert_called_once_with(WALLET, coins={'ETH'})
        self.assertIn('Entry Price: 120', self.jobs()[0]['text'])
        self.assertEqual(next(pos for pos in account.positions if pos.ticker == 'BTC').pnl, 50)

    def test_missing_fill_preserves_baseline_until_retry_succeeds(self):
        account = self.track([position()])
        with self.assertRaises(DataFetchError):
            self.poll(account, [position(2)])
        self.assertEqual(account.positions, [position()])
        self.assertIsNone(account.last_updated)
        self.assertEqual(self.queue.pending_count(), 0)
        self.poll(account, [position(2)], trade())
        self.assertEqual(account.positions[0].size, 2)
        self.assertEqual(self.queue.pending_count(), 2)

    def test_older_fill_does_not_overwrite_processed_state(self):
        account = self.track([])
        self.poll(account, [position()], trade(2000))
        self.deliver_pending()
        with self.assertRaises(DataFetchError):
            self.poll(account, [position(2)], trade(1000))
        self.assertEqual(account.last_trade.timestamp, 2000)
        self.assertEqual(account.positions[0].size, 1)
        self.assertEqual(self.queue.pending_count(), 0)

    def test_removal_during_fill_request_cancels_alert(self):
        account = self.track([])
        self.client.fetch_open_positions.return_value = [position()]
        def remove(*args, **kwargs):
            del self.app.accounts[WALLET]
            return trade()
        self.client.fetch_last_trade.side_effect = remove
        self.app.poll_wallet(WALLET, account)
        self.assertNotIn(WALLET, self.app.accounts)
        self.assertEqual(self.queue.pending_count(), 0)

    def test_success_resets_failure_count_and_sets_next_poll(self):
        account = self.track([])
        account.failures = 5
        with patch('bot.time.time', return_value=1000), patch('bot.time.monotonic', return_value=200):
            self.poll(account, [])
        self.assertEqual(account.failures, 0)
        self.assertEqual(account.last_updated, 1000)
        self.assertEqual(account.next_poll, 205)


if __name__ == '__main__':
    unittest.main()
