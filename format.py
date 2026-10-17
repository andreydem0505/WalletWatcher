"""API parsing and safe Telegram HTML; truncate only complete sections."""
import math
from html import escape
from urllib.parse import quote

from models import Account, Position, Trade


def number(value) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Non-finite number in API response')
    return result


def label(value, limit=64) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError('Invalid text field in API response')
    return value


def format_number(num: float) -> str:
    num = number(num)
    suffixes = ('', 'K', 'M', 'B', 'T')
    magnitude = 0
    while abs(num) >= 1000 and magnitude < len(suffixes) - 1:
        magnitude += 1
        num /= 1000
    return f'{num:.3g}{suffixes[magnitude]}'


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


def on_change_message(wallet: str, account: Account) -> str:
    trade = account.last_trade
    if trade is None:
        raise ValueError('Cannot render an account without a trade')
    volume = abs(number(trade.size) * number(trade.price))
    header = (
        f'❗️ <b>{escape(trade.ticker)} {escape(trade.action)}</b> ❗️\n'
        f'<code>{escape(account.tag or wallet)}</code>\n'
        f'Volume: ${format_number(volume)}\nPrice: {escape(trade.price)}\n\n'
        '<b>Current positions with changes since this alert started:</b>\n'
    )
    footer = '\nhttps://hyperdash.com/address/' + quote(wallet, safe='')
    blocks = []
    for pos in account.positions or []:
        previous = account.snapshot.get(pos.key)
        leverage = (f'{previous.leverage:g}× → {pos.leverage:g}×'
                    if previous is not None and previous.leverage != pos.leverage
                    else f'{pos.leverage:g}')
        change = ' 🆕' if pos.is_new else (
            f' ({"+" if pos.delta > 0 else "-"}${format_number(abs(pos.delta))}) 👈'
            if pos.delta else ''
        )
        if pos.max_leverage is not None and pos.leverage == pos.max_leverage:
            change += '👑' if change else ' 👑'
        blocks.append(
            f'\n<b>{escape(pos.ticker)} {escape(pos.direction)} '
            f'{leverage} {escape(pos.leverage_type)}</b>\n'
            f'Volume: ${format_number(pos.volume)}{change}\n'
            f'Entry Price: {escape(pos.entry_price) if pos.entry_price is not None else "N/A"}\n'
            f'P&amp;L: {"+" if pos.pnl >= 0 else "-"}${format_number(abs(pos.pnl))}\n'
        )
    blocks.extend(f'\n<b>{escape(ticker)} Position Closed</b> ❌\n' for ticker in account.closed_positions)
    text = header
    for index, block in enumerate(blocks):
        if len((text + block + footer).encode('utf-16-le')) // 2 > 3800:
            text += f'\n… {len(blocks) - index} more positions; see the link below.\n'
            break
        text += block
    return text + footer
