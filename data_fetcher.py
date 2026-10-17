"""Bounded requests; failures are isolated to individual wallets by the monitor."""
import time
import math
from collections import deque
from email.utils import parsedate_to_datetime
from typing import Optional

import requests

from format import format_position, format_trade
from models import Position, Trade

API_URL = 'https://api.hyperliquid.xyz/info'


class DataFetchError(Exception):
    pass


class HyperliquidClient:
    def __init__(self, session=None, clock=time.monotonic, perp_dexs=('',), weight_budget=1000):
        self.session = session or requests.Session()
        self.clock = clock
        self.blocked_until = 0.0
        self.perp_dexs = tuple(dict.fromkeys(perp_dexs))
        if not self.perp_dexs:
            raise ValueError('At least one perpetual dex is required')
        self.weight_budget = weight_budget
        self.calls = deque()
        self.heartbeat = lambda: None

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
        weight = 2 if request_type == 'clearinghouseState' else 120
        if sum(call[1] for call in self.calls) + weight > self.weight_budget:
            if self.calls:
                self.blocked_until = max(self.blocked_until, self.calls[0][0] + 60)
            raise DataFetchError('Hyperliquid request weight budget exhausted; retrying later')
        reservation = [now, weight]
        self.calls.append(reservation)
        return reservation

    def _request(self, payload):
        self.heartbeat()
        if self.clock() < self.blocked_until:
            raise DataFetchError('Hyperliquid rate-limit cooldown is active')
        reservation = self._reserve_weight(payload['type'])
        try:
            response = self.session.post(API_URL, json=payload, timeout=(5, 15))
            if response.status_code == 429:
                delay = self.retry_delay(response.headers.get('Retry-After', 30))
                self.blocked_until = self.clock() + delay
            response.raise_for_status()
            data = response.json()
            if payload['type'] == 'userFills' and isinstance(data, list):
                reservation[1] = 20 + math.ceil(len(data) / 20)
            return data
        except (requests.RequestException, ValueError) as error:
            raise DataFetchError(f'Hyperliquid request failed ({type(error).__name__})') from error
        finally:
            self.heartbeat()

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
