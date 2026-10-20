"""Personal account commands, encrypted credentials and live-snapshot rendering."""
from dataclasses import replace
import json
import logging
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import InvalidToken
import requests
from telebot import apihelper

from walletwatcher.storage.credentials import CredentialsStore, normalize_private_key
from walletwatcher.hyperliquid.client import DataFetchError, HyperliquidClient
from walletwatcher.storage.delivery import DeliveryQueue
from walletwatcher.presentation.portfolio import Portfolio, PersonalPosition, parse_positions, portfolio_messages
from walletwatcher.infrastructure.runtime import RedactingFormatter
from support import ApplicationFixture, WALLET, OTHER, position

# Public, deterministic fixture keys. Never use these keys for a funded account.
KEY = '0x' + '01' * 32
SECOND_KEY = '0x' + '02' * 32


def state(coin='VVV', size='-3', balance='77.43'):
    return {'marginSummary': {'accountValue': balance}, 'assetPositions': [
        {'position': {'coin': coin, 'szi': size, 'entryPx': '22.1841',
         'leverage': {'type': 'cross', 'value': 3}, 'positionValue': '69.6936',
         'marginUsed': '23.05', 'unrealizedPnl': '-0.16', 'returnOnEquity': '-0.008',
         'cumFunding': {'sinceOpen': '0.23'}}}]}


def order(kind='Take Profit Market', price='20.0', coin='VVV', side='B', **changes):
    return dict(coin=coin, side=side, orderType=kind, triggerPx=price,
                isTrigger=True, reduceOnly=True, **changes)


def contexts(coin='VVV'):
    return [{'universe': [{'name': coin}], 'collateralToken': 0}, [{'markPx': '23.2312'}]]


def response(data):
    result = Mock(status_code=200)
    result.json.return_value = data
    return result


def snapshot():
    positions = parse_positions(state(), {'VVV': '23.2312'},
                                [order(), order('Stop Market', '25.2')])
    return Portfolio(77.43, positions)


