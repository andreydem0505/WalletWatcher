"""Position identity must survive leverage and margin-mode changes."""
from dataclasses import replace
import unittest

from walletwatcher.presentation.formatting import on_change_message
from walletwatcher.domain.models import Account, Position, Trade


class PositionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.position = Position('BTC', 'Long', 5, 'cross', 1, '100', 100, 2.5)
        self.trade = Trade('BTC', '100', '1', 'Open Long', 1000)

    def test_leverage_and_margin_changes_keep_existing_position(self):
        for changes in ({'leverage': 10}, {'leverage_type': 'isolated'},
                        {'leverage': 10, 'leverage_type': 'isolated'}):
            with self.subTest(changes=changes):
                account = Account(positions=[self.position])
                changed = replace(self.position, **changes)
                # Changes still trigger monitoring, but do not change identity.
                self.assertNotEqual(changed, self.position)
                for timestamp in (1000, 2000):
                    account.update(replace(self.trade, timestamp=timestamp), [replace(changed)])
                    self.assertEqual(account.closed_positions, [])
                    self.assertFalse(account.positions[0].is_new)
                    self.assertEqual(account.positions[0].delta, 0)
                    text = on_change_message('0x' + 'a' * 40, account)
                    self.assertNotIn('Position Closed', text)
                    self.assertNotIn('🆕', text)
                    leverage = '5× → 10×' if changed.leverage != self.position.leverage else '5'
                    self.assertIn(f'BTC Long {leverage} {changed.leverage_type}', text)

    def test_leverage_change_uses_current_alert_baseline(self):
        account = Account(positions=[self.position])
        account.update(self.trade, [replace(self.position, leverage=10)])
        generation = account.generation
        account.update(replace(self.trade, timestamp=2000), [replace(self.position, leverage=20)])
        self.assertEqual(account.generation, generation)
        text = on_change_message('0x' + 'a' * 40, account)
        self.assertIn('BTC Long 5× → 20× cross', text)
        self.assertNotIn('10× → 20×', text)

        account.update(replace(self.trade, timestamp=3000), [replace(self.position)])
        text = on_change_message('0x' + 'a' * 40, account)
        self.assertIn('BTC Long 5 cross', text)
        self.assertNotIn('→', text)

    def test_new_alert_resets_leverage_baseline(self):
        account = Account(positions=[self.position])
        account.update(self.trade, [replace(self.position, leverage=10)])
        generation = account.generation
        account.update(replace(self.trade, timestamp=3601001),
                       [replace(self.position, leverage=20)])
        self.assertNotEqual(account.generation, generation)
        text = on_change_message('0x' + 'a' * 40, account)
        self.assertIn('BTC Long 10× → 20× cross', text)

    def test_new_position_has_no_previous_leverage(self):
        account = Account(positions=[])
        account.update(self.trade, [replace(self.position)])
        text = on_change_message('0x' + 'a' * 40, account)
        self.assertIn('BTC Long 5 cross', text)
        self.assertIn('🆕', text)
        self.assertNotIn('→', text)

    def test_size_delta_is_preserved_when_leverage_changes(self):
        account = Account(positions=[self.position])
        account.update(self.trade, [replace(self.position, leverage=10, size=2, volume=200)])
        self.assertFalse(account.positions[0].is_new)
        self.assertEqual(account.positions[0].delta, 100)
        self.assertEqual(account.closed_positions, [])

    def test_position_can_close_after_leverage_change(self):
        account = Account(positions=[self.position])
        account.update(self.trade, [replace(self.position, leverage=10)])
        account.update(replace(self.trade, timestamp=2000, action='Close Long'), [])
        self.assertEqual(account.closed_positions, ['BTC'])

    def test_direction_change_still_closes_previous_position(self):
        account = Account(positions=[self.position])
        account.update(self.trade, [replace(self.position, direction='Short', size=-1)])
        self.assertTrue(account.positions[0].is_new)
        self.assertEqual(account.closed_positions, ['BTC'])


if __name__ == '__main__':
    unittest.main()
