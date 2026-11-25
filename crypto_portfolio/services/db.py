"""SQLite time-series storage for portfolio snapshots.

One row per snapshot in `snapshots` (the totals), one row per asset-and-source in
`snapshot_assets`, and (from schema v2) one row per data source and snapshot in
`snapshot_sources`.

The schema is versioned with `PRAGMA user_version` and upgraded by `migrate()` every time
a `Database` is opened: the pending steps of `MIGRATIONS` run in a single transaction (all or
nothing), and an existing database is copied to `<file>.bak_v<old>_<YYYYmmdd_HHMMSS>` first.
To change the schema, append a step to `MIGRATIONS`; never edit a step that has shipped.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from models import (
    BINANCE_EARN,
    BINANCE_FUNDING,
    BINANCE_SPOT,
    ONCHAIN,
    STATUS_INFERRED_ERROR,
    Portfolio,
    SourceStatus,
)

log = logging.getLogger(__name__)

# v1: the original schema. Every statement is IF NOT EXISTS, so on a pre-versioning database
# (user_version = 0, tables already there) this step is a no-op and only stamps the version.
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

# v2 (P0.2 + P0.3): per-source status of each snapshot, how the snapshot was triggered, and
# the location/price id of each asset row. Columns get defaults so existing rows stay valid.
SCHEMA_V2 = """
ALTER TABLE snapshot_assets ADD COLUMN detail   TEXT NOT NULL DEFAULT '';
ALTER TABLE snapshot_assets ADD COLUMN price_id TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS snapshot_sources (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    source      TEXT    NOT NULL,          -- binance_spot | binance_earn | binance_funding | onchain:<chain>/<label>
    status      TEXT    NOT NULL,          -- ok | error | disabled
    error       TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (snapshot_id, source)
);
ALTER TABLE snapshots ADD COLUMN trigger TEXT NOT NULL DEFAULT 'unknown';  -- cron | refresh | unknown
"""

# Step i (0-based) upgrades the database from version i to version i + 1. Append only.
MIGRATIONS: list[str] = [
    SCHEMA,     # v1
    SCHEMA_V2,  # v2
]
SCHEMA_VERSION = len(MIGRATIONS)
SOURCES_SCHEMA_VERSION = 2  # first version with snapshot_sources / snapshots.trigger

# How a snapshot was taken (snapshots.trigger).
TRIGGER_CRON = "cron"        # `main.py record`
TRIGGER_REFRESH = "refresh"  # POST /api/portfolio/refresh
TRIGGER_UNKNOWN = "unknown"  # rows older than v2, or a caller that did not say
TRIGGERS = (TRIGGER_CRON, TRIGGER_REFRESH, TRIGGER_UNKNOWN)

# Backfill heuristic for snapshots that predate snapshot_sources: a source total that drops by
# more than HOLE_DROP of its previous value and gets back more than HOLE_RECOVERY of that drop
# in the very next snapshot was almost certainly a failed fetch recorded as 0, not a real move.
HOLE_DROP = 0.9
HOLE_RECOVERY = 0.9


class MigrationError(RuntimeError):
    """The schema could not be brought up to date; the database was left as it was."""

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
    trigger: str = TRIGGER_UNKNOWN

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
        before = schema_version(self.path)
        after = migrate(self.path)
        if before < SOURCES_SCHEMA_VERSION <= after:
            # the snapshots taken before v2 have no status; mark their obvious holes once
            inferred = self.backfill_inferred_errors()
            if inferred:
                log.info("%s: marked %d inferred source error(s) on old snapshots", self.path, inferred)

    def schema_version(self) -> int:
        return schema_version(self.path)

    # -- writes ----------------------------------------------------------------
    def record(self, portfolio: Portfolio, trigger: str = TRIGGER_UNKNOWN) -> int:
        """Persist one snapshot (totals, per-asset rows, per-source status). Returns its id.

        Every holding is stored, dust included: the dust filter is a display concern, and
        throwing rows away here would make the history lie about the real total. The status
        of each source is stored too, so a failed source recorded as 0 is not read later as
        a real drop. `trigger` says what took the snapshot (`cron`, `refresh`).
        """
        if trigger not in TRIGGERS:
            raise ValueError(f"unknown snapshot trigger {trigger!r}; expected one of {TRIGGERS}")
        totals = {source: float(portfolio.totals.get(source, 0.0)) for source in SOURCE_COLUMNS}
        statuses = portfolio.source_statuses()
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO snapshots
                       (timestamp, total_portfolio_usd, binance_spot_usd, binance_earn_usd,
                        binance_funding_usd, onchain_usd, trigger)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    portfolio.timestamp or utc_now_iso(),
                    float(portfolio.total_usd),
                    totals[BINANCE_SPOT],
                    totals[BINANCE_EARN],
                    totals[BINANCE_FUNDING],
                    totals[ONCHAIN],
                    trigger,
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            connection.executemany(
                """INSERT INTO snapshot_assets
                       (snapshot_id, symbol, source, quantity, price_usd, value_usd, detail, price_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (snapshot_id, h.symbol, h.source, float(h.quantity), float(h.price_usd),
                     float(h.value_usd), h.detail or "", h.price_id or "")
                    for h in portfolio.holdings
                ],
            )
            connection.executemany(
                "INSERT INTO snapshot_sources (snapshot_id, source, status, error) VALUES (?, ?, ?, ?)",
                [
                    (snapshot_id, source, status.status, status.error)
                    for source, status in sorted(statuses.items())
                ],
            )
        return snapshot_id

    def backfill_inferred_errors(self) -> int:
        """Mark the holes of snapshots that have no `snapshot_sources` row (taken before v2).

        For each of the four source totals, a snapshot whose value fell by more than
        `HOLE_DROP` from the previous snapshot and recovered more than `HOLE_RECOVERY` of the
        fall in the next one gets `status = 'inferred_error'` for that source. Only single-
        snapshot holes are detected. Idempotent; returns the number of rows inserted.
        """
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT s.*, EXISTS (SELECT 1 FROM snapshot_sources x WHERE x.snapshot_id = s.id)
                              AS has_status
                   FROM snapshots s ORDER BY s.timestamp, s.id"""
            ).fetchall()
            marks = []
            for previous, current, following in zip(rows, rows[1:], rows[2:]):
                if current["has_status"]:
                    continue
                for source, column in SOURCE_COLUMNS.items():
                    before, value, after = (float(r[column] or 0) for r in (previous, current, following))
                    if is_hole(before, value, after):
                        marks.append((current["id"], source, STATUS_INFERRED_ERROR,
                                      f"inferred: {column} {before:.2f} -> {value:.2f} -> {after:.2f}"))
            cursor = connection.executemany(
                "INSERT OR IGNORE INTO snapshot_sources (snapshot_id, source, status, error) "
                "VALUES (?, ?, ?, ?)",
                marks,
            )
            return max(cursor.rowcount, 0)

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
                "SELECT symbol, source, quantity, price_usd, value_usd, detail, price_id "
                "FROM snapshot_assets WHERE snapshot_id = ? ORDER BY value_usd DESC",
                (snapshot_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def sources(self, snapshot_id: int) -> dict[str, SourceStatus]:
        """`snapshot_sources` of one snapshot: source key -> status (empty for old snapshots)."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT source, status, error FROM snapshot_sources WHERE snapshot_id = ? ORDER BY source",
                (snapshot_id,),
            ).fetchall()
        return {row["source"]: SourceStatus(row["status"], row["error"]) for row in rows}

    def latest_assets(self) -> list[dict]:
        snapshot = self.latest()
        return self.assets(snapshot.id) if snapshot else []


