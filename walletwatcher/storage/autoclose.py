"""Durable rule/attempt journal. No keys or signed requests are stored here."""
import json
from uuid import uuid4


class AutocloseStore:
    def __init__(self, outbox, bot_id):
        self.outbox, self.db, self.lock = outbox, outbox.db, outbox.lock
        self.bot_id = str(bot_id)
        with self.lock, self.db:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS autoclose_rules (
                    id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                    wallet TEXT NOT NULL, ticker TEXT NOT NULL, active INTEGER NOT NULL,
                    cancelled INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS autoclose_active
                    ON autoclose_rules(bot_id,user_id,wallet,ticker) WHERE active=1;
                CREATE TABLE IF NOT EXISTS autoclose_attempts (
                    rule_id TEXT NOT NULL, number INTEGER NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(rule_id,number)
                );
                CREATE TABLE IF NOT EXISTS autoclose_nonces (
                    signer TEXT PRIMARY KEY, nonce INTEGER NOT NULL
                );
            ''')

    def create(self, user, wallet, ticker, version):
        with self.lock, self.db:
            if self.db.execute('SELECT 1 FROM autoclose_rules WHERE bot_id=? AND user_id=? '
                               'AND wallet=? AND ticker=? AND active=1',
                               (self.bot_id, user, wallet, ticker)).fetchone():
                raise ValueError('An autoclose rule for this wallet and ticker is already active.')
            rule = dict(id=uuid4().hex, user=user, wallet=wallet, ticker=ticker,
                        version=version, state='queued', attempts=0, filled='0', value='0')
            self.db.execute('INSERT INTO autoclose_rules VALUES (?,?,?,?,?,1,0,?)',
                            (rule['id'], self.bot_id, user, wallet, ticker, json.dumps(rule)))
            return rule

    def active(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute(
                'SELECT data FROM autoclose_rules WHERE bot_id=? AND active=1 ORDER BY rowid', (self.bot_id,))]

    def for_user(self, user):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute(
                'SELECT data FROM autoclose_rules WHERE bot_id=? AND user_id=? '
                'AND active=1 AND cancelled=0 ORDER BY rowid', (self.bot_id, user))]

    def unresolved(self):
        with self.lock:
            result = []
            rows = self.db.execute('SELECT data,active FROM autoclose_rules WHERE bot_id=?', (self.bot_id,))
            for raw, active in rows:
                rule = json.loads(raw)
                if 'pending' in rule:
                    result.append((rule, bool(active)))
            return result

    def save(self, rule):
        with self.lock, self.db:
            self.db.execute('UPDATE autoclose_rules SET data=? WHERE id=? AND active=1',
                            (json.dumps(rule), rule['id']))

    def cancel_source(self, wallet):
        with self.lock, self.db:
            self.db.execute('UPDATE autoclose_rules SET cancelled=1 WHERE bot_id=? AND wallet=? AND active=1',
                            (self.bot_id, wallet))

    def cancelled(self, rule):
        with self.lock:
            row = self.db.execute('SELECT active,cancelled FROM autoclose_rules WHERE id=?', (rule['id'],)).fetchone()
            return not row or not row[0] or bool(row[1])

    def cancel(self, user, wallet=None, ticker=None):
        with self.lock, self.db:
            query = 'UPDATE autoclose_rules SET cancelled=1 WHERE active=1 AND bot_id=? AND user_id=?'
            args = [self.bot_id, user]
            if wallet is not None:
                query += ' AND wallet=?'
                args.append(wallet)
            if ticker is not None:
                query += ' AND ticker=?'
                args.append(ticker)
            return self.db.execute(query, args).rowcount

    def finish(self, rule, state, text, notify=True, update_terminal=False, attempt=None):
        with self.lock, self.db:
            rule['state'] = state
            if attempt is not None:
                self.db.execute('UPDATE autoclose_attempts SET data=? WHERE rule_id=? AND number=?',
                                (json.dumps(attempt), rule['id'], attempt['number']))
            changed = self.db.execute('UPDATE autoclose_rules SET active=0,data=? WHERE id=? AND (active=1 OR ?)',
                                      (json.dumps(rule), rule['id'], update_terminal)).rowcount
            if changed and notify:
                self.outbox.enqueue_transaction(rule['user'], text, key='autoclose:' + rule['id'])

    def notify(self, rule, text, *, key=None, attempt=None):
        with self.lock, self.db:
            if attempt is not None:
                self.db.execute('UPDATE autoclose_attempts SET data=? WHERE rule_id=? AND number=?',
                                (json.dumps(attempt), rule['id'], attempt['number']))
            self.db.execute('UPDATE autoclose_rules SET data=? WHERE id=?', (json.dumps(rule), rule['id']))
            self.outbox.enqueue_transaction(rule['user'], text, key=key or 'autoclose-armed:' + rule['id'])

    def prepare(self, rule, signer, order, now):
        """The send boundary is committed before the HTTP request; never replay it."""
        with self.lock, self.db:
            if self.cancelled(rule):
                return None
            row = self.db.execute('SELECT nonce FROM autoclose_nonces WHERE signer=?', (signer.lower(),)).fetchone()
            nonce = max(int(now * 1000), row[0] + 1 if row else 0)
            self.db.execute('INSERT OR REPLACE INTO autoclose_nonces VALUES (?,?)', (signer.lower(), nonce))
            attempt = dict(number=rule['attempts'] + 1, cloid='0x' + uuid4().hex,
                           nonce=nonce, expires=int(now * 1000) + 15000, sent=now, order=order, resolved=False)
            attempt['order']['c'] = attempt['cloid']
            rule.update(attempts=attempt['number'], pending=attempt, state='closing')
            self.db.execute('INSERT INTO autoclose_attempts VALUES (?,?,?)',
                            (rule['id'], attempt['number'], json.dumps(attempt)))
            self.db.execute('UPDATE autoclose_rules SET data=? WHERE id=?', (json.dumps(rule), rule['id']))
            return attempt

    def record(self, rule, attempt):
        with self.lock, self.db:
            self.db.execute('UPDATE autoclose_attempts SET data=? WHERE rule_id=? AND number=?',
                            (json.dumps(attempt), rule['id'], attempt['number']))
            self.db.execute('UPDATE autoclose_rules SET data=? WHERE id=?', (json.dumps(rule), rule['id']))
