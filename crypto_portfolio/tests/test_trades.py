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
