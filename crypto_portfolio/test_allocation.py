"""M7.rebalance_toggle: asset allocation & rebalancing assistant (ROADMAP_LEDGER §4.5, §9.4).

Pure engine on plain data, config validation, property tests on random portfolios, and the
endpoint + ON/OFF switch on a temporary config/SQLite. No network.
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as config_module
import web_api
from config import (
    DEFAULT_ALLOCATION_BUCKETS,
    AllocationSettings,
    DryPowderRule,
    load_config,
    parse_allocation,
)
from models import BINANCE_EARN, BINANCE_FUNDING, BINANCE_SPOT, MANUAL, ONCHAIN, Holding
from services.analytics.allocation import (
    ILLIQUID,
    LOCKED_EARN,
    Lot,
    allocate,
    apply_liquidity,
    band_edge_deltas,
    contribution_deltas,
    finalize_deltas,
    funding_plan,
    lots_from_rows,
    proportional_deltas,
    split_dry_powder,
    target_deltas,
    turnover,
)
from services.db import Database
from services.portfolio import aggregate

STABLES = ["USDT", "USDC", "FDUSD"]


def settings(**raw) -> AllocationSettings:
    raw.setdefault("enabled", True)
    parsed, errors = parse_allocation(raw, STABLES)
    assert errors == [], errors
    return parsed


def spot(symbol: str, value: float, price: float = 1.0, source: str = BINANCE_SPOT,
         detail: str = "", lock: str | None = None) -> Lot:
    return Lot(symbol=symbol, source=source, value_usd=value, price_usd=price, detail=detail,
               lock_reason=lock)


# ROADMAP_LEDGER §4.5.2 example: NAV 50,770.12, BTC 41.2 %, Stablecoin 33.0 %
EXAMPLE = [
    spot("BTC", 20917.29, 90000.0),
    spot("ETH", 12000.0, 3000.0),
    spot("USDC", 16754.14),
    spot("SOL", 1098.69, 150.0, ONCHAIN, "solana/phantom"),
]


# -- config ------------------------------------------------------------------------------
def test_missing_block_is_off_with_default_targets():
    parsed, errors = parse_allocation(None, STABLES)
    assert errors == []
    assert parsed.enabled is False
    assert parsed.default_targets is True
    assert [(b.name, b.target) for b in parsed.buckets] == [
        (name, spec["target"]) for name, spec in DEFAULT_ALLOCATION_BUCKETS.items()]
    stable = next(b for b in parsed.buckets if b.stablecoins)
    assert set(stable.symbols) == set(STABLES)
    assert parsed.bucket_of("pepe") == "Altcoins"
    assert parsed.bucket_of("wbtc") == "BTC"


def test_load_config_defaults_allocation_off(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("settings: {min_value: 1}\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.allocation_settings.enabled is False
    assert cfg.allocation_errors == []
    path.write_text(yaml.safe_dump({"allocation": {"enabled": "yes", "tolerance_pp": 3}}), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.allocation_settings.enabled is True
    assert cfg.allocation_settings.tolerance_pp == 3


def test_validation_one_message_per_problem():
    raw = {
        "tolerance_pp": 50,
        "rebalance_to": "nearest",
        "buckets": {
            "BTC": {"target": 60, "symbols": ["BTC"]},
            "Coins": {"target": 30, "symbols": ["btc", "ETH"]},
            "Rest": {"target": 5, "symbols": "*"},
            "More": {"target": 10, "symbols": "*"},
        },
    }
    _, errors = parse_allocation(raw, STABLES)
    text = "\n".join(errors)
    assert "tolerance_pp" in text
    assert "rebalance_to" in text
    assert "BTC is already in bucket 'BTC'" in text
    assert "only one bucket may use '*'" in text
    assert "add up to 105" in text
    assert len(errors) == 5


@pytest.mark.parametrize("tolerance", [0, -1, 50, 75, "abc", None])
def test_tolerance_must_be_inside_zero_fifty(tolerance):
    _, errors = parse_allocation({"tolerance_pp": tolerance}, STABLES)
    assert any("tolerance_pp" in e for e in errors)


def test_targets_sum_within_a_hundredth():
    buckets = {"A": {"target": 33.333, "symbols": ["A"]}, "B": {"target": 33.333, "symbols": ["B"]},
               "C": {"target": 33.334, "symbols": "*"}}
    assert parse_allocation({"buckets": buckets}, STABLES)[1] == []
    buckets["C"]["target"] = 33.4
    assert parse_allocation({"buckets": buckets}, STABLES)[1]


def test_dry_powder_rules_are_validated():
    raw = {"dry_powder": [
        {"symbol": "usdc", "source": "binance_earn"},
        {"symbols": ["USDT"], "max_usd": 500},
        {"symbols": [], "sources": ["binance_spot"]},
        {"symbols": ["USDC"], "sources": ["kraken"]},
        {"symbols": ["USDC"], "max_usd": -1},
    ]}
    parsed, errors = parse_allocation(raw, STABLES)
    assert parsed.dry_powder == (DryPowderRule(("USDC",), ("binance_earn",), None),
                                 DryPowderRule(("USDT",), (), 500.0))
    assert len(errors) == 3


def test_not_a_mapping():
    parsed, errors = parse_allocation(["BTC"], STABLES)
    assert parsed.enabled is False and errors


# -- weights and drift -------------------------------------------------------------------
def test_reference_example_btc_under_stable_over():
    report = allocate(EXAMPLE, settings())
    rows = {row["name"]: row for row in report["buckets"]}
    assert report["total_priced_usd"] == pytest.approx(50770.12, abs=0.01)
    assert rows["BTC"]["actual_pct"] == pytest.approx(41.2, abs=0.01)
    assert rows["BTC"]["drift_pp"] == pytest.approx(-8.8, abs=0.01)
    assert rows["BTC"]["breach"] is True and rows["BTC"]["direction"] == "under"
    assert rows["Stablecoin"]["drift_pp"] == pytest.approx(18.0, abs=0.01)
    assert rows["Stablecoin"]["direction"] == "over"
    assert rows["ETH"]["breach"] is False
    assert rows["BTC"]["band"] == [45, 55]
    assert report["rebalance_needed"] is True

    buy_btc = next(a for a in report["actions"] if a["bucket"] == "BTC")
    assert buy_btc["action"] == "BUY" and buy_btc["asset"] == "BTC"
    assert buy_btc["usd"] == pytest.approx(0.088 * 50770.12, abs=1.0)
    assert buy_btc["est_qty"] == pytest.approx(buy_btc["usd"] / 90000, rel=1e-6)
    sell = next(a for a in report["actions"] if a["action"] == "SELL")
    assert sell["asset"] == "USDC" and sell["constrained_by"] is None
    assert sell["usd"] == pytest.approx(16754.14 - 0.15 * 50770.12, abs=0.02)
    assert sum(a["usd"] if a["action"] == "BUY" else -a["usd"] for a in report["actions"]) \
        == pytest.approx(0, abs=0.01)
    # fees on the non-cash side only: BTC, ETH, Altcoins buys
    traded = sum(a["usd"] for a in report["actions"] if a["bucket"] != "Stablecoin")
    assert report["est_fees_usd"] == pytest.approx(traded * 0.001, abs=0.01)
    assert all(row["after_pct"] == pytest.approx(row["target_pct"], abs=0.01) for row in report["buckets"])


def test_no_breach_no_actions():
    lots = [spot("BTC", 5100, 90000), spot("ETH", 2400, 3000), spot("USDT", 1500), spot("SOL", 1000, 150)]
    report = allocate(lots, settings())
    assert report["actions"] == []
    assert report["rebalance_needed"] is False
    assert all(not row["breach"] for row in report["buckets"])


def test_orders_below_min_trade_are_dropped_and_sum_stays_zero():
    lots = [spot("BTC", 4000, 90000), spot("ETH", 2510, 3000), spot("USDT", 2490), spot("SOL", 1000, 150)]
    report = allocate(lots, settings(min_trade_usd=50))
    buckets = {a["bucket"] for a in report["actions"]}
    assert "Altcoins" not in buckets  # exactly on target: no order
    assert "ETH" not in buckets       # $10 off: under the minimum
    assert all(a["usd"] >= 50 for a in report["actions"])
    assert sum(a["usd"] if a["action"] == "BUY" else -a["usd"] for a in report["actions"]) \
        == pytest.approx(0, abs=0.01)


def test_unpriced_and_unassigned_are_reported():
    lots, unpriced = lots_from_rows([
        {"symbol": "BTC", "source": BINANCE_SPOT, "quantity": 1, "price_usd": 100},
        {"symbol": "XYZ", "source": ONCHAIN, "quantity": 5, "price_usd": 0, "detail": "eth/main"},
        {"symbol": "ZERO", "source": ONCHAIN, "quantity": 0, "price_usd": 1},
    ])
    assert unpriced == 1 and len(lots) == 1
    no_catch_all = settings(buckets={"BTC": {"target": 100, "symbols": ["BTC"]}})
    report = allocate(lots + [spot("DOGE", 50, 0.1)], no_catch_all, unpriced_assets=unpriced)
    assert report["unassigned"] == {"usd": 50.0, "symbols": ["DOGE"]}
    assert report["total_priced_usd"] == 100
    assert report["unpriced_assets"] == 1


def test_empty_portfolio_is_not_available():
    report = allocate([], settings())
    assert report["available"] is False and report["reason"] == "no_priced_assets"
    assert report["actions"] == []


# -- band edge ---------------------------------------------------------------------------
def test_band_edge_pulls_breaching_buckets_to_the_edge():
    values = {"A": 40.0, "B": 28.0, "C": 32.0}
    targets = {"A": 50.0, "B": 30.0, "C": 20.0}
    deltas = band_edge_deltas(values, targets, 5)
    assert deltas["A"] == pytest.approx(5.0)    # 40 -> 45 (lower edge)
    assert deltas["C"] == pytest.approx(-7.0)   # 32 -> 25 (upper edge)
    assert deltas["B"] == pytest.approx(2.0)    # in band, covers the gap
    assert sum(deltas.values()) == pytest.approx(0)
    assert turnover(deltas) < turnover(target_deltas(values, targets))


def test_band_edge_without_inband_room_falls_back_toward_target():
    values = {"A": 30.0, "B": 30.0, "C": 40.0}  # A, B under by 10; C over by 20; no in-band bucket
    targets = {"A": 40.0, "B": 40.0, "C": 20.0}
    deltas = band_edge_deltas(values, targets, 5)
    assert sum(deltas.values()) == pytest.approx(0)
    assert turnover(deltas) <= turnover(target_deltas(values, targets)) + 1e-9
    assert deltas["A"] > 0 and deltas["B"] > 0 and deltas["C"] < 0


def random_portfolio(rng: random.Random) -> tuple[dict, dict]:
    n = rng.randint(2, 6)
    names = [f"B{i}" for i in range(n)]
    cuts = sorted(rng.uniform(0, 100) for _ in range(n - 1))
    weights = [b - a for a, b in zip([0, *cuts], [*cuts, 100])]
    targets = dict(zip(names, weights))
    values = {name: rng.choice([0.0, rng.uniform(0, 50000)]) for name in names}
    if sum(values.values()) == 0:
        values[names[0]] = 1000.0
    return values, targets


def test_property_band_edge_turnover_never_exceeds_target():
    rng = random.Random(20261125)
    for _ in range(100):
        values, targets = random_portfolio(rng)
        tol = rng.uniform(0.5, 15)
        band = band_edge_deltas(values, targets, tol)
        full = target_deltas(values, targets)
        assert sum(band.values()) == pytest.approx(0, abs=0.01)
        assert turnover(band) <= turnover(full) + 0.01


def test_property_finalized_orders_sum_to_zero():
    rng = random.Random(7)
    for _ in range(100):
        values, targets = random_portfolio(rng)
        deltas = finalize_deltas(target_deltas(values, targets), rng.choice([0, 20, 100, 500]))
        assert sum(deltas.values()) == pytest.approx(0, abs=0.01)


# -- liquidity ---------------------------------------------------------------------------
def test_sell_capped_at_liquid_value_locked_earn():
    """M7.3 AC1: must sell $5,000 but only $3,000 is liquid -> $3,000, constrained_by locked_earn."""
    values = {"BTC": 3000.0 + 7000.0, "Cash": 0.0}
    deltas, cut, unfilled = apply_liquidity({"BTC": -5000.0, "Cash": 5000.0}, values,
                                            {"BTC": 50.0, "Cash": 50.0}, {"BTC": 3000.0, "Cash": 0.0})
    assert deltas["BTC"] == pytest.approx(-3000)
    assert cut == {"BTC": pytest.approx(2000)}
    assert unfilled == pytest.approx(2000)
    assert deltas["Cash"] == pytest.approx(3000)  # buys shrink so the orders stay self-funded

    lots = [spot("BTC", 3000, 100), spot("BTC", 7000, 100, BINANCE_EARN, "locked", LOCKED_EARN)]
    two = settings(buckets={"BTC": {"target": 50, "symbols": ["BTC"]},
                            "Stablecoin": {"target": 50, "symbols": "@stablecoins"}})
    report = allocate(lots, two)
    sell = next(a for a in report["actions"] if a["action"] == "SELL")
    assert sell["usd"] == pytest.approx(3000) and sell["constrained_by"] == LOCKED_EARN
    assert report["unfilled_usd"] == pytest.approx(2000)
    row = next(r for r in report["buckets"] if r["name"] == "BTC")
    assert row["locked_usd"] == 7000 and row["locked_reasons"] == [LOCKED_EARN]


def test_shortfall_moves_to_another_overweight_bucket():
    values = {"A": 600.0, "B": 300.0, "C": 100.0}
    targets = {"A": 40.0, "B": 20.0, "C": 40.0}
    raw = {"A": -200.0, "B": -20.0, "C": 220.0}  # band-edge style: B is still 80 above target
    deltas, cut, unfilled = apply_liquidity(raw, values, targets, {"A": 50.0, "B": 300.0, "C": 100.0})
    assert deltas["A"] == pytest.approx(-50)
    assert cut == {"A": pytest.approx(150)}
    assert deltas["B"] == pytest.approx(-100)   # gives its 80 left above target, not more
    assert unfilled == pytest.approx(70)        # the rest cannot be sold anywhere
    assert deltas["C"] == pytest.approx(150)    # so the buy shrinks by it
    assert sum(deltas.values()) == pytest.approx(0)

    # in target mode every over-weight bucket already sells down to its target: nothing to move
    deltas, _, unfilled = apply_liquidity(target_deltas(values, targets), values, targets,
                                          {"A": 50.0, "B": 300.0, "C": 100.0})
    assert deltas["B"] == pytest.approx(-100) and unfilled == pytest.approx(150)


def test_sell_order_follows_source_priority_and_flags_cold_storage():
    """M7.3 AC2/AC3: Spot -> Earn flexible -> on-chain -> cold; cold legs need a transfer."""
    lots = [
        spot("BTC", 1000, 100000, MANUAL, "Ledger Nano X"),
        spot("BTC", 1000, 100000, ONCHAIN, "ethereum/main"),
        spot("BTC", 1000, 100000, BINANCE_EARN, "flexible"),
        spot("BTC", 1000, 100000),
        spot("BTC", 1000, 100000, BINANCE_FUNDING),
        spot("BTC", 9000, 100000, BINANCE_EARN, "locked", LOCKED_EARN),
    ]
    two = settings(buckets={"BTC": {"target": 50, "symbols": ["BTC"]},
                            "Stablecoin": {"target": 50, "symbols": "@stablecoins"}},
                   min_trade_usd=0)
    report = allocate(lots + [spot("USDT", 4000)], two)
    sell = next(a for a in report["actions"] if a["action"] == "SELL")
    assert [leg["source"] for leg in sell["legs"]] == [BINANCE_SPOT, BINANCE_EARN, BINANCE_FUNDING,
                                                       ONCHAIN, MANUAL]
    assert sell["usd"] == 5000  # wanted 5000 of 14000: 5000 liquid, exactly enough
    cold = sell["legs"][-1]
    assert cold["requires_transfer"] is True and cold["from"] == "Ledger Nano X"
    assert sell["legs"][1]["requires_redeem"] is True
    assert sell["requires_transfer"] is True and "Ledger Nano X" in sell["from"]
    assert sell["est_qty"] == pytest.approx(0.05)
    assert "Ledger Nano X" in sell["text"] and "transfer" in sell["text"]


def test_manual_illiquid_holding_cannot_be_sold():
    asset = config_module.ManualAsset("Kiln", "staking", (config_module.ManualHolding("ETH", 2.0, liquid=False),))
    lots, _ = lots_from_rows([
        {"symbol": "ETH", "source": MANUAL, "quantity": 2, "price_usd": 3000, "detail": "Kiln"},
        {"symbol": "ETH", "source": BINANCE_EARN, "quantity": 1, "price_usd": 3000, "detail": "locked"},
        {"symbol": "USDT", "source": BINANCE_SPOT, "quantity": 100, "price_usd": 1},
    ], [asset])
    reasons = {(lot.source, lot.lock_reason) for lot in lots}
    assert (MANUAL, ILLIQUID) in reasons and (BINANCE_EARN, LOCKED_EARN) in reasons
    two = settings(buckets={"ETH": {"target": 10, "symbols": ["ETH"]},
                            "Stablecoin": {"target": 90, "symbols": "@stablecoins"}})
    report = allocate([lot for lot in lots if lot.source == MANUAL] + [lots[-1]], two)
    assert all(a["action"] != "SELL" for a in report["actions"])  # nothing liquid to sell
    assert report["unfilled_usd"] > 0


# -- dry powder --------------------------------------------------------------------------
def test_usdc_in_earn_is_dry_powder_not_a_drift():
    """Jean keeps USDC in Earn on purpose to buy dips: no Stablecoin over-weight, no SELL."""
    lots = [spot("BTC", 5000, 90000), spot("ETH", 2500, 3000), spot("USDT", 1500), spot("SOL", 1000, 150),
            spot("USDC", 20000, 1, BINANCE_EARN, "flexible")]
    with_rule = settings(dry_powder=[{"symbols": ["USDC"], "sources": ["binance_earn"]}])
    report = allocate(lots, with_rule)
    assert report["rebalance_needed"] is False and report["actions"] == []
    assert report["total_priced_usd"] == 10000
    assert report["portfolio_priced_usd"] == 30000
    assert report["dry_powder"]["usd"] == 20000
    assert report["dry_powder"]["items"] == [{"symbol": "USDC", "source": BINANCE_EARN, "usd": 20000}]
    assert report["dry_powder"]["pct_of_portfolio"] == pytest.approx(66.6667, abs=1e-3)

    counted = allocate(lots, with_rule, include_dry_powder=True)
    assert counted["rebalance_needed"] is True
    assert counted["dry_powder"]["included"] is True
    assert any(a["action"] == "SELL" and a["bucket"] == "Stablecoin" for a in counted["actions"])

    without_rule = allocate(lots, settings())
    assert without_rule["rebalance_needed"] is True


def test_dry_powder_cap_and_source_filter():
    lots = [spot("USDC", 800), spot("USDC", 500, 1, BINANCE_EARN, "flexible"), spot("USDT", 300)]
    kept, dry = split_dry_powder(lots, (DryPowderRule(("USDC",), (), 1000.0),))
    assert sum(lot.value_usd for lot in dry) == pytest.approx(1000)
    assert sum(lot.value_usd for lot in kept) == pytest.approx(600)
    kept, dry = split_dry_powder(lots, (DryPowderRule(("USDC",), ("binance_earn",)),))
    assert [(lot.source, lot.value_usd) for lot in dry] == [(BINANCE_EARN, 500)]


# -- contribution ------------------------------------------------------------------------
def test_contribution_buys_only_and_sums_exactly():
    """M7.5 AC1: Σ = contribution, no SELL, deepest deficit first."""
    report = allocate(EXAMPLE, settings(), contribution=1000)
    assert report["mode"] == "contribution" and report["contribution_usd"] == 1000
    assert all(a["action"] == "BUY" for a in report["actions"])
    assert sum(a["usd"] for a in report["actions"]) == pytest.approx(1000, abs=1e-9)
    first = report["actions"][0]
    assert first["bucket"] == "BTC"  # most negative drift gets money first
    assert "Stablecoin" not in {a["bucket"] for a in report["actions"]}


def test_contribution_small_amount_goes_to_the_deepest_deficit():
    deltas = contribution_deltas({"A": 400.0, "B": 290.0, "C": 310.0},
                                 {"A": 50.0, "B": 30.0, "C": 20.0}, 50.0)
    assert deltas == {"A": pytest.approx(50.0), "B": 0.0, "C": 0.0}


def test_contribution_respects_min_trade_but_keeps_the_sum():
    values = {"A": 450.0, "B": 295.0, "C": 255.0}
    targets = {"A": 50.0, "B": 30.0, "C": 20.0}
    deltas = contribution_deltas(values, targets, 200.0, min_trade_usd=60)
    assert sum(deltas.values()) == pytest.approx(200)
    assert all(d == 0 or d >= 60 for d in deltas.values())


def test_contribution_on_empty_portfolio_splits_by_target():
    deltas = contribution_deltas({"A": 0.0, "B": 0.0}, {"A": 70.0, "B": 30.0}, 1000.0)
    assert deltas == {"A": pytest.approx(700), "B": pytest.approx(300)}


def test_property_contribution_never_increases_total_drift():
    """M7.5 AC2: after the simulated buys, Σ|drift| <= before."""
    rng = random.Random(42)
    for _ in range(100):
        values, targets = random_portfolio(rng)
        amount = rng.uniform(1, 30000)
        deltas = contribution_deltas(values, targets, amount)
        assert all(d >= -1e-9 for d in deltas.values())
        assert sum(deltas.values()) == pytest.approx(amount, rel=1e-9)
        before_total = sum(values.values())
        after_total = before_total + amount
        before = sum(abs(values[n] / before_total * 100 - targets[n]) for n in values)
        after = sum(abs((values[n] + deltas[n]) / after_total * 100 - targets[n]) for n in values)
        assert after <= before + 1e-6


def test_property_contribution_report_cents_add_up():
    rng = random.Random(3)
    for _ in range(50):
        amount = round(rng.uniform(1, 5000), 2)
        report = allocate(EXAMPLE, settings(min_trade_usd=rng.choice([0, 20, 200])), contribution=amount)
        assert round(sum(a["usd"] for a in report["actions"]), 2) == pytest.approx(amount, abs=0.001)
        assert all(a["action"] == "BUY" for a in report["actions"])


# -- partial funding (DCA) ---------------------------------------------------------------
# V = 26,000: BTC misses 5,000, ETH 2,500, Altcoins 600 (Σ 8,100); Stablecoin is 8,100 over.
DCA = [
    spot("BTC", 8000, 100000),
    spot("ETH", 4000, 4000),
    spot("USDC", 12000),
    spot("SOL", 2000, 200),
]


def signed(actions: list[dict]) -> float:
    return sum(a["usd"] if a["action"] == "BUY" else -a["usd"] for a in actions)


def by_bucket(report: dict) -> dict[str, dict]:
    return {a["bucket"]: a for a in report["actions"]}


@pytest.mark.parametrize("strategy", ["greedy", "proportional"])
def test_funding_is_self_funded_one_sell_in_the_source(strategy):
    report = allocate(DCA, settings(), source_bucket="Stablecoin", funding_amount=2000,
                      strategy=strategy)
    assert report["mode"] == "funding" and report["contribution_usd"] is None
    sells = [a for a in report["actions"] if a["action"] == "SELL"]
    buys = [a for a in report["actions"] if a["action"] == "BUY"]
    assert [a["bucket"] for a in sells] == ["Stablecoin"]
    assert "Stablecoin" not in {a["bucket"] for a in buys}
    assert sum(a["usd"] for a in buys) == pytest.approx(2000, abs=1e-9)
    assert sells[0]["usd"] == pytest.approx(2000, abs=1e-9)
    assert signed(report["actions"]) == pytest.approx(0, abs=1e-9)
    funding = report["funding"]
    assert funding["source_bucket"] == "Stablecoin" and funding["strategy"] == strategy
    assert funding["requested_usd"] == 2000 and funding["applied_usd"] == 2000
    assert funding["limited_by"] is None and funding["source_below_target"] is False
    assert funding["source_excess_usd"] == pytest.approx(8100)
    assert funding["progress_pct"] == pytest.approx(24.7)          # 2,000 / 8,100
    assert funding["remaining_batches_estimate"] == pytest.approx(4.0)  # 8,100 / 2,000 = 4.05
    assert report["total_priced_usd"] == 26000  # V unchanged


def test_funding_strategies_differ_on_the_same_input():
    greedy = allocate(DCA, settings(), source_bucket="Stablecoin", funding_amount=2000)
    prop = allocate(DCA, settings(), source_bucket="Stablecoin", funding_amount=2000,
                    strategy="proportional")
    assert greedy["funding"]["strategy"] == "greedy"  # the default
    # greedy: BTC is 19.2 pp under, ETH 9.6 pp: all 2,000 go to BTC before ETH gets anything
    assert {b: a["usd"] for b, a in by_bucket(greedy).items() if a["action"] == "BUY"} == {"BTC": 2000}
    # proportional: shares of the 8,100 total deficit (5,000 / 2,500 / 600)
    buys = {b: a["usd"] for b, a in by_bucket(prop).items() if a["action"] == "BUY"}
    assert buys == {"BTC": pytest.approx(1234.57, abs=0.01), "ETH": pytest.approx(617.28, abs=0.01),
                    "Altcoins": pytest.approx(148.15, abs=0.01)}
    assert sum(buys.values()) == pytest.approx(2000, abs=1e-9)


def test_proportional_and_greedy_helpers_exclude_the_source():
    values = {"A": 100.0, "B": 300.0, "S": 600.0}
    targets = {"A": 40.0, "B": 40.0, "S": 20.0}
    assert proportional_deltas(values, targets, 200, exclude=("S",)) == {
        "A": pytest.approx(150), "B": pytest.approx(50), "S": 0.0}
    greedy = contribution_deltas(values, targets, 200, exclude=("S",))
    assert greedy["S"] == 0.0 and sum(greedy.values()) == pytest.approx(200)
    assert contribution_deltas(values, targets, 200, exclude=("A", "B", "S")) == {
        "A": 0.0, "B": 0.0, "S": 0.0}


def test_property_funding_balanced_and_source_never_bought():
    rng = random.Random(2026)
    for _ in range(200):
        values, targets = random_portfolio(rng)
        source = rng.choice(list(values))
        liquid = {n: v * rng.choice([1.0, 0.5, 0.0]) for n, v in values.items()}
        amount = rng.uniform(1, 40000)
        strategy = rng.choice(["greedy", "proportional"])
        deltas, info = funding_plan(values, targets, liquid, source, amount, strategy,
                                    rng.choice([0, 20, 200]))
        assert all(d > 0 for n, d in deltas.items() if n != source)
        assert sum(deltas.values()) == pytest.approx(0, abs=1e-6)
        assert info["applied_usd"] <= round(amount, 2) + 1e-9
        assert info["applied_usd"] <= liquid[source] + 1e-9
        assert -deltas.get(source, 0.0) == pytest.approx(info["applied_usd"], abs=1e-9)
        if info["applied_usd"] < round(amount, 2) - 0.01:
            assert info["limited_by"] in ("liquidity", "room")


def test_funding_capped_by_source_liquidity():
    lots = [spot("BTC", 8000, 100000), spot("ETH", 4000, 4000), spot("SOL", 2000, 200),
            spot("USDC", 1500), spot("USDC", 10500, 1, BINANCE_EARN, "locked", LOCKED_EARN)]
    report = allocate(lots, settings(), source_bucket="Stablecoin", funding_amount=2000)
    funding = report["funding"]
    assert funding["limited_by"] == "liquidity"
    assert funding["requested_usd"] == 2000 and funding["applied_usd"] == 1500
    sell = by_bucket(report)["Stablecoin"]
    assert sell["usd"] == 1500 and sell["constrained_by"] == LOCKED_EARN
    assert [leg["source"] for leg in sell["legs"]] == [BINANCE_SPOT]  # never the locked Earn
    assert signed(report["actions"]) == pytest.approx(0, abs=1e-9)


def test_funding_capped_by_room_and_no_forced_buys():
    report = allocate(DCA, settings(), source_bucket="Stablecoin", funding_amount=10000)
    funding = report["funding"]
    assert funding["limited_by"] == "room"
    assert funding["applied_usd"] == pytest.approx(8100)
    rows = {row["name"]: row for row in report["buckets"]}
    for name in ("BTC", "ETH", "Altcoins", "Stablecoin"):  # every bucket lands on its target
        assert rows[name]["after_pct"] == pytest.approx(rows[name]["target_pct"], abs=0.01)
    assert funding["progress_pct"] == pytest.approx(100)

    on_target = [spot("BTC", 13000, 100000), spot("ETH", 6500, 4000), spot("USDC", 3900),
                 spot("SOL", 2600, 200)]  # nothing misses anything: nothing to buy, nothing sold
    empty = allocate(on_target, settings(), source_bucket="Stablecoin", funding_amount=500)
    assert empty["funding"]["limited_by"] == "room"
    assert empty["funding"]["applied_usd"] == 0 and empty["actions"] == []


def test_funding_flags_a_source_falling_under_its_target():
    # Stablecoin is only 1,100 over its 3,900 target; BTC misses 3,000
    lots = [spot("BTC", 10000, 100000), spot("ETH", 8000, 4000), spot("USDC", 5000),
            spot("SOL", 3000, 200)]
    report = allocate(lots, settings(), source_bucket="Stablecoin", funding_amount=2000)
    funding = report["funding"]
    assert funding["applied_usd"] == 2000 and funding["limited_by"] is None
    assert funding["source_below_target"] is True
    assert funding["source_excess_usd"] == pytest.approx(1100)
    assert funding["remaining_batches_estimate"] == pytest.approx(0.6)
    ok = allocate(lots, settings(), source_bucket="Stablecoin", funding_amount=1000)
    assert ok["funding"]["source_below_target"] is False


@pytest.mark.parametrize("strategy", ["greedy", "proportional"])
def test_funding_respects_min_trade(strategy):
    report = allocate(DCA, settings(min_trade_usd=200), source_bucket="Stablecoin",
                      funding_amount=2000, strategy=strategy)
    buys = [a for a in report["actions"] if a["action"] == "BUY"]
    assert all(a["usd"] >= 200 for a in buys)
    assert sum(a["usd"] for a in buys) == pytest.approx(2000, abs=1e-9)
    assert signed(report["actions"]) == pytest.approx(0, abs=1e-9)
    if strategy == "proportional":  # Altcoins' 148.15 share is dropped and shared again
        assert {a["bucket"] for a in buys} == {"BTC", "ETH"}
        assert by_bucket(report)["BTC"]["usd"] == pytest.approx(1333.33, abs=0.01)


def test_funding_from_a_non_stablecoin_bucket():
    # ETH is 5,500 over its 6,500 target; Stablecoin is above target too: BTC and Altcoins buy
    lots = [spot("BTC", 8000, 100000), spot("ETH", 12000, 4000), spot("USDC", 4000),
            spot("SOL", 2000, 200)]
    report = allocate(lots, settings(), source_bucket="ETH", funding_amount=3000,
                      strategy="proportional")
    sell = by_bucket(report)["ETH"]
    assert sell["action"] == "SELL" and sell["asset"] == "ETH"
    assert sell["est_qty"] == pytest.approx(0.75) and "quote_spend" not in sell
    assert sell["text"].startswith("SELL")
    buys = {a["bucket"] for a in report["actions"] if a["action"] == "BUY"}
    assert buys == {"BTC", "Altcoins"}  # Stablecoin has no deficit, ETH is the source
    assert report["funding"]["source_is_stablecoin"] is False
    # two real trades (sell ETH, buy BTC/SOL): fees on both sides
    assert report["est_fees_usd"] == pytest.approx(6000 * 0.001, abs=0.01)


def test_funding_from_stablecoin_is_worded_as_spending_it():
    report = allocate(DCA, settings(), source_bucket="Stablecoin", funding_amount=2000)
    spend = by_bucket(report)["Stablecoin"]
    assert spend["action"] == "SELL" and spend["quote_spend"] is True
    assert spend["text"].startswith("USE ~2,000.00 USDC (~$2,000.00) · Stablecoin")
    assert report["funding"]["source_is_stablecoin"] is True
    assert report["est_fees_usd"] == pytest.approx(2000 * 0.001, abs=0.01)  # one trade, not two


def test_funding_after_pct_is_measured_on_the_unchanged_total():
    report = allocate(DCA, settings(), source_bucket="Stablecoin", funding_amount=2000)
    rows = {row["name"]: row for row in report["buckets"]}
    assert rows["Stablecoin"]["after_pct"] == pytest.approx(10000 / 26000 * 100, abs=1e-3)
    assert rows["BTC"]["after_pct"] == pytest.approx(10000 / 26000 * 100, abs=1e-3)
    assert rows["ETH"]["after_pct"] == pytest.approx(rows["ETH"]["actual_pct"], abs=1e-9)
    assert sum(row["after_pct"] for row in report["buckets"]) == pytest.approx(100, abs=1e-3)
    assert rows["Stablecoin"]["order_usd"] == -2000 and rows["BTC"]["order_usd"] == 2000


def test_funding_runs_even_when_nothing_breaches():
    lots = [spot("BTC", 5100, 90000), spot("ETH", 2400, 3000), spot("USDT", 1500), spot("SOL", 1000, 150)]
    report = allocate(lots, settings(), source_bucket="Stablecoin", funding_amount=100)
    assert report["rebalance_needed"] is False
    assert {(a["bucket"], a["action"], a["usd"]) for a in report["actions"]} == {
        ("ETH", "BUY", 100), ("Stablecoin", "SELL", 100)}


def test_funding_structural_errors_raise():
    with pytest.raises(ValueError, match="Unknown source bucket"):
        allocate(DCA, settings(), source_bucket="Gold", funding_amount=100)
    for bad in (0, -5, float("nan")):
        with pytest.raises(ValueError, match="above 0"):
            allocate(DCA, settings(), source_bucket="Stablecoin", funding_amount=bad)
    with pytest.raises(ValueError, match="not both"):
        allocate(DCA, settings(), contribution=100, source_bucket="Stablecoin", funding_amount=100)
    with pytest.raises(ValueError, match="needs both"):
        allocate(DCA, settings(), source_bucket="Stablecoin")
    with pytest.raises(ValueError, match="strategy"):
        allocate(DCA, settings(), source_bucket="Stablecoin", funding_amount=100, strategy="random")


def test_other_modes_report_no_funding_block():
    assert allocate(DCA, settings())["funding"] is None
    assert allocate(DCA, settings(), contribution=500)["funding"] is None


# -- endpoint + switch -------------------------------------------------------------------
def make_client(tmp_path, allocation=None, holdings=None, **sections) -> tuple[TestClient, Path]:
    path = tmp_path / "config.yaml"
    raw = {"settings": {"db_path": str(tmp_path / "history.db"), "stablecoins": STABLES}, **sections}
    if allocation is not None:
        raw["allocation"] = allocation
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    if holdings is not None:
        portfolio = aggregate(holdings)
        portfolio.timestamp = datetime(2026, 11, 25, 12, tzinfo=timezone.utc).isoformat()
        Database(tmp_path / "history.db").record(portfolio)
    return TestClient(web_api.create_app(path)), path


HOLDINGS = [
    Holding("BTC", BINANCE_SPOT, 0.2324143, 90000.0),
    Holding("ETH", BINANCE_SPOT, 4.0, 3000.0),
    Holding("USDC", BINANCE_SPOT, 16754.14, 1.0),
    Holding("USDC", BINANCE_EARN, 10000.0, 1.0, detail="flexible"),
    Holding("SOL", ONCHAIN, 7.3246, 150.0, detail="solana/phantom"),
]


def test_endpoint_is_off_by_default(tmp_path):
    client, _ = make_client(tmp_path, holdings=HOLDINGS)
    data = client.get("/api/analytics/allocation").json()
    assert data["enabled"] is False and data["reason"] == "disabled"
    assert data["actions"] == [] and data["default_targets"] is True
    assert client.get("/api/settings").json()["allocation"]["enabled"] is False


def test_toggle_on_and_off_keeps_the_other_keys(tmp_path):
    client, path = make_client(tmp_path, allocation={"tolerance_pp": 4, "rebalance_to": "band_edge"},
                               holdings=HOLDINGS)
    response = client.post("/api/settings/allocation", json={"enabled": True})
    assert response.status_code == 200 and response.json()["enabled"] is True
    stored = yaml.safe_load(path.read_text(encoding="utf-8"))["allocation"]
    assert stored == {"tolerance_pp": 4, "rebalance_to": "band_edge", "enabled": True}
    data = client.get("/api/analytics/allocation").json()
    assert data["enabled"] is True and data["available"] is True
    assert data["rebalance_to"] == "band_edge" and data["tolerance_pp"] == 4
    assert data["as_of"].startswith("2026-11-25") and data["n_obs"] == 5

    assert client.post("/api/settings/allocation", json={"enabled": False}).json()["enabled"] is False
    assert client.get("/api/analytics/allocation").json()["reason"] == "disabled"
    assert client.post("/api/settings/allocation", json={"enabled": "maybe"}).status_code == 422


def test_endpoint_reference_payload_and_dry_powder(tmp_path):
    allocation = {"enabled": True, "dry_powder": [{"symbols": ["USDC"], "sources": ["binance_earn"]}]}
    client, _ = make_client(tmp_path, allocation=allocation, holdings=HOLDINGS)
    data = client.get("/api/analytics/allocation").json()
    rows = {row["name"]: row for row in data["buckets"]}
    assert data["total_priced_usd"] == pytest.approx(50770.12, abs=0.05)
    assert rows["BTC"]["drift_pp"] == pytest.approx(-8.8, abs=0.01)
    assert data["dry_powder"]["usd"] == 10000
    assert not any(leg["source"] == BINANCE_EARN for a in data["actions"] for leg in a["legs"])
    included = client.get("/api/analytics/allocation?include_dry_powder=true").json()
    assert included["total_priced_usd"] == pytest.approx(60770.12, abs=0.05)


def test_endpoint_contribution_mode_and_validation(tmp_path):
    client, _ = make_client(tmp_path, allocation={"enabled": True}, holdings=HOLDINGS)
    data = client.get("/api/analytics/allocation?contribution=1000").json()
    assert data["mode"] == "contribution"
    assert all(a["action"] == "BUY" for a in data["actions"])
    assert sum(a["usd"] for a in data["actions"]) == pytest.approx(1000, abs=0.001)
    for bad in ("0", "-5", "abc", "inf", "nan"):
        assert client.get(f"/api/analytics/allocation?contribution={bad}").status_code == 422, bad


def test_endpoint_funding_mode(tmp_path):
    client, _ = make_client(tmp_path, allocation={"enabled": True}, holdings=HOLDINGS)
    url = "/api/analytics/allocation?source_bucket=Stablecoin&funding_amount_usd=5000"
    data = client.get(url).json()
    assert data["mode"] == "funding" and data["funding"]["strategy"] == "greedy"
    assert data["funding"]["applied_usd"] == 5000
    assert signed(data["actions"]) == pytest.approx(0, abs=0.001)
    assert [a["bucket"] for a in data["actions"] if a["action"] == "SELL"] == ["Stablecoin"]
    prop = client.get(url + "&strategy=proportional").json()
    assert prop["funding"]["strategy"] == "proportional"
    assert len([a for a in prop["actions"] if a["action"] == "BUY"]) > \
        len([a for a in data["actions"] if a["action"] == "BUY"])


def test_endpoint_funding_validation(tmp_path):
    client, _ = make_client(tmp_path, allocation={"enabled": True}, holdings=HOLDINGS)
    base = "/api/analytics/allocation?"
    both = client.get(base + "contribution=100&source_bucket=Stablecoin&funding_amount_usd=100")
    assert both.status_code == 422 and "not both" in both.json()["detail"]["message"]
    assert client.get(base + "contribution=100&source_bucket=Stablecoin").status_code == 422
    unknown = client.get(base + "source_bucket=Gold&funding_amount_usd=100")
    assert unknown.status_code == 422 and "Unknown source bucket" in unknown.json()["detail"]["message"]
    assert client.get(base + "source_bucket=Stablecoin").status_code == 422
    assert client.get(base + "funding_amount_usd=100").status_code == 422
    for bad in ("0", "-5", "abc", "inf"):
        assert client.get(base + f"source_bucket=Stablecoin&funding_amount_usd={bad}").status_code == 422
    assert client.get(base + "source_bucket=Stablecoin&funding_amount_usd=100&strategy=x").status_code == 422


def test_endpoint_invalid_targets_is_422_and_dashboard_still_runs(tmp_path):
    allocation = {"enabled": True, "buckets": {"BTC": {"target": 70, "symbols": ["BTC"]},
                                               "Rest": {"target": 20, "symbols": "*"}}}
    client, _ = make_client(tmp_path, allocation=allocation, holdings=HOLDINGS)
    response = client.get("/api/analytics/allocation")
    assert response.status_code == 422
    assert any("add up to 90" in e for e in response.json()["detail"]["errors"])
    assert client.get("/api/portfolio").status_code == 200
    assert client.get("/api/settings").json()["allocation"]["errors"]


def test_endpoint_without_snapshot(tmp_path):
    client, _ = make_client(tmp_path, allocation={"enabled": True})
    data = client.get("/api/analytics/allocation").json()
    assert data["available"] is False and data["reason"] == "no_snapshot"


def test_endpoint_needs_the_token(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_WEB_TOKEN", "tok")
    client, _ = make_client(tmp_path, allocation={"enabled": True}, holdings=HOLDINGS)
    assert client.get("/api/analytics/allocation").status_code == 401
    assert client.post("/api/settings/allocation", json={"enabled": False}).status_code == 401
    ok = client.get("/api/analytics/allocation", headers={"Authorization": "Bearer tok"})
    assert ok.status_code == 200
