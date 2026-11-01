"""Risk providers and the composite verdict (SPEC section 4, FR-6 and FR-12 to FR-15).

Each provider turns one token into a ``RiskAssessment``. Rules:

* Missing or malformed data is never treated as safe (REVIEW finding 7).
* Every assessment carries raw features so research can use them later.
* ``combine`` applies precedence: any REJECTED wins; else PASSED if any
  provider passed; else UNSCREENED (REVIEW finding 11).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

from .http import HttpClient
from .models import PairSnapshot, ScreenResult, ScreenStatus

log = logging.getLogger(__name__)

Feature = float | str | None

# DexScreener chainId -> numeric chain id used by GoPlus and Etherscan V2.
EVM_CHAIN_IDS: Mapping[str, str] = {
    "ethereum": "1",
    "bsc": "56",
    "polygon": "137",
    "arbitrum": "42161",
    "base": "8453",
    "optimism": "10",
    "avalanche": "43114",
}

BURN_ADDRESSES = frozenset(
    {
        "0x0000000000000000000000000000000000000000",
        "0x000000000000000000000000000000000000dead",
        "11111111111111111111111111111111",
    }
)
# Holder tags that indicate pools, lockers or burns rather than real holders (REVIEW finding 8).
NON_HOLDER_TAG_HINTS = ("pool", "lp", "lock", "burn", "raydium", "orca", "meteora", "pump", "uniswap")


@dataclass(frozen=True)
class Creator:
    address: str
    malicious: bool | None = None


@dataclass(frozen=True)
class RiskAssessment:
    provider: str
    status: ScreenStatus
    reasons: tuple[str, ...] = ()
    features: Mapping[str, Feature] = field(default_factory=dict)
    creators: tuple[Creator, ...] = ()


class RiskProvider(Protocol):
    name: str
    creator_only: bool

    def supports(self, chain_id: str) -> bool: ...

    def assess(self, snap: PairSnapshot) -> RiskAssessment: ...


def unscreened(provider: str, reason: str) -> RiskAssessment:
    return RiskAssessment(provider, ScreenStatus.UNSCREENED, (reason,))


def combine(assessments: Sequence[RiskAssessment], *, strict: bool) -> ScreenResult:
    rejected = [r for a in assessments if a.status is ScreenStatus.REJECTED for r in a.reasons]
    if rejected:
        return ScreenResult(ScreenStatus.REJECTED, "; ".join(rejected)[:300])
    if any(a.status is ScreenStatus.PASSED for a in assessments):
        return ScreenResult(ScreenStatus.PASSED)
    if strict:
        return ScreenResult(ScreenStatus.REJECTED, "unscreened (strict mode)")
    return ScreenResult(ScreenStatus.UNSCREENED, "no provider could screen this token")


def creators_of(assessments: Iterable[RiskAssessment]) -> list[Creator]:
    seen: dict[str, Creator] = {}
    for a in assessments:
        for c in a.creators:
            prior = seen.get(c.address)
            if prior is None or (c.malicious and not prior.malicious):
                seen[c.address] = c
    return list(seen.values())


# --- parsing helpers ----------------------------------------------------------
def flag(value: Any) -> bool | None:
    """GoPlus tri-state flag: '1' -> True, '0' -> False, anything else -> unknown.

    Some Solana fields are objects with a ``status`` key; those are unwrapped.
    """
    if isinstance(value, Mapping):
        value = value.get("status")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value) if value in (0, 1) else None
    if isinstance(value, str):
        if value.strip() == "1":
            return True
        if value.strip() == "0":
            return False
    return None


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def top_holder_share(holders: Any, pair_address: str) -> float | None:
    """Share (0-1) of supply held by real top holders, excluding pools, lockers and burns."""
    if not isinstance(holders, list) or not holders:
        return None
    shares: list[float] = []
    for h in holders:
        if not isinstance(h, Mapping):
            continue
        address = str(h.get("address") or h.get("token_account") or "").lower()
        tag = str(h.get("tag") or "").lower()
        if (
            flag(h.get("is_locked")) is True
            or address in BURN_ADDRESSES
            or (pair_address and address == pair_address.lower())
            or any(hint in tag for hint in NON_HOLDER_TAG_HINTS)
        ):
            continue
        share = number(h.get("percent"))
        if share is not None and share >= 0:
            shares.append(share)
    if not shares:
        return 0.0
    total = sum(shares)
    # GoPlus documents fractions (0.1 = 10%); tolerate a percentage scale defensively.
    return total / 100 if total > 1.05 else total


# --- providers ------------------------------------------------------------------
class RugCheckProvider:
    """Solana screening via RugCheck's public summary endpoint."""

    name = "rugcheck"
    creator_only = False

    def __init__(self, http: HttpClient, base_url: str, max_score: int) -> None:
        self._http = http
        self._base = base_url
        self._max_score = max_score

    def supports(self, chain_id: str) -> bool:
        return chain_id == "solana"

    def assess(self, snap: PairSnapshot) -> RiskAssessment:
        try:
            data = self._http.get_json(f"{self._base}/v1/tokens/{quote(snap.token_address)}/report/summary")
        except Exception as exc:
            log.warning("rugcheck unavailable mint=%s error=%s", snap.token_address, type(exc).__name__)
            return unscreened(self.name, "rugcheck unavailable")
        if not isinstance(data, Mapping):
            return unscreened(self.name, "rugcheck malformed response")
        score = number(data.get("score_normalised"))
        features: dict[str, Feature] = {
            "score_normalised": score,
            "lp_locked_pct": number(data.get("lpLockedPct")),
        }
        risks = data.get("risks") or []
        dangers = [
            str(r.get("name", "unnamed"))
            for r in risks
            if isinstance(r, Mapping) and str(r.get("level", "")).lower() == "danger"
        ]
        if dangers:
            return RiskAssessment(
                self.name, ScreenStatus.REJECTED, ("rugcheck danger: " + ", ".join(dangers),), features
            )
        if score is not None and score > self._max_score:
            return RiskAssessment(
                self.name,
                ScreenStatus.REJECTED,
                (f"rugcheck score {score:g} > {self._max_score}",),
                features,
            )
        if score is None:
            return RiskAssessment(self.name, ScreenStatus.UNSCREENED, ("rugcheck score missing",), features)
        return RiskAssessment(self.name, ScreenStatus.PASSED, (), features)


