# DexScreenerMemeCoinTradingSignalBot
# MemeCoinTradingBot

MemeCoinTradingBot is a Python-based trading bot designed to monitor and analyze meme coins on decentralized exchanges. It uses various APIs to fetch token data, verify volume, check for rugpulls, and send alerts via Telegram.

## Features

- Fetches token data from DexScreener API
- Filters tokens based on liquidity and blacklist
- Verifies token volume using Pocket Universe API
- Checks for rugpulls using RugCheck API
- Saves token snapshots to SQLite database
- Analyzes token price pumps
- Sends alerts via Telegram

## Requirements

- Python 3.7+
- `requests` library
- `pandas` library
- `python-telegram-bot` library
- SQLite

## Installation

1. Clone the repository:
    ```sh
    git clone https://github.com/drayerh/MemeCoinTradingBot.git
    cd MemeCoinTradingBot
    ```

2. Install the required Python packages:
    ```sh
    pip install -r requirements.txt
    ```

3. Create a `.env` file in the project directory with the following content:
    ```dotenv
    TELEGRAM_TOKEN="your_telegram_token"
    CHAT_ID="your_chat_id"
    POCKET_UNIVERSE_API="https://api.pocketuniverse.ai/v1/verify"
    RUGCHECK_API="https://api.rugcheck.xyz/v1/token"
    DEXSCREENER_API="https://api.dexscreener.com/latest/dex"
    ```

## Usage

1. Run the bot:
    ```sh
    python main.py
    ```

2. The bot will run in a loop, fetching and analyzing token data every 5 minutes.

## Configuration

- `MIN_LIQUIDITY`: Minimum liquidity threshold for filtering tokens (default: 50000 USD)
- `BLACKLIST_DB`: SQLite database file for storing blacklisted coins and developers (default: `blacklists.db`)
- `TOKEN_DB`: SQLite database file for storing token snapshots (default: `token_data.db`)

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.