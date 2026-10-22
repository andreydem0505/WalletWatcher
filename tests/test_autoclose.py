"""Deterministic exchange model: no real keys, network or funded orders."""
from dataclasses import replace
from decimal import Decimal
import json
import threading
from unittest.mock import Mock, patch
import unittest

from eth_account import Account as EthAccount
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action
import requests

from support import ApplicationFixture, WALLET, OTHER
from walletwatcher.domain.models import Account
from walletwatcher.hyperliquid.client import DataFetchError, HyperliquidClient
from walletwatcher.storage.autoclose import AutocloseStore
from walletwatcher.storage.credentials import Credentials
from walletwatcher.trading.api import Market, TradingAPI, TradingError, decimal, replay, ticker_name, triggered
from walletwatcher.trading.worker import AutocloseWorker

KEY = '0x' + '0' * 63 + '1'  # Public secp256k1 test vector, never a funded wallet.
SIGNER = EthAccount.from_key(KEY).address.lower()


def fill(start='10', size='10', side='A', stamp=10000001, oid=7, tid=1, coin='BTC', px='100'):
    return dict(startPosition=start, sz=size, side=side, time=stamp, oid=oid, tid=tid, coin=coin, px=px)


class Exchange:
    def __init__(self, clock):
        self.clock = clock
        self.source = Decimal(100)
        self.own = Decimal(10)
        self.history = []
        self.orders = []
        self.fraction = Decimal(1)
        self.fail = None
        self.unknown = False
        self.reject = False
        self.market_value = Market('BTC', '', 0, 3)
        self.begin_cycle = Mock()
        self.close = Mock()

    def authority(self, credentials):
        return SIGNER, None

    def market(self, ticker):
        return replace(self.market_value, coin=ticker, dex=ticker.split(':')[0] if ':' in ticker else '')

    def position(self, address, market, fresh=False):
        return (self.source if address == WALLET else self.own), int(self.clock() * 1000)

    def fills(self, address, start, end, fresh=False, anchor=None):
        return [f for f in self.history if start <= f['time'] <= end]

    def mid(self, market):
        return Decimal(100)

    def trade(self, size, side='A', oid=99):
        item = fill(str(self.own), str(size), side, int(self.clock() * 1000), oid,
                    len(self.history) + 1, self.market_value.coin)
        self.history.append(item)
        self.own += decimal(size) * (1 if side == 'B' else -1)

    def send(self, credentials, vault, attempt):
        self.orders.append(json.loads(json.dumps(attempt)))
        if self.reject:
            return {'status': 'ok', 'response': {'data': {'statuses': [{'error': 'IOC no match'}]}}}
        size = abs(self.own) * self.fraction
        self.clock.advance(.001)
        self.trade(size, 'B' if self.own < 0 else 'A', len(self.orders))
        if self.fail:
            raise self.fail
        return {'status': 'ok', 'response': {'data': {'statuses': [
            {'filled': {'oid': len(self.orders), 'totalSz': str(size), 'avgPx': '100'}}]}}}

    def status(self, address, cloid):
        if self.unknown:
            return {'status': 'unknownOid'}
        order = next(o for o in self.orders if o['cloid'] == cloid)
        return {'status': 'order', 'order': {'status': 'rejected' if self.reject else 'filled', 'order': {
            'oid': self.orders.index(order) + 1, 'coin': self.market_value.coin, 'cloid': cloid}}}


