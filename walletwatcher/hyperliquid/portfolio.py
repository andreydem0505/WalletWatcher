"""On-demand portfolio reads; sessions and concurrency belong to one invocation."""
from concurrent.futures import ThreadPoolExecutor
import threading

import requests

from walletwatcher.hyperliquid.errors import DataFetchError
from walletwatcher.presentation.formatting import label, number
from walletwatcher.presentation.portfolio import Portfolio, parse_positions


ABSTRACTION_MODES = ('disabled', 'default', 'dexAbstraction', 'unifiedAccount', 'portfolioMargin')
UNIFIED_MODES = ('unifiedAccount', 'portfolioMargin')


def market_prices(context, dex):
    universe, contexts = context[0]['universe'], context[1]
    prices = {}
    for asset, market in zip(universe, contexts, strict=True):
        coin = label(asset['name'])
        if dex and ':' not in coin:
            coin = dex + ':' + coin
        prices[coin] = market['markPx']
    return prices


def fetch_portfolio(wallet, *, send_request, read_snapshot, session_factory, bind_heartbeat, heartbeat):
    sessions = []
    sessions_lock = threading.Lock()
    local = threading.local()

    def request(payload):
        bind_heartbeat(heartbeat)
        if not hasattr(local, 'session'):
            local.session = session_factory()
            with sessions_lock:
                sessions.append(local.session)
        return send_request(payload, session=local.session)

    def snapshot():
        bind_heartbeat(heartbeat)
        return read_snapshot(wallet)

    # This pool and its HTTP sessions exist only for the current /my job.
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='personal-api')
    try:
        states_future = pool.submit(snapshot)
        mode_future = pool.submit(request, {'type': 'userAbstraction', 'user': wallet})
        states = states_future.result()
        mode = mode_future.result()
        if mode not in ABSTRACTION_MODES:
            raise ValueError('Invalid abstraction mode')
        spot_future = (pool.submit(request, {'type': 'spotClearinghouseState', 'user': wallet})
                       if mode in UNIFIED_MODES else None)
        balance, positions = 0.0, []
        occupied = []
        for dex, state in states:
            if mode not in UNIFIED_MODES:
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
            prices = market_prices(context, dex)
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
