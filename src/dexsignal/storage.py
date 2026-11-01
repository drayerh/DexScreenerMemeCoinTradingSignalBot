"""SQLite storage (SPEC section 6, NFR-7)."""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TextIO

from .models import (
    Observation,
    ObservationKind,
    PairSnapshot,
    ScreenResult,
    ScreenStatus,
    TokenRef,
    norm_address,
)
from .risk import Creator, RiskAssessment

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    pair_address TEXT NOT NULL,
    dex_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price_usd REAL NOT NULL,
    liquidity_usd REAL NOT NULL,
    volume_h24 REAL NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snapshots_token_time
    ON snapshots (chain_id, token_address, observed_at);
CREATE TABLE IF NOT EXISTS screenings (
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (chain_id, token_address)
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,
    change_pct REAL NOT NULL,
    window_minutes INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_observations_token_kind_time
    ON observations (chain_id, token_address, kind, observed_at);
CREATE TABLE IF NOT EXISTS blacklist (
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    reason TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (chain_id, token_address)
);
"""

# v2: risk intelligence, deployers, outcome labels (REVIEW findings 1, 4, 5, 16, 17).
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS tokens (
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    symbol TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'live',
    PRIMARY KEY (chain_id, token_address)
);
CREATE INDEX IF NOT EXISTS ix_tokens_first_seen ON tokens (first_seen);
CREATE INDEX IF NOT EXISTS ix_observations_time ON observations (observed_at);
CREATE TABLE IF NOT EXISTS risk_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    reasons TEXT NOT NULL,
    features TEXT NOT NULL,
    creators TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_risk_checks_token
    ON risk_checks (chain_id, token_address, provider, checked_at);
CREATE TABLE IF NOT EXISTS deployers (
    chain_id TEXT NOT NULL,
    address TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    malicious INTEGER NOT NULL DEFAULT 0,
    manual_block INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (chain_id, address)
);
CREATE TABLE IF NOT EXISTS token_deployers (
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    deployer TEXT NOT NULL,
    source TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (chain_id, token_address, deployer)
);
CREATE INDEX IF NOT EXISTS ix_token_deployers_deployer ON token_deployers (chain_id, deployer);
CREATE TABLE IF NOT EXISTS observation_outcomes (
    observation_id INTEGER NOT NULL,
    horizon_minutes INTEGER NOT NULL,
    price_change_pct REAL,
    liquidity_change_pct REAL,
    measured_at TEXT,
    rugged_after INTEGER,
    PRIMARY KEY (observation_id, horizon_minutes)
);
CREATE TABLE IF NOT EXISTS token_labels (
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    peak_liquidity_usd REAL,
    trough_after_peak_usd REAL,
    rugged INTEGER,
    ret_60 REAL,
    ret_360 REAL,
    ret_1440 REAL,
    labelled_at TEXT NOT NULL,
    PRIMARY KEY (chain_id, token_address)
);
"""

# v3: watchlist of discovered tokens not yet tradeable (FR-19), and discovery time on tokens.
_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS watchlist (
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_checked TEXT NOT NULL,
    last_reason TEXT NOT NULL,
    checks INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (chain_id, token_address)
);
CREATE INDEX IF NOT EXISTS ix_watchlist_first_seen ON watchlist (first_seen);
"""

HORIZONS_MINUTES = (60, 360, 1440)
EXPORTABLE = frozenset(
    {
        "snapshots",
        "observations",
        "risk_checks",
        "observation_outcomes",
        "token_labels",
        "deployers",
        "watchlist",
        "tokens",
    }
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


_norm = norm_address


@dataclass(frozen=True)
class Point:
    observed_at: datetime
    price_usd: float
    liquidity_usd: float
    symbol: str


class Storage:
    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Additive migrations; safe to run on every start (REVIEW finding 16)."""
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        version = int(row["value"]) if row else 0
        if version < 2:
            self._conn.executescript(_SCHEMA_V2)
            # Backfill tokens recorded before v2. They are marked 'v1' and excluded from
            # labelling and evaluation: v1 only stored tokens while they passed the
            # filters, so their history has survivorship bias (review round 2, finding 9).
            self._conn.execute(
                "INSERT OR IGNORE INTO tokens (chain_id, token_address, symbol, first_seen, source)"
                " SELECT chain_id, token_address, MIN(symbol), MIN(observed_at), 'v1' FROM snapshots"
                " GROUP BY chain_id, token_address"
            )
        if version < 3:
            self._conn.executescript(_SCHEMA_V3)
            self._conn.execute("ALTER TABLE tokens ADD COLUMN discovered_at TEXT")
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def schema_version(self) -> int:
        return int(self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0])

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # Blacklist (FR-4)
    def blacklist_add(self, chain_id: str, address: str, reason: str = "") -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO blacklist VALUES (?, ?, ?, ?)",
            (chain_id, _norm(address), reason, _iso(utc_now())),
        )

    def blacklist_remove(self, chain_id: str, address: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM blacklist WHERE chain_id = ? AND token_address = ?",
            (chain_id, _norm(address)),
        )
        return cur.rowcount > 0

    def blacklist_list(self) -> list[tuple[str, str, str]]:
        rows = self._conn.execute(
            "SELECT chain_id, token_address, reason FROM blacklist ORDER BY added_at"
        ).fetchall()
        return [(r["chain_id"], r["token_address"], r["reason"]) for r in rows]

    def is_blacklisted(self, chain_id: str, address: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM blacklist WHERE chain_id = ? AND token_address = ?",
            (chain_id, _norm(address)),
        ).fetchone()
        return row is not None

    # Screening cache (FR-6)
    def save_screen(self, chain_id: str, address: str, result: ScreenResult, now: datetime) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO screenings VALUES (?, ?, ?, ?, ?)",
            (chain_id, _norm(address), result.status.value, result.reason, _iso(now)),
        )

    # Snapshots (FR-7)
    def add_snapshot(self, snap: PairSnapshot, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO snapshots (chain_id, token_address, pair_address, dex_id, symbol,"
            " price_usd, liquidity_usd, volume_h24, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snap.chain_id,
                _norm(snap.token_address),
                snap.pair_address,
                snap.dex_id,
                snap.symbol,
                snap.price_usd,
                snap.liquidity_usd,
                snap.volume_h24,
                _iso(now),
            ),
        )

    def history(self, chain_id: str, address: str, since: datetime) -> list[Point]:
        """Snapshots in the window, oldest first (FR-8 ordering)."""
        rows = self._conn.execute(
            "SELECT observed_at, price_usd, liquidity_usd, symbol FROM snapshots"
            " WHERE chain_id = ? AND token_address = ? AND observed_at >= ?"
            " ORDER BY observed_at ASC, id ASC",
            (chain_id, _norm(address), _iso(since)),
        ).fetchall()
        return [
            Point(datetime.fromisoformat(r["observed_at"]), r["price_usd"], r["liquidity_usd"], r["symbol"])
            for r in rows
        ]

    def prune(self, older_than: datetime) -> int:
        cur = self._conn.execute("DELETE FROM snapshots WHERE observed_at < ?", (_iso(older_than),))
        return cur.rowcount

    # Observations (FR-9)
    def recently_observed(self, obs: Observation, cooldown_minutes: int) -> bool:
        since = obs.observed_at - timedelta(minutes=cooldown_minutes)
        row = self._conn.execute(
            "SELECT 1 FROM observations WHERE chain_id = ? AND token_address = ? AND kind = ?"
            " AND observed_at > ? LIMIT 1",
            (obs.chain_id, _norm(obs.token_address), obs.kind.value, _iso(since)),
        ).fetchone()
        return row is not None

    def add_observation(self, obs: Observation) -> int:
        cur = self._conn.execute(
            "INSERT INTO observations (chain_id, token_address, symbol, kind, change_pct,"
            " window_minutes, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                obs.chain_id,
                _norm(obs.token_address),
                obs.symbol,
                obs.kind.value,
                obs.change_pct,
                obs.window_minutes,
                _iso(obs.observed_at),
            ),
        )
        return int(cur.lastrowid or 0)

    def mark_notified(self, observation_id: int) -> None:
        self._conn.execute("UPDATE observations SET notified = 1 WHERE id = ?", (observation_id,))

    def observations(self) -> list[Observation]:
        rows = self._conn.execute("SELECT * FROM observations ORDER BY observed_at").fetchall()
        return [
            Observation(
                r["chain_id"],
                r["token_address"],
                r["symbol"],
                ObservationKind(r["kind"]),
                r["change_pct"],
                r["window_minutes"],
                datetime.fromisoformat(r["observed_at"]),
            )
            for r in rows
        ]

    # Reporting (FR-11)
    def stats(self) -> dict[str, int]:
        def count(sql: str) -> int:
            return int(self._conn.execute(sql).fetchone()[0])

        return {
            "snapshots": count("SELECT COUNT(*) FROM snapshots"),
            "tokens": count("SELECT COUNT(DISTINCT chain_id || ':' || token_address) FROM snapshots"),
            "observations": count("SELECT COUNT(*) FROM observations"),
            "notified": count("SELECT COUNT(*) FROM observations WHERE notified = 1"),
            "blacklisted": count("SELECT COUNT(*) FROM blacklist"),
            "rejected": count("SELECT COUNT(*) FROM screenings WHERE status = 'rejected'"),
            "risk_checks": count("SELECT COUNT(*) FROM risk_checks"),
            "deployers": count("SELECT COUNT(*) FROM deployers"),
            "labelled_tokens": count("SELECT COUNT(*) FROM token_labels"),
            "rugged_tokens": count("SELECT COUNT(*) FROM token_labels WHERE rugged = 1"),
            "outcomes": count("SELECT COUNT(*) FROM observation_outcomes"),
            "watching": count("SELECT COUNT(*) FROM watchlist"),
            "graduated_tokens": count(
                "SELECT COUNT(*) FROM tokens WHERE discovered_at IS NOT NULL AND discovered_at < first_seen"
            ),
        }

    def export_csv(self, table: str, out: TextIO) -> int:
        if table not in EXPORTABLE:
            raise ValueError(f"table must be one of {sorted(EXPORTABLE)}")
        cur = self._conn.execute(f"SELECT * FROM {table} ORDER BY rowid")  # noqa: S608 - allow-listed
        writer = csv.writer(out)
        writer.writerow([d[0] for d in cur.description])
        n = 0
        for row in cur:
            writer.writerow(list(row))
            n += 1
        return n

    # --- v2: heartbeat (REVIEW finding 17) ------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def record_cycle(self, now: datetime, ok: bool) -> None:
        self.set_meta("last_cycle_at", _iso(now))
        if ok:
            self.set_meta("last_ok_cycle_at", _iso(now))

    # --- v2: tracked tokens (REVIEW finding 1) --------------------------------
    def register_token(
        self, snap: PairSnapshot, now: datetime, discovered_at: datetime | None = None
    ) -> bool:
        """Record first accepted sighting, and when discovery first saw the token (it may have
        waited on the watchlist). Returns True if the token is new."""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO tokens (chain_id, token_address, symbol, first_seen, discovered_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                snap.chain_id,
                _norm(snap.token_address),
                snap.symbol,
                _iso(now),
                _iso(min(discovered_at or now, now)),
            ),
        )
        return cur.rowcount > 0

    # --- v3: watchlist (FR-19) -------------------------------------------------
    def watch(self, ref: TokenRef, reason: str, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO watchlist (chain_id, token_address, first_seen, last_checked, last_reason)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT (chain_id, token_address) DO UPDATE SET"
            " last_checked = excluded.last_checked, last_reason = excluded.last_reason,"
            " checks = checks + 1",
            (ref.chain_id, _norm(ref.token_address), _iso(now), _iso(now), reason),
        )

    def unwatch(self, ref: TokenRef) -> None:
        self._conn.execute(
            "DELETE FROM watchlist WHERE chain_id = ? AND token_address = ?",
            (ref.chain_id, _norm(ref.token_address)),
        )

    def watched(self, since: datetime, limit: int) -> dict[TokenRef, datetime]:
        """Newest watched tokens first seen at or after `since`, with their discovery time."""
        rows = self._conn.execute(
            "SELECT chain_id, token_address, first_seen FROM watchlist WHERE first_seen >= ?"
            " ORDER BY first_seen DESC LIMIT ?",
            (_iso(since), limit),
        ).fetchall()
        return {
            TokenRef(r["chain_id"], r["token_address"]): datetime.fromisoformat(r["first_seen"]) for r in rows
        }

    def prune_watchlist(self, older_than: datetime) -> int:
        cur = self._conn.execute("DELETE FROM watchlist WHERE first_seen < ?", (_iso(older_than),))
        return cur.rowcount

    def is_known(self, chain_id: str, address: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM tokens WHERE chain_id = ? AND token_address = ?", (chain_id, _norm(address))
        ).fetchone()
        return row is not None

    def tracked_tokens(self, first_seen_after: datetime, observed_after: datetime) -> list[TokenRef]:
        """Live tokens still inside their tracking window, plus any token with a recent
        observation whose forward outcomes still need data (review round 2, finding 1)."""
        rows = self._conn.execute(
            "SELECT chain_id, token_address FROM tokens WHERE source = 'live' AND first_seen >= ?"
            " UNION SELECT chain_id, token_address FROM observations WHERE observed_at >= ?",
            (_iso(first_seen_after), _iso(observed_after)),
        ).fetchall()
        return [TokenRef(r["chain_id"], r["token_address"]) for r in rows]

    def symbol_of(self, chain_id: str, address: str) -> str:
        row = self._conn.execute(
            "SELECT symbol FROM tokens WHERE chain_id = ? AND token_address = ?", (chain_id, _norm(address))
        ).fetchone()
        return "?" if row is None else str(row["symbol"])

    def latest_screen(self, chain_id: str, address: str) -> ScreenStatus | None:
        row = self._conn.execute(
            "SELECT status FROM screenings WHERE chain_id = ? AND token_address = ?",
            (chain_id, _norm(address)),
        ).fetchone()
        return None if row is None else ScreenStatus(row["status"])

    def deployers_of(self, chain_id: str, address: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT deployer FROM token_deployers WHERE chain_id = ? AND token_address = ?",
            (chain_id, _norm(address)),
        ).fetchall()
        return [str(r["deployer"]) for r in rows]

    # --- v2: point-in-time risk checks (REVIEW finding 4) ----------------------
    def add_risk(self, chain_id: str, address: str, a: RiskAssessment, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO risk_checks (chain_id, token_address, provider, status, reasons, features,"
            " creators, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chain_id,
                _norm(address),
                a.provider,
                a.status.value,
                json.dumps(list(a.reasons)),
                json.dumps(dict(a.features), sort_keys=True),
                json.dumps([[c.address, c.malicious] for c in a.creators]),
                _iso(now),
            ),
        )

    def latest_risk(
        self,
        chain_id: str,
        address: str,
        provider: str,
        not_before: datetime,
        unscreened_not_before: datetime,
    ) -> RiskAssessment | None:
        """Latest cached assessment. UNSCREENED results are reused only for a shorter
        window, so outages are retried soon without hammering providers (finding 10)."""
        row = self._conn.execute(
            "SELECT * FROM risk_checks WHERE chain_id = ? AND token_address = ? AND provider = ?"
            " AND checked_at >= ? ORDER BY checked_at DESC, id DESC LIMIT 1",
            (chain_id, _norm(address), provider, _iso(min(not_before, unscreened_not_before))),
        ).fetchone()
        if row is None:
            return None
        checked = datetime.fromisoformat(row["checked_at"])
        if row["status"] == ScreenStatus.UNSCREENED.value and checked < unscreened_not_before:
            return None
        if row["status"] != ScreenStatus.UNSCREENED.value and checked < not_before:
            return None
        return RiskAssessment(
            provider=row["provider"],
            status=ScreenStatus(row["status"]),
            reasons=tuple(json.loads(row["reasons"])),
            features=json.loads(row["features"]),
            creators=tuple(Creator(a, m) for a, m in json.loads(row["creators"])),
        )

    # --- v2: deployers (REVIEW findings 5, 6) ----------------------------------
    def link_deployer(
        self, chain_id: str, token_address: str, creator: Creator, source: str, now: datetime
    ) -> None:
        address = _norm(creator.address)
        self._conn.execute(
            "INSERT OR IGNORE INTO deployers (chain_id, address, first_seen) VALUES (?, ?, ?)",
            (chain_id, address, _iso(now)),
        )
        if creator.malicious:
            self._conn.execute(
                "UPDATE deployers SET malicious = 1 WHERE chain_id = ? AND address = ?", (chain_id, address)
            )
        self._conn.execute(
            "INSERT OR IGNORE INTO token_deployers VALUES (?, ?, ?, ?, ?)",
            (chain_id, _norm(token_address), address, source, _iso(now)),
        )

    def deployer_block(self, chain_id: str, address: str, reason: str, now: datetime) -> None:
        address = _norm(address)
        self._conn.execute(
            "INSERT OR IGNORE INTO deployers (chain_id, address, first_seen) VALUES (?, ?, ?)",
            (chain_id, address, _iso(now)),
        )
        self._conn.execute(
            "UPDATE deployers SET manual_block = 1, reason = ? WHERE chain_id = ? AND address = ?",
            (reason, chain_id, address),
        )

    def deployer_unblock(self, chain_id: str, address: str) -> bool:
        cur = self._conn.execute(
            "UPDATE deployers SET manual_block = 0, reason = '' WHERE chain_id = ? AND address = ?"
            " AND manual_block = 1",
            (chain_id, _norm(address)),
        )
        return cur.rowcount > 0

    def deployers(self) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT d.chain_id, d.address, d.malicious, d.manual_block, d.reason,"
            " COUNT(DISTINCT td.token_address) AS tokens,"
            " COALESCE(SUM(CASE WHEN tl.rugged = 1 THEN 1 ELSE 0 END), 0) AS rugs"
            " FROM deployers d"
            " LEFT JOIN token_deployers td ON td.chain_id = d.chain_id AND td.deployer = d.address"
            " LEFT JOIN token_labels tl ON tl.chain_id = td.chain_id AND tl.token_address = td.token_address"
            " GROUP BY d.chain_id, d.address ORDER BY rugs DESC, tokens DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def deployer_block_reason(self, chain_id: str, address: str, max_rugs: int) -> str:
        """Why this deployer is blocked, or '' if it is not."""
        address = _norm(address)
        row = self._conn.execute(
            "SELECT malicious, manual_block, reason FROM deployers WHERE chain_id = ? AND address = ?",
            (chain_id, address),
        ).fetchone()
        if row is not None and row["manual_block"]:
            return f"deployer manually blocked ({row['reason'] or 'no reason'})"
        if row is not None and row["malicious"]:
            return "deployer flagged malicious"
        rugs = self._conn.execute(
            "SELECT COUNT(*) FROM token_deployers td JOIN token_labels tl"
            " ON tl.chain_id = td.chain_id AND tl.token_address = td.token_address"
            " WHERE td.chain_id = ? AND td.deployer = ? AND tl.rugged = 1",
            (chain_id, address),
        ).fetchone()[0]
        if rugs >= max_rugs:
            return f"deployer linked to {rugs} labelled rugs"
        return ""

    # --- v2: outcome labelling (REVIEW findings 1, 2) --------------------------
    def points_between(self, chain_id: str, address: str, start: datetime, end: datetime) -> list[Point]:
        rows = self._conn.execute(
            "SELECT observed_at, price_usd, liquidity_usd, symbol FROM snapshots"
            " WHERE chain_id = ? AND token_address = ? AND observed_at >= ? AND observed_at <= ?"
            " ORDER BY observed_at ASC, id ASC",
            (chain_id, _norm(address), _iso(start), _iso(end)),
        ).fetchall()
        return [
            Point(datetime.fromisoformat(r["observed_at"]), r["price_usd"], r["liquidity_usd"], r["symbol"])
            for r in rows
        ]

    def pending_outcomes(self, now: datetime, oldest: datetime) -> list[tuple[int, str, str, datetime, int]]:
        """(observation id, chain, token, observed_at, horizon) whose horizon has passed and is
        unlabelled. Bounded by ``oldest`` so work does not grow with the database (finding 11)."""
        pending: list[tuple[int, str, str, datetime, int]] = []
        for horizon in HORIZONS_MINUTES:
            rows = self._conn.execute(
                "SELECT o.id, o.chain_id, o.token_address, o.observed_at FROM observations o"
                " WHERE o.observed_at <= ? AND o.observed_at >= ? AND NOT EXISTS (SELECT 1"
                " FROM observation_outcomes oc WHERE oc.observation_id = o.id AND oc.horizon_minutes = ?)",
                (_iso(now - timedelta(minutes=horizon)), _iso(oldest), horizon),
            ).fetchall()
            pending.extend(
                (
                    r["id"],
                    r["chain_id"],
                    r["token_address"],
                    datetime.fromisoformat(r["observed_at"]),
                    horizon,
                )
                for r in rows
            )
        return pending

    def add_outcome(
        self,
        observation_id: int,
        horizon: int,
        price_change: float | None,
        liquidity_change: float | None,
        measured_at: datetime | None,
        *,
        rugged_after: bool | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO observation_outcomes VALUES (?, ?, ?, ?, ?, ?)",
            (
                observation_id,
                horizon,
                price_change,
                liquidity_change,
                None if measured_at is None else _iso(measured_at),
                None if rugged_after is None else int(rugged_after),
            ),
        )

    def unlabelled_tokens(
        self, first_seen_before: datetime, oldest: datetime
    ) -> list[tuple[str, str, datetime]]:
        rows = self._conn.execute(
            "SELECT t.chain_id, t.token_address, t.first_seen FROM tokens t WHERE t.source = 'live'"
            " AND t.first_seen <= ? AND t.first_seen >= ? AND NOT EXISTS (SELECT 1 FROM token_labels l"
            " WHERE l.chain_id = t.chain_id AND l.token_address = t.token_address)",
            (_iso(first_seen_before), _iso(oldest)),
        ).fetchall()
        return [(r["chain_id"], r["token_address"], datetime.fromisoformat(r["first_seen"])) for r in rows]

    def add_token_label(
        self,
        chain_id: str,
        address: str,
        first_seen: datetime,
        *,
        peak: float | None,
        trough: float | None,
        rugged: bool | None,
        returns: dict[int, float | None],
        now: datetime,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO token_labels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chain_id,
                _norm(address),
                _iso(first_seen),
                peak,
                trough,
                None if rugged is None else int(rugged),
                returns.get(60),
                returns.get(360),
                returns.get(1440),
                _iso(now),
            ),
        )

    def outcome_rows(self) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT o.id AS obs_id, o.kind, o.chain_id, o.token_address, oc.horizon_minutes,"
            " oc.price_change_pct, oc.liquidity_change_pct, oc.rugged_after FROM observations o"
            " LEFT JOIN observation_outcomes oc ON oc.observation_id = o.id"
        ).fetchall()
        return [dict(r) for r in rows]

    def label_rows(self) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT l.* FROM token_labels l JOIN tokens t ON t.chain_id = l.chain_id"
            " AND t.token_address = l.token_address WHERE t.source = 'live'"
        ).fetchall()
        return [dict(r) for r in rows]
