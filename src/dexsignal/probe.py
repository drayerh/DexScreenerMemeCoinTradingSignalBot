"""Live contract checks against each provider (REVIEW finding 15).

Uses well-known tokens so a failure means the provider's API changed or is
unreachable, not that a token is risky.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import PairSnapshot, TokenRef
from .providers import DexScreenerClient
from .risk import RiskProvider

USDC_ETHEREUM = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WRAPPED_SOL = "So11111111111111111111111111111111111111112"
FAILURE_MARKERS = ("unavailable", "malformed", "returned no")


@dataclass(frozen=True)
class ProbeResult:
    name: str
    ok: bool
    detail: str


def _known(chain: str, address: str) -> PairSnapshot:
    return PairSnapshot(chain, address, "", "", "PROBE", 1.0, 1.0, 0.0)


def probe_dexscreener(client: DexScreenerClient) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    try:
        refs = client.latest_tokens()
        results.append(ProbeResult("dexscreener profiles", bool(refs), f"{len(refs)} tokens parsed"))
    except Exception as exc:
        results.append(ProbeResult("dexscreener profiles", False, type(exc).__name__))
    pairs = client.best_pairs([TokenRef("ethereum", USDC_ETHEREUM)]).pairs
    results.append(
        ProbeResult(
            "dexscreener pairs", bool(pairs), f"{len(pairs)} USDC pair parsed" if pairs else "no pair"
        )
    )
    return results


def probe_provider(provider: RiskProvider) -> list[ProbeResult]:
    return [
        _probe_one(provider, chain, address)
        for chain, address in (("solana", WRAPPED_SOL), ("ethereum", USDC_ETHEREUM))
        if provider.supports(chain)
    ]


def _probe_one(provider: RiskProvider, chain: str, address: str) -> ProbeResult:
    a = provider.assess(_known(chain, address))
    failed = any(marker in reason for reason in a.reasons for marker in FAILURE_MARKERS)
    detail = f"{chain}: status={a.status.value}"
    if a.reasons:
        detail += f" reasons={'; '.join(a.reasons)}"
    if a.creators:
        detail += f" creators={len(a.creators)}"
    return ProbeResult(f"{provider.name} ({chain})", not failed, detail)
