import requests
import sqlite3
import time
import pandas as pd
from datetime import datetime
import telegram
from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext
import os

# Configuration
DEXSCREENER_API = os.getenv("DEXSCREENER_API")
POCKET_UNIVERSE_API = os.getenv("POCKET_UNIVERSE_API")
RUGCHECK_API = os.getenv("RUGCHECK_API")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
MIN_LIQUIDITY = 50000  # $50k
BLACKLIST_DB = "blacklists.db"
TOKEN_DB = "token_data.db"


class DexScreenerBot:
    """
    A bot to monitor and analyze meme coins on decentralized exchanges.
    """

    def __init__(self):
        """
        Initializes the DexScreenerBot instance, sets up the HTTP session and database connections.
        """
        self.session = requests.Session()
        self.engine = self.create_db_engine()
        self.initialize_databases()

    def create_db_engine(self):
        """
        Creates a connection to the SQLite database for storing token snapshots.

        Returns:
            sqlite3.Connection: SQLite connection object.
        """
        return sqlite3.connect(TOKEN_DB)

    def initialize_databases(self):
        """
        Initializes the SQLite databases for blacklisted coins, developers, and token snapshots.
        """
        with sqlite3.connect(BLACKLIST_DB) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS coin_blacklist
                         (address TEXT PRIMARY KEY)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS dev_blacklist
                         (wallet TEXT PRIMARY KEY)''')

        with self.engine as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS token_snapshots
                         (address TEXT PRIMARY KEY,
                          symbol TEXT,
                          price REAL,
                          liquidity REAL,
                          volume REAL,
                          timestamp DATETIME)''')

    def fetch_dex_data(self):
        """
        Fetches token data from the DexScreener API.

        Returns:
            list: A list of token data dictionaries.
        """
        try:
            response = self.session.get(f"{DEXSCREENER_API}/tokens?limit=100")
            response.raise_for_status()
            return response.json()['pairs']
        except Exception as e:
            print(f"API Error: {e}")
            return []

    def filter_tokens(self, tokens):
        """
        Filters tokens based on liquidity and blacklist status.

        Args:
            tokens (list): A list of token data dictionaries.

        Returns:
            list: A list of filtered token data dictionaries.
        """
        filtered = []
        with sqlite3.connect(BLACKLIST_DB) as conn:
            blacklisted_coins = set(row[0] for row in conn.execute("SELECT address FROM coin_blacklist"))
            blacklisted_devs = set(row[0] for row in conn.execute("SELECT wallet FROM dev_blacklist"))

        for token in tokens:
            if (token['baseToken']['address'] not in blacklisted_coins and
                    token['dexId'] not in blacklisted_devs and
                    token['liquidity'] and
                    token['liquidity']['usd'] > MIN_LIQUIDITY):
                filtered.append({
                    'address': token['baseToken']['address'],
                    'symbol': token['baseToken']['symbol'],
                    'price': token['priceUsd'],
                    'liquidity': token['liquidity']['usd'],
                    'volume': token['volume']['h24']
                })
        return filtered

    def verify_volume(self, token):
        """
        Verifies the volume of a token using internal analysis and the Pocket Universe API.

        Args:
            token (dict): A dictionary containing token data.

        Returns:
            bool: True if the token volume is verified, False otherwise.
        """
        # Internal volume analysis
        volatility = pd.Series(token['historicalPrices']).pct_change().std()
        if volatility > 2.0:
            return False

        # Pocket Universe verification
        try:
            response = self.session.post(POCKET_UNIVERSE_API,
                                         json={'address': token['address']},
                                         headers={'x-api-key': 'your_pocket_api_key'})
            return response.json().get('authentic', False)
        except Exception as e:
            print(f"Volume verification failed: {e}")
            return False

    def check_rugpull(self, address):
        """
        Checks if a token is a rugpull using the RugCheck API.

        Args:
            address (str): The token address.

        Returns:
            bool: True if the token is not a rugpull, False otherwise.
        """
        try:
            response = self.session.get(f"{RUGCHECK_API}/{address}")
            data = response.json()
            return data.get('rating') == 'Good' and not data.get('isBundled')
        except Exception as e:
            print(f"Rugcheck failed: {e}")
            return False

    def save_snapshot(self, token):
        """
        Saves a snapshot of the token data to the SQLite database.

        Args:
            token (dict): A dictionary containing token data.
        """
        with self.engine as conn:
            conn.execute('''INSERT OR REPLACE INTO token_snapshots
                          VALUES (?, ?, ?, ?, ?, ?)''',
                         (token['address'], token['symbol'], token['price'],
                          token['liquidity'], token['volume'], datetime.utcnow()))

    def analyze_pumps(self):
        """
        Analyzes token price pumps from the token snapshots.

        Returns:
            list: A list of dictionaries containing pumped token data.
        """
        with self.engine as conn:
            df = pd.read_sql('''SELECT * FROM token_snapshots
                              ORDER BY timestamp DESC LIMIT 1000''', conn)

        df['price_change'] = df.groupby('address')['price'].pct_change(periods=5)
        pumped = df[df['price_change'] > 1.0]  # 100%+ increase in 5 periods

        return pumped.to_dict('records')

    def send_telegram_alert(self, signal):
        """
        Sends an alert to Telegram with the token signal data.

        Args:
            signal (dict): A dictionary containing the token signal data.
        """
        bot = telegram.Bot(token=TELEGRAM_TOKEN)
        message = f"🚨 {signal['type'].upper()} ALERT\n" \
                  f"Token: {signal['symbol']}\n" \
                  f"Price: ${signal['price']:.6f}\n" \
                  f"Volume: ${signal['volume']:,.2f}\n" \
                  f"Liquidity: ${signal['liquidity']:,.2f}"

        bot.send_message(chat_id=CHAT_ID, text=message)

    def run_cycle(self):
        """
        Runs a single cycle of fetching, filtering, verifying, and analyzing tokens, then sends alerts for pumped tokens.
        """
        tokens = self.fetch_dex_data()
        filtered = self.filter_tokens(tokens)

        for token in filtered:
            if (self.verify_volume(token) and
                    self.check_rugpull(token['address'])):
                self.save_snapshot(token)

        pumped_tokens = self.analyze_pumps()
        for token in pumped_tokens:
            self.send_telegram_alert({
                'type': 'SELL' if token['price_change'] < -0.2 else 'BUY',
                **token
            })


if __name__ == "__main__":
    bot = DexScreenerBot()
    while True:
        bot.run_cycle()
        time.sleep(300)  # Run every 5 minutes