"""N1: Earn yield lens. Offline, on a synthetic SQLite history."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import BINANCE_EARN, Holding, Portfolio, SourceStatus
from services.analytics.earn import earn_report, realised_apr
from services.cash_flows import CHANNEL_EARN, IN, REWARD, CashFlow, upsert_flows
from services.db import Database

END = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
BALANCE, RATE = 10_000.0, 0.05


def record(database, when, quantity=BALANCE, failed=False):
    portfolio = Portfolio(
        timestamp=when.isoformat(),
        holdings=[] if failed else [Holding("USDC", BINANCE_EARN, quantity, 1.0, detail="flexible", listed_apr=0.052)],
        totals={BINANCE_EARN: 0.0 if failed else quantity},
        sources={BINANCE_EARN: SourceStatus("error" if failed else "ok", "boom" if failed else "")},
    )
    return database.record(portfolio)


def reward(when, amount, asset="USDC", price=1.0):
    return CashFlow(timestamp=when.isoformat(), asset=asset, direction=IN, amount=amount, flow_type=REWARD,
                    external_id=f"t:{asset}:{when.isoformat()}", channel=CHANNEL_EARN,
                    price_usd=price)


def history(tmp_path, days, skip=lambda hour: False):
    database = Database(tmp_path / "p.db")
    for hour in range(days * 24, -1, -1):
        if not skip(hour):
            record(database, END - timedelta(hours=hour))
    daily = BALANCE * RATE / 365
    upsert_flows(database, [reward(END - timedelta(days=d, hours=6), daily) for d in range(days + 400)])
    return database


def test_realised_apr_matches_the_rate_paid(tmp_path):
    report = earn_report(history(tmp_path, days=8), {"USDC"})
    usdc = report["assets"][0]
    assert usdc["symbol"] == "USDC" and usdc["listed_apr"] == pytest.approx(0.052)
    week = usdc["realised"]["7d"]
    assert week["sufficient"] and week["apr"] == pytest.approx(RATE, abs=0.002)  # AC: 5.0% +-0.2 pp
    assert usdc["usd_per_day"] == pytest.approx(BALANCE * RATE / 365, rel=0.05)
    assert report["usd_per_day"] == usdc["usd_per_day"]
    # 8 days of snapshots do not cover 30: no made-up figure
    month = usdc["realised"]["30d"]
    assert month["apr"] is None and not month["sufficient"] and month["days"] == pytest.approx(8, abs=0.1)


def test_a_gap_in_the_window_gives_no_figure(tmp_path):
    report = earn_report(history(tmp_path, days=8, skip=lambda hour: 40 <= hour <= 60), {"USDC"})
    assert report["assets"][0]["realised"]["7d"] == {"apr": None, "n_obs": 148, "sufficient": False, "days": 7.0}


def test_failed_earn_fetch_is_not_read_as_an_empty_account(tmp_path):
    database = history(tmp_path, days=8)
    record(database, END - timedelta(hours=3, minutes=30), failed=True)
    week = earn_report(database, {"USDC"})["assets"][0]["realised"]["7d"]
    assert week["apr"] == pytest.approx(RATE, abs=0.002)


def test_zero_balance_never_divides():
    start = END - timedelta(days=7)
    balances = [(start + timedelta(hours=h), 0.0) for h in range(169)]
    assert realised_apr(balances, [(END, 1.0)], 7)["apr"] is None


def test_lifetime_income_by_year_matches_the_ledger(tmp_path):
    database = Database(tmp_path / "p.db")
    upsert_flows(database, [
        reward(datetime(2024, 3, 1, tzinfo=timezone.utc), 10.0),
        reward(datetime(2025, 3, 1, tzinfo=timezone.utc), 20.0),
        reward(datetime(2025, 6, 1, tzinfo=timezone.utc), 0.001, asset="BTC", price=60_000.0),
        reward(datetime(2025, 7, 1, tzinfo=timezone.utc), 5.0, asset="XYZ", price=0.0),
    ])
    report = earn_report(database, {"USDC"})
    life = report["lifetime"]
    assert report["assets"] == [] and report["usd_per_day"] is None  # no snapshot yet: still answers
    assert life["since"].startswith("2024-03-01") and life["unpriced"] == 1
    assert [(y["year"], y["usd"], y["stable_usd"]) for y in life["by_year"]] == [("2024", 10.0, 10.0), ("2025", 80.0, 20.0)]
    assert life["total_usd"] == 90.0 and life["stable_usd"] == 30.0
    with database.connect() as connection:
        ledger = connection.execute("SELECT SUM(value_usd) FROM cash_flows WHERE flow_type = 'reward'").fetchone()[0]
    assert life["total_usd"] == pytest.approx(ledger)
