"""Validate wallet data and save using fsync + atomic replacement."""
import logging
import os
from pathlib import Path
import re
import tempfile

from models import Account

logger = logging.getLogger(__name__)
WALLETS_FILE = Path(__file__).resolve().parent / 'wallets.txt'


def normalize_wallet(wallet: str) -> str:
    wallet = wallet.strip().lower()
    if not re.fullmatch(r'0x[0-9a-f]{40}', wallet):
        raise ValueError('Wallet must be 0x followed by 40 hexadecimal characters')
    return wallet


def validate_tag(tag: str) -> str:
    tag = tag.strip()
    if len(tag) > 100 or any(char in tag for char in (';', '\r', '\n')):
        raise ValueError('Tag must contain at most 100 characters and no semicolons or newlines')
    return tag


def load_accounts(path=WALLETS_FILE) -> dict[str, Account]:
    accounts = {}
    try:
        lines = Path(path).read_text(encoding='utf-8-sig').splitlines()
    except FileNotFoundError:
        return accounts
    for line_no, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        try:
            wallet, _, tag = line.partition(';')
            accounts[normalize_wallet(wallet)] = Account(validate_tag(tag) or None)
        except ValueError as error:
            logger.warning('Ignoring invalid wallet line %d: %s', line_no, error)
    return accounts


def save_wallets(accounts: dict[str, Account], path=WALLETS_FILE) -> None:
    path = Path(path)
    text = '\n'.join(f'{normalize_wallet(wallet)};{validate_tag(account.tag or "")}'
                     for wallet, account in sorted(accounts.items()))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=f'.{path.name}.', delete=False) as file:
            temporary = Path(file.name)
            file.write(text + ('\n' if text else ''))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name == 'posix':
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
