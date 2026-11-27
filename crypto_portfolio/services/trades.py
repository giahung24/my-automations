"""N4: the Binance trade ledger (table ``trades``), filled by the collector after each snapshot.

Two sources, both read-only:

* **spot fills** - ``GET /api/v3/myTrades`` answers one pair at a time, so the pairs asked for are
  every asset the account ever held or moved against the USD and bridge quotes Binance lists
  today. Pairs that already traded are read on every run; the full list once a day.
* **conversions** - ``GET /sapi/v1/convert/tradeFlow``, by 30-day windows.

A trade against a stablecoin is valued at $1.00 per unit of quote. Against anything else the
quote is valued from the price series (``estimated = 1``). Fills are immutable, so a re-read
replaces the row.

The first run also re-prices the cash flows older than the first snapshot from daily market
closes: until then they carried the price of the oldest snapshot, which is not what a deposit
of three years ago was worth.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass

from clients.binance import BRIDGE_QUOTES, USD_QUOTES, BinanceClient
from models import BINANCE_SOURCES
from services.cash_flows import (
    DAY_MS,
    SOURCE_BINANCE,
    PriceBook,
    _client,
    _settings,
    get_cursor,
    set_cursor,
    to_iso_utc,
)
from services.db import Database, utc_now_iso

log = logging.getLogger(__name__)

SPOT, CONVERT = "spot", "convert"
BUY, SELL = "buy", "sell"
QUOTES = (*USD_QUOTES, *BRIDGE_QUOTES)
FULL_SCAN_MS = DAY_MS          # how often pairs that never traded are asked again
KEY_FULL_SCAN = "binance.trades.full_scan"
KEY_CONVERT = "binance.convert"
KEY_HISTORIC = "prices.historic_v1"


@dataclass
class Trade:
    timestamp: str
    venue: str
    base: str
    quote: str
    side: str
    quantity: float
    quote_quantity: float
    external_id: str
    pair: str = ""
    order_id: str = ""
    fee_amount: float = 0.0
    fee_asset: str = ""
    quote_usd: float = 0.0
    estimated: bool = False
    raw_json: str = "{}"

    @property
    def value_usd(self) -> float:
        return self.quote_quantity * self.quote_usd


def _f(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_spot(row: dict, base: str, quote: str) -> Trade | None:
    """One ``myTrades`` fill of the pair ``base + quote``."""
    quantity, quote_quantity, timestamp = _f(row.get("qty")), _f(row.get("quoteQty")), to_iso_utc(row.get("time"))
    if quantity <= 0 or not timestamp or row.get("id") is None:
        return None
    return Trade(
        timestamp=timestamp, venue=SPOT, base=base, quote=quote, pair=base + quote,
        side=BUY if row.get("isBuyer") else SELL, quantity=quantity, quote_quantity=quote_quantity,
        order_id=str(row.get("orderId") or ""), fee_amount=_f(row.get("commission")),
        fee_asset=str(row.get("commissionAsset") or "").upper(),
        external_id=f"{base}{quote}:{row['id']}", raw_json=json.dumps(row, sort_keys=True),
    )


def parse_convert(row: dict, stablecoins: Iterable[str]) -> Trade | None:
    """One successful conversion. The stablecoin side, when there is one, is the quote."""
    if str(row.get("orderStatus") or "SUCCESS").upper() != "SUCCESS":
        return None
    given, got = str(row.get("fromAsset") or "").upper(), str(row.get("toAsset") or "").upper()
    given_amount, got_amount = _f(row.get("fromAmount")), _f(row.get("toAmount"))
    timestamp, key = to_iso_utc(row.get("createTime")), row.get("orderId") or row.get("quoteId")
    if not given or not got or given_amount <= 0 or got_amount <= 0 or not timestamp or key is None:
        return None
    selling = got in stablecoins and given not in stablecoins   # coin -> stablecoin
    base, quote = (given, got) if selling else (got, given)
    quantity, quote_quantity = (given_amount, got_amount) if selling else (got_amount, given_amount)
    return Trade(timestamp=timestamp, venue=CONVERT, base=base, quote=quote, side=SELL if selling else BUY,
                 quantity=quantity, quote_quantity=quote_quantity, order_id=str(key),
                 external_id=str(key), raw_json=json.dumps(row, sort_keys=True))


def upsert_trades(database: Database, trades: Iterable[Trade]) -> int:
    rows = [(t.timestamp, t.venue, t.pair, t.order_id, t.base, t.quote, t.side, t.quantity, t.quote_quantity,
             t.fee_amount, t.fee_asset, t.quote_usd, t.value_usd, int(t.estimated), t.external_id, t.raw_json)
            for t in trades]
    with database.connect() as connection:
        connection.executemany(
            """INSERT OR REPLACE INTO trades
                   (timestamp, venue, pair, order_id, base, quote, side, quantity, quote_quantity,
                    fee_amount, fee_asset, quote_usd, value_usd, estimated, external_id, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
    return len(rows)


def known_assets(database: Database) -> set[str]:
    """Every asset the Binance account ever held (snapshots) or moved (cash flows)."""
    marks = ",".join("?" for _ in BINANCE_SOURCES)
    with database.connect() as connection:
        held = connection.execute(
            f"SELECT DISTINCT symbol FROM snapshot_assets WHERE source IN ({marks})", BINANCE_SOURCES).fetchall()
        moved = connection.execute(
            "SELECT DISTINCT asset FROM cash_flows WHERE source = ?", (SOURCE_BINANCE,)).fetchall()
    return {row[0].upper() for row in (*held, *moved) if row[0]}


def candidate_pairs(assets: Iterable[str], tickers: dict) -> list[tuple[str, str]]:
    """``(base, quote)`` for every listed pair of a known asset against a USD or bridge quote."""
    # shortcut: only pairs Binance lists today; a delisted pair (the old BUSD ones) is not read.
    # Take the pair list from exchangeInfo, status included, if old BUSD buys matter.
    return sorted((asset, quote) for asset in assets for quote in QUOTES
                  if asset != quote and f"{asset}{quote}" in tickers)


def _priced(trade: Trade, prices: PriceBook, stablecoins: set[str]) -> Trade:
    trade.quote_usd = prices.price(trade.quote, trade.timestamp)
    trade.estimated = trade.quote not in stablecoins
    return trade


def reprice_before_history(database: Database, prices: PriceBook) -> int:
    """Once: value the cash flows older than the first snapshot at that day's market close."""
    if not prices.historic_before:
        return 0
    repriced = 0
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT id, asset, timestamp, amount, price_usd FROM cash_flows WHERE timestamp < ?",
            (prices.historic_before,)).fetchall()
        for row in rows:
            if row["asset"].upper() in prices.pinned:
                continue
            price = prices.price(row["asset"], row["timestamp"])
            if price > 0 and price != row["price_usd"]:
                connection.execute(
                    "UPDATE cash_flows SET price_usd = ?, value_usd = ?, updated_at = ? WHERE id = ?",
                    (price, float(row["amount"]) * price, utc_now_iso(), row["id"]))
                repriced += 1
    return repriced


