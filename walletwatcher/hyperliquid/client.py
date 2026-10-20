"""Bounded requests; failures are isolated to individual wallets by the monitor."""
import time
import math
import threading
import json
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from typing import Optional

import requests
import websocket

from walletwatcher.presentation.formatting import format_position, format_trade, label, number
from walletwatcher.domain.models import Position, Trade
from walletwatcher.presentation.portfolio import Portfolio, parse_positions

API_URL = 'https://api.hyperliquid.xyz/info'
WS_URL = 'wss://api.hyperliquid.xyz/ws'


class DataFetchError(Exception):
    def __init__(self, message, *, operation=None, reason='network', status_code=None,
                 retry_after=0, retryable=True):
        super().__init__(message)
        self.operation = operation
        self.reason = reason
        self.status_code = status_code
        self.retry_after = retry_after
        self.retryable = retryable

    def diagnostic(self):
        # Never include response bodies, request payloads, URLs or exception text.
        reasons = {'budget': 'the bot request budget is busy',
                   'rate_limit': 'Hyperliquid rate limiting is active',
                   'timeout': 'the Hyperliquid request timed out',
                   'network': 'a network connection failed',
                   'http': 'Hyperliquid rejected the data request',
                   'invalid_response': 'Hyperliquid returned invalid JSON'}
        result = reasons.get(self.reason, 'the data request failed')
        if type(self.status_code) is int and 100 <= self.status_code <= 599:
            result += f' (HTTP {self.status_code})'
        if self.operation in ('userRole', 'meta', 'perpDexs', 'clearinghouseState',
                              'userFills', 'userFillsByTime', 'allMids', 'orderStatus', 'exchange'):
            result += f' [request: {self.operation}]'
        return result


