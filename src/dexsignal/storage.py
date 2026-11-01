"""SQLite storage (SPEC section 6, NFR-7)."""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TextIO

from .models import Observation, ObservationKind, PairSnapshot, ScreenResult, ScreenStatus

SCHEMA_VERSION = 1

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


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


def _norm(address: str) -> str:
    """EVM addresses are case-insensitive; Solana mints are not. Normalise only 0x-hex."""
    return address.lower() if address.startswith("0x") else address


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
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

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
    def cached_screen(
        self, chain_id: str, address: str, ttl_minutes: int, now: datetime
    ) -> ScreenResult | None:
        row = self._conn.execute(
            "SELECT status, reason, checked_at FROM screenings WHERE chain_id = ? AND token_address = ?",
            (chain_id, _norm(address)),
        ).fetchone()
        if row is None or row["status"] == ScreenStatus.UNSCREENED.value:
            return None
        if datetime.fromisoformat(row["checked_at"]) < now - timedelta(minutes=ttl_minutes):
            return None
        return ScreenResult(ScreenStatus(row["status"]), row["reason"])

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
        }

    def export_csv(self, table: str, out: TextIO) -> int:
        if table not in {"snapshots", "observations"}:
            raise ValueError("table must be 'snapshots' or 'observations'")
        cur = self._conn.execute(f"SELECT * FROM {table} ORDER BY id")  # noqa: S608 - allow-listed
        writer = csv.writer(out)
        writer.writerow([d[0] for d in cur.description])
        n = 0
        for row in cur:
            writer.writerow(list(row))
            n += 1
        return n
