"""Decimal-only perpetual orders; SDK is used only for offline signing."""

from eth_account import Account
from hyperliquid.utils.signing import sign_l1_action

# Preserve imports used by integrations while policy is independent of the SDK.
from walletwatcher.trading.policy import (
    FillHistory, Market, TradingError, checkpoint, decimal, replay, ticker_name, triggered,
)


class TradingAPI:
    def __init__(self, client):
        self.client = client
        self.session = None
        self.cache = {}

    def close(self):
        if self.session is not None:
            self.session.close()
            self.session = None

    def begin_cycle(self):
        self.cache.clear()

    def request(self, payload):
        if self.session is None:
            self.session = self.client.personal_session_factory()
        return self.client._request(payload, session=self.session)

    def market(self, ticker):
        dex = ticker.split(':')[0] if ':' in ticker else ''
        meta = self.request({'type': 'meta', 'dex': dex})
        for index, asset in enumerate(meta['universe']):
            coin = asset['name']
            if dex and ':' not in coin:
                coin = dex + ':' + coin
            if coin.lower() == ticker.lower():
                offset = 0
                if dex:
                    dexes = self.request({'type': 'perpDexs'})
                    indices = [i for i, item in enumerate(dexes) if item and item['name'] == dex]
                    if not indices:
                        raise TradingError('Unknown perpetual DEX.')
                    offset = 100000 + indices[0] * 10000
                precision = int(asset['szDecimals'])
                if not 0 <= precision <= 6 or asset.get('isDelisted'):
                    raise TradingError('This perpetual market is unavailable.')
                return Market(coin, dex, offset + index, precision)
        raise TradingError('Unknown perpetual ticker. HIP-3 tickers must include the DEX prefix.')

    def position(self, address, market, fresh=False):
        key = (address, market.dex)
        if fresh or key not in self.cache:
            state = self.request({'type': 'clearinghouseState', 'user': address, 'dex': market.dex})
            positions = {}
            for item in state['assetPositions']:
                coin = item['position']['coin']
                if market.dex and ':' not in coin:
                    coin = market.dex + ':' + coin
                positions[coin] = str(decimal(item['position']['szi']))
            timestamp = int(state['time'])
            if timestamp <= 0:
                raise TradingError('Missing position snapshot timestamp.')
            self.cache[key] = (positions, timestamp)
        positions, timestamp = self.cache[key]
        return decimal(positions.get(market.coin, '0')), timestamp

    def authority(self, credentials):
        signer = Account.from_key(credentials.private_key).address.lower()
        target = credentials.address.lower()
        target_role = self.request({'type': 'userRole', 'user': target})
        role = target_role.get('role')
        if role not in ('user', 'subAccount'):
            raise TradingError('Trading account must be a user or subaccount; vaults are unsupported.')
        master = target if role == 'user' else target_role['data']['master'].lower()
        signer_role = self.request({'type': 'userRole', 'user': signer})
        owner = signer_role.get('data', {}).get('user', '').lower()
        if not ((signer_role.get('role') == 'agent' and owner == master) or signer == master):
            raise TradingError('The saved key is not authorized to trade this account. Check your API wallet.')
        return signer, target if role == 'subAccount' else None

    def fills(self, address, start, end, fresh=False, anchor=None):
        anchor_id = (anchor['time'], anchor['coin'], str(anchor['tid'])) if anchor else None
        cache_key = ('fills', address, start, end, anchor_id)
        if not fresh and cache_key in self.cache:
            return self.cache[cache_key]
        if anchor is None:
            # Bootstrap old/new rules once. Subsequent checks request only the
            # interval since a retained fill, whose presence proves continuity.
            recent = self.request({'type': 'userFills', 'user': address, 'aggregateByTime': False})
            if not isinstance(recent, list):
                raise TradingError('Invalid fill history.')
            if len(recent) >= 2000 and min(int(f['time']) for f in recent) > start:
                raise TradingError('Fill history continuity cannot be verified. Please arm a new rule.')
            before = [f for f in recent if int(f['time']) <= start]
            if before:
                last = max(before, key=lambda f: (int(f['time']), int(f['tid'])))
                anchor = dict(time=int(last['time']), coin=last['coin'], tid=str(last['tid']))
        if anchor is not None and int(anchor['time']) > start:
            raise TradingError('Fill history anchor is newer than the position checkpoint.')
        result, cursor = {}, int(anchor['time']) if anchor else start
        for _ in range(25):
            page = self.request(dict(type='userFillsByTime', user=address, startTime=cursor,
                                     endTime=end, aggregateByTime=False))
            if not isinstance(page, list):
                raise TradingError('Invalid fill history.')
            for fill in page:
                stamp = int(fill['time'])
                if not cursor <= stamp <= end:
                    raise TradingError('Fill history is outside the requested interval.')
                identity = (str(fill['tid']), fill['coin'])
                if identity in result and result[identity] != fill:
                    raise TradingError('Conflicting fill history.')
                result[identity] = fill
            if len(page) < 500:
                if anchor:
                    saved = result.get((str(anchor['tid']), anchor['coin']))
                    if saved is None or int(saved['time']) != int(anchor['time']):
                        raise TradingError('Fill history continuity cannot be verified: saved fill is missing or changed.')
                values = sorted(result.values(), key=lambda f: (int(f['time']), int(f['tid'])))
                last = values[-1] if values else None
                tail = (dict(time=int(last['time']), coin=last['coin'], tid=str(last['tid'])) if last else None)
                fills = FillHistory(values, tail)
                self.cache[cache_key] = fills
                return fills
            latest = max(int(f['time']) for f in page)
            if latest <= cursor or len(result) >= 10000:
                raise TradingError('Fill history is truncated or ambiguous. Please arm a new rule.')
            cursor = latest  # inclusive overlap; never skip fills in the same millisecond
        raise TradingError('Fill history pagination limit reached.')

    def mid(self, market):
        value = decimal(self.request({'type': 'allMids', 'dex': market.dex})[market.coin])
        if value <= 0:
            raise TradingError('No valid market quote is available.')
        return value

    def send(self, credentials, vault, attempt):
        action = {'type': 'order', 'orders': [attempt['order']], 'grouping': 'na'}
        signature = sign_l1_action(Account.from_key(credentials.private_key), action, vault,
                                   attempt['nonce'], attempt['expires'], True)
        body = dict(action=action, nonce=attempt['nonce'], signature=signature,
                    vaultAddress=vault, expiresAfter=attempt['expires'])
        # No SDK HTTP client, implicit retry, or signed-payload debug logging.
        return self.client._perform_request(body, self.session, exchange=True)

    def status(self, address, cloid):
        return self.request({'type': 'orderStatus', 'user': address, 'oid': cloid})
