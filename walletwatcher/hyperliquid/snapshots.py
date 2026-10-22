"""Short-lived WebSocket snapshots, with an independent connection budget."""
from collections import deque
import json
import threading
import time

import websocket

from walletwatcher.hyperliquid.errors import DataFetchError
from walletwatcher.domain.validation import label

WS_URL = 'wss://api.hyperliquid.xyz/ws'


class DexSnapshotReader:
    def __init__(self, clock=time.monotonic, connection_factory=websocket.WebSocket):
        self.clock = clock
        self.connection_factory = connection_factory
        self.lock = threading.Lock()
        self.connections = deque()

    def fetch(self, wallet, heartbeat):
        # A short-lived subscription belongs only to this /my invocation.
        # Its lock/budget are independent of the monitor's HTTP requests.
        with self.lock:
            heartbeat()
            now = self.clock()
            while self.connections and self.connections[0] <= now - 60:
                self.connections.popleft()
            if len(self.connections) >= 30:
                raise DataFetchError('Hyperliquid websocket connection budget exhausted')
            self.connections.append(now)
            connection = self.connection_factory()
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
                    return parse_snapshot(message, wallet)
            except (websocket.WebSocketException, OSError, ValueError, KeyError,
                    TypeError, AttributeError) as error:
                raise DataFetchError('Hyperliquid websocket snapshot failed') from error
            finally:
                try:
                    connection.close(timeout=1)
                except (websocket.WebSocketException, OSError):
                    pass
                heartbeat()


def parse_snapshot(message, wallet):
    """Validate a complete account snapshot before exposing any DEX state."""
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
