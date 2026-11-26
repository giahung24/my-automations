"""Asset allocation & rebalancing assistant (ROADMAP_LEDGER §4.5, Epic M7).

From the newest snapshot: the weight of every target bucket, its drift from the target, and the
orders that would bring the portfolio back in line. **Suggestions only**: nothing here places an
order, and the Binance key stays read-only.

Four modes of suggestion:

- ``target``    every bucket back to its exact target weight (self-funded, sum of orders = 0);
- ``band_edge`` only the buckets outside the tolerance band, pulled to the nearest band edge;
                the gap is covered by the in-band buckets (less turnover than ``target``);
- contribution  new money only (``?contribution=1000``): BUY orders that add up to exactly the
                amount, poured into the most under-weight buckets first. Nothing is sold.
- funding       partial / DCA rebalance (``?source_bucket=Stablecoin&funding_amount_usd=5000``):
                one SELL in the chosen source bucket, BUY orders in the under-weight buckets,
                Σ BUY = Σ SELL = the amount, so the portfolio value V does not change. The money
                goes deepest deficit first (``strategy=greedy``) or in proportion to each
                bucket's deficit (``strategy=proportional``). The amount is cut to what the source
                can sell now (``limited_by="liquidity"``) and to what the other buckets still miss
                to reach their target (``limited_by="room"``). Computed even when nothing breaches.

"Dry powder" (cash kept on purpose to buy a dip, e.g. the USDC in Simple Earn) is set aside by
`allocation.dry_powder` rules before any weight is computed, so it never shows up as a
Stablecoin over-weight and is never offered for sale.

Liquidity: Simple Earn *locked* positions and manual holdings marked `liquid: false` cannot be
sold. A SELL larger than what the bucket can sell is cut down (``constrained_by``); the gap is
moved to another over-weight bucket, else the BUY orders shrink and ``unfilled_usd`` says by how
much. Sells are taken Spot -> Earn flexible -> Funding -> on-chain -> cold storage, and a leg
that sits off the exchange says so (``requires_transfer``, ``from``).

The engine (`allocate` and the helpers it calls) is pure; `allocation_report` is the only part
that reads SQLite.
"""
from __future__ import annotations

import math
from collections.abc import Collection
from dataclasses import dataclass, replace

from config import AllocationBucket, AllocationSettings, DryPowderRule, ManualAsset
from models import BINANCE_EARN, BINANCE_FUNDING, BINANCE_SPOT, MANUAL, ONCHAIN, SOURCE_LABELS

EPS = 0.005  # half a cent: below this two USD amounts are equal
LOCKED_EARN = "locked_earn"
ILLIQUID = "illiquid_manual"
# Where a SELL is taken from first (ROADMAP_LEDGER M7.3 AC3): the exchange's free balance, then
# what one click redeems, then what has to be moved to the exchange first.
SELL_ORDER = {BINANCE_SPOT: 0, BINANCE_EARN: 1, BINANCE_FUNDING: 2, ONCHAIN: 3, MANUAL: 4}
OFF_EXCHANGE = (ONCHAIN, MANUAL)
STRATEGIES = ("greedy", "proportional")


@dataclass(frozen=True)
class Lot:
    """One priced balance of the snapshot: a symbol in one place (source + detail)."""

    symbol: str
    source: str
    value_usd: float
    price_usd: float
    detail: str = ""
    lock_reason: str | None = None  # LOCKED_EARN / ILLIQUID: cannot be sold now

    @property
    def liquid(self) -> bool:
        return self.lock_reason is None

    @property
    def quantity(self) -> float:
        return self.value_usd / self.price_usd if self.price_usd > 0 else 0.0


# -- dry powder ---------------------------------------------------------------------------
def split_dry_powder(lots: list[Lot], rules: tuple[DryPowderRule, ...] | list[DryPowderRule]
                     ) -> tuple[list[Lot], list[Lot]]:
    """``(kept, dry)``: the value matched by the rules moves to `dry`, in rule order.

    A rule with `max_usd` sets aside at most that much in total; a lot only partly taken is
    split in two (same price, value shared out).
    """
    left = [rule.max_usd for rule in rules]
    kept: list[Lot] = []
    dry: list[Lot] = []
    for lot in lots:
        value = lot.value_usd
        for index, rule in enumerate(rules):
            if value <= 0:
                break
            if lot.symbol not in rule.symbols or (rule.sources and lot.source not in rule.sources):
                continue
            take = value if left[index] is None else min(value, left[index])
            if take <= 0:
                continue
            if left[index] is not None:
                left[index] -= take
            dry.append(replace(lot, value_usd=take))
            value -= take
        if value > EPS:
            kept.append(replace(lot, value_usd=value))
    return kept, dry


