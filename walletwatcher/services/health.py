"""Read-only health snapshots; diagnostics never query the exchange."""
import time

from walletwatcher.presentation.health import health_messages


class HealthService:
    def __init__(self, settings, monitoring, portfolios, client, outbox, autoclose_store, started_at):
        self.settings, self.monitoring, self.portfolios = settings, monitoring, portfolios
        self.client, self.outbox, self.autoclose_store = client, outbox, autoclose_store
        self.started_at = started_at
        self.supervisor = None

    @property
    def config(self):
        return self.settings.config

    def send(self, chat, user):
        with self.monitoring.lock:
            accounts = list(self.monitoring.accounts.values())
            successes = [account.last_updated for account in accounts if account.last_updated is not None]
            monitoring = dict(wallets=len(accounts), unchecked=len(accounts) - len(successes),
                              failing=sum(account.failures > 0 for account in accounts),
                              last_cycle=self.monitoring.last_cycle, oldest_success=min(successes, default=None), now=time.time(),
                              freshness_limit=max(60, 3 * self.config.poll_interval))
        personal = self.portfolios.pending_count()
        rules = self.autoclose_store.for_user(user) if user is not None else None
        unresolved = sum(rule['user'] == user for rule, _ in self.autoclose_store.unresolved()) if user is not None else 0
        messages = health_messages(
            uptime=time.monotonic() - self.started_at, monitoring=monitoring,
            budget=self.client.request_status(), delivery=self.outbox.diagnostics(chat),
            workers=self.supervisor.diagnostics() if self.supervisor is not None else {},
            personal=personal, rules=rules, unresolved=unresolved)
        for message in messages:
            self.outbox.enqueue(chat, message, html=True)
