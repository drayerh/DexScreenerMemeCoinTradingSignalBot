# DexSignal — Specification

Version 1.2 · Status: implemented. Changes from 1.0 follow the expert review in [REVIEW.md](REVIEW.md); 1.2 adds the watchlist and extra discovery feeds after the first live run (section 8).

## 1. Purpose

DexSignal is a **private research tool**. It samples newly profiled tokens
from DexScreener, screens out obvious scams, stores a price and liquidity
history, and reports defined market observations (sharp rises, sharp falls,
liquidity collapses) to its owner.

It is not a trading bot and gives no buy or sell advice. It places no orders,
holds no keys and moves no funds.

## 2. Non-goals

- Executing trades or connecting to wallets.
- Publishing signals to public channels. Notifications go to one private chat.
- Funding-source (mixer) tracing of deployers. Needs transaction-graph data (REVIEW finding 21).
- Machine-learning classifiers before enough labelled data exists (REVIEW finding 22).

## 3. External contracts

All base URLs are configurable. Shapes below are taken from the providers'
published documentation; fields the documentation leaves unspecified are
parsed defensively (missing or malformed values reject the record, never
crash the run).

| Provider | Endpoint | Used fields |
| --- | --- | --- |
| DexScreener | `GET /token-profiles/latest/v1`, `/token-boosts/latest/v1`, `/token-boosts/top/v1` (documented limit 60 req/min) | `chainId`, `tokenAddress` |
| DexScreener | `GET /tokens/v1/{chainId}/{addresses}` (comma-separated) | `chainId`, `dexId`, `pairAddress`, `baseToken.address`, `baseToken.symbol`, `priceUsd` (string), `liquidity.usd`, `volume.h24`, `pairCreatedAt` |
| RugCheck | `GET /v1/tokens/{mint}/report/summary` (Solana mints only) | `score_normalised`, `risks[].level`, `risks[].name` |
| Telegram Bot API | `POST /bot{token}/sendMessage` | `ok` |
| GoPlus | `GET /api/v1/token_security/{chain_id}?contract_addresses=` (EVM) | `is_honeypot`, `cannot_sell_all`, `owner_change_balance`, `hidden_owner`, `transfer_pausable`, `can_take_back_ownership`, `is_airdrop_scam`, `fake_token`, `selfdestruct`, `sell_tax`, `buy_tax`, `honeypot_with_same_creator`, `creator_address`, `holders[]` |
| GoPlus | `GET /api/v1/solana/token_security?contract_addresses=` | `mintable`, `freezable`, `balance_mutable_authority`, `closable`, `creators[].malicious_address`, `holders[]`, `dex[].burn_percent` |
| Etherscan V2 | `GET /v2/api?module=contract&action=getcontractcreation` (key required) | `result[].contractCreator` |
| Pocket Universe | `/rug_check/{api_key}` (method and parameter configurable; undocumented) | `status`, `is_scam`, `message`, `rug_percent`, `fresh_percent` |

GoPlus flags are strings `"1"`/`"0"`. Anything else is **unknown**, never safe.

## 4. Functional requirements

