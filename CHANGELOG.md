# Changelog

## 1.1.0

Implements the expert review in `docs/REVIEW.md` (19 of 22 findings; 3 deferred
with reasons).

### Added
- GoPlus screening for EVM chains and Solana: honeypots, taxes, owner powers,
  mint and freeze authority, malicious creators, holder concentration.
- Deployer intelligence: deployer table, manual block, malicious flag,
  serial-rugger rule based on labelled rugs; Etherscan V2 creator fallback.
- Pocket Universe provider, gated behind configuration and a `probe` command.
- Point-in-time risk history (`risk_checks`), composite verdict, strict mode.
- 24-hour tracking of accepted tokens, outcome labels at 1/6/24 hours, rug
  labels, and `evaluate` with a baseline and Wilson intervals.
- `health` (heartbeat), `probe`, `deployer` and `evaluate` commands; JSON logs;
  secret redaction in all logs.
- Schema v2 with an additive migration from v1.
- `pip-audit` in CI, Dependabot, nightly provider probe, non-root Docker image.

### Changed
- RugCheck moved into the risk-provider framework; it now records `lpLockedPct`
  and treats a missing score as unscreened.
- `DSB_RETENTION_DAYS` must be at least 2 so labelling has the data it needs.

## 1.0.0

Rebuilt as a private research tool against a written specification
(`docs/SPEC.md`).

### Fixed
- Token discovery called a DexScreener endpoint that does not exist
  (`/latest/dex/tokens?limit=100`). It now uses `/token-profiles/latest/v1`
  and batched `/tokens/v1/{chainId}/{addresses}` lookups.
- Every accepted token crashed the cycle with `KeyError: 'historicalPrices'`.
- Snapshots overwrote each other (one row per token), so price changes could
  never be detected. History is now kept, and windows are read oldest first.
- Every alert was labelled BUY. Alerts are now neutral research observations
  (`RISE`, `FALL`, `LIQUIDITY_DROP`).
- The developer blacklist compared exchange names with wallet addresses.
- A placeholder Pocket Universe API key was hard-coded.
- `.env` was documented but never loaded.
- Alerts repeated every cycle; a cooldown now applies.

### Added
- Timeouts, retries with backoff, and per-host rate limiting on all HTTP calls.
- RugCheck screening via the documented summary endpoint, with caching.
- Volume-sanity heuristic, liquidity-drop observations, retention pruning.
- CLI: `run`, `once`, `stats`, `export`, `blacklist`.
- Tests (44, about 98% coverage), `ruff`, `mypy --strict`, GitHub Actions CI,
  pre-commit hooks.

### Removed
- Pocket Universe integration (its API contract could not be verified).
- Developer-wallet blacklist (no reliable data source; see SPEC non-goals).
- Committed IDE settings (`.idea/`), unused dependencies, `requirements.txt`
  (replaced by `pyproject.toml`).
