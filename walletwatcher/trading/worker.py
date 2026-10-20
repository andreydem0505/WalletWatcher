"""Single supervised worker serializes trading decisions, independently of monitoring."""
import logging
import time
from dataclasses import asdict
from decimal import Decimal

from walletwatcher.hyperliquid.client import DataFetchError
from walletwatcher.trading.api import Market, TradingAPI, TradingError, checkpoint, decimal, replay, triggered

logger = logging.getLogger(__name__)


class AutocloseWorker:
    def __init__(self, app, store, api=None, clock=time.time):
        self.app, self.store = app, store
        self.api = api or TradingAPI(app.client)
        self.clock = clock

    def valid(self, rule):
        with self.app.lock:
            tracked = rule['wallet'] in self.app.accounts
        return (tracked and rule['user'] in self.app.config.chat_ids
                and self.app.credentials.version(rule['user']) == rule['version'])

    def finish(self, rule, state, reason, update_terminal=False, attempt=None):
        self.store.finish(rule, state, f"Autoclose {rule['ticker']}: {reason}",
                          notify=rule['user'] in self.app.config.chat_ids, update_terminal=update_terminal,
                          attempt=attempt)

    def success(self, rule, update_terminal=False, attempt=None):
        amount = decimal(rule['filled'])
        if amount <= 0:
            self.finish(rule, 'cancelled', 'your original position was closed independently. Rule finished.')
            return
        average = decimal(rule['value']) / amount
        self.finish(rule, 'done', f"market close confirmed.\nAccount: {rule['address']}\n"
                    f"Quantity: {amount}\nAverage price: {average:.8f}\nSource: {rule['wallet']}",
                    update_terminal, attempt)

    def arm(self, rule):
        credentials = self.app.credentials.load(rule['user'])
        if credentials is None:
            raise TradingError('Credentials are missing. Save them with /creds first.')
        rule['activation_stage'] = 'checking trading permissions'
        signer, vault = self.api.authority(credentials)
        rule['activation_stage'] = 'loading market metadata'
        market = self.api.market(rule['ticker'])
        rule['activation_stage'] = 'reading the tracked wallet position'
        source, _ = self.api.position(rule['wallet'], market, fresh=True)
        rule['activation_stage'] = 'reading your trading account position'
        own, stamp = self.api.position(credentials.address, market, fresh=True)
        if source == 0:
            raise TradingError('The tracked wallet has no open position in this ticker.')
        if own == 0:
            raise TradingError('Your trading account has no open position in this ticker.')
        rule['activation_stage'] = 'checking your position fill history'
        boundary = self.api.fills(credentials.address, stamp, stamp)
        own_boundary = [f for f in boundary if f['coin'] == market.coin]
        if own_boundary:
            last = own_boundary[-1]
            after = decimal(last['startPosition']) + decimal(last['sz']) * (1 if last['side'] == 'B' else -1)
            if after != own:
                raise TradingError('Your position changed during activation. Please retry /autoclose.')
        if self.clock() >= rule.get('activation_deadline', float('inf')):
            raise TradingError('Activation timed out; no rule was armed. Please retry /autoclose.')
        rule.update(address=credentials.address, signer=signer, vault=vault, market=asdict(market), ticker=market.coin,
                    source_size=str(source), own_size=str(own), original_size=str(own),
                    checkpoint=stamp, state='armed')
        checkpoint(rule, own, stamp, boundary)
        if not self.valid(rule) or self.store.cancelled(rule):
            self.finish(rule, 'cancelled', 'cancelled before activation; account or monitoring settings changed.')
            return
        self.store.notify(rule, f"Autoclose armed for {market.coin}.\nSource: {rule['wallet']}\n"
                          f"Account: {credentials.address}\nSource baseline: {abs(source)}. "
                          'Trigger: less than 10% remaining in the original direction. '
                          'One-shot market close, 1% price limit, up to 3 attempts.')

    @staticmethod
    def terminal_status(rule, status):
        attempt = rule['pending']
        terminal = False
        if status.get('status') == 'order':
            details = status['order']
            order = details['order']
            if order.get('cloid') != attempt['cloid'] or order['coin'] != rule['ticker']:
                raise TradingError('Order reconciliation returned a different order.')
            if attempt.get('oid', order['oid']) != order['oid']:
                raise TradingError('Order reconciliation returned a different order ID.')
            attempt['oid'] = order['oid']
            outcome = details['status']
            terminal = outcome in ('filled', 'canceled', 'rejected') or outcome.endswith(('Canceled', 'Rejected'))
        # An explicit exchange rejection is definitive even if orderStatus unknown.
        return terminal or bool(attempt.get('rejected'))

    def reconcile(self, rule, current, fills, status=None, persist=True):
        attempt = rule['pending']
        response = attempt.get('response', {})
        status = status if status is not None else self.api.status(rule['address'], attempt['cloid'])
        terminal = self.terminal_status(rule, status)
        oid = attempt.get('oid')
        matched = [f for f in fills if oid is not None and str(f['oid']) == str(oid)
                   and f['coin'] == rule['ticker']]
        quantity = sum((decimal(f['sz']) for f in matched), Decimal(0))
        value = sum((decimal(f['sz']) * decimal(f['px']) for f in matched), Decimal(0))
        if quantity > decimal(attempt['order']['s']):
            raise TradingError('Order fills exceed the submitted size.')
        if any(f['side'] != ('B' if attempt['order']['b'] else 'A') or decimal(f['px']) <= 0
               or decimal(f['sz']) <= 0 for f in matched):
            raise TradingError('Order fills have an unexpected side, size or price.')
        expected = decimal(response.get('totalSz', '0'))
        if expected > quantity or (status.get('status') == 'order'
                                    and status['order']['status'] == 'filled' and not matched):
            terminal = False  # HTTP response is not a substitute for reconciled fills.
        if not terminal:
            self.store.record(rule, attempt)
            if self.clock() * 1000 > attempt['expires'] + 5000:
                raise TradingError('Previous order outcome is unknown. No further order will be sent; check your account.')
            return False
        rule['filled'] = str(decimal(rule['filled']) + quantity)
        rule['value'] = str(decimal(rule['value']) + value)
        attempt.update(resolved=True, filled=str(quantity), value=str(value))
        rule.pop('pending')
        rule['last_send'] = attempt['sent']
        if persist:
            self.store.record(rule, attempt)
        return True

    def step(self, rule):
        invalid = not self.valid(rule) or self.store.cancelled(rule)
        if invalid and 'pending' not in rule:
            self.finish(rule, 'cancelled', 'cancelled; no further orders will be sent.')
            return
        for other, active in self.store.unresolved():
            if (other['id'] != rule['id'] and other.get('address') == rule.get('address')
                    and other['ticker'] == rule['ticker']):
                if not active:
                    raise TradingError('A previous order for this account and ticker is unresolved. '
                                       'No new order will be sent; check your account.')
                return  # One unresolved order per trading account and instrument.
        if rule['state'] == 'queued':
            if 'activation_deadline' not in rule:
                rule['activation_deadline'] = self.clock() + 300
                self.store.save(rule)
            if self.clock() >= rule['activation_deadline']:
                self.finish(rule, 'failed', rule.get('activation_error', 'Activation did not complete.') +
                            ' Activation timed out; no rule was armed. Please retry /autoclose.')
                return
            if self.clock() < rule.get('activation_retry_at', 0):
                return
            self.arm(rule)
            return
        market = Market(**rule['market'])
        current, stamp = self.api.position(rule['address'], market)
        fills = self.api.fills(rule['address'], rule['checkpoint'], stamp)
        ended = replay(rule, current, stamp, fills)
        if 'pending' in rule:
            if not self.reconcile(rule, current, fills):
                return
            if current == 0:
                self.success(rule)
                return
        if invalid:
            self.finish(rule, 'cancelled', 'cancelled; an already submitted order may have executed. '
                        f"Confirmed filled: {rule['filled']}. Remaining position: {abs(current)}.")
            return
        if ended or current == 0 or current * decimal(rule['original_size']) <= 0:
            if current == 0 and decimal(rule['filled']) > 0:
                self.success(rule)
                return
            self.finish(rule, 'cancelled', 'your original position was closed or reversed. A reopened position is not affected.')
            return
        checkpoint(rule, current, stamp, fills)
        self.store.save(rule)
        if rule['attempts'] >= 3:
            self.finish(rule, 'failed', f"3 market-close attempts completed. Remaining position: {abs(current)}. "
                        'The price limit was not widened.')
            return
        source, _ = self.api.position(rule['wallet'], market)
        if rule['state'] == 'armed' and not triggered(rule['source_size'], source):
            return
        if self.clock() < rule.get('last_send', 0) + 2:
            return
        credentials = self.app.credentials.load(rule['user'])
        if credentials is None:
            raise TradingError('Credentials are missing.')
        signer, vault = self.api.authority(credentials)
        if signer != rule['signer'] or vault != rule['vault']:
            raise TradingError('Trading authority changed. Rule cancelled.')
        if 'price' not in rule:
            rule['price'] = market.price(self.api.mid(market), current < 0)
        # Refresh immediately before submitting, replaying any fills since the
        # cycle snapshot. External manual trading cannot be atomic with our POST.
        latest, stamp = self.api.position(rule['address'], market, fresh=True)
        fills = self.api.fills(rule['address'], rule['checkpoint'], stamp)
        if replay(rule, latest, stamp, fills) or latest * current <= 0:
            self.finish(rule, 'cancelled', 'your original position closed or reversed before order submission.')
            return
        checkpoint(rule, latest, stamp, fills)
        if self.app.stop.is_set() or not self.valid(rule) or self.store.cancelled(rule):
            return
        attempt = self.store.prepare(rule, signer, market.order(latest, rule['price']), self.clock())
        if attempt is None:
            return
        try:
            result = self.api.send(credentials, vault, attempt)
        except DataFetchError:
            # Durable pending attempt is reconciled next cycle, never resubmitted.
            return
        finally:
            self.api.begin_cycle()  # Other rules must observe post-order positions.
        if result.get('status') != 'ok':
            attempt.update(rejected=True, resolved=True)
            rule.pop('pending')
            self.store.record(rule, attempt)
            raise TradingError('Exchange rejected the order; check API wallet permissions and account limits. '
                               'No further orders will be sent.')
        else:
            statuses = result['response']['data']['statuses']
            if len(statuses) != 1:
                raise TradingError('Unexpected order response. Check your account before arming a new rule.')
            status = statuses[0]
            if 'filled' in status:
                attempt['response'] = status['filled']
                attempt['oid'] = status['filled']['oid']
            elif 'error' in status:
                attempt['rejected'] = True
                # Do not relay untrusted exchange strings or credentials.
                attempt['error'] = 'IOC order was rejected or could not match within the price limit.'
        self.store.record(rule, attempt)

    def cycle(self):
        self.api.begin_cycle()
        # A failed rule can still have an in-flight request. Keep its account
        # fenced until a definitive status is observed, including after restart.
        for rule, active in self.store.unresolved():
            if active or self.app.stop.is_set():
                continue
            try:
                status = self.api.status(rule['address'], rule['pending']['cloid'])
                if not self.terminal_status(rule, status):
                    continue
                attempt = rule['pending']
                try:
                    current, stamp = self.api.position(rule['address'], Market(**rule['market']), fresh=True)
                    fills = self.api.fills(rule['address'], rule['checkpoint'], stamp)
                    if self.reconcile(rule, current, fills, status, persist=False):
                        if current == 0 and decimal(rule['filled']) > 0:
                            self.success(rule, update_terminal=True, attempt=attempt)
                        else:
                            self.finish(rule, 'failed', f"late reconciliation confirmed {rule['filled']} filled. "
                                        f"Remaining position: {abs(current)}. No further orders will be sent.",
                                        update_terminal=True, attempt=attempt)
                except TradingError:
                    attempt = rule.pop('pending')
                    attempt['resolved'] = True  # The verified order is terminal.
                    self.finish(rule, 'failed', 'previous order is terminal, but execution details '
                                'could not be verified. Check your account.', update_terminal=True, attempt=attempt)
            except Exception:
                # No trading or repeated notifications during late reconciliation.
                continue
        for rule in self.store.active():
            if self.app.stop.is_set():
                break
            try:
                self.step(rule)
            except DataFetchError as error:
                if rule['state'] == 'queued':
                    self.activation_failed(rule, error)
                elif 'pending' in rule and self.clock() * 1000 > rule['pending']['expires'] + 5000:
                    self.finish(rule, 'failed', 'order outcome could not be verified. No further orders will be sent; check your account.')
                # Read-only API/rate failures defer the check without fabricating a close.
            except TradingError as error:
                self.finish(rule, 'failed', str(error))
            except Exception as error:
                logger.error('Autoclose rule %s failed (%s)', rule['id'], type(error).__name__)
                self.finish(rule, 'failed', 'account data or credentials could not be verified. '
                            'No further orders will be sent; check your account.')

    def activation_failed(self, rule, error):
        stage = rule.get('activation_stage', 'loading account data')
        reason = f"activation failed while {stage}: {error.diagnostic()}."
        # Only safe, structured details; never log credentials or exception text.
        logger.warning('Autoclose rule %s: %s', rule['id'], reason)
        failures = rule.get('activation_failures', 0) + 1
        rule.update(activation_failures=failures, activation_error=reason)
        if not error.retryable or failures >= 3:
            self.finish(rule, 'failed', reason + ' No rule was armed. Please retry /autoclose later.')
            return
        deadline = rule.setdefault('activation_deadline', self.clock() + 300)
        delay = max(self.app.config.poll_interval * 2 ** (failures - 1), error.retry_after)
        rule['activation_retry_at'] = min(deadline, self.clock() + delay)
        self.store.save(rule)
        if failures == 1 and rule['user'] in self.app.config.chat_ids:
            self.app.outbox.enqueue(rule['user'],
                f"Autoclose {rule['ticker']}: {reason} Retrying automatically; the rule is not armed yet.",
                key='autoclose-activation:' + rule['id'])

    def run(self, heartbeat):
        self.app.client.set_heartbeat(heartbeat)
        try:
            while not self.app.stop.is_set():
                heartbeat()
                self.cycle()
                self.app.stop.wait(self.app.config.poll_interval)
        finally:
            self.api.close()