# -- weights ------------------------------------------------------------------------------
def bucket_lots(lots: list[Lot], settings: AllocationSettings
                ) -> tuple[dict[str, list[Lot]], list[Lot]]:
    """Lots grouped by bucket name (every bucket present), and the lots no bucket takes."""
    grouped: dict[str, list[Lot]] = {bucket.name: [] for bucket in settings.buckets}
    unassigned: list[Lot] = []
    for lot in lots:
        name = settings.bucket_of(lot.symbol)
        if name is None:
            unassigned.append(lot)
        else:
            grouped[name].append(lot)
    return grouped, unassigned


def target_deltas(values: dict[str, float], targets: dict[str, float]) -> dict[str, float]:
    """Δ_b = w*_b · V − V_b for every bucket (sums to 0)."""
    total = sum(values.values())
    return {name: targets[name] / 100 * total - values[name] for name in values}


def turnover(deltas: dict[str, float]) -> float:
    """USD bought (= USD sold when the orders are self-funded)."""
    return sum(d for d in deltas.values() if d > 0)


def _spread(amount: float, rooms: dict[str, float]) -> dict[str, float]:
    """Share `amount` (>= 0) out in proportion to `rooms`, never more than a room."""
    capacity = sum(rooms.values())
    if amount <= 0 or capacity <= 0:
        return {}
    take = min(amount, capacity)
    return {name: take * room / capacity for name, room in rooms.items() if room > 0}


def band_edge_deltas(values: dict[str, float], targets: dict[str, float],
                     tolerance_pp: float) -> dict[str, float]:
    """Breaching buckets to the nearest band edge, the in-band ones covering the difference.

    The in-band buckets share the gap in proportion to the room left to their opposite band
    edge. If they cannot absorb it all, the breaching buckets move on past their edge toward the
    target; if even that is not enough the full `target` orders are used. The result never
    trades more than `target_deltas` would.
    """
    total = sum(values.values())
    full = target_deltas(values, targets)
    if total <= 0:
        return {name: 0.0 for name in values}
    deltas = {name: 0.0 for name in values}
    over, under, inside = [], [], []
    for name, value in values.items():
        drift = value / total * 100 - targets[name]
        if drift > tolerance_pp:
            over.append(name)
            edge = min(targets[name] + tolerance_pp, 100.0)
        elif drift < -tolerance_pp:
            under.append(name)
            edge = max(targets[name] - tolerance_pp, 0.0)
        else:
            inside.append(name)
            continue
        deltas[name] = edge / 100 * total - value
    if not over and not under:
        return deltas

    # what the other buckets must add (> 0: they buy) or give (< 0: they sell)
    residual = -sum(deltas.values())
    if abs(residual) > EPS:
        sign = 1.0 if residual > 0 else -1.0
        if sign > 0:
            rooms = {n: max(0.0, min(targets[n] + tolerance_pp, 100.0) / 100 * total - values[n])
                     for n in inside}
        else:
            rooms = {n: max(0.0, values[n] - max(targets[n] - tolerance_pp, 0.0) / 100 * total)
                     for n in inside}
        for name, share in _spread(abs(residual), rooms).items():
            deltas[name] += sign * share
        residual = -sum(deltas.values())

    if abs(residual) > EPS:  # in-band room exhausted: breaching buckets go past their edge
        sign = 1.0 if residual > 0 else -1.0
        side = under if sign > 0 else over
        rooms = {n: max(0.0, sign * (full[n] - deltas[n])) for n in side}
        for name, share in _spread(abs(residual), rooms).items():
            deltas[name] += sign * share
        residual = -sum(deltas.values())

    if abs(residual) > EPS or turnover(deltas) > turnover(full) + EPS:
        return full
    return deltas