class RuleTests(ApplicationFixture):
    def setUp(self):
        super().setUp()
        self.app.accounts[WALLET] = Account()
        self.app.credentials.save(1, OTHER, KEY)
        self.exchange = Exchange(self.clock)
        self.worker = AutocloseWorker(
            self.app.autoclose_store, self.exchange, credentials=self.app.credentials,
            access=self.app.autoclose_access, requests=self.app.client,
            stop=self.app.stop, clock=self.clock)
        self.store = self.app.autoclose_store

    def command(self, text, user=1, kind='private', chat=None):
        message = self.message(text, chat=user if chat is None else chat, user=user)
        message.chat.type = kind
        self.app.handle_message(message)

    def arm(self, ticker='BTC'):
        self.clock.advance(.01)
        self.command(f'/autoclose {WALLET} {ticker}')
        self.worker.cycle()
        rules = self.store.active()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]['state'], 'armed')
        return rules[0]

    def state(self):
        return json.loads(self.queue.db.execute('SELECT data FROM autoclose_rules ORDER BY rowid DESC').fetchone()[0])

    def trigger(self):
        self.exchange.source = Decimal(9)
        self.clock.advance(1)
        self.worker.cycle()

    def reconcile(self):
        self.clock.advance(3)
        self.worker.cycle()

    def test_threshold_strict_and_baseline_does_not_grow(self):
        rule = self.arm()
        for size in (200, 15, 10, 100):
            self.exchange.source = Decimal(size)
            self.clock.advance(1)
            self.worker.cycle()
            self.assertFalse(self.exchange.orders)
        self.assertEqual(self.store.active()[0]['source_size'], rule['source_size'])
        self.trigger()
        self.assertEqual(len(self.exchange.orders), 1)
        self.reconcile()
        self.assertEqual(self.state()['state'], 'done')
        result = [j for j in self.jobs() if j['key'].startswith('autoclose:')][0]
        for text in ('Quantity: 10', 'Average price: 100.00000000', OTHER, WALLET):
            self.assertIn(text, result['text'])
        self.worker.cycle()
        self.assertEqual(len(self.exchange.orders), 1)

    def test_source_reverse_and_full_close_trigger_for_both_sides(self):
        for source, own in ((0, 10), (-50, 10), (0, -10), (50, -10)):
            with self.subTest(source=source, own=own):
                self.exchange.source = Decimal(-100 if source == 50 else 100)
                self.exchange.own = Decimal(own)
                self.arm()
                self.exchange.source = Decimal(source)
                self.clock.advance(1)
                self.worker.cycle()
                order = self.exchange.orders[-1]['order']
                self.assertEqual(order['b'], own < 0)
                self.assertTrue(order['r'])
                self.assertEqual(order['t'], {'limit': {'tif': 'Ioc'}})
                self.reconcile()
                self.assertFalse(self.store.active())

    def test_source_intrapoll_dip_ignored(self):
        self.arm()
        self.exchange.source = Decimal(5)
        self.exchange.source = Decimal(100)
        self.worker.cycle()
        self.assertFalse(self.exchange.orders)

    def test_duplicate_preserves_baseline(self):
        rule = self.arm()
        self.exchange.source = Decimal(150)
        self.command(f'/autoclose {WALLET} btc')
        self.worker.cycle()
        self.assertEqual(self.store.active()[0]['source_size'], '100')
        self.assertEqual(self.store.active()[0]['id'], rule['id'])
        self.assertIn('already active', self.jobs()[-1]['text'])

    def test_user_growth_and_partial_reduction_close_whole_remainder(self):
        self.arm()
        self.clock.advance(1)
        self.exchange.trade(5, 'B')
        self.clock.advance(1)
        self.exchange.trade(3, 'A')
        self.trigger()
        self.assertEqual(self.exchange.orders[0]['order']['s'], '12')
        self.reconcile()
        self.assertEqual(self.state()['filled'], '12')

    def test_manual_close_reopen_and_flip_never_inherited(self):
        for reopen in (False, True, 'flip'):
            with self.subTest(reopen=reopen):
                self.exchange.own = Decimal(10)
                self.arm()
                self.clock.advance(1)
                self.exchange.trade(10 if reopen != 'flip' else 20)
                if reopen is True:
                    self.clock.advance(1)
                    self.exchange.trade(10, 'B')
                self.trigger()
                self.assertFalse(self.exchange.orders)
                self.assertFalse(self.store.active())
                self.assertEqual(self.state()['state'], 'cancelled')

    def test_history_gap_stops_without_order(self):
        self.arm()
        self.exchange.own = Decimal(8)
        self.trigger()
        self.assertEqual(self.state()['state'], 'failed')
        self.assertFalse(self.exchange.orders)

    def test_pending_timeout_after_fill_reconciled_after_restart(self):
        self.arm()
        self.exchange.fail = DataFetchError('timeout')
        self.trigger()
        self.assertIn('pending', self.store.active()[0])
        replacement = AutocloseStore(self.queue, '12345')
        self.worker = AutocloseWorker(
            replacement, self.exchange, credentials=self.app.credentials,
            access=self.app.autoclose_access, requests=self.app.client,
            stop=self.app.stop, clock=self.clock)
        self.reconcile()
        self.assertEqual(self.state()['state'], 'done')
        self.assertEqual(len(self.exchange.orders), 1)
        self.assertEqual(len([j for j in self.jobs() if j['key'].startswith('autoclose:')]), 1)

    def test_unknown_outcome_stops_instead_of_retry(self):
        self.arm()
        self.exchange.fail = DataFetchError('timeout')
        self.exchange.unknown = True
        self.trigger()
        self.reconcile()
        self.assertTrue(self.store.active())
        self.clock.advance(30)
        self.worker.cycle()
        self.assertFalse(self.store.active())
        self.assertEqual(len(self.exchange.orders), 1)
        self.assertIn('unknown', self.jobs()[-1]['text'])

    def test_partial_fill_three_attempts_fixed_price_and_persisted_count(self):
        self.arm()
        self.exchange.fraction = Decimal('.5')
        self.trigger()
        self.reconcile()  # resolves 1, sends 2
        self.worker = AutocloseWorker(
            self.store, self.exchange, credentials=self.app.credentials,
            access=self.app.autoclose_access, requests=self.app.client,
            stop=self.app.stop, clock=self.clock)
        self.reconcile()  # resolves 2, sends 3
        self.reconcile()  # resolves 3, reports remainder
        self.assertEqual(len(self.exchange.orders), 3)
        self.assertEqual(self.state()['state'], 'failed')
        self.assertEqual(self.state()['filled'], '8.750')
        self.assertEqual({o['order']['p'] for o in self.exchange.orders}, {'99.000'})
        self.assertEqual([o['order']['s'] for o in self.exchange.orders], ['10', '5.0', '2.50'])
        self.assertEqual(len({o['cloid'] for o in self.exchange.orders}), 3)
        self.assertIn('Remaining position: 1.250', self.jobs()[-1]['text'])

    def test_retry_spacing(self):
        self.arm()
        self.exchange.fraction = Decimal('.5')
        self.trigger()
        self.clock.advance(.5)
        self.worker.cycle()
        self.assertEqual(len(self.exchange.orders), 1)
        self.clock.advance(2)
        self.worker.cycle()
        self.assertEqual(len(self.exchange.orders), 2)

    def test_rejected_ioc_three_attempts(self):
        self.arm()
        self.exchange.reject = True
        self.trigger()
        for _ in range(3):
            self.reconcile()
        self.assertEqual(len(self.exchange.orders), 3)
        self.assertEqual(self.state()['state'], 'failed')

    def test_cancel_queued_armed_and_while_inflight(self):
        for stage in ('queued', 'armed', 'pending'):
            with self.subTest(stage=stage):
                self.exchange.own = Decimal(10)
                self.exchange.source = Decimal(100)
                self.command(f'/autoclose {WALLET} BTC')
                if stage != 'queued':
                    self.worker.cycle()
                if stage == 'pending':
                    self.exchange.fraction = Decimal('.5')
                    self.trigger()
                count = len(self.exchange.orders)
                self.command(f'/cancelautoclose {WALLET} BTC')
                self.reconcile()
                self.assertEqual(len(self.exchange.orders), count)
                self.assertFalse(self.store.active())

    def test_cancel_after_full_fill_reports_success(self):
        self.arm()
        self.trigger()
        self.command(f'/cancelautoclose {WALLET} BTC')
        self.reconcile()
        self.assertEqual(self.state()['state'], 'done')

    def test_remove_wallet_readd_does_not_reactivate(self):
        self.arm()
        self.app._command(1, f'/removewallet {WALLET}')
        self.app._command(1, f'/addwallet {WALLET}')
        self.trigger()
        self.assertFalse(self.exchange.orders)
        self.assertEqual(self.state()['state'], 'cancelled')

    def test_credentials_replacement_even_same_address_cancels(self):
        self.arm()
        self.app.credentials.save(1, OTHER, KEY)
        self.trigger()
        self.assertFalse(self.exchange.orders)

    def test_access_revocation_cancels_without_sending_to_removed_chat(self):
        self.arm()
        before = len(self.jobs())
        self.app.config = replace(self.config, chat_ids=(2,))
        self.trigger()
        self.assertEqual(len(self.jobs()), before)
        self.assertFalse(self.exchange.orders)
        self.assertFalse(self.store.active())

    def test_multiple_rules_one_position_serialized(self):
        self.arm()
        self.app.accounts[OTHER] = Account()
        self.app.credentials.save(2, OTHER, KEY)
        self.command(f'/autoclose {WALLET} BTC', user=2)
        self.worker.cycle()
        self.exchange.unknown = True
        self.trigger()
        self.assertEqual(len(self.exchange.orders), 1)
        self.exchange.unknown = False
        self.reconcile()
        self.assertEqual(len(self.exchange.orders), 1)
        self.assertFalse(self.store.active())

    def test_other_user_cannot_cancel(self):
        self.arm()
        self.app.credentials.save(2, OTHER, KEY)
        self.command(f'/cancelautoclose {WALLET} BTC', user=2)
        self.assertFalse(self.store.cancelled(self.store.active()[0]))
        self.assertIn('No active', self.jobs()[-1]['text'])

    def test_registration_errors_and_private_gate(self):
        cases = [('/autoclose', 'Usage:'),
                 (f'/autoclose {OTHER} BTC', 'not being tracked'),
                 (f'/autoclose {WALLET} @1', 'Invalid perpetual'),
                 (f'/cancelautoclose {WALLET} BTC', 'No active')]
        for command, expected in cases:
            self.command(command)
            self.assertIn(expected, self.jobs()[-1]['text'])
        self.command(f'/autoclose {WALLET} BTC', user=2)
        self.assertIn('Credentials are missing', self.jobs()[-1]['text'])
        self.command(f'/autoclose {WALLET} BTC', kind='group')
        self.assertIn('private chat', self.jobs()[-1]['text'])
        count = len(self.jobs())
        self.command(f'/autoclose {WALLET} BTC', user=99)
        self.assertEqual(len(self.jobs()), count)

    def test_no_source_or_own_position_errors(self):
        for owner in ('source', 'own'):
            setattr(self.exchange, owner, Decimal(0))
            self.command(f'/autoclose {WALLET} BTC')
            self.worker.cycle()
            self.assertEqual(self.state()['state'], 'failed')
            self.assertIn('no open position', self.jobs()[-1]['text'])
            setattr(self.exchange, owner, Decimal(100))

    def test_queued_request_recovers_and_bot_namespace_isolated(self):
        self.command(f'/autoclose {WALLET} BTC')
        other = AutocloseStore(self.queue, 'other-bot')
        self.assertFalse(other.active())
        self.worker = AutocloseWorker(
            AutocloseStore(self.queue, '12345'), self.exchange, credentials=self.app.credentials,
            access=self.app.autoclose_access, requests=self.app.client,
            stop=self.app.stop, clock=self.clock)
        self.worker.cycle()
        self.assertEqual(self.state()['state'], 'armed')

    def test_atomic_finish_notification_rolls_back_on_full_outbox(self):
        rule = self.arm()
        with patch.object(self.queue, 'enqueue_transaction', side_effect=RuntimeError('full')):
            with self.assertRaises(RuntimeError):
                self.store.finish(rule, 'done', 'text')
        self.assertEqual(self.store.active()[0]['state'], 'armed')
        self.store.finish(rule, 'done', 'text')
        self.store.finish(rule, 'done', 'text')
        self.assertEqual(len([j for j in self.jobs() if j['key'].startswith('autoclose:')]), 1)

    def test_prepare_cancel_and_monotonic_nonce(self):
        rule = self.arm()
        order = self.exchange.market_value.order(Decimal(10), '99')
        a = self.store.prepare(rule, SIGNER, order, self.clock())
        b = self.store.prepare(rule, SIGNER, order, self.clock() - 1)
        self.assertEqual(b['nonce'], a['nonce'] + 1)
        self.store.cancel(1, WALLET, 'BTC')
        self.assertIsNone(self.store.prepare(rule, SIGNER, order, self.clock()))

    def test_slow_registration_does_not_block_commands_or_my(self):
        entered, release = threading.Event(), threading.Event()
        original = self.exchange.authority
        def slow(creds):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(creds)
        self.exchange.authority = slow
        self.command(f'/autoclose {WALLET} BTC')
        thread = threading.Thread(target=self.worker.cycle)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.command('/my')
            self.assertFalse(self.app.personal_queue.empty())
            before_help = len(self.jobs())
            self.app._command(1, '/help')
            self.assertIn('/autoclose', ''.join(job['text'] for job in self.jobs()[before_help:]))
            self.command(f'/cancelautoclose {WALLET} BTC')
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.exchange.orders)
        self.assertFalse(self.store.active())

    def test_transient_reads_defer_and_worker_shutdown_closes_session(self):
        self.arm()
        with patch.object(self.exchange, 'position', side_effect=DataFetchError('429')):
            self.worker.cycle()
        self.assertTrue(self.store.active())
        self.app.stop.set()
        self.worker.run(Mock())
        self.exchange.close.assert_called_once()


    def test_missing_credentials_at_registration_and_execution(self):
        self.command(f'/autoclose {WALLET} BTC')
        with patch.object(self.app.credentials, 'load', return_value=None):
            self.worker.cycle()
        self.assertIn('Credentials are missing', self.jobs()[-1]['text'])
        self.arm()
        with patch.object(self.app.credentials, 'load', return_value=None):
            self.trigger()
        self.assertIn('Credentials are missing', self.jobs()[-1]['text'])
        self.assertFalse(self.exchange.orders)

    def test_changed_authority_and_corrupt_credentials_stop(self):
        self.arm()
        with patch.object(self.exchange, 'authority', return_value=('other-signer', None)):
            self.trigger()
        self.assertIn('authority changed', self.jobs()[-1]['text'])
        self.exchange.source = Decimal(100)
        self.arm()
        with patch.object(self.app.credentials, 'load', side_effect=RuntimeError(KEY)), \
                self.assertLogs('walletwatcher.trading.worker', level='ERROR') as logs:
            self.trigger()
        self.assertNotIn(KEY, ''.join(logs.output))
        self.assertNotIn(KEY, self.jobs()[-1]['text'])

    def test_manual_close_during_last_refresh(self):
        self.arm()
        original = self.exchange.position
        def close(address, market, fresh=False):
            if address == OTHER and fresh:
                self.clock.advance(.001)
                self.exchange.trade(10)
            return original(address, market, fresh)
        with patch.object(self.exchange, 'position', side_effect=close):
            self.trigger()
        self.assertFalse(self.exchange.orders)
        self.assertIn('before order submission', self.jobs()[-1]['text'])

    def test_cancel_at_last_check_and_prepare_boundary(self):
        self.arm()
        original = self.exchange.mid
        def cancel(market):
            self.store.cancel(1)
            return original(market)
        with patch.object(self.exchange, 'mid', side_effect=cancel):
            self.trigger()
        self.assertFalse(self.exchange.orders)
        self.worker.cycle()
        self.arm()
        original_prepare = self.store.prepare
        def race(rule, signer, order, now):
            self.store.cancel(1)
            return original_prepare(rule, signer, order, now)
        with patch.object(self.store, 'prepare', side_effect=race):
            self.trigger()
        self.assertFalse(self.exchange.orders)

    def test_stop_before_cycle_and_live_worker_wait(self):
        self.arm()
        self.app.stop.set()
        self.worker.cycle()
        self.assertFalse(self.exchange.orders)
        self.app.stop.clear()
        def cycle():
            self.app.stop.set()
        with patch.object(self.worker, 'cycle', side_effect=cycle):
            beat = Mock()
            self.worker.run(beat)
        beat.assert_called_once()
        self.exchange.close.assert_called_once()

    def test_failed_http_reconciliation_after_expiration_stops(self):
        self.arm()
        self.trigger()
        self.clock.advance(30)
        with patch.object(self.exchange, 'position', side_effect=DataFetchError('offline')):
            self.worker.cycle()
        self.assertFalse(self.store.active())
        self.assertIn('could not be verified', self.jobs()[-1]['text'])

    def test_exchange_outer_error_unknown_shape_and_resting_response(self):
        for response in ({'status': 'err'},
                         {'status': 'ok', 'response': {'data': {'statuses': []}}},
                         {'status': 'ok', 'response': {'data': {'statuses': [{'resting': {'oid': 7}}]}}}):
            self.exchange.own, self.exchange.source = Decimal(10), Decimal(100)
            self.arm()
            self.exchange.unknown = True
            with patch.object(self.exchange, 'send', return_value=response):
                self.trigger()
            self.clock.advance(30)
            self.worker.cycle()
            self.assertFalse(self.store.active())

    def test_reconcile_rejects_wrong_order_and_excess_fills(self):
        for mutation, message in (({'cloid': 'wrong'}, 'different order'),
                                  ({'oid': 999}, 'different order ID')):
            self.exchange.own, self.exchange.source = Decimal(10), Decimal(100)
            self.arm()
            self.trigger()
            status = self.exchange.status(OTHER, self.store.active()[0]['pending']['cloid'])
            status['order']['order'].update(mutation)
            with patch.object(self.exchange, 'status', return_value=status):
                self.reconcile()
            self.assertIn(message, self.jobs()[-1]['text'])
        self.exchange.own, self.exchange.source = Decimal(10), Decimal(100)
        rule = self.arm()
        self.trigger()
        rule = self.store.active()[0]
        matched = [fill(size='11', oid=rule['pending']['oid'])]
        with self.assertRaisesRegex(TradingError, 'exceed'):
            self.worker.reconcile(rule, Decimal(0), matched)
        matched = [fill(side='B', oid=rule['pending']['oid'])]
        with self.assertRaisesRegex(TradingError, 'unexpected side'):
            self.worker.reconcile(rule, Decimal(0), matched)

    def test_http_success_waits_for_actual_fills(self):
        self.arm()
        self.trigger()
        rule = self.store.active()[0]
        self.assertFalse(self.worker.reconcile(rule, Decimal(0), []))
        self.assertIn('pending', self.store.active()[0])

    def test_restart_after_resolved_order_before_final_reply(self):
        self.arm()
        self.trigger()
        rule = self.store.active()[0]
        self.assertTrue(self.worker.reconcile(rule, Decimal(0), self.exchange.history))
        self.worker = AutocloseWorker(
            AutocloseStore(self.queue, '12345'), self.exchange, credentials=self.app.credentials,
            access=self.app.autoclose_access, requests=self.app.client,
            stop=self.app.stop, clock=self.clock)
        self.reconcile()
        self.assertEqual(self.state()['state'], 'done')
        self.assertEqual(len(self.exchange.orders), 1)

    def test_independent_manual_close_during_rejected_order_is_not_bot_success(self):
        self.arm()
        self.exchange.reject = True
        self.trigger()
        self.clock.advance(1)
        self.exchange.trade(10)
        self.reconcile()
        self.assertEqual(self.state()['state'], 'cancelled')

    def test_command_storage_failure_is_safe(self):
        with patch.object(self.store, 'create', side_effect=RuntimeError(KEY)), \
                self.assertLogs('walletwatcher.presentation.telegram', level='ERROR') as logs:
            self.command(f'/autoclose {WALLET} BTC')
        self.assertNotIn(KEY, ''.join(logs.output))
        self.assertIn('no activation', self.jobs()[-1]['text'])

    def test_hip3_rule_and_pending_restart_before_post(self):
        self.exchange.market_value = Market('xyz:EWZ', 'xyz', 110000, 2)
        self.arm('xyz:EWZ')
        rule = self.store.active()[0]
        order = self.exchange.market_value.order('10', '99')
        self.store.prepare(rule, SIGNER, order, self.clock())
        self.exchange.unknown = True
        self.worker = AutocloseWorker(
            self.store, self.exchange, credentials=self.app.credentials,
            access=self.app.autoclose_access, requests=self.app.client,
            stop=self.app.stop, clock=self.clock)
        self.reconcile()
        self.assertFalse(self.exchange.orders)
        self.clock.advance(30)
        self.worker.cycle()
        self.assertFalse(self.store.active())


    def test_same_millisecond_round_trip_is_detected(self):
        self.arm()
        self.exchange.trade(10)
        self.exchange.trade(10, 'B')
        self.trigger()
        self.assertFalse(self.exchange.orders)
        self.assertEqual(self.state()['state'], 'cancelled')

    def test_checkpoint_boundary_ids_prevent_replaying_processed_fills(self):
        self.arm()
        self.clock.advance(1)
        self.exchange.trade(1, 'B')
        self.worker.cycle()
        boundary = self.store.active()[0]['boundary']
        self.assertTrue(boundary)
        self.worker.cycle()  # same server millisecond and identical fill response
        self.assertTrue(self.store.active())
        self.assertEqual(self.store.active()[0]['own_size'], '11')
        self.assertEqual(self.store.active()[0]['boundary'], boundary)

    def test_activation_boundary_records_opening_fill_and_rejects_inconsistent_snapshot(self):
        self.exchange.own = Decimal(0)
        self.exchange.trade(10, 'B')
        self.command(f'/autoclose {WALLET} BTC')
        self.worker.cycle()
        self.assertEqual(self.store.active()[0]['boundary'], ['1'])
        self.worker.cycle()
        self.assertTrue(self.store.active())
        self.store.cancel(1)
        self.worker.cycle()
        self.exchange.own = Decimal(11)  # snapshot contradicts last boundary fill
        self.command(f'/autoclose {WALLET} BTC')
        self.worker.cycle()
        self.assertIn('changed during activation', self.jobs()[-1]['text'])
        self.assertFalse(self.store.active())

    def test_registration_api_error_is_reported(self):
        self.command(f'/autoclose {WALLET} BTC')
        with patch.object(self.exchange, 'authority', side_effect=DataFetchError('offline')):
            self.worker.cycle()
        self.assertIn('Retrying automatically', self.jobs()[-1]['text'])
        self.assertEqual(self.store.active()[0]['state'], 'queued')

    def test_full_database_reopen_preserves_rules_orders_credentials_and_notifications(self):
        from walletwatcher.app import Application
        from walletwatcher.storage.delivery import DeliveryQueue
        from walletwatcher.storage.wallets import save_wallets
        save_wallets(self.app.accounts, self.config.wallets_file)
        self.arm()
        self.trigger()
        outbox = DeliveryQueue(self.sender, self.config.database_file, clock=self.clock)
        self.addCleanup(outbox.close)
        restored = Application(self.config, bot=self.sender, client=self.client, outbox=outbox)
        worker = AutocloseWorker(
            restored.autoclose_store, self.exchange, credentials=restored.credentials,
            access=restored.autoclose_access, requests=restored.client,
            stop=restored.stop, clock=self.clock)
        self.clock.advance(3)
        worker.cycle()
        self.assertEqual(self.state()['state'], 'done')
        self.assertEqual(len(self.exchange.orders), 1)
        self.assertEqual(restored.credentials.load(1).address, OTHER)
        worker.cycle()
        self.assertEqual(len([j for j in self.jobs() if j['key'].startswith('autoclose:')]), 1)

    def test_malformed_open_order_status_does_not_permit_retry(self):
        self.arm()
        self.trigger()
        rule = self.store.active()[0]
        status = self.exchange.status(OTHER, rule['pending']['cloid'])
        status['order']['status'] = 'new-unrecognized-state'
        with patch.object(self.exchange, 'status', return_value=status):
            self.reconcile()
            self.assertEqual(len(self.exchange.orders), 1)
            self.clock.advance(30)
            self.worker.cycle()
        self.assertFalse(self.store.active())


    def test_commands_to_signed_http_order_and_confirmation_end_to_end(self):
        for coin, subaccount in (('BTC', False), ('xyz:EWZ', False), ('BTC', True)):
            with self.subTest(coin=coin, subaccount=subaccount):
                self.exchange = Exchange(self.clock)
                self.exchange.market_value = Market(coin, coin.split(':')[0] if ':' in coin else '',
                                                    110000 if ':' in coin else 0, 3)
                session = Mock()
                client = HyperliquidClient(personal_session_factory=lambda: session, clock=self.clock)
                api = TradingAPI(client)
                self.addCleanup(api.close)
                self.worker = AutocloseWorker(
                    self.store, api, credentials=self.app.credentials,
                    access=self.app.autoclose_access, requests=self.app.client,
                    stop=self.app.stop, clock=self.clock)
                def post(url, json, timeout):
                    if url.endswith('/exchange'):
                        vault = OTHER if subaccount else None
                        self.assertEqual(json['vaultAddress'], vault)
                        signer = recover_agent_or_user_from_l1_action(json['action'], json['signature'],
                                  vault, json['nonce'], json['expiresAfter'], True)
                        self.assertEqual(signer.lower(), SIGNER)
                        attempt = dict(order=json['action']['orders'][0], nonce=json['nonce'],
                                       cloid=json['action']['orders'][0]['c'],
                                       number=len(self.exchange.orders) + 1)
                        data = self.exchange.send(Credentials(OTHER, KEY), vault, attempt)
                    else:
                        kind = json['type']
                        if kind == 'userRole':
                            data = ({'role': 'subAccount', 'data': {'master': WALLET}} if subaccount else
                                    {'role': 'user'}) if json['user'] == OTHER else {
                                        'role': 'agent', 'data': {'user': WALLET if subaccount else OTHER}}
                        elif kind == 'meta':
                            data = {'universe': [{'name': coin, 'szDecimals': 3}]}
                        elif kind == 'perpDexs':
                            data = [None, {'name': 'xyz'}]
                        elif kind == 'clearinghouseState':
                            size = self.exchange.source if json['user'] == WALLET else self.exchange.own
                            data = {'time': int(self.clock() * 1000), 'assetPositions': [
                                {'position': {'coin': coin, 'szi': str(size)}}] if size else []}
                        elif kind == 'userFills':
                            data = list(reversed(self.exchange.history))
                        elif kind == 'userFillsByTime':
                            data = self.exchange.fills(OTHER, json['startTime'], json['endTime'])
                        elif kind == 'allMids':
                            data = {coin: '100'}
                        elif kind == 'orderStatus':
                            data = self.exchange.status(OTHER, json['oid'])
                        else:
                            self.fail(f'Unexpected request: {kind}')
                    response = Mock(status_code=200)
                    response.json.return_value = data
                    return response
                session.post.side_effect = post
                self.arm(coin)
                self.trigger()
                self.reconcile()
                self.assertEqual(self.state()['state'], 'done')
                self.assertEqual(len(self.exchange.orders), 1)
                self.assertEqual(self.exchange.orders[0]['order']['a'], self.exchange.market_value.asset)


    def test_unknown_partial_order_fences_other_rules_and_late_reconciliation_releases(self):
        self.arm()
        self.app.credentials.save(2, OTHER, KEY)
        self.command(f'/autoclose {WALLET} BTC', user=2)
        self.worker.cycle()
        self.exchange.fraction = Decimal('.5')
        self.exchange.unknown = True
        self.trigger()
        self.clock.advance(30)
        self.worker.cycle()
        self.assertFalse(self.store.active())
        self.assertEqual(len(self.exchange.orders), 1)
        self.assertIn('unresolved', self.jobs()[-1]['text'])
        self.worker.cycle()  # the terminal rule still fences the account
        self.assertEqual(len(self.exchange.orders), 1)
        self.exchange.unknown = False
        self.worker.cycle()
        self.assertFalse(self.store.unresolved())
        self.exchange.source = Decimal(100)
        self.arm()
        self.assertEqual(len(self.exchange.orders), 1)

    def test_late_full_close_updates_notification_without_duplicate_order(self):
        self.arm()
        self.exchange.unknown = True
        self.trigger()
        self.clock.advance(30)
        self.worker.cycle()
        self.assertEqual(self.state()['state'], 'failed')
        self.exchange.unknown = False
        self.worker.cycle()
        self.assertEqual(self.state()['state'], 'done')
        self.assertFalse(self.store.unresolved())
        self.assertEqual(len(self.exchange.orders), 1)
        final = [j for j in self.jobs() if j['key'].startswith('autoclose:')]
        self.assertEqual(len(final), 1)
        self.assertIn('market close confirmed', final[0]['text'])

    def test_late_terminal_order_with_missing_history_releases_fence_without_false_success(self):
        self.arm()
        self.exchange.unknown = True
        self.trigger()
        self.clock.advance(30)
        self.worker.cycle()
        self.exchange.unknown = False
        with patch.object(self.exchange, 'fills', side_effect=TradingError('truncated')):
            self.worker.cycle()
        self.assertFalse(self.store.unresolved())
        self.assertIn('details could not be verified', self.jobs()[-1]['text'])
        self.assertEqual(self.state()['state'], 'failed')


    def test_late_confirmation_and_order_journal_rollback_together_on_delivery_failure(self):
        self.arm()
        self.exchange.unknown = True
        self.trigger()
        self.clock.advance(30)
        self.worker.cycle()
        self.exchange.unknown = False
        with patch.object(self.queue, 'enqueue_transaction', side_effect=RuntimeError('full')):
            self.worker.cycle()
        self.assertTrue(self.store.unresolved())
        self.assertEqual(self.state()['state'], 'failed')
        self.worker.cycle()
        self.assertFalse(self.store.unresolved())
        self.assertEqual(self.state()['state'], 'done')
        self.assertEqual(len(self.exchange.orders), 1)

    def test_late_reconciliation_read_failure_keeps_fence(self):
        self.arm()
        self.exchange.unknown = True
        self.trigger()
        self.clock.advance(30)
        self.worker.cycle()
        with patch.object(self.exchange, 'status', side_effect=DataFetchError('offline')):
            self.worker.cycle()
        self.assertTrue(self.store.unresolved())
        self.assertEqual(len(self.exchange.orders), 1)


