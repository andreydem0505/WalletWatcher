"""Telegram command parsing, access checks and update polling."""
import logging
import time

from walletwatcher.infrastructure.runtime import redact_secrets
from walletwatcher.presentation.commands import HELP_TEXT, autoclose_messages, monitoring_messages
from walletwatcher.presentation.intersections import intersection_messages
from walletwatcher.services.portfolios import PortfolioRequest
from walletwatcher.storage.wallets import normalize_wallet, validate_tag
from walletwatcher.trading.policy import ticker_name

logger = logging.getLogger(__name__)


class TelegramController:
    def __init__(self, settings, bot, client, outbox, stop, credentials,
                 autoclose_store, monitoring, portfolios, health, replies):
        self.settings, self.bot, self.client = settings, bot, client
        self.outbox, self.stop, self.credentials = outbox, stop, credentials
        self.autoclose_store, self.monitoring = autoclose_store, monitoring
        self.portfolios, self.health, self.replies = portfolios, health, replies
        self.offset = int(outbox.get_metadata('telegram_offset', 0))

    @property
    def config(self):
        return self.settings.config

    def reply(self, chat, text):
        self.replies.reply(chat, text)

    def broadcast(self, text):
        self.replies.broadcast(text)

    def handle_message(self, message):
        chat = message.chat.id
        if chat not in self.config.chat_ids or not isinstance(message.text, str):
            return
        args = message.text.split()
        command = args[0].split('@', 1)[0].lower() if args else ''
        if command in ('/autoclose', '/cancelautoclose', '/autocloses'):
            self.autoclose_command(message, command, args)
            return
        if command in ('/creds', '/my'):
            self.personal_command(message, command, args)
            return
        try:
            self._command(chat, message.text, user=self._private_user_id(message))
        except ValueError as error:
            self.reply(chat, redact_secrets(str(error)))
        except Exception:
            logger.exception('Command failed in chat %s', chat)
            self.reply(chat, 'Command failed; no success was confirmed. Please retry.')

    def delete_credentials_message(self, message):
        message_id = getattr(message, 'message_id', None)
        if message_id is None:
            return False
        try:
            return bool(self.bot.delete_message(message.chat.id, message_id, timeout=10))
        except Exception as error:
            # Never render an exception or the inbound command containing a key.
            result = getattr(error, 'result_json', None)
            if isinstance(result, dict) and result.get('error_code') == 429:
                try:
                    delay = float(result.get('parameters', {}).get('retry_after', 30))
                    if not 0 < delay < float('inf'):
                        raise ValueError()
                except (AttributeError, TypeError, ValueError):
                    delay = 30
                self.outbox.defer_global(delay + 1)
            logger.warning('Could not delete credentials message (%s)', type(error).__name__)
            return False

    @staticmethod
    def _private_user_id(message):
        user = getattr(getattr(message, 'from_user', None), 'id', None)
        if (getattr(message.chat, 'type', None) == 'private'
                and type(user) is int and user > 0 and user == message.chat.id):
            return user
        return None

    def autoclose_command(self, message, command, args):
        user = self._private_user_id(message)
        chat = message.chat.id
        if user is None:
            self.reply(chat, 'Autoclose commands are available only in your private chat with the bot.')
            return
        try:
            if command == '/autocloses':
                if len(args) != 1:
                    raise ValueError('Usage: /autocloses')
                self.list_autocloses(user)
                return
            if len(args) != 3:
                raise ValueError(f'Usage: {command} <wallet> <ticker>')
            wallet, ticker = normalize_wallet(args[1]), ticker_name(args[2])
            version = self.credentials.version(user)
            if version is None:
                raise ValueError('Credentials are missing. Save them with /creds first.')
            if command == '/cancelautoclose':
                if not self.autoclose_store.cancel(user, wallet, ticker):
                    raise ValueError('No active autoclose rule was found for this wallet and ticker.')
                self.reply(chat, 'Autoclose cancellation recorded. No new orders will be submitted. '
                           'An already submitted order may still execute.')
                return
            with self.monitoring.lock:
                if wallet not in self.monitoring.accounts:
                    raise ValueError('This wallet is not being tracked.')
            self.autoclose_store.create(user, wallet, ticker, version)
            self.reply(chat, 'Autoclose request saved. Checking fresh positions and API-wallet permissions.')
        except ValueError as error:
            self.reply(chat, redact_secrets(str(error)))
        except Exception as error:
            logger.error('Autoclose command failed (%s)', type(error).__name__)
            self.reply(chat, 'Autoclose command failed; no activation was confirmed. Please retry.')

    def list_autocloses(self, user):
        rules = self.autoclose_store.for_user(user)
        if not rules:
            self.reply(user, 'You have no active autoclose rules.')
            return
        for text in autoclose_messages(rules):
            self.outbox.enqueue(user, text, html=True)

    def personal_command(self, message, command, args):
        chat = message.chat.id
        deleted = self.delete_credentials_message(message) if command == '/creds' else False
        user_id = self._private_user_id(message)
        if user_id is None:
            self.reply(chat, 'Команды /creds и /my доступны только в личном чате с ботом.')
            return
        try:
            if command == '/creds':
                if len(args) != 3:
                    raise ValueError('Использование: /creds <address> <private key API wallet>')
                self.credentials.save(user_id, args[1], args[2])
                self.autoclose_store.cancel(user_id)
                response = 'Данные аккаунта сохранены и привязаны к вашему Telegram ID. Позиции: /my'
                if not deleted:
                    response += '\nСообщение с ключом удалить не удалось — удалите его вручную.'
                self.reply(chat, response)
                return
            if len(args) != 1:
                raise ValueError('Использование: /my')
            address = self.credentials.get_address(user_id)
            if address is None:
                self.reply(chat, 'Данные аккаунта ещё не сохранены. Используйте /creds <address> <private key API wallet>.')
                return
            self.portfolios.enqueue(PortfolioRequest(chat, address, user_id))
        except ValueError as error:
            self.reply(chat, redact_secrets(str(error)))
        except Exception as error:
            logger.error('Personal command %s failed for user %s (%s)', command, user_id, type(error).__name__)
            self.reply(chat, 'Команда не выполнена. Повторите попытку позже.')

    def _command(self, chat, text, user=None):
        args = text.split()
        command = args[0].split('@', 1)[0].lower() if args else ''
        if command == '/help':
            if len(args) != 1:
                raise ValueError('Usage: /help')
            self.reply(chat, HELP_TEXT)
            return
        if command == '/health':
            if len(args) != 1:
                raise ValueError('Usage: /health')
            self.health.send(chat, user)
            return
        if command == '/wallet':
            if len(args) != 2:
                raise ValueError('Usage: /wallet <address>')
            self.portfolios.enqueue(PortfolioRequest(chat, normalize_wallet(args[1])))
            return
        if command == '/intersections':
            if len(args) != 1:
                raise ValueError('Usage: /intersections')
            with self.monitoring.lock:
                messages = intersection_messages(self.monitoring.accounts_in_file_order())
            for message in messages:
                self.outbox.enqueue(chat, message, html=True)
            return
        if command in ('/addwallet', '/removewallet', '/settag'):
            self._wallet_command(chat, command, args)
            return
        self._send_status(chat)

    def _wallet_command(self, chat, command, args):
        expected = 3 if command == '/settag' else 2
        if len(args) < expected or (expected == 2 and len(args) != 2):
            raise ValueError(f'Usage: {command} <wallet>' + (' <tag>' if expected == 3 else ''))
        wallet = normalize_wallet(args[1])
        tag = validate_tag(' '.join(args[2:])) if command == '/settag' else None
        operation = {'/addwallet': 'add', '/removewallet': 'remove', '/settag': 'tag'}[command]
        changed = self.monitoring.change_wallet(operation, wallet, tag)
        if changed:
            response = {'add': f'Wallet {wallet} added',
                        'remove': f'Wallet {wallet} removed',
                        'tag': f'Wallet {wallet} tag set to {tag}'}[operation]
            self.broadcast(response)
        else:
            response = (f'Wallet {wallet} is already being tracked' if operation == 'add'
                        else f'Wallet {wallet} is not being tracked')
            self.reply(chat, response)

    def _send_status(self, chat):
        with self.monitoring.lock:
            accounts = self.monitoring.accounts_in_file_order()
            delivery = self.outbox.diagnostics(chat)
            messages = monitoring_messages(
                accounts, self.monitoring.last_cycle, delivery['pending'], now=time.time(),
                freshness_limit=max(60, 3 * self.config.poll_interval), delivery=delivery)
        for message in messages:
            self.outbox.enqueue(chat, message, html=True)

    def polling(self, heartbeat):
        self.client.set_heartbeat(heartbeat)
        failures = 0
        while not self.stop.is_set():
            heartbeat()
            flood_until = float(self.outbox.get_metadata('telegram_flood_until', 0))
            if time.time() < flood_until:
                self.stop.wait(max(0, min(1, flood_until - time.time())))
                continue
            try:
                updates = self.bot.get_updates(offset=self.offset, timeout=30,
                                               long_polling_timeout=20, allowed_updates=['message'])
                for update in updates:
                    if self.stop.is_set():
                        return
                    heartbeat()
                    if update.message is not None:
                        self.handle_message(update.message)
                    self.offset = update.update_id + 1
                    self.outbox.set_metadata('telegram_offset', self.offset)
                failures = 0
            except Exception as error:
                failures = min(failures + 1, 8)
                result = getattr(error, 'result_json', None) or {}
                if not isinstance(result, dict):
                    result = {}
                try:
                    parameters = result.get('parameters')
                    delay = float(parameters.get('retry_after', min(60, 2 ** failures))
                                  if isinstance(parameters, dict) else min(60, 2 ** failures))
                    if not 0 < delay < float('inf'):
                        raise ValueError()
                except (TypeError, ValueError):
                    delay = 30
                if result.get('error_code') in (401, 409):
                    delay = max(delay, 60)
                if result.get('error_code') == 429:
                    self.outbox.defer_global(delay + 1)
                logger.warning('Telegram polling failed (%s, code=%s); retry in %.1fs',
                               type(error).__name__, result.get('error_code'), delay)
                # Long flood waits are healthy inactivity; keep heartbeats alive.
                deadline = time.monotonic() + delay + 1
                while not self.stop.is_set() and time.monotonic() < deadline:
                    heartbeat()
                    self.stop.wait(min(1, max(0, deadline - time.monotonic())))
            self.stop.wait(0.1)