def contribution_deltas(values: dict[str, float], targets: dict[str, float], amount: float,
                        min_trade_usd: float = 0.0, exclude: Collection[str] = ()) -> dict[str, float]:
    """BUY-only orders adding up to exactly `amount`, deepest deficit first (water-filling).

    With V' = V + amount, every funded bucket ends at the same drift λ (as low as possible),
    the others are left alone: x_b = max(0, (w*_b + λ)·V' − V_b) and Σ x_b = amount. A
    bucket that would get less than `min_trade_usd` is dropped and the amount shared again
    among the rest; the most under-weight bucket is always kept, so the sum stays exact.
    Buckets in `exclude` never receive money (the source of a funding trade).
    """
    candidates = [name for name in values if name not in exclude]
    if amount <= 0 or not candidates:
        return {name: 0.0 for name in values}
    after_total = sum(values.values()) + amount
    eligible = [name for name in candidates if targets[name] > 0] or candidates
    while True:
        orders = _water_fill(values, targets, amount, after_total, eligible)
        funded = [n for n in eligible if orders[n] > EPS]
        small = [n for n in funded if orders[n] < min_trade_usd]
        if not small or len(funded) <= 1:
            break
        eligible.remove(min(small, key=lambda n: (orders[n], n)))  # share again without it
    return {name: orders.get(name, 0.0) for name in values}


def _water_fill(values: dict[str, float], targets: dict[str, float], amount: float,
                after_total: float, names: list[str]) -> dict[str, float]:
    """Solve Σ max(0, (w*_b + λ)·V' − V_b) = amount over `names` (piecewise linear in λ)."""
    # λ_b: the drift (as a fraction, measured against V') where bucket b starts receiving money
    levels = sorted(names, key=lambda n: (values[n] / after_total - targets[n] / 100, n))
    lam = 0.0
    for count in range(1, len(levels) + 1):
        active = levels[:count]
        lam = (amount + sum(values[n] - targets[n] / 100 * after_total for n in active)) \
            / (count * after_total)
        if count == len(levels):
            break
        following = levels[count]
        if lam <= values[following] / after_total - targets[following] / 100:
            break
    return {n: max(0.0, (targets[n] / 100 + lam) * after_total - values[n]) for n in names}


def deficits(values: dict[str, float], targets: dict[str, float], total: float,
             exclude: Collection[str] = ()) -> dict[str, float]:
    """USD each bucket misses to reach its target at portfolio value `total` (> 0 only)."""
    out = {}
    for name in values:
        if name in exclude:
            continue
        room = targets[name] / 100 * total - values[name]
        if room > EPS:
            out[name] = room
    return out


def proportional_deltas(values: dict[str, float], targets: dict[str, float], amount: float,
                        min_trade_usd: float = 0.0, exclude: Collection[str] = ()) -> dict[str, float]:
    """BUY-only orders adding up to `amount`, shared in proportion to each bucket's deficit.

    The deficits are measured at the current value V = Σ values (a funding trade does not change
    it). A share under `min_trade_usd` is dropped and the amount shared again among the rest, in
    the same proportions (they may then pass their target a little, as in `contribution_deltas`);
    the largest deficit is always kept, so the sum stays exact.
    """
    rooms = deficits(values, targets, sum(values.values()), exclude)
    if amount <= 0 or not rooms:
        return {name: 0.0 for name in values}
    while True:
        shares = _spread(amount, rooms)
        given = sum(shares.values())
        if given < amount - 1e-9:  # fewer rooms than the amount once some were dropped
            shares = {name: share * amount / given for name, share in shares.items()}
        small = [n for n, share in shares.items() if share < min_trade_usd]
        if not small or len(shares) <= 1:
            break
        del rooms[min(small, key=lambda n: (shares[n], n))]
    return {name: shares.get(name, 0.0) for name in values}