class GoPlusProvider:
    """Contract-risk, holder and creator screening for EVM chains and Solana."""

    name = "goplus"
    creator_only = False

    def __init__(
        self,
        http: HttpClient,
        base_url: str,
        token: str,
        *,
        max_tax_pct: float,
        max_top10_pct: float,
    ) -> None:
        self._http = http
        self._base = base_url
        self._headers = {"Authorization": f"Bearer {token}"} if token else None
        self._max_tax = max_tax_pct / 100
        self._max_top10 = max_top10_pct / 100

    def supports(self, chain_id: str) -> bool:
        return chain_id == "solana" or chain_id in EVM_CHAIN_IDS

    def _fetch(self, snap: PairSnapshot) -> Mapping[str, Any] | None:
        address = snap.token_address
        if snap.chain_id == "solana":
            url = f"{self._base}/api/v1/solana/token_security"
        else:
            url = f"{self._base}/api/v1/token_security/{EVM_CHAIN_IDS[snap.chain_id]}"
            address = address.lower()
        data = self._http.get_json(url, params={"contract_addresses": address}, headers=self._headers)
        if not isinstance(data, Mapping) or str(data.get("code")) != "1":
            return None
        result = data.get("result")
        if not isinstance(result, Mapping):
            return None
        entry = result.get(address) or result.get(snap.token_address)
        if entry is None and len(result) == 1:
            entry = next(iter(result.values()))
        return entry if isinstance(entry, Mapping) else None

    def assess(self, snap: PairSnapshot) -> RiskAssessment:
        try:
            entry = self._fetch(snap)
        except Exception as exc:
            log.warning("goplus unavailable token=%s error=%s", snap.token_address, type(exc).__name__)
            return unscreened(self.name, "goplus unavailable")
        if entry is None:
            return unscreened(self.name, "goplus returned no data")
        if snap.chain_id == "solana":
            return self._assess_solana(entry, snap)
        return self._assess_evm(entry, snap)

    def _assess_evm(self, e: Mapping[str, Any], snap: PairSnapshot) -> RiskAssessment:
        reasons: list[str] = []
        for key in (
            "is_honeypot",
            "cannot_sell_all",
            "owner_change_balance",
            "hidden_owner",
            "transfer_pausable",
            "can_take_back_ownership",
            "is_airdrop_scam",
            "fake_token",
            "selfdestruct",
        ):
            if flag(e.get(key)) is True:
                reasons.append(f"goplus {key}")
        sell_tax, buy_tax = number(e.get("sell_tax")), number(e.get("buy_tax"))
        for label, tax in (("sell_tax", sell_tax), ("buy_tax", buy_tax)):
            if tax is not None and tax > self._max_tax:
                reasons.append(f"goplus {label} {tax:.0%}")
        same_creator = number(e.get("honeypot_with_same_creator"))
        if same_creator is not None and same_creator > 0:
            reasons.append(f"goplus creator made {same_creator:g} honeypots")
        top10 = top_holder_share(e.get("holders"), snap.pair_address)
        if top10 is not None and top10 > self._max_top10:
            reasons.append(f"top-10 holders own {top10:.0%}")
        creator = str(e.get("creator_address") or "").lower()
        features: dict[str, Feature] = {
            "is_honeypot": _tri(flag(e.get("is_honeypot"))),
            "sell_tax": sell_tax,
            "buy_tax": buy_tax,
            "is_open_source": _tri(flag(e.get("is_open_source"))),
            "is_mintable": _tri(flag(e.get("is_mintable"))),
            "holder_count": number(e.get("holder_count")),
            "top10_share": top10,
            "creator_percent": number(e.get("creator_percent")),
            "honeypot_with_same_creator": same_creator,
        }
        creators = (Creator(creator, same_creator is not None and same_creator > 0),) if creator else ()
        if reasons:
            return RiskAssessment(self.name, ScreenStatus.REJECTED, tuple(reasons), features, creators)
        if flag(e.get("is_honeypot")) is None:
            return RiskAssessment(
                self.name, ScreenStatus.UNSCREENED, ("goplus honeypot status unknown",), features, creators
            )
        return RiskAssessment(self.name, ScreenStatus.PASSED, (), features, creators)

    def _assess_solana(self, e: Mapping[str, Any], snap: PairSnapshot) -> RiskAssessment:
        reasons: list[str] = []
        for key in ("mintable", "freezable", "balance_mutable_authority", "closable"):
            if flag(e.get(key)) is True:
                reasons.append(f"goplus {key}")
        creators: list[Creator] = []
        for c in e.get("creators") or []:
            if isinstance(c, Mapping) and c.get("address"):
                malicious = flag(c.get("malicious_address"))
                creators.append(Creator(str(c["address"]), malicious))
                if malicious:
                    reasons.append("goplus malicious creator")
        top10 = top_holder_share(e.get("holders"), snap.pair_address)
        if top10 is not None and top10 > self._max_top10:
            reasons.append(f"top-10 holders own {top10:.0%}")
        burn = [number(d.get("burn_percent")) for d in e.get("dex") or [] if isinstance(d, Mapping)]
        burn_known = [b for b in burn if b is not None]
        features: dict[str, Feature] = {
            "mintable": _tri(flag(e.get("mintable"))),
            "freezable": _tri(flag(e.get("freezable"))),
            "top10_share": top10,
            "lp_burn_pct": max(burn_known) if burn_known else None,
        }
        if reasons:
            return RiskAssessment(self.name, ScreenStatus.REJECTED, tuple(reasons), features, tuple(creators))
        if flag(e.get("mintable")) is None or flag(e.get("freezable")) is None:
            return RiskAssessment(
                self.name,
                ScreenStatus.UNSCREENED,
                ("goplus authority status unknown",),
                features,
                tuple(creators),
            )
        return RiskAssessment(self.name, ScreenStatus.PASSED, (), features, tuple(creators))


