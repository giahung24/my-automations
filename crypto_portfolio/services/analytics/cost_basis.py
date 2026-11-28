"""N4: average entry price and unrealised PnL per coin (``GET /api/analytics/cost-basis``).

One running position per asset for the whole portfolio (not per wallet, so moving a coin to a
cold wallet changes nothing), replayed over every event in time order:

    buy / deposit / reward   quantity += q,  cost += USD value then
    sell / withdrawal / fee  quantity and cost shrink in the same proportion (average cost)

What an event was worth:

* a trade - the price it filled at (``trades``);
* a card or P2P purchase - the price paid (``cash_flows``);
* a crypto deposit from outside - the market price that day. It is the best available guess,
  not what was paid, so the position is flagged ``estimated``;
* an Earn reward - the price when it was received.

Internal transfers are skipped. Stablecoins are not tracked: their basis is $1.

The ledger rarely explains the whole balance (dust conversions, airdrops, trades on delisted
pairs). ``tracked`` is the share of today's quantity it does explain; the unrealised PnL is
computed on that share only, and a coin the ledger knows nothing about has no figure at all.

``replay()`` is pure; ``cost_basis_report()`` reads SQLite only.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from models import BINANCE_EARN
from services.cash_flows import CHANNEL_FIAT, CHANNEL_FIAT_PAYMENT, CHANNEL_P2P, COMPLETED, DEPOSIT, IN
from services.db import Database, parse_timestamp

PAID_CHANNELS = (CHANNEL_FIAT, CHANNEL_FIAT_PAYMENT, CHANNEL_P2P)  # the price is what was paid
ESTIMATED_SHARE = 0.01   # a position is "est." when more than this share of its cost is a guess
MIN_TRACKED = 0.01       # below this the ledger knows nothing useful about the coin
BUYS_LIMIT = 20


@dataclass
class Position:
    quantity: float = 0.0
    cost: float = 0.0
    estimated_cost: float = 0.0

    def add(self, quantity: float, usd: float, estimated: bool = False) -> None:
        if quantity <= 0:
            return
        self.quantity += quantity
        self.cost += usd
        self.estimated_cost += usd if estimated else 0.0

    def remove(self, quantity: float) -> None:
        if quantity <= 0 or self.quantity <= 0:
            return
        keep = max(1.0 - quantity / self.quantity, 0.0)   # selling more than the ledger knows: flat
        self.quantity *= keep
        self.cost *= keep
        self.estimated_cost *= keep


def replay(events: Iterable[tuple], stablecoins: Iterable[str] = ()) -> dict[str, Position]:
    """``events``: ``(timestamp, asset, signed quantity, usd, estimated)``, any order."""
    skip = {s.upper() for s in stablecoins}
    positions: dict[str, Position] = defaultdict(Position)
    # within one second what comes in is booked before what goes out: a coin deposited and sold
    # in the same second must not leave a phantom position behind
    for _, asset, quantity, usd, estimated in sorted(events, key=lambda event: (event[0], event[2] <= 0)):
        if asset in skip:
            continue
        if quantity > 0:
            if usd <= 0:   # no price known: untracked, rather than quantity added at zero cost
                continue
            positions[asset].add(quantity, usd, estimated)
        else:
            positions[asset].remove(-quantity)
    return positions


def trade_events(row) -> list[tuple]:
    """The quantity effects of one ``trades`` row: base leg, quote leg, and a fee in a third asset."""
    when, base, quote, value = row["timestamp"], row["base"], row["quote"], float(row["value_usd"] or 0)
    quantity, quote_quantity = float(row["quantity"]), float(row["quote_quantity"])
    fee, fee_asset = float(row["fee_amount"] or 0), row["fee_asset"] or ""
    estimated = bool(row["estimated"])
    received = base if row["side"] == "buy" else quote
    netted = fee if fee_asset == received else 0.0   # a fee in the asset received shrinks what arrives
    if row["side"] == "buy":
        events = [(when, base, quantity - netted, value, estimated), (when, quote, -quote_quantity, 0.0, False)]
    else:
        events = [(when, base, -quantity, 0.0, False), (when, quote, quote_quantity - netted, value, True)]
    if fee > 0 and fee_asset and fee_asset != received:   # paid from another balance: BNB, or the asset sold
        events.append((when, fee_asset, -fee, 0.0, False))
    return events


def flow_events(row) -> list[tuple]:
    """The quantity effect of one external ``cash_flows`` row."""
    amount, usd = float(row["amount"]), float(row["value_usd"] or 0)
    if row["direction"] == IN:   # with no price it stays untracked: see `replay`
        guess = row["flow_type"] == DEPOSIT and row["channel"] not in PAID_CHANNELS
        return [(row["timestamp"], row["asset"], amount, usd, guess)]
    fee = float(row["fee_amount"] or 0) if (row["fee_asset"] or row["asset"]) == row["asset"] else 0.0
    return [(row["timestamp"], row["asset"], -(amount + fee), 0.0, False)]


def cost_basis_report(database: Database, stablecoins: Iterable[str] = (), min_value: float = 1.0) -> dict:
    stable = {str(s).upper() for s in stablecoins}
    latest = database.latest_healthy()   # a failed source is stored as no rows, not as coins sold
    payload = {"as_of": latest.timestamp if latest else None, "assets": [], "buys": [],
               "unrealised_usd": None, "tracked_pct": None, "benchmark_apr": None, "trades": 0}
    if latest is None:
        return payload
    with database.connect() as connection:
        trades = connection.execute("SELECT * FROM trades ORDER BY timestamp, id").fetchall()
        flows = connection.execute(
            "SELECT timestamp, asset, direction, amount, fee_amount, fee_asset, value_usd, flow_type, channel "
            "FROM cash_flows WHERE status = ? AND is_internal_transfer = 0", (COMPLETED,)).fetchall()
    payload["trades"] = len(trades)
    events = [event for row in trades for event in trade_events(row)]
    events += [event for row in flows for event in flow_events(row)]
    positions = replay(events, stable)

    held: dict[str, dict] = {}
    benchmark = (0.0, None)   # (value, listed APR) of the largest stablecoin position in Earn
    for row in database.assets(latest.id):
        if row["symbol"] in stable:
            if row["source"] == BINANCE_EARN and row.get("listed_apr") is not None and row["value_usd"] > benchmark[0]:
                benchmark = (row["value_usd"], row["listed_apr"])
            continue
        asset = held.setdefault(row["symbol"], {"quantity": 0.0, "value_usd": 0.0, "price": 0.0})
        asset["quantity"] += row["quantity"]
        asset["value_usd"] += row["value_usd"]
        asset["price"] = asset["price"] or row["price_usd"]
    payload["benchmark_apr"] = benchmark[1]

    total, covered_value, held_value = 0.0, 0.0, 0.0
    for symbol, asset in sorted(held.items(), key=lambda item: -item[1]["value_usd"]):
        if asset["value_usd"] < min_value or asset["quantity"] <= 0:
            continue
        position = positions.get(symbol, Position())
        covered = min(position.quantity, asset["quantity"])
        tracked = covered / asset["quantity"]
        entry = position.cost / position.quantity if position.quantity > 0 and tracked >= MIN_TRACKED else None
        item = {"symbol": symbol, "quantity": asset["quantity"], "price": asset["price"],
                "value_usd": round(asset["value_usd"], 2), "tracked": round(tracked, 4),
                "avg_entry": entry, "unrealised_usd": None, "unrealised_pct": None,
                "estimated": position.cost > 0 and position.estimated_cost / position.cost > ESTIMATED_SHARE}
        if entry and asset["price"] > 0:
            item["unrealised_usd"] = round((asset["price"] - entry) * covered, 2)
            item["unrealised_pct"] = round((asset["price"] / entry - 1) * 100, 2)
            total += item["unrealised_usd"]
            covered_value += covered * asset["price"]
        held_value += asset["value_usd"]
        payload["assets"].append(item)
    if covered_value > 0:
        payload["unrealised_usd"] = round(total, 2)
    if held_value > 0:
        payload["tracked_pct"] = round(covered_value / held_value * 100, 1)

    payload["buys"] = _buys(trades, stable, {s: a["price"] for s, a in held.items()}, benchmark[1], latest.dt)
    return payload


def _buys(trades, stable: set[str], prices: dict[str, float], benchmark_apr, now) -> list[dict]:
    """The latest buys, one line per order: what was paid, and how it did since."""
    orders: dict[tuple, dict] = {}
    for row in trades:
        if row["side"] != "buy" or row["base"] in stable or float(row["value_usd"] or 0) <= 0:
            continue
        key = (row["venue"], row["pair"], row["order_id"] or row["external_id"], row["base"])   # order ids are per pair
        order = orders.setdefault(key, {"timestamp": row["timestamp"], "symbol": row["base"], "quantity": 0.0,
                                        "value_usd": 0.0, "estimated": False})
        order["timestamp"] = max(order["timestamp"], row["timestamp"])
        order["quantity"] += float(row["quantity"])
        order["value_usd"] += float(row["value_usd"])
        order["estimated"] = order["estimated"] or bool(row["estimated"])
    buys = sorted(orders.values(), key=lambda order: order["timestamp"], reverse=True)[:BUYS_LIMIT]
    for order in buys:
        order["price"] = order["value_usd"] / order["quantity"]
        order["value_usd"] = round(order["value_usd"], 2)
        current = prices.get(order["symbol"]) or 0.0
        order["since_pct"] = round((current / order["price"] - 1) * 100, 2) if current > 0 else None
        order["vs_stable_pp"] = None
        if order["since_pct"] is not None and benchmark_apr is not None:
            days = max((now - parse_timestamp(order["timestamp"])).total_seconds() / 86400, 0.0)
            # what the same dollars would have earned in the stablecoin at today's listed rate
            order["vs_stable_pp"] = round(order["since_pct"] - benchmark_apr * days / 365 * 100, 2)
    return buys