def funding_plan(values: dict[str, float], targets: dict[str, float], liquid: dict[str, float],
                 source: str, amount: float, strategy: str = "greedy",
                 min_trade_usd: float = 0.0) -> tuple[dict[str, float], dict]:
    """Partial / DCA rebalance: sell `amount` of `source`, buy the under-weight buckets with it.

    Returns ``(deltas, info)``: the orders (rounded to cents, Σ = 0, the source the only SELL)
    and the ``funding`` block of the report. Raises ValueError on a structural mistake (unknown
    source, amount <= 0, unknown strategy); a too-large amount is cut down, never refused.
    """
    if source not in values:
        raise ValueError(f"Unknown source bucket '{source}'. Choose one of: {', '.join(values)}.")
    if not amount or amount <= 0 or not math.isfinite(amount):
        raise ValueError("The funding amount must be a number of USD above 0.")
    if strategy not in STRATEGIES:
        raise ValueError(f"Unknown strategy '{strategy}': use {' or '.join(STRATEGIES)}.")
    total = sum(values.values())
    room = sum(deficits(values, targets, total, exclude=(source,)).values())
    can_sell = max(0.0, liquid.get(source, 0.0))
    cap = min(can_sell, room)
    limited_by = None
    if amount > cap + EPS:
        limited_by = "liquidity" if can_sell <= room else "room"
    # whole cents; a cap is rounded down so the SELL never outgrows what the source can sell
    applied = max(0.0, min(round(amount, 2), math.floor(cap * 100 + 1e-6) / 100))

    deltas: dict[str, float] = {}
    if applied > 0:
        if strategy == "greedy":
            # the source's value goes down by what it sells, so V' = Σ values2 + applied = V
            values2 = dict(values)
            values2[source] -= applied
            raw = contribution_deltas(values2, targets, applied, min_trade_usd, exclude=(source,))
        else:
            raw = proportional_deltas(values, targets, applied, min_trade_usd, exclude=(source,))
        deltas = finalize_deltas({n: d for n, d in raw.items() if n != source}, 0.0,
                                 keep_sum=applied)
        bought = round(sum(deltas.values()), 2)
        if bought > 0:
            deltas[source] = -bought
        else:
            deltas = {}
    applied = round(sum(d for d in deltas.values() if d > 0), 2)

    source_target = targets[source] / 100 * total
    excess = max(0.0, values[source] - source_target)
    has_progress = excess > EPS and applied > 0
    info = {
        "source_bucket": source,
        "requested_usd": round(amount, 2),
        "applied_usd": applied,
        "strategy": strategy,
        "limited_by": limited_by,
        "source_below_target": values[source] - applied < source_target - EPS,
        "source_liquid_usd": round(can_sell, 2),
        "room_usd": round(room, 2),
        "source_excess_usd": round(excess, 2),
        "progress_pct": round(applied / excess * 100, 1) if has_progress else None,
        "remaining_batches_estimate": round(excess / applied, 1) if has_progress else None,
    }
    return deltas, info


# -- liquidity ----------------------------------------------------------------------------
def apply_liquidity(deltas: dict[str, float], values: dict[str, float],
                    targets: dict[str, float], liquid: dict[str, float]
                    ) -> tuple[dict[str, float], dict[str, float], float]:
    """Cut every SELL down to what its bucket can sell now.

    Returns ``(deltas, cut, unfilled_usd)``; `cut` maps a constrained bucket to the USD it could
    not sell. The missing amount is first sold from other buckets still above their target (in
    proportion to what they have left to give), then whatever remains is taken off the BUY
    orders pro rata and reported as `unfilled_usd`. The orders stay self-funded.
    """
    deltas = dict(deltas)
    total = sum(values.values())
    cut: dict[str, float] = {}
    for name, delta in deltas.items():
        if delta < 0 and -delta > liquid.get(name, 0.0) + EPS:
            cut[name] = -delta - liquid.get(name, 0.0)
            deltas[name] = -liquid.get(name, 0.0)
    shortfall = sum(cut.values())
    if shortfall <= EPS:
        return deltas, cut, 0.0

    rooms = {}
    for name, delta in deltas.items():
        if name in cut:
            continue
        excess = values[name] - targets[name] / 100 * total + min(delta, 0.0)  # left above target
        spare = liquid.get(name, 0.0) + min(delta, 0.0)                          # left to sell
        room = min(excess, spare)
        if room > EPS:
            rooms[name] = room
    for name, share in _spread(shortfall, rooms).items():
        deltas[name] -= share
        shortfall -= share

    unfilled = 0.0
    if shortfall > EPS:
        buys = sum(d for d in deltas.values() if d > 0)
        if buys > 0:
            scale = max(0.0, (buys - shortfall) / buys)
            for name, delta in deltas.items():
                if delta > 0:
                    deltas[name] = delta * scale
        unfilled = shortfall
    return deltas, cut, unfilled