class HyperliquidClient:
    def __init__(self, session=None, clock=time.monotonic, perp_dexs=('',), weight_budget=1000,
                 personal_session_factory=None):
        self.session = session or requests.Session()
        self.clock = clock
        self.blocked_until = 0.0
        self.blocked_reason = 'rate_limit'
        self.perp_dexs = tuple(dict.fromkeys(perp_dexs))
        if not self.perp_dexs:
            raise ValueError('At least one perpetual dex is required')
        self.weight_budget = weight_budget
        self.calls = deque()
        self.heartbeat = lambda: None
        self.worker = threading.local()
        self.request_lock = threading.Lock()
        self.budget_lock = threading.Lock()
        self.personal_session_factory = personal_session_factory or requests.Session
        self.snapshot_lock = threading.Lock()
        self.ws_connections = deque()

    def set_heartbeat(self, callback):
        self.worker.heartbeat = callback

    @staticmethod
    def retry_delay(value):
        try:
            delay = float(value)
        except (ValueError, TypeError):
            try:
                delay = parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, AttributeError, OverflowError):
                return 30
        return max(1, delay) if math.isfinite(delay) else 30

    def _reserve_weight(self, request_type):
        now = self.clock()
        while self.calls and self.calls[0][0] <= now - 60:
            self.calls.popleft()
        # userFills: base 20 + up to 2000/20 response items. Reserve the
        # documented upper bound, then adjust to the actual returned count.
        weight = (2 if request_type in ('clearinghouseState', 'spotClearinghouseState', 'allMids', 'orderStatus') else
                  120 if request_type in ('userFills', 'userFillsByTime') else
                  60 if request_type == 'userRole' else 1 if request_type == 'exchange' else 20)
        if sum(call[1] for call in self.calls) + weight > self.weight_budget:
            if self.calls:
                self.blocked_until = max(self.blocked_until, self.calls[0][0] + 60)
                self.blocked_reason = 'budget'
            raise DataFetchError('Hyperliquid request weight budget exhausted; retrying later',
                                 operation=request_type, reason='budget',
                                 retry_after=max(1, self.blocked_until - now))
        reservation = [now, weight]
        self.calls.append(reservation)
        return reservation

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
        with self.budget_lock:
            if self.clock() < self.blocked_until:
                raise DataFetchError('Hyperliquid rate-limit cooldown is active', operation=operation,
                                     reason=self.blocked_reason,
                                     retry_after=max(1, self.blocked_until - self.clock()))
            reservation = self._reserve_weight(operation)
        response = None
        try:
            response = (session if session is not None else self.session).post(
                API_URL.replace('/info', '/exchange') if exchange else API_URL,
                json=payload, timeout=(5, 15))
            if response.status_code == 429:
                delay = self.retry_delay(response.headers.get('Retry-After', 30))
                with self.budget_lock:
                    self.blocked_until = max(self.blocked_until, self.clock() + delay)
                    self.blocked_reason = 'rate_limit'
            response.raise_for_status()
            data = response.json()
            if not exchange and payload['type'] in ('userFills', 'userFillsByTime') and isinstance(data, list):
                with self.budget_lock:
                    reservation[1] = 20 + math.ceil(len(data) / 20)
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
                                 retry_after=max(0, self.blocked_until - self.clock())) from error
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
        # A short-lived subscription belongs only to this /my invocation.
        # Its lock/budget are independent of the monitor's HTTP requests.
        with self.snapshot_lock:
            heartbeat = getattr(self.worker, 'heartbeat', self.heartbeat)
            heartbeat()
            now = self.clock()
            while self.ws_connections and self.ws_connections[0] <= now - 60:
                self.ws_connections.popleft()
            if len(self.ws_connections) >= 30:
                raise DataFetchError('Hyperliquid websocket connection budget exhausted')
            self.ws_connections.append(now)
            connection = websocket.WebSocket()
            deadline = now + 20
            try:
                connection.connect(WS_URL, timeout=5)
                connection.send(json.dumps({'method': 'subscribe', 'subscription': {
                    'type': 'allDexsClearinghouseState', 'user': wallet}}))
                while True:
                    heartbeat()
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        raise DataFetchError('Hyperliquid websocket snapshot timed out')
                    connection.settimeout(min(15, remaining))
                    raw = connection.recv()
                    if not raw:
                        raise DataFetchError('Hyperliquid websocket closed before snapshot')
                    message = json.loads(raw)
                    if message['channel'] in ('subscriptionResponse', 'pong'):
                        continue
                    if message['channel'] != 'allDexsClearinghouseState':
                        raise ValueError('Unexpected websocket channel')
                    data = message['data']
                    if data['user'].lower() != wallet.lower():
                        raise ValueError('Unexpected snapshot user')
                    states = data['clearinghouseStates']
                    if not isinstance(states, list):
                        raise ValueError('Invalid DEX states')
                    result, seen = [], set()
                    for pair in states:
                        if not isinstance(pair, list) or len(pair) != 2:
                            raise ValueError('Invalid DEX state pair')
                        dex, state = pair
                        if dex != '':
                            dex = label(dex, 32)
                        if dex in seen or not isinstance(state, dict):
                            raise ValueError('Duplicate DEX or invalid state')
                        seen.add(dex)
                        result.append((dex, state))
                    if '' not in seen:
                        raise ValueError('Missing main DEX state')
                    return result
            except (websocket.WebSocketException, OSError, ValueError, KeyError,
                    TypeError, AttributeError) as error:
                raise DataFetchError('Hyperliquid websocket snapshot failed') from error
            finally:
                try:
                    connection.close(timeout=1)
                except (websocket.WebSocketException, OSError):
                    pass
                heartbeat()

    def fetch_portfolio(self, wallet):
        heartbeat = getattr(self.worker, 'heartbeat', self.heartbeat)
        sessions, sessions_lock, local = [], threading.Lock(), threading.local()

        def request(payload):
            self.set_heartbeat(heartbeat)
            if not hasattr(local, 'session'):
                local.session = self.personal_session_factory()
                with sessions_lock:
                    sessions.append(local.session)
            return self._request(payload, session=local.session)

        def snapshot():
            self.set_heartbeat(heartbeat)
            return self._fetch_dex_states(wallet)

        # This pool and its HTTP sessions exist only for the current /my job.
        pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='personal-api')
        try:
            states_future = pool.submit(snapshot)
            mode_future = pool.submit(request, {'type': 'userAbstraction', 'user': wallet})
            states = states_future.result()
            mode = mode_future.result()
            if mode not in ('disabled', 'default', 'dexAbstraction', 'unifiedAccount', 'portfolioMargin'):
                raise ValueError('Invalid abstraction mode')
            spot_future = (pool.submit(request, {'type': 'spotClearinghouseState', 'user': wallet})
                           if mode in ('unifiedAccount', 'portfolioMargin') else None)
            balance, positions = 0.0, []
            occupied = []
            for dex, state in states:
                if mode not in ('unifiedAccount', 'portfolioMargin'):
                    balance += number(state['marginSummary']['accountValue'])
                raw = state['assetPositions']
                if not isinstance(raw, list):
                    raise ValueError('Invalid asset positions')
                # Avoid extra market/order requests for empty accounts.
                if not any(number(item['position']['szi']) != 0 for item in raw):
                    continue
                occupied.append((dex, state))
            pending = [(dex, state,
                        pool.submit(request, {'type': 'metaAndAssetCtxs', 'dex': dex}),
                        pool.submit(request, {'type': 'frontendOpenOrders', 'user': wallet, 'dex': dex}))
                       for dex, state in occupied]
            for dex, state, context_future, orders_future in pending:
                context = context_future.result()
                universe, contexts = context[0]['universe'], context[1]
                prices = {}
                for asset, market in zip(universe, contexts, strict=True):
                    coin = label(asset['name'])
                    if dex and ':' not in coin:
                        coin = dex + ':' + coin
                    prices[coin] = market['markPx']
                orders = orders_future.result()
                positions.extend(parse_positions(state, prices, orders))
            if spot_future is not None:
                spot = spot_future.result()
                if not isinstance(spot['balances'], list):
                    raise ValueError('Invalid balances')
                # Unified balances must not be added once per DEX. Report USDC
                # collateral itself, not a fictitious USD sum of different tokens.
                balance = sum(number(item['total']) for item in spot['balances'] if item['coin'] == 'USDC')
            return Portfolio(number(balance), positions)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError, OverflowError) as error:
            raise DataFetchError('Invalid personal account response') from error
        finally:
            # No outstanding requests or pooled connections survive this command.
            pool.shutdown(wait=True, cancel_futures=True)
            for session in sessions:
                try:
                    session.close()
                except (requests.RequestException, OSError):
                    pass
            heartbeat()
