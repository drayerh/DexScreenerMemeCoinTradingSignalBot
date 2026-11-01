# DexSignal

A private research tool that watches newly profiled tokens on
[DexScreener](https://dexscreener.com), screens out obvious scams, keeps a
price and liquidity history, and reports defined market observations to you.

> **Not financial advice.** DexSignal reports observations for private
> research. It does not trade, hold keys, or move funds, and it should not be
> used to broadcast buy or sell calls to others.

## What it does each cycle

1. Fetches new tokens from DexScreener: latest profiles, latest boosts and top boosts.
   Tokens with no pool or too little liquidity yet go on a watchlist and are
   rechecked every cycle for 6 hours, so they are picked up once they qualify.
2. Looks up market data per chain in batches and keeps each token's most liquid pair.
3. Skips tokens that are blacklisted, illiquid (below $50k), or show
   suspicious volume (24h volume above 50 × liquidity).
4. Screens new tokens with every applicable risk provider:
   - **RugCheck** (Solana): danger-level risks or a high risk score.
   - **GoPlus** (EVM chains and Solana): honeypots, high taxes, hidden or
     powerful owners, active mint or freeze authority, concentrated holders.
   - **Pocket Universe** (Solana, optional): volume from known rug pullers.
   - **Deployer intelligence**: blocks tokens whose deployer you blocked, that
     GoPlus flags as malicious, or that is linked to repeated labelled rugs.
     Etherscan fills in EVM deployers when GoPlus has none.

   Missing data never counts as safe. Any rejection wins.
5. Stores a snapshot. Accepted tokens keep being sampled for 24 hours even if
   liquidity collapses, so rugs are measured rather than filtered out.
6. Over a 30-minute window, raises an observation when price rises 100% or
   more (`RISE`), falls 50% or more (`FALL`), or liquidity falls 50% or more
   (`LIQUIDITY_DROP`).
7. Sends each new observation to your private Telegram chat, if configured.
   The same observation is not repeated within 6 hours.
8. Labels outcomes: price and liquidity change 1, 6 and 24 hours after each
   observation, and whether each token rugged within 24 hours.

Every threshold is configurable. The full behaviour, API contracts and
acceptance criteria are in [docs/SPEC.md](docs/SPEC.md); the expert review
behind them is in [docs/REVIEW.md](docs/REVIEW.md).

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
dexsignal deployer block solana <wallet> --reason "serial rugger"
dexsignal deployer list            # tokens and labelled rugs per deployer
dexsignal evaluate                 # do observations beat the base rate?
dexsignal health                   # exit 1 if no successful cycle in 15 min
dexsignal probe                    # check every provider's live API
dexsignal probe pocket <mint>      # print a raw Pocket Universe response
```

`python main.py` still works and runs `dexsignal run`.

## Is it working? Read `evaluate`

`evaluate` compares each observation kind with a **baseline** of every token
from its first sighting: forward price changes at 1, 6 and 24 hours, the share
that went up (with a Wilson 95% interval), and the share that rugged within 24
hours. Groups under 30 are flagged. An observation kind is only informative if
it clearly differs from the baseline.

## Pocket Universe

Pocket Universe documents its `/rug_check/{api_key}` endpoint and response
fields, but not its HTTP method, parameter name or API host. Set
`DSB_POCKET_UNIVERSE_BASE_URL` and `DSB_POCKET_UNIVERSE_API_KEY`, run
`dexsignal probe pocket <mint>`, and confirm the response shape before relying
on it. Adjust `DSB_POCKET_UNIVERSE_METHOD` and `DSB_POCKET_UNIVERSE_PARAM` if
needed. The key is part of the URL, so it is redacted from all logs.

## Live run on GitHub

The **Live research run** workflow runs the bot against the live APIs on a
GitHub runner. Start it from the Actions tab (choose the number of cycles; up
to 60, five minutes apart). The summary shows `stats`, `evaluate` and the
deployer list; the database and CSV exports are attached to the run. Each run
starts with an empty database, so it suits checks and short samples, not the
24-hour outcome labels.

## Docker

```sh
docker build -t dexsignal .
docker run -d --name dexsignal --env-file .env -v dexsignal-data:/data dexsignal
```

The image runs as a non-root user, logs JSON, and reports health through
`dexsignal health`.

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

CI runs the same checks on Python 3.10 to 3.13 for every pull request, plus
`pip-audit`. A nightly workflow runs `dexsignal probe` against the live APIs;
add `DSB_GOPLUS_TOKEN`, `DSB_ETHERSCAN_API_KEY` and the Pocket Universe values
as repository secrets to include the keyed providers.

## Limitations

- Deployer blacklists catch careless repeat offenders; deployers who use fresh
  wallets need funding-source tracing, which is not implemented.
- The volume check is a heuristic for wash trading, not proof.
- RugCheck's score scale is not documented in detail; review
  `DSB_RUGCHECK_MAX_SCORE` against RugCheck's current documentation.
- Provider APIs change. Malformed responses are skipped, not trusted.

## License

MIT. See [LICENSE.md](LICENSE.md).