# -- order hygiene ------------------------------------------------------------------------
def finalize_deltas(deltas: dict[str, float], min_trade_usd: float, sink: str | None = None,
                    keep_sum: float = 0.0) -> dict[str, float]:
    """Drop orders under `min_trade_usd`, round to cents, keep Σ = `keep_sum` exactly.

    The leftover of the dropped / rounded orders is taken off an existing order of the same
    sign, so no order ever grows (a SELL never outgrows its liquidity): `sink` (the Stablecoin
    bucket) if it qualifies, else the largest one. An order that shrinks under the minimum is
    dropped in turn.
    """
    kept = {n: d for n, d in deltas.items() if abs(d) >= max(min_trade_usd, EPS)}
    for _ in range(len(deltas) + 2):
        kept = {n: round(d, 2) for n, d in kept.items()}
        residual = round(sum(kept.values()) - keep_sum, 2)
        if abs(residual) < 0.01 or not kept:
            break
        same_sign = [n for n, d in kept.items() if d * residual > 0]
        if not same_sign:  # only with a non-zero `keep_sum`: grow the largest order by the cents
            largest = max(kept, key=lambda n: (abs(kept[n]), n))
            kept[largest] = round(kept[largest] - residual, 2)
            continue
        target = sink if sink in same_sign else max(same_sign, key=lambda n: (abs(kept[n]), n))
        reduced = kept[target] - residual
        if reduced * kept[target] <= 0:  # the residual swallows the whole order
            del kept[target]
            continue
        kept[target] = round(reduced, 2)
        if abs(kept[target]) < min_trade_usd and len(kept) > 1:
            del kept[target]
    return {n: d for n, d in kept.items() if abs(d) >= 0.01}


# -- orders -------------------------------------------------------------------------------
def _fmt_qty(quantity: float) -> str:
    if quantity >= 1000:
        return f"{quantity:,.2f}"
    if quantity >= 1:
        return f"{quantity:,.4f}".rstrip("0").rstrip(".")
    return f"{quantity:.8f}".rstrip("0").rstrip(".") or "0"


def _fmt_usd(amount: float) -> str:
    return f"${amount:,.2f}"


def _place(lot: Lot) -> str:
    """Human name of where a lot sits: the wallet / manual label, else the source label."""
    if lot.source in OFF_EXCHANGE and lot.detail:
        return lot.detail
    if lot.source == BINANCE_EARN and lot.detail:
        return f"{SOURCE_LABELS[BINANCE_EARN]} ({lot.detail})"
    return SOURCE_LABELS.get(lot.source, lot.source)


def _symbol_prices(lots: list[Lot]) -> dict[str, tuple[float, float]]:
    """symbol -> (value, value-weighted price) over `lots`."""
    totals: dict[str, list[float]] = {}
    for lot in lots:
        entry = totals.setdefault(lot.symbol, [0.0, 0.0])
        entry[0] += lot.value_usd
        entry[1] += lot.quantity
    return {s: (v, v / q if q > 0 else 0.0) for s, (v, q) in totals.items()}


def buy_order(bucket: AllocationBucket, usd: float, lots: list[Lot],
              known_prices: dict[str, float]) -> dict:
    """BUY in the bucket's largest asset (else its first listed symbol)."""
    prices = _symbol_prices(lots)
    if prices:
        asset = max(prices, key=lambda s: (prices[s][0], s))
        price = prices[asset][1]
    else:
        asset = bucket.symbols[0] if bucket.symbols else None
        price = known_prices.get(asset, 0.0) if asset else 0.0
    est_qty = usd / price if asset and price > 0 else None
    what = f"{_fmt_qty(est_qty)} {asset}" if est_qty is not None else (asset or f"assets of {bucket.name}")
    return {
        "bucket": bucket.name, "action": "BUY", "usd": round(usd, 2), "asset": asset,
        "price_usd": round(price, 8) if price else None,
        "est_qty": round(est_qty, 8) if est_qty is not None else None,
        "constrained_by": None, "legs": [], "requires_transfer": False, "from": [],
        "text": f"BUY ~{what} (~{_fmt_usd(usd)}) · {bucket.name}",
    }


