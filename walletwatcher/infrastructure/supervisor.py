"""Restart crashed/stalled bot processes with backoff inside Docker."""
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from walletwatcher.config import ROOT
from walletwatcher.infrastructure.runtime import InstanceLock, configure_logging, in_docker

logger = logging.getLogger(__name__)


def terminate(process, timeout=10):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def supervise(command, stop, heartbeat_path, watchdog_timeout=180, *, initial_delay=2,
              maximum_delay=60, stable_after=300):
    """A clean exit is intentional; nonzero exits and watchdog timeouts restart."""
    delay = initial_delay
    while not stop.is_set():
        Path(heartbeat_path).touch()
        environment = dict(os.environ, WALLETWATCHER_HEARTBEAT=str(heartbeat_path),
                           WALLETWATCHER_PARENT_PID=str(os.getpid()), PYTHONUNBUFFERED='1')
        started = time.monotonic()
        try:
            process = subprocess.Popen(command, cwd=ROOT, env=environment)
        except OSError:
            logger.exception('Cannot start bot process; retrying')
            if stop.wait(delay):
                return 0
            delay = min(maximum_delay, delay * 2)
            continue
        stalled = False
        last_heartbeat = Path(heartbeat_path).stat().st_mtime_ns
        last_seen = time.monotonic()
        try:
            while process.poll() is None and not stop.wait(0.2):
                heartbeat = Path(heartbeat_path).stat().st_mtime_ns
                if heartbeat != last_heartbeat:
                    last_heartbeat = heartbeat
                    last_seen = time.monotonic()
                if time.monotonic() - last_seen > watchdog_timeout:
                    stalled = True
                    logger.error('Bot heartbeat expired; replacing stalled process')
                    terminate(process)
            if stop.is_set():
                terminate(process)
                return 0
            code = process.wait()
        finally:
            terminate(process)
        if not stalled and code in (0, 78):
            if code == 78:
                logger.error('Fix .env configuration before restarting')
            return code
        if time.monotonic() - started >= stable_after:
            delay = initial_delay
        logger.warning('Bot exited (%s); restart in %.1fs', code, delay)
        if stop.wait(delay):
            return 0
        delay = min(maximum_delay, delay * 2)
    return 0


def main():
    configure_logging()
    if not in_docker():
        logger.error('WalletWatcher must run in Docker: docker compose up -d --build')
        return 78
    # Validate before starting a child: configuration errors cannot be repaired
    # by repeatedly restarting the same process.
    from walletwatcher.config import Config, ConfigError
    try:
        config = Config.from_env()
    except ConfigError as error:
        logger.error('Configuration error: %s', error)
        return 78
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    config.wallets_file.parent.mkdir(parents=True, exist_ok=True)
    heartbeat = Path(os.getenv('WALLETWATCHER_HEARTBEAT', '/tmp/walletwatcher-heartbeat'))
    with InstanceLock(config.wallets_file.parent / 'supervisor.lock'):
        try:
            return supervise([sys.executable, '-m', 'walletwatcher.app'], stop,
                             heartbeat, config.watchdog_timeout)
        finally:
            heartbeat.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        logger.exception('Supervisor failed')
        raise SystemExit(1)
