"""Compact operational status, expanded only for the affected components."""
from html import escape
from math import ceil

from walletwatcher.infrastructure.runtime import redact_secrets
from walletwatcher.presentation.commands import format_timestamp, html_messages

WORKER_NAMES = ('monitor', 'delivery', 'polling', 'personal', 'autoclose')
CHAT_WAIT_GRACE = 10
QUEUE_WAIT_GRACE = 30


def _monitoring_details(monitoring, pause):
    oldest = monitoring['oldest_success']
    now, limit = monitoring['now'], monitoring['freshness_limit']
    stale = (monitoring['last_cycle'] is None or now - monitoring['last_cycle'] > limit
             or (oldest is not None and now - oldest > limit))
    if not (stale or monitoring['unchecked'] or monitoring['failing']):
        return None
    age = f' ({int(max(0, now - oldest))}s ago)' if oldest is not None else ''
    return ('<b>Monitoring</b>\n'
            f"Wallets: {monitoring['wallets']}; never checked: {monitoring['unchecked']}\n"
            f"Wallets with failures: {monitoring['failing']}\n"
            f"Last cycle: {format_timestamp(monitoring['last_cycle'])}\n"
            f'Oldest wallet success: {format_timestamp(oldest)}{age}\n'
            f"Next position request: {'paused — ' + escape(pause) if pause else 'available'}")


def _worker_details(workers):
    if not workers:
        return '<b>Workers</b>\nNot started.'
    lines = []
    for name in dict.fromkeys((*WORKER_NAMES, *workers)):
        status = workers.get(name)
        if status is None:
            lines.append(f'{escape(name)}: not started')
        elif status['state'] != 'running':
            line = (f"{escape(name)}: {escape(status['state'])}; "
                    f"heartbeat {int(status['heartbeat_age'])}s ago")
            if status['state'] == 'awaiting restart':
                line += f"; retry in {ceil(status['restart_in'])}s"
            lines.append(line)
    return '<b>Workers</b>\n' + '\n'.join(lines) if lines else None


def _autoclose_details(rules, unresolved):
    if rules is None:
        return []
    delayed = [rule for rule in rules if 'check_error_since' in rule
               or (rule['state'] == 'queued' and rule.get('activation_error'))]
    if not delayed and not unresolved:
        return []
    sections = ['<b>Your autoclose rules</b>\n'
                f"Queued: {sum(rule['state'] == 'queued' for rule in rules)}; "
                f"armed: {sum(rule['state'] == 'armed' for rule in rules)}; "
                f"closing: {sum(rule['state'] == 'closing' for rule in rules)}\n"
                f'Delayed checks: {len(delayed)}; unconfirmed orders: {unresolved}']
    for rule in delayed:
        error = rule.get('last_check_error', {})
        detail = rule.get('activation_error') if rule['state'] == 'queued' else (
            f"{error.get('stage', 'checking account data')}: {error.get('detail', 'exchange data unavailable')}")
        sections.append(f"<b>{escape(rule['ticker'])}</b>\n<code>{escape(rule['wallet'])}</code>\n"
                        + escape(redact_secrets(detail)))
    return sections


def health_messages(*, uptime, monitoring, budget, delivery, workers, personal, rules, unresolved):
    details = []
    pause_reason = budget['monitor_pause_reason']
    monitor = _monitoring_details(monitoring, budget['monitor_pause'])
    if monitor:
        details.append(monitor)
    # Yielding to a scheduled or triggered close is normal priority handling.
    if budget['cooldown'] or budget['pending'] or pause_reason == 'budget':
        details.append('<b>Hyperliquid — bot budget (rolling 60s)</b>\n'
                       f"Weight: {budget['used']}/{budget['limit']} "
                       f"({100 * budget['used'] / budget['limit']:.0f}%)\n"
                       f"Pending trading demand: {budget['pending']}\n"
                       f"Exchange cooldown: {ceil(budget['cooldown'])}s")
    if (delivery['retrying'] or delivery['cooldown'] or delivery['reply_wait'] > CHAT_WAIT_GRACE
            or delivery['overdue'] > QUEUE_WAIT_GRACE):
        details.append('<b>Telegram</b>\n'
                       f"Pending notifications: {delivery['pending']}; retrying: {delivery['retrying']}\n"
                       f"Delivery cooldown: {ceil(delivery['cooldown'])}s; "
                       f"this chat wait: {ceil(delivery['reply_wait'])}s\n"
                       f"Oldest overdue notification: {int(delivery['overdue'])}s")
    worker = _worker_details(workers)
    if worker:
        details.append(worker)
    details.extend(_autoclose_details(rules, unresolved))
    running = sum(status['state'] == 'running' for name, status in workers.items() if name in WORKER_NAMES)
    summary = [('<b>⚠️ Health: issues or delays</b>' if details else '<b>✅ Health: OK</b>'),
               f"Wallets: {monitoring['wallets']} | Workers: {running}/{len(WORKER_NAMES)}",
               f"API: {budget['used']}/{budget['limit']} | Pending notifications: {delivery['pending']}"]
    if rules is not None:
        summary.append(f'Your autoclose rules: {len(rules)}')
    if pause_reason in ('trading_priority', 'trading_capacity'):
        summary.append('Monitoring: autoclose priority')
    if personal:
        summary.append(f'Portfolio requests queued or running: {personal}')
    if details:
        summary.append(f'Uptime: {int(max(0, uptime))}s')
    return html_messages(section + '\n' for section in ['\n'.join(summary), *details])
