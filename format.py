from models import Position, Trade


def format_number(num: int) -> str:
    num = float('{:.3g}'.format(num))
    magnitude = 0
    while abs(num) >= 1000:
        magnitude += 1
        num /= 1000.0
    return '{}{}'.format('{:f}'.format(num).rstrip('0').rstrip('.'), ['', 'K', 'M', 'B', 'T'][magnitude])


def format_position(data: dict) -> Position:
    return Position(
        ticker=data['position']['coin'],
        size=data['position']['szi'],
        leverage=data['position']['leverage']['value'],
        leverage_type=data['position']['leverage']['type'],
        direction='Short' if data['position']['szi'][0] == '-' else 'Long',
        entry_price=data['position']['entryPx'],
        volume=int(float(data['position']['positionValue'])),
    )


def format_trade(data: dict) -> Trade:
    return Trade(
        ticker=data['coin'],
        price=data['px'],
        size=data['sz'],
        action=data['dir'],
    )