"""Environment validation and paths independent of the current directory."""
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import re

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


class ConfigError(ValueError):
    pass


def interval(name: str, default: float, minimum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        if not math.isfinite(value) or value < minimum or value > 3600:
            raise ValueError()
        return value
    except ValueError as error:
        raise ConfigError(f'{name} must be a number between {minimum} and 3600') from error


def parse_perp_dexs(value: str) -> tuple[str, ...]:
    names = [name.strip() for name in value.split(',')]
    if not names or any(not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', name) for name in names):
        raise ConfigError('PERP_DEXS must contain comma-separated dex names; use main for the original market')
    return tuple(dict.fromkeys('' if name == 'main' else name for name in names))


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    chat_ids: tuple[int, ...]
    admin_id: int
    mode: str
    poll_interval: float = 5
    edit_interval: float = 15
    chat_interval: float = 3.1
    global_interval: float = 0.1
    watchdog_timeout: float = 180
    wallets_file: Path = ROOT / 'wallets.txt'
    database_file: Path = ROOT / 'state.sqlite3'
    perp_dexs: tuple[str, ...] = ('',)

    @property
    def recipients(self):
        return (self.admin_id,) if self.mode == 'TEST' else self.chat_ids

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / '.env')
        token = os.getenv('BOT_TOKEN', '').strip()
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token):
            raise ConfigError('BOT_TOKEN is missing or malformed')
        try:
            ids = tuple(dict.fromkeys(int(value.strip()) for value in os.getenv('CHAT_IDS', '').split(',') if value.strip()))
            admin = int(os.getenv('ADMIN_ID', ''))
        except ValueError as error:
            raise ConfigError('CHAT_IDS and ADMIN_ID must contain integer Telegram IDs') from error
        if not ids or 0 in ids or admin == 0:
            raise ConfigError('CHAT_IDS must not be empty and IDs must not be zero')
        mode = os.getenv('MODE', 'PROD').strip().upper()
        if mode not in ('TEST', 'PROD'):
            raise ConfigError('MODE must be TEST or PROD')
        directory = Path(os.getenv('WALLETWATCHER_DATA_DIR', '/data'))
        if not directory.is_absolute():
            raise ConfigError('WALLETWATCHER_DATA_DIR must be an absolute path')
        return cls(token, ids, admin, mode,
                   interval('POLL_INTERVAL', 5, 1), interval('EDIT_INTERVAL', 15, 5),
                   interval('CHAT_INTERVAL', 3.1, 3.1), interval('GLOBAL_INTERVAL', 0.1, 0.05),
                   interval('WATCHDOG_TIMEOUT', 180, 60),
                   wallets_file=directory / 'wallets.txt',
                   database_file=directory / 'state.sqlite3',
                   perp_dexs=parse_perp_dexs(os.getenv('PERP_DEXS', 'main')))