def _tri(value: bool | None) -> str | None:
    return None if value is None else ("1" if value else "0")


class EtherscanCreatorProvider:
    """Deployer lookup for EVM tokens via Etherscan V2 getcontractcreation (creator only)."""

    name = "etherscan"
    creator_only = True

    def __init__(self, http: HttpClient, base_url: str, api_key: str) -> None:
        self._http = http
        self._base = base_url
        self._key = api_key

    def supports(self, chain_id: str) -> bool:
        return chain_id in EVM_CHAIN_IDS

    def assess(self, snap: PairSnapshot) -> RiskAssessment:
        params = {
            "chainid": EVM_CHAIN_IDS[snap.chain_id],
            "module": "contract",
            "action": "getcontractcreation",
            "contractaddresses": snap.token_address,
            "apikey": self._key,
        }
        try:
            data = self._http.get_json(f"{self._base}/v2/api", params=params)
        except Exception as exc:
            log.warning("etherscan unavailable error=%s", type(exc).__name__)
            return unscreened(self.name, "etherscan unavailable")
        rows = data.get("result") if isinstance(data, Mapping) and str(data.get("status")) == "1" else None
        if not isinstance(rows, list):
            return unscreened(self.name, "etherscan returned no creator")
        creators = tuple(
            Creator(str(r["contractCreator"]).lower())
            for r in rows
            if isinstance(r, Mapping) and r.get("contractCreator")
        )
        return RiskAssessment(self.name, ScreenStatus.UNSCREENED, ("creator lookup only",), {}, creators)


