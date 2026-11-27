"""N2: explain the quantity changes no recorded flow accounts for. Reads SQLite only.

Between two consecutive good snapshots, for every asset held on Binance:

    residual = quantity_after - quantity_before - rewards - deposits + withdrawals (+ fees)

Quantities are summed over Spot + Earn + Funding, so moving a coin between the three is not a
residual. A snapshot where a Binance source did not answer is skipped: its zero is a failed fetch.

Trades synced from Binance (``trades``, N4) are subtracted like any other flow and listed as they
happened. What is left is read from the snapshots: residuals of opposite sign in the same interval,
worth about the same, are one **inferred trade** at the snapshot prices (a trade the ledger does
not have: not synced yet, or on a pair it does not read). Everything else is **unexplained**.
Nothing inferred is stored.

Scope: Binance only. On-chain wallets have no flow history yet (F2.5), so every transfer into
them would read as unexplained.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from models import BINANCE_SOURCES, STATUS_OK
from services.analytics.cost_basis import trade_events
from services.cash_flows import COMPLETED, IN, SOURCE_BINANCE
from services.db import Database, parse_timestamp

DUST_USD = 1.0          # a residual worth less is rounding or a reward landing a moment late
TRADE_TOLERANCE = 0.15  # the two sides of a trade may differ this much (fees, price moving in the hour)


def pair_residuals(residuals: dict[str, tuple[float, float]]) -> tuple[dict | None, list[dict]]:
    """``{asset: (quantity, usd)}`` of one interval -> (inferred trade or None, unexplained legs)."""
    legs = [{"asset": asset, "amount": quantity, "value_usd": round(usd, 2)}
            for asset, (quantity, usd) in sorted(residuals.items()) if abs(usd) >= DUST_USD]
    sold = sorted((leg for leg in legs if leg["amount"] < 0), key=lambda leg: leg["value_usd"])
    bought = sorted((leg for leg in legs if leg["amount"] > 0), key=lambda leg: -leg["value_usd"])
    out_usd = -sum(leg["value_usd"] for leg in sold)
    in_usd = sum(leg["value_usd"] for leg in bought)
    if sold and bought and abs(in_usd - out_usd) <= TRADE_TOLERANCE * max(in_usd, out_usd):
        return {"sold": sold, "bought": bought, "value_usd": round((in_usd + out_usd) / 2, 2)}, []
    return None, legs


def inferred_items(database: Database, since_iso: str | None = None, tz_offset: int = 0) -> list[dict]:
    """Activity items (``kind`` ``trade`` / ``unexplained``) for the snapshots from ``since_iso`` on."""
    marks = ",".join("?" for _ in BINANCE_SOURCES)
    # shortcut: every Binance asset row of the window is loaded on each call; fine for a year of
    # hourly snapshots, cache per snapshot id if the Activity tab ever feels slow.
    with database.connect() as connection:
        snapshots = connection.execute(
            f"""SELECT s.id, s.timestamp FROM snapshots s
                WHERE s.is_backfilled = 0 AND s.timestamp >= ?
                  AND NOT EXISTS (SELECT 1 FROM snapshot_sources x WHERE x.snapshot_id = s.id
                                  AND x.source IN ({marks}) AND x.status <> ?)
                ORDER BY s.timestamp, s.id""",
            (since_iso or "", *BINANCE_SOURCES, STATUS_OK),
        ).fetchall()
        if len(snapshots) < 2:
            return []
        held = connection.execute(
            f"""SELECT a.snapshot_id, a.symbol, SUM(a.quantity) AS quantity, MAX(a.price_usd) AS price
                FROM snapshot_assets a JOIN snapshots s ON s.id = a.snapshot_id
                WHERE a.source IN ({marks}) AND s.is_backfilled = 0 AND s.timestamp >= ?
                GROUP BY a.snapshot_id, a.symbol""",
            (*BINANCE_SOURCES, snapshots[0]["timestamp"]),
        ).fetchall()
        flows = connection.execute(
            "SELECT timestamp, asset, direction, amount, fee_amount, fee_asset FROM cash_flows "
            "WHERE source = ? AND status = ? AND timestamp > ?",
            (SOURCE_BINANCE, COMPLETED, snapshots[0]["timestamp"]),
        ).fetchall()

        trades = connection.execute(
            "SELECT * FROM trades WHERE timestamp > ? ORDER BY timestamp, id", (snapshots[0]["timestamp"],)
        ).fetchall()

    times = [parse_timestamp(row["timestamp"]) for row in snapshots]
    state: dict[int, dict[str, tuple[float, float]]] = defaultdict(dict)
    for row in held:
        state[row["snapshot_id"]][row["symbol"]] = (float(row["quantity"] or 0), float(row["price"] or 0))
    explained: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))  # interval -> asset -> qty
    for row in flows:
        index = bisect_left(times, parse_timestamp(row["timestamp"]))  # first snapshot at or after the flow
        if index >= len(times):
            continue
        if row["direction"] == IN:
            explained[index][row["asset"]] += float(row["amount"])
        else:
            fee = float(row["fee_amount"] or 0) if (row["fee_asset"] or row["asset"]) == row["asset"] else 0.0
            explained[index][row["asset"]] -= float(row["amount"]) + fee

    for row in trades:
        index = bisect_left(times, parse_timestamp(row["timestamp"]))
        if index < len(times):
            for _, asset, quantity, _, _ in trade_events(row):
                explained[index][asset] += quantity

    offset = timedelta(minutes=tz_offset)
    items: list[dict] = []
    loose: dict[str, list[tuple[int, dict]]] = defaultdict(list)  # local day -> (snapshot id, leg)
    for index in range(1, len(snapshots)):
        before, after = state[snapshots[index - 1]["id"]], state[snapshots[index]["id"]]
        residuals = {}
        for asset in before.keys() | after.keys() | explained[index].keys():
            quantity = after.get(asset, (0.0, 0.0))[0] - before.get(asset, (0.0, 0.0))[0] - explained[index][asset]
            price = after.get(asset, (0.0, 0.0))[1] or before.get(asset, (0.0, 0.0))[1]
            if quantity:
                residuals[asset] = (quantity, quantity * price)
        trade, legs = pair_residuals(residuals)
        snapshot_id, timestamp = snapshots[index]["id"], snapshots[index]["timestamp"]
        day = (times[index].astimezone(timezone.utc) + offset).strftime("%Y-%m-%d")
        if trade:
            items.append(_item("trade", f"trade:{snapshot_id}", snapshot_id, timestamp, day,
                               value_usd=trade["value_usd"], sold=trade["sold"], bought=trade["bought"],
                               assets=[*trade["sold"], *trade["bought"]],
                               window_start=snapshots[index - 1]["timestamp"]))
        for leg in legs:
            loose[day].append((snapshot_id, {**leg, "timestamp": timestamp}))
    for day, entries in loose.items():
        newest = max(entries, key=lambda entry: entry[0])
        legs = [leg for _, leg in entries]
        items.append(_item("unexplained", f"unexplained:{day}", newest[0], newest[1]["timestamp"], day,
                           value_usd=round(sum(leg["value_usd"] for leg in legs), 2), assets=legs))
    return items


def trade_items(database: Database, since_iso: str | None = None, tz_offset: int = 0) -> list[dict]:
    """Activity items for the trades synced from Binance, one per order."""
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM trades WHERE timestamp >= ? ORDER BY timestamp, id", (since_iso or "",)).fetchall()
    offset = timedelta(minutes=tz_offset)
    orders: dict[tuple, dict] = {}
    for row in rows:
        order = orders.setdefault((row["venue"], row["order_id"] or row["external_id"]),
                                  {"legs": defaultdict(lambda: [0.0, 0.0]), "value": 0.0})
        order.update(timestamp=row["timestamp"], last_id=row["id"], venue=row["venue"],
                     estimated=order.get("estimated", False) or bool(row["estimated"]))
        sign = 1 if row["side"] == "buy" else -1
        value = float(row["value_usd"] or 0)
        for asset, quantity in ((row["base"], sign * float(row["quantity"])),
                                (row["quote"], -sign * float(row["quote_quantity"]))):
            order["legs"][asset][0] += quantity
            order["legs"][asset][1] += value if quantity > 0 else -value
        order["value"] += value
    items = []
    for (venue, key), order in orders.items():
        legs = [{"asset": asset, "amount": quantity, "value_usd": round(usd, 2)}
                for asset, (quantity, usd) in sorted(order["legs"].items()) if quantity]
        when = parse_timestamp(order["timestamp"])
        item = _item("trade", f"{venue}:{key}", order["last_id"], order["timestamp"],
                     (when.astimezone(timezone.utc) + offset).strftime("%Y-%m-%d"),
                     value_usd=round(order["value"], 2), assets=legs, venue=venue,
                     sold=[leg for leg in legs if leg["amount"] < 0], bought=[leg for leg in legs if leg["amount"] > 0])
        item.update(inferred=False, estimated=order["estimated"])
        items.append(item)
    return items


def _item(kind: str, key: str, snapshot_id: int, timestamp: str, day: str, **extra) -> dict:
    return {
        "id": key, "key": key, "kind": kind, "group": True, "inferred": True,
        "timestamp": timestamp, "day": day, "asset": "", "amount": None, "direction": None,
        "from": "Binance", "to": "Binance", "source": "binance", "sources": ["binance"],
        "channel": "", "status": COMPLETED, "match": None, "needs_review": False,
        "_last_id": snapshot_id, **extra,
    }
