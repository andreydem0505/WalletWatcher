import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests
import telebot
from telebot import apihelper

from bot import Application
from config import Config, ConfigError
from data_fetcher import DataFetchError, HyperliquidClient
from delivery import DeliveryQueue, OutboxFull
from format import format_number, format_position, on_change_message
from models import Account, Position, Trade
from runtime import InstanceLock, RedactingFormatter, WorkerSupervisor
from serialization import load_accounts, save_wallets
from supervisor import supervise

WALLET = '0x' + 'a' * 40
OTHER = '0x' + 'b' * 40


def setUpModule():
    # The suite intentionally generates faults; keep their production logging
    # from obscuring the actual test result.
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def position(size=1, ticker='BTC'):
    return Position(ticker, 'Long', 5, 'cross', size, '100', size * 100, 2.5)


def trade(timestamp=1000, action='Open Long'):
    return Trade('BTC', '100', '1', action, timestamp)


def api_error(code, description='', **parameters):
    return apihelper.ApiTelegramException('test', None, {
        'error_code': code, 'description': description, 'parameters': parameters})


class Clock:
    def __init__(self):
        self.now = 10000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.sqlite3'
        self.clock = Clock()
        self.bot = Mock()
        self.bot.send_message.return_value = SimpleNamespace(message_id=7)
        self.queue = self.make_queue()
        self.addCleanup(lambda: self.queue.close())

    def make_queue(self):
        return DeliveryQueue(self.bot, self.path, clock=self.clock)

    def put(self, text='first', generation='one', chat=1):
        self.queue.enqueue(chat, text, key=f'wallet:{WALLET}:{chat}', generation=generation, html=True)

    def test_coalescing_and_edit_cooldown_without_starvation(self):
        self.put()
        self.assertTrue(self.queue.deliver_once())
        for index in range(14):
            self.clock.advance(1)
            self.put(f'update {index}')
            self.assertFalse(self.queue.deliver_once())
        self.clock.advance(1)
        self.assertTrue(self.queue.deliver_once())
        self.bot.edit_message_text.assert_called_once()
        self.assertEqual(self.bot.edit_message_text.call_args.kwargs['text'], 'update 13')
        self.put('update 13')
        self.assertEqual(self.queue.pending_count(), 0)

    def test_new_alert_sends_new_message(self):
        self.put()
        self.queue.deliver_once()
        self.clock.advance(4)
        self.put('new action', generation='two')
        self.queue.deliver_once()
        self.assertEqual(self.bot.send_message.call_count, 2)
        self.bot.edit_message_text.assert_not_called()

    def test_429_blocks_all_outbound_requests_and_survives_restart(self):
        self.bot.send_message.side_effect = [api_error(429, retry_after=20),
                                             SimpleNamespace(message_id=8), SimpleNamespace(message_id=9)]
        self.put()
        self.queue.deliver_once()
        self.put('latest')
        self.queue.enqueue(2, 'command reply')
        self.queue.close()
        self.queue = self.make_queue()
        self.clock.advance(20)
        self.assertFalse(self.queue.deliver_once())
        self.clock.advance(1)
        self.assertTrue(self.queue.deliver_once())
        self.clock.advance(0.2)
        self.assertTrue(self.queue.deliver_once())
        self.assertEqual(self.queue.pending_count(), 0)
        self.assertIn('latest', [call.kwargs['text'] for call in self.bot.send_message.call_args_list])

    def test_unchanged_message_error_is_success(self):
        self.put()
        self.queue.deliver_once()
        self.clock.advance(15)
        self.bot.edit_message_text.side_effect = api_error(400, 'Bad Request: message is not modified')
        self.put('changed')
        self.queue.deliver_once()
        self.assertEqual(self.queue.pending_count(), 0)
        self.put('changed')
        self.assertEqual(self.queue.pending_count(), 0)

    def test_deleted_message_gets_replacement(self):
        self.put()
        self.queue.deliver_once()
        self.clock.advance(15)
        self.bot.edit_message_text.side_effect = api_error(400, 'Bad Request: message to edit not found')
        self.put('changed')
        self.queue.deliver_once()
        self.assertEqual(self.queue.pending_count(), 1)
        self.clock.advance(4)
        self.queue.deliver_once()
        self.assertEqual(self.bot.send_message.call_count, 2)
        self.assertEqual(self.queue.pending_count(), 0)

    def test_network_failure_retries_without_another_position_change(self):
        self.bot.send_message.side_effect = [requests.Timeout(), SimpleNamespace(message_id=9)]
        self.put()
        self.queue.deliver_once()
        self.clock.advance(4)
        self.queue.deliver_once()
        self.assertEqual(self.queue.pending_count(), 0)

    def test_one_blocked_chat_does_not_block_other_recipients(self):
        self.bot.send_message.side_effect = [api_error(403, 'Forbidden'), SimpleNamespace(message_id=9)]
        self.put(chat=1)
        self.put(chat=2)
        self.queue.deliver_once()
        self.clock.advance(0.2)
        self.queue.deliver_once()
        self.assertEqual(self.bot.send_message.call_args.kwargs['chat_id'], 2)
        self.assertEqual(self.queue.pending_count(), 1)

    def test_command_replies_obey_per_chat_rate_limit(self):
        self.queue.enqueue(1, 'a')
        self.queue.enqueue(1, 'b')
        self.queue.deliver_once()
        self.clock.advance(3)
        self.assertFalse(self.queue.deliver_once())
        self.clock.advance(0.2)
        self.assertTrue(self.queue.deliver_once())

    def test_pending_update_is_not_lost_during_inflight_send(self):
        self.put()

        def update_during_send(**kwargs):
            self.put('newer')
            return SimpleNamespace(message_id=7)

        self.bot.send_message.side_effect = update_during_send
        self.queue.deliver_once()
        self.assertEqual(self.queue.pending_count(), 1)
        self.clock.advance(15)
        self.queue.deliver_once()
        self.assertEqual(self.bot.edit_message_text.call_args.kwargs['text'], 'newer')
        self.assertEqual(self.queue.pending_count(), 0)

    def test_removing_wallet_cancels_pending_delivery(self):
        self.put()
        self.queue.forget_wallet(WALLET)
        self.assertFalse(self.queue.deliver_once())
        self.assertEqual(self.queue.pending_count(), 0)

    def test_readded_wallet_during_inflight_send_keeps_new_generation(self):
        self.put()

        def remove_and_readd(**kwargs):
            self.queue.forget_wallet(WALLET)
            self.put('new account alert', generation='two')
            return SimpleNamespace(message_id=7)

        self.bot.send_message.side_effect = remove_and_readd
        self.queue.deliver_once()
        self.assertEqual(self.queue.pending_count(), 1)
        self.bot.send_message.side_effect = None
        self.clock.advance(4)
        self.queue.deliver_once()
        self.assertEqual(self.bot.send_message.call_count, 2)
        self.assertEqual(self.queue.pending_count(), 0)

    def test_outbox_capacity_keeps_existing_updates_replaceable(self):
        self.put()
        with self.queue.lock, self.queue.db:
            self.queue.db.executemany('INSERT INTO outbox VALUES (?, 2, ?, ?, 0, 1, 0, 0, 0)',
                                     [(f'reply:{index}', '', 'text') for index in range(9999)])
        with self.assertRaises(OutboxFull):
            self.queue.enqueue(3, 'overflow')
        self.put('replace existing')
        self.assertEqual(self.queue.pending_count(), 10000)

    def test_receipt_survives_restart_and_prevents_duplicate_send(self):
        self.put()
        self.queue.deliver_once()
        self.queue.close()
        self.queue = self.make_queue()
        self.put()
        self.assertEqual(self.queue.pending_count(), 0)

    def test_changing_bot_identity_clears_old_offsets_and_notifications(self):
        self.queue.bind_bot('111')
        self.queue.set_metadata('telegram_offset', 123)
        self.put()
        self.queue.bind_bot('222')
        self.assertEqual(self.queue.pending_count(), 0)
        self.assertIsNone(self.queue.get_metadata('telegram_offset'))

    def test_test_mode_does_not_deliver_production_backlog(self):
        self.put(chat=1)
        self.put(chat=2)
        self.queue.enqueue(2, 'command reply')
        self.queue.prune([WALLET], [1], [1, 2])
        self.queue.deliver_once()
        self.clock.advance(0.2)
        self.queue.deliver_once()
        self.assertEqual(self.bot.send_message.call_args.kwargs['text'], 'command reply')
        self.assertEqual(self.queue.pending_count(), 0)

    def test_invalid_retry_after_uses_safe_fallback(self):
        self.bot.send_message.side_effect = api_error(429, retry_after='nan')
        self.put()
        self.queue.deliver_once()
        self.assertEqual(float(self.queue.get_metadata('telegram_due')), self.clock() + 31)

    def test_shorter_concurrent_flood_wait_does_not_override_longer_one(self):
        self.queue.defer_global(120)
        self.queue.defer_global(10)
        self.assertEqual(float(self.queue.get_metadata('telegram_due')), self.clock() + 120)
        self.assertEqual(float(self.queue.get_metadata('telegram_flood_until')), self.clock() + 120)

    def test_actual_telebot_request_signatures_without_network(self):
        real_bot = telebot.TeleBot('12345:test', threaded=False)
        self.queue.bot = real_bot
        requests_seen = []

        def sender(method, url, **kwargs):
            requests_seen.append((url, kwargs))
            response = requests.Response()
            response.status_code = 200
            result = [] if url.endswith('getUpdates') else {
                'message_id': 7, 'date': 1, 'chat': {'id': 1, 'type': 'private'}, 'text': 'ok'}
            response._content = json.dumps({'ok': True, 'result': result}).encode()
            return response

        with patch.object(apihelper, 'CUSTOM_REQUEST_SENDER', sender):
            self.put()
            self.queue.deliver_once()
            self.clock.advance(15)
            self.put('updated')
            self.queue.deliver_once()
            self.assertEqual(real_bot.get_updates(timeout=30, long_polling_timeout=20), [])
        self.assertEqual(len(requests_seen), 3)
        self.assertEqual(requests_seen[1][1]['params']['parse_mode'], 'HTML')
        self.assertEqual(requests_seen[1][1]['timeout'], (20, 20))