| ID | Requirement |
| --- | --- |
| FR-1 Discovery | Each cycle fetches every feed in `DSB_DISCOVERY_FEEDS` (default: latest profiles, latest boosts, top boosts) and de-duplicates by `(chainId, tokenAddress)`. A failed feed is skipped; discovery fails only if all feeds fail. |
| FR-2 Market data | Pair data is fetched per chain in batches of at most `DSB_BATCH_SIZE` addresses (default 30). For each token the pair with the highest USD liquidity is kept. |
| FR-3 Liquidity filter | Tokens with liquidity below `DSB_MIN_LIQUIDITY_USD` (default 50,000) are skipped. |
| FR-4 Blacklist | Blacklisted tokens (by chain and address) are skipped. The blacklist is managed from the CLI. |
| FR-5 Volume sanity | A token is flagged `suspicious_volume` and skipped when 24h volume exceeds `DSB_MAX_VOLUME_TO_LIQUIDITY` × liquidity (default 50). This is a heuristic for wash trading, not proof. |
| FR-6 Rug screening | For Solana tokens, RugCheck's summary is fetched. A token is rejected if any risk has level `danger`, or if `score_normalised` exceeds `DSB_RUGCHECK_MAX_SCORE` (default 50; higher means riskier per RugCheck's scale; confirm against RugCheck's current documentation). Results are cached for `DSB_SCREEN_TTL_MINUTES` (default 60). Non-Solana tokens are recorded as `unscreened` and still stored. If RugCheck is unreachable the token is `unscreened` for this cycle, never treated as safe. |
| FR-7 History | Every accepted token produces a new snapshot row per cycle. History is never overwritten. Rows older than `DSB_RETENTION_DAYS` (default 30) are pruned. |
| FR-8 Observations | Using snapshots inside a trailing window of `DSB_WINDOW_MINUTES` (default 30), ordered oldest to newest, with at least `DSB_MIN_POINTS` (default 3) points: `RISE` when price change ≥ `DSB_RISE_PCT` (default +100%); `FALL` when price change ≤ −`DSB_FALL_PCT` (default 50%); `LIQUIDITY_DROP` when liquidity change ≤ −`DSB_LIQ_DROP_PCT` (default 50%). |
| FR-9 De-duplication | The same observation kind for the same token is not reported again within `DSB_ALERT_COOLDOWN_MINUTES` (default 360). Every report is stored. |
| FR-10 Notification | If Telegram credentials are set, each new observation is sent to one chat, labelled as a research observation and not financial advice. Without credentials, observations are logged only. Notification failure never stops the cycle. |
| FR-11 CLI | `run`, `once`, `stats`, `export <table>`, `blacklist add/remove/list`, `deployer block/unblock/list`, `evaluate [--json]`, `health [--max-age-minutes]`, `probe [all | pocket <mint>]`. |
| FR-12 Risk providers | Each applicable provider assesses a new token; results are appended to `risk_checks` with a timestamp (point-in-time) and reused within `DSB_SCREEN_TTL_MINUTES`. Unscreened results are reused only within `DSB_NEGATIVE_TTL_MINUTES` (default 10), except creator-only lookups, which use the full TTL. Cheap local filters (FR-3 to FR-5) run first. |
| FR-13 Verdict | Any REJECTED wins; otherwise PASSED if any provider passed; otherwise UNSCREENED. With `DSB_STRICT_SCREENING=true`, UNSCREENED is rejected. |
| FR-14 Contract risk | GoPlus rejects EVM tokens with any danger flag, buy or sell tax above `DSB_MAX_TAX_PCT` (default 10%), or a creator with prior honeypots; Solana tokens with active mint, freeze, balance-mutation or close authority, or a malicious creator. A missing honeypot or authority status is UNSCREENED. |
| FR-15 Concentration | Top-10 holder share, excluding locked holders, the pair address, burn addresses and pool or locker tags, above `DSB_MAX_TOP10_PCT` (default 50%) rejects the token. |
| FR-16 Deployers | Creators from any provider are linked to tokens. A new token is rejected if its deployer is manually blocked, flagged malicious, or linked to at least `DSB_DEPLOYER_MAX_RUGS` (default 2) labelled rugs. Etherscan is queried only when no other provider returned a creator. |
| FR-17 Tracking and labels | Accepted tokens are sampled every cycle until the longest horizon plus its tolerance has passed (at least `DSB_TRACK_HOURS`), and while any observation on them still needs outcome data, whatever their liquidity. A tracked token missing from a successful lookup is recorded as a zero snapshot (vanished pair); one in a failed batch is skipped. Tracked tokens that are blacklisted, deployer-blocked or rejected are sampled but raise no observations; unscreened ones are re-screened. Each observation gets forward price and liquidity changes at +1h, +6h and +24h (gaps recorded) and a post-observation rug flag. Each live token gets a rug label (liquidity falling `DSB_RUG_LIQ_DROP_PCT`, default 80%, from its peak within 24h) and forward returns from first sighting. Tokens migrated from v1 are excluded. |
| FR-19 Watchlist | A discovered token that is not yet tradeable — no pool yet (pairs listed without liquidity, such as bonding curves), no pair data, a failed lookup, low liquidity or suspicious volume — is kept on a watchlist and rechecked every cycle for `DSB_WATCH_HOURS` (default 6; 0 disables), newest first, at most `DSB_WATCH_MAX_TOKENS` (default 1000) per cycle. It goes through all filters each time. When accepted it leaves the watchlist and is tracked from that moment (`first_seen`), with its discovery time kept in `discovered_at`. Any other rejection removes it. Tokens already known are not re-watched. |
| FR-18 Evaluation | `evaluate` reports, for accepted tokens from first sighting (baseline) and each observation kind: sample size, coverage of matured outcomes, median forward change, share up with Wilson 95% interval, and rug rate (per observation, after the observation only); horizons and groups under 30 are flagged; the age difference between baseline and observations is stated. |

## 5. Non-functional requirements

