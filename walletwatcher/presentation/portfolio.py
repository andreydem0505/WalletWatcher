"""Portfolio HTML presentation with compatibility exports for existing callers."""
from html import escape

# Compatibility exports; models and parsing no longer live in presentation.
from walletwatcher.domain.portfolio import PersonalPosition, Portfolio
from walletwatcher.hyperliquid.parsing import parse_positions


def money(value, *, signed=False):
    sign = '-' if value < 0 else '+' if signed and value > 0 else ''
    return f'{sign}${abs(value):.2f}'


def portfolio_messages(portfolio):
    blocks = [f'<b>Balance: {money(portfolio.balance)}</b>']
    if not portfolio.positions:
        blocks.append('No open positions.')
    for personal in sorted(portfolio.positions, key=lambda item: (-item.position.volume, item.position.ticker)):
        pos = personal.position
        percent = (f'{"+" if personal.roe > 0 else ""}{personal.roe * 100:.1f}%'
                   if personal.roe is not None else 'N/A')
        funding = money(personal.funding, signed=True) if personal.funding is not None else 'N/A'
        lines = [f'<b>{escape(pos.ticker)} {escape(pos.direction)} {pos.leverage:g} {escape(pos.leverage_type)}</b>',
                 f'Margin: {money(personal.margin)}',
                 f'Entry Price: {escape(pos.entry_price) if pos.entry_price is not None else "N/A"}',
                 f'Cur Price: {escape(personal.current_price)}',
                 f'P&amp;L: {money(pos.pnl, signed=True)} ({percent})', f'Funding: {funding}']
        lines.extend(f'🟢 TP: {escape(price)}' for price in personal.take_profit)
        lines.extend(f'🔴 SL: {escape(price)}' for price in personal.stop_loss)
        blocks.append('\n'.join(lines))
    messages, current = [], ''
    for block in blocks:
        # Split very large position/order lists at complete lines, never in HTML.
        parts = [block] if len(block.encode('utf-16-le')) // 2 <= 3500 else block.splitlines()
        for index, part in enumerate(parts):
            separator = '\n\n' if index == 0 else '\n'
            candidate = current + (separator if current else '') + part
            if len(candidate.encode('utf-16-le')) // 2 > 3800:
                messages.append(current)
                current = part
            else:
                current = candidate
    if current:
        messages.append(current)
    return messages
