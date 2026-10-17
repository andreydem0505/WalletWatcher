"""Current Hyperliquid contracts, market separation and IP weight limits."""
from datetime import datetime, timezone
from email.utils import format_datetime
import unittest
from unittest.mock import Mock

import requests

from config import ConfigError, parse_perp_dexs
from data_fetcher import DataFetchError, HyperliquidClient
from format import format_position, on_change_message
from models import Account, Trade


def state(coin='BTC', size='0.62274', entry='85062.3'):
    # Fields from the live public HLP response verified on 2026-10-04.
    return {'assetPositions': [{'type': 'oneWay', 'position': {
        'coin': coin, 'szi': size, 'entryPx': entry,
        'leverage': {'type': 'cross', 'value': 20},
        'positionValue': '53119.722', 'unrealizedPnl': '148.022489',
        'liquidationPx': None, 'marginUsed': '2655.9861', 'maxLeverage': 50,
    }}]}


def fill(coin, timestamp, direction='Open Long'):
    return dict(coin=coin, time=timestamp, dir=direction, px='100', sz='1',
                fee='0.01', feeToken='USDC', closedPnl='0', crossed=True, tid=123)


def response(data):
    result = Mock(status_code=200)
    result.json.return_value = data
    return result


class MainnetContractTests(unittest.TestCase):
    def client(self, *responses, **kwargs):
        session = Mock()
        session.post.side_effect = list(responses)
        return HyperliquidClient(session, **kwargs), session

    def test_original_and_hip3_positions_use_explicit_dex(self):
        client, session = self.client(response(state()), response(state('xyz:XYZ100')), perp_dexs=('', 'xyz'))
        self.assertEqual([pos.ticker for pos in client.fetch_open_positions('wallet')], ['BTC', 'xyz:XYZ100'])
        self.assertEqual([call.kwargs['json']['dex'] for call in session.post.call_args_list], ['', 'xyz'])

    def test_partial_multi_dex_response_is_rejected(self):
        client, _ = self.client(response(state()), requests.Timeout(), perp_dexs=('', 'xyz'))
        with self.assertRaises(DataFetchError):
            client.fetch_open_positions('wallet')

    def test_nullable_entry_price_renders_as_unknown(self):
        position = format_position(state(entry=None)['assetPositions'][0])
        self.assertIsNone(position.entry_price)
        account = Account(positions=[position], last_trade=Trade('BTC', '100', '1', 'Open Long', 1))
        self.assertIn('Entry Price: N/A', on_change_message('wallet', account))

    def test_zero_position_with_null_entry_is_not_open(self):
        client, _ = self.client(response(state(size='0', entry=None)))
        self.assertEqual(client.fetch_open_positions('wallet'), [])

    def test_spot_and_unmonitored_dex_fills_are_excluded(self):
        client, _ = self.client(response([fill('BTC', 1), fill('@107', 10, 'Buy'),
            fill('PURR/USDC', 11, 'Sell'), fill('xyz:XYZ100', 12), fill('#1', 13, 'Buy')]))
        self.assertEqual(client.fetch_last_trade('wallet').ticker, 'BTC')

    def test_only_changed_asset_fills_are_selected(self):
        client, _ = self.client(response([fill('BTC', 1), fill('ETH', 2), fill('xyz:XYZ100', 3)]), perp_dexs=('', 'xyz'))
        self.assertEqual(client.fetch_last_trade('wallet', coins={'BTC'}).ticker, 'BTC')

    def test_closed_hip3_position_can_find_its_fill(self):
        client, _ = self.client(response([fill('BTC', 5), fill('xyz:XYZ100', 3, 'Close Long')]), perp_dexs=('', 'xyz'))
        self.assertEqual(client.fetch_last_trade('wallet', coins={'xyz:XYZ100'}).action, 'Close Long')

    def test_no_matching_perpetual_fill_returns_none(self):
        client, _ = self.client(response([fill('@107', 1, 'Buy'), fill('ETH', 2)]))
        self.assertIsNone(client.fetch_last_trade('wallet', coins={'BTC'}))

    def test_weight_budget_defers_calls_until_window_expires(self):
        now = [10000.0]
        client, session = self.client(response({'assetPositions': []}), response({'assetPositions': []}),
                                     response({'assetPositions': []}), clock=lambda: now[0], weight_budget=4)
        client.fetch_open_positions('wallet')
        client.fetch_open_positions('wallet')
        with self.assertRaises(DataFetchError):
            client.fetch_open_positions('wallet')
        self.assertEqual(session.post.call_count, 2)
        now[0] += 60
        client.fetch_open_positions('wallet')
        self.assertEqual(session.post.call_count, 3)

    def test_fill_weight_includes_returned_items(self):
        client, _ = self.client(response([fill('BTC', index) for index in range(21)]))
        client.fetch_last_trade('wallet')
        self.assertEqual(client.calls[-1][1], 22)

    def test_retry_after_supports_dates_and_long_waits(self):
        future = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + 120, timezone.utc)
        self.assertGreater(HyperliquidClient.retry_delay(format_datetime(future, usegmt=True)), 115)
        self.assertEqual(HyperliquidClient.retry_delay('7200'), 7200)
        self.assertEqual(HyperliquidClient.retry_delay('nan'), 30)

    def test_dex_configuration_validates_names(self):
        self.assertEqual(parse_perp_dexs('main,xyz,main'), ('', 'xyz'))
        for invalid in ('', 'all,*', 'main,', 'XYZ', 'main,https://example.com'):
            with self.subTest(value=invalid), self.assertRaises(ConfigError):
                parse_perp_dexs(invalid)

    def test_alert_uses_current_hyperdash_route(self):
        wallet = '0x' + 'a' * 40
        account = Account(positions=[], last_trade=Trade('BTC', '1', '1', 'Open Long', 1))
        self.assertTrue(on_change_message(wallet, account).endswith('https://hyperdash.com/address/' + wallet))


if __name__ == '__main__':
    unittest.main()
