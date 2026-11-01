"""Outcome labelling (REVIEW findings 1, 2, 5). Idempotent: safe to run every cycle."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from .signals import pct_change
from .storage import HORIZONS_MINUTES, Point, Storage

LABEL_WINDOW = timedelta(hours=24)


def tolerance(horizon_minutes: int) -> timedelta:
    """How late a measurement may be and still count for a horizon."""
    return timedelta(minutes=max(30, horizon_minutes // 10))


def _first_at_or_after(points: Sequence[Point], target: datetime, latest: datetime) -> Point | None:
    for p in points:
        if target <= p.observed_at <= latest:
            return p
    return None


def label_outcomes(storage: Storage, now: datetime) -> int:
    """Forward price and liquidity change for each observation at each horizon."""
    written = 0
    for obs_id, chain, token, observed_at, horizon in storage.pending_outcomes(now):
        target = observed_at + timedelta(minutes=horizon)
        late = target + tolerance(horizon)
        base_points = storage.points_between(chain, token, observed_at - timedelta(hours=1), observed_at)
        later = storage.points_between(chain, token, target, late)
        hit = _first_at_or_after(later, target, late)
        if base_points and hit is not None:
            base = base_points[-1]
            storage.add_outcome(
                obs_id,
                horizon,
                round(pct_change(base.price_usd, hit.price_usd), 4),
                round(pct_change(base.liquidity_usd, hit.liquidity_usd), 4),
                hit.observed_at,
            )
            written += 1
        elif now > late:
            storage.add_outcome(obs_id, horizon, None, None, None)  # missing: counted as no coverage
            written += 1
    return written


def label_tokens(storage: Storage, now: datetime, rug_liq_drop_pct: float) -> int:
    """Rug label and baseline forward returns for every token 24h after first sighting."""
    written = 0
    max_tol = tolerance(max(HORIZONS_MINUTES))
    for chain, token, first_seen in storage.unlabelled_tokens(now - LABEL_WINDOW - max_tol):
        points = storage.points_between(chain, token, first_seen, first_seen + LABEL_WINDOW + max_tol)
        in_window = [p for p in points if p.observed_at <= first_seen + LABEL_WINDOW]
        peak = trough = None
        rugged: bool | None = None
        if len(in_window) >= 2:
            peak_index = max(range(len(in_window)), key=lambda i: in_window[i].liquidity_usd)
            peak = in_window[peak_index].liquidity_usd
            trough = min(p.liquidity_usd for p in in_window[peak_index:])
            rugged = peak > 0 and trough <= peak * (1 - rug_liq_drop_pct / 100)
        returns: dict[int, float | None] = {}
        for horizon in HORIZONS_MINUTES:
            target = first_seen + timedelta(minutes=horizon)
            hit = _first_at_or_after(points, target, target + tolerance(horizon))
            returns[horizon] = (
                round(pct_change(points[0].price_usd, hit.price_usd), 4) if points and hit else None
            )
        storage.add_token_label(
            chain, token, first_seen, peak=peak, trough=trough, rugged=rugged, returns=returns, now=now
        )
        written += 1
    return written
