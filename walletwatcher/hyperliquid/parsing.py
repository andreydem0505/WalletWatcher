"""Translate Hyperliquid response fields into domain values, without HTML or I/O."""
from walletwatcher.domain.models import Position, Trade
from walletwatcher.domain.portfolio import PersonalPosition
from walletwatcher.domain.validation import label, number


def format_position(data: dict) -> Position:
    position = data['position']
    size = number(position['szi'])
    entry = position['entryPx']
    if entry is not None:
        number(entry)
    max_leverage = position.get('maxLeverage')
    if max_leverage is not None:
        max_leverage = number(max_leverage)
        if max_leverage <= 0:
            raise ValueError('Invalid maximum leverage in API response')
    return Position(label(position['coin']), 'Short' if size < 0 else 'Long',
                    number(position['leverage']['value']), label(position['leverage']['type']),
                    size, label(str(entry)) if entry is not None else None, abs(number(position['positionValue'])),
                    number(position['unrealizedPnl']), max_leverage=max_leverage)


def format_trade(data: dict) -> Trade:
    number(data['px'])
    number(data['sz'])
    timestamp = int(data['time'])
    if timestamp < 0:
        raise ValueError('Negative trade timestamp')
    return Trade(label(data['coin']), label(str(data['px'])), label(str(data['sz'])), label(data['dir']), timestamp)


def parse_positions(state, prices, orders):
    if not isinstance(state['assetPositions'], list) or not isinstance(orders, list):
        raise ValueError('Invalid positions or orders')
    result = []
    for item in state['assetPositions']:
        pos = format_position(item)
        if pos.size == 0:
            continue
        data = item['position']
        margin = number(data['marginUsed'])
        if margin < 0 or pos.leverage <= 0:
            raise ValueError('Invalid margin or leverage')
        current = label(str(prices[pos.ticker]))
        if number(current) <= 0:
            raise ValueError('Invalid mark price')
        raw_roe = data.get('returnOnEquity')
        roe = number(raw_roe) if raw_roe is not None else (pos.pnl / margin if margin else None)
        if roe is not None:
            number(roe * 100)
        funding = data.get('cumFunding', {}).get('sinceOpen')
        # Hyperliquid accumulates funding costs: positive = paid, negative = received.
        # Present the account's cash flow, with expenses negative and income positive.
        personal = PersonalPosition(pos, margin, current, roe,
                                    -number(funding) if funding is not None else None)
        for order in orders:
            if (order.get('coin') != pos.ticker or order.get('isTrigger') is not True
                    or not (order.get('reduceOnly') is True or order.get('isPositionTpsl') is True)
                    or order.get('side') != ('A' if pos.size > 0 else 'B')):
                continue
            kind = str(order.get('orderType', '')).lower()
            target = personal.take_profit if kind.startswith('take profit') else (
                personal.stop_loss if kind.startswith('stop') else None)
            if target is None:
                continue
            price = label(str(order['triggerPx']))
            if number(price) <= 0:
                raise ValueError('Invalid trigger price')
            if price not in target:
                target.append(price)
        result.append(personal)
    return result
