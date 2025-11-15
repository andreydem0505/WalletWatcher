from models import Position


WALLETS_FILE = 'wallets.txt'


def load_wallets() -> dict[str, None]:
    wallet_positions = {}
    with open(WALLETS_FILE, 'r') as f:
        for line in f:
            wallet = line.strip()
            if wallet:
                wallet_positions[wallet] = None
    return wallet_positions


def save_wallets(wallet_positions: dict[str, list[Position]]):
    with open(WALLETS_FILE, 'w') as f:
        f.write('\n'.join(wallet_positions.keys()))
