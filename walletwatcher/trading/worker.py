"""Single supervised worker serializes trading decisions, independently of monitoring."""
import logging
from hashlib import sha256
import time
from threading import Event
from dataclasses import asdict
from decimal import Decimal
from typing import Callable

from walletwatcher.hyperliquid.errors import DataFetchError
from walletwatcher.hyperliquid.rate_limit import REQUEST_WEIGHTS, WINDOW_SECONDS
from walletwatcher.trading.gateway import TradingGateway
from walletwatcher.trading.ports import CredentialReader, RuleAccess, TradingRequests
from walletwatcher.trading.policy import (
    ACTIVATION_TIMEOUT, MAX_CLOSE_ATTEMPTS, MIN_RETRY_INTERVAL, ORDER_CONFIRMATION_GRACE_MS,
    Market, TradingError, checkpoint, decimal, replay, triggered,
)

logger = logging.getLogger(__name__)


class AutocloseWorker:
    def __init__(self, store, api: TradingGateway, *, credentials: CredentialReader,
                 access: RuleAccess, requests: TradingRequests, stop: Event,
                 clock: Callable[[], float] = time.time):
        self.store, self.api = store, api
        self.credentials, self.access = credentials, access
        self.requests, self.stop = requests, stop
        self.clock = clock
        self.closing_rules = set()

    def valid(self, rule):
        return (self.access.tracks_wallet(rule['wallet']) and self.access.allows_user(rule['user'])
                and self.credentials.version(rule['user']) == rule['version'])

    def finish(self, rule, state, reason, update_terminal=False, attempt=None):
        self.store.finish(rule, state, f"Autoclose {rule['ticker']}: {reason}",
                          notify=self.access.allows_user(rule['user']), update_terminal=update_terminal,
                          attempt=attempt)
        self.closing_rules.discard(rule['id'])
        self.requests.set_close_priority(rule['id'], False)

    def set_close_priority(self, rule, enabled):
        self.requests.set_close_priority(rule['id'], enabled)
        if enabled:
            self.closing_rules.add(rule['id'])
        else:
            self.closing_rules.discard(rule['id'])
        if rule.get('close_priority', False) != enabled:
            rule['close_priority'] = enabled
            self.store.save(rule)

    def notify_attempt_issue(self, rule, attempt, kind, explanation):
        """Journal the notice with the attempt so retries/restarts cannot spam it."""
        if kind in attempt.get('notices', []) or not self.access.allows_user(rule['user']):
            return
        attempt.setdefault('notices', []).append(kind)
        self.store.notify(rule, f"Autoclose {rule['ticker']}: attempt {attempt['number']}/{MAX_CLOSE_ATTEMPTS}: "
                          + explanation,
                          key=f"autoclose-attempt:{rule['id']}:{attempt['number']}:{kind}", attempt=attempt)

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
        credentials = self.credentials.load(rule['user'])
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
        own_boundary = [f for f in boundary if f['coin'] == market.coin and int(f['time']) == stamp]
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
            if self.clock() * 1000 > attempt['expires'] + ORDER_CONFIRMATION_GRACE_MS:
                raise TradingError('Previous order outcome is unknown. No further order will be sent; check your account.')
            self.notify_attempt_issue(rule, attempt, 'unverified',
                'order execution is not yet verified by exchange status and fills. '
                'No new order will be sent until this outcome is known.')
            return False
        if persist and current != 0 and quantity < decimal(attempt['order']['s']):
            kind = 'rejected' if quantity == 0 else 'partial'
            self.notify_attempt_issue(rule, attempt, kind,
                f"the IOC order filled {quantity} of {attempt['order']['s']}. Remaining position: {abs(current)}. "
                'A retry requires a fresh position check and keeps the original 1% price limit.')
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
            self.activate(rule)
            return
        market = Market(**rule['market'])
        rule['check_stage'] = 'reading your trading account position'
        current, stamp = self.api.position(rule['address'], market)
        source_triggered = rule['state'] == 'closing'
        if rule['state'] == 'armed' and current * decimal(rule['original_size']) > 0:
            rule['check_stage'] = 'reading the tracked wallet position'
            source, _ = self.api.position(rule['wallet'], market)
            source_triggered = triggered(rule['source_size'], source)
        # Protect every remaining validation, quote, send and reconciliation
        # request as soon as the source trigger is observed, even before POST.
        self.set_close_priority(rule, source_triggered)
        rule['check_stage'] = 'checking your position fill history'
        fills = self.api.fills(rule['address'], rule['checkpoint'], stamp,
                               anchor=rule.get('history_anchor'))
        ended = replay(rule, current, stamp, fills)
        if 'pending' in rule:
            rule['check_stage'] = 'reconciling the previous order'
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
        if rule['attempts'] >= MAX_CLOSE_ATTEMPTS:
            self.finish(rule, 'failed', f"3 market-close attempts completed. Remaining position: {abs(current)}. "
                        'The price limit was not widened.')
            return
        if not source_triggered:
            return
        if self.clock() < max(rule.get('last_send', 0) + MIN_RETRY_INTERVAL, rule.get('next_attempt_at', 0)):
            return
        self.submit_close(rule, market, current)

    def activate(self, rule):
        if 'activation_deadline' not in rule:
            rule['activation_deadline'] = self.clock() + ACTIVATION_TIMEOUT
            self.store.save(rule)
        if self.clock() >= rule['activation_deadline']:
            self.finish(rule, 'failed', rule.get('activation_error', 'Activation did not complete.') +
                        ' Activation timed out; no rule was armed. Please retry /autoclose.')
            return
        if self.clock() < rule.get('activation_retry_at', 0):
            return
        self.arm(rule)

    def submit_close(self, rule, market, current):
        """Revalidate the position and commit the journal before the HTTP send."""
        credentials = self.credentials.load(rule['user'])
        if credentials is None:
            raise TradingError('Credentials are missing.')
        rule['check_stage'] = 'checking trading permissions'
        signer, vault = self.api.authority(credentials)
        if signer != rule['signer'] or vault != rule['vault']:
            raise TradingError('Trading authority changed. Rule cancelled.')
        if 'price' not in rule:
            rule['check_stage'] = 'reading the market quote'
            rule['price'] = market.price(self.api.mid(market), current < 0)
        # Refresh immediately before submitting, replaying any fills since the
        # cycle snapshot. External manual trading cannot be atomic with our POST.
        rule['check_stage'] = 'refreshing your position before submission'
        latest, stamp = self.api.position(rule['address'], market, fresh=True)
        rule['check_stage'] = 'verifying fills before submission'
        fills = self.api.fills(rule['address'], rule['checkpoint'], stamp, fresh=True,
                               anchor=rule.get('history_anchor'))
        if replay(rule, latest, stamp, fills) or latest * current <= 0:
            self.finish(rule, 'cancelled', 'your original position closed or reversed before order submission.')
            return
        checkpoint(rule, latest, stamp, fills)
        if self.stop.is_set() or not self.valid(rule) or self.store.cancelled(rule):
            return
        attempt = self.store.prepare(rule, signer, market.order(latest, rule['price']), self.clock())
        if attempt is None:
            return
        try:
            rule['check_stage'] = 'submitting the reduce-only order'
            result = self.api.send(credentials, vault, attempt)
        except DataFetchError as error:
            if error.submitted is False:
                # The local limiter rejected the request before any HTTP send.
                # This outcome is known; it must not fence the account forever
                # behind an order ID that the exchange has never received.
                attempt.update(resolved=True, not_submitted=True)
                rule.pop('pending')
                rule['next_attempt_at'] = self.clock() + max(MIN_RETRY_INTERVAL, error.retry_after)
                self.store.record(rule, attempt)
            # Actual transport failures remain pending and are reconciled first.
            raise
        finally:
            self.api.begin_cycle()  # Other rules must observe post-order positions.
        if result.get('status') != 'ok':
            attempt.update(rejected=True, resolved=True)
            rule.pop('pending')
            self.store.record(rule, attempt)
            raise TradingError('Exchange rejected the order; check API wallet permissions and account limits. '
                               'No further orders will be sent.')
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
        if attempt.get('rejected'):
            self.notify_attempt_issue(rule, attempt, 'rejected', attempt['error'] +
                ' The position is not confirmed closed. A retry requires a fresh position check; '
                'at most 3 attempts will be sent with the original 1% price limit.')
        elif 'response' in attempt and decimal(attempt['response']['totalSz']) < decimal(attempt['order']['s']):
            self.notify_attempt_issue(rule, attempt, 'partial',
                f"the exchange reports a partial fill: {attempt['response']['totalSz']} of {attempt['order']['s']}. "
                'Execution and the remaining position must be verified before retrying.')
        elif 'response' not in attempt:
            self.notify_attempt_issue(rule, attempt, 'unverified',
                'the exchange did not report a completed IOC execution. '
                'No new order will be sent until order status and fills establish the outcome.')

    def cycle(self):
        self.requests.set_trading_priority(True)
        active = self.store.active()
        self.closing_rules = {rule['id'] for rule in active
                              if (rule['state'] == 'closing' or rule.get('close_priority'))
                              and ('pending' in rule or not self.store.cancelled(rule))}
        self.requests.sync_close_priorities(self.closing_rules)
        self.requests.reserve_for_trading(bool(active))
        self.requests.begin_trading_cycle()
        self.schedule_next_check(active, delay=0)
        self.api.begin_cycle()
        if not self.closing_rules:
            self.reconcile_terminal_rules()
        for rule in sorted(self.store.active(), key=lambda item: item['id'] not in self.closing_rules):
            if self.stop.is_set():
                break
            if self.closing_rules and rule['id'] not in self.closing_rules:
                continue  # New registrations and idle checks yield to closing.
            self.process_rule(rule)
        active = self.store.active()
        self.requests.reserve_for_trading(bool(active))
        self.requests.finish_trading_cycle(
            {rule['id'] for rule in active}
            | {rule['id'] for rule, _ in self.store.unresolved()})
        self.schedule_next_check(active, delay=self.access.poll_interval)

    def schedule_next_check(self, rules, delay):
        # A waiting rule reads own/source positions and then a fill page. Keep
        # enough capacity for one complete pass, not just its largest request:
        # otherwise the next pass can spend 4 of the protected 120 units first.
        waiting = [rule for rule in rules if rule['state'] == 'armed' and not self.store.cancelled(rule)]
        weights = [2 * REQUEST_WEIGHTS['clearinghouseState'] + REQUEST_WEIGHTS['userFillsByTime']
                   + (0 if rule.get('history_anchor') else REQUEST_WEIGHTS['userFills'])
                   for rule in waiting]
        # Even a quiet cycle spends weight. Protect recurring checks throughout
        # the rolling window, using observed response weights when larger.
        # 25 = two snapshots (4) + a page retaining an anchor (21).
        # Without an anchor, two empty history responses cost at least 40 + 4.
        recurring = sum(25 if rule.get('history_anchor') else 44 for rule in waiting)
        peak = recurring + max((weight - (25 if rule.get('history_anchor') else 44)
                                for rule, weight in zip(waiting, weights)), default=0)
        self.requests.schedule_trading_check(peak, delay,
                                             interval=self.access.poll_interval, recurring=recurring)

    def reconcile_terminal_rules(self):
        # A failed rule can still have an in-flight request. Keep its account
        # fenced until a definitive status is observed, including after restart.
        for rule, active in self.store.unresolved():
            if active or self.stop.is_set():
                continue
            self.requests.set_trading_rule(rule['id'])
            try:
                status = self.api.status(rule['address'], rule['pending']['cloid'])
                if not self.terminal_status(rule, status):
                    continue
                attempt = rule['pending']
                try:
                    current, stamp = self.api.position(rule['address'], Market(**rule['market']), fresh=True)
                    fills = self.api.fills(rule['address'], rule['checkpoint'], stamp,
                                           anchor=rule.get('history_anchor'))
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
            except Exception as error:
                # Never retry orders during late reconciliation, but explain
                # failures instead of silently swallowing them.
                detail = error.diagnostic() if isinstance(error, DataFetchError) else 'exchange data could not be verified'
                # A failed transaction may have already mutated the in-memory
                # rule. Reload the committed journal before recording a notice.
                saved = next((item for item, _ in self.store.unresolved() if item['id'] == rule['id']), None)
                if saved is not None:
                    try:
                        kind = 'late-reconciliation-' + sha256(detail.encode()).hexdigest()[:12]
                        self.notify_attempt_issue(saved, saved['pending'], kind,
                            f'late order reconciliation failed: {detail}. No further orders will be sent; check your account.')
                    except Exception as notice_error:
                        logger.error('Autoclose notice could not be persisted (%s); reconciliation will retry',
                                     type(notice_error).__name__)
                continue

    def process_rule(self, rule):
        self.requests.set_trading_rule(rule['id'])
        try:
            self.step(rule)
        except DataFetchError as error:
            if rule['state'] == 'queued':
                self.activation_failed(rule, error)
            elif not error.retryable and 'pending' not in rule:
                self.finish(rule, 'failed',
                    f"closing failed while {rule.get('check_stage', 'checking exchange data')}: "
                    f'{error.diagnostic()}. This error is not retryable; no further orders will be sent.')
            elif 'pending' in rule and self.clock() * 1000 > rule['pending']['expires'] + ORDER_CONFIRMATION_GRACE_MS:
                self.finish(rule, 'failed', f"order outcome could not be verified while {rule.get('check_stage', 'checking exchange data')}: "
                            f'{error.diagnostic()}. No further orders will be sent; check your account.')
            else:
                self.check_failed(rule, error)
        except TradingError as error:
            self.finish(rule, 'failed', str(error))
        except Exception as error:
            logger.error('Autoclose rule %s failed (%s)', rule['id'], type(error).__name__)
            self.finish(rule, 'failed', f"closing failed while {rule.get('check_stage', 'checking account data')}: "
                        'account data or credentials could not be verified. '
                        'No further orders will be sent; check your account.')
        else:
            if rule['state'] != 'queued' and 'check_error_since' in rule:
                rule.pop('check_error_since')
                rule.pop('check_delay_notified', None)
                rule['last_check_recovered_at'] = self.clock()
                self.store.save(rule)
                logger.info('Autoclose rule %s: checks resumed', rule['id'])

    def check_failed(self, rule, error):
        now = self.clock()
        stage = rule.get('check_stage', 'checking account data')
        detail = error.diagnostic()
        previous = rule.get('last_check_error', {})
        closing = bool(rule.get('close_priority') or rule['state'] == 'closing' or 'pending' in rule)
        changed = (previous.get('stage'), previous.get('detail'), previous.get('closing', False)) != (stage, detail, closing)
        if changed or now >= rule.get('check_error_logged_at', 0) + 60:
            logger.warning('Autoclose rule %s delayed while %s: %s', rule['id'], stage, detail)
            rule['check_error_logged_at'] = now
        rule['last_check_error'] = dict(at=now, stage=stage, detail=detail, closing=closing)
        rule.setdefault('check_error_since', now)
        notify = changed or not rule.get('check_delay_notified')
        if error.reason == 'budget':
            notify = not self.repeated_budget_warning(rule, stage, detail, closing, now)
        if notify and self.access.allows_user(rule['user']):
            rule['check_delay_notified'] = True
            rule['check_error_serial'] = rule.get('check_error_serial', 0) + 1
            outcome = ('The submitted order outcome is unknown; no new order will be sent until it is verified.'
                       if 'pending' in rule else 'Automatic retries continue. Position closure is not confirmed.')
            if not closing:
                outcome = 'The rule remains active. No close order has been submitted. Automatic retries continue.'
            self.store.notify(rule, f"Autoclose {rule['ticker']}: checks are delayed while {stage}: {detail}. "
                              + outcome,
                              key=f"autoclose-delay:{rule['id']}:{rule['check_error_serial']}")
        else:
            self.store.save(rule)

    @staticmethod
    def repeated_budget_warning(rule, stage, detail, closing, now):
        """Keep a quiet window per failure context, not just the last stage.

        Fresh checks may fail alternately at the early and final history read.
        Neither switching stages nor an individual successful check ends that
        shortage. A trigger, a new attempt or an unknown order outcome does
        need a separate immediate warning.
        """
        context = (stage, detail, closing, rule['attempts'], rule.get('pending', {}).get('cloid'))
        key = sha256(repr(context).encode()).hexdigest()
        notices = {key: until for key, until in rule.get('budget_notices', {}).items() if now < until}
        quiet_until = notices.get(key, 0)
        # Rules saved before this change have a single waiting-stage marker.
        legacy = rule.get('budget_notice', {})
        if not closing and (legacy.get('stage'), legacy.get('detail')) == (stage, detail):
            quiet_until = max(quiet_until, legacy.get('quiet_until', 0))
        notices[key] = now + WINDOW_SECONDS
        rule['budget_notices'] = notices
        return now < quiet_until

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
        deadline = rule.setdefault('activation_deadline', self.clock() + ACTIVATION_TIMEOUT)
        delay = max(self.access.poll_interval * 2 ** (failures - 1), error.retry_after)
        rule['activation_retry_at'] = min(deadline, self.clock() + delay)
        if self.access.allows_user(rule['user']):
            self.store.notify(rule,
                f"Autoclose {rule['ticker']}: {reason} Retrying automatically; the rule is not armed yet.",
                key=f"autoclose-activation:{rule['id']}:{failures}")
        else:
            self.store.save(rule)

    def run(self, heartbeat):
        self.requests.set_heartbeat(heartbeat)
        try:
            while not self.stop.is_set():
                heartbeat()
                self.cycle()
                self.stop.wait(self.access.poll_interval)
        except Exception:
            try:
                for rule in self.store.active():
                    if rule['id'] in self.closing_rules:
                        self.check_failed(rule, DataFetchError('Autoclose worker interrupted', reason='worker_failure'))
            except Exception as notice_error:
                logger.error('Autoclose interruption notice could not be persisted (%s)', type(notice_error).__name__)
            raise
        finally:
            # A supervised restart must not let monitoring spend the budget
            # while a known close is still pending. The new cycle restores it
            # from SQLite; only application shutdown releases it unconditionally.
            if self.stop.is_set():
                self.requests.reserve_for_trading(False)
            self.requests.set_trading_priority(False)
            self.api.close()
