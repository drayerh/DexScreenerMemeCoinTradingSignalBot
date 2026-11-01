"""Regression tests for the second independent review (docs/REVIEW.md, round 2)."""

from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import responses

from dexsignal import cli
from dexsignal.config import Settings
from dexsignal.evaluate import evaluate
from dexsignal.logsetup import RedactingFilter
from dexsignal.models import ScreenStatus, TokenRef
from dexsignal.pipeline import Pipeline
from dexsignal.providers import PairFetch
from dexsignal.risk import Creator, RiskAssessment, top_holder_share
from dexsignal.storage import SCHEMA_VERSION, Storage

from .conftest import T0, FakeClock, FakeMarket, FakeNotifier, FakeProvider, snap


def build(
    storage: Storage, providers: list[FakeProvider] | None = None, settings: Settings | None = None
) -> tuple[Pipeline, FakeMarket, FakeClock, FakeNotifier]:
    market, clock, notifier = FakeMarket(), FakeClock(), FakeNotifier()
    p = Pipeline(
        settings or Settings(), storage, market, providers or [FakeProvider()], notifier, clock=clock
    )
    return p, market, clock, notifier


def run_minutes(p: Pipeline, clock: FakeClock, minutes: int, step_seconds: int = 303) -> None:
    """Run cycles every ~5 minutes with 3 s drift, like a real scheduler."""
    elapsed = 0
    while elapsed < minutes * 60:
        p.run_cycle()
        clock.now += timedelta(seconds=step_seconds)
        elapsed += step_seconds


# Finding 1: +24h measurements must actually be taken under realistic timing.
def test_24h_horizon_measured_with_drifting_clock(storage: Storage) -> None:
    p, market, clock, _ = build(storage)
    market.pairs = [snap(price=1)]
    p.run_cycle()
    clock.now += timedelta(seconds=303)
    for price in (2, 3):
        market.pairs = [snap(price=price)]
        p.run_cycle()
        clock.now += timedelta(seconds=303)

    class FeedLeft(FakeMarket):
        """The token has left the discovery feed but its pair is still quoted."""

        def latest_tokens(self) -> list[TokenRef]:
            return []

    p.market = gone = FeedLeft()
    gone.pairs = [snap(price=4)]
    run_minutes(p, clock, 30 * 60)
    label = storage.label_rows()[0]
    assert label["ret_1440"] is not None
    day = [r for r in storage.outcome_rows() if r["horizon_minutes"] == 1440]
    assert day and day[0]["price_change_pct"] is not None


# Finding 2: a vanished pair is a rug, an outage is not.
def test_vanished_pair_is_labelled_rugged(storage: Storage) -> None:
    p, market, clock, _ = build(storage)
    for _ in range(3):
        market.pairs = [snap(liquidity=200_000, volume=0)]
        p.run_cycle()
        clock.advance(5)
    market.pairs = []  # delisted: DexScreener returns nothing for the token
    report = p.run_cycle()
    assert report.missing == 1
    clock.advance(60 * 27)
    p.run_cycle()
    assert storage.label_rows()[0]["rugged"] == 1


def test_failed_batch_is_not_counted_as_missing(storage: Storage) -> None:
    p, market, clock, _ = build(storage)
    market.pairs = [snap()]
    p.run_cycle()
    clock.advance(5)
    market.pairs = []
    market.failed = {TokenRef("solana", "Mint111")}
    assert p.run_cycle().missing == 0


# Finding 3: blocks applied after acceptance stop observations but not sampling.
def test_blacklist_after_acceptance_stops_alerts_not_sampling(storage: Storage) -> None:
    p, market, clock, notifier = build(storage)
    market.pairs = [snap(price=1)]
    p.run_cycle()
    clock.advance(5)
    storage.blacklist_add("solana", "Mint111", "manual")
    for price in (2, 3, 4):
        market.pairs = [snap(price=price)]
        p.run_cycle()
        clock.advance(5)
    assert storage.stats()["snapshots"] == 4
    assert storage.observations() == [] and notifier.sent == []


def test_deployer_block_after_acceptance_stops_alerts(storage: Storage) -> None:
    provider = FakeProvider(creators=(Creator("Dev"),))
    p, market, clock, notifier = build(storage, [provider])
    market.pairs = [snap(price=1)]
    p.run_cycle()
    storage.deployer_block("solana", "Dev", "later found rugging", T0)
    for price in (2, 3):
        clock.advance(5)
        market.pairs = [snap(price=price)]
        p.run_cycle()
    assert notifier.sent == []


def test_unscreened_known_token_is_rescreened(storage: Storage) -> None:
    class Flaky(FakeProvider):
        def assess(self, s):  # type: ignore[no-untyped-def]
            self.calls += 1
            status = ScreenStatus.UNSCREENED if self.calls == 1 else ScreenStatus.REJECTED
            return RiskAssessment(self.name, status, ("x",) if status is ScreenStatus.REJECTED else ())

    flaky = Flaky()
    p, market, clock, notifier = build(storage, [flaky])
    market.pairs = [snap(price=1)]
    p.run_cycle()  # accepted as unscreened
    for price in (2, 3, 4):
        clock.advance(11)  # past the negative-cache TTL
        market.pairs = [snap(price=price)]
        p.run_cycle()
    assert flaky.calls >= 2
    assert storage.latest_screen("solana", "Mint111") is ScreenStatus.REJECTED
    assert notifier.sent == []


# Finding 4: probe errors never print the key.
@responses.activate
def test_probe_pocket_error_hides_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DSB_POCKET_UNIVERSE_BASE_URL", "https://pu.example")
    monkeypatch.setenv("DSB_POCKET_UNIVERSE_API_KEY", "SUPERSECRETKEY")
    responses.get("https://pu.example/rug_check/SUPERSECRETKEY", status=401)
    assert cli.main(["probe", "pocket", "Mint1"]) == 1
    captured = capsys.readouterr()
    assert "SUPERSECRETKEY" not in captured.out + captured.err
    assert "status=401" in captured.err


