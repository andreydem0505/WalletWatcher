"""Decimal-only perpetual orders; SDK is used only for offline signing."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import re

from eth_account import Account
from hyperliquid.utils.signing import sign_l1_action


class TradingError(ValueError):
    """A safe, user-facing failure that must stop the rule."""


def decimal(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise TradingError('Invalid numeric response from Hyperliquid.')
    return result


def ticker_name(value):
    if not re.fullmatch(r'(?:[a-zA-Z][a-zA-Z0-9_-]{0,31}:)?[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}', value):
        raise TradingError('Invalid perpetual ticker. Use BTC or a full HIP-3 ticker such as xyz:EWZ.')
    return value.upper() if ':' not in value else value.split(':')[0].lower() + ':' + value.split(':')[1].upper()


def triggered(base, current):
    base, current = decimal(base), decimal(current)
    return base * current <= 0 or abs(current) * 10 < abs(base)


@dataclass(frozen=True)
class Market:
    coin: str
    dex: str
    asset: int
    decimals: int

    def price(self, mid, buy):
        raw = decimal(mid) * (Decimal('1.01') if buy else Decimal('0.99'))
        if raw <= 0:
            raise TradingError('Invalid market price.')
        # Integer prices are always allowed. Otherwise <=5 significant figures
        # and <=6-szDecimals decimal places. Round INTO the permitted 1% bound.
        places = max(0, min(6 - self.decimals, 4 - raw.adjusted()))
        result = raw.quantize(Decimal(1).scaleb(-places), rounding=ROUND_FLOOR if buy else ROUND_CEILING)
        if result <= 0:
            raise TradingError('Price cannot be represented within the 1% limit.')
        return format(result, 'f')

    def order(self, size, price):
        size = decimal(size)
        amount = abs(size)
        if amount == 0 or amount != amount.quantize(Decimal(1).scaleb(-self.decimals)):
            raise TradingError('Position size does not match the market precision.')
        return dict(a=self.asset, b=size < 0, p=price, s=format(amount, 'f'),
                    r=True, t={'limit': {'tif': 'Ioc'}})


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

    def fills(self, address, start, end):
        # Conservative continuity guard: if 2000 recent fills do not reach our
        # checkpoint, do not risk the exchange's 10000-fill retention boundary.
        recent = self.request({'type': 'userFills', 'user': address, 'aggregateByTime': False})
        if not isinstance(recent, list):
            raise TradingError('Invalid fill history.')
        if len(recent) >= 2000 and min(int(f['time']) for f in recent) > start:
            raise TradingError('Fill history continuity cannot be verified. Please arm a new rule.')
        result, cursor = {}, start
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
                return sorted(result.values(), key=lambda f: (int(f['time']), int(f['tid'])))
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


def replay(rule, current, timestamp, fills):
    """Validate continuity and detect original-position closure, including round trips."""
    previous = decimal(rule['own_size'])
    if timestamp < rule['checkpoint']:
        raise TradingError('Position snapshot moved backwards in time.')
    ended = False
    for fill in fills:
        if (fill['coin'] != rule['ticker'] or int(fill['time']) < rule['checkpoint']
                or (int(fill['time']) == rule['checkpoint']
                    and str(fill['tid']) in rule.get('boundary', []))):
            continue
        start = decimal(fill['startPosition'])
        amount = decimal(fill['sz'])
        if start != previous or amount <= 0 or fill['side'] not in ('B', 'A'):
            raise TradingError('Position history is incomplete or inconsistent.')
        after = start + (amount if fill['side'] == 'B' else -amount)
        if start == 0 or start * after <= 0:
            ended = True
        previous = after
    if previous != current:
        raise TradingError('Position and fill history do not agree; no order was sent.')
    return ended


def checkpoint(rule, current, timestamp, fills):
    rule.update(own_size=str(current), checkpoint=timestamp,
                boundary=[str(f['tid']) for f in fills
                          if f['coin'] == rule['ticker'] and int(f['time']) == timestamp])
