"""Local heartbeat probe; it never calls Telegram or Hyperliquid."""
import math
import os
from pathlib import Path
import time


def healthy(path, timeout, now=None):
    try:
        timeout = float(timeout)
        if not math.isfinite(timeout) or not 60 <= timeout <= 3600:
            return False
        age = (time.time() if now is None else now) - Path(path).stat().st_mtime
        # Allow the supervisor's maximum 60s restart backoff and child startup.
        return 0 <= age <= timeout + 90
    except (OSError, ValueError, TypeError):
        return False


def main():
    return 0 if healthy(os.getenv('WALLETWATCHER_HEARTBEAT', '/tmp/walletwatcher-heartbeat'),
                        os.getenv('WATCHDOG_TIMEOUT', '180')) else 1


if __name__ == '__main__':
    raise SystemExit(main())
