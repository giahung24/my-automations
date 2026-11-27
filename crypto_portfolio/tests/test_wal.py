"""P0.4: the database runs in WAL mode, so the gateway's `mode=ro` reader never sees
`database is locked` while the collector is writing.

    .venv/bin/pytest test_wal.py -q
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api_gateway import ENDPOINT, Settings, connect_readonly, create_app
from models import BINANCE_SPOT, Holding
from services.portfolio import aggregate
from services.db import JOURNAL_MODE, Database, enable_wal, journal_mode
from test_all import make_v0_db

KEY = "k" * 43


def portfolio(value: float) -> Portfolio:
    return aggregate([Holding("BTC", BINANCE_SPOT, 1.0, value)])


def snapshot_count(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])


def open_write_transaction(path: Path, lock: str = "IMMEDIATE") -> sqlite3.Connection:
    """A collector in the middle of `record`: one uncommitted snapshot, write lock held."""
    writer = sqlite3.connect(path, isolation_level=None, timeout=0)
    writer.execute(f"BEGIN {lock}")
    writer.execute(
        "INSERT INTO snapshots (timestamp, total_portfolio_usd) VALUES ('2099-01-01T00:00:00+00:00', 999)"
    )
    return writer


@pytest.fixture
def db(tmp_path) -> Path:
    database = Database(tmp_path / "p.db")
    database.record(portfolio(100.0))
    return database.path


# -- WAL is switched on, once, at init ---------------------------------------------------
def test_new_database_is_in_wal_mode(db):
    assert JOURNAL_MODE == "wal"
    assert journal_mode(db) == "wal"
    # persistent: a fresh, plain connection (not ours) sees it too
    connection = sqlite3.connect(db)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    connection.close()


def test_existing_rollback_database_is_switched_to_wal_without_data_loss(tmp_path):
    path = tmp_path / "old.db"
    make_v0_db(path, snapshots=4)
    assert journal_mode(path) == "delete"

    database = Database(path)

    assert journal_mode(path) == "wal"
    assert database.count() == 4


def test_enable_wal_is_idempotent(db):
    assert enable_wal(db) == "wal"
    assert enable_wal(db) == "wal"
    Database(db)  # re-opening an already-WAL database is fine
    assert journal_mode(db) == "wal"


def test_enable_wal_reports_a_mode_that_could_not_be_set(caplog):
    # an in-memory database cannot use WAL; we must say so instead of pretending
    assert enable_wal(":memory:") == "memory"
    assert "WAL" in caplog.text


def test_wal_is_enabled_by_init_not_by_every_connection(db, monkeypatch):
    database = Database(db)
    calls = []
    monkeypatch.setattr("services.db.enable_wal", lambda path: calls.append(path) or "wal")
    database.record(portfolio(1.0))
    database.history()
    assert calls == []  # journal_mode is persistent in the file; no per-connection pragma


# -- AC: a mode=ro read while a write transaction is open is not locked -----------------
@pytest.mark.parametrize("lock", ["IMMEDIATE", "EXCLUSIVE"])
def test_readonly_read_during_open_write_transaction_is_not_locked(db, lock):
    writer = open_write_transaction(db, lock)
    try:
        reader = connect_readonly(db)
        try:
            started = time.monotonic()
            assert snapshot_count(reader) == 1  # last committed state, not the pending row
            assert time.monotonic() - started < 1  # no busy-wait on the writer's lock
        finally:
            reader.close()
    finally:
        writer.execute("COMMIT")
        writer.close()

    reader = connect_readonly(db)
    assert snapshot_count(reader) == 2  # the commit is visible to the next read
    reader.close()


def test_readonly_read_during_database_record_is_not_locked(db):
    """Same AC through the real collector code path: read inside `Database.connect()`."""
    database = Database(db)
    with database.connect() as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO snapshots (timestamp) VALUES ('2099-01-01T00:00:00+00:00')")
        reader = connect_readonly(db)
        assert snapshot_count(reader) == 1
        reader.close()
    assert database.count() == 2


def test_rollback_mode_would_lock_the_reader(tmp_path):
    """Control: without WAL, the same read fails, so the AC test above is meaningful."""
    path = tmp_path / "rollback.db"
    make_v0_db(path, snapshots=1)
    writer = open_write_transaction(path, "EXCLUSIVE")
    try:
        reader = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            snapshot_count(reader)
        reader.close()
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_gateway_answers_200_while_collector_writes(db):
    settings = Settings(api_key=KEY, db_path=db, allowed_hosts=frozenset({"testserver"}))
    client = TestClient(create_app(settings), raise_server_exceptions=False)
    writer = open_write_transaction(db, "EXCLUSIVE")
    try:
        response = client.get(ENDPOINT, headers={"X-API-Key": KEY})
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert response.status_code == 200
    assert response.json()["value"] == "100.00"


# -- the read-only side stays read-only and works without a live writer ------------------
def test_readonly_read_works_with_no_wal_files_left(db):
    # the last writer closed cleanly: SQLite checkpointed and removed -wal (and -shm)
    assert not Path(f"{db}-wal").exists()
    reader = connect_readonly(db)
    assert snapshot_count(reader) == 1
    reader.close()


def test_readonly_connection_cannot_write_or_change_journal_mode(db):
    reader = connect_readonly(db)
    with pytest.raises(sqlite3.OperationalError):
        reader.execute("INSERT INTO snapshots (timestamp) VALUES ('x')")
    reader.close()
    reader = connect_readonly(db)
    try:  # a read-only connection either refuses or ignores the switch out of WAL
        assert reader.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "wal"
    except sqlite3.OperationalError:
        pass
    reader.close()
    assert journal_mode(db) == "wal"


def test_readonly_reader_does_not_block_the_collector(db):
    reader = connect_readonly(db)
    reader.execute("BEGIN")
    assert snapshot_count(reader) == 1  # holds a read snapshot open
    try:
        Database(db).record(portfolio(200.0))  # would wait on a SHARED lock in rollback mode
    finally:
        reader.execute("COMMIT")
        reader.close()
    assert Database(db).count() == 2


# -- file mode -------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_database_file_is_private(tmp_path):
    path = tmp_path / "new.db"
    Database(path)
    assert path.stat().st_mode & 0o777 == 0o600

    wide = tmp_path / "wide.db"
    Database(wide)
    wide.chmod(0o644)
    Database(wide)                                  # an existing world-readable file is tightened
    assert wide.stat().st_mode & 0o777 == 0o600
