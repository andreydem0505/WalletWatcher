"""Live access policy shared with the autoclose worker through RuleAccess."""
from typing import Callable

from walletwatcher.services.settings import Settings


class AutocloseAccess:
    def __init__(self, settings: Settings, is_tracked: Callable[[str], bool]):
        self.settings, self.is_tracked = settings, is_tracked

    @property
    def poll_interval(self):
        return self.settings.config.poll_interval

    def allows_user(self, user):
        return user in self.settings.config.chat_ids

    def tracks_wallet(self, wallet):
        return self.is_tracked(wallet)
