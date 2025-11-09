import requests


API_URL = 'https://api.hyperliquid.xyz/info'


def format_position(pos):
    return {
        'ticker': pos['position']['coin'],
        'size': pos['position']['szi'],
        'leverage': pos['position']['leverage']['value'],
        'leverage_type': pos['position']['leverage']['type'],
        'direction': 'Short' if pos['position']['szi'][0] == '-' else 'Long',
        'entry_price': pos['position']['entryPx'],
    }


def fetch_open_positions(wallet):
    payload = {
        "type": "clearinghouseState",
        "user": wallet
    }
    r = requests.post(API_URL, json=payload)
    data = r.json()['assetPositions']
    return list(map(format_position, data))


def fetch_last_trade(wallet):
    payload = {
        "type": "userFills",
        "user": wallet,
        "aggregateByTime": True
    }
    r = requests.post(API_URL, json=payload)
    data = r.json()[0]
    return {
        'ticker': data['coin'],
        'price': data['px'],
        'size': data['sz'],
        'action': data['dir'],
    }