"""Wallet snapshots, alert state and the independent monitoring loop."""
import copy
import logging
import threading
import time

from walletwatcher.domain.models import Account, TradeAction
from walletwatcher.hyperliquid.errors import DataFetchError
from walletwatcher.presentation.formatting import on_change_message
from walletwatcher.storage.wallets import load_accounts, save_wallets

logger = logging.getLogger(__name__)


class MonitoringService:
    def __init__(self, settings, client, outbox, stop, cancel_source):
        self.settings, self.client = settings, client
        self.outbox, self.stop = outbox, stop
        self.cancel_source = cancel_source
        self.lock = threading.RLock()
        self.accounts = load_accounts(settings.config.wallets_file)
        self.last_cycle = None

    @property
    def config(self):
        return self.settings.config

    def is_tracked(self, wallet):
        with self.lock:
            return wallet in self.accounts

    def accounts_in_file_order(self):
        # Only refresh ordering; keep the monitored Account objects and their state.
        try:
            file_order = load_accounts(self.config.wallets_file)
        except OSError as error:
            logger.warning('Could not read wallet order (%s); using tracked order', type(error).__name__)
            file_order = {}
        ordered = dict.fromkeys((*file_order, *self.accounts))
        return {wallet: self.accounts[wallet] for wallet in ordered if wallet in self.accounts}

    def change_wallet(self, operation, wallet, tag=None):
        with self.lock:
            proposed = self.accounts_in_file_order()
            if operation == 'add':
                if wallet in proposed:
                    return False
                proposed[wallet] = Account()
            else:
                if wallet not in proposed:
                    return False
                if operation == 'remove':
                    del proposed[wallet]
                else:
                    # Validate/save without changing live state on write failure.
                    proposed[wallet] = copy.copy(proposed[wallet])
                    proposed[wallet].tag = tag
            save_wallets(proposed, self.config.wallets_file)
            self.accounts = proposed
            if operation == 'remove':
                self.outbox.forget_wallet(wallet)
                self.cancel_source(wallet)
        return True

    def poll_wallet(self, wallet, account):
        positions = self.client.fetch_open_positions(wallet)
        with self.lock:
            if self.accounts.get(wallet) is not account:
                return
            before = {position.ticker: position for position in account.positions or []}
            after = {position.ticker: position for position in positions}
            changed_coins = {coin for coin in before.keys() | after.keys() if before.get(coin) != after.get(coin)}
            changed = account.positions is not None and bool(changed_coins)
        trade = self.client.fetch_last_trade(wallet, coins=changed_coins) if changed else None
        if changed and trade is None:
            raise DataFetchError('Positions changed but no matching perpetual fill was found; retrying')
        with self.lock:
            if self.accounts.get(wallet) is not account:
                return
            if account.positions is None:
                account.positions = positions
            elif changed:
                if trade.timestamp < account.last_actions.get(TradeAction(trade.ticker, trade.action), 0):
                    raise DataFetchError('Received an older fill; retrying')
                account.update(trade, positions)
            # Requeue current state even if the API positions have not changed.
            # A failed enqueue must not turn a detected trade into a lost alert.
            if account.last_trade is not None:
                text = on_change_message(wallet, account)
                for chat in self.config.recipients:
                    self.outbox.enqueue(chat, text, key=f'wallet:{wallet}:{chat}',
                                        generation=account.generation, html=True)
            account.last_updated = time.time()
            account.failures = 0
            account.next_poll = time.monotonic() + self.config.poll_interval

    def run(self, heartbeat):
        self.client.set_heartbeat(heartbeat)
        while not self.stop.is_set():
            heartbeat()
            with self.lock:
                accounts = list(self.accounts.items())
            for wallet, account in accounts:
                if self.stop.is_set():
                    return
                heartbeat()
                if time.monotonic() < account.next_poll:
                    continue
                try:
                    self.poll_wallet(wallet, account)
                except Exception as error:
                    if isinstance(error, DataFetchError) and error.reason in ('trading_priority', 'trading_capacity'):
                        break  # A deliberate pause must not increase wallet failure backoff.
                    with self.lock:
                        account.failures = min(account.failures + 1, 10)
                        account.next_poll = time.monotonic() + min(300, 2 ** account.failures)
                    logger.warning('Wallet %s check failed (%s): %s; retry scheduled',
                                   wallet, type(error).__name__, error)
                self.stop.wait(0.2)
            with self.lock:
                self.last_cycle = time.time()
            self.stop.wait(1)  # Also wait when there are no wallets.
