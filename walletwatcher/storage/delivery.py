"""One durable outbox for every Telegram send/edit, including command replies."""
import logging
import sqlite3
import threading
import time
from uuid import uuid4

logger = logging.getLogger(__name__)


class OutboxFull(RuntimeError):
    pass


class DeliveryQueue:
    def __init__(self, bot, path, edit_interval=15, chat_interval=3.1,
                 global_interval=0.1, clock=time.time):
        self.bot = bot
        self.clock = clock
        self.edit_interval = edit_interval
        self.chat_interval = chat_interval
        self.global_interval = global_interval
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS outbox (
                key TEXT PRIMARY KEY, chat INTEGER NOT NULL, generation TEXT NOT NULL,
                text TEXT NOT NULL, html INTEGER NOT NULL, revision INTEGER NOT NULL,
                due REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                persistent INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS receipts (
                key TEXT PRIMARY KEY, generation TEXT NOT NULL,
                message_id INTEGER NOT NULL, text TEXT NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chats (chat INTEGER PRIMARY KEY, due REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS outbox_due ON outbox(due);
        ''')

    def bind_bot(self, bot_id):
        with self.lock, self.db:
            previous = self.get_metadata('bot_id')
            if previous is not None and previous != bot_id:
                for table in ('outbox', 'receipts', 'chats', 'metadata'):
                    self.db.execute(f'DELETE FROM {table}')
                logger.warning('Bot identity changed; discarded the previous bot delivery state')
            self._set_metadata('bot_id', bot_id)

    def get_metadata(self, key, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            return row['value'] if row else default

    def set_metadata(self, key, value):
        with self.lock, self.db:
            self._set_metadata(key, value)

    def _set_metadata(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?, ?)', (key, str(value)))

    def enqueue(self, chat, text, *, key=None, generation='', html=False):
        with self.lock, self.db:
            self.enqueue_transaction(chat, text, key=key, generation=generation, html=html)

    def enqueue_transaction(self, chat, text, *, key=None, generation='', html=False):
        """Caller owns lock/transaction; allows business state and reply to commit together."""
        if not text:
            return
        persistent = key is not None
        key = key or 'reply:' + uuid4().hex
        receipt = self.db.execute('SELECT * FROM receipts WHERE key=?', (key,)).fetchone()
        pending = self.db.execute('SELECT * FROM outbox WHERE key=?', (key,)).fetchone()
        if pending is not None and pending['generation'] == generation and pending['text'] == text:
            return
        if pending is None and receipt is not None and receipt['generation'] == generation and receipt['text'] == text:
            return
        if pending is None and self.db.execute('SELECT count(*) FROM outbox').fetchone()[0] >= 10000:
            raise OutboxFull('Telegram outbox is full; delivery needs attention')
        # Keep a previous retry deadline when replacing text: new updates must
        # not bypass a flood wait or a network-error backoff.
        self.db.execute('''
            INSERT INTO outbox VALUES (?, ?, ?, ?, ?, 1, ?, 0, ?)
            ON CONFLICT(key) DO UPDATE SET generation=excluded.generation,
                text=excluded.text, html=excluded.html, revision=outbox.revision+1
        ''', (key, chat, generation, text, int(html), self.clock(), int(persistent)))

    def forget_wallet(self, wallet):
        prefix = f'wallet:{wallet}:'
        with self.lock, self.db:
            for table in ('outbox', 'receipts'):
                self.db.execute(f'DELETE FROM {table} WHERE key LIKE ?', (prefix + '%',))

    def prune(self, wallets, recipients, reply_chats=None):
        allowed = {f'wallet:{wallet}:{chat}' for wallet in wallets for chat in recipients}
        with self.lock, self.db:
            for table in ('outbox', 'receipts'):
                keys = self.db.execute(f"SELECT key FROM {table} WHERE key LIKE 'wallet:%'").fetchall()
                self.db.executemany(f'DELETE FROM {table} WHERE key=?',
                                    [(row['key'],) for row in keys if row['key'] not in allowed])
            # Do not send old replies to chats removed from the configuration.
            rows = self.db.execute('SELECT key, chat FROM outbox').fetchall()
            self.db.executemany('DELETE FROM outbox WHERE key=?',
                                [(row['key'],) for row in rows
                                 if row['chat'] not in set(recipients) | set(reply_chats or [])])

    def pending_count(self):
        with self.lock:
            return self.db.execute('SELECT count(*) FROM outbox').fetchone()[0]

    def diagnostics(self, chat):
        """Queue counters and delivery waits only; never expose message contents."""
        with self.lock:
            now = self.clock()
            pending, retrying, oldest_due = self.db.execute(
                'SELECT count(*), COALESCE(sum(attempts > 0), 0), min(due) FROM outbox').fetchone()
            row = self.db.execute('SELECT due FROM chats WHERE chat=?', (chat,)).fetchone()
            cooldown = max(0, float(self.get_metadata('telegram_flood_until', 0)) - now)
            wait = max(0, float(self.get_metadata('telegram_due', 0)) - now,
                       row[0] - now if row else 0)
            return dict(pending=pending, retrying=retrying, cooldown=cooldown,
                        reply_wait=max(wait, cooldown), overdue=max(0, now - oldest_due) if oldest_due is not None else 0)

    def _block_until(self, deadline):
        for key in ('telegram_due', 'telegram_flood_until'):
            self._set_metadata(key, max(deadline, float(self.get_metadata(key, 0))))

    def defer_global(self, delay):
        with self.lock, self.db:
            self._block_until(self.clock() + delay)

    def _postpone(self, job, delay, *, global_block=False, chat_block=False):
        deadline = self.clock() + delay
        with self.lock, self.db:
            self.db.execute('UPDATE outbox SET due=?, attempts=attempts+1 WHERE key=?',
                            (deadline, job['key']))
            if global_block:
                self._block_until(deadline)
            if chat_block:
                self.db.execute('INSERT OR REPLACE INTO chats VALUES (?, ?)', (job['chat'], deadline))

    def _acknowledge(self, job, message_id):
        with self.lock, self.db:
            if self.db.execute('SELECT 1 FROM outbox WHERE key=?', (job['key'],)).fetchone() is None:
                return  # Wallet was removed while the request was in flight.
            if job['persistent']:
                self.db.execute('INSERT OR REPLACE INTO receipts VALUES (?, ?, ?, ?, ?)',
                                (job['key'], job['generation'], message_id, job['text'], self.clock()))
            self.db.execute('DELETE FROM outbox WHERE key=? AND revision=? AND generation=?',
                            (job['key'], job['revision'], job['generation']))

    def deliver_once(self):
        """Perform at most one ready request; never sleep holding a lock."""
        now = self.clock()
        with self.lock, self.db:
            if now < float(self.get_metadata('telegram_due', 0)):
                return False
            job = self.db.execute('''
                SELECT o.*, r.message_id, r.generation AS delivered_generation,
                       r.updated AS edited_at
                FROM outbox o LEFT JOIN receipts r ON r.key=o.key
                    LEFT JOIN chats c ON c.chat=o.chat
                WHERE o.due <= ? AND COALESCE(c.due, 0) <= ?
                    AND (r.generation IS NULL OR r.generation != o.generation
                         OR r.updated + ? <= ?)
                ORDER BY o.due, o.rowid LIMIT 1
            ''', (now, now, self.edit_interval, now)).fetchone()
            if job is None:
                return False
            self._set_metadata('telegram_due', now + self.global_interval)
            self.db.execute('INSERT OR REPLACE INTO chats VALUES (?, ?)',
                            (job['chat'], now + self.chat_interval))
        editing = job['message_id'] is not None and job['generation'] == job['delivered_generation']
        try:
            kwargs = dict(chat_id=job['chat'], text=job['text'],
                          parse_mode='HTML' if job['html'] else '',
                          disable_web_page_preview=True, timeout=20)
            if editing:
                self.bot.edit_message_text(message_id=job['message_id'], **kwargs)
                message_id = job['message_id']
            else:
                message_id = self.bot.send_message(**kwargs).message_id
        except Exception as error:
            result = getattr(error, 'result_json', None) or {}
            if not isinstance(result, dict):
                result = {}
            code = result.get('error_code', getattr(error, 'error_code', None))
            description = str(result.get('description', getattr(error, 'description', ''))).lower()
            if code == 429:
                try:
                    parameters = result.get('parameters')
                    delay = float(parameters.get('retry_after', 30) if isinstance(parameters, dict) else 30)
                    if not 0 < delay < float('inf'):
                        raise ValueError()
                except (ValueError, TypeError):
                    delay = 30
                self._postpone(job, delay + 1, global_block=True)
                logger.warning('Telegram flood wait: retry in %.1fs', delay + 1)
            elif editing and code == 400 and 'message is not modified' in description:
                self._acknowledge(job, job['message_id'])
            elif editing and code == 400 and any(value in description for value in (
                    'message to edit not found', "message can't be edited", 'message_id_invalid')):
                with self.lock, self.db:
                    self.db.execute('DELETE FROM receipts WHERE key=?', (job['key'],))
                self._postpone(job, self.chat_interval)
                logger.warning('Telegram message unavailable; will send a replacement')
            elif code == 403 or (code == 400 and 'chat not found' in description):
                self._postpone(job, 3600, chat_block=True)
                logger.error('Telegram chat %s is unavailable; retry in one hour', job['chat'])
            elif code == 401:
                self._postpone(job, 300, global_block=True)
                logger.error('Telegram rejected BOT_TOKEN; check configuration')
            else:
                delay = 3600 if code == 400 else min(300, 2 ** min(job['attempts'] + 1, 9))
                self._postpone(job, delay)
                # Do not log exception strings containing Telegram token URLs.
                logger.warning('Telegram request failed (%s, code=%s); retry in %ss',
                               type(error).__name__, code, delay)
            return True
        self._acknowledge(job, message_id)
        return True

    def run(self, stop, heartbeat):
        while not stop.is_set():
            heartbeat()
            self.deliver_once()
            stop.wait(0.1)

    def close(self):
        with self.lock:
            self.db.close()
