import requests
from format import format_position, format_trade
from models import Position, Trade


API_URL = 'https://api.hyperliquid.xyz/info'


def fetch_open_positions(wallet: str) -> list[Position]:
    payload = {
        "type": "clearinghouseState",
        "user": wallet
    }
    r = requests.post(API_URL, json=payload)
    data = r.json()['assetPositions']
    return list(map(format_position, data))


def fetch_last_trade(wallet: str) -> Trade:
    payload = {
        "type": "userFills",
        "user": wallet,
        "aggregateByTime": True
    }
    r = requests.post(API_URL, json=payload)
    data = r.json()[0]
    return format_trade(data)