def sell_order(bucket: AllocationBucket, usd: float, lots: list[Lot],
               constrained_by: str | None) -> dict:
    """SELL taken from the bucket's liquid lots, Spot -> Earn flexible -> Funding -> on-chain -> cold."""
    ordered = sorted((lot for lot in lots if lot.liquid),
                     key=lambda lot: (SELL_ORDER.get(lot.source, 9), -lot.value_usd, lot.symbol))
    legs, left = [], usd
    for lot in ordered:
        if left <= EPS:
            break
        take = min(left, lot.value_usd)
        left -= take
        legs.append({
            "asset": lot.symbol, "source": lot.source, "from": _place(lot), "usd": round(take, 2),
            "est_qty": round(take / lot.price_usd, 8) if lot.price_usd > 0 else None,
            "requires_transfer": lot.source in OFF_EXCHANGE,
            "requires_redeem": lot.source == BINANCE_EARN,
        })
    assets = list(dict.fromkeys(leg["asset"] for leg in legs))
    asset = assets[0] if assets else None
    est_qty = None
    if len(assets) == 1 and all(leg["est_qty"] is not None for leg in legs):
        est_qty = round(sum(leg["est_qty"] for leg in legs), 8)
    transfer_from = list(dict.fromkeys(leg["from"] for leg in legs if leg["requires_transfer"]))
    if est_qty is not None:
        what = f"{_fmt_qty(est_qty)} {asset}"
    else:
        what = " + ".join(assets) if assets else f"assets of {bucket.name}"
    text = f"SELL ~{what} (~{_fmt_usd(usd)}) · {bucket.name}"
    places = list(dict.fromkeys(leg["from"] for leg in legs))
    if places:
        text += " · from " + ", ".join(places)
    if transfer_from:
        text += " (transfer to Binance first)"
    return {
        "bucket": bucket.name, "action": "SELL", "usd": round(usd, 2), "asset": asset,
        "price_usd": round(legs[0]["usd"] / legs[0]["est_qty"], 8)
        if legs and legs[0]["est_qty"] else None,
        "est_qty": est_qty, "constrained_by": constrained_by, "legs": legs,
        "requires_transfer": bool(transfer_from), "from": transfer_from, "text": text,
    }


def _as_quote_spend(order: dict) -> dict:
    """A stablecoin "SELL" that funds buys is not a trade of its own: the buys are paid in it
    directly (X/USDC pairs). Same order, flagged and worded as money spent."""
    text = "USE" + order["text"].removeprefix("SELL") + " · pay the buys with it"
    return {**order, "quote_spend": True, "text": text}