class MathTests(unittest.TestCase):
    def test_exact_decimal_threshold(self):
        for base in ('100', '-100'):
            sign = Decimal(1 if base[0] != '-' else -1)
            self.assertFalse(triggered(base, sign * Decimal(10)))
            self.assertTrue(triggered(base, sign * Decimal('9.99999999999999999999')))
            self.assertTrue(triggered(base, 0))
            self.assertTrue(triggered(base, -sign))

    def test_precision_rounding_keeps_price_inside_one_percent(self):
        for precision in range(7):
            market = Market('X', '', 0, precision)
            for mid in ('0.00012345', '22.1841', '80979.4', '1000000'):
                for buy in (True, False):
                    raw = Decimal(mid) * (Decimal('1.01') if buy else Decimal('.99'))
                    if raw < 1 and precision == 6 and buy:
                        with self.assertRaises(TradingError):
                            market.price(mid, buy)
                        continue
                    try:
                        price = Decimal(market.price(mid, buy))
                    except TradingError:
                        self.assertLess(raw, 1)
                        continue
                    self.assertLessEqual(price, raw) if buy else self.assertGreaterEqual(price, raw)
                    self.assertGreater(price, 0)
        for mid in (0, -1):
            with self.assertRaises(TradingError):
                Market('BTC', '', 0, 3).price(mid, False)

    def test_size_and_ticker_validation(self):
        self.assertEqual(ticker_name('XYZ:ewz'), 'xyz:EWZ')
        self.assertEqual(ticker_name('btc'), 'BTC')
        for ticker in ('@1', '../BTC', 'BTC/USDC', '', 'a:b:c'):
            with self.assertRaises(TradingError):
                ticker_name(ticker)
        for size in ('0', '.0001'):
            with self.assertRaises(TradingError):
                Market('BTC', '', 0, 3).order(size, '100')
        for value in ('NaN', 'Infinity'):
            with self.assertRaises(TradingError):
                decimal(value)

    def test_replay_consistency_and_lifecycle(self):
        rule = dict(own_size='10', checkpoint=1, ticker='BTC', boundary=['1'])
        self.assertFalse(replay(rule, Decimal(10), 2, []))
        self.assertTrue(replay(rule, Decimal(0), 3, [fill(stamp=2)]))
        self.assertTrue(replay(rule, Decimal(10), 4, [fill(stamp=2), fill('0', '10', 'B', 3)]))
        self.assertFalse(replay(rule, Decimal(10), 4, [fill(stamp=1), fill(stamp=2, coin='ETH')]))
        for current, stamp, fills in [(Decimal(10), 0, []), (Decimal(9), 2, []),
                                      (Decimal(0), 2, [fill(start='9', stamp=2)]),
                                      (Decimal(0), 2, [fill(size='0', stamp=2)]),
                                      (Decimal(0), 2, [fill(side='X', stamp=2)])]:
            with self.assertRaises(TradingError):
                replay(rule, current, stamp, fills)


