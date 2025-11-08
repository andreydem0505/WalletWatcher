import os
from time import sleep
import threading
import telebot
from prettytable import PrettyTable
from datetime import datetime
from pytz import timezone
from dotenv import load_dotenv
import atexit
import requests
import logging


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', filename='logs', filemode='w')
logger = logging.getLogger(__name__)


load_dotenv()

TOKEN = os.getenv('BOT_TOKEN')

# List of users allowed to interact with the bot
CHAT_IDS = list(map(int, os.getenv('CHAT_IDS').split(',')))

TIMEZONE = timezone('Europe/Moscow')

bot = telebot.TeleBot(TOKEN, parse_mode='Markdown')

# Time of the last update of all wallets positions
last_updated = datetime.now()

# Connect each wallet to its last known positions
wallet_positions = {}

WALLETS_FILE = 'wallets.txt'


# Load wallets from file while starting up
with open(WALLETS_FILE, 'r') as f:
    for line in f:
        wallet = line.strip()
        if wallet:
            wallet_positions[wallet] = []


def add_wallet(chat_id, wallet):
    if wallet not in wallet_positions:
        wallet_positions[wallet] = []
        send_everyone(f'Wallet {wallet} added')
    else:
        bot.send_message(chat_id, f'Wallet {wallet} is already being tracked')


def remove_wallet(chat_id, wallet):
    if wallet in wallet_positions:
        del wallet_positions[wallet]
        send_everyone(chat_id, f'Wallet {wallet} removed')
    else:
        bot.send_message(chat_id, f'Wallet {wallet} is not being tracked')


@bot.message_handler(func=lambda m: True)
def reply(m):

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
        message += '\n'.join(wallet_positions.keys())
        bot.send_message(m.chat.id, message)


# Convert data from API to a more convenient format
def format_position(pos):
    return {
        'ticker': pos['position']['coin'],
        'size': pos['position']['szi'],
        'leverage': pos['position']['leverage']['value'],
        'leverage_type': pos['position']['leverage']['type'],
        'direction': 'Short' if pos['position']['szi'][0] == '-' else 'Long',
        'entry_price': pos['position']['entryPx'],
    }


# Format positions as a table
def table(positions):
    t = PrettyTable(['Ticker', 'Direction', 'Leverage', 'Leverage Type', 'Size', 'Entry Price'])
    for pos in positions:
        t.add_row([pos['ticker'], pos['direction'], pos['leverage'], pos['leverage_type'], pos['size'], pos['entry_price']])
    t.align = 'l'
    return t


def send_everyone(message):
    try:
        for chat_id in CHAT_IDS:
            bot.send_message(chat_id, message)
    except Exception as e:
        logger.error(f"exception while sending message: {e}")


def worker():
    global last_updated, wallet_positions
    while True:
        try:
            for wallet, positions in wallet_positions.items():
                payload = {
                    "type": "clearinghouseState",
                    "user": wallet
                }
                r = requests.post('https://api.hyperliquid.xyz/info', json=payload)
                data = r.json()['assetPositions']
                new_positions = list(map(format_position, data))
                if positions == []:
                    wallet_positions[wallet] = new_positions
                elif new_positions != positions:
                    t = table(new_positions)
                    message = f'*Positions changed!*\n`{t}`\nhttps://hyperdash.info/trader/{wallet}'
                    send_everyone(message)
                    wallet_positions[wallet] = new_positions
                sleep(1)
            last_updated = datetime.now()
        except Exception as e:
            logger.error(f"exception in worker: {e}")


# Save wallets to file on exit
def save_wallets():
    try:
        with open(WALLETS_FILE, 'w') as f:
            f.write('\n'.join(wallet_positions.keys()))
    except Exception as e:
        logger.error(f"exception while saving wallets: {e}")


atexit.register(save_wallets)
threading.Thread(target=worker, daemon=True).start()
while True:
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        logger.error(f"exception while polling: {e}")
        sleep(5)
