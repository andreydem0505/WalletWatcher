from models import Account


WALLETS_FILE = 'wallets.txt'


def load_accounts() -> dict[str, Account]:
    accounts = {}
    with open(WALLETS_FILE, 'r') as file:
        for line in file:
            wallet, tag = line.strip().split(';')
            if wallet:
                accounts[wallet] = Account(tag)
    return accounts


def save_wallets(accounts: dict[str, Account]):
    with open(WALLETS_FILE, 'w') as file:
        file.write('\n'.join(f"{wallet};{account.tag if account.tag else ''}" for wallet, account in accounts.items()))