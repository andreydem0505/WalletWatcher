"""Maximum-leverage badges use the limit returned for each position."""
from dataclasses import replace
import unittest

from format import format_position, on_change_message
from models import Account, Trade


class MaxLeverageTests(unittest.TestCase):
    def position(self, coin='BTC', leverage=40, maximum=40, mode='cross'):
        return format_position({'position': {
            'coin': coin, 'szi': '-1', 'entryPx': '100000',
            'leverage': {'type': mode, 'value': leverage},
            'maxLeverage': maximum, 'positionValue': '100000', 'unrealizedPnl': '9.26',
        }})

    def message(self, account):
        return on_change_message('0x' + 'a' * 40, account)

    def trade(self, coin='BTC', timestamp=1000):
        return Trade(coin, '100000', '1', 'Open Short', timestamp)

    def test_new_maximum_leverage_positions_have_adjacent_crown(self):
        for coin, leverage, mode in (('BTC', 40, 'cross'), ('xyz:EWZ', 2, 'isolated')):
            with self.subTest(coin=coin, mode=mode):
                position = self.position(coin, leverage, str(leverage), mode)
                self.assertEqual(position.max_leverage, leverage)
                account = Account(positions=[])
                account.update(self.trade(coin), [position])
                self.assertIn('Volume: $100K 🆕👑\n', self.message(account))

    def test_below_or_unknown_maximum_has_no_crown(self):
        for maximum in (50, None):
            with self.subTest(maximum=maximum):
                account = Account(positions=[])
                account.update(self.trade(), [self.position(maximum=maximum)])
                text = self.message(account)
                self.assertIn('Volume: $100K 🆕\n', text)
                self.assertNotIn('👑', text)

    def test_existing_position_keeps_volume_delta_and_crown(self):
        account = Account(positions=[self.position()])
        account.update(self.trade(), [self.position()])
        self.assertIn('Volume: $100K 👑\n', self.message(account))
        account.update(self.trade(timestamp=2000),
                       [replace(self.position(), size=-2, volume=200000)])
        self.assertIn('Volume: $200K (+$100K) 👈👑\n', self.message(account))

    def test_crown_tracks_current_leverage_and_preserves_arrow(self):
        account = Account(positions=[self.position(leverage=20)])
        account.update(self.trade(), [self.position()])
        text = self.message(account)
        self.assertIn('BTC Short 20× → 40× cross', text)
        self.assertIn('Volume: $100K 👑\n', text)
        self.assertNotIn('Position Closed', text)
        account.update(self.trade(timestamp=2000), [self.position(leverage=10)])
        text = self.message(account)
        self.assertIn('BTC Short 20× → 10× cross', text)
        self.assertNotIn('👑', text)

    def test_each_coin_uses_its_own_maximum(self):
        account = Account(positions=[])
        account.update(self.trade(), [self.position(), self.position('xyz:EWZ', 2, 10, 'isolated')])
        text = self.message(account)
        self.assertEqual(text.count('👑'), 1)
        self.assertIn('BTC Short 40 cross</b>\nVolume: $100K 🆕👑', text)
        self.assertIn('xyz:EWZ Short 2 isolated</b>\nVolume: $100K 🆕\n', text)

    def test_limit_change_alone_does_not_trigger_position_change(self):
        position = self.position()
        self.assertEqual(position, replace(position, max_leverage=50))

    def test_invalid_limits_are_rejected(self):
        for maximum in (0, -1, 'nan', 'inf', 'invalid'):
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                self.position(maximum=maximum)


if __name__ == '__main__':
    unittest.main()