class PocketUniverseProvider:
    """Volume-authenticity check (Solana). Disabled unless a key and base URL are configured.

    Pocket Universe documents ``/rug_check/{api_key}`` and the fields ``status``,
    ``is_scam``, ``rug_percent``, ``fresh_percent`` and ``message``, but not the
    HTTP method or parameter name, so both are configurable. Confirm the contract
    with ``dexsignal probe pocket <mint>`` before relying on it (REVIEW finding 14).
    """

    name = "pocket_universe"
    creator_only = False

    def __init__(self, http: HttpClient, base_url: str, api_key: str, *, method: str, param: str) -> None:
        self._http = http
        self._url = f"{base_url}/rug_check/{quote(api_key)}"
        self._method = method.upper()
        self._param = param

    def supports(self, chain_id: str) -> bool:
        return chain_id == "solana"

    def raw(self, mint: str) -> Any:
        if self._method == "POST":
            return self._http.post_json(self._url, {self._param: mint})
        return self._http.get_json(self._url, params={self._param: mint})

    def assess(self, snap: PairSnapshot) -> RiskAssessment:
        try:
            data = self.raw(snap.token_address)
        except Exception as exc:  # the URL holds the key: log the type only (REVIEW finding 12)
            log.warning("pocket universe unavailable error=%s", type(exc).__name__)
            return unscreened(self.name, "pocket universe unavailable")
        if not isinstance(data, Mapping):
            return unscreened(self.name, "pocket universe malformed response")
        features: dict[str, Feature] = {
            "rug_percent": number(data.get("rug_percent")),
            "fresh_percent": number(data.get("fresh_percent")),
        }
        if data.get("status") != "processed":
            return RiskAssessment(
                self.name, ScreenStatus.UNSCREENED, ("pocket universe not processed",), features
            )
        if data.get("is_scam") is True:
            message = str(data.get("message") or "flagged as scam")[:160]
            return RiskAssessment(
                self.name, ScreenStatus.REJECTED, (f"pocket universe: {message}",), features
            )
        if data.get("is_scam") is False:
            return RiskAssessment(self.name, ScreenStatus.PASSED, (), features)
        return RiskAssessment(
            self.name, ScreenStatus.UNSCREENED, ("pocket universe verdict missing",), features
        )
