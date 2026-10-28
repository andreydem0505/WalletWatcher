"""Compact operational status, expanded only for the affected components."""
from html import escape
from math import ceil

from walletwatcher.infrastructure.runtime import redact_secrets
from walletwatcher.presentation.attention import (
    CHAT_WAIT_GRACE, QUEUE_WAIT_GRACE, delivery_delayed, mark_attention, stale,
)
from walletwatcher.presentation.commands import format_timestamp, html_messages

WORKER_NAMES = ('monitor', 'delivery', 'polling', 'personal', 'autoclose')


def _monitoring_details(monitoring, pause, pause_reason):
    oldest = monitoring['oldest_success']
    now, limit = monitoring['now'], monitoring['freshness_limit']
    old_cycle = stale(monitoring['last_cycle'], now, limit)
    old_wallet = stale(oldest, now, limit)
    if not (old_cycle or (oldest is not None and old_wallet) or monitoring['unchecked'] or monitoring['failing']):
        return None
    age = f' ({int(max(0, now - oldest))}s ago)' if oldest is not None else ''
    return '\n'.join([
        mark_attention('<b>Monitoring</b>'),
        mark_attention(f"Wallets: {monitoring['wallets']}; never checked: {monitoring['unchecked']}", monitoring['unchecked']),
        mark_attention(f"Wallets with failures: {monitoring['failing']}", monitoring['failing']),
        mark_attention(f"Last cycle: {format_timestamp(monitoring['last_cycle'])}", old_cycle),
        mark_attention(f'Oldest wallet success: {format_timestamp(oldest)}{age}', old_wallet),
        mark_attention(f"Next position request: {'paused — ' + escape(pause) if pause else 'available'}",
                       pause_reason in ('budget', 'rate_limit')),
    ])


def _worker_details(workers):
    if not workers:
        return mark_attention('<b>Workers</b>') + '\n' + mark_attention('Not started.')
    lines = []
    for name in dict.fromkeys((*WORKER_NAMES, *workers)):
        status = workers.get(name)
        if status is None:
            lines.append(mark_attention(f'{escape(name)}: not started'))
        elif status['state'] != 'running':
            line = (f"{escape(name)}: {escape(status['state'])}; "
                    f"heartbeat {int(status['heartbeat_age'])}s ago")
            if status['state'] == 'awaiting restart':
                line += f"; retry in {ceil(status['restart_in'])}s"
            lines.append(mark_attention(line))
    return mark_attention('<b>Workers</b>') + '\n' + '\n'.join(lines) if lines else None


def _autoclose_details(rules, unresolved):
    if rules is None:
        return []
    delayed = [rule for rule in rules if 'check_error_since' in rule
               or (rule['state'] == 'queued' and rule.get('activation_error'))]
    if not delayed and not unresolved:
        return []
    sections = [mark_attention('<b>Your autoclose rules</b>') + '\n'
                f"Queued: {sum(rule['state'] == 'queued' for rule in rules)}; "
                f"armed: {sum(rule['state'] == 'armed' for rule in rules)}; "
                f"closing: {sum(rule['state'] == 'closing' for rule in rules)}\n"
                + mark_attention(f'Delayed checks: {len(delayed)}', bool(delayed)) + '; '
                + mark_attention(f'unconfirmed orders: {unresolved}', unresolved)]
    for rule in delayed:
        error = rule.get('last_check_error', {})
        detail = rule.get('activation_error') if rule['state'] == 'queued' else (
            f"{error.get('stage', 'checking account data')}: {error.get('detail', 'exchange data unavailable')}")
        sections.append(mark_attention(f"<b>{escape(rule['ticker'])}</b>")
                        + f"\n<code>{escape(rule['wallet'])}</code>\n"
                        + mark_attention(escape(redact_secrets(detail))))
    return sections


def health_messages(*, uptime, monitoring, budget, delivery, workers, personal, rules, unresolved):
    details = []
    pause_reason = budget['monitor_pause_reason']
    monitor = _monitoring_details(monitoring, budget['monitor_pause'], pause_reason)
    if monitor:
        details.append(monitor)
    # Yielding to a scheduled or triggered close is normal priority handling.
    budget_issue = budget['cooldown'] or budget['pending'] or pause_reason == 'budget'
    if budget_issue:
        details.append('\n'.join([
            mark_attention('<b>Hyperliquid — bot budget (rolling 60s)</b>'),
            mark_attention(f"Weight: {budget['used']}/{budget['limit']} ({100 * budget['used'] / budget['limit']:.0f}%)",
                           pause_reason == 'budget' or budget['used'] >= budget['limit']),
            mark_attention(f"Pending trading demand: {budget['pending']}", budget['pending']),
            mark_attention(f"Exchange cooldown: {ceil(budget['cooldown'])}s", budget['cooldown']),
        ]))
    delivery_issue = delivery_delayed(delivery)
    if delivery_issue:
        details.append('\n'.join([
            mark_attention('<b>Telegram</b>'),
            mark_attention(f"Pending notifications: {delivery['pending']}; retrying: {delivery['retrying']}",
                           delivery['retrying'] or delivery['overdue'] > QUEUE_WAIT_GRACE),
            mark_attention(f"Delivery cooldown: {ceil(delivery['cooldown'])}s; this chat wait: {ceil(delivery['reply_wait'])}s",
                           delivery['cooldown'] or delivery['reply_wait'] > CHAT_WAIT_GRACE),
            mark_attention(f"Oldest overdue notification: {int(delivery['overdue'])}s", delivery['overdue'] > QUEUE_WAIT_GRACE),
        ]))
    worker = _worker_details(workers)
    if worker:
        details.append(worker)
    autoclose = _autoclose_details(rules, unresolved)
    details.extend(autoclose)
    running = sum(status['state'] == 'running' for name, status in workers.items() if name in WORKER_NAMES)
    summary = [('<b>❗ Health: issues or delays</b>' if details else '<b>✅ Health: OK</b>'),
               mark_attention(f"Wallets: {monitoring['wallets']}", monitor is not None) + ' | '
               + mark_attention(f'Workers: {running}/{len(WORKER_NAMES)}', worker is not None),
               mark_attention(f"API: {budget['used']}/{budget['limit']}", budget_issue) + ' | '
               + mark_attention(f"Pending notifications: {delivery['pending']}", delivery_issue)]
    if rules is not None:
        summary.append(mark_attention(f'Your autoclose rules: {len(rules)}', bool(autoclose)))
    if pause_reason in ('trading_priority', 'trading_capacity'):
        summary.append('Monitoring: autoclose priority')
    if personal:
        summary.append(f'Portfolio requests queued or running: {personal}')
    if details:
        summary.append(f'Uptime: {int(max(0, uptime))}s')
    return html_messages(section + '\n' for section in ['\n'.join(summary), *details])
