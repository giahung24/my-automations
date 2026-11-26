"""One-off data correction (run on 2026-11-26): treat the cold-wallet BTC of "My Ledgy" as held
since the very first checkpoint, instead of a deposit made on 2026-11-25.

Why
---
The manual asset "My Ledgy" (0.02540344 BTC, `manual_assets:` in config.yaml) was declared on
2026-11-25T21:25:50Z. By design (M6.3), a manual asset only exists from its first snapshot on and
its first appearance is booked as a `deposit` cash flow. That BTC was in fact held all along, so
the declaration produced a ~$1,530 step in the Net Worth chart on 2026-11-25 and a fake deposit
in TWR / PnL / attribution.

What it does (one transaction, rolled back on any error)
--------------------------------------------------------
For every snapshot strictly before the declaration (`timestamp < 2026-11-25T21:25:50+00:00`),
fetched (`is_backfilled = 0`) and monthly checkpoints (`is_backfilled = 1`) alike:

* adds one `snapshot_assets` row `BTC / manual / <quantity> / detail = 'My Ledgy'`, priced at
  - fetched snapshots: the BTC `price_usd` already stored in that same snapshot (BTC is also
    held in Binance Earn), no API call; `price_id = 'correction:snapshot:<source of that row>'`;
  - monthly checkpoints: the open of the monthly BTCUSDT kline, through
    `services.backfill.fetch_monthly_opens` / `price_key` (the mechanism that built those
    checkpoints); `price_id = 'backfill:binance:BTCUSDT'`. The fetched price is cross-checked
    against the BTC price already stored in the checkpoint;
* adds the value to `snapshots.manual_usd` and `snapshots.total_portfolio_usd`;
* records `manual:My Ledgy = ok` in `snapshot_sources` for fetched snapshots that have per-source
  statuses (what the collector would have written had the asset been declared then);
* deletes the `cash_flows` row of the declaration (`manual:My Ledgy:BTC:182`, id 56): the BTC is
  no longer new money.

Snapshots from the declaration on are untouched. The script refuses to run twice (it aborts if a
target snapshot already has a manual BTC row for the label).

    cp portfolio_history.db portfolio_history.db.bak_btc_backfill_<timestamp>   # first!
    .venv/bin/python scripts/backfill_manual_btc.py --dry-run
    .venv/bin/python scripts/backfill_manual_btc.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import load_config  # noqa: E402
from models import MANUAL, manual_source  # noqa: E402
from services.backfill import QUOTE, fetch_monthly_opens, month_key, price_key  # noqa: E402

LABEL = "My Ledgy"
SYMBOL = "BTC"
DECLARED_AT = "2026-11-25T21:25:50+00:00"
DECLARATION_FLOW_EXTERNAL_ID = "manual:My Ledgy:BTC:182"
DECLARATION_FLOW_ID = 56
PRICE_CHECK_TOLERANCE = 1e-6  # relative; the checkpoints were built from the same klines


def config_quantity(config_path: str | None) -> float:
    config = load_config(config_path)
    for asset in config.manual:
        if asset.label == LABEL:
            quantities = [h.quantity for h in asset.holdings if h.symbol.upper() == SYMBOL]
            if len(quantities) != 1 or quantities[0] <= 0:
                raise SystemExit(f"expected one positive {SYMBOL} holding in '{LABEL}', got {quantities}")
            return float(quantities[0])
    raise SystemExit(f"manual asset '{LABEL}' not found in config")


def stored_btc_price(connection, snapshot_id: int) -> tuple[float, str] | None:
    """Price of the (non-manual) BTC row of the snapshot, and the source it came from."""
    row = connection.execute(
        "SELECT price_usd, source FROM snapshot_assets WHERE snapshot_id = ? AND symbol = ? "
        "AND source <> ? AND price_usd > 0 ORDER BY id DESC LIMIT 1",
        (snapshot_id, SYMBOL, MANUAL)).fetchone()
    return (float(row["price_usd"]), row["source"]) if row else None


def plan(connection, quantity: float) -> list[dict]:
    targets = connection.execute(
        "SELECT id, timestamp, is_backfilled, manual_usd, total_portfolio_usd FROM snapshots "
        "WHERE timestamp < ? ORDER BY timestamp, id", (DECLARED_AT,)).fetchall()
    if not targets:
        raise SystemExit("no snapshot before the declaration, nothing to do")

    already = connection.execute(
        f"SELECT COUNT(*) FROM snapshot_assets WHERE source = ? AND detail = ? AND symbol = ? "
        f"AND snapshot_id IN ({','.join('?' * len(targets))})",
        (MANUAL, LABEL, SYMBOL, *[t["id"] for t in targets])).fetchone()[0]
    if already:
        raise SystemExit(f"{already} target snapshot(s) already hold manual {SYMBOL} for '{LABEL}': "
                         "already corrected, aborting")

    checkpoints = [t for t in targets if t["is_backfilled"]]
    monthly: dict[str, float] = {}
    if checkpoints:
        months = [datetime.fromisoformat(t["timestamp"]) for t in checkpoints]
        asset = price_key(SYMBOL)
        monthly = fetch_monthly_opens(asset, months)
        missing = [month_key(m) for m in months if month_key(m) not in monthly]
        if missing:
            raise SystemExit(f"no {asset}{QUOTE} monthly open for {missing}")

    rows = []
    for target in targets:
        stored = stored_btc_price(connection, target["id"])
        if target["is_backfilled"]:
            price = monthly[month_key(datetime.fromisoformat(target["timestamp"]))]
            price_id = f"backfill:binance:{price_key(SYMBOL)}{QUOTE}"
            if stored and abs(stored[0] - price) > PRICE_CHECK_TOLERANCE * price:
                raise SystemExit(f"snapshot {target['id']} ({target['timestamp']}): kline open {price} "
                                 f"!= stored BTC price {stored[0]}")
        else:
            if stored is None:
                raise SystemExit(f"fetched snapshot {target['id']} ({target['timestamp']}) has no BTC price")
            price, source = stored
            price_id = f"correction:snapshot:{source}"
        rows.append({"id": target["id"], "timestamp": target["timestamp"],
                     "is_backfilled": target["is_backfilled"], "price": price, "price_id": price_id,
                     "value": quantity * price, "total_before": float(target["total_portfolio_usd"])})
    return rows


def apply(connection, rows: list[dict], quantity: float) -> int:
    """All writes; the caller owns the transaction. Returns the number of deleted flows."""
    source_key = manual_source(LABEL)
    for row in rows:
        connection.execute(
            "INSERT INTO snapshot_assets (snapshot_id, symbol, source, quantity, price_usd, value_usd, "
            "detail, price_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row["id"], SYMBOL, MANUAL, quantity, row["price"], row["value"], LABEL, row["price_id"]))
        connection.execute(
            "UPDATE snapshots SET manual_usd = manual_usd + ?, "
            "total_portfolio_usd = total_portfolio_usd + ? WHERE id = ?",
            (row["value"], row["value"], row["id"]))
        if not row["is_backfilled"]:
            connection.execute(
                "INSERT OR IGNORE INTO snapshot_sources (snapshot_id, source, status, error) "
                "SELECT ?, ?, 'ok', '' WHERE EXISTS "
                "(SELECT 1 FROM snapshot_sources WHERE snapshot_id = ?)",
                (row["id"], source_key, row["id"]))

    flow = connection.execute("SELECT * FROM cash_flows WHERE id = ?", (DECLARATION_FLOW_ID,)).fetchone()
    expected = {"source": MANUAL, "asset": SYMBOL, "flow_type": "deposit", "account": LABEL,
                "external_id": DECLARATION_FLOW_EXTERNAL_ID, "timestamp": DECLARED_AT}
    if flow is None or any(flow[k] != v for k, v in expected.items()):
        raise RuntimeError(f"cash_flows id={DECLARATION_FLOW_ID} is not the declaration deposit "
                           f"{expected}: {dict(flow) if flow else None}")
    if flow["matched_flow_id"] is not None or flow["is_internal_transfer"]:
        raise RuntimeError(f"cash_flows id={DECLARATION_FLOW_ID} is paired with another flow; "
                           "unpair it first")
    return connection.execute("DELETE FROM cash_flows WHERE id = ?", (DECLARATION_FLOW_ID,)).rowcount


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(ROOT / "portfolio_history.db"))
    parser.add_argument("--config", default=None, help="config.yaml (default: the project's)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    args = parser.parse_args(argv)

    quantity = config_quantity(args.config)
    connection = sqlite3.connect(args.db, isolation_level=None)  # explicit BEGIN/COMMIT below
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = plan(connection, quantity)
        fetched = sum(1 for r in rows if not r["is_backfilled"])
        print(f"{LABEL}: {quantity} {SYMBOL} -> {len(rows)} snapshot(s) before {DECLARED_AT} "
              f"({fetched} fetched, {len(rows) - fetched} monthly checkpoints)")
        for row in rows:
            print(f"  #{row['id']:>4} {row['timestamp']}  bf={row['is_backfilled']}  "
                  f"BTC ${row['price']:>11,.2f}  +${row['value']:>9,.2f}  "
                  f"total ${row['total_before']:>12,.2f} -> ${row['total_before'] + row['value']:>12,.2f}  "
                  f"[{row['price_id']}]")
        if args.dry_run:
            connection.execute("ROLLBACK")
            print("dry run: nothing written")
            return 0
        deleted = apply(connection, rows, quantity)
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    print(f"committed: {len(rows)} snapshot(s) corrected, {deleted} cash flow(s) deleted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
