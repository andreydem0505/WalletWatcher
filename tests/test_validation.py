"""API and file validation must not silently turn bad data into valid state."""
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from config import Config, ConfigError
from data_fetcher import DataFetchError, HyperliquidClient
from models import Account
from serialization import load_accounts, save_wallets, validate_tag
from support import WALLET, OTHER


class APIValidationTests(unittest.TestCase):
    def test_bad_fill_payloads_are_wrapped_as_fetch_errors(self):
        valid = dict(coin='BTC', px='100', sz='1', dir='Open Long', time=1000)
        payloads = (None, {}, [valid | dict(coin=123)], [valid | dict(time='bad')],
                    [valid | dict(px='nan')], [dict(coin='BTC')])
        for payload in payloads:
            with self.subTest(payload=payload):
                session = Mock()
                session.post.return_value.json.return_value = payload
                with self.assertRaises(DataFetchError):
                    HyperliquidClient(session).fetch_last_trade(WALLET)

    def test_http_and_json_failures_keep_original_cause(self):
        for failure in (requests.HTTPError('server error'), ValueError('bad json')):
            with self.subTest(failure=type(failure).__name__):
                session = Mock()
                if isinstance(failure, requests.HTTPError):
                    session.post.return_value.raise_for_status.side_effect = failure
                else:
                    session.post.return_value.json.side_effect = failure
                client = HyperliquidClient(session)
                heartbeat = Mock()
                client.heartbeat = heartbeat
                with self.assertRaises(DataFetchError) as raised:
                    client.fetch_open_positions(WALLET)
                self.assertIs(raised.exception.__cause__, failure)
                self.assertEqual(heartbeat.call_count, 2)

    def test_retry_after_fallbacks_and_http_dates_use_bounded_positive_delays(self):
        date = format_datetime(datetime.fromtimestamp(1120, timezone.utc), usegmt=True)
        with patch('data_fetcher.time.time', return_value=1000):
            self.assertEqual(HyperliquidClient.retry_delay(date), 120)
            for invalid in ('bad date', None, 'nan', 'inf'):
                self.assertEqual(HyperliquidClient.retry_delay(invalid), 30)
            self.assertEqual(HyperliquidClient.retry_delay('-10'), 1)


class FileAndConfigValidationTests(unittest.TestCase):
    def test_bom_comments_duplicates_and_invalid_tags_load_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wallets.txt'
            path.write_text(f'\ufeff# comment\n{WALLET.upper()};first\n{WALLET};last\n{OTHER};bad;tag\n')
            with self.assertLogs('serialization', level='WARNING'):
                accounts = load_accounts(path)
            self.assertEqual(set(accounts), {WALLET})
            self.assertEqual(accounts[WALLET].tag, 'last')

    def test_file_flush_failure_preserves_previous_wallet_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wallets.txt'
            save_wallets({WALLET: Account('original')}, path)
            with patch('serialization.os.fsync', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    save_wallets({OTHER: Account('new')}, path)
            self.assertEqual(load_accounts(path)[WALLET].tag, 'original')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_tags_reject_file_delimiters_and_line_breaks(self):
        for tag in ('a;b', 'a\nb', 'a\rb', 'x' * 101):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                validate_tag(tag)
        self.assertEqual(validate_tag('  whale  '), 'whale')

    def test_default_prod_mode_deduplicates_recipients_and_rejects_zero_ids(self):
        environment = dict(BOT_TOKEN='12345:test', CHAT_IDS='1,2,1', ADMIN_ID='1')
        with patch('config.load_dotenv'), patch.dict('os.environ', environment, clear=True):
            config = Config.from_env()
            self.assertEqual(config.recipients, (1, 2))
            self.assertEqual(config.mode, 'PROD')
            self.assertNotIn(config.token, repr(config))
            for invalid in (dict(CHAT_IDS='0,1'), dict(CHAT_IDS=''), dict(ADMIN_ID='0')):
                with self.subTest(invalid=invalid), patch.dict('os.environ', invalid):
                    with self.assertRaises(ConfigError):
                        Config.from_env()
