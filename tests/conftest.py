from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest

from dexsignal.config import Settings
from dexsignal.models import Observation, PairSnapshot, ScreenStatus, TokenRef
from dexsignal.risk import Creator, RiskAssessment
from dexsignal.storage import Storage

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: int) -> None:
        self.now += timedelta(minutes=minutes)


class FakeMarket:
    def __init__(self) -> None:
        self.pairs: list[PairSnapshot] = []
        self.fail_discovery = False

    def latest_tokens(self) -> list[TokenRef]:
        if self.fail_discovery:
            raise RuntimeError("down")
        return [TokenRef(p.chain_id, p.token_address) for p in self.pairs]

    def best_pairs(self, refs: list[TokenRef]) -> list[PairSnapshot]:
        return list(self.pairs)


class FakeProvider:
    """Test double for a RiskProvider."""

    def __init__(
        self,
        status: ScreenStatus = ScreenStatus.PASSED,
        *,
        name: str = "fake",
        chains: tuple[str, ...] = ("solana",),
        creators: tuple[Creator, ...] = (),
        creator_only: bool = False,
        raises: bool = False,
    ) -> None:
        self.name = name
        self.creator_only = creator_only
        self.status = status
        self.chains = chains
        self.creators = creators
        self.raises = raises
        self.calls = 0

    def supports(self, chain_id: str) -> bool:
        return chain_id in self.chains

    def assess(self, snap: PairSnapshot) -> RiskAssessment:
        self.calls += 1
        if self.raises:
            raise RuntimeError("boom")
        reasons = ("fake reject",) if self.status is ScreenStatus.REJECTED else ()
        return RiskAssessment(self.name, self.status, reasons, {"x": 1.0}, self.creators)


class FakeNotifier:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[Observation] = []

    def send(self, obs: Observation) -> bool:
        self.sent.append(obs)
        return self.ok


def snap(
    price: float = 1.0,
    liquidity: float = 100_000.0,
    volume: float = 200_000.0,
    chain: str = "solana",
    address: str = "Mint111",
    symbol: str = "MEME",
) -> PairSnapshot:
    return PairSnapshot(chain, address, "Pair111", "raydium", symbol, price, liquidity, volume)


@pytest.fixture
def storage() -> Iterator[Storage]:
    s = Storage(":memory:")
    yield s
    s.close()


@pytest.fixture
def settings() -> Settings:
    return Settings()
