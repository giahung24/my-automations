"""SQLite time-series storage for portfolio snapshots.

One row per snapshot in `snapshots` (the totals), one row per asset-and-source in
`snapshot_assets`, and (from schema v2) one row per data source and snapshot in
`snapshot_sources`. From schema v3, `snapshots.is_backfilled = 1` marks the monthly checkpoints
reconstructed by `services/backfill.py` (P0.8): they feed the long-range charts, but they are
never "the latest snapshot", never the reference of a 24h change, and never part of the
hole heuristic - they were not fetched, so they say nothing about a source's health. From
schema v4, `cash_flows` holds deposits/withdrawals (crypto, fiat, P2P) and `sync_state` the
incremental sync cursors (`services/cash_flows.py`, Epic F2). From schema v5, `snapshots.manual_usd`
is the fifth per-source total: assets declared by hand in `manual_assets:` (Epic M6).

The schema is versioned with `PRAGMA user_version` and upgraded by `migrate()` every time
a `Database` is opened: the pending steps of `MIGRATIONS` run in a single transaction (all or
nothing), and an existing database is copied to `<file>.bak_v<old>_<YYYYmmdd_HHMMSS>` first.
To change the schema, append a step to `MIGRATIONS`; never edit a step that has shipped.

The file runs in WAL mode (`PRAGMA journal_mode=WAL`, set once by `Database.__init__` and stored
in the file itself), so the API gateway's `mode=ro` connections read the last committed snapshot
while the collector is writing, instead of failing with `database is locked`.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from models import (
    BINANCE_EARN,
    BINANCE_FUNDING,
    BINANCE_SPOT,
    MANUAL,
    ONCHAIN,
    STATUS_ERROR,
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

# v3 (P0.8): monthly checkpoints reconstructed from historical prices, not fetched.
SCHEMA_V3 = """
ALTER TABLE snapshots ADD COLUMN is_backfilled INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_snapshots_backfilled ON snapshots(is_backfilled, timestamp);
"""

# v4 (F2): deposits, withdrawals, fiat and P2P orders - every quantity change that is money
# entering or leaving the portfolio rather than profit - plus the incremental sync cursors.
# docs/SPEC.md §3.3; `channel` and `counterparty_label` are additions of task F2.flow.
SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS cash_flows (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp             TEXT    NOT NULL,     -- ISO8601 UTC: completion time of the flow
    window_start          TEXT,                 -- flows inferred from snapshot deltas only
    source                TEXT    NOT NULL,     -- binance | evm:<chain> | solana
    account               TEXT    NOT NULL,     -- 'binance' or a wallet address
    account_label         TEXT    NOT NULL DEFAULT '',
    asset                 TEXT    NOT NULL,     -- symbol, as in snapshot_assets.symbol
    network               TEXT    NOT NULL DEFAULT '',  -- Binance network name (ETH, BSC, SOL...)
    channel               TEXT    NOT NULL DEFAULT 'crypto',  -- crypto | fiat | fiat_payment | p2p | onchain
    direction             TEXT    NOT NULL CHECK (direction IN ('in', 'out')),
    amount                REAL    NOT NULL CHECK (amount >= 0),  -- always positive, sign is `direction`
    fee_amount            REAL    NOT NULL DEFAULT 0 CHECK (fee_amount >= 0),
    fee_asset             TEXT    NOT NULL DEFAULT '',
    price_usd             REAL    NOT NULL DEFAULT 0,   -- price of `asset` when the flow happened
    value_usd             REAL    NOT NULL DEFAULT 0,   -- amount * price_usd, always positive
    flow_type             TEXT    NOT NULL
                          CHECK (flow_type IN ('deposit', 'withdrawal', 'reward', 'airdrop', 'fee', 'adjustment')),
    status                TEXT    NOT NULL DEFAULT 'completed'
                          CHECK (status IN ('pending', 'completed', 'failed')),
    tx_hash               TEXT    NOT NULL DEFAULT '',
    counterparty_address  TEXT    NOT NULL DEFAULT '',  -- withdrawal target / deposit sender, if known
    counterparty_label    TEXT    NOT NULL DEFAULT '',  -- label of the owned wallet it matched
    is_internal_transfer  INTEGER NOT NULL DEFAULT 0 CHECK (is_internal_transfer IN (0, 1)),
    matched_flow_id       INTEGER REFERENCES cash_flows(id) ON DELETE SET NULL,
    match_method          TEXT    NOT NULL DEFAULT '',  -- address | tx_hash | heuristic | manual
    match_confidence      REAL    NOT NULL DEFAULT 0 CHECK (match_confidence BETWEEN 0 AND 1),
    detection_method      TEXT    NOT NULL,     -- binance_api | snapshot_delta | tx_verify | manual
    classified_by         TEXT    NOT NULL DEFAULT 'auto' CHECK (classified_by IN ('auto', 'user')),
    snapshot_id_before    INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
    snapshot_id_after     INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
    external_id           TEXT    NOT NULL,     -- Binance id / order number (prefixed per channel)
    raw_json              TEXT    NOT NULL DEFAULT '{}',
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL,
    note                  TEXT    NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cash_flows_ext    ON cash_flows(source, flow_type, external_id);
CREATE INDEX        IF NOT EXISTS idx_cash_flows_ts    ON cash_flows(timestamp);
CREATE INDEX        IF NOT EXISTS idx_cash_flows_tx    ON cash_flows(tx_hash) WHERE tx_hash <> '';
CREATE INDEX        IF NOT EXISTS idx_cash_flows_asset ON cash_flows(asset, timestamp);
CREATE TABLE IF NOT EXISTS sync_state (
    key        TEXT PRIMARY KEY,                -- 'binance.deposit', 'binance.withdraw', ...
    value      TEXT NOT NULL,                   -- epoch ms up to which the stream is synced
    updated_at TEXT NOT NULL
);
"""