def sync_trades(config, database: Database, client: BinanceClient | None = None,
                now_ms: int | None = None) -> dict:
    """Pull new spot fills and conversions into ``trades``. Never raises on an API error."""
    report = {"spot": 0, "convert": 0, "repriced": 0, "errors": []}
    settings = _settings(config)
    if not settings.enabled or not getattr(config, "binance_enabled", False):
        return report
    client = client or _client(config)
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    start_ms = max(now_ms - settings.backfill_days * DAY_MS, 0)
    stablecoins = {str(s).upper() for s in getattr(config, "stablecoins", ()) or ()}

    closes: dict[str, dict[str, float]] = {}

    def historic(symbol: str, day: str) -> float:
        if symbol not in closes:
            closes[symbol] = client.daily_closes(symbol, start_ms, now_ms)
        return closes[symbol].get(day, 0.0)

    with database.connect() as connection:
        first = connection.execute("SELECT MIN(timestamp) FROM snapshots WHERE is_backfilled = 0").fetchone()[0]
    prices = PriceBook(database, stablecoins, fallback=client.price_usd, historic=historic,
                       historic_before=first or "")

    try:
        tickers = client.prices()
        pairs = candidate_pairs(known_assets(database), tickers)
        full = now_ms - (get_cursor(database, KEY_FULL_SCAN) or 0) >= FULL_SCAN_MS
        for base, quote in pairs:
            key = f"binance.trades.{base}{quote}"
            cursor = get_cursor(database, key)
            if cursor is None and not full:
                continue
            rows = client.my_trades(base + quote, (cursor or 0) + 1 if cursor is not None else 0)
            trades = [_priced(t, prices, stablecoins) for t in (parse_spot(r, base, quote) for r in rows) if t]
            report["spot"] += upsert_trades(database, trades)
            if rows:
                set_cursor(database, key, max(int(row["id"]) for row in rows))
        if full:
            set_cursor(database, KEY_FULL_SCAN, now_ms)
    except Exception as exc:  # noqa: BLE001 - the snapshot is safe; the next run resumes from the cursors
        report["errors"].append(f"binance spot trades: {exc}")

    try:
        cursor = get_cursor(database, KEY_CONVERT)
        for chunk_end, rows in client.convert_history(max((cursor or start_ms) - DAY_MS, start_ms), now_ms):
            trades = [_priced(t, prices, stablecoins) for t in (parse_convert(r, stablecoins) for r in rows) if t]
            report["convert"] += upsert_trades(database, trades)
            set_cursor(database, KEY_CONVERT, chunk_end)
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(f"binance convert history: {exc}")

    try:
        if get_cursor(database, KEY_HISTORIC) is None:
            report["repriced"] = reprice_before_history(database, prices)
            set_cursor(database, KEY_HISTORIC, 1)
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(f"historic prices: {exc}")
    return report
