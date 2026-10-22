"""Logging and worker supervision shared by the bot and process supervisor."""
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import threading
import time


def in_docker():
    """Docker creates this marker; imports and offline tests remain usable."""
    return Path('/.dockerenv').is_file()


def redact_secrets(text):
    text = re.sub(r'\d{5,}:[A-Za-z0-9_-]+', '<BOT_TOKEN>', text)
    return re.sub(r'(?<![0-9a-fA-F])(?:0[xX])?[0-9a-fA-F]{64}(?![0-9a-fA-F])',
                  '<PRIVATE_KEY>', text)


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        # Redact the rendered traceback too: requests exceptions contain URLs.
        return redact_secrets(super().format(record))


def configure_logging(path=None):
    handlers = [logging.StreamHandler()]
    if path is not None:
        try:
            handlers.append(RotatingFileHandler(path, maxBytes=5 * 1024 * 1024,
                                                backupCount=3, encoding='utf-8'))
        except OSError:
            logging.warning('Log file unavailable; using stderr')
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
                        handlers=handlers, force=True)
    for handler in handlers:
        handler.setFormatter(RedactingFormatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))


class WorkerSupervisor:
    def __init__(self, stop, timeout=180, clock=time.monotonic):
        self.stop = stop
        self.timeout = timeout
        self.clock = clock
        self.workers = {}
        self.lock = threading.Lock()

    def beat(self, name):
        with self.lock:
            self.workers[name]['heartbeat'] = self.clock()

    def add(self, name, target):
        with self.lock:
            self.workers[name] = dict(target=target, thread=None, heartbeat=self.clock(), restart_at=0)
        self._start(name)

    def _start(self, name):
        worker = self.workers[name]
        def guarded():
            try:
                worker['target'](lambda: self.beat(name))
            except Exception:
                logging.getLogger(__name__).exception('Worker %s crashed', name)

        with self.lock:
            worker['heartbeat'] = self.clock()
            worker['restart_at'] = self.clock() + 5
            worker['thread'] = threading.Thread(target=guarded, name=name, daemon=True)
            worker['thread'].start()

    def diagnostics(self):
        """Return copied liveness data without restarting or touching workers."""
        with self.lock:
            now = self.clock()
            result = {}
            for name, worker in self.workers.items():
                age = max(0, now - worker['heartbeat'])
                thread = worker['thread']
                if thread is None:
                    state = 'starting'
                elif not thread.is_alive():
                    state = 'stopped' if self.stop.is_set() else 'awaiting restart'
                else:
                    state = 'stalled' if age > self.timeout else 'running'
                result[name] = dict(state=state, heartbeat_age=age,
                                    restart_in=max(0, worker['restart_at'] - now))
            return result

    def check(self):
        for name, worker in self.workers.items():
            if not worker['thread'].is_alive():
                if not self.stop.is_set() and self.clock() >= worker['restart_at']:
                    logging.getLogger(__name__).warning('Restarting worker %s', name)
                    self._start(name)
            elif self.clock() - worker['heartbeat'] > self.timeout:
                # Python cannot safely kill a stuck thread. The process must be
                # replaced, rather than start a second writer beside it.
                raise RuntimeError(f'Worker {name} stopped responding')

    def join(self, timeout=5):
        deadline = time.monotonic() + timeout
        for worker in self.workers.values():
            worker['thread'].join(max(0, deadline - time.monotonic()))
        return all(not worker['thread'].is_alive() for worker in self.workers.values())


class InstanceLock:
    """Prevent multiple local pollers sharing one state database (POSIX)."""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        if os.name != 'posix':
            raise RuntimeError('The supervisor requires Linux or macOS')
        import fcntl
        self.file = self.path.open('a')
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('Another WalletWatcher instance is already running') from None
        return self

    def __exit__(self, *args):
        self.file.close()