# v5 (M6.2): the fifth per-source total, manual assets (cold wallet, external staking). Older
# snapshots had none, so 0 is their true value; the four other totals are untouched.
SCHEMA_V5 = """
ALTER TABLE snapshots ADD COLUMN manual_usd REAL NOT NULL DEFAULT 0;
"""

# v6 (N1.1 + N3.1): the APR Binance lists for a Simple Earn position (NULL = not known), and what
# each stablecoin held was worth against its references when the snapshot was taken.
SCHEMA_V6 = """
ALTER TABLE snapshot_assets ADD COLUMN listed_apr REAL;
CREATE TABLE IF NOT EXISTS peg_checks (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    symbol      TEXT    NOT NULL,
    reference   TEXT    NOT NULL,          -- binance:USDCUSDT | defillama:coingecko:usd-coin
    price       REAL,                      -- NULL when the reference failed
    error       TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (snapshot_id, symbol, reference)
);
"""

# Step i (0-based) upgrades the database from version i to version i + 1. Append only.
MIGRATIONS: list[str] = [
    SCHEMA,     # v1
    SCHEMA_V2,  # v2
    SCHEMA_V3,  # v3
    SCHEMA_V4,  # v4
    SCHEMA_V5,  # v5
    SCHEMA_V6,  # v6
]
SCHEMA_VERSION = len(MIGRATIONS)
SOURCES_SCHEMA_VERSION = 2  # first version with snapshot_sources / snapshots.trigger
BACKFILL_SCHEMA_VERSION = 3  # first version with snapshots.is_backfilled
CASH_FLOWS_SCHEMA_VERSION = 4  # first version with cash_flows / sync_state
MANUAL_SCHEMA_VERSION = 5  # first version with snapshots.manual_usd

# Readers (the `mode=ro` API gateway) never block on, or are blocked by, the collector's writes.
# Persistent: once set, every later connection to the file - from any process - uses it.
JOURNAL_MODE = "wal"

# How a snapshot was taken (snapshots.trigger).
TRIGGER_CRON = "cron"        # `main.py record`
TRIGGER_REFRESH = "refresh"  # POST /api/portfolio/refresh
TRIGGER_UNKNOWN = "unknown"  # rows older than v2, or a caller that did not say
TRIGGER_BACKFILL = "backfill"  # services/backfill.py (always with is_backfilled = 1)
TRIGGERS = (TRIGGER_CRON, TRIGGER_REFRESH, TRIGGER_UNKNOWN, TRIGGER_BACKFILL)

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
    MANUAL: "manual_usd",
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
    is_backfilled: bool = False
    manual_usd: float = 0.0

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


def _make_private(path: Path) -> None:
    """Keep the history (balances, wallet addresses) readable by its owner only: mode 0600.

    Created up front so SQLite never makes it with the process umask (usually 0644); an
    existing wider file is tightened. SQLite gives `-wal` / `-shm` the main file's mode. A
    filesystem without POSIX modes (or a file owned by someone else) is left as it is.
    """
    try:
        try:
            os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
            return
        except FileExistsError:
            pass
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            path.chmod(0o600)
            for sidecar in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
                if sidecar.exists():
                    sidecar.chmod(0o600)
            log.warning("%s was readable by group/others (mode %o); reset to 600", path, mode)
    except OSError as exc:
        log.debug("could not restrict %s: %s", path, exc)


