"""Shared attention markers and delay thresholds for local status reports."""
CHAT_WAIT_GRACE = 10
QUEUE_WAIT_GRACE = 30


def mark_attention(text, needed=True):
    return f'❗ {text}' if needed else text


def stale(timestamp, now, limit):
    return timestamp is None or now - timestamp > limit


def delivery_delayed(delivery):
    return (delivery['retrying'] or delivery['cooldown'] or delivery['reply_wait'] > CHAT_WAIT_GRACE
            or delivery['overdue'] > QUEUE_WAIT_GRACE)
