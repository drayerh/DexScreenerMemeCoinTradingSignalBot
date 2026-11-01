"""Watchlist and multi-feed discovery (SPEC FR-1, FR-19; acceptance criteria 17-19)."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
import requests
import responses

from dexsignal.config import ConfigError, Settings
from dexsignal.http import HttpClient
from dexsignal.models import PairSnapshot, ScreenStatus, TokenRef
from dexsignal.pipeline import Pipeline
from dexsignal.providers import DexScreenerClient, PairFetch
from dexsignal.storage import SCHEMA_VERSION, Storage

from .conftest import FakeClock, FakeProvider, snap

DS = "https://api.dexscreener.com"
SOL = TokenRef("solana", "Mint111")


class FeedMarket:
    """Discovery lists `listed` only while `in_feed`; pairs and pool state are set by the test."""

    def __init__(self) -> None:
        self.listed: list[TokenRef] = [SOL]
        self.in_feed = True
        self.pairs: list[PairSnapshot] = []
        self.unpooled: set[TokenRef] = {SOL}
        self.failed: set[TokenRef] = set()
        self.requested: list[list[TokenRef]] = []

    def latest_tokens(self) -> list[TokenRef]:
        return list(self.listed) if self.in_feed else []

    def best_pairs(self, refs: list[TokenRef]) -> PairFetch:
        self.requested.append(list(refs))
        wanted = set(refs)
        pairs = [p for p in self.pairs if TokenRef(p.chain_id, p.token_address) in wanted]
        return PairFetch(pairs, self.failed & wanted, self.unpooled & wanted)


def build(
    storage: Storage, settings: Settings | None = None, provider: FakeProvider | None = None
) -> tuple[Pipeline, FeedMarket, FakeClock]:
    market, clock = FeedMarket(), FakeClock()
    p = Pipeline(settings or Settings(), storage, market, [provider or FakeProvider()], None, clock=clock)
    return p, market, clock


# AC-17: a token discovered before it has a pool is accepted once it qualifies, after
# leaving the discovery feed, with its discovery time kept.
def test_unpooled_token_graduates_after_leaving_feed(storage: Storage) -> None:
    p, market, clock = build(storage)
    report = p.run_cycle()
    assert report.rejected == {"no_pool_yet": 1} and report.accepted == 0
    market.in_feed = False
    clock.advance(30)
    market.unpooled = set()
    market.pairs = [snap(liquidity=20_000, volume=10_000)]
    report = p.run_cycle()
    assert report.watching == 1 and report.rejected == {"low_liquidity": 1}
    clock.advance(30)
    market.pairs = [snap(liquidity=90_000, volume=10_000)]
    report = p.run_cycle()
    assert report.accepted == 1 and report.graduated == 1
    assert storage.stats()["watching"] == 0 and storage.stats()["graduated_tokens"] == 1
    clock.advance(5)
    assert p.run_cycle().tracked == 1  # now tracked like any accepted token


def test_watch_expires_after_watch_hours(storage: Storage) -> None:
    p, market, clock = build(storage, Settings(watch_hours=1))
    p.run_cycle()
    market.in_feed = False
    clock.advance(59)
    assert p.run_cycle().watching == 1
    clock.advance(2)
    calls = len(market.requested)
    assert p.run_cycle().watching == 0
    assert len(market.requested) == calls  # nothing left to look up
    assert storage.stats()["watching"] == 0


def test_rejection_for_risk_ends_watch(storage: Storage) -> None:
    p, market, clock = build(storage, provider=FakeProvider(ScreenStatus.REJECTED))
    p.run_cycle()
    clock.advance(5)
    market.unpooled = set()
    market.pairs = [snap(liquidity=90_000, volume=10_000)]
    assert p.run_cycle().rejected == {"risk_rejected": 1}
    assert storage.stats()["watching"] == 0


def test_watchlist_disabled(storage: Storage) -> None:
    p, market, clock = build(storage, Settings(watch_hours=0))
    p.run_cycle()
    market.in_feed = False
    clock.advance(5)
    report = p.run_cycle()
    assert report.watching == 0 and storage.stats()["watching"] == 0


def test_watch_limit_keeps_newest(storage: Storage) -> None:
    p, market, clock = build(storage, Settings(watch_max_tokens=1))
    p.run_cycle()
    clock.advance(5)
    newer = TokenRef("solana", "Mint222")
    market.listed, market.unpooled = [newer], {SOL, newer}
    p.run_cycle()
    market.in_feed = False
    clock.advance(5)
    p.run_cycle()
    assert market.requested[-1] == [newer]


def test_lookup_failure_and_missing_pair_reasons(storage: Storage) -> None:
    p, market, _ = build(storage)
    other = TokenRef("solana", "Mint222")
    market.listed, market.unpooled, market.failed = [SOL, other], set(), {other}
    assert p.run_cycle().rejected == {"no_pair_data": 1, "lookup_failed": 1}
    assert storage.stats()["watching"] == 2


def test_known_token_is_not_rewatched(storage: Storage) -> None:
    p, market, clock = build(storage)
    market.unpooled = set()
    market.pairs = [snap(liquidity=90_000, volume=10_000)]
    p.run_cycle()
    clock.advance(60 * 30)  # beyond tracking
    market.pairs = [snap(liquidity=1_000, volume=10)]
    assert p.run_cycle().rejected == {"low_liquidity": 1}
    assert storage.stats()["watching"] == 0


# AC-18: discovery merges every configured feed and survives a failed one.
@responses.activate
def test_feeds_are_merged_and_deduplicated() -> None:
    responses.get(f"{DS}/token-profiles/latest/v1", json=[{"chainId": "base", "tokenAddress": "0xAB"}])
    responses.get(
        f"{DS}/token-boosts/latest/v1",
        json=[{"chainId": "base", "tokenAddress": "0xab"}, {"chainId": "solana", "tokenAddress": "S"}],
    )
    responses.get(f"{DS}/token-boosts/top/v1", status=404)
    client = DexScreenerClient(HttpClient(5, 6000), DS, 30, Settings().discovery_feeds)
    assert client.latest_tokens() == [TokenRef("base", "0xAB"), TokenRef("solana", "S")]


@responses.activate
def test_discovery_fails_only_when_every_feed_fails() -> None:
    responses.get(f"{DS}/token-profiles/latest/v1", status=404)
    responses.get(f"{DS}/token-boosts/latest/v1", status=404)
    client = DexScreenerClient(HttpClient(5, 6000), DS, 30, ("profiles", "boosts_latest"))
    with pytest.raises(requests.HTTPError):
        client.latest_tokens()


# AC-19: a pump.fun bonding-curve pair (no liquidity object) means 'no pool yet', not 'no data'.
@responses.activate
def test_bonding_curve_pair_is_unpooled() -> None:
    curve = {
        "chainId": "solana",
        "dexId": "pumpfun",
        "pairAddress": "Curve",
        "priceUsd": "0.000003",
        "baseToken": {"address": "Pump1", "symbol": "PUMP"},
        "volume": {"h24": 96_000},
    }
    responses.get(f"{DS}/tokens/v1/solana/Pump1,Gone1", json=[curve])
    fetch = DexScreenerClient(HttpClient(5, 6000), DS, 30).best_pairs(
        [TokenRef("solana", "Pump1"), TokenRef("solana", "Gone1")]
    )
    assert fetch.pairs == [] and fetch.unpooled == {TokenRef("solana", "Pump1")}


def test_discovery_feed_setting() -> None:
    s = Settings.from_env({"DSB_DISCOVERY_FEEDS": " Profiles, boosts_top ,profiles"})
    assert s.discovery_feeds == ("profiles", "boosts_top")
    with pytest.raises(ConfigError):
        Settings.from_env({"DSB_DISCOVERY_FEEDS": "profiles,trending"})
    with pytest.raises(ConfigError):
        Settings.from_env({"DSB_DISCOVERY_FEEDS": " , "})


def test_v2_database_migrates_to_v3(tmp_path: Path) -> None:
    db = tmp_path / "v2.db"
    s = Storage(db)
    s.close()
    conn = sqlite3.connect(db)
    conn.executescript(
        "DROP TABLE watchlist; ALTER TABLE tokens DROP COLUMN discovered_at;"
        "UPDATE meta SET value = '2' WHERE key = 'schema_version';"
        "INSERT INTO tokens (chain_id, token_address, symbol, first_seen)"
        " VALUES ('solana', 'Old', 'OLD', '2026-01-01T00:00:00+00:00');"
    )
    conn.commit()
    conn.close()
    s = Storage(db)
    assert s.schema_version() == SCHEMA_VERSION == 3
    assert s.is_known("solana", "Old") and s.stats()["watching"] == 0
    s.watch(SOL, "no_pool_yet", FakeClock().now)
    assert list(s.watched(FakeClock().now - timedelta(hours=1), 10)) == [SOL]
    s.close()
