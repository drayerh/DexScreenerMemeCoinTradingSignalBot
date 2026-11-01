"""One research cycle: discover, fetch, screen, store, observe, notify."""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from .config import Settings
from .models import Observation, PairSnapshot, ScreenResult, ScreenStatus, TokenRef
from .signals import detect
from .storage import Storage, utc_now

log = logging.getLogger(__name__)


class MarketData(Protocol):
    def latest_tokens(self) -> list[TokenRef]: ...
    def best_pairs(self, refs: list[TokenRef]) -> list[PairSnapshot]: ...


class Screener(Protocol):
    def supports(self, chain_id: str) -> bool: ...
    def screen(self, mint: str) -> ScreenResult: ...


class Notifier(Protocol):
    def send(self, obs: Observation) -> bool: ...


@dataclass
class CycleReport:
    discovered: int = 0
    accepted: int = 0
    rejected: Counter[str] = field(default_factory=Counter)
    snapshots: int = 0
    observations: int = 0
    notified: int = 0
    duration_seconds: float = 0.0
    error: str = ""


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        market: MarketData,
        screener: Screener,
        notifier: Notifier | None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.s = settings
        self.storage = storage
        self.market = market
        self.screener = screener
        self.notifier = notifier
        self.clock = clock

    def _screen(self, snap: PairSnapshot, now: datetime) -> ScreenResult:
        if not self.screener.supports(snap.chain_id):
            return ScreenResult(ScreenStatus.UNSCREENED, "chain not supported by rugcheck")
        cached = self.storage.cached_screen(snap.chain_id, snap.token_address, self.s.screen_ttl_minutes, now)
        if cached is not None:
            return cached
        result = self.screener.screen(snap.token_address)
        self.storage.save_screen(snap.chain_id, snap.token_address, result, now)
        return result

    def _reject_reason(self, snap: PairSnapshot, now: datetime) -> str:
        if self.storage.is_blacklisted(snap.chain_id, snap.token_address):
            return "blacklisted"
        if snap.liquidity_usd < self.s.min_liquidity_usd:
            return "low_liquidity"
        if snap.volume_h24 > self.s.max_volume_to_liquidity * snap.liquidity_usd:
            return "suspicious_volume"
        if self._screen(snap, now).status is ScreenStatus.REJECTED:
            return "rugcheck_rejected"
        return ""

    def run_cycle(self) -> CycleReport:
        started = time.monotonic()
        report = CycleReport()
        now = self.clock()
        try:
            refs = self.market.latest_tokens()
        except Exception as exc:  # NFR-1
            log.warning("discovery failed error=%s", exc)
            report.error = "discovery failed"
            report.duration_seconds = time.monotonic() - started
            return report
        report.discovered = len(refs)
        pairs = self.market.best_pairs(refs) if refs else []
        report.rejected["no_pair_data"] = max(0, len(refs) - len(pairs))

        accepted: list[PairSnapshot] = []
        for snap in pairs:
            try:
                reason = self._reject_reason(snap, now)
            except Exception as exc:  # NFR-1: one token never aborts the cycle
                log.warning("screening error token=%s error=%s", snap.token_address, exc)
                reason = "screening_error"
            if reason:
                report.rejected[reason] += 1
            else:
                accepted.append(snap)

        with self.storage.transaction():
            for snap in accepted:
                self.storage.add_snapshot(snap, now)
        report.accepted = report.snapshots = len(accepted)

        since = now - timedelta(minutes=self.s.window_minutes)
        for snap in accepted:
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

        self.storage.prune(now - timedelta(days=self.s.retention_days))
        report.rejected = +report.rejected  # drop zero counts
        report.duration_seconds = time.monotonic() - started
        log.info(
            "cycle discovered=%d accepted=%d rejected=%s observations=%d notified=%d duration=%.1fs",
            report.discovered,
            report.accepted,
            dict(report.rejected),
            report.observations,
            report.notified,
            report.duration_seconds,
        )
        return report
