"""Listed APR with Binance's tiered bonus (``tierAnnualPercentageRate``). Offline.

The payload shapes below are copied from a real ``GET /sapi/v1/simple-earn/flexible/position``
answer (2026-11-27): ``tierAnnualPercentageRate`` is an object whose keys are balance bands
``"<min>-<max><ASSET>"`` and whose values are the bonus APR of that band; positions with no bonus
leave the field out.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clients.binance import effective_apr, parse_tiers
from models import BINANCE_EARN, Holding, Portfolio
from services.analytics.earn import earn_report
from services.db import MIGRATIONS, SCHEMA_VERSION, Database, migrate, schema_version
from services.portfolio import portfolio_from_db
from tests.test_all import binance_session, make_client

USDC_ROW = {"totalAmount": "25949.68776972", "tierAnnualPercentageRate": {"0-5000USDC": "0.03000000"},
            "latestAnnualPercentageRate": "0.02017994", "asset": "USDC", "productId": "USDC001"}
LINK_ROW = {"totalAmount": "200.19382747", "latestAnnualPercentageRate": "0.00011811", "asset": "LINK"}


def flexible(*rows):
    return make_client(binance_session(**{
        "/simple-earn/flexible/position": {"rows": list(rows), "total": len(rows)}})).earn_flexible()


def test_parse_tiers_reads_the_band_keys():
    assert parse_tiers({"0-5000USDC": "0.03000000"}) == [{"min": 0.0, "max": 5000.0, "apr": 0.03}]
    assert parse_tiers({"0-0.01BTC": "0.0025"}) == [{"min": 0.0, "max": 0.01, "apr": 0.0025}]
    # several bands come back sorted; an unreadable key or rate is skipped, not guessed
    assert parse_tiers({"5-10ETH": "0.01", "0-5ETH": "0.02", "over 10ETH": "0.5", "10-20ETH": "x"}) == [
        {"min": 0.0, "max": 5.0, "apr": 0.02}, {"min": 5.0, "max": 10.0, "apr": 0.01}]
    assert parse_tiers(None) == [] and parse_tiers({}) == [] and parse_tiers("0.03") == []


def test_effective_apr_pays_each_band_only_on_the_balance_inside_it():
    tiers = [{"min": 0.0, "max": 5000.0, "apr": 0.03}]
    # whole balance inside the band: base + full bonus
    assert effective_apr(0.02, tiers, 4000) == pytest.approx(0.05)
    # 25k: the 3% is earned on the first 5k only -> 2% + 3% x 5k / 25k
    assert effective_apr(0.02, tiers, 25_000) == pytest.approx(0.026)
    two = [{"min": 0.0, "max": 5.0, "apr": 0.02}, {"min": 5.0, "max": 10.0, "apr": 0.01}]
    assert effective_apr(0.001, two, 20) == pytest.approx(0.001 + (0.02 * 5 + 0.01 * 5) / 20)
    # no tier: the base rate unchanged; nothing known at all: None
    assert effective_apr(0.02, [], 25_000) == 0.02
    assert effective_apr(None, [], 10) is None
    assert effective_apr(None, tiers, 1000) == pytest.approx(0.03)


def test_earn_flexible_lists_base_plus_tier_with_breakdown():
    usdc, link = flexible(USDC_ROW, LINK_ROW)
    assert usdc.listed_apr == pytest.approx(0.02017994 + 0.03 * 5000 / 25949.68776972)  # ~2.60%
    assert usdc.listed_base_apr == pytest.approx(0.02017994)
    assert usdc.listed_tiers == [{"min": 0.0, "max": 5000.0, "apr": 0.03}]
    # no `tierAnnualPercentageRate`: falls back to the base rate, empty breakdown
    assert link.listed_apr == pytest.approx(0.00011811) == link.listed_base_apr
    assert link.listed_tiers == []


def test_earn_flexible_below_the_band_cap_gets_the_full_bonus():
    small = dict(USDC_ROW, totalAmount="1000")
    assert flexible(small)[0].listed_apr == pytest.approx(0.05017994)


def test_breakdown_round_trips_through_sqlite_and_the_earn_report(tmp_path):
    database = Database(tmp_path / "p.db")
    holding = Holding("USDC", BINANCE_EARN, 25_000.0, 1.0, detail="flexible", listed_apr=0.026,
                      listed_base_apr=0.02, listed_tiers=[{"min": 0.0, "max": 5000.0, "apr": 0.03}])
    snapshot_id = database.record(Portfolio(timestamp="2026-11-27T10:00:00+00:00", holdings=[holding],
                                            totals={BINANCE_EARN: 25_000.0}))
    row = database.assets(snapshot_id)[0]
    assert (row["listed_apr"], row["listed_base_apr"]) == (0.026, 0.02)
    assert row["listed_tiers"] == [{"min": 0.0, "max": 5000.0, "apr": 0.03}]
    stored = portfolio_from_db(database).holdings[0]
    assert stored.listed_tiers == holding.listed_tiers and stored.listed_base_apr == 0.02
    usdc = earn_report(database, {"USDC"})["assets"][0]
    assert usdc["listed_apr"] == pytest.approx(0.026)
    assert usdc["listed_base_apr"] == pytest.approx(0.02)
    assert usdc["listed_tiers"] == [{"min": 0.0, "max": 5000.0, "apr": 0.03}]


def test_v6_database_migrates_with_an_empty_breakdown(tmp_path):
    path = tmp_path / "old.db"
    migrate(path, MIGRATIONS[:6])
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO snapshots (timestamp, total_portfolio_usd) VALUES ('2026-11-27T00:00:00+00:00', 5)")
        connection.execute("INSERT INTO snapshot_assets (snapshot_id, symbol, source, quantity, listed_apr) "
                           "VALUES (1, 'USDC', 'binance_earn', 5, 0.0196)")
    database = Database(path)
    assert schema_version(path) == SCHEMA_VERSION >= 7
    row = database.assets(1)[0]
    assert row["listed_apr"] == 0.0196 and row["listed_base_apr"] is None and row["listed_tiers"] == []
