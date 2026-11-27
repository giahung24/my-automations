"""N5: the analyst views agree with the analytics engine, and `export` writes them out."""
from __future__ import annotations

import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as cli
from models import BINANCE_EARN, BINANCE_SPOT, Holding, SourceStatus
from services.analytics.quant import daily_closes, daily_returns, degraded_snapshot_ids, load_flows
from services.cash_flows import CHANNEL_EARN, IN, OUT, REWARD, CashFlow, upsert_flows
from services.db import ANALYST_VIEWS, Database
from services.portfolio import aggregate
from services.trades import Trade, upsert_trades

T0 = datetime(2026, 11, 20, 0, 30, tzinfo=timezone.utc)


def seeded(tmp_path) -> Database:
    database = Database(tmp_path / "p.db")
    btc = 0.5
    for hour in range(0, 5 * 24, 6):
        when = T0 + timedelta(hours=hour)
        if hour == 30:
            btc += 0.1     # the deposit below
        if hour == 78:
            btc -= 0.05    # the withdrawal below
        failed = hour == 66   # a day's last snapshot fails: the close falls back to the one before
        portfolio = aggregate([Holding("BTC", BINANCE_SPOT, 0 if failed else btc, 90_000.0 + 40 * hour),
                               Holding("USDC", BINANCE_EARN, 1000.0, 1.0)])
        portfolio.timestamp = when.isoformat()
        portfolio.sources = {BINANCE_SPOT: SourceStatus("error" if failed else "ok"), BINANCE_EARN: SourceStatus("ok")}
        database.record(portfolio)
    at = lambda hours: (T0 + timedelta(hours=hours)).isoformat()  # noqa: E731
    upsert_flows(database, [
        CashFlow(timestamp=at(27), asset="BTC", direction=IN, amount=0.1, flow_type="deposit", external_id="d", price_usd=91_000.0),
        CashFlow(timestamp=at(75), asset="BTC", direction=OUT, amount=0.05, flow_type="withdrawal", external_id="w", price_usd=93_000.0),
        CashFlow(timestamp=at(80), asset="BTC", direction=OUT, amount=0.2, flow_type="withdrawal", external_id="i",
                 price_usd=93_000.0, is_internal_transfer=True),
        CashFlow(timestamp=at(10), asset="USDC", direction=IN, amount=0.3, flow_type=REWARD, external_id="r1",
                 channel=CHANNEL_EARN, price_usd=1.0),
        CashFlow(timestamp=at(12), asset="USDC", direction=IN, amount=0.2, flow_type=REWARD, external_id="r2",
                 channel=CHANNEL_EARN, price_usd=1.0),
    ])
    upsert_trades(database, [Trade(timestamp=at(50), venue="spot", base="BTC", quote="USDC", side="buy", quantity=0.01,
                                   quote_quantity=920.0, external_id="t", quote_usd=1.0)])
    return database


def view(database, name):
    with database.connect() as connection:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {name}").fetchall()]


def test_daily_returns_view_matches_the_quant_engine(tmp_path):
    database = seeded(tmp_path)
    with database.connect() as connection:
        snapshots = [(datetime.fromisoformat(r["timestamp"]), r["total_portfolio_usd"], r["id"])
                     for r in connection.execute("SELECT * FROM snapshots ORDER BY timestamp")]
    closes = daily_closes(snapshots, degraded_snapshot_ids(database))
    flows = load_flows(database, closes[0].timestamp, closes[-1].timestamp)
    expected = daily_returns(closes, flows)

    rows = view(database, "v_daily_returns")
    assert [row["day"] for row in rows] == [str(close.day) for close in closes]
    assert [row["nav_usd"] for row in rows] == pytest.approx([close.nav for close in closes])
    assert rows[0]["return"] is None
    assert [row["return"] for row in rows[1:]] == pytest.approx([o.r for o in expected], rel=1e-6)
    assert [row["net_flow_usd"] for row in rows[1:]] == pytest.approx([o.net_flow_usd for o in expected])
    assert any(row["net_flow_usd"] for row in rows)   # the fixture really has flows in the window


def test_other_views_and_export(tmp_path, capsys):
    database = seeded(tmp_path)
    positions = view(database, "v_positions_daily")
    assert {row["symbol"] for row in positions} == {"BTC", "USDC"} and len(positions) == 10
    assert [(f["flow_type"], f["signed_usd"]) for f in view(database, "v_external_flows")] == [("deposit", 9100.0), ("withdrawal", -4650.0)]
    assert view(database, "v_earn_income_daily") == [{"day": "2026-11-20", "asset": "USDC", "amount": 0.5, "value_usd": 0.5, "payouts": 2}]
    assert view(database, "v_trades")[0]["price_usd"] == 92_000.0

    out = tmp_path / "csv"
    assert cli.cmd_export(SimpleNamespace(out=str(out)), SimpleNamespace(db_path=database.path)) == 0
    assert sorted(p.name for p in out.iterdir()) == sorted(f"{name.removeprefix('v_')}.csv" for name in ANALYST_VIEWS)
    with open(out / "daily_returns.csv", newline="", encoding="utf-8") as handle:
        table = list(csv.reader(handle))
    assert table[0] == ["day", "nav_usd", "net_flow_usd", "return"] and len(table) == 6