# -- engine -------------------------------------------------------------------------------
def allocate(lots: list[Lot], settings: AllocationSettings, *, contribution: float | None = None,
             source_bucket: str | None = None, funding_amount: float | None = None,
             strategy: str = "greedy", include_dry_powder: bool = False,
             unpriced_assets: int = 0) -> dict:
    """Weights, drift and suggested orders for `lots` (pure; see the module docstring).

    Raises ValueError when the funding request is malformed (see `funding_plan`) or mixed with
    a `contribution`.
    """
    funding_requested = source_bucket is not None or funding_amount is not None
    if funding_requested and contribution:
        raise ValueError("Use either new money (contribution) or partial funding "
                         "(source_bucket + funding_amount_usd), not both.")
    if funding_requested and (source_bucket is None or funding_amount is None):
        raise ValueError("Partial funding needs both source_bucket and funding_amount_usd.")
    all_value = sum(lot.value_usd for lot in lots)
    if include_dry_powder:
        kept, dry = list(lots), split_dry_powder(lots, settings.dry_powder)[1]
    else:
        kept, dry = split_dry_powder(lots, settings.dry_powder)
    grouped, unassigned = bucket_lots(kept, settings)
    buckets = {bucket.name: bucket for bucket in settings.buckets}
    targets = {bucket.name: bucket.target for bucket in settings.buckets}
    values = {name: sum(lot.value_usd for lot in grouped[name]) for name in buckets}
    liquid = {name: sum(lot.value_usd for lot in grouped[name] if lot.liquid) for name in buckets}
    total = sum(values.values())
    tol = settings.tolerance_pp
    stable_sink = next((b.name for b in settings.buckets if b.stablecoins), None)
    known_prices = {s: p for s, (_, p) in _symbol_prices(lots).items()}

    rows = []
    breached = False
    for bucket in settings.buckets:
        name = bucket.name
        actual = values[name] / total * 100 if total > 0 else None
        drift = actual - bucket.target if actual is not None else None
        breach = drift is not None and abs(drift) > tol + 1e-9
        breached = breached or breach
        locked = [lot for lot in grouped[name] if not lot.liquid]
        rows.append({
            "name": name,
            "target_pct": round(bucket.target, 4),
            "actual_pct": round(actual, 4) if actual is not None else None,
            "drift_pp": round(drift, 4) if drift is not None else None,
            "breach": breach,
            "direction": None if not breach else ("over" if drift > 0 else "under"),
            "band": [round(max(bucket.target - tol, 0.0), 4), round(min(bucket.target + tol, 100.0), 4)],
            "value_usd": round(values[name], 2),
            "liquid_usd": round(liquid[name], 2),
            "locked_usd": round(values[name] - liquid[name], 2),
            "locked_reasons": sorted({lot.lock_reason for lot in locked}),
            "symbols": sorted({lot.symbol for lot in grouped[name]}),
            "catch_all": bucket.catch_all,
            "stablecoin": bucket.stablecoins,
        })

    mode = "funding" if funding_requested else "contribution" if contribution else "rebalance"
    cut: dict[str, float] = {}
    unfilled = 0.0
    funding = None
    if funding_requested:
        deltas, funding = funding_plan(values, targets, liquid, source_bucket, funding_amount,
                                       strategy, settings.min_trade_usd)
        funding["source_is_stablecoin"] = buckets[source_bucket].stablecoins
        if funding["limited_by"] == "liquidity":
            cut[source_bucket] = funding["requested_usd"] - funding["applied_usd"]
    elif contribution:
        raw = contribution_deltas(values, targets, contribution, settings.min_trade_usd)
        deltas = finalize_deltas(raw, 0.0, keep_sum=round(contribution, 2))
    elif breached and total > 0:
        if settings.rebalance_to == "band_edge":
            raw = band_edge_deltas(values, targets, tol)
        else:
            raw = target_deltas(values, targets)
        raw, cut, unfilled = apply_liquidity(raw, values, targets, liquid)
        deltas = finalize_deltas(raw, settings.min_trade_usd, sink=stable_sink)
    else:
        deltas = {}

    actions = []
    for bucket in settings.buckets:  # config order keeps the list stable between refreshes
        usd = deltas.get(bucket.name, 0.0)
        if usd > 0:
            actions.append(buy_order(bucket, usd, grouped[bucket.name], known_prices))
    for bucket in settings.buckets:
        usd = deltas.get(bucket.name, 0.0)
        if usd < 0:
            reason = None
            if bucket.name in cut:
                reasons = {lot.lock_reason for lot in grouped[bucket.name] if not lot.liquid}
                reason = LOCKED_EARN if LOCKED_EARN in reasons else (ILLIQUID if reasons else LOCKED_EARN)
            order = sell_order(bucket, -usd, grouped[bucket.name], reason)
            if funding and bucket.stablecoins and bucket.name == source_bucket:
                order = _as_quote_spend(order)
            actions.append(order)

    # a funding trade moves money between buckets: V does not change (only new money adds to it)
    after_total = total + (contribution or 0.0) if mode == "contribution" else total
    for row in rows:
        after = values[row["name"]] + deltas.get(row["name"], 0.0)
        row["after_pct"] = round(after / after_total * 100, 4) if after_total > 0 else None
        row["order_usd"] = round(deltas.get(row["name"], 0.0), 2)

    # a trade between two non-cash buckets is two trades (sell, then buy); the stablecoin side
    # of a trade is the other half of the same trade, not a trade of its own
    traded = sum(abs(a["usd"]) for a in actions if a["bucket"] != stable_sink)
    dry_items: dict[tuple[str, str], float] = {}
    for lot in dry:
        dry_items[(lot.symbol, lot.source)] = dry_items.get((lot.symbol, lot.source), 0.0) + lot.value_usd
    dry_usd = sum(dry_items.values())
    return {
        "available": total > 0 or bool(contribution),
        "reason": None if total > 0 or contribution else "no_priced_assets",
        "mode": mode,
        "contribution_usd": round(contribution, 2) if contribution and mode == "contribution" else None,
        "funding": funding,
        "rebalance_to": settings.rebalance_to,
        "tolerance_pp": tol,
        "min_trade_usd": settings.min_trade_usd,
        "fee_rate": settings.fee_rate,
        "default_targets": settings.default_targets,
        "total_priced_usd": round(total, 2),
        "portfolio_priced_usd": round(all_value, 2),
        "unpriced_assets": unpriced_assets,
        "n_obs": len(kept),
        "buckets": rows,
        "actions": actions,
        "rebalance_needed": breached,
        "turnover_usd": round(sum(a["usd"] for a in actions if a["action"] == "BUY"), 2),
        "est_fees_usd": round(traded * settings.fee_rate, 2),
        "unfilled_usd": round(unfilled, 2),
        "dry_powder": {
            "usd": round(dry_usd, 2),
            "pct_of_portfolio": round(dry_usd / all_value * 100, 4) if all_value > 0 else None,
            "included": include_dry_powder,
            "configured": bool(settings.dry_powder),
            "items": [{"symbol": s, "source": src, "usd": round(v, 2)}
                      for (s, src), v in sorted(dry_items.items(), key=lambda kv: -kv[1])],
        },
        "unassigned": {
            "usd": round(sum(lot.value_usd for lot in unassigned), 2),
            "symbols": sorted({lot.symbol for lot in unassigned}),
        },
    }


