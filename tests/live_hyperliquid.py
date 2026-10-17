"""Opt-in public mainnet compatibility check; never uses an exchange key."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import parse_perp_dexs
from data_fetcher import HyperliquidClient
from format import on_change_message
from models import Account
from serialization import normalize_wallet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wallet', required=True, type=normalize_wallet)
    parser.add_argument('--dexs', default='main', type=parse_perp_dexs)
    arguments = parser.parse_args()
    client = HyperliquidClient(perp_dexs=arguments.dexs)
    try:
        positions = client.fetch_open_positions(arguments.wallet)
        trade = client.fetch_last_trade(arguments.wallet)
        print(f'clearinghouseState: OK; {len(positions)} open positions')
        print(f'userFills + aggregateByTime: OK; latest perpetual fill: {trade.ticker if trade else "none"}')
        if trade is not None:
            text = on_change_message(arguments.wallet, Account(positions=positions, last_trade=trade))
            assert len(text.encode('utf-16-le')) // 2 <= 4096
            print('Telegram alert rendering: OK')
        return 0
    finally:
        client.close()


if __name__ == '__main__':
    raise SystemExit(main())
