# DexSignal

A private research tool that watches newly profiled tokens on
[DexScreener](https://dexscreener.com), screens out obvious scams, keeps a
price and liquidity history, and reports defined market observations to you.

> **Not financial advice.** DexSignal reports observations for private
> research. It does not trade, hold keys, or move funds, and it should not be
> used to broadcast buy or sell calls to others.

## What it does each cycle

1. Fetches the latest token profiles from DexScreener.
2. Looks up market data per chain in batches and keeps each token's most liquid pair.
3. Skips tokens that are blacklisted, illiquid (below $50k), or show
   suspicious volume (24h volume above 50 × liquidity).
4. Screens Solana tokens with RugCheck. Any `danger`-level risk or a high risk
   score rejects the token. Other chains are stored as "unscreened".
5. Stores a snapshot. History is never overwritten.
6. Over a 30-minute window, raises an observation when price rises 100% or
   more (`RISE`), falls 50% or more (`FALL`), or liquidity falls 50% or more
   (`LIQUIDITY_DROP`).
7. Sends each new observation to your private Telegram chat, if configured.
   The same observation is not repeated within 6 hours.

Every threshold is configurable. The full behaviour, API contracts and
acceptance criteria are in [docs/SPEC.md](docs/SPEC.md).

## Install

Requires Python 3.10 or newer.

```sh
git clone https://github.com/drayerh/DexScreenerMemeCoinTradingSignalBot.git
cd DexScreenerMemeCoinTradingSignalBot
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dotenv]'
cp .env.example .env   # then fill in the Telegram pair, if wanted
```

## Use

```sh
dexsignal run                      # run every 5 minutes until Ctrl+C
dexsignal once                     # run one cycle (exit code 1 if discovery failed)
dexsignal stats                    # database counts
dexsignal export snapshots -o snapshots.csv
dexsignal export observations      # CSV to stdout
dexsignal blacklist add solana <mint> --reason "known rug"
dexsignal blacklist list
dexsignal blacklist remove solana <mint>
```

`python main.py` still works and runs `dexsignal run`.

## Configuration

All settings are environment variables prefixed `DSB_`; see
[.env.example](.env.example) and the table in [docs/SPEC.md](docs/SPEC.md).
Telegram needs both `DSB_TELEGRAM_TOKEN` and `DSB_TELEGRAM_CHAT_ID`, or neither.
Without them, observations are logged and stored only.

## Development

```sh
pip install -e '.[dev]'
ruff check . && ruff format --check .
mypy src tests          # strict mode
pytest                  # fails below 90% coverage
```

CI runs the same checks on Python 3.10 to 3.13 for every pull request.

## Limitations

- RugCheck covers Solana only; other chains are not scam-screened.
- The volume check is a heuristic for wash trading, not proof.
- RugCheck's score scale is not documented in detail; review
  `DSB_RUGCHECK_MAX_SCORE` against RugCheck's current documentation.
- Provider APIs change. Malformed responses are skipped, not trusted.

## License

MIT. See [LICENSE.md](LICENSE.md).
