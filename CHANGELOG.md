# Changelog

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
