"""Outcome labelling (REVIEW findings 1, 2, 5; review round 2, findings 1, 2, 7). Idempotent."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from .signals import pct_change
from .storage import HORIZONS_MINUTES, Point, Storage

LABEL_WINDOW = timedelta(hours=24)


def tolerance(horizon_minutes: int) -> timedelta:
    """How late a measurement may be and still count for a horizon."""
    return timedelta(minutes=max(30, horizon_minutes // 10))


MAX_HORIZON = timedelta(minutes=max(HORIZONS_MINUTES)) + tolerance(max(HORIZONS_MINUTES))


def tracking_window(track_hours: int) -> timedelta:
    """Tokens must be sampled until the longest horizon plus its tolerance has passed,
    or the +24h measurement can never be taken (review round 2, finding 1)."""
    return max(timedelta(hours=track_hours), MAX_HORIZON)


def _first_at_or_after(points: Sequence[Point], target: datetime, latest: datetime) -> Point | None:
    for p in points:
        if target <= p.observed_at <= latest:
            return p
    return None


def rugged(
    points: Sequence[Point], rug_liq_drop_pct: float
) -> tuple[float | None, float | None, bool | None]:
    """(peak, trough after peak, rugged) for a liquidity path; None when under 2 points."""
    if len(points) < 2:
        return None, None, None
    peak_index = max(range(len(points)), key=lambda i: points[i].liquidity_usd)
    peak = points[peak_index].liquidity_usd
    trough = min(p.liquidity_usd for p in points[peak_index:])
    return peak, trough, bool(peak > 0 and trough <= peak * (1 - rug_liq_drop_pct / 100))


def label_outcomes(storage: Storage, now: datetime, rug_liq_drop_pct: float, oldest: datetime) -> int:
    """Forward price and liquidity change for each observation at each horizon.

    At the 24h horizon the observation also gets ``rugged_after``: a rug judged only
    on the liquidity path *after* the observation, so it carries no pre-event
    information (review round 2, finding 7).
    """
    written = 0
    for obs_id, chain, token, observed_at, horizon in storage.pending_outcomes(now, oldest):
        target = observed_at + timedelta(minutes=horizon)
        late = target + tolerance(horizon)
        base_points = storage.points_between(chain, token, observed_at - timedelta(hours=1), observed_at)
        later = storage.points_between(chain, token, target, late)
        hit = _first_at_or_after(later, target, late)
        rug_after: bool | None = None
        if horizon == max(HORIZONS_MINUTES):
            _, _, rug_after = rugged(
                storage.points_between(chain, token, observed_at, target), rug_liq_drop_pct
            )
        if base_points and hit is not None:
            base = base_points[-1]
            storage.add_outcome(
                obs_id,
                horizon,
                round(pct_change(base.price_usd, hit.price_usd), 4),
                round(pct_change(base.liquidity_usd, hit.liquidity_usd), 4),
                hit.observed_at,
                rugged_after=rug_after,
            )
            written += 1
        elif now > late:
            storage.add_outcome(obs_id, horizon, None, None, None, rugged_after=rug_after)  # gap: no coverage
            written += 1
    return written


def label_tokens(storage: Storage, now: datetime, rug_liq_drop_pct: float, oldest: datetime) -> int:
    """Rug label and baseline forward returns for every live token after its window."""
    written = 0
    for chain, token, first_seen in storage.unlabelled_tokens(now - MAX_HORIZON, oldest):
        points = storage.points_between(chain, token, first_seen, first_seen + MAX_HORIZON)
        in_window = [p for p in points if p.observed_at <= first_seen + LABEL_WINDOW]
        peak, trough, rug = rugged(in_window, rug_liq_drop_pct)
        returns: dict[int, float | None] = {}
        for horizon in HORIZONS_MINUTES:
            target = first_seen + timedelta(minutes=horizon)
            hit = _first_at_or_after(points, target, target + tolerance(horizon))
            returns[horizon] = (
                round(pct_change(points[0].price_usd, hit.price_usd), 4) if points and hit else None
            )
        storage.add_token_label(
            chain, token, first_seen, peak=peak, trough=trough, rugged=rug, returns=returns, now=now
        )
        written += 1
    return written