def is_hole(before: float, value: float, after: float) -> bool:
    """`value` fell by more than HOLE_DROP of `before` and `after` won back > HOLE_RECOVERY of it."""
    drop = before - value
    if before <= 0 or drop <= HOLE_DROP * before:
        return False
    return after - value > HOLE_RECOVERY * drop


# -- migrations ------------------------------------------------------------------
def schema_version(path: str | Path) -> int:
    """`PRAGMA user_version` of the database at `path` (0 for a missing or pre-versioning file)."""
    connection = sqlite3.connect(path, timeout=30)
    try:
        return _user_version(connection)
    finally:
        connection.close()


def migrate(path: str | Path, migrations: Sequence[str] | None = None) -> int:
    """Bring the database at `path` up to `len(migrations)` and return the resulting version.

    The pending steps and the new `user_version` are committed in one transaction, so a step
    that fails rolls every step of this run back and leaves `user_version` untouched. A database
    that already holds tables is backed up before anything changes. A database newer than the
    code is refused rather than written to with a schema we do not know.
    """
    steps = MIGRATIONS if migrations is None else list(migrations)
    target = len(steps)
    path = Path(path)
    # isolation_level=None: we issue BEGIN/COMMIT ourselves. The sqlite3 module's implicit
    # transactions (and executescript) would commit between DDL statements.
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        current = _user_version(connection)
        _check_not_newer(path, current, target)
        if current == target:
            return current
        if _has_tables(connection):
            backup_database(connection, path, current)

        connection.execute("BEGIN IMMEDIATE")  # take the write lock before re-reading the version
        try:
            # Another process may have migrated between our first read and the lock.
            current = _user_version(connection)
            _check_not_newer(path, current, target)
            for version in range(current + 1, target + 1):
                try:
                    for statement in split_statements(steps[version - 1]):
                        connection.execute(statement)
                except sqlite3.Error as exc:
                    raise MigrationError(
                        f"{path}: migration to schema v{version} failed ({exc}); "
                        f"rolled back, database left at v{current}"
                    ) from exc
            connection.execute(f"PRAGMA user_version = {int(target)}")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        if current != target:
            log.info("%s: schema migrated v%d -> v%d", path, current, target)
        return target
    finally:
        connection.close()


