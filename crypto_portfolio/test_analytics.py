"""Tests for services/analytics/attribution.py and GET /api/portfolio/attribution (Epic F1).

    .venv/bin/pytest test_analytics.py -q

The engine tests build `SnapshotState`s by hand (no database); the loader and endpoint tests
write snapshots to a temp SQLite file. Nothing touches the network.
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as config_module
import web_api
from models import (
    BINANCE_EARN,
    BINANCE_FUNDING,
    BINANCE_SPOT,
    ONCHAIN,
    STATUS_ERROR,
    STATUS_INFERRED_ERROR,
    STATUS_OK,
    Holding,
    SourceStatus,
)
from services.analytics import attribution as attr
from services.analytics.attribution import (
    BUCKETS,
    Position,
    SnapshotState,
    attribute,
    attribution_report,
    decompose,
    load_states,
    parse_window,
    position_key,
    window_snapshot_ids,
)
from services.db import Database
from services.portfolio import aggregate

ARB = "onchain:arbitrum/main"
ETH_MAIN = "onchain:ethereum/main"
BASE = "onchain:base/main"


# -- helpers -----------------------------------------------------------------------
def state(snapshot_id: int, positions: dict, degraded=(), nav: float | None = None,
          timestamp: str | None = None) -> SnapshotState:
    """`positions`: {(symbol, source_key): (quantity, price)}; NAV defaults to the sum of values."""
    built = {key: Position(q, q * p) for key, (q, p) in positions.items()}
    return SnapshotState(
        snapshot_id=snapshot_id,
        timestamp=timestamp or f"2026-11-25T{snapshot_id:02d}:00:00+00:00",
        nav=sum(p.value for p in built.values()) if nav is None else nav,
        positions=built,
        degraded=frozenset(degraded),
    )


def row(result, key: str) -> dict:
    return next(r for r in result.contributors if r["key"] == key)


def assert_identity(result) -> None:
    assert result.identity_error is None
    assert sum(result.buckets.values()) == pytest.approx(result.delta_usd, abs=1e-6)
    assert sum(r["total"] for r in result.contributors) == pytest.approx(result.delta_usd, abs=1e-6)


# -- one-step decomposition (§2.2.1) ---------------------------------------------------
def test_attribution_manual_case():
    """AC2: Q0=2, P0=100 -> Q1=3, P1=110: price 20, quantity 100, interaction 10, dV 130."""
    result = attribute([state(1, {("BTC", BINANCE_SPOT): (2, 100)}),
                        state(2, {("BTC", BINANCE_SPOT): (3, 110)})])
    btc = row(result, "BTC")
    assert btc["price"] == pytest.approx(20)
    assert btc["quantity"] == pytest.approx(100)
    assert btc["interaction"] == pytest.approx(10)
    assert btc["total"] == pytest.approx(130)
    assert result.delta_usd == pytest.approx(130)
    assert result.buckets["revaluation"] == 0 and result.buckets["data_gap"] == 0
    assert btc["price_change_pct"] == pytest.approx(10)
    assert_identity(result)


def test_attribution_identity_property():
    """AC1: on 300 random histories the buckets always add up to dNAV, both methods, all groupings."""
    rng = random.Random(20261125)
    symbols = ["BTC", "ETH", "USDT", "SOL", "PEPE", "NEW"]
    sources = [BINANCE_SPOT, BINANCE_EARN, BINANCE_FUNDING, ETH_MAIN, ARB, ONCHAIN]
    for case in range(300):
        states = []
        for index in range(rng.randint(2, 6)):
            positions = {}
            for symbol in symbols:
                for source in sources:
                    roll = rng.random()
                    if roll < 0.45:
                        continue  # absent: covers coins appearing and disappearing
                    price = 0.0 if roll < 0.5 else rng.uniform(0.0001, 70000)  # P=0 data gaps
                    positions[(symbol, source)] = (rng.uniform(0, 50), price)
            degraded = rng.sample(sources + ["unknown"], rng.choice([0, 0, 0, 1, 2]))
            states.append(state(index + 1, positions, degraded))
        for method in attr.METHODS:
            for group_by in attr.GROUP_BY:
                result = attribute(states, method, group_by)
                assert result.identity_error is None, (case, method, group_by)
                assert sum(result.buckets.values()) == pytest.approx(
                    states[-1].nav - states[0].nav, abs=1e-6 * max(1, states[0].nav, states[-1].nav))


def test_transfer_between_sources_with_different_prices_has_no_price_effect():
    """AC3: 1 ETH from Binance (ticker $3000) to a wallet (DefiLlama $3000.50) is no market move."""
    before = state(1, {("ETH", BINANCE_SPOT): (2, 3000.0), ("ETH", ETH_MAIN): (1, 3000.5)})
    after = state(2, {("ETH", BINANCE_SPOT): (1, 3000.0), ("ETH", ETH_MAIN): (2, 3000.5)})
    result = attribute([before, after])
    eth = row(result, "ETH")
    assert eth["price"] == pytest.approx(0)
    assert eth["interaction"] == pytest.approx(0)
    assert eth["quantity"] == pytest.approx(0.5)  # the price gap between the feeds, nothing more
    # at source grain the move shows as two opposite quantity effects
    by_source = attribute([before, after], group_by="source")
    assert row(by_source, BINANCE_SPOT)["quantity"] == pytest.approx(-3000.0)
    assert row(by_source, ONCHAIN)["quantity"] == pytest.approx(3000.5)


def test_spot_to_earn_transfer_cancels_at_symbol_grain():
    before = state(1, {("USDT", BINANCE_SPOT): (500, 1.0), ("USDT", BINANCE_EARN): (1000, 1.0)})
    after = state(2, {("USDT", BINANCE_SPOT): (0, 1.0), ("USDT", BINANCE_EARN): (1500, 1.0)})
    result = attribute([before, after])
    assert row(result, "USDT")["total"] == pytest.approx(0)
    assert result.buckets["price"] == pytest.approx(0)


# -- edge cases (§2.2.4), AC4 ------------------------------------------------------------
def test_new_coin_is_all_quantity():
    result = attribute([state(1, {("BTC", BINANCE_SPOT): (1, 60000)}),
                        state(2, {("BTC", BINANCE_SPOT): (1, 60000), ("SOL", BINANCE_SPOT): (10, 150)})])
    sol = row(result, "SOL")
    assert sol["quantity"] == pytest.approx(1500)
    assert sol["price"] == 0 and sol["interaction"] == 0 and sol["revaluation"] == 0
    assert sol["weight_start"] == 0 and sol["price_change_pct"] is None
    assert_identity(result)


def test_disappeared_coin_is_all_quantity():
    result = attribute([state(1, {("BTC", BINANCE_SPOT): (1, 60000), ("SOL", BINANCE_SPOT): (10, 150)}),
                        state(2, {("BTC", BINANCE_SPOT): (1, 60000), ("SOL", BINANCE_SPOT): (0, 160)})])
    sol = row(result, "SOL")
    assert sol["quantity"] == pytest.approx(-1500)
    assert sol["price"] == 0 and sol["interaction"] == 0
    assert_identity(result)


def test_price_appearing_from_zero_is_revaluation_not_market():
    """P0 = 0 < P1: DefiLlama just priced the token. Units already held -> revaluation."""
    result = attribute([state(1, {("XYZ", ETH_MAIN): (100, 0.0)}),
                        state(2, {("XYZ", ETH_MAIN): (120, 2.0)})])
    xyz = row(result, "XYZ")
    assert xyz["price"] == 0
    assert xyz["revaluation"] == pytest.approx(200)  # 100 units already held, valued at 2
    assert xyz["quantity"] == pytest.approx(40)      # 20 new units at the new price
    assert result.buckets["revaluation"] == pytest.approx(200)
    assert_identity(result)


def test_price_dropping_to_zero_is_revaluation_not_market():
    """P0 > 0 = P1 while the source reported ok: the lost value is not a crash of the coin."""
    result = attribute([state(1, {("XYZ", ETH_MAIN): (100, 2.0)}),
                        state(2, {("XYZ", ETH_MAIN): (100, 0.0)})])
    xyz = row(result, "XYZ")
    assert xyz["price"] == 0 and xyz["interaction"] == 0
    assert xyz["revaluation"] == pytest.approx(-200)
    assert xyz["quantity"] == pytest.approx(0)
    assert_identity(result)


def test_unpriced_on_both_sides_moves_nothing():
    result = attribute([state(1, {("XYZ", ETH_MAIN): (100, 0.0)}),
                        state(2, {("XYZ", ETH_MAIN): (300, 0.0)})])
    assert row(result, "XYZ")["total"] == 0
    assert_identity(result)


def test_decompose_buckets_always_sum_to_the_value_change():
    for before, after in [(None, Position(2, 20)), (Position(2, 20), None), (Position(2, 0), Position(3, 9)),
                          (Position(2, 8), Position(2, 0)), (Position(0, 0), Position(0, 0)),
                          (Position(1.5, 3), Position(4, 17))]:
        split = decompose(before, after)
        expected = (after.value if after else 0) - (before.value if before else 0)
        assert sum(split.values()) == pytest.approx(expected)


def test_stablecoin_pinned_at_one_is_all_quantity_including_earn_interest():
    result = attribute([state(1, {("USDC", BINANCE_EARN): (10000, 1.0)}),
                        state(2, {("USDC", BINANCE_EARN): (10001.37, 1.0)})])
    usdc = row(result, "USDC")
    assert usdc["price"] == 0
    assert usdc["quantity"] == pytest.approx(1.37)
    assert usdc["interaction"] == 0


def test_dust_is_still_attributed():
    result = attribute([state(1, {("DUST", BINANCE_SPOT): (1000, 0.0001), ("BTC", BINANCE_SPOT): (1, 100)}),
                        state(2, {("DUST", BINANCE_SPOT): (1000, 0.0002), ("BTC", BINANCE_SPOT): (1, 100)})])
    assert row(result, "DUST")["price"] == pytest.approx(0.1)
    assert_identity(result)


# -- data_gap (F1.4) ----------------------------------------------------------------------
def test_failed_wallet_source_goes_to_data_gap_not_price():
    """AC F1.4: `onchain:arbitrum/main = error` creates no fake negative price effect for ETH."""
    full = {("ETH", ETH_MAIN): (1, 3000), ("ETH", ARB): (2, 3000), ("BTC", BINANCE_SPOT): (1, 60000)}
    hole = {("ETH", ETH_MAIN): (1, 3000), ("BTC", BINANCE_SPOT): (1, 60000)}  # Arbitrum RPC died
    states = [state(1, full), state(2, hole, degraded=[ARB]), state(3, full)]
    result = attribute(states)
    eth = row(result, "ETH")
    assert eth["price"] == 0 and eth["quantity"] == 0
    assert result.degraded_pairs == 2
    # the hole and its recovery cancel inside data_gap
    assert result.buckets["data_gap"] == pytest.approx(0)
    assert_identity(result)

    # a window ending inside the hole: the loss is a data gap, not a sale or a crash
    partial = attribute(states[:2])
    assert row(partial, "ETH")["data_gap"] == pytest.approx(-6000)
    assert row(partial, "ETH")["price"] == 0 and row(partial, "ETH")["quantity"] == 0
    assert partial.buckets["price"] == 0
    assert_identity(partial)


def test_failure_in_the_earlier_snapshot_also_degrades_the_pair():
    states = [state(1, {("BTC", BINANCE_EARN): (0, 0)}, degraded=[BINANCE_EARN]),
              state(2, {("BTC", BINANCE_EARN): (1, 60000)})]
    result = attribute(states)
    assert row(result, "BTC")["data_gap"] == pytest.approx(60000)
    assert row(result, "BTC")["quantity"] == 0


def test_whole_onchain_inferred_error_covers_every_wallet():
    """Old snapshots get `inferred_error` on `onchain` as a whole: every wallet is degraded."""
    before = state(1, {("ETH", ARB): (2, 3000), ("ETH", BINANCE_SPOT): (1, 3000)})
    after = state(2, {("ETH", BINANCE_SPOT): (1, 3100)}, degraded=[ONCHAIN])
    result = attribute([before, after])
    eth = row(result, "ETH")
    assert eth["data_gap"] == pytest.approx(-6000)
    assert eth["price"] == pytest.approx(100)  # Binance is healthy: its move stays a price effect


def test_onchain_rows_without_wallet_are_degraded_by_any_wallet_failure():
    """Rows written before P0.3 have no `detail`: any wallet failure may be theirs."""
    before = state(1, {("ETH", ONCHAIN): (3, 3000)})
    after = state(2, {("ETH", ONCHAIN): (1, 3000)}, degraded=[BASE])
    assert row(attribute([before, after]), "ETH")["data_gap"] == pytest.approx(-6000)
    # ...while a Binance failure does not touch on-chain rows
    after_ok = state(2, {("ETH", ONCHAIN): (1, 3000)}, degraded=[BINANCE_SPOT, "unknown"])
    result = attribute([before, after_ok])
    assert row(result, "ETH")["quantity"] == pytest.approx(-6000)
    assert result.degraded_pairs == 0


# -- chained vs endpoint (F1.3) ----------------------------------------------------------
def test_chained_puts_a_mid_window_buy_in_price_endpoint_in_interaction():
    """AC F1.3: buy 1 BTC mid-window, then BTC +5%."""
    states = [state(1, {("BTC", BINANCE_SPOT): (1, 100.0)}),
              state(2, {("BTC", BINANCE_SPOT): (2, 100.0)}),   # bought 1 BTC at 100
              state(3, {("BTC", BINANCE_SPOT): (2, 105.0)})]   # +5%
    chained = attribute(states, method="chained")
    endpoint = attribute(states, method="endpoint")
    assert row(chained, "BTC")["price"] == pytest.approx(10)        # 2 BTC x +5
    assert row(chained, "BTC")["interaction"] == pytest.approx(0)
    assert row(endpoint, "BTC")["price"] == pytest.approx(5)        # only the BTC held at the start
    assert row(endpoint, "BTC")["interaction"] == pytest.approx(5)  # the bought BTC's gain
    assert chained.snapshots == endpoint.snapshots == 3
    assert_identity(chained)
    assert_identity(endpoint)


def test_attribute_validates_its_arguments():
    one = state(1, {("BTC", BINANCE_SPOT): (1, 1)})
    with pytest.raises(ValueError):
        attribute([one])
    with pytest.raises(ValueError):
        attribute([one, one], method="twr")
    with pytest.raises(ValueError):
        attribute([one, one], group_by="chain")


def test_identity_error_is_reported_not_hidden():
    """A stored NAV that disagrees with the asset rows is flagged in the result."""
    result = attribute([state(1, {("BTC", BINANCE_SPOT): (1, 100)}),
                        state(2, {("BTC", BINANCE_SPOT): (1, 110)}, nav=150)])
    assert result.identity_error is not None
    assert result.identity_error["difference_usd"] == pytest.approx(-40)


def test_contributors_are_sorted_and_share_the_gross_move():
    result = attribute([state(1, {("BTC", BINANCE_SPOT): (1, 100), ("ETH", BINANCE_SPOT): (1, 100)}),
                        state(2, {("BTC", BINANCE_SPOT): (1, 130), ("ETH", BINANCE_SPOT): (1, 90)})])
    assert [r["key"] for r in result.contributors] == ["BTC", "ETH"]
    assert row(result, "BTC")["share_of_move"] == pytest.approx(0.75)
    assert row(result, "ETH")["share_of_move"] == pytest.approx(-0.25)
    assert row(result, "BTC")["weight_start"] == pytest.approx(0.5)
    symbol_source = attribute(
        [state(1, {("BTC", BINANCE_SPOT): (1, 100)}), state(2, {("BTC", BINANCE_SPOT): (1, 90)})],
        group_by="symbol_source")
    assert symbol_source.contributors[0]["key"] == "BTC@binance_spot"


# -- window parsing ------------------------------------------------------------------------
@pytest.mark.parametrize("window, hours", [("24h", 24), ("1h", 1), ("7d", 168), ("365d", 8760), ("999h", 999)])
def test_parse_window(window, hours):
    assert parse_window(window) == hours


@pytest.mark.parametrize("window", ["0h", "0d", "366d", "999d", "24", "1w", "-1h", "1000h", "", "24 h"])
def test_parse_window_rejects(window):
    with pytest.raises(ValueError):
        parse_window(window)


# -- loader (F1.1) ----------------------------------------------------------------------
def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def record(database: Database, when: datetime, holdings: list[Holding], statuses=None) -> int:
    portfolio = aggregate(holdings)
    portfolio.timestamp = iso(when)
    if statuses is not None:
        portfolio.sources = statuses
    return database.record(portfolio)


def test_load_states_groups_rows_per_symbol_and_source_key(tmp_path):
    database = Database(tmp_path / "h.db")
    now = datetime.now(timezone.utc)
    snapshot_id = record(database, now, [
        Holding("BTC", BINANCE_EARN, 0.1, 60000, detail="flexible"),
        Holding("BTC", BINANCE_EARN, 0.3, 60000, detail="locked"),
        Holding("ETH", ONCHAIN, 1.0, 3000.0, detail="ethereum/main"),
        Holding("ETH", ONCHAIN, 0.5, 3001.0, detail="arbitrum/main"),
        Holding("ETH", ONCHAIN, 0.25, 2999.0, detail="base/main"),
        Holding("ETH", ONCHAIN, 1.0, 3000.0, detail="base/main"),  # two rows of one wallet merge
    ], statuses={BINANCE_EARN: SourceStatus(STATUS_OK), ARB: SourceStatus(STATUS_ERROR, "503")})

    [loaded] = load_states(database, [snapshot_id])
    assert loaded.snapshot_id == snapshot_id
    btc = loaded.positions[("BTC", BINANCE_EARN)]
    assert btc.quantity == pytest.approx(0.4) and btc.price == pytest.approx(60000)
    base = loaded.positions[("ETH", BASE)]
    assert base.quantity == pytest.approx(1.25)
    assert base.price == pytest.approx((0.25 * 2999 + 3000) / 1.25)  # P = V / Q
    assert set(loaded.positions) == {("BTC", BINANCE_EARN), ("ETH", ETH_MAIN), ("ETH", ARB), ("ETH", BASE)}
    assert loaded.degraded == {ARB}
    assert loaded.nav == pytest.approx(0.4 * 60000 + 3000 + 0.5 * 3001 + 0.25 * 2999 + 3000)


def test_load_states_merges_legacy_onchain_rows_without_detail(tmp_path):
    database = Database(tmp_path / "h.db")
    snapshot_id = record(database, datetime.now(timezone.utc), [
        Holding("ETH", ONCHAIN, 1.0, 3000.0), Holding("ETH", ONCHAIN, 2.0, 3003.0),
        Holding("ETH", ONCHAIN, 1.0, 2997.0)])
    [loaded] = load_states(database, [snapshot_id])
    eth = loaded.positions[position_key("ETH", ONCHAIN)]
    assert eth.quantity == pytest.approx(4.0)
    assert eth.price == pytest.approx((3000 + 6006 + 2997) / 4)


def test_load_states_keeps_order_skips_unknown_ids_and_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(attr, "_SQL_CHUNK", 2)  # force several IN (...) batches
    database = Database(tmp_path / "h.db")
    now = datetime.now(timezone.utc)
    ids = [record(database, now - timedelta(hours=5 - i), [Holding("BTC", BINANCE_SPOT, 1, 100 + i)])
           for i in range(5)]
    loaded = load_states(database, [ids[3], ids[0], 999, ids[4], ids[1], ids[2], ids[0]])
    assert [s.snapshot_id for s in loaded] == [ids[3], ids[0], ids[4], ids[1], ids[2]]
    assert [s.positions[("BTC", BINANCE_SPOT)].price for s in loaded] == [103, 100, 104, 101, 102]
    assert load_states(database, []) == []


def test_load_states_marks_inferred_errors_as_degraded(tmp_path):
    database = Database(tmp_path / "h.db")
    snapshot_id = record(database, datetime.now(timezone.utc), [Holding("BTC", BINANCE_SPOT, 1, 1)],
                         statuses={ONCHAIN: SourceStatus(STATUS_INFERRED_ERROR, "hole"),
                                   BINANCE_SPOT: SourceStatus(STATUS_OK)})
    assert load_states(database, [snapshot_id])[0].degraded == {ONCHAIN}


def test_window_snapshot_ids(tmp_path):
    database = Database(tmp_path / "h.db")
    now = datetime.now(timezone.utc)
    btc = [Holding("BTC", BINANCE_SPOT, 1, 100)]
    assert window_snapshot_ids(database, 24) == []
    old = record(database, now - timedelta(hours=30), btc)
    ref = record(database, now - timedelta(hours=25), btc)
    mid = record(database, now - timedelta(hours=10), btc)
    last = record(database, now, btc)
    assert window_snapshot_ids(database, 24) == [ref, mid, last]
    assert window_snapshot_ids(database, 24, "endpoint") == [ref, last]
    assert window_snapshot_ids(database, 365 * 24) == [old, ref, mid, last]  # short history: oldest
    assert window_snapshot_ids(database, 1) == [mid, last]


def test_window_snapshot_ids_picks_the_snapshot_closest_to_the_cutoff(tmp_path):
    """A hole in the history: the snapshot before the cutoff is days away, the one after is close."""
    database = Database(tmp_path / "h.db")
    now = datetime.now(timezone.utc)
    btc = [Holding("BTC", BINANCE_SPOT, 1, 100)]
    record(database, now - timedelta(days=10), btc)
    near = record(database, now - timedelta(hours=23), btc)     # 1h from the cutoff, 9 days closer
    mid = record(database, now - timedelta(hours=5), btc)
    last = record(database, now, btc)
    assert window_snapshot_ids(database, 24) == [near, mid, last]
    assert window_snapshot_ids(database, 24, "endpoint") == [near, last]


def test_window_snapshot_ids_keeps_the_earlier_snapshot_when_it_is_closer_or_tied(tmp_path):
    database = Database(tmp_path / "h.db")
    now = datetime.now(timezone.utc)
    btc = [Holding("BTC", BINANCE_SPOT, 1, 100)]
    before = record(database, now - timedelta(hours=26), btc)   # 2h before the cutoff
    after = record(database, now - timedelta(hours=22), btc)    # 2h after: a tie goes to the full window
    last = record(database, now, btc)
    assert window_snapshot_ids(database, 24) == [before, after, last]
    assert window_snapshot_ids(database, 23) == [after, last]


def test_window_snapshot_ids_never_anchors_on_the_latest_snapshot(tmp_path):
    """Only the newest snapshot sits after the cutoff: fall back to the one before, however far."""
    database = Database(tmp_path / "h.db")
    now = datetime.now(timezone.utc)
    btc = [Holding("BTC", BINANCE_SPOT, 1, 100)]
    old = record(database, now - timedelta(days=10), btc)
    last = record(database, now, btc)
    assert window_snapshot_ids(database, 24) == [old, last]


@pytest.mark.parametrize("actual, requested, expected", [
    (24, 24, "full"), (21, 24, "full"), (27, 24, "full"),
    (20, 24, "partial"), (6, 24, "partial"), (84, 168, "partial"),
    (28, 24, "stretched"), (240, 24, "stretched"),
])
def test_coverage_of(actual, requested, expected):
    assert attr.coverage_of(actual, requested) == expected


# -- report and endpoint (F1.5) ------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: False)
    for name in (*config_module.ENV_OVERRIDES, "PORTFOLIO_WEB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def exploding_fetcher(*_args, **_kwargs):
    raise AssertionError("the attribution endpoint must never fetch live data")


def make_client(tmp_path, settings=None) -> TestClient:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"settings": {"db_path": str(tmp_path / "history.db"), **(settings or {})}}),
                   encoding="utf-8")
    return TestClient(web_api.create_app(cfg, fetcher=exploding_fetcher))


def seed_day(db_path: Path) -> Database:
    """Five hourly-ish snapshots over 24h: BTC falls, ETH rises, SOL is bought, USDC earns."""
    database = Database(db_path)
    now = datetime.now(timezone.utc)
    path = [
        (60000, 3000, 0, 10000.00),
        (59500, 3010, 0, 10000.20),
        (59000, 3020, 10, 10000.40),
        (58800, 3030, 10, 10000.60),
        (58500, 3050, 10, 10000.80),
    ]
    for index, (btc, eth, sol_qty, usdc) in enumerate(path):
        holdings = [
            Holding("BTC", BINANCE_SPOT, 0.5, btc),
            Holding("ETH", ONCHAIN, 2.0, eth, detail="ethereum/main"),
            Holding("USDC", BINANCE_EARN, usdc, 1.0, detail="flexible"),
            Holding("DUST", BINANCE_SPOT, 10, 0.001),
        ]
        if sol_qty:
            holdings.append(Holding("SOL", BINANCE_SPOT, sol_qty, 150.0))
        record(database, now - timedelta(hours=24 - 6 * index), holdings)
    return database


def test_attribution_endpoint_schema(tmp_path):
    seed_day(tmp_path / "history.db")
    response = make_client(tmp_path).get("/api/portfolio/attribution")
    assert response.status_code == 200
    data = response.json()
    for key in ("window", "from", "to", "hours", "snapshots", "method", "group_by", "nav_start", "nav_end",
                "delta_usd", "delta_pct", "buckets", "contributors", "top_gainers", "top_losers", "others",
                "degraded_pairs", "identity_error"):
        assert key in data, key
    assert data["window"] == "24h" and data["method"] == "chained" and data["group_by"] == "symbol"
    assert data["insufficient_history"] is False
    assert data["snapshots"] == 5
    assert data["hours"] == pytest.approx(24, abs=0.05)
    assert set(data["buckets"]) == set(BUCKETS)
    assert sum(data["buckets"].values()) == pytest.approx(data["delta_usd"], abs=0.03)
    assert data["delta_usd"] == pytest.approx(data["nav_end"] - data["nav_start"], abs=0.01)
    assert data["identity_error"] is None and data["degraded_pairs"] == 0

    by_key = {c["key"]: c for c in data["contributors"]}
    assert by_key["BTC"]["price"] == pytest.approx(-750)          # 0.5 x (58500 - 60000)
    assert by_key["ETH"]["price"] == pytest.approx(100)           # 2 x (3050 - 3000)
    assert by_key["SOL"]["quantity"] == pytest.approx(1500) and by_key["SOL"]["price"] == 0
    assert by_key["USDC"]["quantity"] == pytest.approx(0.8) and by_key["USDC"]["price"] == 0
    assert by_key["BTC"]["price_change_pct"] == pytest.approx(-2.5)
    assert data["top_gainers"] == ["SOL", "ETH", "USDC"]
    assert data["top_losers"] == ["BTC"]
    assert [g["key"] for g in data["gainers"]] == data["top_gainers"]
    assert data["others"] == {"count": 0, "total": 0.0}  # DUST did not move


def test_attribution_endpoint_top_and_others(tmp_path):
    seed_day(tmp_path / "history.db")
    data = make_client(tmp_path).get("/api/portfolio/attribution?top=1").json()
    assert data["top_gainers"] == ["SOL"] and data["top_losers"] == ["BTC"]
    assert data["others"]["count"] == 2  # ETH, USDC
    assert data["others"]["total"] == pytest.approx(100.8, abs=0.01)


def test_attribution_endpoint_methods_and_groupings(tmp_path):
    seed_day(tmp_path / "history.db")
    client = make_client(tmp_path)
    endpoint = client.get("/api/portfolio/attribution?method=endpoint").json()
    assert endpoint["snapshots"] == 2 and endpoint["method"] == "endpoint"
    by_source = client.get("/api/portfolio/attribution?group_by=source").json()
    assert {c["key"] for c in by_source["contributors"]} == {BINANCE_SPOT, BINANCE_EARN, ONCHAIN}
    assert all(c["price_change_pct"] is None for c in by_source["contributors"])
    week = client.get("/api/portfolio/attribution?window=7d").json()
    assert week["window"] == "7d" and week["snapshots"] == 5  # history shorter than the window


@pytest.mark.parametrize("query", ["window=999d", "window=366d", "window=0h", "window=1w", "window=24",
                                   "group_by=chain", "method=twr", "top=0", "top=21", "top=x"])
def test_attribution_endpoint_rejects_bad_parameters(tmp_path, query):
    assert make_client(tmp_path).get(f"/api/portfolio/attribution?{query}").status_code == 422


def test_attribution_endpoint_with_insufficient_history(tmp_path):
    client = make_client(tmp_path)
    empty = client.get("/api/portfolio/attribution").json()
    assert empty["insufficient_history"] is True and empty["snapshots"] == 0
    record(Database(tmp_path / "history.db"), datetime.now(timezone.utc), [Holding("BTC", BINANCE_SPOT, 1, 1)])
    single = client.get("/api/portfolio/attribution").json()
    assert single["insufficient_history"] is True and single["snapshots"] == 1


def test_attribution_endpoint_ignores_backfilled_checkpoints(tmp_path):
    database = Database(tmp_path / "history.db")
    old = aggregate([Holding("BTC", BINANCE_SPOT, 1, 10000)])
    old.timestamp = iso(datetime.now(timezone.utc) - timedelta(days=60))
    database.replace_backfill([old])
    record(database, datetime.now(timezone.utc), [Holding("BTC", BINANCE_SPOT, 1, 60000)])
    data = make_client(tmp_path).get("/api/portfolio/attribution?window=365d").json()
    assert data["insufficient_history"] is True


def test_attribution_endpoint_reports_data_gaps(tmp_path):
    database = Database(tmp_path / "history.db")
    now = datetime.now(timezone.utc)
    wallets = lambda arb: [Holding("ETH", ONCHAIN, 1, 3000, detail="ethereum/main")] + (
        [Holding("ETH", ONCHAIN, 2, 3000, detail="arbitrum/main")] if arb else [])
    ok = {ETH_MAIN: SourceStatus(STATUS_OK), ARB: SourceStatus(STATUS_OK)}
    record(database, now - timedelta(hours=2), wallets(True), ok)
    record(database, now, wallets(False), {ETH_MAIN: SourceStatus(STATUS_OK),
                                           ARB: SourceStatus(STATUS_ERROR, "arbitrum/main: HTTP 503")})
    data = make_client(tmp_path).get("/api/portfolio/attribution").json()
    assert data["degraded_pairs"] == 1
    assert data["buckets"]["data_gap"] == pytest.approx(-6000)
    assert data["buckets"]["price"] == 0 and data["buckets"]["quantity"] == 0


def test_attribution_endpoint_is_behind_the_token(tmp_path):
    seed_day(tmp_path / "history.db")
    client = make_client(tmp_path, settings={"web_token": "letmein"})
    assert client.get("/api/portfolio/attribution").status_code == 401
    ok = client.get("/api/portfolio/attribution", headers={"Authorization": "Bearer letmein"})
    assert ok.status_code == 200 and ok.json()["snapshots"] == 5


def test_change_24h_advertises_attribution(tmp_path):
    seed_day(tmp_path / "history.db")
    change = make_client(tmp_path).get("/api/portfolio").json()["change_24h"]
    assert change["has_attribution"] is True


def test_attribution_report_validates_arguments(tmp_path):
    database = Database(tmp_path / "h.db")
    for kwargs in ({"window": "0h"}, {"method": "twr"}, {"group_by": "chain"}):
        with pytest.raises(ValueError):
            attribution_report(database, **kwargs)


def gap_report(tmp_path, offsets_hours, window="24h") -> dict:
    database = Database(tmp_path / "history.db")
    now = datetime.now(timezone.utc)
    for index, offset in enumerate(offsets_hours):
        record(database, now - timedelta(hours=offset), [Holding("BTC", BINANCE_SPOT, 1, 100 + index)])
    return make_client(tmp_path).get(f"/api/portfolio/attribution?window={window}").json()


def test_attribution_endpoint_reports_full_coverage(tmp_path):
    seed_day(tmp_path / "history.db")
    data = make_client(tmp_path).get("/api/portfolio/attribution").json()
    assert data["requested_hours"] == 24 and data["coverage"] == "full"
    assert data["history_start"] == data["from"]


def test_attribution_endpoint_short_history_is_partial(tmp_path):
    data = gap_report(tmp_path, [84, 40, 0], window="7d")       # tracking started 3.5 days ago
    assert data["requested_hours"] == 168 and data["coverage"] == "partial"
    assert data["hours"] == pytest.approx(84, abs=0.05)
    assert data["history_start"] == data["from"]


def test_attribution_endpoint_gap_picks_the_closest_snapshot(tmp_path):
    data = gap_report(tmp_path, [240, 23, 5, 0])                # nothing between 10 days and 23h ago
    assert data["hours"] == pytest.approx(23, abs=0.05) and data["coverage"] == "full"
    assert data["snapshots"] == 3 and data["history_start"] != data["from"]


def test_attribution_endpoint_gap_inside_the_window_is_partial(tmp_path):
    data = gap_report(tmp_path, [240, 12, 0])                   # 12h is closer to 24h ago than 10 days
    assert data["hours"] == pytest.approx(12, abs=0.05) and data["coverage"] == "partial"
    assert data["history_start"] != data["from"]


def test_attribution_endpoint_stretched_when_only_older_data_exists(tmp_path):
    data = gap_report(tmp_path, [36, 0])                        # the only reference is 36h old
    assert data["hours"] == pytest.approx(36, abs=0.05) and data["coverage"] == "stretched"
    assert data["requested_hours"] == 24


def test_change_24h_and_attribution_agree_across_a_gap(tmp_path):
    """The Portfolio chip and the Analytics 24H figure must describe the same window."""
    database = Database(tmp_path / "history.db")
    now = datetime.now(timezone.utc)
    for index, offset in enumerate([240, 23, 5, 0]):
        record(database, now - timedelta(hours=offset), [Holding("BTC", BINANCE_SPOT, 1, 100 + index)])
    client = make_client(tmp_path)
    change = client.get("/api/portfolio").json()["change_24h"]
    data = client.get("/api/portfolio/attribution").json()
    assert change["since"] == data["from"]
    assert change["usd"] == data["delta_usd"] == 2
    assert change["hours"] == pytest.approx(data["hours"], abs=0.01)
    assert change["requested_hours"] == 24 and change["coverage"] == data["coverage"] == "full"
