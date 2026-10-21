"""Pure trading rules and Decimal calculations; no network or signing dependencies."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import re

MAX_CLOSE_ATTEMPTS = 3
MIN_RETRY_INTERVAL = 2
ACTIVATION_TIMEOUT = 300
ORDER_CONFIRMATION_GRACE_MS = 5000


class TradingError(ValueError):
    """A safe, user-facing failure that must stop the rule."""


class FillHistory(list):
    def __init__(self, values, anchor):
        super().__init__(values)
        self.anchor = anchor


def decimal(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise TradingError('Invalid numeric response from Hyperliquid.')
    return result


def ticker_name(value):
    if not re.fullmatch(r'(?:[a-zA-Z][a-zA-Z0-9_-]{0,31}:)?[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}', value):
        raise TradingError('Invalid perpetual ticker. Use BTC or a full HIP-3 ticker such as xyz:EWZ.')
    if ':' not in value:
        return value.upper()
    dex, ticker = value.split(':')
    return f'{dex.lower()}:{ticker.upper()}'


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
    if getattr(fills, 'anchor', None) is not None:
        rule['history_anchor'] = fills.anchor
