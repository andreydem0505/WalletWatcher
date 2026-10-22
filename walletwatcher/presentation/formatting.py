"""Safe Telegram HTML; legacy parsing imports remain available."""
from html import escape
from urllib.parse import quote

from walletwatcher.domain.models import Account, Position, Trade
from walletwatcher.domain.validation import label, number
# Compatibility exports; API adapters import parsing directly.
from walletwatcher.hyperliquid.parsing import format_position, format_trade


def format_number(num: float) -> str:
    num = number(num)
    suffixes = ('', 'K', 'M', 'B', 'T')
    magnitude = 0
    while abs(num) >= 1000 and magnitude < len(suffixes) - 1:
        magnitude += 1
        num /= 1000
    return f'{num:.3g}{suffixes[magnitude]}'


def position_block(pos: Position, previous=None) -> str:
    """The four position lines shared by trade alerts and intersections."""
    leverage = (f'{previous.leverage:g}× → {pos.leverage:g}×'
                if previous is not None and previous.leverage != pos.leverage
                else f'{pos.leverage:g}')
    change = ' 🆕' if pos.is_new else (
        f' ({"+" if pos.delta > 0 else "-"}${format_number(abs(pos.delta))}) 👈'
        if pos.delta else ''
    )
    if pos.max_leverage is not None and pos.leverage == pos.max_leverage:
        change = change + '👑' if pos.is_new else ' 👑' + change
    return (
        f'<b>{escape(pos.ticker)} {escape(pos.direction)} '
        f'{leverage} {escape(pos.leverage_type)}</b>\n'
        f'Volume: ${format_number(pos.volume)}{change}\n'
        f'Entry Price: {escape(pos.entry_price) if pos.entry_price is not None else "N/A"}\n'
        f'P&amp;L: {"+" if pos.pnl >= 0 else "-"}${format_number(abs(pos.pnl))}'
    )


def on_change_message(wallet: str, account: Account) -> str:
    trade = account.last_trade
    if trade is None:
        raise ValueError('Cannot render an account without a trade')
    volume = abs(number(trade.size) * number(trade.price))
    tag = f'<code>{escape(account.tag)}</code>\n' if account.tag else ''
    header = (
        f'❗️ <b>{escape(trade.ticker)} {escape(trade.action)}</b> ❗️\n'
        f'{tag}<code>{escape(wallet)}</code>\n'
        f'Volume: ${format_number(volume)}\nPrice: {escape(trade.price)}\n\n'
        '<b>Current positions with changes since this alert started:</b>\n'
    )
    footer = '\nhttps://hyperdash.com/address/' + quote(wallet, safe='')
    blocks = []
    for pos in account.positions or []:
        previous = account.snapshot.get(pos.key)
        blocks.append('\n' + position_block(pos, previous) + '\n')
    blocks.extend(f'\n<b>{escape(ticker)} Position Closed</b> ❌\n' for ticker in account.closed_positions)
    text = header
    for index, block in enumerate(blocks):
        if len((text + block + footer).encode('utf-16-le')) // 2 > 3800:
            text += f'\n… {len(blocks) - index} more positions; see the link below.\n'
            break
        text += block
    return text + footer
