"""Observation rules (FR-8). Pure functions: no I/O."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from .models import Observation, ObservationKind
from .storage import Point


def pct_change(start: float, end: float) -> float:
    if start <= 0:
        return 0.0
    return (end - start) / start * 100.0


def detect(
    chain_id: str,
    token_address: str,
    points: Sequence[Point],
    *,
    now: datetime,
    window_minutes: int,
    min_points: int,
    rise_pct: float,
    fall_pct: float,
    liq_drop_pct: float,
) -> list[Observation]:
    """Compare the oldest and newest point in the window (points must be oldest first)."""
    if len(points) < min_points:
        return []
    first, last = points[0], points[-1]
    if last.observed_at < first.observed_at:
        raise ValueError("points must be ordered oldest first")
    price_change = pct_change(first.price_usd, last.price_usd)
    liq_change = pct_change(first.liquidity_usd, last.liquidity_usd)

    found: list[Observation] = []

    def add(kind: ObservationKind, change: float) -> None:
        found.append(
            Observation(chain_id, token_address, last.symbol, kind, round(change, 2), window_minutes, now)
        )

    if price_change >= rise_pct:
        add(ObservationKind.RISE, price_change)
    elif price_change <= -fall_pct:
        add(ObservationKind.FALL, price_change)
    if liq_change <= -liq_drop_pct:
        add(ObservationKind.LIQUIDITY_DROP, liq_change)
    return found