class Database:
    def __init__(self, path: str | Path = "portfolio_history.db"):
        self.path = Path(path)
        if self.path.parent and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        _make_private(self.path)
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
        enable_wal(self.path)
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
        if trigger == TRIGGER_BACKFILL:
            raise ValueError("backfilled snapshots are written by services/backfill.py, not record()")
        statuses = portfolio.source_statuses()
        with self.connect() as connection:
            snapshot_id = _insert_snapshot(connection, portfolio, trigger)
            connection.executemany(
                "INSERT INTO snapshot_sources (snapshot_id, source, status, error) VALUES (?, ?, ?, ?)",
                [
                    (snapshot_id, source, status.status, status.error)
                    for source, status in sorted(statuses.items())
                ],
            )
            connection.executemany(
                "INSERT OR REPLACE INTO peg_checks (snapshot_id, symbol, reference, price, error) "
                "VALUES (?, ?, ?, ?, ?)",
                [(snapshot_id, c["symbol"], c["reference"], c.get("price"), c.get("error") or "")
                 for c in portfolio.peg_checks],
            )
        return snapshot_id

    def replace_backfill(self, portfolios: Sequence[Portfolio]) -> list[int]:
        """Atomically swap every backfilled snapshot for `portfolios`; returns the new ids.

        The old checkpoints (and, by cascade, their asset rows) are deleted and the new ones
        inserted in one transaction, so re-running the backfill never duplicates a month and a
        failure leaves the previous set intact. Backfilled snapshots get `is_backfilled = 1`,
        `trigger = 'backfill'` and no `snapshot_sources` rows: nothing was fetched.
        """
        with self.connect() as connection:
            connection.execute("DELETE FROM snapshots WHERE is_backfilled = 1")
            return [
                _insert_snapshot(connection, portfolio, TRIGGER_BACKFILL, is_backfilled=True)
                for portfolio in portfolios
            ]

    def backfill_inferred_errors(self) -> int:
        """Mark the holes of snapshots that have no `snapshot_sources` row (taken before v2).

        For each per-source total, a snapshot whose value fell by more than
        `HOLE_DROP` from the previous snapshot and recovered more than `HOLE_RECOVERY` of the
        fall in the next one gets `status = 'inferred_error'` for that source. Only single-
        snapshot holes are detected. Idempotent; returns the number of rows inserted.
        """
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT s.*, EXISTS (SELECT 1 FROM snapshot_sources x WHERE x.snapshot_id = s.id)
                              AS has_status
                   FROM snapshots s WHERE s.is_backfilled = 0 ORDER BY s.timestamp, s.id"""
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
    def history(self, limit: int | None = None, days: int | None = None,
                include_backfilled: bool = True) -> list[Snapshot]:
        """Snapshots oldest-first, optionally the last `days` and/or the most recent `limit`.

        Backfilled monthly checkpoints are included unless `include_backfilled` is false.
        """
        conditions, params = [], []
        if days:
            cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
            conditions.append("timestamp >= ?")
            params.append(datetime.fromtimestamp(cutoff, timezone.utc).isoformat())
        if not include_backfilled:
            conditions.append("is_backfilled = 0")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM snapshots {where} ORDER BY timestamp DESC, id DESC"
        if limit:
            query += " LIMIT ?"
            params.append(int(limit))
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return list(reversed([_as_snapshot(row) for row in rows]))

    def latest(self) -> Snapshot | None:
        """Newest snapshot that was really fetched (a backfilled checkpoint never is)."""
        rows = self.history(limit=1, include_backfilled=False)
        return rows[0] if rows else None

    def snapshot_before(self, timestamp: str) -> Snapshot | None:
        """Newest fetched snapshot taken at or before `timestamp` (an ISO8601 UTC string)."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE timestamp <= ? AND is_backfilled = 0 "
                "ORDER BY timestamp DESC, id DESC LIMIT 1",
                (timestamp,),
            ).fetchone()
        return _as_snapshot(row) if row else None

    def snapshot_after(self, timestamp: str) -> Snapshot | None:
        """Oldest fetched snapshot taken at or after `timestamp` (an ISO8601 UTC string)."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE timestamp >= ? AND is_backfilled = 0 "
                "ORDER BY timestamp ASC, id ASC LIMIT 1",
                (timestamp,),
            ).fetchone()
        return _as_snapshot(row) if row else None

    def count(self, backfilled: bool | None = None) -> int:
        """Number of snapshots; `backfilled=True/False` counts only that kind."""
        query, params = "SELECT COUNT(*) FROM snapshots", ()
        if backfilled is not None:
            query, params = f"{query} WHERE is_backfilled = ?", (int(backfilled),)
        with self.connect() as connection:
            return int(connection.execute(query, params).fetchone()[0])

    def assets(self, snapshot_id: int) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT symbol, source, quantity, price_usd, value_usd, detail, price_id, listed_apr "
                "FROM snapshot_assets WHERE snapshot_id = ? ORDER BY value_usd DESC",
                (snapshot_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def failed_snapshots_since(self, timestamp: str, hours: int = 24) -> int:
        """Snapshots taken in the `hours` up to `timestamp` in which at least one source failed."""
        start = (parse_timestamp(timestamp) - timedelta(hours=hours)).isoformat()
        with self.connect() as connection:
            return int(connection.execute(
                "SELECT COUNT(DISTINCT x.snapshot_id) FROM snapshot_sources x "
                "JOIN snapshots s ON s.id = x.snapshot_id "
                "WHERE x.status IN (?, ?) AND s.timestamp > ? AND s.timestamp <= ?",
                (STATUS_ERROR, STATUS_INFERRED_ERROR, start, timestamp),
            ).fetchone()[0])

    def peg_checks(self, snapshot_id: int) -> list[dict]:
        """`peg_checks` of one snapshot (empty before v6, or when no stablecoin was held)."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT symbol, reference, price, error FROM peg_checks WHERE snapshot_id = ? "
                "ORDER BY symbol, reference",
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


