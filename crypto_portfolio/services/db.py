"""SQLite time-series storage for portfolio snapshots.

Two tables: one row per snapshot in `snapshots` (the totals), one row per asset-and-source
in `snapshot_assets`. The schema is created on first use, so there is nothing to migrate
and `portfolio_history.db` can be deleted at any time to start over.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from models import BINANCE_EARN, BINANCE_FUNDING, BINANCE_SPOT, ONCHAIN, Portfolio

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp            TEXT    NOT NULL,
    total_portfolio_usd  REAL    NOT NULL DEFAULT 0,
    binance_spot_usd     REAL    NOT NULL DEFAULT 0,
    binance_earn_usd     REAL    NOT NULL DEFAULT 0,
    binance_funding_usd  REAL    NOT NULL DEFAULT 0,
    onchain_usd          REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS snapshot_assets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id  INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    symbol       TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    quantity     REAL    NOT NULL DEFAULT 0,
    price_usd    REAL    NOT NULL DEFAULT 0,
    value_usd    REAL    NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_snapshots_ts        ON snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_assets_snapshot     ON snapshot_assets(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_assets_symbol       ON snapshot_assets(symbol);
"""

SOURCE_COLUMNS = {
    BINANCE_SPOT: "binance_spot_usd",
    BINANCE_EARN: "binance_earn_usd",
    BINANCE_FUNDING: "binance_funding_usd",
    ONCHAIN: "onchain_usd",
}


@dataclass
class Snapshot:
    id: int
    timestamp: str
    total_portfolio_usd: float
    binance_spot_usd: float
    binance_earn_usd: float
    binance_funding_usd: float
    onchain_usd: float

    @property
    def dt(self) -> datetime:
        return parse_timestamp(self.timestamp)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_timestamp(value: str) -> datetime:
    """Parse our own ISO8601 timestamps, tolerating 'Z' and naive strings from older rows."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Database:
    def __init__(self, path: str | Path = "portfolio_history.db"):
        self.path = Path(path)
        if self.path.parent and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    # -- writes ----------------------------------------------------------------
    def record(self, portfolio: Portfolio) -> int:
        """Persist one snapshot (totals + per-asset breakdown). Returns the snapshot id.

        Every holding is stored, dust included: the dust filter is a display concern, and
        throwing rows away here would make the history lie about the real total.
        """
        totals = {source: float(portfolio.totals.get(source, 0.0)) for source in SOURCE_COLUMNS}
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO snapshots
                       (timestamp, total_portfolio_usd, binance_spot_usd, binance_earn_usd,
                        binance_funding_usd, onchain_usd)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    portfolio.timestamp or utc_now_iso(),
                    float(portfolio.total_usd),
                    totals[BINANCE_SPOT],
                    totals[BINANCE_EARN],
                    totals[BINANCE_FUNDING],
                    totals[ONCHAIN],
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            connection.executemany(
                """INSERT INTO snapshot_assets (snapshot_id, symbol, source, quantity, price_usd, value_usd)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (snapshot_id, h.symbol, h.source, float(h.quantity), float(h.price_usd), float(h.value_usd))
                    for h in portfolio.holdings
                ],
            )
        return snapshot_id

    # -- reads -----------------------------------------------------------------
    def history(self, limit: int | None = None, days: int | None = None) -> list[Snapshot]:
        """Snapshots oldest-first, optionally the last `days` and/or the most recent `limit`."""
        where, params = "", []
        if days:
            cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
            where = "WHERE timestamp >= ?"
            params.append(datetime.fromtimestamp(cutoff, timezone.utc).isoformat())
        query = f"SELECT * FROM snapshots {where} ORDER BY timestamp DESC, id DESC"
        if limit:
            query += " LIMIT ?"
            params.append(int(limit))
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return list(reversed([_as_snapshot(row) for row in rows]))

    def latest(self) -> Snapshot | None:
        rows = self.history(limit=1)
        return rows[0] if rows else None

    def snapshot_before(self, timestamp: str) -> Snapshot | None:
        """Newest snapshot taken at or before `timestamp` (an ISO8601 UTC string)."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE timestamp <= ? ORDER BY timestamp DESC, id DESC LIMIT 1",
                (timestamp,),
            ).fetchone()
        return _as_snapshot(row) if row else None

    def snapshot_after(self, timestamp: str) -> Snapshot | None:
        """Oldest snapshot taken at or after `timestamp` (an ISO8601 UTC string)."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE timestamp >= ? ORDER BY timestamp ASC, id ASC LIMIT 1",
                (timestamp,),
            ).fetchone()
        return _as_snapshot(row) if row else None

    def count(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])

    def assets(self, snapshot_id: int) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT symbol, source, quantity, price_usd, value_usd "
                "FROM snapshot_assets WHERE snapshot_id = ? ORDER BY value_usd DESC",
                (snapshot_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_assets(self) -> list[dict]:
        snapshot = self.latest()
        return self.assets(snapshot.id) if snapshot else []


def _as_snapshot(row: sqlite3.Row) -> Snapshot:
    return Snapshot(
        id=int(row["id"]),
        timestamp=row["timestamp"],
        total_portfolio_usd=float(row["total_portfolio_usd"]),
        binance_spot_usd=float(row["binance_spot_usd"]),
        binance_earn_usd=float(row["binance_earn_usd"]),
        binance_funding_usd=float(row["binance_funding_usd"]),
        onchain_usd=float(row["onchain_usd"]),
    )
