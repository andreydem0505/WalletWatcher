"""Characterize known limitations, not guarantees of complete trade tracking.

These passing tests describe existing behavior. Deliberate fixes should update
them together with the documented monitoring limits.
"""
from unittest.mock import Mock

from walletwatcher.hyperliquid.client import HyperliquidClient
from walletwatcher.domain.models import Account
from support import ApplicationFixture, WALLET, position, trade


class KnownMonitoringLimitationsTests(ApplicationFixture):
    def install_api(self, snapshots, fills):
        snapshots = iter(snapshots)
        session = Mock()
        def post(url, *, json, timeout):
            data = {'assetPositions': next(snapshots)} if json['type'] == 'clearinghouseState' else fills
            response = Mock(status_code=200)
            response.json.return_value = data
            return response
        session.post.side_effect = post
        self.app.client = HyperliquidClient(session=session)
        return session

    def api_position(self, size):
        return {'position': dict(coin='BTC', szi=str(size), entryPx='100',
            leverage=dict(type='cross', value=5), positionValue=str(size * 100), unrealizedPnl='0')}

    def fills(self, final_size=1):
        return [dict(coin='BTC', px='100', sz='1', dir='Open Long', time=2000, tid=1),
                dict(coin='BTC', px='100', sz=str(final_size), dir='Close Long', time=3000, tid=2)]

    def test_round_trip_between_polls_is_not_detected(self):
        for snapshot in ([], [self.api_position(1)]):
            with self.subTest(snapshot=snapshot):
                session = self.install_api([snapshot, snapshot], self.fills())
                account = Account()
                self.app.accounts[WALLET] = account
                self.app.poll_wallet(WALLET, account)
                self.app.poll_wallet(WALLET, account)
                self.assertEqual([call.kwargs['json']['type'] for call in session.post.call_args_list],
                                 ['clearinghouseState', 'clearinghouseState'])
                self.assertIsNone(account.last_trade)
                self.assertEqual(self.queue.pending_count(), 0)

    def test_multiple_fills_are_represented_by_latest_fill_only(self):
        self.install_api([[self.api_position(1)], [self.api_position(1.5)]], self.fills(0.5))
        account = Account()
        self.app.accounts[WALLET] = account
        self.app.poll_wallet(WALLET, account)
        self.app.poll_wallet(WALLET, account)
        self.assertEqual(account.last_trade.action, 'Close Long')
        self.assertEqual(account.last_trade.timestamp, 3000)
        self.assertEqual(len(account.last_actions), 1)
        self.assertIn('BTC Close Long', self.jobs()[0]['text'])

    def test_leverage_change_can_reuse_processed_fill(self):
        account = Account(positions=[position()])
        account.update(trade(), [position()])
        self.app.accounts[WALLET] = account
        self.client.fetch_open_positions.return_value = [position()]
        self.app.poll_wallet(WALLET, account)
        self.deliver_pending()
        self.sender.reset_mock()
        self.client.fetch_open_positions.return_value = [position(leverage=10)]
        self.client.fetch_last_trade.return_value = trade()
        self.app.poll_wallet(WALLET, account)
        self.deliver_pending()
        self.assertEqual(account.last_trade.timestamp, 1000)
        self.assertEqual(self.sender.edit_message_text.call_count, 2)
        self.assertIn('5× → 10×', self.sender.edit_message_text.call_args.kwargs['text'])

    def test_tag_change_can_edit_alert_without_new_trade(self):
        account = Account(positions=[])
        account.update(trade(), [position()])
        self.app.accounts[WALLET] = account
        self.client.fetch_open_positions.return_value = [position()]
        self.app.poll_wallet(WALLET, account)
        self.deliver_pending()
        self.app.handle_message(self.message(f'/settag {WALLET} whale'))
        self.deliver_pending()
        self.sender.reset_mock()
        self.app.poll_wallet(WALLET, self.app.accounts[WALLET])
        self.deliver_pending()
        self.client.fetch_last_trade.assert_not_called()
        self.assertEqual(self.sender.edit_message_text.call_count, 2)
        self.assertIn('<code>whale</code>', self.sender.edit_message_text.call_args.kwargs['text'])