| ID | Requirement |
| --- | --- |
| NFR-1 Resilience | Every HTTP call has connect and read timeouts. 429 and 5xx responses are retried with exponential backoff, honouring `Retry-After`. A failure in one token, batch or provider never aborts the cycle. |
| NFR-2 Rate limits | Requests to each host are spaced to stay under `DSB_MAX_REQUESTS_PER_MINUTE` (default 50, below DexScreener's documented 60). |
| NFR-3 Secrets | Secrets come only from the environment (optionally a local `.env`). None are logged or committed. `.env` and databases are git-ignored. |
| NFR-4 Shutdown | SIGINT and SIGTERM finish the current cycle step and exit cleanly. |
| NFR-5 Observability | Structured log lines per cycle: tokens discovered, accepted, rejected by reason, snapshots written, observations raised, duration. |
| NFR-6 Quality gates | `ruff` clean, `mypy --strict` clean, test coverage ≥ 90%, CI on Python 3.10 to 3.13. |
| NFR-7 Storage | SQLite in WAL mode with indexes on `(chain_id, token_address, observed_at)`. Schema is versioned with additive migrations (v1 to v2 to v3). |
| NFR-8 Redaction | Every configured secret is removed from every log line, whatever its source. |
| NFR-9 Liveness | Each cycle records a heartbeat; `health` exits non-zero when the last successful cycle is older than the limit. |
| NFR-10 Supply chain | `pip-audit` in CI; Dependabot for Python packages and GitHub Actions; nightly provider probe. |

## 6. Data model

- `snapshots(id, chain_id, token_address, pair_address, dex_id, symbol, price_usd, liquidity_usd, volume_h24, observed_at)`
- `screenings(chain_id, token_address, status, reason, checked_at)` — latest result per token.
- `observations(id, chain_id, token_address, symbol, kind, change_pct, window_minutes, observed_at, notified)`
- `blacklist(chain_id, token_address, reason, added_at)`
- `meta(key, value)` — holds `schema_version` and heartbeat times.
- `tokens(chain_id, token_address, symbol, first_seen, source, discovered_at)` — first accepted sighting (drives tracking) and first discovery.
- `watchlist(chain_id, token_address, first_seen, last_checked, last_reason, checks)` — young tokens awaiting a pool or liquidity (v3).
- `risk_checks(id, chain_id, token_address, provider, status, reasons, features, creators, checked_at)` — append-only.
- `deployers(chain_id, address, first_seen, malicious, manual_block, reason)` and `token_deployers(chain_id, token_address, deployer, source, first_seen)`.
- `observation_outcomes(observation_id, horizon_minutes, price_change_pct, liquidity_change_pct, measured_at)`.
- `token_labels(chain_id, token_address, first_seen, peak_liquidity_usd, trough_after_peak_usd, rugged, ret_60, ret_360, ret_1440, labelled_at)`.

## 7. Acceptance criteria

Each maps to automated tests in `tests/`.

1. A token whose price doubles across cycles produces exactly one `RISE` observation within the cooldown (FR-7, FR-8, FR-9).
2. Price history is kept: N cycles write N rows per token (FR-7).
3. Observations are computed oldest to newest; a falling series never produces `RISE` (FR-8).
4. A malformed record, a failed batch, or an unreachable RugCheck does not stop the cycle (NFR-1, FR-6).
5. Blacklisted, illiquid, suspicious-volume and `danger`-rated tokens are never stored (FR-3 to FR-6).
6. Requests are batched per chain at the configured size (FR-2).
7. Notification failures are logged and the observation is still stored (FR-10).
8. Missing Telegram credentials disable notification without errors (FR-10).
9. A collapsing tracked token is still sampled below the liquidity floor and receives a rug label (FR-17).
10. Missing or unknown provider flags never produce PASSED (FR-14).
11. A deployer with manual block, malicious flag or enough labelled rugs blocks new tokens (FR-16).
12. Secrets never appear in logs (NFR-8).
13. A v1 database migrates to v2 without data loss; backfilled tokens are excluded from labels (NFR-7).
14. The +24h horizon is measured under realistic, drifting cycle timing (FR-17).
15. A vanished pair is labelled as a rug; a failed batch is not (FR-17).
16. Blocks applied after acceptance stop observations but not sampling (FR-17).
17. A token discovered before it has a pool is accepted once it qualifies, even after leaving the discovery feed, and keeps its discovery time (FR-19).
18. Discovery merges all configured feeds and survives one failing (FR-1).
19. A pair listed without liquidity is reported as `no_pool_yet`, not `no_pair_data` (FR-19).

## 8. Live run findings (v1.2)

The first live run (9 cycles, 1 November 2026) accepted no tokens. Every
discovered token was either still on a pump.fun bonding curve (DexScreener lists
the pair with no liquidity) or below the $50k floor. The latest-profiles feed
shows tokens in their first minutes, so a token that later qualifies has
already left the feed. FR-19 (watchlist) and the boost feeds in FR-1 address
this without lowering the liquidity floor.

Note for evaluation: `first_seen` is now the moment a token was accepted. For
graduated tokens this is later than discovery; the baseline therefore measures
tokens from the moment they became tradeable, as before.
