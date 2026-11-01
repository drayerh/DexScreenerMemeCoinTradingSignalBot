# Expert Panel Review — Risk Intelligence Plan

Date: 2026-11-01 · Scope: the plan to add Pocket Universe, deployer blacklists,
contract-risk screening, holder concentration and outcome labelling.

The panel members are analytical lenses applied to the plan, not real people.
Every finding has a disposition: **Implemented** in this change, or
**Deferred** with a reason.

## Panel

| Lens | Framework applied |
| --- | --- |
| On-chain forensics investigator | Attribution chains; adversary adaptation |
| Quantitative researcher | Research design: bias, leakage, base rates, statistical power |
| Smart-contract security auditor | Token-contract threat taxonomy (honeypot, mint, freeze, owner powers) |
| Data engineer | Point-in-time data, idempotent jobs, schema evolution |
| Site reliability engineer | SLOs, liveness, failure modes |
| Application security engineer | Secrets handling, supply chain (OWASP, SLSA ideas) |
| API integration engineer | Contract testing against third-party drift |
| Compliance counsel | Market-abuse and publication risk |
| Red team | Pre-mortem: how does this mislead its owner? |

## Findings

| # | Lens | Finding | Disposition |
| --- | --- | --- | --- |
| 1 | Quant | **Survivorship bias.** Outcome labels computed only from tokens still in the discovery feed miss exactly the tokens that collapsed and dropped out. | Implemented: every accepted token is tracked for `DSB_TRACK_HOURS` (default 24) and snapshotted every cycle regardless of filters. |
| 2 | Quant | **No base rate.** A "RISE" hit rate means nothing without the forward returns of all tokens as a control. | Implemented: `dexsignal evaluate` reports a baseline (every token from first sighting) beside each observation kind. |
| 3 | Quant | **Small samples overclaim.** | Implemented: proportions carry Wilson 95% intervals; groups under 30 are flagged. |
| 4 | Data | **Look-ahead leakage.** Overwriting risk results lets later knowledge contaminate earlier decisions. | Implemented: risk checks are append-only with timestamps (point-in-time). |
| 5 | Forensics | **Circular "serial rugger" rule.** Counting our own FALL alerts as rugs conflates price moves with rugs. | Implemented: a rug is a labelled outcome — liquidity falling ≥ `DSB_RUG_LIQ_DROP_PCT` (default 80%) from its peak within 24h of first sighting. Deployer reputation counts labelled rugs only. |
| 6 | Forensics | **Deployer addresses are cheap to rotate.** A blacklist alone catches only careless repeat offenders. | Partly: the blacklist plus GoPlus `honeypot_with_same_creator` and `malicious_address` flags. Funding-source clustering is deferred (needs paid tracing data). |
| 7 | Auditor | **Missing ≠ safe.** GoPlus encodes flags as strings `"1"`/`"0"`; an absent field must not pass. | Implemented: tri-state parsing; unknown flags are recorded as unknown, never as safe. |
| 8 | Auditor | **Holder concentration misfires** if LP pools, lockers and burn addresses count as "holders". | Implemented: excludes locked holders, the pair address and known burn addresses. |
| 9 | Auditor | **EVM tokens are unscreened today.** | Implemented: GoPlus EVM screening (honeypot, sell/buy tax, hidden owner, owner balance control, pausable transfers, reclaimable ownership, fake/airdrop scam). |
| 10 | Auditor | **Solana authorities.** Active mint or freeze authority is a primary rug vector. | Implemented: GoPlus Solana `mintable`, `freezable`, malicious creators. |
| 11 | Data | **Provider disagreement needs precedence.** | Implemented: any REJECTED wins; otherwise PASSED if any provider passed; otherwise UNSCREENED. `DSB_STRICT_SCREENING=1` rejects UNSCREENED. |
| 12 | AppSec | **Secrets in URLs.** Pocket Universe puts the API key in the path; Telegram in the path; Etherscan in the query. Exceptions include URLs. | Implemented: a logging filter redacts every configured secret from all log output. |
| 13 | AppSec | **Supply chain.** | Implemented: `pip-audit` in CI, Dependabot for pip and Actions. Pinning Actions by commit SHA is deferred to Dependabot's first update. |
| 14 | API | **Undocumented Pocket Universe contract** (method and parameter names not stated). | Implemented behind a gate: disabled unless a key is set; method and parameter are configurable; `dexsignal probe pocket <mint>` prints the raw response to confirm the contract before trusting it. |
| 15 | API | **Silent drift** in provider schemas corrupts data without errors. | Implemented: `dexsignal probe` validates every provider against known tokens; a scheduled nightly workflow runs it. |
| 16 | Data | **Schema evolution.** | Implemented: versioned additive migration v1 → v2. |
| 17 | SRE | **No liveness signal.** A stalled loop looks identical to a quiet market. | Implemented: heartbeat in the database; `dexsignal health` exits non-zero if no successful cycle within `--max-age-minutes`; Docker `HEALTHCHECK` uses it. |
| 18 | SRE | **Logs not machine-readable.** | Implemented: `DSB_LOG_FORMAT=json`. |
| 19 | SRE | **Cost and rate limits** grow with per-token providers. | Implemented: cheap filters run before paid/remote checks; per-provider caching with TTL; shared per-host rate limiter. |
| 20 | Compliance | **Publication risk.** | Unchanged: private chat only, neutral wording, no buy/sell language. |
| 21 | Red team | **Mixer-funded deployers** (e.g. Tornado Cash) are a strong red flag. | Deferred: requires transaction-graph tracing data. |
| 22 | Red team | **ML before data.** | Deferred until several thousand labelled observations exist; `evaluate` provides the evidence base first. |

