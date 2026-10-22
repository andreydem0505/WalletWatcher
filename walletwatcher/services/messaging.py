"""Command replies through the persistent Telegram outbox."""


class Replies:
    def __init__(self, settings, outbox):
        self.settings, self.outbox = settings, outbox

    def reply(self, chat, text):
        # Plain text avoids user-supplied Markdown/HTML breaking command replies.
        for start in range(0, len(text), 1500):
            self.outbox.enqueue(chat, text[start:start + 1500])

    def broadcast(self, text):
        for chat in self.settings.config.recipients:
            self.reply(chat, text)