# -- loader -------------------------------------------------------------------------------
def lots_from_rows(rows: list[dict], manual_assets: list[ManualAsset] = ()) -> tuple[list[Lot], int]:
    """`snapshot_assets` rows -> priced lots, and the number of unpriced symbols.

    Liquidity comes from the row itself (`binance_earn` / `locked`) and from config.yaml for
    manual holdings (`liquid: false`).
    """
    illiquid = {(asset.label, holding.symbol) for asset in manual_assets
                for holding in asset.holdings if not holding.liquid}
    lots: list[Lot] = []
    unpriced: set[str] = set()
    for row in rows:
        quantity = float(row.get("quantity") or 0.0)
        price = float(row.get("price_usd") or 0.0)
        if quantity <= 0:
            continue
        symbol = str(row["symbol"]).upper()
        if price <= 0:
            unpriced.add(symbol)
            continue
        source = row["source"]
        detail = row.get("detail") or ""
        reason = None
        if source == BINANCE_EARN and detail == "locked":
            reason = LOCKED_EARN
        elif source == MANUAL and (detail, symbol) in illiquid:
            reason = ILLIQUID
        lots.append(Lot(symbol=symbol, source=source, value_usd=quantity * price, price_usd=price,
                        detail=detail, lock_reason=reason))
    priced = {lot.symbol for lot in lots}
    return lots, len(unpriced - priced)


def allocation_report(database, settings: AllocationSettings, manual_assets: list[ManualAsset] = (),
                      *, contribution: float | None = None, source_bucket: str | None = None,
                      funding_amount: float | None = None, strategy: str = "greedy",
                      include_dry_powder: bool = False) -> dict:
    """`allocate` over the newest snapshot in SQLite. Never touches the network."""
    snapshot = database.latest()
    if snapshot is None:
        return {"enabled": True, "available": False, "reason": "no_snapshot", "as_of": None,
                "snapshot_id": None, "buckets": [], "actions": [], "rebalance_needed": False}
    lots, unpriced = lots_from_rows(database.assets(snapshot.id), list(manual_assets))
    report = allocate(lots, settings, contribution=contribution, source_bucket=source_bucket,
                      funding_amount=funding_amount, strategy=strategy,
                      include_dry_powder=include_dry_powder, unpriced_assets=unpriced)
    return {"enabled": True, "as_of": snapshot.timestamp, "snapshot_id": snapshot.id, **report}