## Round 2 — independent code review

A separate reviewer, who had not seen the code being written, audited the
implementation, ran simulations, and reported 12 findings plus two minor ones.
All are regression-tested in `tests/test_review2.py`.

| # | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| 1 | High | Tracking stopped at exactly 24h, so the +24h measurement (and late observations' +6h/+24h) almost never had data under real timing. | Fixed: tracking lasts until the longest horizon plus tolerance (about 26.4h), and continues for any token with an observation whose outcomes are pending. Tested with a drifting clock. |
| 2 | High | A rug where the pair vanishes or returns no price was labelled "not rugged". | Fixed: a tracked token absent from a successful lookup is recorded as a zero-liquidity, zero-price snapshot. Tokens in a failed batch are skipped as an outage. |
| 3 | High | Accepted tokens bypassed later blacklisting, deployer blocks and re-screening forever. | Fixed: tracked tokens are still sampled, but raise no observations when blacklisted, deployer-blocked or rejected. Unscreened tokens are re-screened. Tokens outside tracking go through all filters again. |
| 4 | High | `probe pocket` could print the API key in an error traceback. | Fixed: errors print only the exception type and HTTP status. |
| 5 | Medium | URL-encoded keys and tracebacks escaped redaction. | Fixed: encoded variants are redacted, and tracebacks are rendered and redacted inside the filter. |
| 6 | Medium | "Baseline (all tokens)" contained only accepted tokens, measured from a different token age than observations. | Partly: renamed to "accepted tokens from first sighting", with the age caveat printed in the report. An age-matched baseline and sampling of rejected tokens are deferred. |
| 7 | Medium | Per-kind rug rates included rugs that happened before the observation. | Fixed: per-observation `rugged_after` uses only the liquidity path after the observation. |
| 8 | Medium | Coverage denominators included outcomes not yet due; small-sample flag ignored per-horizon counts. | Fixed: only matured outcomes count; each horizon carries its own small-sample flag. |
| 9 | Medium | Migrated v1 tokens carried v1's survivorship bias into labels and evaluation. | Fixed: backfilled tokens are marked `v1` and excluded from tracking, labelling and evaluation. Migration test now covers observations and blacklist. |
| 10 | Medium | Unscreened results were never cached, so Etherscan and GoPlus were re-queried every cycle. | Fixed: unscreened results are reused for `DSB_NEGATIVE_TTL_MINUTES` (default 10); creator-only lookups for the full TTL. |
| 11 | Low | Per-cycle labelling work grew with the database. | Fixed: queries bounded by the retention window; index on `observations(observed_at)`. |
| 12 | Low | EVM address case differed between discovery and tracking, duplicating requests. | Fixed: references are normalised before de-duplication. |
| — | Low | Holder-share scale guessed from the sum; non-list GoPlus fields raised errors. | Fixed: percentage scale assumed only when a single holder exceeds 1; non-list fields are ignored. |
