import os
from time import sleep
import threading
import telebot
from datetime import datetime
from pytz import timezone
from dotenv import load_dotenv
import logging
import signal
import atexit
from models import Account, MessageId
from serialization import load_accounts, save_wallets
from data_fetcher import fetch_open_positions, fetch_last_trade
from format import format_number


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', filename='logs', filemode='w')
logger = logging.getLogger(__name__)


load_dotenv()

TOKEN = os.getenv('BOT_TOKEN')

# List of users allowed to interact with the bot
CHAT_IDS = list(map(int, os.getenv('CHAT_IDS').split(',')))

ADMIN_ID = int(os.getenv('ADMIN_ID'))

# Possible values: PROD, TEST
MODE = os.getenv('MODE')

TIMEZONE = timezone('Europe/Moscow')

TRADER_URL = 'https://legacy.hyperdash.com/trader/'

bot = telebot.TeleBot(TOKEN, parse_mode='Markdown')

# Time of the last update of all wallets positions
last_updated = datetime.now()

accounts = load_accounts()

MESSAGE_UPDATE_TIME = 0.5 * len(CHAT_IDS)


def add_wallet(chat_id: int, wallet: str):
    if wallet not in accounts:
        accounts[wallet] = Account()
        send_everyone(f'Wallet {wallet} added')
    else:
        bot.send_message(chat_id, f'Wallet {wallet} is already being tracked')


def remove_wallet(chat_id: int, wallet: str):
    if wallet in accounts:
        del accounts[wallet]
        send_everyone(f'Wallet {wallet} removed')
    else:
        bot.send_message(chat_id, f'Wallet {wallet} is not being tracked')


def set_tag(chat_id: int, wallet: str, tag: str):
    if ';' in tag:
        bot.send_message(chat_id, 'Tag cannot contain semicolon character')
        return
    if wallet in accounts:
        accounts[wallet].tag = tag
        send_everyone(f'Wallet {wallet} tag set to {tag}')
    else:
        bot.send_message(chat_id, f'Wallet {wallet} is not being tracked')


@bot.message_handler(func=lambda m: True)
def reply(m: telebot.types.Message):

    # ignore messages from unauthorized users
    if m.chat.id not in CHAT_IDS:
        return
    
    if m.text.startswith('/addwallet '):
        try:
            wallet = m.text.split(' ')[1]
            add_wallet(m.chat.id, wallet)
        except IndexError:
            bot.send_message(m.chat.id, 'Usage: /addwallet <wallet>')

    elif m.text.startswith('/removewallet '):
        try:
            wallet = m.text.split(' ')[1]
            remove_wallet(m.chat.id, wallet)
        except IndexError:
            bot.send_message(m.chat.id, 'Usage: /removewallet <wallet>')

    elif m.text.startswith('/settag '):
        try:
            wallet = m.text.split(' ')[1]
            tag = ' '.join(m.text.split(' ')[2:])
            set_tag(m.chat.id, wallet, tag)
        except ValueError:
            bot.send_message(m.chat.id, 'Usage: /settag <wallet> <tag>')
    
    else:
        message = f'Last updated: {str(last_updated.astimezone(TIMEZONE))}\n\n'
        message += 'Tracked wallets:\n'
        message += '\n'.join([f"`{k}` {f'({v.tag})' if v.tag else ''}" for k, v in accounts.items()])
        bot.send_message(m.chat.id, message)


def send_everyone(message: str) -> list[MessageId]:
    if MODE == 'TEST':
        msg = bot.send_message(ADMIN_ID, message)
        return [MessageId(ADMIN_ID, msg.message_id)]
    message_ids = []
    for chat_id in CHAT_IDS:
        try:
            msg = bot.send_message(chat_id, message)
            message_ids.append(MessageId(chat_id, msg.message_id))
        except Exception as e:
            logger.error(f"exception while sending message: {e}")
    return message_ids


def on_change_message(wallet: str, account: Account) -> str:
    last_trade = account.last_trade
    volume = int(float(last_trade.size) * float(last_trade.price))
    message = f"❗️ *{last_trade.ticker} {last_trade.action}* ❗️"
    message += f"\n`{account.tag if account.tag else wallet}`"
    message += f"\nVolume: ${format_number(volume)}"
    message += f"\nPrice: {last_trade.price}\n"
    message += '\n*Current positions with last hour changes:*\n'
    for pos in account.positions:
        message += f"\n*{pos.ticker} {pos.direction} {pos.leverage} {pos.leverage_type}*"
        message += f"\nVolume: ${format_number(pos.volume)}"
        if pos.is_new:
            message += " 🆕"
        elif pos.delta != 0:
            sign = '+' if pos.delta > 0 else '-'
            message += f" _({sign}${format_number(abs(pos.delta))})_ 👈"
        message += f"\nEntry Price: {pos.entry_price}"
        sign = '+' if pos.pnl > 0 else '-'
        message += f"\nP&L: {sign}${format_number(abs(pos.pnl))}\n"
    for closed_ticker in account.closed_positions:
        message += f"\n*{closed_ticker} Position Closed* ❌\n"
    message += '\n' + TRADER_URL + wallet
    return message


def edit_message(message_ids: list[MessageId], text: str):
    for message_id in message_ids:
        try:
            bot.edit_message_text(chat_id=message_id.chat_id, message_id=message_id.message_id, text=text)
        except Exception as e:
            logger.error(f"exception while editing message: {e}")


def worker():
    global last_updated, accounts
    while True:
        try:
            for wallet, account in accounts.items():
                new_positions = fetch_open_positions(wallet)
                if account.positions is None:
                    account.positions = new_positions
                elif new_positions != account.positions:
                    last_trade = fetch_last_trade(wallet)
                    account.update(last_trade, new_positions)
                    message = on_change_message(wallet, account)
                    if account.need_new_message:
                        account.message_ids = send_everyone(message)
                    elif account.last_message != message:
                        edit_message(account.message_ids, message)
                    account.last_message = message
                    sleep(MESSAGE_UPDATE_TIME)
                sleep(0.2)
            last_updated = datetime.now()
        except Exception as e:
            logger.error(f"exception in worker: {e}")
            sleep(10)


def on_exit(signum, frame):
    send_everyone('Bot is shutting down')
    save_wallets(accounts)
    raise SystemExit('terminating')


atexit.register(save_wallets, accounts)
signal.signal(signal.SIGTERM, on_exit)

send_everyone('Bot restarted')

threading.Thread(target=worker, daemon=True).start()

while True:
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        logger.error(f"exception while polling: {e}")
        sleep(5)
