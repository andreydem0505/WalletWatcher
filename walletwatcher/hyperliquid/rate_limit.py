"""Shared IP request accounting, independent of HTTP sessions and API parsing."""
from collections import deque
from email.utils import parsedate_to_datetime
import math
import threading
import time

from walletwatcher.hyperliquid.errors import DataFetchError

WINDOW_SECONDS = 60
FILL_REQUESTS = frozenset(('userFills', 'userFillsByTime'))
REQUEST_WEIGHTS = {
    'clearinghouseState': 2,
    'spotClearinghouseState': 2,
    'allMids': 2,
    'orderStatus': 2,
    'userFills': 120,
    'userFillsByTime': 120,
    'userRole': 60,
    'exchange': 1,
}


def retry_delay(value):
    """Parse either Retry-After seconds or an HTTP date, with a safe fallback."""
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, AttributeError, OverflowError):
            return 30
    return max(1, delay) if math.isfinite(delay) else 30


class RequestBudget:
    """Atomically admit requests and protect only pending trading demand.

    Reservations remain mutable so response-dependent fill weights can be
    corrected while concurrent HTTP requests share the same rolling window.
    """

    def __init__(self, limit=1000, clock=time.monotonic):
        self.limit = limit
        self.clock = clock
        self.lock = threading.Lock()
        self.worker = threading.local()
        self.calls = deque()
        self.trading_waiters = {}
        self.closing_rules = set()
        self.blocked_until = 0.0

    def set_close_priority(self, rule_id, enabled):
        with self.lock:
            if enabled:
                self.closing_rules.add(rule_id)
            else:
                self.closing_rules.discard(rule_id)

    def sync_close_priorities(self, rule_ids):
        with self.lock:
            self.closing_rules = set(rule_ids)

    def set_trading_priority(self, enabled):
        self.worker.trading = bool(enabled)
        if not enabled:
            with self.lock:
                self.trading_waiters.pop(threading.current_thread(), None)

    def set_trading_rule(self, rule_id):
        self.worker.trading_rule = rule_id

    def finish_trading_cycle(self, active_rules):
        # Retry backoff must not release a rule's priority; cancellation must.
        with self.lock:
            owner = threading.current_thread()
            pending = self.trading_waiters.get(owner, {})
            for key in list(pending):
                if key[0] not in active_rules:
                    del pending[key]
            if not pending:
                self.trading_waiters.pop(owner, None)
            self.closing_rules.intersection_update(active_rules)
        self.worker.trading_rule = None

    def reserve_for_trading(self, enabled):
        # Active rules alone do not withhold capacity from monitoring.
        if not enabled:
            with self.lock:
                self.trading_waiters.clear()
                self.closing_rules.clear()

    def reserve(self, operation):
        with self.lock:
            if self.closing_rules and (not getattr(self.worker, 'trading', False)
                                       or getattr(self.worker, 'trading_rule', None) not in self.closing_rules):
                raise DataFetchError('Automatic position closing has priority; ordinary requests are paused',
                                     operation=operation, reason='trading_priority',
                                     retry_after=1, submitted=False)
            if self.clock() < self.blocked_until:
                raise DataFetchError('Hyperliquid rate-limit cooldown is active',
                                     operation=operation, reason='rate_limit',
                                     retry_after=max(1, self.blocked_until - self.clock()),
                                     submitted=False)
            return self._reserve_weight(operation)

    def _reserve_weight(self, operation):
        now = self.clock()
        while self.calls and self.calls[0][0] <= now - WINDOW_SECONDS:
            self.calls.popleft()
        # Fill requests reserve the 2000-item upper bound before sending.
        weight = REQUEST_WEIGHTS.get(operation, 20)
        priority = getattr(self.worker, 'trading', False)
        total = sum(call[1] for call in self.calls)
        if priority:
            self._update_trading_demand(operation, weight, total)
        # One worker makes sequential requests, so protect its largest pending
        # operation rather than summing all the stages it may retry.
        reserve = 0 if priority else sum(max(pending.values()) for pending in self.trading_waiters.values())
        if total + weight + reserve > self.limit:
            raise DataFetchError('Hyperliquid request weight budget exhausted; retrying later',
                                 operation=operation, reason='budget',
                                 retry_after=self._retry_after(now, total, weight + reserve),
                                 submitted=False)
        reservation = [now, weight, priority]
        self.calls.append(reservation)
        return reservation

    def _update_trading_demand(self, operation, weight, total):
        owner = threading.current_thread()
        key = (getattr(self.worker, 'trading_rule', None), operation)
        if total + weight > self.limit and weight <= self.limit:
            self.trading_waiters.setdefault(owner, {})[key] = weight
        else:
            pending = self.trading_waiters.get(owner, {})
            pending.pop(key, None)
            if not pending:
                self.trading_waiters.pop(owner, None)

    def _retry_after(self, now, total, required):
        retry_at = now + WINDOW_SECONDS
        for stamp, used, _ in self.calls:
            total -= used
            if total + required <= self.limit:
                retry_at = stamp + WINDOW_SECONDS
                break
        return max(1, retry_at - now)

    def record_response(self, reservation, operation, data):
        if operation in FILL_REQUESTS and isinstance(data, list):
            with self.lock:
                reservation[1] = 20 + math.ceil(len(data) / 20)

    def defer(self, delay):
        """Only the exchange's actual HTTP 429 establishes a global cooldown."""
        with self.lock:
            self.blocked_until = max(self.blocked_until, self.clock() + delay)

    def cooldown_remaining(self):
        with self.lock:
            return max(0, self.blocked_until - self.clock())
