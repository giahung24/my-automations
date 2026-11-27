"""F3.quant: TWR, Base-100 vs BTC/ETH, Max Drawdown / underwater, annualised volatility.

Pure engines are tested on plain data; the report and the endpoint on a temporary SQLite file.
No network.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

import web_api
from config import load_config
from models import BINANCE_SPOT, ONCHAIN, Holding, SourceStatus, STATUS_ERROR, STATUS_OK
from services.analytics.pnl import Flow
from services.analytics.quant import (
    Close,
    Observation,
    annualized_volatility,
    build_series,
    daily_closes,
    daily_returns,
    drawdown,
    parse_period,
    quant_report,
    rebase,
    time_weighted_return,
    twr_index,
)
from services.cash_flows import CashFlow, set_cursor, upsert_flows
from services.db import Database
from services.portfolio import aggregate

DAY0 = datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc)
NOW_MS = int((DAY0 + timedelta(days=400)).timestamp() * 1000)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def closes_of(navs, start=DAY0) -> list[Close]:
    return [Close((start + timedelta(days=i)).date(), start + timedelta(days=i), nav, i + 1)
            for i, nav in enumerate(navs)]


# == daily closes ===========================================================================
def test_daily_close_is_newest_healthy_snapshot_of_the_day():
    t = datetime(2026, 3, 1, 1, 0, tzinfo=timezone.utc)
    snapshots = [(t, 100.0, 1), (t + timedelta(hours=10), 110.0, 2), (t + timedelta(hours=20), 0.0, 3),
                 (t + timedelta(days=1), 120.0, 4)]
    closes = daily_closes(snapshots, degraded={3})
    assert [(c.day, c.nav, c.snapshot_id) for c in closes] == [
        (date(2026, 3, 1), 110.0, 2), (date(2026, 3, 2), 120.0, 4)]


def test_day_with_only_degraded_snapshots_is_a_gap():
    closes = daily_closes([(DAY0, 100.0, 1), (DAY0 + timedelta(days=1), 0.0, 2),
                           (DAY0 + timedelta(days=2), 105.0, 3)], degraded={2})
    observations = daily_returns(closes)
    assert len(observations) == 1 and observations[0].gap_days == 2
    assert observations[0].r == pytest.approx(0.05)


# == Modified Dietz daily returns ===========================================================
def test_return_without_flow_is_simple_ratio():
    obs = daily_returns(closes_of([1000.0, 1100.0, 990.0]))
    assert obs[0].r == pytest.approx(0.1, abs=1e-12) and obs[1].r == pytest.approx(-0.1, abs=1e-12)


def test_mid_day_deposit_on_flat_market_is_zero_return():
    closes = closes_of([10_000.0, 11_000.0])
    deposit = Flow(DAY0 + timedelta(hours=12), 1000.0)  # w = 0.5
    obs = daily_returns(closes, [deposit])
    assert obs[0].r == pytest.approx(0.0, abs=1e-12) and obs[0].net_flow_usd == 1000


def test_flow_at_previous_close_belongs_to_the_previous_step():
    closes = closes_of([100.0, 200.0, 200.0])
    obs = daily_returns(closes, [Flow(DAY0 + timedelta(days=1), 100.0)])  # exactly at t_1: in (t0, t1]
    assert obs[0].r == pytest.approx(0.0) and obs[1].r == pytest.approx(0.0)


def test_withdrawing_almost_everything_is_unstable():
    closes = closes_of([1000.0, 5.0])
    obs = daily_returns(closes, [Flow(DAY0 + timedelta(minutes=1), -995.0)])
    assert obs[0].r is None and obs[0].unstable


# == TWR ======================================================================================
def test_twr_plus_ten_minus_ten_is_minus_one_percent():
    index = twr_index([0.10, -0.10])
    assert index[0] == 100 and index[-1] == pytest.approx(99.0)
    twr = time_weighted_return(index, 30)
    assert twr["twr_pct"] == pytest.approx(-1.0)
    assert "annualized_pct" not in twr  # never annualise a period shorter than a year


def test_twr_annualised_only_from_one_year():
    index = [100.0, 121.0]
    out = time_weighted_return(index, 730)
    assert out["annualized_pct"] == pytest.approx(10.0)
    assert "annualized_pct" in time_weighted_return(index, 365)
    assert "annualized_pct" not in time_weighted_return(index, 364)


def test_twr_removes_cash_flows():
    """NAV doubles only because of a deposit: TWR is 0, the raw NAV change is +100%."""
    closes = closes_of([1000.0, 1000.0, 2000.0, 2200.0])
    series = build_series(closes, [Flow(DAY0 + timedelta(days=2), 1000.0)])  # at the close of day 2
    assert series.index[2] == pytest.approx(100.0)
    assert time_weighted_return(series.index, 3)["twr_pct"] == pytest.approx(10.0)


def test_unstable_return_carries_the_index():
    assert twr_index([0.1, None, 0.1]) == pytest.approx([100, 110, 110, 121])


# == Base 100 ================================================================================
def test_rebase_starts_at_100_from_first_known_price():
    assert rebase([None, 50.0, 75.0, None, 25.0]) == [None, 100.0, 150.0, None, 50.0]
    assert rebase([None, 0.0]) == [None, None]


# == Max Drawdown / underwater ================================================================
def test_mdd_reference_case():
    days = [date(2026, 1, d) for d in (1, 2, 3, 4)]
    out = drawdown(days, [100, 120, 90, 130])
    assert out["max_drawdown"] == pytest.approx(-0.25)
    assert (out["peak"], out["trough"], out["recovery"]) == ("2026-01-02", "2026-01-03", "2026-01-04")
    assert out["duration_days"] == 2 and out["recovered"] and out["current_drawdown"] == 0
    assert [p["drawdown"] for p in out["underwater"]] == pytest.approx([0, 0, -0.25, 0])


def test_mdd_not_recovered_and_monotonic():
    days = [date(2026, 1, d) for d in (1, 2, 3)]
    out = drawdown(days, [100, 80, 90])
    assert out["max_drawdown"] == pytest.approx(-0.2) and out["recovery"] is None
    assert out["current_drawdown"] == pytest.approx(-0.1) and out["duration_days"] == 2
    flat = drawdown(days, [100, 101, 102])
    assert flat["max_drawdown"] == 0 and flat["peak"] is None


def test_withdrawing_half_the_nav_is_not_a_drawdown():
    closes = closes_of([10_000.0, 10_000.0, 5_000.0, 5_000.0])
    series = build_series(closes, [Flow(DAY0 + timedelta(days=2), -5_000.0)])
    out = drawdown(series.days, series.index)
    assert out["max_drawdown"] == pytest.approx(0.0)
    raw = drawdown(series.days, [c.nav for c in closes])
    assert raw["max_drawdown"] == pytest.approx(-0.5)  # what the raw NAV would wrongly show


# == volatility ==============================================================================
def test_volatility_matches_numpy_and_excludes_gaps_and_unstable():
    rng = np.random.default_rng(7)
    r = rng.normal(0.001, 0.02, 40)
    observations = [Observation(date(2026, 1, 1) + timedelta(days=i), float(x)) for i, x in enumerate(r)]
    observations += [Observation(date(2026, 3, 1), 0.5, gap_days=3),
                     Observation(date(2026, 3, 2), None, unstable=True)]
    vol = annualized_volatility(observations, min_observations=30)
    assert vol["annualized"] == pytest.approx(np.std(r, ddof=1) * math.sqrt(365), abs=1e-12)
    assert vol["n_obs"] == 40 and vol["excluded_obs"] == 2 and vol["sufficient"]


def test_volatility_insufficient_sample():
    obs = [Observation(date(2026, 1, i), 0.01 * i) for i in range(1, 5)]
    vol = annualized_volatility(obs, min_observations=30)
    assert vol["n_obs"] == 4 and vol["sufficient"] is False and vol["annualized"] is not None
    assert annualized_volatility(obs[:1])["annualized"] is None


def test_parse_period():
    assert parse_period("30d") == 30 and parse_period("1y") == 365 and parse_period("all") is None
    for bad in ("0d", "x", "99999d"):
        with pytest.raises(ValueError):
            parse_period(bad)


# == report (SQLite) =========================================================================
def record(database: Database, when: datetime, holdings, sources=None) -> int:
    portfolio = aggregate(holdings)
    portfolio.timestamp = iso(when)
    if sources is not None:
        portfolio.sources = sources
    return database.record(portfolio)


def seed(database: Database, days: int = 5):
    """USDT 1000 + 0.01 BTC + 0.1 ETH; BTC goes 50k -> +2%/day, ETH flat at 3000."""
    for i in range(days):
        btc = 50_000 * 1.02 ** i
        record(database, DAY0 + timedelta(days=i), [Holding("USDT", BINANCE_SPOT, 1000, 1),
                                                    Holding("BTC", BINANCE_SPOT, 0.01, btc),
                                                    Holding("ETH", ONCHAIN, 0.1, 3000, detail="ethereum/main")])


def test_report_base100_against_btc_and_eth(tmp_path):
    database = Database(tmp_path / "h.db")
    seed(database)
    report = quant_report(database)
    assert report["available"] and report["n_obs"] == 4 and report["sufficient"] is False
    chart = report["chart"]
    assert chart["portfolio"][0] == chart["BTC"][0] == chart["ETH"][0] == 100
    assert chart["BTC"][-1] == pytest.approx(100 * 1.02 ** 4, abs=1e-3)
    assert chart["ETH"] == [100.0] * 5
    navs = [1000 + 500 * 1.02 ** i + 300 for i in range(5)]
    assert chart["portfolio"][-1] == pytest.approx(100 * navs[-1] / navs[0], abs=1e-3)
    btc = report["benchmarks"]["BTC"]
    assert btc["available"] and btc["return_pct"] == pytest.approx((1.02 ** 4 - 1) * 100, abs=1e-3)
    assert btc["excess_pct"] == pytest.approx(btc["portfolio_return_pct"] - btc["return_pct"], abs=1e-3)
    assert report["volatility"]["n_obs"] == 4 and report["volatility"]["sufficient"] is False
    assert report["flow_adjusted"] is False


def test_report_benchmark_never_priced_is_unavailable(tmp_path):
    database = Database(tmp_path / "h.db")
    for i in range(3):
        record(database, DAY0 + timedelta(days=i), [Holding("USDT", BINANCE_SPOT, 1000 + i, 1)])
    report = quant_report(database)
    assert report["benchmarks"]["BTC"]["available"] is False
    assert report["chart"]["BTC"] == [None, None, None]


def test_report_uses_wrapped_alias_for_benchmark_price(tmp_path):
    database = Database(tmp_path / "h.db")
    for i, price in enumerate((60_000, 66_000)):
        record(database, DAY0 + timedelta(days=i), [Holding("WBTC", ONCHAIN, 1, price, detail="ethereum/main")])
    assert quant_report(database)["benchmarks"]["BTC"]["return_pct"] == pytest.approx(10.0)


def test_report_twr_removes_synced_deposit_and_skips_degraded_close(tmp_path):
    database = Database(tmp_path / "h.db")
    record(database, DAY0, [Holding("USDT", BINANCE_SPOT, 1000, 1)])
    record(database, DAY0 + timedelta(days=1), [Holding("USDT", BINANCE_SPOT, 3000, 1)])
    # A failed source on day 2: the 0 recorded there must not become a close (nor a drawdown).
    record(database, DAY0 + timedelta(days=2), [],
           sources={BINANCE_SPOT: SourceStatus(STATUS_ERROR, "boom")})
    record(database, DAY0 + timedelta(days=3), [Holding("USDT", BINANCE_SPOT, 3300, 1)])
    upsert_flows(database, [CashFlow(timestamp=iso(DAY0 + timedelta(hours=12)), asset="USDT", direction="in",
                                     amount=2000, price_usd=1.0, flow_type="deposit", external_id="d1")])
    for key in ("binance.deposit", "binance.withdraw"):
        set_cursor(database, key, NOW_MS)
    report = quant_report(database)
    assert report["flow_adjusted"] is True and report["flow_count"] == 1
    assert report["n_obs"] == 2 and report["gap_obs"] == 1
    # day 1: (3000 - 1000 - 2000) / (1000 + 0.5 * 2000) = 0 ; day 3: +10%
    assert report["twr"]["twr_pct"] == pytest.approx(10.0)
    assert report["drawdown"]["max_drawdown_pct"] == 0
    assert report["volatility"]["excluded_obs"] == 1


def test_report_period_filters_and_empty_db(tmp_path):
    database = Database(tmp_path / "h.db")
    empty = quant_report(database)
    assert empty["available"] is False and empty["n_obs"] == 0 and empty["sufficient"] is False
    seed(database, days=10)
    week = quant_report(database, period="3d")
    assert week["start"] == (DAY0 + timedelta(days=6)).date().isoformat() and week["n_obs"] == 3
    with pytest.raises(ValueError):
        quant_report(database, period="0d")


# == endpoint ================================================================================
def make_client(tmp_path) -> TestClient:
    path = tmp_path / "config.yaml"
    path.write_text(f"settings:\n  db_path: {tmp_path / 'h.db'}\n", encoding="utf-8")
    cfg = load_config(path)
    app = web_api.create_app(path, fetcher=lambda *_a, **_k: pytest.fail("no network"))
    return TestClient(app), cfg


def test_endpoint_returns_quant_payload(tmp_path):
    client, cfg = make_client(tmp_path)
    seed(Database(cfg.db_path))
    response = client.get("/api/analytics/quant", params={"period": "all"})
    assert response.status_code == 200
    body = response.json()
    assert {"twr", "drawdown", "volatility", "benchmarks", "chart", "n_obs", "sufficient", "as_of",
            "period"} <= set(body)
    assert len(body["drawdown"]["underwater"]) == len(body["chart"]["dates"]) == 5
    assert client.get("/api/analytics/quant", params={"period": "bad"}).status_code == 422
    assert client.get("/api/analytics/quant", params={"period": "0d"}).status_code == 422
