"""N4: trade ledger, cost basis, and real trades in the reconciliation. Offline."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import CashFlowSettings
from models import BINANCE_EARN, BINANCE_FUNDING, BINANCE_SPOT, ONCHAIN, Holding, Portfolio, SourceStatus
from services.activity import activity_report
from services.analytics.cost_basis import Position, cost_basis_report, replay
from services.cash_flows import IN, OUT, REWARD, CashFlow, upsert_flows
from services.db import Database
from services.reconcile import inferred_items
from services.trades import (
    Trade,
    candidate_pairs,
    parse_convert,
    parse_spot,
    sync_trades,
    upsert_trades,
)

T0 = datetime(2026, 11, 25, 10, tzinfo=timezone.utc)
STABLE = {"USDC", "USDT"}
PRICES = {"USDC": 1.0, "USDT": 1.0, "BTC": 90_000.0, "SOL": 150.0, "LINK": 14.0, "ETH": 3000.0}


def at(hours: float) -> str:
    return (T0 + timedelta(hours=hours)).isoformat()


def ms(hours: float) -> int:
    return int((T0 + timedelta(hours=hours)).timestamp() * 1000)


def snap(database, hour, spot=None, earn=None, onchain=None, apr=None):
    holdings = [Holding(s, BINANCE_SPOT, q, PRICES[s]) for s, q in (spot or {}).items()]
    holdings += [Holding(s, BINANCE_EARN, q, PRICES[s], listed_apr=apr) for s, q in (earn or {}).items()]
    holdings += [Holding(s, ONCHAIN, q, PRICES[s], detail="ethereum/cold") for s, q in (onchain or {}).items()]
    sources = {s: SourceStatus("ok") for s in (BINANCE_SPOT, BINANCE_EARN, BINANCE_FUNDING)}
    return database.record(Portfolio(timestamp=at(hour), holdings=holdings, totals={}, sources=sources))


def buy(hour, base, quantity, price, quote="USDC", order="1", **extra):
    return Trade(timestamp=at(hour), venue="spot", base=base, quote=quote, side=extra.pop("side", "buy"),
                 quantity=quantity, quote_quantity=quantity * price, order_id=order,
                 external_id=f"{base}{quote}:{order}:{hour}", quote_usd=1.0, pair=base + quote, **extra)


def flow(hour, asset, amount, direction=IN, flow_type="deposit", price=0.0, **extra):
    return CashFlow(timestamp=at(hour), asset=asset, direction=direction, amount=amount, flow_type=flow_type,
                    external_id=f"{flow_type}:{asset}:{hour}", price_usd=price, **extra)


# -- parsing -------------------------------------------------------------------------------
def test_parse_spot_fill():
    row = {"id": 7, "orderId": 55, "price": "88000", "qty": "0.05", "quoteQty": "4400", "commission": "0.00005",
           "commissionAsset": "BTC", "time": ms(0), "isBuyer": True}
    trade = parse_spot(row, "BTC", "USDC")
    assert (trade.side, trade.quantity, trade.quote_quantity, trade.fee_asset) == ("buy", 0.05, 4400.0, "BTC")
    assert trade.external_id == "BTCUSDC:7" and trade.order_id == "55" and trade.pair == "BTCUSDC"
    assert parse_spot({**row, "isBuyer": False}, "BTC", "USDC").side == "sell"
    assert parse_spot({**row, "qty": "0"}, "BTC", "USDC") is None


def test_parse_convert_puts_the_stablecoin_on_the_quote_side():
    row = {"orderId": 9, "orderStatus": "SUCCESS", "fromAsset": "USDT", "fromAmount": "300", "toAsset": "SOL",
           "toAmount": "2", "createTime": ms(0)}
    bought = parse_convert(row, STABLE)
    assert (bought.side, bought.base, bought.quote, bought.quantity, bought.quote_quantity) == ("buy", "SOL", "USDT", 2.0, 300.0)
    sold = parse_convert({**row, "fromAsset": "SOL", "fromAmount": "2", "toAsset": "USDT", "toAmount": "300"}, STABLE)
    assert (sold.side, sold.base, sold.quote, sold.quantity) == ("sell", "SOL", "USDT", 2.0)
    assert parse_convert({**row, "orderStatus": "FAIL"}, STABLE) is None


def test_candidate_pairs_only_lists_what_binance_quotes():
    tickers = {"BTCUSDC": 1, "BTCUSDT": 1, "SOLBTC": 1, "USDCUSDT": 1, "DOGEUSDT": 1}
    assert candidate_pairs({"BTC", "SOL", "USDC"}, tickers) == [("BTC", "USDC"), ("BTC", "USDT"), ("SOL", "BTC"), ("USDC", "USDT")]


# -- sync ----------------------------------------------------------------------------------
class FakeClient:
    def __init__(self):
        self.calls: list[tuple] = []
        self.fills = {"BTCUSDC": [{"id": 1, "orderId": 10, "qty": "0.05", "quoteQty": "4400", "commission": "0",
                                   "commissionAsset": "BNB", "time": ms(0.5), "isBuyer": True}],
                      "SOLBTC": [{"id": 4, "orderId": 11, "qty": "10", "quoteQty": "0.02", "commission": "0",
                                  "commissionAsset": "BNB", "time": ms(0.6), "isBuyer": True}]}

    def prices(self):
        return {"BTCUSDC": 90000.0, "SOLBTC": 0.0017, "SOLUSDC": 150.0}

    def price_usd(self, asset, stablecoins=None):
        return PRICES.get(asset, 0.0)

    def my_trades(self, symbol, from_id=0):
        self.calls.append((symbol, from_id))
        return [row for row in self.fills.get(symbol, []) if row["id"] >= from_id]

    def convert_history(self, start_ms, end_ms):
        yield end_ms, [{"orderId": 77, "orderStatus": "SUCCESS", "fromAsset": "USDT", "fromAmount": "140",
                        "toAsset": "LINK", "toAmount": "10", "createTime": ms(0.7)}]

    def daily_closes(self, asset, start_ms, end_ms):
        return {"2024-03-01": 7.0} if asset == "LINK" else {}


def config():
    return SimpleNamespace(cash_flow_settings=CashFlowSettings(), binance_enabled=True, stablecoins=STABLE)


def rows(database, query="SELECT * FROM trades ORDER BY timestamp"):
    with database.connect() as connection:
        return [dict(row) for row in connection.execute(query).fetchall()]


def test_sync_stores_fills_and_conversions_then_only_reads_what_is_new(tmp_path):
    database = Database(tmp_path / "p.db")
    snap(database, 0, spot={"BTC": 0.1, "SOL": 5.0, "USDC": 9000.0})
    client, now = FakeClient(), ms(2)
    report = sync_trades(config(), database, client, now_ms=now)
    assert report == {"spot": 2, "convert": 1, "repriced": 0, "errors": []}
    btc, sol, link = rows(database)
    assert (btc["base"], btc["value_usd"], btc["estimated"]) == ("BTC", 4400.0, 0)
    assert (sol["quote"], sol["estimated"]) == ("BTC", 1) and sol["value_usd"] == pytest.approx(0.02 * 90000)
    assert (link["venue"], link["value_usd"]) == ("convert", 140.0)
    assert ("SOLUSDC", 0) in client.calls   # first run asks every listed pair

    client.calls.clear()
    client.fills["BTCUSDC"].append({"id": 2, "orderId": 12, "qty": "0.01", "quoteQty": "900", "commission": "0",
                                    "commissionAsset": "BNB", "time": ms(2.5), "isBuyer": True})
    again = sync_trades(config(), database, client, now_ms=now + 3_600_000)
    assert again["spot"] == 1 and len(rows(database)) == 4   # the conversion is re-read, not duplicated
    assert sorted(client.calls) == [("BTCUSDC", 2), ("SOLBTC", 5)]   # pairs that never traded wait for the daily scan


def test_first_sync_reprices_deposits_older_than_the_first_snapshot(tmp_path):
    database = Database(tmp_path / "p.db")
    snap(database, 0, spot={"LINK": 100.0})
    old = CashFlow(timestamp="2024-03-01T08:00:00+00:00", asset="LINK", direction=IN, amount=100.0,
                   flow_type="deposit", external_id="old", price_usd=14.0)   # priced from the first snapshot
    upsert_flows(database, [old])
    assert sync_trades(config(), database, FakeClient(), now_ms=ms(2))["repriced"] == 1
    [stored] = rows(database, "SELECT price_usd, value_usd FROM cash_flows")
    assert stored == {"price_usd": 7.0, "value_usd": 700.0}
    assert sync_trades(config(), database, FakeClient(), now_ms=ms(3))["repriced"] == 0   # once


# -- cost basis ----------------------------------------------------------------------------
def test_average_cost_survives_a_partial_sale():
    positions = replay([("1", "BTC", 1.0, 80_000.0, False), ("2", "BTC", 1.0, 100_000.0, False),
                        ("3", "BTC", -1.0, 0.0, False), ("0", "USDC", 5.0, 5.0, False)], STABLE)
    assert positions["BTC"] == Position(1.0, 90_000.0, 0.0) and "USDC" not in positions
    over = replay([("1", "SOL", 1.0, 100.0, False), ("2", "SOL", -5.0, 0.0, False)])
    assert over["SOL"].quantity == 0 and over["SOL"].cost == 0   # sold more than the ledger knew: flat, not negative


def test_report_flags_estimates_and_never_invents_a_basis(tmp_path):
    database = Database(tmp_path / "p.db")
    upsert_trades(database, [buy(-48, "BTC", 0.1, 80_000.0, fee_amount=0.0001, fee_asset="BTC")])
    upsert_flows(database, [
        flow(-40, "LINK", 100.0, price=10.0),                                    # deposit from outside: a guess
        flow(-30, "SOL", 4.0, price=100.0, channel="p2p"),                       # bought P2P: a real price
        flow(-20, "SOL", 1.0, price=120.0, flow_type=REWARD, channel="earn"),
        flow(-10, "BTC", 0.05, direction=OUT, flow_type="withdrawal", is_internal_transfer=True),  # to the cold wallet
        flow(-5, "ETH", 0.5),                                                    # no price: stays untracked
    ])
    snap(database, 0, spot={"SOL": 10.0, "ETH": 0.5}, earn={"USDC": 5000.0}, onchain={"BTC": 0.0999, "LINK": 100.0}, apr=0.026)
    report = cost_basis_report(database, STABLE)
    by = {asset["symbol"]: asset for asset in report["assets"]}

    assert by["BTC"]["avg_entry"] == pytest.approx(8000 / 0.0999) and by["BTC"]["tracked"] == 1.0
    assert by["BTC"]["unrealised_usd"] == pytest.approx(0.0999 * 90_000 - 8000, abs=0.01) and not by["BTC"]["estimated"]
    assert by["LINK"]["estimated"] and by["LINK"]["unrealised_pct"] == 40.0
    assert by["SOL"]["avg_entry"] == pytest.approx(104.0) and by["SOL"]["tracked"] == 0.5 and not by["SOL"]["estimated"]
    assert by["SOL"]["unrealised_usd"] == pytest.approx((150 - 104) * 5)   # only on the half the ledger explains
    assert by["ETH"]["avg_entry"] is None and by["ETH"]["unrealised_usd"] is None and by["ETH"]["tracked"] == 0
    assert "USDC" not in by and report["benchmark_apr"] == 0.026
    assert report["unrealised_usd"] == pytest.approx(sum(a["unrealised_usd"] or 0 for a in by.values()))

    [order] = report["buys"]
    assert order["symbol"] == "BTC" and order["price"] == 80_000.0 and order["since_pct"] == 12.5
    assert order["vs_stable_pp"] == pytest.approx(12.5 - 0.026 * 2 / 365 * 100, abs=0.01)


# -- reconciliation with real trades -----------------------------------------------------------
def test_a_synced_trade_explains_the_change_and_is_listed_as_real(tmp_path):
    database = Database(tmp_path / "p.db")
    snap(database, 0, spot={"USDC": 9000.0})
    snap(database, 1, spot={"USDC": 4600.0, "BTC": 0.05})
    assert [item["kind"] for item in inferred_items(database)] == ["trade"]   # before the ledger knows

    upsert_trades(database, [buy(0.5, "BTC", 0.03, 88_000.0, order="A"), buy(0.6, "BTC", 0.02, 88_000.0, order="A")])
    assert inferred_items(database) == []
    [item] = activity_report(database, days=0, now=T0 + timedelta(hours=2))["items"]
    assert item["kind"] == "trade" and item["inferred"] is False and item["value_usd"] == 4400.0
    assert [(leg["asset"], leg["amount"]) for leg in item["assets"]] == [("BTC", 0.05), ("USDC", -4400.0)]


# -- regressions from the Phase 3 review -----------------------------------------------------------
class FlakyClient(FakeClient):
    """BTCUSDC answers an error; `closes_fail` makes the daily closes unreachable."""

    def __init__(self, broken=("BTCUSDC",), closes_fail=False, error="HTTP 400 pair in break status"):
        super().__init__()
        self.broken, self.closes_fail, self.error, self.close_calls = broken, closes_fail, error, 0

    def my_trades(self, symbol, from_id=0):
        if symbol in self.broken:
            self.calls.append((symbol, from_id))
            raise RuntimeError(self.error)
        return super().my_trades(symbol, from_id)

    def daily_closes(self, asset, start_ms, end_ms):
        self.close_calls += 1
        if self.closes_fail:
            raise RuntimeError("klines unreachable")
        return super().daily_closes(asset, start_ms, end_ms)


def test_one_failing_pair_does_not_hide_the_pairs_after_it(tmp_path):
    database = Database(tmp_path / "p.db")
    snap(database, 0, spot={"BTC": 0.1, "SOL": 5.0, "USDC": 9000.0})
    client = FlakyClient()
    report = sync_trades(config(), database, client, now_ms=ms(2))
    assert report["spot"] == 1 and [row["pair"] for row in rows(database) if row["venue"] == "spot"] == ["SOLBTC"]
    assert "BTCUSDC" in report["errors"][0]
    client.calls.clear()
    sync_trades(config(), database, client, now_ms=ms(3))
    assert ("SOLUSDC", 0) not in client.calls   # the daily scan was completed: not redone every hour

    limited = FlakyClient(error="HTTP 429 Too Many Requests")
    other = Database(tmp_path / "q.db")
    snap(other, 0, spot={"BTC": 0.1, "SOL": 5.0, "USDC": 9000.0})
    sync_trades(config(), other, limited, now_ms=ms(2))
    assert [call[0] for call in limited.calls] == ["BTCUSDC"]   # asked to slow down: stop there...
    limited.calls.clear()
    sync_trades(config(), other, limited, now_ms=ms(3))
    assert limited.calls                                        # ...and take the scan up again next run


def test_historic_repricing_waits_for_the_prices_and_catches_late_rows(tmp_path):
    database = Database(tmp_path / "p.db")
    snap(database, 0, spot={"LINK": 100.0})
    old = lambda key, day: CashFlow(timestamp=f"2024-03-{day:02d}T08:00:00+00:00", asset="LINK", direction=IN,  # noqa: E731
                                    amount=10.0, flow_type="deposit", external_id=key, price_usd=14.0)
    upsert_flows(database, [old("a", 1), old("b", 1), old("c", 1)])
    down = FlakyClient(broken=(), closes_fail=True)
    assert sync_trades(config(), database, down, now_ms=ms(2))["repriced"] == 0
    assert down.close_calls == 1   # asked once for LINK, not once per row (stablecoins are never asked)
    assert sync_trades(config(), database, FakeClient(), now_ms=ms(3))["repriced"] == 3   # prices back: done now
    upsert_flows(database, [old("late", 1)])   # a history backfill that finished afterwards
    assert sync_trades(config(), database, FakeClient(), now_ms=ms(4))["repriced"] == 1
    assert {row["price_usd"] for row in rows(database, "SELECT price_usd FROM cash_flows")} == {7.0}


def test_a_trade_with_no_price_that_day_is_untracked_not_guessed(tmp_path):
    database = Database(tmp_path / "p.db")
    snap(database, 0, spot={"SOL": 10.0})
    client = FakeClient()
    client.fills = {"SOLBTC": [{"id": 1, "orderId": 1, "qty": "10", "quoteQty": "0.02", "commission": "0",
                                "commissionAsset": "BNB", "time": int(datetime(2019, 5, 1, tzinfo=timezone.utc).timestamp() * 1000),
                                "isBuyer": True}]}
    sync_trades(config(), database, client, now_ms=ms(2))
    [trade] = [row for row in rows(database) if row["venue"] == "spot"]
    assert trade["value_usd"] == 0 and trade["estimated"] == 1   # not 0.02 x today's BTC price
    [sol] = cost_basis_report(database, STABLE)["assets"]
    assert sol["avg_entry"] is None and sol["tracked"] == 0


def test_fee_taken_from_the_asset_sold_is_an_event():
    from services.analytics.cost_basis import trade_events

    row = {"timestamp": "t", "base": "BNB", "quote": "USDT", "side": "sell", "quantity": 10.0, "quote_quantity": 6000.0,
           "fee_amount": 0.0075, "fee_asset": "BNB", "value_usd": 6000.0, "estimated": 0}
    assert sum(q for _, asset, q, _, _ in trade_events(row) if asset == "BNB") == pytest.approx(-10.0075)
    bought = {**row, "side": "buy", "fee_asset": "USDT", "fee_amount": 6.0}
    assert sum(q for _, asset, q, _, _ in trade_events(bought) if asset == "USDT") == pytest.approx(-6006.0)
    netted = {**row, "side": "buy", "fee_asset": "BNB"}
    assert [(a, q) for _, a, q, _, _ in trade_events(netted)] == [("BNB", pytest.approx(9.9925)), ("USDT", -6000.0)]


def test_same_second_deposit_and_sale_leave_no_phantom_position():
    positions = replay([("2026-01-01T00:00:00+00:00", "BTC", -1.0, 0.0, False),
                        ("2026-01-01T00:00:00+00:00", "BTC", 1.0, 90_000.0, True)])
    assert positions["BTC"].quantity == 0


def test_order_ids_are_per_pair(tmp_path):
    database = Database(tmp_path / "p.db")
    snap(database, 0, spot={"BTC": 1.0, "SOL": 1.0})
    upsert_trades(database, [buy(-2, "BTC", 0.01, 90_000.0, order="7"), buy(-2, "SOL", 1.0, 150.0, order="7")])
    report = activity_report(database, days=0, now=T0 + timedelta(hours=1))
    assert sorted(item["bought"][0]["asset"] for item in report["items"]) == ["BTC", "SOL"]
    assert len(cost_basis_report(database, STABLE)["buys"]) == 2


def test_a_failed_newest_snapshot_does_not_empty_the_reports(tmp_path):
    from services.analytics.earn import earn_report

    database = Database(tmp_path / "p.db")
    upsert_trades(database, [buy(-5, "BTC", 0.1, 80_000.0)])
    snap(database, 0, earn={"BTC": 0.1, "USDC": 500.0}, apr=0.02)
    failed = {s: SourceStatus("error", "timeout") for s in (BINANCE_SPOT, BINANCE_EARN, BINANCE_FUNDING)}
    database.record(Portfolio(timestamp=at(1), holdings=[], totals={}, sources=failed))
    assert [a["symbol"] for a in cost_basis_report(database, STABLE)["assets"]] == ["BTC"]
    assert {a["symbol"] for a in earn_report(database, STABLE)["assets"]} == {"BTC", "USDC"}


def test_tier_band_of_an_asset_whose_name_starts_with_a_digit():
    from clients.binance import parse_tiers

    assert parse_tiers({"0-2001INCH": "0.03"}, "1INCH") == [{"min": 0.0, "max": 200.0, "apr": 0.03}]
    assert parse_tiers({"0-5000USDC": "0.03"}, "USDC") == [{"min": 0.0, "max": 5000.0, "apr": 0.03}]
