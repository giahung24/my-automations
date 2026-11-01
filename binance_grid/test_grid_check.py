import numpy as np
import pandas as pd
import pytest

from grid_check import adx, decide, efficiency_ratio, net_profit_per_grid, report

# Worked example of SPEC.md
EXAMPLE = {"pair_vs_sma200": 0.08, "btc_vs_sma200": 0.12, "sma50_slope_20d": 0.02, "adx14": 18,
           "efficiency_ratio_30d": 0.25, "vol30_ann": 0.55, "vol_ratio_30_180": 0.9,
           "atr14_daily_pct": 0.032, "atr14_4h_pct": 0.011, "range30": [2750, 3250], "range30_position": 0.5}
ARGS = {"price": 3000, "fee": 0.001, "days": 21, "capital": 1000, "min_notional": 5}


def candles(close):
    c = pd.Series(close, dtype=float)
    return pd.DataFrame({"high": c + 1, "low": c - 1, "close": c})


def test_adx_steady_uptrend_is_max():
    assert adx(candles(np.arange(100, 200))).iloc[-1] == pytest.approx(100)


def test_adx_zigzag_is_low():
    assert adx(candles([100, 102] * 50)).iloc[-1] < 10


def test_efficiency_ratio():
    assert efficiency_ratio(pd.Series(np.arange(31.0))) == pytest.approx(1)
    assert efficiency_ratio(pd.Series([1.0, 2.0] * 15 + [1.0])) == pytest.approx(0)
    assert efficiency_ratio(pd.Series([0.0, 2, 1, 3]), n=3) == pytest.approx(3 / 5)


def test_net_profit_per_grid_binance_example():
    # Binance FAQ: 400-450, 5 grids, 0.1% fee -> about 2.18% per grid
    assert net_profit_per_grid(400, 450, 5, 0.001) == pytest.approx(0.0218, abs=5e-5)


def test_worked_example():
    r = report(EXAMPLE, **ARGS)
    p = r["params"]
    assert r["verdict"] == "GO"
    assert p["lower"] == pytest.approx(2461.5, rel=1e-3)
    assert p["upper"] == pytest.approx(3656.5, rel=1e-3)
    assert p["grids"] == 36
    assert p["net_profit_per_grid"] == pytest.approx(0.009, abs=1e-4)
    assert p["investment_usdc"] == 1000
    assert p["stop_loss"] == pytest.approx(2383, rel=1e-3)
    assert r["worst_case_loss_pct"] == pytest.approx(-0.16, abs=0.005)
    assert r["warnings"] == []


def test_grid_count_never_breaks_fee_floor():
    # ATR 4h under the 0.6% floor; round() alone would give 41 grids at 0.59% spacing -> NO-GO
    r = report({**EXAMPLE, "atr14_4h_pct": 0.003, "atr14_daily_pct": 0.02, "vol30_ann": 0.338}, **ARGS)
    p = r["params"]
    assert r["verdict"] == "GO"
    assert p["grids"] == 40
    assert (p["upper"] / p["lower"]) ** (1 / p["grids"]) - 1 >= 0.006


def test_caution_halves_capital_and_widens_range():
    r = report({**EXAMPLE, "adx14": 25}, **ARGS)
    assert r["verdict"] == "CAUTION"
    assert r["params"]["investment_usdc"] == 500
    assert r["params"]["lower"] < report(EXAMPLE, **ARGS)["params"]["lower"]


@pytest.mark.parametrize("override, verdict, reason", [
    ({}, "GO", "ADX<22"),
    ({"btc_vs_sma200": -0.01, "adx14": 40}, "NO-GO", "downtrend"),  # first matching rule wins
    ({"pair_vs_sma200": -0.01, "sma50_slope_20d": -0.01}, "NO-GO", "downtrend"),
    ({"pair_vs_sma200": -0.01}, "GO", "ADX<22"),  # below SMA200 but SMA50 not falling
    ({"efficiency_ratio_30d": 0.6}, "NO-GO", "strong trend"),
    ({"vol_ratio_30_180": 1.6}, "NO-GO", "volatility"),
    ({"range30_position": 0.9}, "CAUTION", "not met: price mid-range"),
])
def test_decide(override, verdict, reason):
    v, why = decide({**EXAMPLE, **override})
    assert v == verdict and why.startswith(reason)


def test_no_go_has_no_params():
    r = report({**EXAMPLE, "adx14": 35}, **ARGS)
    assert r["verdict"] == "NO-GO" and r["params"] is None and r["worst_case_loss_pct"] is None