# Finding 5: encoded secrets and tracebacks are redacted.
def test_redacts_encoded_secret_and_traceback() -> None:
    f = RedactingFilter(["a+b=c@d"])
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "GET /rug_check/a%2Bb%3Dc%40d", (), None)
    f.filter(record)
    assert "a%2Bb" not in record.getMessage()
    try:
        raise ValueError("token a+b=c@d leaked")
    except ValueError:
        exc = sys.exc_info()
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", (), exc)
    f.filter(record)
    assert record.exc_info is None and record.exc_text is not None
    assert "a+b=c@d" not in record.exc_text and "***" in record.exc_text


# Finding 8: pending outcomes do not dilute coverage.
def test_coverage_excludes_pending_outcomes(storage: Storage) -> None:
    p, market, clock, _ = build(storage)
    for price in (1, 2, 3):
        market.pairs = [snap(price=price)]
        p.run_cycle()
        clock.advance(5)
    clock.advance(55)
    market.pairs = [snap(price=6)]
    p.run_cycle()
    rise = next(g for g in evaluate(storage) if g.group == "RISE")
    assert rise.horizons[60].coverage == 1.0  # one matured, one measured
    assert rise.horizons[1440].coverage is None  # nothing matured yet


# Finding 9: migrated v1 tokens are kept but excluded from labels; other tables survive.
def test_v1_migration_preserves_tables_and_excludes_backfill(tmp_path: Path) -> None:
    db = tmp_path / "v1.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta VALUES ('schema_version', '1');"
        "CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, chain_id TEXT NOT NULL,"
        " token_address TEXT NOT NULL, pair_address TEXT NOT NULL, dex_id TEXT NOT NULL,"
        " symbol TEXT NOT NULL, price_usd REAL NOT NULL, liquidity_usd REAL NOT NULL,"
        " volume_h24 REAL NOT NULL, observed_at TEXT NOT NULL);"
        "INSERT INTO snapshots VALUES"
        " (NULL, 'solana', 'Old1', 'P', 'd', 'OLD', 1, 1, 1, '2026-01-01T00:00:00+00:00');"
        "CREATE TABLE observations (id INTEGER PRIMARY KEY AUTOINCREMENT, chain_id TEXT NOT NULL,"
        " token_address TEXT NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL,"
        " change_pct REAL NOT NULL, window_minutes INTEGER NOT NULL, observed_at TEXT NOT NULL,"
        " notified INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO observations VALUES"
        " (NULL, 'solana', 'Old1', 'OLD', 'RISE', 120, 30, '2026-01-01T00:00:00+00:00', 1);"
        "CREATE TABLE blacklist (chain_id TEXT NOT NULL, token_address TEXT NOT NULL,"
        " reason TEXT NOT NULL, added_at TEXT NOT NULL, PRIMARY KEY (chain_id, token_address));"
        "INSERT INTO blacklist VALUES ('solana', 'Bad', 'rug', '2026-01-01T00:00:00+00:00');"
    )
    conn.commit()
    conn.close()
    s = Storage(db)
    assert s.schema_version() == SCHEMA_VERSION
    assert s.is_blacklisted("solana", "Bad") and len(s.observations()) == 1
    assert s.unlabelled_tokens(T0 + timedelta(days=10), T0 - timedelta(days=10)) == []
    assert s.tracked_tokens(T0 - timedelta(days=10), T0 + timedelta(days=10)) == []
    s.close()


# Finding 10: unscreened results are negatively cached; creator-only lookups use the full TTL.
def test_negative_cache(storage: Storage) -> None:
    unknown = FakeProvider(ScreenStatus.UNSCREENED, chains=("base",))
    creator = FakeProvider(ScreenStatus.UNSCREENED, name="es", chains=("base",), creator_only=True)
    # Strict mode rejects the unscreened token, so it is re-screened on every cycle.
    p, market, clock, _ = build(storage, [unknown, creator], Settings(strict_screening=True))
    market.pairs = [snap(chain="base", address="0xt1")]
    for _ in range(3):
        assert p.run_cycle().rejected == {"risk_rejected": 1}
        clock.advance(2)
    assert unknown.calls == 1 and creator.calls == 1
    clock.advance(11)  # past the 10-minute negative TTL, inside the 60-minute TTL
    p.run_cycle()
    assert unknown.calls == 2 and creator.calls == 1


# Finding 12: EVM addresses are de-duplicated case-insensitively.
def test_tracked_and_latest_refs_deduplicated(storage: Storage) -> None:
    seen: list[list[TokenRef]] = []

    class Recording(FakeMarket):
        def latest_tokens(self) -> list[TokenRef]:
            return [TokenRef("base", "0xABC")]

        def best_pairs(self, refs: list[TokenRef]) -> PairFetch:
            seen.append(list(refs))
            return super().best_pairs(refs)

    market = Recording()
    p = Pipeline(Settings(), storage, market, [FakeProvider()], None, clock=FakeClock())
    market.pairs = [snap(chain="base", address="0xABC")]
    p.run_cycle()
    p.run_cycle()
    assert seen[-1] == [TokenRef("base", "0xabc")]


# Low findings.
def test_percentage_scale_detected_only_from_values_above_one() -> None:
    assert (
        top_holder_share([{"address": "a", "percent": "0.6"}, {"address": "b", "percent": "0.5"}], "") == 1.1
    )
    assert (
        top_holder_share([{"address": "a", "percent": "30"}, {"address": "b", "percent": "0.5"}], "") == 0.305
    )
