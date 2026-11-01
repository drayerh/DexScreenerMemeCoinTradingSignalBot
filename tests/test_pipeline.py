"""Acceptance criteria 1-5, 7, 8 from docs/SPEC.md section 7."""

from __future__ import annotations

from dexsignal.config import Settings
from dexsignal.models import ObservationKind, ScreenStatus
from dexsignal.pipeline import Pipeline
from dexsignal.storage import Storage

from .conftest import FakeClock, FakeMarket, FakeNotifier, FakeProvider, snap


def make(storage: Storage, settings: Settings, **kw: object) -> tuple[Pipeline, FakeMarket, FakeClock]:
    market = FakeMarket()
    clock = FakeClock()
    providers = kw.get("providers", [FakeProvider()])
    notifier = kw.get("notifier", FakeNotifier())
    p = Pipeline(settings, storage, market, providers, notifier, clock=clock)  # type: ignore[arg-type]
    return p, market, clock


def run_series(p: Pipeline, market: FakeMarket, clock: FakeClock, prices: list[float], **kw: float) -> None:
    for price in prices:
        market.pairs = [snap(price=price, **kw)]  # type: ignore[arg-type]
        p.run_cycle()
        clock.advance(5)


def test_history_is_kept_one_row_per_cycle(storage: Storage, settings: Settings) -> None:
    p, market, clock = make(storage, settings)
    run_series(p, market, clock, [1, 1.1, 1.2, 1.3])
    assert storage.stats()["snapshots"] == 4  # AC-2


def test_doubling_price_reports_one_rise_within_cooldown(storage: Storage, settings: Settings) -> None:
    notifier = FakeNotifier()
    p, market, clock = make(storage, settings, notifier=notifier)
    run_series(p, market, clock, [1, 1.5, 2.5, 3, 4, 5])
    obs = storage.observations()
    assert [o.kind for o in obs] == [ObservationKind.RISE]  # AC-1
    assert obs[0].change_pct >= 100
    assert len(notifier.sent) == 1
    assert storage.stats()["notified"] == 1


def test_falling_series_never_reports_rise(storage: Storage, settings: Settings) -> None:
    p, market, clock = make(storage, settings)
    run_series(p, market, clock, [4, 3, 2, 1])
    kinds = {o.kind for o in storage.observations()}
    assert ObservationKind.RISE not in kinds  # AC-3
    assert ObservationKind.FALL in kinds


def test_liquidity_drop_is_reported(storage: Storage, settings: Settings) -> None:
    p, market, clock = make(storage, settings)
    for liq in [200_000, 150_000, 90_000]:
        market.pairs = [snap(liquidity=liq, volume=0)]
        p.run_cycle()
        clock.advance(5)
    assert [o.kind for o in storage.observations()] == [ObservationKind.LIQUIDITY_DROP]


def test_too_few_points_reports_nothing(storage: Storage, settings: Settings) -> None:
    p, market, clock = make(storage, settings)
    run_series(p, market, clock, [1, 10])
    assert storage.observations() == []


def test_rejections_are_never_stored(storage: Storage, settings: Settings) -> None:
    p, market, _ = make(storage, settings, providers=[FakeProvider(ScreenStatus.REJECTED)])
    storage.blacklist_add("ethereum", "0xABC")
    market.pairs = [
        snap(address="Danger1"),  # rugcheck rejected
        snap(address="Poor1", liquidity=1_000),  # illiquid
        snap(address="Wash1", liquidity=60_000, volume=60_000 * 51),  # suspicious volume
        snap(chain="ethereum", address="0xabc"),  # blacklisted, case-insensitive EVM
    ]
    report = p.run_cycle()
    assert storage.stats()["snapshots"] == 0  # AC-5
    assert report.rejected == {
        "risk_rejected": 1,
        "low_liquidity": 1,
        "suspicious_volume": 1,
        "blacklisted": 1,
    }


def test_non_solana_tokens_are_stored_unscreened(storage: Storage, settings: Settings) -> None:
    provider = FakeProvider()
    p, market, _ = make(storage, settings, providers=[provider])
    market.pairs = [snap(chain="base", address="0xdef")]
    p.run_cycle()
    assert provider.calls == 0
    assert storage.stats()["snapshots"] == 1


def test_rejected_tokens_use_cached_risk_checks(storage: Storage, settings: Settings) -> None:
    provider = FakeProvider(ScreenStatus.REJECTED)
    p, market, clock = make(storage, settings, providers=[provider])
    run_series(p, market, clock, [1, 1, 1])
    assert provider.calls == 1
    clock.advance(settings.screen_ttl_minutes + 1)
    p.run_cycle()
    assert provider.calls == 2


def test_accepted_tokens_are_not_rescreened(storage: Storage, settings: Settings) -> None:
    provider = FakeProvider()
    p, market, clock = make(storage, settings, providers=[provider])
    run_series(p, market, clock, [1, 1, 1])
    assert provider.calls == 1


def test_screener_crash_does_not_stop_cycle(storage: Storage, settings: Settings) -> None:
    p, market, _ = make(storage, settings, providers=[FakeProvider(raises=True)])
    market.pairs = [snap(address="A"), snap(chain="base", address="0xB")]
    report = p.run_cycle()
    assert report.rejected == {"screening_error": 1}  # AC-4
    assert report.accepted == 1


def test_discovery_failure_returns_report(storage: Storage, settings: Settings) -> None:
    p, market, _ = make(storage, settings)
    market.fail_discovery = True
    report = p.run_cycle()
    assert report.error == "discovery failed"


def test_notification_failure_still_stores_observation(storage: Storage, settings: Settings) -> None:
    p, market, clock = make(storage, settings, notifier=FakeNotifier(ok=False))
    run_series(p, market, clock, [1, 2, 3])
    assert len(storage.observations()) == 1  # AC-7
    assert storage.stats()["notified"] == 0


def test_no_notifier_configured(storage: Storage, settings: Settings) -> None:
    p, market, clock = make(storage, settings, notifier=None)
    run_series(p, market, clock, [1, 2, 3])
    assert len(storage.observations()) == 1  # AC-8


def test_old_snapshots_are_pruned(storage: Storage) -> None:
    settings = Settings(retention_days=2)
    p, market, clock = make(storage, settings)
    run_series(p, market, clock, [1])
    clock.advance(60 * 24 * 3)
    run_series(p, market, clock, [1])
    assert storage.stats()["snapshots"] == 1