class DataTests(unittest.TestCase):
    def test_http_timeout_is_bounded_and_wrapped(self):
        session = Mock()
        session.post.side_effect = requests.Timeout()
        client = HyperliquidClient(session)
        with self.assertRaises(DataFetchError):
            client.fetch_open_positions(WALLET)
        self.assertEqual(session.post.call_args.kwargs['timeout'], (5, 15))

    def test_empty_and_out_of_order_fills(self):
        session = Mock()
        session.post.return_value.json.return_value = []
        client = HyperliquidClient(session)
        self.assertIsNone(client.fetch_last_trade(WALLET))
        session.post.return_value.json.return_value = [
            dict(coin='BTC', px='100', sz='1', dir='Open Long', time=value) for value in (2, 5, 3)]
        self.assertEqual(client.fetch_last_trade(WALLET).timestamp, 5)

    def test_invalid_payloads_do_not_become_empty_positions(self):
        for payload in (None, {}, {'assetPositions': None}, {'assetPositions': [{}]}):
            session = Mock()
            session.post.return_value.json.return_value = payload
            with self.subTest(payload=payload), self.assertRaises(DataFetchError):
                HyperliquidClient(session).fetch_open_positions(WALLET)

    def test_rate_limit_blocks_subsequent_wallets(self):
        clock = Clock()
        session = Mock()
        session.post.return_value.status_code = 429
        session.post.return_value.headers = {'Retry-After': '20'}
        session.post.return_value.raise_for_status.side_effect = requests.HTTPError()
        client = HyperliquidClient(session, clock)
        for wallet in (WALLET, OTHER):
            with self.assertRaises(DataFetchError):
                client.fetch_open_positions(wallet)
        self.assertEqual(session.post.call_count, 1)


