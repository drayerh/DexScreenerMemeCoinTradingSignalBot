"""Clients for DexScreener and Telegram (see SPEC section 3). Risk providers live in risk.py."""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .http import HttpClient
from .models import Observation, PairSnapshot, TokenRef, norm_address

log = logging.getLogger(__name__)


def _as_float(value: Any) -> float | None:
    """Parse numbers that providers may send as strings; reject NaN/inf/negatives."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def parse_pair(raw: Any) -> PairSnapshot | None:
    """Turn one DexScreener pair object into a snapshot, or None if unusable."""
    if not isinstance(raw, dict):
        return None
    base = raw.get("baseToken")
    liquidity = raw.get("liquidity")
    volume = raw.get("volume")
    if not isinstance(base, dict) or not isinstance(liquidity, dict):
        return None
    price = _as_float(raw.get("priceUsd"))
    liq = _as_float(liquidity.get("usd"))
    vol = _as_float(volume.get("h24")) if isinstance(volume, dict) else 0.0
    chain_id, address = raw.get("chainId"), base.get("address")
    if price is None or price == 0 or liq is None or vol is None:
        return None
    if not isinstance(chain_id, str) or not isinstance(address, str) or not address:
        return None
    return PairSnapshot(
        chain_id=chain_id,
        token_address=address,
        pair_address=str(raw.get("pairAddress") or ""),
        dex_id=str(raw.get("dexId") or ""),
        symbol=str(base.get("symbol") or "?")[:32],
        price_usd=price,
        liquidity_usd=liq,
        volume_h24=vol,
    )


@dataclass
class PairFetch:
    """Pairs found, plus the tokens whose batch failed (so absence there means 'unknown',
    not 'delisted'; review round 2, finding 2)."""

    pairs: list[PairSnapshot] = field(default_factory=list)
    failed: set[TokenRef] = field(default_factory=set)


class DexScreenerClient:
    def __init__(self, http: HttpClient, base_url: str, batch_size: int) -> None:
        self._http = http
        self._base = base_url
        self._batch = batch_size

    def latest_tokens(self) -> list[TokenRef]:
        """FR-1: newest token profiles, de-duplicated, order preserved."""
        data = self._http.get_json(f"{self._base}/token-profiles/latest/v1")
        items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        seen: set[TokenRef] = set()
        refs: list[TokenRef] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            chain, address = item.get("chainId"), item.get("tokenAddress")
            if isinstance(chain, str) and isinstance(address, str) and chain and address:
                ref = TokenRef(chain, address)
                if ref not in seen:
                    seen.add(ref)
                    refs.append(ref)
        return refs

    def best_pairs(self, refs: Iterable[TokenRef]) -> PairFetch:
        """FR-2: batched lookups per chain; keep the most liquid pair per token."""
        ref_list = list(refs)
        by_chain: dict[str, list[str]] = {}
        for ref in ref_list:
            by_chain.setdefault(ref.chain_id, []).append(ref.token_address)
        wanted = {(r.chain_id, r.token_address.lower()) for r in ref_list}
        best: dict[tuple[str, str], PairSnapshot] = {}
        failed: set[TokenRef] = set()
        for chain, addresses in by_chain.items():
            for batch in _chunks(addresses, self._batch):
                url = f"{self._base}/tokens/v1/{quote(chain)}/{','.join(quote(a) for a in batch)}"
                try:
                    data = self._http.get_json(url)
                except Exception as exc:  # NFR-1: one failed batch never aborts the cycle
                    log.warning(
                        "pair batch failed chain=%s size=%d error=%s", chain, len(batch), type(exc).__name__
                    )
                    failed.update(TokenRef(chain, norm_address(a)) for a in batch)
                    continue
                for raw in data if isinstance(data, list) else []:
                    snap = parse_pair(raw)
                    if snap is None:
                        continue
                    key = (snap.chain_id, snap.token_address.lower())
                    if key not in wanted:
                        continue  # pairs where our token is only the quote side
                    current = best.get(key)
                    if current is None or snap.liquidity_usd > current.liquidity_usd:
                        best[key] = snap
        return PairFetch(list(best.values()), failed)


def format_observation(obs: Observation) -> str:
    sign = "+" if obs.change_pct >= 0 else ""
    labels = {
        "RISE": "Price rise",
        "FALL": "Price fall",
        "LIQUIDITY_DROP": "Liquidity drop",
    }
    return (
        f"[Research observation] {labels[obs.kind.value]}\n"
        f"Token: {obs.symbol} ({obs.chain_id})\n"
        f"Address: {obs.token_address}\n"
        f"Change: {sign}{obs.change_pct:.1f}% over {obs.window_minutes} min\n"
        f"Time (UTC): {obs.observed_at:%Y-%m-%d %H:%M}\n"
        "Not financial advice. For private research only."
    )


class TelegramNotifier:
    """FR-10: one private chat; failures are reported to the caller, never raised."""

    def __init__(self, http: HttpClient, base_url: str, token: str, chat_id: str) -> None:
        self._http = http
        self._url = f"{base_url}/bot{token}/sendMessage"
        self._chat_id = chat_id

    def send(self, obs: Observation) -> bool:
        try:
            data = self._http.post_json(
                self._url,
                {"chat_id": self._chat_id, "text": format_observation(obs), "disable_web_page_preview": True},
            )
        except Exception as exc:
            # Never log the URL: it contains the bot token (NFR-3).
            log.warning("telegram send failed kind=%s error=%s", obs.kind.value, type(exc).__name__)
            return False
        return bool(isinstance(data, dict) and data.get("ok"))
