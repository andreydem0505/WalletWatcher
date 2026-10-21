"""English help and command response formatting, independent of delivery/storage."""
from datetime import datetime, timezone, timedelta
from html import escape

HELP_TEXT = (
    'Available commands:\n\n'
    '/addwallet <address> — add a wallet to monitoring.\n\n'
    '/removewallet <address> — remove a wallet from monitoring.\n\n'
    '/settag <address> <tag> — set a name for a tracked wallet. '
    'Names may contain spaces: up to 100 characters, without semicolons or line breaks.\n\n'
    '/creds <address> <private_key> — save or replace your trading account address '
    'and API wallet private key, linked to your Telegram ID. '
    'Only available in a private chat with the bot.\n\n'
    '/my — show your account balance and open positions: margin, prices, '
    'P&L, funding and configured TP/SL. No arguments; private chat only. '
    'Save your credentials using /creds first.\n\n'
    '/autoclose <wallet> <ticker> — once the tracked wallet has less than 10% of its '
    'initial position remaining, market-close your position in this ticker. '
    'Private chat only; /creds required. One-shot, 1% price limit, up to 3 attempts.\n\n'
    '/cancelautoclose <wallet> <ticker> — cancel your rule. Already submitted orders '
    'may still execute. Use full HIP-3 tickers such as xyz:EWZ.\n\n'
    '/autocloses — your active rules; private chat only.\n\n'
    '<address> — a wallet address starting with 0x; for /creds, use the trading '
    'account address, not the API wallet address.\n'
    '<private_key> — the API/agent wallet private key, not your main wallet key.\n'
    '<tag> — the wallet name.\n'
    'Arguments in <…> are required: replace them with your values without the brackets.\n\n'
    'Any other message shows monitoring status, tracked wallets, last successful '
    'check times, failure counts and pending notifications.'
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


def monitoring_messages(accounts, last_cycle, pending_notifications):
    lines = [f'Wallets: {len(accounts)}', f'Last monitoring cycle: {format_timestamp(last_cycle)}',
             f'Pending notifications: {pending_notifications}', '', 'Tracked wallets:']
    for wallet, account in accounts.items():
        last = format_timestamp(account.last_updated)
        tag = f'\n{escape(account.tag)}' if account.tag else ''
        lines.append(f'<code>{escape(wallet)}</code>{tag}\nLast success: {last}; failures: {account.failures}\n')
    return html_messages(lines)


def autoclose_messages(rules):
    return html_messages(f"<code>{escape(rule['wallet'])}</code> {escape(rule['ticker'])}" for rule in rules)
