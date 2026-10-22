"""Per-user API-wallet credentials, encrypted before being written to SQLite."""
import os
from pathlib import Path
import re
import hashlib

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import ec

# Preserve the historical Credentials import path.
from walletwatcher.domain.credentials import Credentials
from walletwatcher.storage.wallets import normalize_wallet


def normalize_private_key(value):
    value = value.removeprefix('0x').removeprefix('0X')
    if not re.fullmatch(r'[0-9a-fA-F]{64}', value):
        raise ValueError('Private key must contain 64 hexadecimal characters. Use an API-wallet key.')
    try:
        ec.derive_private_key(int(value, 16), ec.SECP256K1())
    except ValueError:
        raise ValueError('Invalid API-wallet private key.') from None
    return '0x' + value.lower()


class CredentialsStore:
    def __init__(self, database, lock, bot_id, key_file):
        self.db = database
        self.lock = lock
        self.bot_id = str(bot_id)
        self.key_file = Path(key_file)
        with self.lock, self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS user_credentials (
                bot_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                address TEXT NOT NULL, encrypted_key BLOB NOT NULL,
                PRIMARY KEY (bot_id, user_id)
            )''')

    def _cipher(self, create=False):
        try:
            key = self.key_file.read_bytes()
        except FileNotFoundError:
            if not create or self.db.execute('SELECT 1 FROM user_credentials LIMIT 1').fetchone():
                raise RuntimeError('Credentials encryption key is missing; restore the key file') from None
            key = Fernet.generate_key()
            descriptor = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, 'wb') as file:
                    file.write(key)
                    file.flush()
                    os.fsync(file.fileno())
                if os.name == 'posix':
                    directory = os.open(self.key_file.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            except Exception:
                self.key_file.unlink(missing_ok=True)
                raise
        return Fernet(key)

    def save(self, user_id, address, private_key):
        if type(user_id) is not int or user_id <= 0:
            raise ValueError('A valid Telegram user ID is required.')
        address = normalize_wallet(address)
        private_key = normalize_private_key(private_key)
        with self.lock, self.db:
            encrypted = self._cipher(create=True).encrypt(private_key.encode('ascii'))
            self.db.execute('INSERT OR REPLACE INTO user_credentials VALUES (?, ?, ?, ?)',
                            (self.bot_id, user_id, address, encrypted))

    def get_address(self, user_id):
        # /my only needs the public address; do not decrypt a key for read access.
        with self.lock:
            row = self.db.execute('SELECT address FROM user_credentials WHERE bot_id=? AND user_id=?',
                                  (self.bot_id, user_id)).fetchone()
            return row[0] if row else None

    def version(self, user_id):
        """Opaque registration version; replacement invalidates armed rules."""
        with self.lock:
            row = self.db.execute('SELECT address,encrypted_key FROM user_credentials WHERE bot_id=? AND user_id=?',
                                  (self.bot_id, user_id)).fetchone()
            return hashlib.sha256(row[0].encode() + row[1]).hexdigest() if row else None

    def load(self, user_id):
        with self.lock:
            row = self.db.execute('SELECT address, encrypted_key FROM user_credentials WHERE bot_id=? AND user_id=?',
                                  (self.bot_id, user_id)).fetchone()
            if row is None:
                return None
            private_key = self._cipher().decrypt(row[1]).decode('ascii')
            return Credentials(row[0], private_key)
