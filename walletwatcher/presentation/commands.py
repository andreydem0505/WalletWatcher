"""English help and command response formatting, independent of delivery/storage."""
from datetime import datetime, timezone, timedelta
from html import escape
import time

from walletwatcher.presentation.attention import delivery_delayed, mark_attention, stale

HELP_TEXT = (
    'Available commands:\n\n'
    '/addwallet <address> — track a wallet.\n'
    '/removewallet <address> — stop tracking.\n'
    '/settag <address> <tag> — name a wallet.\n'
    '/creds <address> <private_key> — save or replace your trading credentials.\n'
    '/my — balance, positions, prices, P&L, funding and TP/SL.\n'
    '/wallet <address> — the same portfolio for any address; no credentials needed.\n'
    '/autoclose <wallet> <ticker> — one-shot market close of your position when '
    'the source reduces its initial position by >90%. 1% price limit, up to 3 attempts.\n'
    '/cancelautoclose <wallet> <ticker> — cancel a rule; submitted orders may still execute.\n'
    '/autocloses — list your active rules.\n'
    '/intersections — shared tickers from the latest monitoring snapshots.\n\n'
    '/health — workers, monitoring freshness, request budget and delivery status.\n\n'
    'Only available in a private chat: /creds, /my, /autoclose, /cancelautoclose, /autocloses. '
    '/my and /autoclose require /creds.\n\n'
    '<address>/<wallet>: 0x address; /creds uses your trading account, not the agent.\n'
    '<private_key>: API/agent key, not your main wallet key.\n'
    '<tag>: up to 100 characters; spaces allowed, no semicolons or line breaks.\n'
    '<ticker>: full symbol, e.g. BTC or xyz:EWZ.\n'
    'Arguments in <…> are required; omit the brackets.\n\n'
    'Any other message shows monitoring status and tracked wallets.'
)

MESSAGE_LIMIT = 1500
MOSCOW = timezone(timedelta(hours=3))


def html_messages(lines, limit=MESSAGE_LIMIT):
    """Split between complete sections, preserving copyable addresses and tags."""
    messages, text = [], ''
    for line in lines:
        candidate = text + ('\n' if text else '') + line
        if text and len(candidate) > limit:
            messages.append(text)
            text = line
        else:
            text = candidate
    if text:
        messages.append(text)
    return messages


def format_timestamp(timestamp):
    return datetime.fromtimestamp(timestamp, MOSCOW).strftime('%Y-%m-%d %H:%M:%S') if timestamp else 'not yet'


def monitoring_messages(accounts, last_cycle, pending_notifications, *, now=None, freshness_limit=60, delivery=None):
    now = time.time() if now is None else now
    lines = [f'Wallets: {len(accounts)}',
             mark_attention(f'Last monitoring cycle: {format_timestamp(last_cycle)}', stale(last_cycle, now, freshness_limit)),
             mark_attention(f'Pending notifications: {pending_notifications}', delivery is not None and delivery_delayed(delivery)),
             '', 'Tracked wallets:']
    for wallet, account in accounts.items():
        last = format_timestamp(account.last_updated)
        tag = f'\n{escape(account.tag)}' if account.tag else ''
        status = mark_attention(f'Last success: {last}; failures: {account.failures}',
                                account.failures or stale(account.last_updated, now, freshness_limit))
        lines.append(f'<code>{escape(wallet)}</code>{tag}\n{status}\n')
    return html_messages(lines)


def autoclose_messages(rules):
    return html_messages(f"<code>{escape(rule['wallet'])}</code> {escape(rule['ticker'])}" for rule in rules)
