# Hyperliquid Wallets Monitor 🐳

### This should be considered an MVP. Any contribution is appreciated. ⭐️

The bot monitors new trades of specified Hyperliquid accounts and sends alerts when detected.

![Example of the alert](message.png)

### Message Structure

When the bot detects a new trade, it sends an alert with the following information:

- **Trade Header**: Shows the ticker symbol and action (e.g., "BTC LONG" or "ETH SHORT")
- **Wallet/Tag**: Displays the wallet address or custom tag if set
- **Trade Details**:
  - Volume: Total trade volume in USD
  - Price: Execution price
- **Current Positions**: Lists all open positions for the wallet
  - Ticker, direction, leverage, and leverage type
  - Position volume in USD
  - Entry price
  - Delta (profit/loss) if position changed (marked with 👈)
- **Closed Positions**: Shows any positions that were closed (marked with ❌)
- **Trader Link**: Direct link to view the wallet on Hyperdash

### Setup instructions:

1. **Create a Telegram Bot**  
   Use [@BotFather](https://telegram.me/botfather) to create a new bot. Add the received token to the `.env` file.

2. **Configure Access**  
   Add Telegram IDs of authorized users to the `.env` file. These can be your friends or community members. These users can manage monitored wallets and interact with the bot. Other users will be ignored.

3. **Initialize wallets.txt**  
   Create an empty `wallets.txt` file to store Hyperliquid addresses for monitoring. The bot reads this file on startup and updates it before shutdown.

4. **Manage Wallets**  
   Authorized users can:  
   - Add wallets: `/addwallet 0x...`  
   - Remove wallets: `/removewallet 0x...`  
   - Set wallet tag: `/settag 0x... <tag_name>`  
   - Check status: Send any message to get last update time and monitored wallets list.

### Environment Configuration (.env)
```bash
BOT_TOKEN=    # Token from BotFather
CHAT_IDS=     # Comma-separated list of authorized Telegram IDs
ADMIN_ID=     # Admin Telegram ID
MODE=         # TEST (local testing) or PROD (production)
```

### Deployment
Run the bot as a service using `systemctl` for continuous monitoring.