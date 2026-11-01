"""Evidence report: does each observation kind beat the base rate? (REVIEW findings 2, 3)."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any

from .storage import HORIZONS_MINUTES, Storage

SMALL_SAMPLE = 30


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score 95% interval for a proportion; None when n == 0."""
    if n == 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass
class HorizonStats:
    n: int
    coverage: float | None
    median_change_pct: float | None
    share_up: float | None
    share_up_ci: tuple[float, float] | None


@dataclass
class GroupStats:
    group: str
    n: int
    horizons: dict[int, HorizonStats] = field(default_factory=dict)
    labelled_tokens: int = 0
    rug_rate: float | None = None
    rug_rate_ci: tuple[float, float] | None = None
    small_sample: bool = False


def _horizon(values: list[float | None], total: int) -> HorizonStats:
    known = [v for v in values if v is not None]
    up = sum(1 for v in known if v > 0)
    return HorizonStats(
        n=len(known),
        coverage=None if total == 0 else round(len(known) / total, 4),
        median_change_pct=round(median(known), 2) if known else None,
        share_up=round(up / len(known), 4) if known else None,
        share_up_ci=wilson(up, len(known)),
    )


def _rug(labels: Iterable[Any]) -> tuple[int, float | None, tuple[float, float] | None]:
    known = [int(v) for v in labels if v is not None]
    if not known:
        return 0, None, None
    return len(known), round(sum(known) / len(known), 4), wilson(sum(known), len(known))


def evaluate(storage: Storage) -> list[GroupStats]:
    groups: list[GroupStats] = []

    labels = storage.label_rows()
    base = GroupStats("BASELINE (all tokens)", len(labels))
    for h in HORIZONS_MINUTES:
        base.horizons[h] = _horizon([row[f"ret_{h}"] for row in labels], len(labels))  # type: ignore[misc]
    base.labelled_tokens, base.rug_rate, base.rug_rate_ci = _rug(row["rugged"] for row in labels)
    groups.append(base)

    by_kind: dict[str, list[Mapping[str, Any]]] = {}
    for row in storage.outcome_rows():
        by_kind.setdefault(str(row["kind"]), []).append(row)
    for kind in sorted(by_kind):
        rows = by_kind[kind]
        tokens = {(r["chain_id"], r["token_address"]): r["rugged"] for r in rows}
        g = GroupStats(kind, len({r["obs_id"] for r in rows}))
        for h in HORIZONS_MINUTES:
            at_h = [r for r in rows if r["horizon_minutes"] == h]
            g.horizons[h] = _horizon([r["price_change_pct"] for r in at_h], g.n)
        g.labelled_tokens, g.rug_rate, g.rug_rate_ci = _rug(tokens.values())
        groups.append(g)

    for g in groups:
        g.small_sample = g.n < SMALL_SAMPLE
    return groups


def to_dicts(groups: list[GroupStats]) -> list[dict[str, Any]]:
    return [asdict(g) for g in groups]


def render(groups: list[GroupStats]) -> str:
    def pct(v: float | None) -> str:
        return "-" if v is None else f"{v * 100:.0f}%"

    def ci(v: tuple[float, float] | None) -> str:
        return "" if v is None else f" [{v[0] * 100:.0f}-{v[1] * 100:.0f}]"

    lines = ["Research evidence report (not financial advice). Intervals are Wilson 95%."]
    for g in groups:
        flag = "  (small sample: interpret with caution)" if g.small_sample else ""
        lines.append(f"\n{g.group}  n={g.n}{flag}")
        for h, s in g.horizons.items():
            med = "-" if s.median_change_pct is None else f"{s.median_change_pct:+.1f}%"
            lines.append(
                f"  +{h // 60}h: measured {s.n} (coverage {pct(s.coverage)}), median {med},"
                f" up {pct(s.share_up)}{ci(s.share_up_ci)}"
            )
        lines.append(
            f"  rugged within 24h: {pct(g.rug_rate)}{ci(g.rug_rate_ci)}"
            f" of {g.labelled_tokens} labelled tokens"
        )
    return "\n".join(lines)
