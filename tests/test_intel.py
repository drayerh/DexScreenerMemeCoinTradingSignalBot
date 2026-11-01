"""Deployer intelligence, tracking, labelling and evaluation (REVIEW findings 1-6, 11, 16, 17)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

from dexsignal.config import Settings
from dexsignal.evaluate import evaluate, render, to_dicts, wilson
from dexsignal.labels import label_outcomes, label_tokens
from dexsignal.logsetup import JsonFormatter, RedactingFilter
from dexsignal.models import ObservationKind, ScreenStatus
from dexsignal.pipeline import Pipeline
from dexsignal.risk import Creator
from dexsignal.storage import Storage

from .conftest import T0, FakeClock, FakeMarket, FakeNotifier, FakeProvider, snap


def build(
    storage: Storage, settings: Settings, providers: list[FakeProvider]
) -> tuple[Pipeline, FakeMarket, FakeClock]:
    market, clock = FakeMarket(), FakeClock()
    return Pipeline(settings, storage, market, providers, FakeNotifier(), clock=clock), market, clock


def test_manually_blocked_deployer_rejects_new_token(storage: Storage, settings: Settings) -> None:
    storage.deployer_block("solana", "DevWallet", "known rugger", T0)
    p, market, _ = build(storage, settings, [FakeProvider(creators=(Creator("DevWallet"),))])
    market.pairs = [snap(address="NewToken")]
    report = p.run_cycle()
    assert report.rejected == {"deployer_blocked": 1}
    assert storage.deployer_unblock("solana", "DevWallet")
    assert not storage.deployer_unblock("solana", "DevWallet")


def test_malicious_creator_is_flagged_and_blocks(storage: Storage, settings: Settings) -> None:
    p, market, _ = build(storage, settings, [FakeProvider(creators=(Creator("Evil", True),))])
    market.pairs = [snap(address="T1")]
    assert p.run_cycle().rejected == {"deployer_blocked": 1}
    row = next(d for d in storage.deployers() if d["address"] == "Evil")
    assert row["malicious"] == 1 and row["tokens"] == 1


def test_serial_rugger_blocked_after_labelled_rugs(storage: Storage) -> None:
    settings = Settings(deployer_max_rugs=2)
    for token in ("R1", "R2"):
        storage.link_deployer("solana", token, Creator("Rugger"), "fake", T0)
        storage.add_token_label("solana", token, T0, peak=1, trough=0, rugged=True, returns={}, now=T0)
    assert storage.deployer_block_reason("solana", "Rugger", 2) == "deployer linked to 2 labelled rugs"
    p, market, _ = build(storage, settings, [FakeProvider(creators=(Creator("Rugger"),))])
    market.pairs = [snap(address="R3")]
    assert p.run_cycle().rejected == {"deployer_blocked": 1}


def test_creator_only_provider_skipped_when_creator_known(storage: Storage, settings: Settings) -> None:
    main = FakeProvider(chains=("base",), creators=(Creator("0xdev"),))
    fallback = FakeProvider(ScreenStatus.UNSCREENED, name="es", chains=("base",), creator_only=True)
    p, market, _ = build(storage, settings, [fallback, main])
    market.pairs = [snap(chain="base", address="0xt1")]
    p.run_cycle()
    assert main.calls == 1 and fallback.calls == 0


def test_strict_mode_rejects_unscreened(storage: Storage) -> None:
    p, market, _ = build(storage, Settings(strict_screening=True), [FakeProvider()])
    market.pairs = [snap(chain="base", address="0xt1")]  # provider does not support base
    assert p.run_cycle().rejected == {"risk_rejected": 1}


def test_tracked_token_keeps_being_sampled_through_a_rug(storage: Storage, settings: Settings) -> None:
    """Survivorship: a collapsing token below the liquidity floor is still measured and labelled."""
    p, market, clock = build(storage, settings, [FakeProvider()])
    for liquidity in [200_000, 210_000, 5_000]:
        market.pairs = [snap(liquidity=liquidity, volume=0)]
        p.run_cycle()
        clock.advance(5)
    assert storage.stats()["snapshots"] == 3
    clock.advance(60 * 27)  # past the 24h window plus measurement tolerance
    market.pairs = []
    report = p.run_cycle()
    assert report.tracked == 0  # tracking window has ended
    assert report.labelled >= 1
    label = storage.label_rows()[0]
    assert label["rugged"] == 1 and label["peak_liquidity_usd"] == 210_000


def test_outcome_labels_measured_and_missing(storage: Storage, settings: Settings) -> None:
    p, market, clock = build(storage, settings, [FakeProvider()])
    for price in [1, 2, 3]:
        market.pairs = [snap(price=price)]
        p.run_cycle()
        clock.advance(5)
    assert [o.kind for o in storage.observations()] == [ObservationKind.RISE]
    clock.advance(55)  # one hour after the observation
    market.pairs = [snap(price=6)]
    p.run_cycle()
    rows = {r["horizon_minutes"]: r for r in storage.outcome_rows()}
    assert rows[60]["price_change_pct"] == 100.0
    # No further data: the 6h and 24h horizons become explicit gaps, not silent omissions.
    market.pairs = []
    clock.advance(60 * 26)
    assert label_outcomes(storage, clock(), 80, T0) == 2  # 6h and 24h recorded as gaps
    assert label_outcomes(storage, clock(), 80, T0) == 0  # idempotent
    rows = {r["horizon_minutes"]: r for r in storage.outcome_rows()}
    assert rows[1440]["price_change_pct"] is None


def test_evaluate_reports_baseline_and_kinds(storage: Storage, settings: Settings) -> None:
    p, market, clock = build(storage, settings, [FakeProvider()])
    for price in [1, 2, 3]:
        market.pairs = [snap(price=price)]
        p.run_cycle()
        clock.advance(5)
    clock.advance(60 * 27)
    p.run_cycle()
    groups = evaluate(storage)
    assert groups[0].group.startswith("ACCEPTED TOKENS") and groups[0].n == 1
    assert groups[1].group == "RISE" and groups[1].small_sample
    text = render(groups)
    assert "Wilson 95%" in text and "rug rate" in text
    assert to_dicts(groups)[1]["group"] == "RISE"


def test_evaluate_empty_database(storage: Storage) -> None:
    groups = evaluate(storage)
    assert len(groups) == 1 and groups[0].n == 0
    assert "n=0" in render(groups)


def test_wilson_interval() -> None:
    assert wilson(0, 0) is None
    low, high = wilson(5, 10) or (0, 0)
    assert 0.2 < low < 0.5 < high < 0.8


def test_label_tokens_with_single_point_is_unknown(storage: Storage) -> None:
    storage.register_token(snap(), T0)
    storage.add_snapshot(snap(), T0)
    assert label_tokens(storage, T0 + timedelta(hours=27), 80, T0) == 1
    assert storage.label_rows()[0]["rugged"] is None


def test_heartbeat_and_failed_discovery(storage: Storage, settings: Settings) -> None:
    p, market, _ = build(storage, settings, [FakeProvider()])
    market.fail_discovery = True
    p.run_cycle()
    assert storage.get_meta("last_cycle_at") is not None
    assert storage.get_meta("last_ok_cycle_at") is None
    market.fail_discovery = False
    p.run_cycle()
    assert storage.get_meta("last_ok_cycle_at") is not None


def test_migration_from_v1(tmp_path: Path) -> None:
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
    )
    conn.commit()
    conn.close()
    s = Storage(db)
    assert s.schema_version() == 2
    assert s.is_known("solana", "Old1")
    s.close()


def test_redacting_filter_and_json_formatter() -> None:
    f = RedactingFilter(["SECRET123", ""])
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "url=%s", ("https://h/botSECRET123/send",), None
    )
    assert f.filter(record)
    assert record.getMessage() == "url=https://h/bot***/send"
    assert '"message": "url=https://h/bot***/send"' in JsonFormatter().format(record)
