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
from .labels import label_outcomes, label_tokens
from .models import Observation, PairSnapshot, ScreenStatus, TokenRef
from .risk import RiskAssessment, RiskProvider, combine, creators_of
from .signals import detect
from .storage import Storage, utc_now

log = logging.getLogger(__name__)


class MarketData(Protocol):
    def latest_tokens(self) -> list[TokenRef]: ...
    def best_pairs(self, refs: list[TokenRef]) -> list[PairSnapshot]: ...


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
        results: list[RiskAssessment] = []
        for provider in sorted(self.providers, key=lambda p: p.creator_only):
            if not provider.supports(snap.chain_id):
                continue
            if provider.creator_only and creators_of(results):
                continue  # deployer already known; skip the extra lookup
            cached = self.storage.latest_risk(snap.chain_id, snap.token_address, provider.name, not_before)
            if cached is None:
                cached = provider.assess(snap)
                self.storage.add_risk(snap.chain_id, snap.token_address, cached, now)
            results.append(cached)
        return results

    def _reject_reason(self, snap: PairSnapshot, now: datetime) -> str:
        """Cheap local filters first, then remote risk providers (REVIEW finding 19)."""
        if self.storage.is_blacklisted(snap.chain_id, snap.token_address):
            return "blacklisted"
        if snap.liquidity_usd < self.s.min_liquidity_usd:
            return "low_liquidity"
        if snap.volume_h24 > self.s.max_volume_to_liquidity * snap.liquidity_usd:
            return "suspicious_volume"
        assessments = self._assess(snap, now)
        for creator in creators_of(assessments):
            source = next(a.provider for a in assessments if creator in a.creators)
            self.storage.link_deployer(snap.chain_id, snap.token_address, creator, source, now)
            if self.storage.deployer_block_reason(snap.chain_id, creator.address, self.s.deployer_max_rugs):
                return "deployer_blocked"
        verdict = combine(assessments, strict=self.s.strict_screening)
        self.storage.save_screen(snap.chain_id, snap.token_address, verdict, now)
        if verdict.status is ScreenStatus.REJECTED:
            return "risk_rejected"
        return ""

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
        tracked = self.storage.tracked_tokens(now - timedelta(hours=self.s.track_hours))
        report.tracked = len(tracked)
        refs = list(dict.fromkeys([*latest, *tracked]))
        pairs = self.market.best_pairs(refs) if refs else []
        report.rejected["no_pair_data"] = max(0, len(refs) - len(pairs))

        to_store: list[PairSnapshot] = []
        for snap in pairs:
            if self.storage.is_known(snap.chain_id, snap.token_address):
                # Already accepted: keep sampling whatever happens, so collapses are
                # measured rather than filtered out (REVIEW finding 1).
                to_store.append(snap)
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
                report.accepted += 1

        with self.storage.transaction():
            for snap in to_store:
                self.storage.register_token(snap, now)
                self.storage.add_snapshot(snap, now)
        report.snapshots = len(to_store)

        self._observe(to_store, now, report)
        report.labelled = label_outcomes(self.storage, now) + label_tokens(
            self.storage, now, self.s.rug_liq_drop_pct
        )
        self.storage.prune(now - timedelta(days=self.s.retention_days))
        self.storage.record_cycle(now, ok=True)
        report.rejected = +report.rejected  # drop zero counts
        report.duration_seconds = time.monotonic() - started
        log.info(
            "cycle discovered=%d tracked=%d accepted=%d rejected=%s snapshots=%d observations=%d"
            " notified=%d labelled=%d duration=%.1fs",
            report.discovered,
            report.tracked,
            report.accepted,
            dict(report.rejected),
            report.snapshots,
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
