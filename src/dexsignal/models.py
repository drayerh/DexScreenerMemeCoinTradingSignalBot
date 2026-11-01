"""Domain types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


def norm_address(address: str) -> str:
    """EVM addresses are case-insensitive; Solana mints are not. Normalise only 0x-hex."""
    return address.lower() if address.startswith("0x") else address


class ObservationKind(str, Enum):
    RISE = "RISE"
    FALL = "FALL"
    LIQUIDITY_DROP = "LIQUIDITY_DROP"


class ScreenStatus(str, Enum):
    PASSED = "passed"
    REJECTED = "rejected"
    UNSCREENED = "unscreened"


@dataclass(frozen=True)
class TokenRef:
    chain_id: str
    token_address: str


@dataclass(frozen=True)
class PairSnapshot:
    chain_id: str
    token_address: str
    pair_address: str
    dex_id: str
    symbol: str
    price_usd: float
    liquidity_usd: float
    volume_h24: float


@dataclass(frozen=True)
class ScreenResult:
    status: ScreenStatus
    reason: str = ""


@dataclass(frozen=True)
class Observation:
    chain_id: str
    token_address: str
    symbol: str
    kind: ObservationKind
    change_pct: float
    window_minutes: int
    observed_at: datetime
