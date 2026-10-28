"""Behavior at the boundaries extracted during the readability refactor."""
import unittest
from unittest.mock import Mock

import requests

from support import Clock, WALLET, OTHER
from walletwatcher.domain.models import Account
from walletwatcher.hyperliquid.client import HyperliquidClient
from walletwatcher.hyperliquid.errors import DataFetchError
from walletwatcher.hyperliquid.rate_limit import RequestBudget
from walletwatcher.presentation.commands import autoclose_messages, html_messages, monitoring_messages


def session(data=None):
    result = Mock()
    result.post.return_value.status_code = 200
    result.post.return_value.json.return_value = {} if data is None else data
    return result


class SharedBudgetTests(unittest.TestCase):
    def clients(self, limit):
        self.clock = Clock()
        budget = RequestBudget(limit, self.clock)
        clients = [HyperliquidClient(session(), clock=self.clock, budget=budget) for _ in range(2)]
        for client in clients:
            self.addCleanup(client.close)
        return clients

    def test_separate_http_clients_share_one_rolling_limit(self):
        first, second = self.clients(4)
        first._request({'type': 'clearinghouseState'})
        second._request({'type': 'clearinghouseState'})
        with self.assertRaises(DataFetchError) as caught:
            second._request({'type': 'clearinghouseState'})
        self.assertEqual(caught.exception.reason, 'budget')
        self.assertFalse(caught.exception.submitted)
        self.assertEqual(second.session.post.call_count, 1)
        self.clock.advance(60)
        second._request({'type': 'clearinghouseState'})
        self.assertEqual(second.session.post.call_count, 2)

    def test_exchange_cooldown_is_shared_between_http_clients(self):
        first, second = self.clients(1000)
        first.session.post.return_value.status_code = 429
        first.session.post.return_value.headers = {'Retry-After': '12'}
        first.session.post.return_value.raise_for_status.side_effect = requests.HTTPError()
        with self.assertRaises(DataFetchError):
            first._request({'type': 'meta'})
        with self.assertRaises(DataFetchError) as caught:
            second._request({'type': 'clearinghouseState'})
        self.assertEqual(caught.exception.retry_after, 12)
        self.assertEqual(caught.exception.reason, 'rate_limit')
        second.session.post.assert_not_called()
        self.clock.advance(12)
        second._request({'type': 'clearinghouseState'})
        second.session.post.assert_called_once()

    def test_session_cleanup_failure_does_not_discard_a_valid_portfolio(self):
        for error in (OSError('closed'), requests.ConnectionError('closed')):
            with self.subTest(error=type(error).__name__):
                personal = session('disabled')
                personal.close.side_effect = error
                client = HyperliquidClient(session(), personal_session_factory=lambda: personal)
                self.addCleanup(client.close)
                client._fetch_dex_states = Mock(return_value=[('', {
                    'marginSummary': {'accountValue': '77.43'}, 'assetPositions': []})])
                portfolio = client.fetch_portfolio(WALLET)
                self.assertEqual(portfolio.balance, 77.43)
                self.assertEqual(portfolio.positions, [])
                personal.close.assert_called_once()


class CommandPresentationTests(unittest.TestCase):
    def test_status_preserves_exact_text_wallet_order_tags_and_separators(self):
        accounts = {OTHER: Account(tag='Whale & <BTC>'), WALLET: Account()}
        accounts[OTHER].last_updated = 1000.999
        accounts[OTHER].failures = 2
        self.assertEqual(monitoring_messages(accounts, 1000.999, 3, now=1001), [
            'Wallets: 2\n'
            'Last monitoring cycle: 1970-01-01 03:16:40\n'
            'Pending notifications: 3\n\n'
            'Tracked wallets:\n'
            f'<code>{OTHER}</code>\nWhale &amp; &lt;BTC&gt;\n'
            '❗ Last success: 1970-01-01 03:16:40; failures: 2\n\n'
            f'<code>{WALLET}</code>\n❗ Last success: not yet; failures: 0\n'
        ])

    def test_autoclose_list_preserves_order_and_escapes_complete_rules(self):
        rules = [dict(wallet=OTHER, ticker='BTC'), dict(wallet=WALLET, ticker='xyz:SPCX')]
        self.assertEqual(autoclose_messages(rules), [
            f'<code>{OTHER}</code> BTC\n<code>{WALLET}</code> xyz:SPCX'])

    def test_chunking_keeps_sections_intact_at_exact_boundary(self):
        first, second = '<code>first</code>', '<code>second</code>'
        limit = len(first) + len(second) + 1
        self.assertEqual(html_messages([first, second], limit), [first + '\n' + second])
        self.assertEqual(html_messages([first, second], limit - 1), [first, second])

    def test_empty_or_oversized_sections_do_not_emit_empty_messages(self):
        self.assertEqual(html_messages([]), [])
        self.assertEqual(html_messages(['long section'], limit=3), ['long section'])
