# DexSignal — Specification

Version 1.0 · Status: implemented by this branch

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
- Developer-wallet blacklisting. DexScreener does not expose deployer wallets,
  and RugCheck's full-report schema is undocumented, so a reliable mapping is
  not possible today. Token and pair blacklists are supported instead.
- Third-party "volume authenticity" services. The previous Pocket Universe
  integration was removed: its API contract could not be verified. Volume
  sanity is checked with documented internal heuristics (FR-5).

## 3. External contracts

All base URLs are configurable. Shapes below are taken from the providers'
published documentation; fields the documentation leaves unspecified are
parsed defensively (missing or malformed values reject the record, never
crash the run).

| Provider | Endpoint | Used fields |
| --- | --- | --- |
| DexScreener | `GET /token-profiles/latest/v1` (documented limit 60 req/min) | `chainId`, `tokenAddress` |
| DexScreener | `GET /tokens/v1/{chainId}/{addresses}` (comma-separated) | `chainId`, `dexId`, `pairAddress`, `baseToken.address`, `baseToken.symbol`, `priceUsd` (string), `liquidity.usd`, `volume.h24`, `pairCreatedAt` |
| RugCheck | `GET /v1/tokens/{mint}/report/summary` (Solana mints only) | `score_normalised`, `risks[].level`, `risks[].name` |
| Telegram Bot API | `POST /bot{token}/sendMessage` | `ok` |

## 4. Functional requirements

| ID | Requirement |
| --- | --- |
| FR-1 Discovery | Each cycle fetches the latest token profiles and de-duplicates them by `(chainId, tokenAddress)`. |
| FR-2 Market data | Pair data is fetched per chain in batches of at most `DSB_BATCH_SIZE` addresses (default 30). For each token the pair with the highest USD liquidity is kept. |
| FR-3 Liquidity filter | Tokens with liquidity below `DSB_MIN_LIQUIDITY_USD` (default 50,000) are skipped. |
| FR-4 Blacklist | Blacklisted tokens (by chain and address) are skipped. The blacklist is managed from the CLI. |
| FR-5 Volume sanity | A token is flagged `suspicious_volume` and skipped when 24h volume exceeds `DSB_MAX_VOLUME_TO_LIQUIDITY` × liquidity (default 50). This is a heuristic for wash trading, not proof. |
| FR-6 Rug screening | For Solana tokens, RugCheck's summary is fetched. A token is rejected if any risk has level `danger`, or if `score_normalised` exceeds `DSB_RUGCHECK_MAX_SCORE` (default 50; higher means riskier per RugCheck's scale; confirm against RugCheck's current documentation). Results are cached for `DSB_SCREEN_TTL_MINUTES` (default 60). Non-Solana tokens are recorded as `unscreened` and still stored. If RugCheck is unreachable the token is `unscreened` for this cycle, never treated as safe. |
| FR-7 History | Every accepted token produces a new snapshot row per cycle. History is never overwritten. Rows older than `DSB_RETENTION_DAYS` (default 30) are pruned. |
| FR-8 Observations | Using snapshots inside a trailing window of `DSB_WINDOW_MINUTES` (default 30), ordered oldest to newest, with at least `DSB_MIN_POINTS` (default 3) points: `RISE` when price change ≥ `DSB_RISE_PCT` (default +100%); `FALL` when price change ≤ −`DSB_FALL_PCT` (default 50%); `LIQUIDITY_DROP` when liquidity change ≤ −`DSB_LIQ_DROP_PCT` (default 50%). |
| FR-9 De-duplication | The same observation kind for the same token is not reported again within `DSB_ALERT_COOLDOWN_MINUTES` (default 360). Every report is stored. |
| FR-10 Notification | If Telegram credentials are set, each new observation is sent to one chat, labelled as a research observation and not financial advice. Without credentials, observations are logged only. Notification failure never stops the cycle. |
| FR-11 CLI | `dexsignal run` (loop), `dexsignal once`, `dexsignal export` (CSV of snapshots or observations), `dexsignal blacklist add/remove/list`, `dexsignal stats`. |

## 5. Non-functional requirements

| ID | Requirement |
| --- | --- |
| NFR-1 Resilience | Every HTTP call has connect and read timeouts. 429 and 5xx responses are retried with exponential backoff, honouring `Retry-After`. A failure in one token, batch or provider never aborts the cycle. |
| NFR-2 Rate limits | Requests to each host are spaced to stay under `DSB_MAX_REQUESTS_PER_MINUTE` (default 50, below DexScreener's documented 60). |
| NFR-3 Secrets | Secrets come only from the environment (optionally a local `.env`). None are logged or committed. `.env` and databases are git-ignored. |
| NFR-4 Shutdown | SIGINT and SIGTERM finish the current cycle step and exit cleanly. |
| NFR-5 Observability | Structured log lines per cycle: tokens discovered, accepted, rejected by reason, snapshots written, observations raised, duration. |
| NFR-6 Quality gates | `ruff` clean, `mypy --strict` clean, test coverage ≥ 90%, CI on Python 3.10 to 3.13. |
| NFR-7 Storage | SQLite in WAL mode with indexes on `(chain_id, token_address, observed_at)`. Schema is versioned. |

## 6. Data model

- `snapshots(id, chain_id, token_address, pair_address, dex_id, symbol, price_usd, liquidity_usd, volume_h24, observed_at)`
- `screenings(chain_id, token_address, status, reason, checked_at)` — latest result per token.
- `observations(id, chain_id, token_address, symbol, kind, change_pct, window_minutes, observed_at, notified)`
- `blacklist(chain_id, token_address, reason, added_at)`
- `meta(key, value)` — holds `schema_version`.

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
