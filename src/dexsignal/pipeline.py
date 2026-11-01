"""One research cycle: discover, fetch, screen, store, observe, label, notify."""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from .config import Settings
from .labels import MAX_HORIZON, label_outcomes, label_tokens, tracking_window
from .models import Observation, PairSnapshot, ScreenStatus, TokenRef, norm_address
from .providers import PairFetch
from .risk import RiskAssessment, RiskProvider, combine, creators_of
from .signals import detect
from .storage import Storage, utc_now

log = logging.getLogger(__name__)


class MarketData(Protocol):
    def latest_tokens(self) -> list[TokenRef]: ...
    def best_pairs(self, refs: list[TokenRef]) -> PairFetch: ...


class Notifier(Protocol):
    def send(self, obs: Observation) -> bool: ...


@dataclass
class CycleReport:
    discovered: int = 0
    tracked: int = 0
    accepted: int = 0
    rejected: Counter[str] = field(default_factory=Counter)
    snapshots: int = 0
    observations: int = 0
    notified: int = 0
    labelled: int = 0
    missing: int = 0
    duration_seconds: float = 0.0
    error: str = ""


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        market: MarketData,
        providers: Sequence[RiskProvider],
        notifier: Notifier | None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.s = settings
        self.storage = storage
        self.market = market
        self.providers = list(providers)
        self.notifier = notifier
        self.clock = clock

    # --- screening -------------------------------------------------------------
    def _assess(self, snap: PairSnapshot, now: datetime) -> list[RiskAssessment]:
        """Run applicable providers with point-in-time caching; creator-only ones last, if needed."""
        not_before = now - timedelta(minutes=self.s.screen_ttl_minutes)
        negative_not_before = now - timedelta(minutes=self.s.negative_ttl_minutes)
        results: list[RiskAssessment] = []
        for provider in sorted(self.providers, key=lambda p: p.creator_only):
            if not provider.supports(snap.chain_id):
                continue
            if provider.creator_only and creators_of(results):
                continue  # deployer already known; skip the extra lookup
            # Creator-only lookups are UNSCREENED by design, so they get the full TTL.
            unscreened_not_before = not_before if provider.creator_only else negative_not_before
            cached = self.storage.latest_risk(
                snap.chain_id, snap.token_address, provider.name, not_before, unscreened_not_before
            )
            if cached is None:
                cached = provider.assess(snap)
                self.storage.add_risk(snap.chain_id, snap.token_address, cached, now)
            results.append(cached)
        return results

    def _screen_providers(self, snap: PairSnapshot, now: datetime) -> str:
        """Run risk providers and deployer checks; return a rejection reason or ''."""
        assessments = self._assess(snap, now)
        blocked = ""
        for creator in creators_of(assessments):
            source = next(a.provider for a in assessments if creator in a.creators)
            self.storage.link_deployer(snap.chain_id, snap.token_address, creator, source, now)
            if not blocked and self.storage.deployer_block_reason(
                snap.chain_id, creator.address, self.s.deployer_max_rugs
            ):
                blocked = "deployer_blocked"
        verdict = combine(assessments, strict=self.s.strict_screening)
        self.storage.save_screen(snap.chain_id, snap.token_address, verdict, now)
        if blocked:
            return blocked
        return "risk_rejected" if verdict.status is ScreenStatus.REJECTED else ""

    def _reject_reason(self, snap: PairSnapshot, now: datetime) -> str:
        """Cheap local filters first, then remote risk providers (REVIEW finding 19)."""
        if self.storage.is_blacklisted(snap.chain_id, snap.token_address):
            return "blacklisted"
        if snap.liquidity_usd < self.s.min_liquidity_usd:
            return "low_liquidity"
        if snap.volume_h24 > self.s.max_volume_to_liquidity * snap.liquidity_usd:
            return "suspicious_volume"
        return self._screen_providers(snap, now)

    def _blocked_now(self, snap: PairSnapshot) -> bool:
        """A tracked token is still sampled, but blocked tokens raise no observations
        (review round 2, finding 3)."""
        if self.storage.is_blacklisted(snap.chain_id, snap.token_address):
            return True
        if self.storage.latest_screen(snap.chain_id, snap.token_address) is ScreenStatus.REJECTED:
            return True
        return any(
            self.storage.deployer_block_reason(snap.chain_id, d, self.s.deployer_max_rugs)
            for d in self.storage.deployers_of(snap.chain_id, snap.token_address)
        )

    def _missing_snapshot(self, ref: TokenRef) -> PairSnapshot:
        """A tracked pair that vanished, or returned no price or liquidity: recorded as zero
        so rugs by delisting are measured, not dropped (review round 2, finding 2)."""
        symbol = self.storage.symbol_of(ref.chain_id, ref.token_address)
        return PairSnapshot(ref.chain_id, ref.token_address, "", "missing", symbol, 0.0, 0.0, 0.0)

    def _partition(
        self,
        refs: list[TokenRef],
        found: dict[TokenRef, PairSnapshot],
        *,
        failed: set[TokenRef],
        tracked_set: set[TokenRef],
        now: datetime,
        report: CycleReport,
    ) -> tuple[list[PairSnapshot], list[PairSnapshot]]:
        """Split this cycle's tokens into (to store, may raise observations)."""
        to_store: list[PairSnapshot] = []
        observable: list[PairSnapshot] = []
        for ref in refs:
            snap = found.get(ref)
            if ref in tracked_set:
                if snap is None:
                    if ref in failed:
                        continue  # outage, not evidence of a rug
                    snap = self._missing_snapshot(ref)
                    report.missing += 1
                elif self.storage.latest_screen(snap.chain_id, snap.token_address) in (
                    None,
                    ScreenStatus.UNSCREENED,
                ):
                    try:
                        self._screen_providers(snap, now)  # retry screening for unscreened tokens
                    except Exception as exc:
                        log.warning(
                            "re-screen error token=%s error=%s", ref.token_address, type(exc).__name__
                        )
                to_store.append(snap)
                if not self._blocked_now(snap):
                    observable.append(snap)
                continue
            if snap is None:
                continue
            try:
                reason = self._reject_reason(snap, now)
            except Exception as exc:  # NFR-1: one token never aborts the cycle
                log.warning("screening error token=%s error=%s", snap.token_address, type(exc).__name__)
                reason = "screening_error"
            if reason:
                report.rejected[reason] += 1
            else:
                to_store.append(snap)
                observable.append(snap)
                report.accepted += 1

        return to_store, observable

    # --- cycle -------------------------------------------------------------------
    def run_cycle(self) -> CycleReport:
        started = time.monotonic()
        report = CycleReport()
        now = self.clock()
        try:
            latest = self.market.latest_tokens()
        except Exception as exc:  # NFR-1
            log.warning("discovery failed error=%s", type(exc).__name__)
            report.error = "discovery failed"
            report.duration_seconds = time.monotonic() - started
            self.storage.record_cycle(now, ok=False)
            return report
        report.discovered = len(latest)
        tracked = self.storage.tracked_tokens(now - tracking_window(self.s.track_hours), now - MAX_HORIZON)
        tracked_set = {TokenRef(t.chain_id, norm_address(t.token_address)) for t in tracked}
        report.tracked = len(tracked_set)
        refs = list(
            dict.fromkeys(TokenRef(r.chain_id, norm_address(r.token_address)) for r in [*latest, *tracked])
        )
        fetch = self.market.best_pairs(refs) if refs else PairFetch()
        found = {TokenRef(p.chain_id, norm_address(p.token_address)): p for p in fetch.pairs}
        report.rejected["no_pair_data"] = len([r for r in refs if r not in found and r not in tracked_set])

        to_store, observable = self._partition(
            refs, found, failed=fetch.failed, tracked_set=tracked_set, now=now, report=report
        )

        with self.storage.transaction():
            for snap in to_store:
                self.storage.register_token(snap, now)
                self.storage.add_snapshot(snap, now)
        report.snapshots = len(to_store)

        self._observe(observable, now, report)
        oldest = now - timedelta(days=self.s.retention_days)
        report.labelled = label_outcomes(self.storage, now, self.s.rug_liq_drop_pct, oldest) + label_tokens(
            self.storage, now, self.s.rug_liq_drop_pct, oldest
        )
        self.storage.prune(oldest)
        self.storage.record_cycle(now, ok=True)
        report.rejected = +report.rejected  # drop zero counts
        report.duration_seconds = time.monotonic() - started
        log.info(
            "cycle discovered=%d tracked=%d accepted=%d rejected=%s snapshots=%d missing=%d"
            " observations=%d notified=%d labelled=%d duration=%.1fs",
            report.discovered,
            report.tracked,
            report.accepted,
            dict(report.rejected),
            report.snapshots,
            report.missing,
            report.observations,
            report.notified,
            report.labelled,
            report.duration_seconds,
        )
        return report

    def _observe(self, snaps: list[PairSnapshot], now: datetime, report: CycleReport) -> None:
        since = now - timedelta(minutes=self.s.window_minutes)
        for snap in snaps:
            points = self.storage.history(snap.chain_id, snap.token_address, since)
            for obs in detect(
                snap.chain_id,
                snap.token_address,
                points,
                now=now,
                window_minutes=self.s.window_minutes,
                min_points=self.s.min_points,
                rise_pct=self.s.rise_pct,
                fall_pct=self.s.fall_pct,
                liq_drop_pct=self.s.liq_drop_pct,
            ):
                if self.storage.recently_observed(obs, self.s.alert_cooldown_minutes):
                    continue
                obs_id = self.storage.add_observation(obs)
                report.observations += 1
                log.info(
                    "observation kind=%s token=%s chain=%s change=%.1f%%",
                    obs.kind.value,
                    obs.symbol,
                    obs.chain_id,
                    obs.change_pct,
                )
                if self.notifier is not None and self.notifier.send(obs):
                    self.storage.mark_notified(obs_id)
                    report.notified += 1
