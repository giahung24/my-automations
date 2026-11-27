"""N3: peg check. Offline: the references are plain dicts and callables."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import BINANCE_EARN, Holding, Portfolio
from services.db import MIGRATIONS, SCHEMA_VERSION, Database, migrate, schema_version
from services.peg import LLAMA_IDS, check_pegs, peg_warnings
from services.portfolio import portfolio_from_db

LLAMA = {LLAMA_IDS["USDC"]: 0.9995, LLAMA_IDS["USDT"]: 1.0004}


def test_usdc_has_two_references_and_usdt_one():
    rows = check_pegs({"USDC", "USDT"}, {"USDCUSDT": 0.9991}, lambda keys: LLAMA)
    refs = {(r["symbol"], r["reference"].split(":")[0]) for r in rows}
    assert refs == {("USDC", "binance"), ("USDC", "defillama"), ("USDT", "defillama")}
    assert all(not r["error"] for r in rows)
    assert peg_warnings(rows) == []  # under 0.3%: hidden


def test_off_peg_reports_the_worst_usable_reference():
    rows = check_pegs({"USDC"}, {"USDCUSDT": 0.996}, lambda keys: {LLAMA_IDS["USDC"]: 0.999})
    assert peg_warnings(rows) == [{"symbol": "USDC", "status": "off", "deviation_pct": -0.4}]


def test_one_failed_reference_falls_back_to_the_other():
    def boom(keys):
        raise RuntimeError("timeout")

    rows = check_pegs({"USDC"}, {"USDCUSDT": 1.0001}, boom)
    assert [bool(r["error"]) for r in rows] == [False, True]
    assert peg_warnings(rows) == []


def test_every_reference_failed_is_unchecked():
    def boom(keys):
        raise RuntimeError("timeout")

    rows = check_pegs({"USDC", "USDT"}, None, boom)
    assert {w["symbol"]: w["status"] for w in peg_warnings(rows)} == {"USDC": "unchecked", "USDT": "unchecked"}


def test_record_stores_checks_and_listed_apr_and_cascades(tmp_path):
    database = Database(tmp_path / "p.db")
    portfolio = Portfolio(
        timestamp="2026-11-27T10:00:00+00:00",
        holdings=[Holding("USDC", BINANCE_EARN, 100.0, 1.0, detail="flexible", listed_apr=0.052)],
        totals={BINANCE_EARN: 100.0},
        peg_checks=check_pegs({"USDC"}, None, lambda keys: {LLAMA_IDS["USDC"]: 0.99}),
    )
    snapshot_id = database.record(portfolio)
    assert database.assets(snapshot_id)[0]["listed_apr"] == 0.052
    stored = portfolio_from_db(database)
    assert stored.holdings[0].listed_apr == 0.052
    assert peg_warnings(stored.peg_checks) == [{"symbol": "USDC", "status": "off", "deviation_pct": -1.0}]
    with database.connect() as connection:
        connection.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))
    assert database.peg_checks(snapshot_id) == []


def test_v5_database_migrates_without_losing_rows(tmp_path):
    path = tmp_path / "old.db"
    migrate(path, MIGRATIONS[:5])
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO snapshots (timestamp, total_portfolio_usd) VALUES ('2026-11-01T00:00:00+00:00', 5)")
        connection.execute("INSERT INTO snapshot_assets (snapshot_id, symbol, source, quantity) VALUES (1, 'BTC', 'binance_spot', 1)")
    database = Database(path)
    assert schema_version(path) == SCHEMA_VERSION >= 6
    assert database.assets(1)[0]["listed_apr"] is None
    assert database.peg_checks(1) == []