def _insert_snapshot(connection: sqlite3.Connection, portfolio: Portfolio, trigger: str,
                     is_backfilled: bool = False) -> int:
    """Insert one `snapshots` row and its `snapshot_assets` rows; returns the snapshot id."""
    totals = {source: float(portfolio.totals.get(source, 0.0)) for source in SOURCE_COLUMNS}
    cursor = connection.execute(
        """INSERT INTO snapshots
               (timestamp, total_portfolio_usd, binance_spot_usd, binance_earn_usd,
                binance_funding_usd, onchain_usd, manual_usd, trigger, is_backfilled)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            portfolio.timestamp or utc_now_iso(),
            float(portfolio.total_usd),
            totals[BINANCE_SPOT],
            totals[BINANCE_EARN],
            totals[BINANCE_FUNDING],
            totals[ONCHAIN],
            totals[MANUAL],
            trigger,
            int(is_backfilled),
        ),
    )
    snapshot_id = int(cursor.lastrowid)
    connection.executemany(
        """INSERT INTO snapshot_assets
               (snapshot_id, symbol, source, quantity, price_usd, value_usd, detail, price_id,
                listed_apr)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (snapshot_id, h.symbol, h.source, float(h.quantity), float(h.price_usd),
             float(h.value_usd), h.detail or "", h.price_id or "", h.listed_apr)
            for h in portfolio.holdings
        ],
    )
    return snapshot_id


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


def journal_mode(path: str | Path) -> str:
    """Current journal mode of the database at `path` (`wal`, `delete`, ...), lower case."""
    connection = sqlite3.connect(path, timeout=30)
    try:
        return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        connection.close()


def enable_wal(path: str | Path) -> str:
    """Switch the database at `path` to WAL; returns the journal mode actually in effect.

    A no-op when the file is already in WAL. The switch needs a moment of exclusive access, so
    it waits (busy timeout) for other connections rather than failing. SQLite silently keeps
    the old mode where WAL is impossible (in-memory database, some network filesystems); that
    is logged, because readers would then be locked out during writes again.
    """
    connection = sqlite3.connect(path, timeout=30)
    try:
        mode = str(connection.execute(f"PRAGMA journal_mode = {JOURNAL_MODE}").fetchone()[0]).lower()
    finally:
        connection.close()
    if mode != JOURNAL_MODE:
        log.warning("%s: could not enable WAL (journal mode stays %r); readers may hit "
                    "'database is locked' while a snapshot is written", path, mode)
    return mode


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
        is_backfilled=bool(row["is_backfilled"]) if "is_backfilled" in row.keys() else False,
        manual_usd=float(row["manual_usd"] or 0.0) if "manual_usd" in row.keys() else 0.0,
    )
