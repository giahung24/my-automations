"""N9: the investment diary. Offline."""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import web_api
from models import BINANCE_SPOT, Holding
from services.db import MIGRATIONS, Database, migrate
from services.portfolio import aggregate


@pytest.fixture
def env(tmp_path):
    db_path = tmp_path / "history.db"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"settings": {"db_path": str(db_path)}}), encoding="utf-8")
    return TestClient(web_api.create_app(cfg, fetcher=lambda *a: None)), Database(db_path)


def test_write_edit_delete_a_note(env):
    client, database = env
    assert client.put("/api/notes/2026-11-25", json={"body": "  Bought the dip.\n<b>USDT out</b> "}).json() == {
        "day": "2026-11-25", "body": "Bought the dip.\n<b>USDT out</b>"}   # stored as typed: escaping is the page's job
    client.put("/api/notes/2026-11-25", json={"body": "Bought the dip, second thoughts."})
    assert [n["body"] for n in database.notes()] == ["Bought the dip, second thoughts."]   # one note per day
    assert client.put("/api/notes/2026-11-25", json={"body": "   "}).json() == {"day": "2026-11-25", "body": ""}
    assert database.notes() == []


def test_bad_days_and_oversized_notes_are_refused(env):
    client, _ = env
    for day in ("2026-13-40", "yesterday", "2026-1-1"):
        assert client.put(f"/api/notes/{day}", json={"body": "x"}).status_code == 422, day
    assert client.put("/api/notes/2026-11-25", json={"body": "x" * 2001}).status_code == 422


def test_note_writes_need_the_access_token(env, monkeypatch):
    client, database = env
    monkeypatch.setenv("PORTFOLIO_WEB_TOKEN", "secret-token-123")
    assert client.put("/api/notes/2026-11-25", json={"body": "x"}).status_code == 401
    assert database.notes() == []


def test_notes_show_in_activity_and_in_the_period_they_belong_to(env):
    client, database = env
    now = datetime.now(timezone.utc)
    for hours in (30, 2):
        portfolio = aggregate([Holding("BTC", BINANCE_SPOT, 1.0, 90_000.0 + hours)])
        portfolio.timestamp = (now - timedelta(hours=hours)).isoformat()
        database.record(portfolio)
    today, old = now.strftime("%Y-%m-%d"), (now - timedelta(days=40)).strftime("%Y-%m-%d")
    database.set_note(today, "Rebalanced.")
    database.set_note(old, "Before the window.")
    assert client.get("/api/cash-flows?days=30").json()["notes"] == {today: "Rebalanced."}
    assert set(client.get("/api/cash-flows?days=0").json()["notes"]) == {today, old}
    why = client.get("/api/portfolio/attribution?window=7d").json()
    assert [n["body"] for n in why["notes"]] == ["Rebalanced."]


def test_notes_survive_a_migration_and_its_backup(tmp_path):
    path = tmp_path / "p.db"
    migrate(path, MIGRATIONS[:11])            # the version that introduced notes
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO notes VALUES ('2026-11-25', 'keep me', '2026-11-25T10:00:00+00:00')")
    later = [*MIGRATIONS[:11], "CREATE TABLE future_table (x INTEGER);"]
    assert migrate(path, later) == 12
    [backup] = list(tmp_path.glob("p.db.bak_v11_*"))
    for file in (path, backup):
        with sqlite3.connect(file) as connection:
            assert connection.execute("SELECT body FROM notes").fetchall() == [("keep me",)]