class ModelAndStorageTests(unittest.TestCase):
    def test_first_change_repeat_and_close(self):
        account = Account(positions=[])
        account.update(trade(), [position()])
        self.assertTrue(account.need_new_message)
        self.assertTrue(account.positions[0].is_new)
        generation = account.generation
        account.update(trade(2000), [position(2)])
        self.assertFalse(account.need_new_message)
        self.assertEqual(generation, account.generation)
        account.update(trade(3000, 'Close Long'), [])
        self.assertEqual(account.closed_positions, ['BTC'])
        self.assertNotEqual(generation, account.generation)

    def test_safe_html_and_long_position_list(self):
        account = Account(tag='<>&', positions=[])
        account.update(trade(), [position(ticker=f'COIN{index}') for index in range(100)])
        text = on_change_message(WALLET, account)
        self.assertIn('&lt;&gt;&amp;', text)
        self.assertIn('more positions', text)
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 4096)
        self.assertEqual(text.count('<b>'), text.count('</b>'))

    def test_non_finite_values_rejected(self):
        for value in (float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                format_number(value)

    def test_missing_blank_and_malformed_wallet_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wallets.txt'
            self.assertEqual(load_accounts(path), {})
            path.write_text(f'\ninvalid\n# comment\n{WALLET.upper()};tag\n{OTHER}\n')
            accounts = load_accounts(path)
            self.assertEqual(set(accounts), {WALLET, OTHER})
            self.assertEqual(accounts[WALLET].tag, 'tag')

    def test_failed_atomic_save_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wallets.txt'
            save_wallets({WALLET: Account('old')}, path)
            with patch('serialization.os.replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    save_wallets({OTHER: Account('new')}, path)
            self.assertEqual(load_accounts(path)[WALLET].tag, 'old')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_non_trade_price_changes_do_not_trigger_alerts(self):
        first = position()
        second = position()
        second.volume = 300
        second.pnl = -55
        self.assertEqual(first, second)


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.config = Config('12345:test', (1, 2), 1, 'PROD', wallets_file=root / 'wallets.txt',
                             database_file=root / 'state.sqlite3')
        self.bot = Mock()
        self.bot.send_message.return_value = SimpleNamespace(message_id=1)
        self.client = Mock()
        self.app = Application(self.config, self.bot, self.client)
        self.addCleanup(self.app.outbox.close)

    def message(self, text, chat=1):
        return SimpleNamespace(chat=SimpleNamespace(id=chat), text=text)

    def test_commands_validate_and_persist_immediately(self):
        self.app.handle_message(self.message('/addwallet'))
        self.assertFalse(self.app.accounts)
        self.app.handle_message(self.message(f'/addwallet {WALLET}'))
        self.assertIn(WALLET, load_accounts(self.config.wallets_file))
        self.app.handle_message(self.message(f'/settag {WALLET} whale <>&'))
        self.assertEqual(load_accounts(self.config.wallets_file)[WALLET].tag, 'whale <>&')
        self.app.handle_message(self.message(f'/removewallet {WALLET}'))
        self.assertFalse(load_accounts(self.config.wallets_file))

    def test_non_text_and_unauthorized_messages_are_ignored(self):
        self.app.handle_message(self.message(None))
        self.app.handle_message(self.message(f'/addwallet {WALLET}', 99))
        self.assertFalse(self.app.accounts)
        self.assertEqual(self.app.outbox.pending_count(), 0)

    def test_wallet_list_formats_only_addresses_as_copyable_code(self):
        self.app.accounts = {WALLET: Account(tag='whale <>&')}
        self.app.handle_message(self.message('status'))
        self.app.outbox.deliver_once()
        request = self.bot.send_message.call_args.kwargs
        self.assertEqual(request['parse_mode'], 'HTML')
        self.assertIn(f'<code>{WALLET}</code>\nwhale &lt;&gt;&amp;\nLast success: not yet; failures: 0',
                      request['text'])

    def test_long_wallet_list_preserves_complete_copyable_addresses(self):
        wallets = [f'0x{index:040x}' for index in range(50)]
        self.app.accounts = {wallet: Account(tag='<>&' * 30) for wallet in wallets}
        self.app.handle_message(self.message('status'))
        jobs = self.app.outbox.db.execute('SELECT text, html FROM outbox ORDER BY rowid').fetchall()
        self.assertGreater(len(jobs), 1)
        text = '\n'.join(job['text'] for job in jobs)
        for wallet in wallets:
            self.assertEqual(text.count(f'<code>{wallet}</code>'), 1)
        for job in jobs:
            self.assertTrue(job['html'])
            self.assertLessEqual(len(job['text']), 1500)
            self.assertEqual(job['text'].count('<code>'), job['text'].count('</code>'))
            self.assertNotIn('<>&', job['text'])

    def test_command_write_failure_does_not_change_live_state(self):
        with patch('bot.save_wallets', side_effect=OSError('disk full')):
            self.app.handle_message(self.message(f'/addwallet {WALLET}'))
        self.assertFalse(self.app.accounts)

    def test_workers_shut_down_and_close_resources(self):
        self.bot.get_updates.return_value = []
        timer = threading.Timer(0.1, self.app.stop.set)
        timer.start()
        try:
            self.app.run()
        finally:
            self.app.stop.set()
            timer.join(1)
        self.client.close.assert_called_once()

    def test_first_trade_queues_alert_and_unchanged_positions_retry_enqueue(self):
        account = Account(positions=[])
        self.app.accounts[WALLET] = account
        self.client.fetch_open_positions.return_value = [position()]
        self.client.fetch_last_trade.return_value = trade()
        with patch.object(self.app.outbox, 'enqueue', side_effect=OutboxFull()):
            with self.assertRaises(OutboxFull):
                self.app.poll_wallet(WALLET, account)
        self.app.poll_wallet(WALLET, account)
        self.assertEqual(self.app.outbox.pending_count(), 2)
        self.assertEqual(self.client.fetch_last_trade.call_count, 1)

    def test_wallet_removed_during_request_is_not_reinserted(self):
        account = Account(positions=[])
        self.app.accounts[WALLET] = account

        def remove(wallet):
            del self.app.accounts[wallet]
            return [position()]

        self.client.fetch_open_positions.side_effect = remove
        self.app.poll_wallet(WALLET, account)
        self.assertEqual(self.app.outbox.pending_count(), 0)

    def test_failed_wallet_does_not_prevent_next_wallet_check(self):
        self.app.accounts = {WALLET: Account(), OTHER: Account()}

        def fetch(wallet):
            if wallet == WALLET:
                raise requests.Timeout()
            self.app.stop.set()
            return []

        self.client.fetch_open_positions.side_effect = fetch
        self.app.monitor(lambda: None)
        self.assertEqual(self.app.accounts[WALLET].failures, 1)
        self.assertIsNotNone(self.app.accounts[OTHER].last_updated)

    def test_polling_recovers_and_persists_update_offset(self):
        update = SimpleNamespace(update_id=123, message=self.message('status'))

        def stop_polling(**kwargs):
            self.app.stop.set()
            return []

        outcomes = iter([requests.Timeout(), [update], 'stop'])

        def fetch(**kwargs):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome == 'stop':
                self.app.stop.set()
                return []
            return outcome

        self.bot.get_updates.side_effect = fetch
        with patch('bot.time.monotonic', side_effect=lambda: time.perf_counter() * 10000):
            self.app.polling(lambda: None)
        self.assertEqual(self.app.outbox.get_metadata('telegram_offset'), '124')


class SupervisionTests(unittest.TestCase):
    def test_crashed_worker_restarts_and_stalled_worker_is_detected(self):
        stop = threading.Event()
        clock = Clock()
        calls = threading.Event()
        restarted = threading.Event()

        def worker(beat):
            if not calls.is_set():
                calls.set()
                raise RuntimeError('simulated crash')
            beat()
            restarted.set()
            stop.wait()

        supervisor = WorkerSupervisor(stop, timeout=10, clock=clock)
        supervisor.add('test', worker)
        try:
            supervisor.workers['test']['thread'].join(1)
            clock.advance(5)
            supervisor.check()
            self.assertTrue(restarted.wait(1))
            self.assertTrue(supervisor.workers['test']['thread'].is_alive())
            clock.advance(11)
            with self.assertRaises(RuntimeError):
                supervisor.check()
        finally:
            stop.set()
            supervisor.join()

    def test_actual_child_process_is_restarted_after_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'child.py'
            counter = root / 'counter'
            script.write_text(
                'from pathlib import Path\nimport sys\n'
                f'p = Path({str(counter)!r})\n'
                'count = int(p.read_text()) + 1 if p.exists() else 1\n'
                'p.write_text(str(count))\nsys.exit(1 if count == 1 else 0)\n')
            result = supervise([sys.executable, str(script)], threading.Event(), root / 'heartbeat',
                               initial_delay=0.01, maximum_delay=0.02)
            self.assertEqual(result, 0)
            self.assertEqual(counter.read_text(), '2')

    def test_hung_child_is_killed_and_restarted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'child.py'
            counter = root / 'counter'
            script.write_text(
                'from pathlib import Path\nimport time\n'
                f'p = Path({str(counter)!r})\n'
                'count = int(p.read_text()) + 1 if p.exists() else 1\n'
                'p.write_text(str(count))\n'
                'if count == 1: time.sleep(60)\n')
            result = supervise([sys.executable, str(script)], threading.Event(), root / 'heartbeat',
                               watchdog_timeout=0.3, initial_delay=0.01)
            self.assertEqual(result, 0)
            self.assertEqual(counter.read_text(), '2')

    def test_config_error_does_not_enter_restart_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            result = supervise([sys.executable, '-c', 'raise SystemExit(78)'], threading.Event(),
                               Path(directory) / 'heartbeat')
            self.assertEqual(result, 78)

    def test_second_local_instance_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'instance.lock'
            with InstanceLock(path):
                with self.assertRaises(RuntimeError):
                    with InstanceLock(path):
                        pass


class ConfigurationTests(unittest.TestCase):
    def test_token_is_redacted_from_exception_traceback(self):
        try:
            raise requests.Timeout('https://api.telegram.org/bot123456789:secret/getUpdates')
        except requests.Timeout:
            record = logging.LogRecord('test', logging.ERROR, '', 1, 'request failed', (), sys.exc_info())
        formatted = RedactingFormatter().format(record)
        self.assertNotIn('123456789:secret', formatted)
        self.assertIn('<BOT_TOKEN>', formatted)

    def test_invalid_environment_and_intervals(self):
        environment = dict(BOT_TOKEN='12345:test', CHAT_IDS='1,2,1', ADMIN_ID='1', MODE='TEST')
        with patch('config.load_dotenv'), patch.dict(os.environ, environment, clear=True):
            config = Config.from_env()
            self.assertEqual(config.recipients, (1,))
            self.assertEqual(config.chat_ids, (1, 2))
            for name, value in (('EDIT_INTERVAL', '0'), ('POLL_INTERVAL', 'nan'),
                                ('CHAT_IDS', 'abc'), ('MODE', 'typo'), ('BOT_TOKEN', '')):
                with self.subTest(name=name), patch.dict(os.environ, {name: value}):
                    with self.assertRaises(ConfigError):
                        Config.from_env()


if __name__ == '__main__':
    unittest.main()
