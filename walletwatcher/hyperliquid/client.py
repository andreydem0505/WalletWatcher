"""Bounded requests; failures are isolated to individual wallets by the monitor."""
import time
import threading
from typing import Optional

import requests
import websocket

from walletwatcher.presentation.formatting import format_position, format_trade
from walletwatcher.domain.models import Position, Trade
from walletwatcher.hyperliquid.errors import DataFetchError
from walletwatcher.hyperliquid.rate_limit import RequestBudget, retry_delay
from walletwatcher.hyperliquid.portfolio import fetch_portfolio
from walletwatcher.hyperliquid.snapshots import DexSnapshotReader, WS_URL

API_URL = 'https://api.hyperliquid.xyz/info'


class HyperliquidClient:
    def __init__(self, session=None, clock=time.monotonic, perp_dexs=('',), weight_budget=1000,
                 personal_session_factory=None, budget=None):
        self.session = session or requests.Session()
        self.clock = clock
        self.perp_dexs = tuple(dict.fromkeys(perp_dexs))
        if not self.perp_dexs:
            raise ValueError('At least one perpetual dex is required')
        self.budget = budget if budget is not None else RequestBudget(weight_budget, clock)
        self.heartbeat = lambda: None
        self.worker = threading.local()
        self.request_lock = threading.Lock()
        self.personal_session_factory = personal_session_factory or requests.Session
        self.snapshots = DexSnapshotReader(clock, connection_factory=lambda: websocket.WebSocket())

    def set_heartbeat(self, callback):
        self.worker.heartbeat = callback

    # Keep the existing client facade while accounting has its own component.
    @property
    def calls(self):
        return self.budget.calls

    @property
    def trading_waiters(self):
        return self.budget.trading_waiters

    @property
    def blocked_until(self):
        return self.budget.blocked_until

    def set_trading_priority(self, enabled):
        self.budget.set_trading_priority(enabled)

    def set_trading_rule(self, rule_id):
        self.budget.set_trading_rule(rule_id)

    def finish_trading_cycle(self, active_rules):
        self.budget.finish_trading_cycle(active_rules)

    def reserve_for_trading(self, enabled):
        self.budget.reserve_for_trading(enabled)

    def set_close_priority(self, rule_id, enabled):
        self.budget.set_close_priority(rule_id, enabled)

    def sync_close_priorities(self, rule_ids):
        self.budget.sync_close_priorities(rule_ids)

    def begin_trading_cycle(self):
        self.budget.begin_trading_cycle()

    def schedule_trading_check(self, weight, delay, **forecast):
        self.budget.schedule_trading_check(weight, delay, **forecast)

    retry_delay = staticmethod(retry_delay)

    def _reserve_weight(self, request_type):
        return self.budget.reserve(request_type)

    def _request(self, payload, session=None):
        # Personal requests have their own sessions; only IP accounting is shared.
        if session is not None:
            return self._perform_request(payload, session)
        with self.request_lock:
            return self._perform_request(payload)

    def _perform_request(self, payload, session=None, *, exchange=False):
        heartbeat = getattr(self.worker, 'heartbeat', self.heartbeat)
        heartbeat()
        operation = 'exchange' if exchange else payload['type']
        reservation = self.budget.reserve(operation)
        response = None
        try:
            response = (session if session is not None else self.session).post(
                API_URL.replace('/info', '/exchange') if exchange else API_URL,
                json=payload, timeout=(5, 15))
            if response.status_code == 429:
                delay = self.retry_delay(response.headers.get('Retry-After', 30))
                self.budget.defer(delay)
            response.raise_for_status()
            data = response.json()
            self.budget.record_response(reservation, operation, data)
            return data
        except (requests.RequestException, ValueError) as error:
            status = getattr(response, 'status_code', None)
            status = status if type(status) is int else None
            if isinstance(error, requests.Timeout):
                reason = 'timeout'
            elif isinstance(error, requests.HTTPError):
                reason = 'rate_limit' if status == 429 else 'http'
            elif isinstance(error, ValueError):
                reason = 'invalid_response'
            else:
                reason = 'network'
            raise DataFetchError(f'Hyperliquid request failed ({type(error).__name__})',
                                 operation=operation, reason=reason, status_code=status,
                                 retryable=reason in ('timeout', 'network', 'rate_limit')
                                 or (reason == 'http' and (status is None or status == 408 or status >= 500)),
                                 retry_after=self.budget.cooldown_remaining()) from error
        finally:
            heartbeat()

    def fetch_open_positions(self, wallet: str) -> list[Position]:
        result = []
        for dex in self.perp_dexs:
            data = self._request({'type': 'clearinghouseState', 'user': wallet, 'dex': dex})
            try:
                positions = data['assetPositions']
                if not isinstance(positions, list):
                    raise ValueError('assetPositions must be a list')
                for value in positions:
                    position = format_position(value)
                    if position.size != 0:
                        result.append(position)
            except (KeyError, TypeError, ValueError, OverflowError) as error:
                raise DataFetchError(f'Invalid clearinghouseState response for dex {dex or "main"}') from error
        # Publish only a complete multi-dex snapshot. A failed request must not
        # make another market's positions look closed.
        return sorted(result, key=lambda pos: pos.ticker)

    def fetch_last_trade(self, wallet: str, coins=None) -> Optional[Trade]:
        data = self._request({'type': 'userFills', 'user': wallet, 'aggregateByTime': True})
        try:
            if not isinstance(data, list):
                raise ValueError('userFills must be a list')
            candidates = []
            for fill in data:
                coin = fill['coin']
                if not isinstance(coin, str):
                    raise ValueError('Invalid fill coin')
                dex = coin.split(':', 1)[0] if ':' in coin else ''
                if coin.startswith(('@', '#')) or '/' in coin or fill.get('dir') in ('Buy', 'Sell'):
                    continue
                if dex in self.perp_dexs and (coins is None or coin in coins):
                    candidates.append(fill)
            return format_trade(max(candidates, key=lambda fill: int(fill['time']))) if candidates else None
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise DataFetchError('Invalid userFills response') from error

    def close(self):
        self.session.close()

    def _fetch_dex_states(self, wallet):
        heartbeat = getattr(self.worker, 'heartbeat', self.heartbeat)
        return self.snapshots.fetch(wallet, heartbeat)

    def fetch_portfolio(self, wallet):
        return fetch_portfolio(
            wallet, send_request=self._request, read_snapshot=self._fetch_dex_states,
            session_factory=self.personal_session_factory, bind_heartbeat=self.set_heartbeat,
            heartbeat=getattr(self.worker, 'heartbeat', self.heartbeat))