def backup_database(connection: sqlite3.Connection, path: Path, version: int) -> Path:
    """Copy the database to `<file>.bak_v<version>_<YYYYmmdd_HHMMSS>` (UTC) next to it.

    Uses SQLite's online backup API, so the copy is consistent even if another process is
    writing, and includes anything still in a WAL file.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    target = path.with_name(f"{path.name}.bak_v{version}_{stamp}")
    suffix = 1
    while target.exists():  # two migrations within the same second must not overwrite
        target = path.with_name(f"{path.name}.bak_v{version}_{stamp}_{suffix}")
        suffix += 1
    destination = sqlite3.connect(target)
    try:
        connection.backup(destination)
    finally:
        destination.close()
    log.info("%s: backed up schema v%d to %s", path, version, target)
    return target


def split_statements(script: str) -> list[str]:
    """Split a SQL script into single statements, respecting quotes and comments.

    `Connection.execute` runs one statement, and `executescript` cannot be used because it
    commits first, which would break the all-or-nothing transaction of `migrate`.
    """
    statements: list[str] = []
    buffer = ""
    for char in script:
        buffer += char
        if char == ";" and sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if _strip_sql_comments(buffer).strip():
        raise ValueError(f"incomplete SQL statement (missing ';'?): {buffer.strip()[:80]!r}")
    return statements


def _strip_sql_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*|/\*.*?\*/", "", sql, flags=re.DOTALL)


def _user_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _has_tables(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    return row is not None


def _check_not_newer(path: Path, current: int, target: int) -> None:
    if current > target:
        raise MigrationError(
            f"{path}: schema v{current} is newer than this code (v{target}); refusing to open it"
        )


def _as_snapshot(row: sqlite3.Row) -> Snapshot:
    return Snapshot(
        id=int(row["id"]),
        timestamp=row["timestamp"],
        total_portfolio_usd=float(row["total_portfolio_usd"]),
        binance_spot_usd=float(row["binance_spot_usd"]),
        binance_earn_usd=float(row["binance_earn_usd"]),
        binance_funding_usd=float(row["binance_funding_usd"]),
        onchain_usd=float(row["onchain_usd"]),
        trigger=row["trigger"] if "trigger" in row.keys() else TRIGGER_UNKNOWN,
    )
