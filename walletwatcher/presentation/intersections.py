"""Shared ticker report from the latest monitored wallet snapshots."""
from html import escape

from walletwatcher.presentation.commands import html_messages
from walletwatcher.presentation.formatting import position_block


def intersection_messages(accounts):
    if not accounts:
        return ['No wallets are being tracked.']
    groups = {}
    unavailable = 0
    for wallet, account in accounts.items():
        if account.positions is None:
            unavailable += 1
        for position in account.positions or []:
            if position.size != 0:
                groups.setdefault(position.ticker, {})[wallet] = (account, position)
    sections = []
    for ticker in sorted(groups):
        wallets = groups[ticker]
        if len(wallets) < 2:
            continue
        for wallet, (account, position) in wallets.items():
            tag = f'<code>{escape(account.tag)}</code>\n' if account.tag else ''
            sections.append(tag + f'<code>{escape(wallet)}</code>\n'
                            + position_block(position, account.snapshot.get(position.key)))
    if not sections:
        sections.append('No tickers have open positions in more than one tracked wallet.')
    if unavailable:
        sections.append(f'Position data is not available yet for {unavailable} tracked wallet(s); '
                        'these wallets are excluded. Please retry after a monitoring cycle.')
    return html_messages(section + '\n' for section in sections)
