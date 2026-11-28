"""N2: residuals, inferred trades, unexplained changes, data-health count. Offline."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import BINANCE_EARN, BINANCE_FUNDING, BINANCE_SPOT, Holding, Portfolio, SourceStatus
from services.activity import activity_report
from services.cash_flows import CHANNEL_EARN, IN, OUT, REWARD, CashFlow, upsert_flows
from services.db import Database
from services.reconcile import inferred_items, pair_residuals

T0 = datetime(2026, 11, 25, 10, tzinfo=timezone.utc)
PRICES = {"USDC": 1.0, "USDT": 1.0, "BTC": 90_000.0, "SOL": 140.0, "LINK": 14.0}


def snap(database, hour, spot=None, earn=None, failed=()):
    holdings = [Holding(s, BINANCE_SPOT, q, PRICES[s]) for s, q in (spot or {}).items()]
    holdings += [Holding(s, BINANCE_EARN, q, PRICES[s]) for s, q in (earn or {}).items()]
    sources = {s: SourceStatus("error" if s in failed else "ok")
               for s in (BINANCE_SPOT, BINANCE_EARN, BINANCE_FUNDING)}
    when = (T0 + timedelta(hours=hour)).isoformat()
    return database.record(Portfolio(timestamp=when, holdings=holdings, totals={}, sources=sources))


def flow(hour, asset, amount, direction=IN, flow_type="deposit", fee=0.0, **extra):
    when = (T0 + timedelta(hours=hour)).isoformat()
    return CashFlow(timestamp=when, asset=asset, direction=direction, amount=amount, flow_type=flow_type,
                    fee_amount=fee, external_id=f"{flow_type}:{asset}:{hour}", **extra)


def kinds(items):
    return [item["kind"] for item in items]


def test_spot_to_earn_move_is_not_a_residual(tmp_path):
    database = Database(tmp_path / "p.db")
    snap(database, 0, spot={"USDC": 5000.0})
    snap(database, 1, earn={"USDC": 5000.0})
    assert inferred_items(database) == []


def test_recorded_flows_and_rewards_leave_no_residual(tmp_path):
    database = Database(tmp_path / "p.db")
    snap(database, 0, spot={"USDC": 5000.0, "SOL": 10.0})
    upsert_flows(database, [
        flow(0.5, "USDC", 1000.0),                                             # deposit
        flow(0.5, "SOL", 3.0, direction=OUT, flow_type="withdrawal", fee=0.01),  # withdrawal + fee
        flow(0.6, "USDC", 2.5, flow_type=REWARD, channel=CHANNEL_EARN),
    ])
    snap(database, 1, spot={"USDC": 6002.5, "SOL": 6.99})
    assert inferred_items(database) == []


def test_stablecoins_into_coins_is_one_inferred_trade(tmp_path):
    database = Database(tmp_path / "p.db")
    snap(database, 0, spot={"USDT": 2000.0}, earn={"USDC": 20_000.0, "BTC": 0.1})
    snap(database, 1, earn={"USDC": 17_600.0, "BTC": 0.146, "SOL": 1.8})   # -4400 stable, +4392 coins
    snap(database, 2, earn={"USDC": 17_600.0, "BTC": 0.146, "SOL": 1.8})
    [trade] = inferred_items(database)
    assert trade["kind"] == "trade" and trade["inferred"] is True
    assert [leg["asset"] for leg in trade["sold"]] == ["USDC", "USDT"]
    assert [leg["asset"] for leg in trade["bought"]] == ["BTC", "SOL"]
    assert trade["value_usd"] == 4396.0 and trade["timestamp"] == (T0 + timedelta(hours=1)).isoformat()


def test_one_sided_change_is_unexplained_and_grouped_by_day(tmp_path):
    database = Database(tmp_path / "p.db")
    snap(database, 0, spot={"LINK": 100.0, "USDC": 100.0})
    snap(database, 1, spot={"LINK": 130.0, "USDC": 100.0})
    snap(database, 2, spot={"LINK": 130.0, "USDC": 40.0})
    snap(database, 3, spot={"LINK": 130.0, "USDC": 40.5})   # 50 cents: dust, ignored
    [item] = inferred_items(database)
    assert item["kind"] == "unexplained" and item["group"] and "sold" not in item
    assert [(leg["asset"], leg["amount"]) for leg in item["assets"]] == [("LINK", 30.0), ("USDC", -60.0)]


def test_a_failed_binance_fetch_creates_no_residual(tmp_path):
    database = Database(tmp_path / "p.db")
    snap(database, 0, earn={"USDC": 9000.0})
    snap(database, 1, failed=(BINANCE_EARN,))   # recorded as 0, not a real withdrawal
    snap(database, 2, earn={"USDC": 9000.0})
    assert inferred_items(database) == []
    assert database.failed_snapshots_since((T0 + timedelta(hours=2)).isoformat()) == 1
    assert database.failed_snapshots_since((T0 + timedelta(hours=30)).isoformat()) == 0


def test_unbalanced_sides_are_not_called_a_trade():
    trade, legs = pair_residuals({"USDC": (-5000.0, -5000.0), "BTC": (0.0001, 9.0)})
    assert trade is None and len(legs) == 2


def test_activity_lists_filters_and_pages_the_inferred_items(tmp_path):
    database = Database(tmp_path / "p.db")
    snap(database, 0, spot={"USDC": 9000.0})
    snap(database, 1, spot={"USDC": 4500.0, "BTC": 0.05})
    snap(database, 2, spot={"USDC": 4500.0, "BTC": 0.05, "LINK": 20.0})
    upsert_flows(database, [flow(0.2, "USDC", 1.0, flow_type=REWARD, channel=CHANNEL_EARN)])
    now = T0 + timedelta(hours=3)
    report = activity_report(database, days=0, now=now)
    assert sorted(kinds(report["items"])) == ["reward", "trade", "unexplained"]
    assert report["facets"]["kind"]["trade"] == 1 and report["facets"]["kind"]["unexplained"] == 1
    assert kinds(activity_report(database, days=0, kind="trade", now=now)["items"]) == ["trade"]
    assert kinds(activity_report(database, days=0, asset="LINK", now=now)["items"]) == ["unexplained"]
    first = activity_report(database, days=0, limit=1, now=now)
    second = activity_report(database, days=0, limit=1, cursor=first["next_cursor"], now=now)
    assert first["items"][0]["key"] != second["items"][0]["key"]
    assert all("_last_id" not in item for item in first["items"] + second["items"])


def test_old_history_is_not_reconciled_but_recent_history_is(tmp_path):
    database = Database(tmp_path / "p.db")
    snap(database, 0, spot={"LINK": 100.0})
    snap(database, 1, spot={"LINK": 130.0})   # unexplained
    recent = activity_report(database, days=0, now=T0 + timedelta(days=89))
    assert kinds(recent["items"]) == ["unexplained"]
    late = activity_report(database, days=0, now=T0 + timedelta(days=91))
    assert late["items"] == [] and late["facets"]["kind"]["unexplained"] == 0