class CredentialsTests(ApplicationFixture):
    def test_keys_are_encrypted_and_bound_to_bot_and_user(self):
        self.app.credentials.save(1, WALLET.upper(), KEY)
        self.app.credentials.save(2, OTHER, SECOND_KEY)
        self.assertEqual(self.app.credentials.load(1).address, WALLET)
        self.assertEqual(self.app.credentials.load(1).private_key, KEY)
        self.assertEqual(self.app.credentials.load(2).private_key, SECOND_KEY)
        self.assertIsNone(self.app.credentials.load(99))
        self.assertNotIn(KEY, repr(self.app.credentials.load(1)))
        rows = self.queue.db.execute('SELECT encrypted_key FROM user_credentials').fetchall()
        self.assertTrue(all(KEY.encode() not in row[0] and SECOND_KEY.encode() not in row[0] for row in rows))
        another_bot = CredentialsStore(self.queue.db, self.queue.lock, 'other', self.root / 'credentials.key')
        self.assertIsNone(another_bot.get_address(1))
        self.assertIsNone(another_bot.load(1))

    def test_key_file_is_private_and_credentials_survive_reopening(self):
        self.app.credentials.save(1, WALLET, KEY)
        path = self.root / 'credentials.key'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        stored_key = path.read_bytes()
        reopened = CredentialsStore(self.queue.db, self.queue.lock, '12345', path)
        self.assertEqual(reopened.load(1).private_key, KEY)
        self.assertEqual(path.read_bytes(), stored_key)
        self.assertNotIn(KEY.encode(), self.config.database_file.read_bytes())
        wal = Path(str(self.config.database_file) + '-wal')
        if wal.exists():
            self.assertNotIn(KEY.encode(), wal.read_bytes())

    def test_replacement_updates_only_senders_credentials(self):
        self.app.credentials.save(1, WALLET, KEY)
        self.app.credentials.save(2, OTHER, SECOND_KEY)
        self.app.credentials.save(1, OTHER, SECOND_KEY)
        self.assertEqual(self.app.credentials.get_address(1), OTHER)
        self.assertEqual(self.app.credentials.load(2).private_key, SECOND_KEY)

    def test_invalid_credentials_never_replace_saved_values(self):
        self.app.credentials.save(1, WALLET, KEY)
        for bad in ('bad', '0x' + '0' * 64, '0x' + 'f' * 64, 'g' * 64):
            with self.subTest(key=bad), self.assertRaises(ValueError):
                self.app.credentials.save(1, OTHER, bad)
        for user in (0, -1, True, '1'):
            with self.subTest(user=user), self.assertRaises(ValueError):
                self.app.credentials.save(user, OTHER, KEY)
        with self.assertRaises(ValueError):
            self.app.credentials.save(1, 'not an address', KEY)
        self.assertEqual(self.app.credentials.load(1).address, WALLET)
        self.assertEqual(self.app.credentials.load(1).private_key, KEY)
        self.assertEqual(normalize_private_key(KEY[2:].upper()), KEY)

    def test_missing_key_file_cannot_be_replaced_over_existing_credentials(self):
        self.app.credentials.save(1, WALLET, KEY)
        self.app.credentials.key_file.unlink()
        self.assertEqual(self.app.credentials.get_address(1), WALLET)
        with self.assertRaises(RuntimeError):
            self.app.credentials.load(1)
        with self.assertRaises(RuntimeError):
            self.app.credentials.save(2, OTHER, SECOND_KEY)
        self.assertFalse(self.app.credentials.key_file.exists())

    def test_ciphertext_tampering_fails_authentication(self):
        self.app.credentials.save(1, WALLET, KEY)
        with self.queue.db:
            self.queue.db.execute('UPDATE user_credentials SET encrypted_key=?', (b'invalid ciphertext',))
        with self.assertRaises(InvalidToken):
            self.app.credentials.load(1)

    def test_key_write_failure_does_not_save_credentials_or_partial_file(self):
        with patch('walletwatcher.storage.credentials.os.fsync', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.app.credentials.save(1, WALLET, KEY)
        self.assertIsNone(self.app.credentials.get_address(1))
        self.assertFalse(self.app.credentials.key_file.exists())

    def test_private_keys_are_redacted_from_messages_and_tracebacks(self):
        try:
            raise RuntimeError('failed with ' + KEY)
        except RuntimeError:
            import sys
            record = logging.LogRecord('test', logging.ERROR, '', 1, SECOND_KEY[2:], (), sys.exc_info())
        text = RedactingFormatter().format(record)
        self.assertNotIn(KEY, text)
        self.assertNotIn(SECOND_KEY[2:], text)
        self.assertEqual(text.count('<PRIVATE_KEY>'), 2)


class PersonalCommandTests(ApplicationFixture):
    def personal(self, text, user=1, private=True):
        return SimpleNamespace(text=text, from_user=SimpleNamespace(id=user), message_id=123,
                               chat=SimpleNamespace(id=user if private else 1,
                                                    type='private' if private else 'group'))

    def save(self, user=1, address=WALLET, key=KEY):
        self.app.handle_message(self.personal(f'/creds {address} {key}', user))

    def test_creds_save_reply_is_private_and_never_echoes_key(self):
        self.save()
        self.assertEqual(self.app.credentials.load(1).private_key, KEY)
        self.sender.delete_message.assert_called_once_with(1, 123, timeout=10)
        self.assertEqual([job['chat'] for job in self.jobs()], [1])
        self.assertNotIn(KEY, self.jobs()[0]['text'])
        self.assertIn('сохранены', self.jobs()[0]['text'])
        self.assertFalse(self.app.accounts)

    def test_my_without_credentials_explains_registration(self):
        self.app.handle_message(self.personal('/my'))
        self.assertIn('ещё не сохранены', self.jobs()[0]['text'])
        self.client.fetch_portfolio.assert_not_called()

    def test_personal_credentials_survive_application_restart(self):
        from walletwatcher.app import Application
        self.save()
        self.deliver_pending()
        self.queue.close()
        self.queue = DeliveryQueue(self.sender, self.config.database_file, clock=self.clock)
        self.addCleanup(self.queue.close)
        session = Mock()
        answers = {'userAbstraction': 'disabled', 'metaAndAssetCtxs': contexts(),
                   'frontendOpenOrders': [order(), order('Stop Market', '25.2')]}
        session.post.side_effect = lambda *args, **kwargs: response(answers[kwargs['json']['type']])
        client = HyperliquidClient(session, personal_session_factory=lambda: session)
        reopened = Application(self.config, bot=self.sender, client=client, outbox=self.queue)
        self.assertEqual(reopened.credentials.load(1).private_key, KEY)
        connection = Mock()
        connection.recv.return_value = json.dumps({'channel': 'allDexsClearinghouseState', 'data': {
            'user': WALLET, 'clearinghouseStates': [['', state()]]}})
        with patch('walletwatcher.hyperliquid.client.websocket.WebSocket', return_value=connection):
            reopened.handle_message(self.personal('/my'))
            self.assertTrue(reopened.process_personal_request())
        connection.close.assert_called_once_with(timeout=1)
        self.assertIn('Balance: $77.43', self.jobs()[0]['text'])
        self.deliver_pending()
        request = self.sender.send_message.call_args.kwargs
        self.assertEqual(request['chat_id'], 1)
        self.assertEqual(request['parse_mode'], 'HTML')
        self.assertIn('Cur Price: 23.2312', request['text'])
        self.assertIn('🟢 TP: 20.0\n🔴 SL: 25.2', request['text'])
        self.assertNotIn(KEY, request['text'])
        self.assertTrue(all('private' not in str(call.kwargs['json']) for call in session.post.call_args_list))

    def test_my_reads_only_senders_address_and_keeps_reply_private_in_test_mode(self):
        self.save(1, WALLET, KEY)
        self.save(2, OTHER, SECOND_KEY)
        self.deliver_pending()
        self.app.config = replace(self.config, mode='TEST')
        self.client.fetch_portfolio.return_value = snapshot()
        with patch.object(self.app.credentials, 'load', side_effect=AssertionError('must not decrypt')):
            self.app.handle_message(self.personal('/my@wallet_bot', user=2))
            self.assertTrue(self.app.process_personal_request())
        self.client.fetch_portfolio.assert_called_once_with(OTHER)
        self.assertEqual([job['chat'] for job in self.jobs()], [2])
        self.assertTrue(self.jobs()[0]['html'])
        self.assertIn('<b>Balance: $77.43</b>', self.jobs()[0]['text'])
        self.assertNotIn(SECOND_KEY, self.jobs()[0]['text'])

    def test_personal_commands_reject_group_and_missing_sender(self):
        for text in ('/my', f'/creds {WALLET} {KEY}'):
            self.app.handle_message(self.personal(text, user=2, private=False))
        self.assertIsNone(self.app.credentials.get_address(2))
        self.assertTrue(all('личном чате' in job['text'] for job in self.jobs()))
        self.client.fetch_portfolio.assert_not_called()
        message = self.personal('/my')
        message.from_user = None
        self.app.handle_message(message)
        self.assertIn('личном чате', self.jobs()[-1]['text'])

    def test_unauthorized_private_user_cannot_register(self):
        self.app.handle_message(self.personal(f'/creds {WALLET} {KEY}', user=999))
        self.assertIsNone(self.app.credentials.get_address(999))
        self.assertEqual(self.queue.pending_count(), 0)
        self.sender.delete_message.assert_not_called()

    def test_invalid_commands_do_not_save_and_do_not_reveal_secret(self):
        for command in ('/creds', f'/creds bad {KEY}', f'/creds {WALLET} bad', '/my extra'):
            self.app.handle_message(self.personal(command))
        self.assertIsNone(self.app.credentials.get_address(1))
        self.assertTrue(all(KEY not in job['text'] for job in self.jobs()))
        self.client.fetch_portfolio.assert_not_called()

    def test_unavailable_data_returns_retry_message_without_stale_snapshot(self):
        self.save()
        self.deliver_pending()
        self.client.fetch_portfolio.side_effect = DataFetchError('unavailable')
        with self.assertLogs('walletwatcher.app', level='WARNING'):
            self.app.handle_message(self.personal('/my'))
            self.assertTrue(self.app.process_personal_request())
        self.assertIn('свежие данные', self.jobs()[0]['text'])
        self.assertNotIn('Balance:', self.jobs()[0]['text'])

    def test_storage_failure_does_not_log_or_echo_private_key(self):
        with patch.object(self.app.credentials, 'save', side_effect=RuntimeError(KEY)), \
                self.assertLogs('walletwatcher.app', level='ERROR') as logs:
            self.save()
        self.assertNotIn(KEY, '\n'.join(logs.output))
        self.assertNotIn(KEY, self.jobs()[0]['text'])
        self.assertNotIn('сохранены', self.jobs()[0]['text'])

    def test_validation_exception_cannot_echo_private_key(self):
        with patch.object(self.app.credentials, 'save', side_effect=ValueError(KEY)):
            self.save()
        self.assertNotIn(KEY, self.jobs()[0]['text'])
        self.assertIn('<PRIVATE_KEY>', self.jobs()[0]['text'])

    def test_deletion_failure_preserves_saved_credentials_and_flood_wait(self):
        error = apihelper.ApiTelegramException('delete', None,
            dict(error_code=429, description='flood wait', parameters=dict(retry_after=20)))
        self.sender.delete_message.side_effect = error
        with self.assertLogs('walletwatcher.app', level='WARNING'):
            self.save()
        self.assertEqual(self.app.credentials.load(1).private_key, KEY)
        self.assertEqual(float(self.queue.get_metadata('telegram_flood_until')), self.clock() + 21)
        self.assertIn('удалите его вручную', self.jobs()[0]['text'])

    def test_invalid_deletion_retry_after_uses_default_cooldown(self):
        error = apihelper.ApiTelegramException('delete', None,
            dict(error_code=429, description='flood wait', parameters=dict(retry_after='nan')))
        self.sender.delete_message.side_effect = error
        with self.assertLogs('walletwatcher.app', level='WARNING'):
            self.save()
        self.assertEqual(float(self.queue.get_metadata('telegram_flood_until')), self.clock() + 31)

    def test_legacy_message_without_id_can_still_save_credentials(self):
        message = self.personal(f'/creds {WALLET} {KEY}')
        del message.message_id
        self.app.handle_message(message)
        self.assertEqual(self.app.credentials.load(1).private_key, KEY)
        self.sender.delete_message.assert_not_called()


class PersonalPortfolioTests(unittest.TestCase):
    def client(self, mode, states, *data):
        session = Mock()
        answers = {('userAbstraction', None): mode}
        remaining = iter(data)
        if mode in ('unifiedAccount', 'portfolioMargin'):
            answers['spotClearinghouseState', None] = next(remaining)
        for dex, value in states:
            raw = value.get('assetPositions') or []
            if any(float(item['position']['szi']) != 0 for item in raw):
                for kind in ('metaAndAssetCtxs', 'frontendOpenOrders'):
                    answers[kind, dex] = next(remaining, requests.Timeout())

        def post(*args, **kwargs):
            payload = kwargs['json']
            item = answers[payload['type'], payload.get('dex')]
            if isinstance(item, Exception):
                raise item
            return response(item)

        session.post.side_effect = post
        client = HyperliquidClient(session, perp_dexs=('',), personal_session_factory=lambda: session)
        client._fetch_dex_states = Mock(return_value=states)
        return client, session

    def test_fresh_snapshot_matches_requested_message(self):
        client, session = self.client('disabled', [('', state())], contexts(), [order(), order('Stop Market', '25.2')])
        portfolio = client.fetch_portfolio(WALLET)
        self.assertEqual(portfolio_messages(portfolio), [
            '<b>Balance: $77.43</b>\n\n<b>VVV Short 3 cross</b>\nMargin: $23.05\n'
            'Entry Price: 22.1841\nCur Price: 23.2312\nP&amp;L: -$0.16 (-0.8%)\n'
            'Funding: -$0.23\n🟢 TP: 20.0\n🔴 SL: 25.2'])
        for call in session.post.call_args_list:
            self.assertNotIn(KEY, str(call))
        self.assertCountEqual([call.kwargs['json']['type'] for call in session.post.call_args_list],
                         ['userAbstraction', 'metaAndAssetCtxs', 'frontendOpenOrders'])
        self.assertEqual([call[1] for call in client.calls], [20, 20, 20])
        client._fetch_dex_states.assert_called_once_with(WALLET)

    def test_hip3_markets_are_discovered_independently_of_monitor_configuration(self):
        client, session = self.client('disabled', [('', state()), ('xyz', state('xyz:EWZ', '2', balance='10'))],
            contexts(), [], contexts('EWZ'), [order('Stop Limit', '25.2', 'xyz:EWZ', 'A')])
        portfolio = client.fetch_portfolio(WALLET)
        self.assertEqual(portfolio.balance, 87.43)
        self.assertEqual([item.position.ticker for item in portfolio.positions], ['VVV', 'xyz:EWZ'])
        self.assertEqual(portfolio.positions[1].stop_loss, ['25.2'])
        self.assertCountEqual([call.kwargs['json'].get('dex') for call in session.post.call_args_list],
                         [None, '', '', 'xyz', 'xyz'])

    def test_unified_accounts_use_usdc_balance_once(self):
        for mode in ('unifiedAccount', 'portfolioMargin'):
            client, _ = self.client(mode, [('', {'assetPositions': []}), ('xyz', {'assetPositions': []})],
                {'balances': [dict(coin='USDC', total='77.43'), dict(coin='USDT', total='10')]})
            self.assertEqual(client.fetch_portfolio(WALLET).balance, 77.43)

    def test_empty_account_shows_balance_and_no_positions_without_extra_requests(self):
        client, session = self.client('default', [('', {'marginSummary': {'accountValue': '77.43'}, 'assetPositions': []})])
        portfolio = client.fetch_portfolio(WALLET)
        self.assertEqual(portfolio_messages(portfolio), ['<b>Balance: $77.43</b>\n\nNo open positions.'])
        self.assertEqual(session.post.call_count, 1)

    def test_empty_dex_balance_is_included_without_prices_or_orders(self):
        client, session = self.client('disabled', [('', state()),
            ('empty', {'marginSummary': {'accountValue': '10'}, 'assetPositions': []})], contexts(), [])
        portfolio = client.fetch_portfolio(WALLET)
        self.assertEqual(portfolio.balance, 87.43)
        self.assertCountEqual([call.kwargs['json']['type'] for call in session.post.call_args_list],
                         ['userAbstraction', 'metaAndAssetCtxs', 'frontendOpenOrders'])

    def test_partial_api_failure_never_returns_incomplete_portfolio(self):
        client, _ = self.client('disabled', [('', state()), ('xyz', state('xyz:EWZ'))],
                                contexts(), [], requests.Timeout())
        with self.assertRaises(DataFetchError):
            client.fetch_portfolio(WALLET)

    def test_missing_price_or_malformed_response_is_rejected(self):
        for broken_context in (None, {}, [{'universe': []}, []], contexts('OTHER')):
            with self.subTest(context=broken_context):
                client, _ = self.client('disabled', [('', state())], broken_context, [])
                with self.assertRaises(DataFetchError):
                    client.fetch_portfolio(WALLET)
        client, _ = self.client({'bad': 'mode'}, [('', state())])
        with self.assertRaises(DataFetchError):
            client.fetch_portfolio(WALLET)

    def test_malformed_account_state_is_rejected(self):
        client, _ = self.client('disabled', [('', {'marginSummary': {'accountValue': '77.43'}, 'assetPositions': None})])
        with self.assertRaises(DataFetchError):
            client.fetch_portfolio(WALLET)
        client, _ = self.client('unifiedAccount', [('', {'assetPositions': []})], {'balances': None})
        with self.assertRaises(DataFetchError):
            client.fetch_portfolio(WALLET)

    def test_only_matching_closing_trigger_orders_are_shown(self):
        tp = order()
        orders = [tp, tp, order('Stop Limit', '25.2'),
                  tp | dict(reduceOnly=False), tp | dict(isTrigger=False),
                  tp | dict(side='A'), tp | dict(coin='OTHER'), tp | dict(orderType='Trailing Stop')]
        result = parse_positions(state(), {'VVV': '23.2312'}, orders)[0]
        self.assertEqual(result.take_profit, ['20.0'])
        self.assertEqual(result.stop_loss, ['25.2'])
        text = portfolio_messages(Portfolio(77.43, [result]))[0]
        self.assertEqual(text.count('TP:'), 1)
        self.assertEqual(text.count('SL:'), 1)

    def test_absent_tp_sl_are_omitted_and_missing_roe_uses_margin(self):
        data = state()
        del data['assetPositions'][0]['position']['returnOnEquity']
        result = parse_positions(data, {'VVV': '23.2312'}, [])[0]
        self.assertAlmostEqual(result.roe, -0.16 / 23.05)
        text = portfolio_messages(Portfolio(77.43, [result]))[0]
        self.assertNotIn('TP:', text)
        self.assertNotIn('SL:', text)

    def test_invalid_financial_fields_are_rejected(self):
        for changes in (dict(marginUsed='-1'), dict(leverage=dict(type='cross', value=0))):
            data = state()
            data['assetPositions'][0]['position'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parse_positions(data, {'VVV': '23.2312'}, [])
        with self.assertRaises(ValueError):
            parse_positions(state(), {'VVV': '0'}, [])
        with self.assertRaises(ValueError):
            parse_positions(state(), {'VVV': '23.2312'}, [order(price='0')])
        with self.assertRaises(ValueError):
            parse_positions(state(), {'VVV': '23.2312'}, {})

    def test_zero_size_position_is_excluded(self):
        self.assertEqual(parse_positions(state(size='0'), {}, []), [])

    def test_request_heartbeats_are_bound_to_the_calling_worker(self):
        session = Mock()
        session.post.return_value = response({'assetPositions': []})
        client = HyperliquidClient(session)
        first, second = Mock(), Mock()
        errors = []
        def fetch(callback):
            try:
                client.set_heartbeat(callback)
                client.fetch_open_positions(WALLET)
            except Exception as error:
                errors.append(error)
        workers = [threading.Thread(target=fetch, args=(callback,)) for callback in (first, second)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(2)
        self.assertFalse(errors)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(first.call_count, 2)
        self.assertEqual(second.call_count, 2)
        self.assertEqual(session.post.call_count, 2)

    def test_null_entry_missing_funding_and_zero_margin_are_not_fabricated(self):
        data = state()
        data['assetPositions'][0]['position'].update(entryPx=None, marginUsed='0', returnOnEquity=None, cumFunding={})
        result = parse_positions(data, {'VVV': '23.2312'}, [])[0]
        text = portfolio_messages(Portfolio(77.43, [result]))[0]
        self.assertIn('Entry Price: N/A', text)
        self.assertIn('P&amp;L: -$0.16 (N/A)', text)
        self.assertIn('Funding: N/A', text)

    def test_funding_costs_are_expenses_and_receipts_are_income_for_both_sides(self):
        for size in ('3', '-3'):
            for raw, expected, display in [('0.23', -0.23, '-$0.23'),
                                           ('-0.23', 0.23, '+$0.23'),
                                           ('0', 0, '$0.00'), ('-0', 0, '$0.00')]:
                with self.subTest(size=size, raw=raw):
                    data = state(size=size)
                    data['assetPositions'][0]['position']['cumFunding'] = {
                        'sinceOpen': raw, 'sinceChange': '999', 'allTime': '888'}
                    personal = parse_positions(data, {'VVV': '23.2312'}, [])[0]
                    self.assertEqual(personal.funding, expected)
                    text = portfolio_messages(Portfolio(77.43, [personal]))[0]
                    self.assertIn(f'Funding: {display}\n', text + '\n')
                    self.assertEqual(personal.position.pnl, -0.16)

    def test_personal_profit_and_funding_show_explicit_positive_signs(self):
        for pnl, roe, funding, expected in [
                (7.27, 1.344, 0.01, 'P&amp;L: +$7.27 (+134.4%)\nFunding: +$0.01'),
                (-7.27, -1.344, -0.01, 'P&amp;L: -$7.27 (-134.4%)\nFunding: -$0.01'),
                (0, 0, 0, 'P&amp;L: $0.00 (0.0%)\nFunding: $0.00')]:
            with self.subTest(pnl=pnl):
                personal = PersonalPosition(position(pnl=pnl), 23.05, '100', roe, funding)
                text = portfolio_messages(Portfolio(77.43, [personal]))[0]
                self.assertIn(expected, text)
                self.assertIn('<b>Balance: $77.43</b>', text)
                self.assertIn('Margin: $23.05', text)

    def test_long_personal_portfolio_splits_without_omitting_positions_or_html(self):
        positions = [PersonalPosition(position(ticker=f'COIN{index}<>&'), 23.05, '23.2312', 0.01, -0.23)
                     for index in range(100)]
        messages = portfolio_messages(Portfolio(77.43, positions))
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message.encode('utf-16-le')) // 2, 4096)
            self.assertEqual(message.count('<b>'), message.count('</b>'))
        text = '\n'.join(messages)
        for index in range(100):
            self.assertEqual(text.count(f'COIN{index}&lt;&gt;&amp; Long'), 1)
        self.assertIn('Funding: -$0.23', text)

    def test_hundreds_of_tp_levels_do_not_break_message_limit(self):
        personal = snapshot().positions[0]
        personal.take_profit = [f'{index}.0' for index in range(1000)]
        messages = portfolio_messages(Portfolio(77.43, [personal]))
        self.assertGreater(len(messages), 1)
        self.assertEqual(sum(message.count('TP:') for message in messages), 1000)
        self.assertTrue(all(len(message.encode('utf-16-le')) // 2 <= 4096 for message in messages))
