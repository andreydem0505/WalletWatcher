"""User-visible alert contracts, including notional volume and HTML limits."""
from dataclasses import replace
import unittest

from format import format_position, format_trade, on_change_message
from models import Account, Trade
from support import WALLET, position, trade


class AlertFormattingTests(unittest.TestCase):
    def test_complete_alert_matches_user_visible_format(self):
        btc = position(-1.235, leverage=40, entry_price='80979.4',
                       volume=100000, pnl=9.26, max_leverage=40)
        ewz = position(600, ticker='xyz:EWZ', leverage=2, leverage_type='isolated',
                       entry_price='41.6058', volume=25000, pnl=269, max_leverage=10)
        account = Account(tag='шортист SpaceX', positions=[replace(ewz)])
        account.update(Trade('BTC', '80970.0', '0.6175', 'Open Short', 1000), [btc, ewz])
        self.assertEqual(on_change_message(WALLET, account), (
            '❗️ <b>BTC Open Short</b> ❗️\n<code>шортист SpaceX</code>\n'
            'Volume: $50K\nPrice: 80970.0\n\n'
            '<b>Current positions with changes since this alert started:</b>\n'
            '\n<b>BTC Short 40 cross</b>\nVolume: $100K 🆕👑\n'
            'Entry Price: 80979.4\nP&amp;L: +$9.26\n'
            '\n<b>xyz:EWZ Long 2 isolated</b>\nVolume: $25K\n'
            'Entry Price: 41.6058\nP&amp;L: +$269\n'
            '\nhttps://hyperdash.com/address/' + WALLET))

    def test_trade_and_position_volumes_are_not_multiplied_by_leverage(self):
        for leverage in (1, 40):
            with self.subTest(leverage=leverage):
                account = Account(positions=[position(leverage=leverage, volume=100000)],
                                  last_trade=Trade('BTC', '100', '500', 'Open Long', 1000))
                volumes = [line for line in on_change_message(WALLET, account).splitlines()
                           if line.startswith('Volume:')]
                self.assertEqual(volumes, ['Volume: $50K', 'Volume: $100K'])

    def test_long_alert_with_crowns_arrows_and_escaped_text_stays_valid(self):
        initial = [position(ticker=f'COIN{index}<>&', max_leverage=10) for index in range(100)]
        account = Account(tag='😀<>&' * 20, positions=initial)
        account.update(trade(), [replace(pos, leverage=10) for pos in initial])
        text = on_change_message(WALLET, account)
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 4096)
        self.assertIn('5× → 10×', text)
        self.assertIn('👑', text)
        self.assertIn('&lt;&gt;&amp;', text)
        self.assertIn('more positions', text)
        self.assertEqual(text.count('<b>'), text.count('</b>'))
        self.assertEqual(text.count('<code>'), text.count('</code>'))
        self.assertTrue(text.endswith('/' + WALLET))

    def test_negative_pnl_and_short_size_render_positive_notional(self):
        parsed = format_position({'position': dict(coin='BTC', szi='-2', entryPx='100',
            leverage=dict(value=5, type='cross'), positionValue='-200', unrealizedPnl='-12.5')})
        account = Account(positions=[parsed], last_trade=trade())
        text = on_change_message(WALLET, account)
        self.assertIn('BTC Short 5 cross', text)
        self.assertIn('Volume: $200', text)
        self.assertIn('P&amp;L: -$12.5', text)

    def test_invalid_trade_data_and_account_without_trade_are_rejected(self):
        fill = dict(coin='BTC', px='100', sz='1', dir='Open Long', time=1000)
        for changes in (dict(time=-1), dict(coin=''), dict(dir=''), dict(px='nan'), dict(sz='inf')):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                format_trade(fill | changes)
        with self.assertRaises(ValueError):
            on_change_message(WALLET, Account())
