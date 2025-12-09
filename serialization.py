from models import Account


WALLETS_FILE = 'wallets.txt'


def load_accounts() -> dict[str, Account]:
    accounts = {}
    with open(WALLETS_FILE, 'r') as f:
        for line in f:
            wallet, tag = line.strip().split(';')
            if wallet:
                accounts[wallet] = Account(tag)
    return accounts


def save_wallets(accounts: dict[str, Account]):
    with open(WALLETS_FILE, 'w') as f:
        f.write('\n'.join(f"{wallet};{account.tag if account.tag else ''}" for wallet, account in accounts.items()))