class APITests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.session = Mock()
        self.client.personal_session_factory.return_value = self.session
        self.api = TradingAPI(self.client)
        self.responses = []
        self.client._request.side_effect = lambda payload, session=None: self.responses.pop(0)

    def test_markets_main_hip3_canonical_case_and_offset(self):
        self.responses = [{'universe': [{'name': 'BTC', 'szDecimals': 5}]}]
        self.assertEqual(self.api.market('BTC'), Market('BTC', '', 0, 5))
        self.responses = [{'universe': [{'name': 'EWZ', 'szDecimals': 2}]},
                          [None, {'name': 'xyz'}]]
        self.assertEqual(self.api.market('xyz:EWZ'), Market('xyz:EWZ', 'xyz', 110000, 2))
        self.responses = [{'universe': [{'name': 'kPEPE', 'szDecimals': 0}]}]
        self.assertEqual(self.api.market('KPEPE').coin, 'kPEPE')
        for metadata in ({'universe': []}, {'universe': [{'name': 'BTC', 'szDecimals': 7}]},
                         {'universe': [{'name': 'BTC', 'szDecimals': 3, 'isDelisted': True}]}):
            self.responses = [metadata]
            with self.assertRaises(TradingError):
                self.api.market('BTC')
        self.responses = [{'universe': [{'name': 'xyz:EWZ', 'szDecimals': 2}]}, [None]]
        with self.assertRaises(TradingError):
            self.api.market('xyz:EWZ')

    def test_position_cache_grouped_by_wallet_dex_and_fresh_bypass(self):
        market = Market('xyz:EWZ', 'xyz', 110000, 2)
        self.responses = [{'time': 123, 'assetPositions': [
            {'position': {'coin': 'EWZ', 'szi': '-2.01'}}]}]
        self.assertEqual(self.api.position(OTHER, market), (Decimal('-2.01'), 123))
        self.assertEqual(self.api.position(OTHER, market), (Decimal('-2.01'), 123))
        self.assertEqual(self.client._request.call_count, 1)
        self.responses = [{'time': 124, 'assetPositions': []}]
        self.assertEqual(self.api.position(OTHER, market, fresh=True), (Decimal(0), 124))
        self.api.begin_cycle()
        self.responses = [{'time': 0, 'assetPositions': []}]
        with self.assertRaises(TradingError):
            self.api.position(OTHER, market)
        self.api.close()
        self.session.close.assert_called_once()
        self.api.close()

    def test_authority_main_subaccount_owner_and_agent(self):
        credentials = Credentials(OTHER, KEY)
        self.responses = [{'role': 'user'}, {'role': 'agent', 'data': {'user': OTHER}}]
        self.assertEqual(self.api.authority(credentials), (SIGNER, None))
        self.responses = [{'role': 'subAccount', 'data': {'master': WALLET}},
                          {'role': 'agent', 'data': {'user': WALLET}}]
        self.assertEqual(self.api.authority(credentials), (SIGNER, OTHER))
        self.responses = [{'role': 'user'}, {'role': 'user'}]
        self.assertEqual(self.api.authority(Credentials(SIGNER, KEY)), (SIGNER, None))
        for role in ('vault', 'missing', 'agent'):
            self.responses = [{'role': role}]
            with self.assertRaises(TradingError):
                self.api.authority(credentials)
        self.responses = [{'role': 'user'}, {'role': 'agent', 'data': {'user': WALLET}}]
        with self.assertRaises(TradingError):
            self.api.authority(credentials)

    def test_sign_and_recover_reduce_only_order_without_network(self):
        self.api.session = self.session
        attempt = dict(order=Market('BTC', '', 0, 3).order('-2.001', '101'),
                       nonce=123456, expires=138456)
        self.client._perform_request.return_value = {'status': 'ok'}
        for vault in (None, OTHER):
            self.assertEqual(self.api.send(Credentials(OTHER, KEY), vault, attempt), {'status': 'ok'})
            args, kwargs = self.client._perform_request.call_args
            payload = args[0]
            signer = recover_agent_or_user_from_l1_action(payload['action'], payload['signature'],
                     vault, payload['nonce'], payload['expiresAfter'], True)
            self.assertEqual(signer.lower(), SIGNER)
            self.assertNotIn(KEY, json.dumps(payload))
            self.assertTrue(payload['action']['orders'][0]['r'])
            self.assertEqual(kwargs, {'exchange': True})
            self.assertEqual(payload['vaultAddress'], vault)

    def test_mid_and_status(self):
        market = Market('BTC', '', 0, 3)
        self.responses = [{'BTC': '100'}]
        self.assertEqual(self.api.mid(market), Decimal(100))
        self.responses = [{'BTC': '0'}]
        with self.assertRaises(TradingError):
            self.api.mid(market)
        self.responses = [{'status': 'unknownOid'}]
        self.assertEqual(self.api.status(OTHER, '0xabc'), {'status': 'unknownOid'})
        self.assertEqual(self.client._request.call_args.args[0]['oid'], '0xabc')

    def test_history_pagination_overlap_deduplication(self):
        first = [fill(stamp=i + 1, tid=i) for i in range(500)]
        self.responses = [[], first, [first[-1], fill(stamp=501, tid=500)]]
        result = self.api.fills(OTHER, 1, 1000)
        self.assertEqual(len(result), 501)
        self.assertEqual(self.client._request.call_args.args[0]['startTime'], 500)
        self.assertFalse(self.client._request.call_args.args[0]['aggregateByTime'])

    def test_history_invalid_and_retention_guards(self):
        scenarios = [([{}], 'Invalid'),
                     ([[fill(stamp=2)] * 2000], 'continuity'),
                     ([[], {}], 'Invalid'),
                     ([[], [fill(stamp=0)]], 'outside'),
                     ([[], [fill(), fill(size='1')]], 'Conflicting'),
                     ([[], [fill(stamp=1, tid=i) for i in range(500)]], 'truncated')]
        for responses, message in scenarios:
            self.responses = responses
            with self.subTest(message=message), self.assertRaisesRegex(TradingError, message):
                self.api.fills(OTHER, 1, 10000002)

    def test_http_exchange_endpoint_weight_budget_cooldown_and_errors(self):
        session = Mock()
        response = session.post.return_value
        response.status_code = 200
        response.json.return_value = {'status': 'ok'}
        client = HyperliquidClient(session=session)
        self.assertEqual(client._perform_request({'action': {}}, session, exchange=True), {'status': 'ok'})
        self.assertTrue(session.post.call_args.args[0].endswith('/exchange'))
        self.assertEqual(client.calls[-1][1], 1)
        response.json.return_value = []
        client._request({'type': 'userFillsByTime'}, session)
        self.assertEqual(client.calls[-1][1], 20)
        client._request({'type': 'userRole'}, session)
        self.assertEqual(client.calls[-1][1], 60)
        response.status_code = 429
        response.headers = {'Retry-After': '2'}
        response.raise_for_status.side_effect = requests.HTTPError()
        with self.assertRaises(DataFetchError):
            client._perform_request({'action': {}}, session, exchange=True)
        with self.assertRaisesRegex(DataFetchError, 'cooldown'):
            client._request({'type': 'userRole'}, session)
