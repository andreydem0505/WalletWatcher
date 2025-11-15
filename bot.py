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
from models import Position, Trade
from serialization import load_wallets, save_wallets
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

bot = telebot.TeleBot(TOKEN, parse_mode='Markdown')

# Time of the last update of all wallets positions
last_updated = datetime.now()

wallet_positions = load_wallets()


def add_wallet(chat_id: int, wallet: str):
    if wallet not in wallet_positions:
        wallet_positions[wallet] = None
        send_everyone(f'Wallet {wallet} added')
    else:
        bot.send_message(chat_id, f'Wallet {wallet} is already being tracked')


def remove_wallet(chat_id: int, wallet: str):
    if wallet in wallet_positions:
        del wallet_positions[wallet]
        send_everyone(chat_id, f'Wallet {wallet} removed')
    else:
        bot.send_message(chat_id, f'Wallet {wallet} is not being tracked')


@bot.message_handler(func=lambda m: True)
def reply(m: telebot.types.Message):

    # ignore messages from unauthorized users
    if m.chat.id not in CHAT_IDS:
        return
    
    if m.text.startswith('/addwallet '):
        wallet = m.text.split(' ')[1]
        add_wallet(m.chat.id, wallet)

    elif m.text.startswith('/removewallet '):
        wallet = m.text.split(' ')[1]
        remove_wallet(m.chat.id, wallet)

    else:
        message = f'Last updated: {str(last_updated.astimezone(TIMEZONE))}\n\n'
        message += 'Tracked wallets:\n'
        message += '\n'.join(map(lambda x: f"`{x}`", wallet_positions.keys()))
        bot.send_message(m.chat.id, message)


def send_everyone(message: str):
    if MODE == 'TEST':
        bot.send_message(ADMIN_ID, message)
        return
    for chat_id in CHAT_IDS:
        try:
            bot.send_message(chat_id, message)
        except Exception as e:
            logger.error(f"exception while sending message: {e}")
            continue


def on_change_message(wallet: str, positions: list[Position], last_trade: Trade) -> str:
    volume = int(float(last_trade.size) * float(last_trade.price))
    message = f"❗️ *{last_trade.ticker} {last_trade.action}* ❗️"
    message += f"\nVolume: ${format_number(volume)}"
    message += f"\nPrice: {last_trade.price}\n"
    message += '\n*Current positions:*\n'
    for pos in positions:
        message += f"\n*{pos.ticker} {pos.direction} {pos.leverage} {pos.leverage_type}*"
        message += f"\nVolume: ${format_number(pos.volume)}"
        message += f"\nEntry Price: {pos.entry_price}\n"
    message += f'\nhttps://hyperdash.info/trader/{wallet}'
    return message


def worker():
    global last_updated, wallet_positions
    while True:
        try:
            for wallet, positions in wallet_positions.items():
                new_positions = fetch_open_positions(wallet)
                if positions is None:
                    wallet_positions[wallet] = new_positions
                elif new_positions != positions:
                    last_trade = fetch_last_trade(wallet)
                    message = on_change_message(wallet, new_positions, last_trade)
                    send_everyone(message)
                    wallet_positions[wallet] = new_positions
                sleep(1)
            last_updated = datetime.now()
        except Exception as e:
            logger.error(f"exception in worker: {e}")


def on_exit(signum, frame):
    send_everyone('Bot is shutting down')
    save_wallets(wallet_positions)
    raise SystemExit('terminating')


atexit.register(save_wallets, wallet_positions)
signal.signal(signal.SIGTERM, on_exit)

send_everyone('Bot restarted')

threading.Thread(target=worker, daemon=True).start()

while True:
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        logger.error(f"exception while polling: {e}")
        sleep(5)
