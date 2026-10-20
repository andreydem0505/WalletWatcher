"""Fresh personal account snapshots and their Telegram HTML presentation."""
from dataclasses import dataclass, field
from html import escape
from typing import Optional

from walletwatcher.presentation.formatting import format_position, label, number
from walletwatcher.domain.models import Position


@dataclass
class PersonalPosition:
    position: Position
    margin: float
    current_price: str
    roe: Optional[float]
    funding: Optional[float]
    take_profit: list[str] = field(default_factory=list)
    stop_loss: list[str] = field(default_factory=list)


@dataclass
class Portfolio:
    balance: float
    positions: list[PersonalPosition]


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
