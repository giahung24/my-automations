"""Attribution analysis: why did the net worth move? (ROADMAP_LEDGER Epic F1, §2.2-§2.4)

Between two consecutive snapshots, each position (quantity Q, price P, value V = Q*P) moves by

    dV = Q0*(P1 - P0)      price effect        - the market moved
       + (Q1 - Q0)*P0      quantity effect     - deposits, withdrawals, trades, Earn interest
       + (Q1 - Q0)*(P1-P0) interaction         - reported on its own, never split

and the moves of every position add up to dNAV exactly. Two extra buckets keep artefacts out of
the price effect:

* ``revaluation`` - a price that appears from 0 (a token DefiLlama just started pricing) or
  drops to 0 (a pricing API returned nothing while the source still reported ok). Not a market move.
* ``data_gap`` - the whole dV of a position whose source failed in either snapshot of the pair
  (``snapshot_sources.status`` = error or inferred_error). A dead Arbitrum RPC makes the ETH
  held there vanish for one snapshot; without this bucket it would read as a sale, then a buy.

So ``dNAV = sum(price + quantity + interaction) + revaluation + data_gap``, checked on every run.

Grain: effects are computed per ``(symbol, source_key)``, where ``source_key`` is the
``snapshot_sources`` key of the row (``binance_spot``, ``binance_earn``, ``onchain:<chain>/<wallet>``).
Each source has its own price feed (Binance ticker vs DefiLlama), so computing at ``symbol`` grain
would turn a transfer between two sources into a fake price effect. Effects are additive, so they
are summed up to the requested ``group_by`` afterwards; a Spot -> Earn transfer shows as two opposite
quantity effects that cancel at ``symbol`` grain.

Multi-step windows: ``chained`` (default) applies the one-step split to every consecutive pair
in the window and sums; ``endpoint`` applies it once between the first and the last snapshot.

`attribute()` is pure (no I/O). `load_states()` and `window_snapshot_ids()` read SQLite;
`attribution_report()` glues them into the JSON served by ``GET /api/portfolio/attribution``.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from models import ONCHAIN, STATUS_ERROR, STATUS_INFERRED_ERROR, is_onchain_source, onchain_source
from services.db import Database, Snapshot, parse_timestamp

log = logging.getLogger(__name__)

METHOD_CHAINED = "chained"
METHOD_ENDPOINT = "endpoint"
METHODS = (METHOD_CHAINED, METHOD_ENDPOINT)

GROUP_SYMBOL = "symbol"
GROUP_SOURCE = "source"
GROUP_SYMBOL_SOURCE = "symbol_source"
GROUP_BY = (GROUP_SYMBOL, GROUP_SOURCE, GROUP_SYMBOL_SOURCE)

PRICE, QUANTITY, INTERACTION, REVALUATION, DATA_GAP = (
    "price", "quantity", "interaction", "revaluation", "data_gap")
BUCKETS = (PRICE, QUANTITY, INTERACTION, REVALUATION, DATA_GAP)

# Statuses that make a pair "degraded" for that source (ledger §1.2).
DEGRADED_STATUSES = (STATUS_ERROR, STATUS_INFERRED_ERROR)

# |sum(buckets) - dNAV| must stay under this fraction of max(1, NAV) (ledger §2.3 step 6).
IDENTITY_TOLERANCE = 1e-6

# A contributor must move at least this much (half a displayed cent) to be listed as a gainer/loser.
MIN_CONTRIBUTION_USD = 0.005

# How well the snapshots really cover the window that was asked for.
COVERAGE_FULL, COVERAGE_PARTIAL, COVERAGE_STRETCHED = "full", "partial", "stretched"
# The real span may differ from the requested one by less than this fraction and still be "full".
COVERAGE_TOLERANCE = 0.15

WINDOW_RE = re.compile(r"^(\d{1,3})([hd])$")
MAX_WINDOW_HOURS = 365 * 24
DEFAULT_TOP = 5

# SQLite builds before 3.32 cap a statement at 999 bound variables.
_SQL_CHUNK = 500


# -- data ------------------------------------------------------------------------
@dataclass(frozen=True)
class Position:
    """Total quantity and value of one ``(symbol, source_key)`` in one snapshot."""

    quantity: float
    value: float

    @property
    def price(self) -> float:
        """Value-weighted price ``V / Q`` (0 when nothing is held)."""
        return self.value / self.quantity if self.quantity > 0 else 0.0


@dataclass
class SnapshotState:
    """Everything attribution needs from one snapshot.

    ``positions`` maps ``(symbol, source_key)`` to a Position; ``degraded`` holds the
    ``snapshot_sources`` keys whose fetch failed in this snapshot.
    """

    snapshot_id: int
    timestamp: str
    nav: float
    positions: dict[tuple[str, str], Position] = field(default_factory=dict)
    degraded: frozenset[str] = frozenset()

    def is_degraded(self, source_key: str) -> bool:
        """Whether rows of `source_key` are unreliable in this snapshot.

        A failure recorded on the whole on-chain half (``onchain``, as the inferred-hole marks of
        old snapshots are) covers every wallet; an on-chain row without a wallet (``onchain``,
        written before P0.3) cannot be pinned to one, so any wallet failure covers it.
        """
        degraded = self.degraded
        if not degraded:
            return False
        if source_key in degraded:
            return True
        if is_onchain_source(source_key):
            if ONCHAIN in degraded:
                return True
            if source_key == ONCHAIN:
                return any(is_onchain_source(s) for s in degraded)
        return False


@dataclass
class Attribution:
    """Result of `attribute()`; amounts in USD."""

    method: str
    group_by: str
    start: SnapshotState
    end: SnapshotState
    snapshots: int                          # states the window spans (both ends included)
    buckets: dict[str, float]               # bucket -> amount; sums to delta_usd
    contributors: list[dict]                # one per group, sorted by total, gainers first
    degraded_pairs: int                     # pairs where at least one position went to data_gap
    identity_error: dict | None = None      # set when sum(buckets) != delta_usd

    @property
    def delta_usd(self) -> float:
        return self.end.nav - self.start.nav

    @property
    def delta_pct(self) -> float | None:
        return self.delta_usd / self.start.nav * 100 if self.start.nav > 0 else None


def position_key(symbol: str, source: str, detail: str = "") -> tuple[str, str]:
    """``(symbol, source_key)`` of a `snapshot_assets` row.

    On-chain rows are keyed by wallet (``onchain:<chain>/<label>``, the `snapshot_sources` key),
    so a failed RPC can be matched to the rows it emptied; Binance rows by their source alone
    (Earn ``flexible`` and ``locked`` rows share one price, they merge).
    """
    if source == ONCHAIN and detail:
        return symbol, onchain_source(detail)
    return symbol, source


def base_source(source_key: str) -> str:
    """``onchain:<chain>/<label>`` -> ``onchain``; Binance keys are unchanged."""
    return ONCHAIN if is_onchain_source(source_key) else source_key


def group_key(key: tuple[str, str], group_by: str) -> str:
    symbol, source_key = key
    if group_by == GROUP_SYMBOL:
        return symbol
    if group_by == GROUP_SOURCE:
        return base_source(source_key)
    return f"{symbol}@{base_source(source_key)}"


# -- engine ----------------------------------------------------------------------
def decompose(before: Position | None, after: Position | None) -> dict[str, float]:
    """Split the value change of one position between two snapshots into buckets.

    The last term of every branch is computed as the remainder of dV, so the buckets add up to
    ``after.value - before.value`` exactly, whatever the rounding of the stored prices.
    Edge cases (ledger §2.2.4):

    * new position (Q0 = 0): P0 := P1, so price = interaction = 0 and all of dV is quantity;
    * closed position (Q1 = 0): P1 := P0, all of dV is quantity;
    * P0 = 0 < P1 (just got a price): the value of the units already held is ``revaluation``,
      the units added are quantity at P1;
    * P0 > 0 = P1 (lost its price): the value of the units still held is ``revaluation``,
      the units added or removed are quantity at P0;
    * no units on either side: whatever value is left can only be a pricing artefact.
    """
    q0, v0 = (before.quantity, before.value) if before else (0.0, 0.0)
    q1, v1 = (after.quantity, after.value) if after else (0.0, 0.0)
    delta = v1 - v0
    if q0 <= 0 and q1 <= 0:
        return {REVALUATION: delta} if delta else {}
    if q0 <= 0 or q1 <= 0:
        return {QUANTITY: delta}
    p0, p1 = v0 / q0, v1 / q1
    if p0 <= 0 and p1 <= 0:
        return {REVALUATION: delta} if delta else {}
    if p0 <= 0:
        quantity = (q1 - q0) * p1
        return {QUANTITY: quantity, REVALUATION: delta - quantity}
    if p1 <= 0:
        quantity = (q1 - q0) * p0
        return {QUANTITY: quantity, REVALUATION: delta - quantity}
    price = q0 * (p1 - p0)
    quantity = (q1 - q0) * p0
    return {PRICE: price, QUANTITY: quantity, INTERACTION: delta - price - quantity}


def attribute(states: Sequence[SnapshotState], method: str = METHOD_CHAINED,
              group_by: str = GROUP_SYMBOL) -> Attribution:
    """Attribute ``states[-1].nav - states[0].nav`` to buckets and groups. Pure, no I/O.

    `states` must be ordered oldest first and hold at least two snapshots. With ``endpoint``
    only the first and the last are compared; with ``chained`` every consecutive pair is.
    """
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}; expected one of {METHODS}")
    if group_by not in GROUP_BY:
        raise ValueError(f"unknown group_by {group_by!r}; expected one of {GROUP_BY}")
    if len(states) < 2:
        raise ValueError("attribution needs at least two snapshots")

    start, end = states[0], states[-1]
    pairs = [(start, end)] if method == METHOD_ENDPOINT else list(zip(states, states[1:]))

    effects: dict[str, dict[str, float]] = defaultdict(lambda: dict.fromkeys(BUCKETS, 0.0))
    degraded_pairs = 0
    for before, after in pairs:
        pair_degraded = False
        for key in before.positions.keys() | after.positions.keys():
            old, new = before.positions.get(key), after.positions.get(key)
            source_key = key[1]
            if before.is_degraded(source_key) or after.is_degraded(source_key):
                split = {DATA_GAP: (new.value if new else 0.0) - (old.value if old else 0.0)}
                pair_degraded = pair_degraded or bool(split[DATA_GAP])
            else:
                split = decompose(old, new)
            target = effects[group_key(key, group_by)]
            for bucket, amount in split.items():
                target[bucket] += amount
        degraded_pairs += pair_degraded

    buckets = {bucket: sum(e[bucket] for e in effects.values()) for bucket in BUCKETS}
    contributors = _contributors(effects, start, end, group_by)

    expected = end.nav - start.nav
    attributed = sum(buckets.values())
    tolerance = IDENTITY_TOLERANCE * max(1.0, abs(start.nav), abs(end.nav))
    identity_error = None
    if abs(attributed - expected) > tolerance:
        identity_error = {
            "expected_usd": expected,
            "attributed_usd": attributed,
            "difference_usd": attributed - expected,
        }
        log.error("attribution identity broken between snapshots %s and %s: buckets sum to %.6f, "
                  "NAV moved %.6f (the stored totals disagree with the asset rows)",
                  start.snapshot_id, end.snapshot_id, attributed, expected)

    return Attribution(
        method=method, group_by=group_by, start=start, end=end, snapshots=len(states),
        buckets=buckets, contributors=contributors, degraded_pairs=degraded_pairs,
        identity_error=identity_error,
    )


def _contributors(effects: dict[str, dict[str, float]], start: SnapshotState, end: SnapshotState,
                  group_by: str) -> list[dict]:
    """One row per group with its buckets, total, share of the move, start weight, price change."""
    def holdings(state: SnapshotState) -> dict[str, list[float]]:
        grouped: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for key, position in state.positions.items():
            entry = grouped[group_key(key, group_by)]
            entry[0] += position.quantity
            entry[1] += position.value
        return grouped

    first, last = holdings(start), holdings(end)
    totals = {group: sum(values.values()) for group, values in effects.items()}
    gross = sum(abs(total) for total in totals.values())
    rows = []
    for group, values in effects.items():
        q0, v0 = first.get(group, (0.0, 0.0))
        q1, v1 = last.get(group, (0.0, 0.0))
        price_change = None
        if group_by != GROUP_SOURCE and q0 > 0 and q1 > 0 and v0 > 0 and v1 > 0:
            price_change = ((v1 / q1) / (v0 / q0) - 1) * 100
        rows.append({
            "key": group,
            **values,
            "total": totals[group],
            # sum of |totals| so the share means something even when gains and losses cancel out
            "share_of_move": totals[group] / gross if gross > 0 else 0.0,
            "weight_start": v0 / start.nav if start.nav > 0 else None,
            "price_change_pct": price_change,
        })
    rows.sort(key=lambda row: (-row["total"], row["key"]))
    return rows


# -- loading ---------------------------------------------------------------------
def load_states(database: Database, ids: Iterable[int]) -> list[SnapshotState]:
    """`SnapshotState`s of the snapshots `ids`, in the order given (unknown ids are skipped).

    Rows are summed per ``(symbol, source_key)``: ETH on three chains of one wallet stays three
    keys, the ``flexible`` and ``locked`` Earn rows of a coin become one.
    """
    ids = list(dict.fromkeys(int(i) for i in ids))
    if not ids:
        return []
    navs: dict[int, tuple[str, float]] = {}
    positions: dict[int, dict[tuple[str, str], list[float]]] = defaultdict(
        lambda: defaultdict(lambda: [0.0, 0.0]))
    degraded: dict[int, set[str]] = defaultdict(set)
    with database.connect() as connection:
        for chunk in _chunks(ids, _SQL_CHUNK):
            marks = ",".join("?" * len(chunk))
            for row in connection.execute(
                    f"SELECT id, timestamp, total_portfolio_usd FROM snapshots WHERE id IN ({marks})", chunk):
                navs[int(row["id"])] = (row["timestamp"], float(row["total_portfolio_usd"]))
            for row in connection.execute(
                    f"""SELECT snapshot_id, symbol, source, detail,
                               SUM(quantity) AS q, SUM(value_usd) AS v
                        FROM snapshot_assets WHERE snapshot_id IN ({marks})
                        GROUP BY snapshot_id, symbol, source, detail""", chunk):
                entry = positions[int(row["snapshot_id"])][
                    position_key(row["symbol"], row["source"], row["detail"] or "")]
                entry[0] += float(row["q"] or 0.0)
                entry[1] += float(row["v"] or 0.0)
            for row in connection.execute(
                    f"SELECT snapshot_id, source FROM snapshot_sources "
                    f"WHERE snapshot_id IN ({marks}) AND status IN (?, ?)",
                    (*chunk, *DEGRADED_STATUSES)):
                degraded[int(row["snapshot_id"])].add(row["source"])

    return [
        SnapshotState(
            snapshot_id=snapshot_id,
            timestamp=navs[snapshot_id][0],
            nav=navs[snapshot_id][1],
            positions={key: Position(q, v) for key, (q, v) in positions[snapshot_id].items()},
            degraded=frozenset(degraded[snapshot_id]),
        )
        for snapshot_id in ids if snapshot_id in navs
    ]


def reference_snapshot(database: Database, cutoff: datetime, end: datetime) -> Snapshot | None:
    """The fetched snapshot a window ending at `end` starts from: the one closest to `cutoff`.

    Two candidates: the newest snapshot at or before the cutoff and the oldest one after it
    (strictly older than `end`, a window cannot start where it ends). With both, the nearer to
    the cutoff wins and a tie goes to the earlier one, which covers the whole window. Taking the
    earlier one unconditionally would let a hole in the history turn "24h" into a 10-day figure
    while a snapshot 23h old sits right there. `change_since` (the Portfolio chip) uses this too,
    so both tabs describe the same window.
    """
    stamp = _utc_iso(cutoff)
    before = database.snapshot_before(stamp)
    after = database.snapshot_after(stamp)
    if after is not None and after.dt >= end:
        after = None
    if before is None or after is None:
        return before or after
    return after if after.dt - cutoff < cutoff - before.dt else before


def coverage_of(actual_hours: float, requested_hours: float) -> str:
    """``full`` when the real span is within 15% of the requested one, ``partial`` when it is
    shorter (young history, or a hole at the cutoff), ``stretched`` when it is longer (the only
    reference is older than the window)."""
    if abs(actual_hours - requested_hours) < COVERAGE_TOLERANCE * requested_hours:
        return COVERAGE_FULL
    return COVERAGE_PARTIAL if actual_hours < requested_hours else COVERAGE_STRETCHED


def window_snapshot_ids(database: Database, hours: float, method: str = METHOD_CHAINED) -> list[int]:
    """Ids of the fetched snapshots covering the last `hours`, oldest first.

    The window ends at the newest snapshot and starts at `reference_snapshot()`: the snapshot
    closest to the cutoff, on either side of it. ``endpoint`` returns those two ids only. Fewer
    than two ids means there is nothing to compare. Backfilled monthly checkpoints are never
    used: they were not fetched and only hold BTC/ETH/stablecoins.
    """
    latest = database.latest()
    if latest is None:
        return []
    reference = reference_snapshot(database, latest.dt - timedelta(hours=hours), latest.dt)
    if reference is None or reference.id == latest.id or reference.dt > latest.dt:
        return [latest.id]
    if method == METHOD_ENDPOINT:
        return [reference.id, latest.id]
    with database.connect() as connection:
        ids = [int(row["id"]) for row in connection.execute(
            "SELECT id FROM snapshots WHERE is_backfilled = 0 AND timestamp >= ? AND timestamp <= ? "
            "ORDER BY timestamp, id",
            (reference.timestamp, latest.timestamp),
        )]
    first, last = ids.index(reference.id), ids.index(latest.id)
    return ids[first:last + 1] if first < last else [reference.id, latest.id]


def parse_window(window: str) -> float:
    """``24h`` / ``7d`` -> hours. Raises ValueError outside ``^\\d{1,3}[hd]$``, 0, or over 365d."""
    match = WINDOW_RE.match(str(window or "").strip())
    if not match:
        raise ValueError(f"window must look like 24h or 7d, got {window!r}")
    amount, unit = int(match.group(1)), match.group(2)
    hours = amount * 24 if unit == "d" else amount
    if hours <= 0:
        raise ValueError("window must be longer than 0")
    if hours > MAX_WINDOW_HOURS:
        raise ValueError("window can be at most 365d")
    return float(hours)


# -- report ----------------------------------------------------------------------
def attribution_report(database: Database, window: str = "24h", method: str = METHOD_CHAINED,
                       group_by: str = GROUP_SYMBOL, top: int = DEFAULT_TOP) -> dict:
    """The ``GET /api/portfolio/attribution`` payload (ledger §2.4). Reads SQLite only.

    Raises ValueError on a bad window, method or group_by. With fewer than two snapshots in the
    window the payload says ``"insufficient_history": true`` instead of attributing anything.
    """
    hours = parse_window(window)
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}")
    if group_by not in GROUP_BY:
        raise ValueError(f"unknown group_by {group_by!r}")
    top = max(1, int(top))

    ids = window_snapshot_ids(database, hours, method)
    states = load_states(database, ids) if len(ids) >= 2 else []
    if len(states) < 2:
        return {
            "window": window, "method": method, "group_by": group_by,
            "insufficient_history": True, "snapshots": len(ids),
        }

    result = attribute(states, method=method, group_by=group_by)
    contributors = [_rounded(row) for row in result.contributors]
    gainers = [row for row in contributors if row["total"] >= MIN_CONTRIBUTION_USD][:top]
    losers = sorted((row for row in contributors if row["total"] <= -MIN_CONTRIBUTION_USD),
                    key=lambda row: (row["total"], row["key"]))[:top]
    listed = {row["key"] for row in gainers} | {row["key"] for row in losers}
    rest = [row for row in result.contributors if row["key"] not in listed]
    span = (parse_timestamp(result.end.timestamp) - parse_timestamp(result.start.timestamp)).total_seconds()
    oldest = database.snapshot_after("")  # oldest fetched snapshot: when live tracking started

    return {
        "window": window,
        "insufficient_history": False,
        "from": result.start.timestamp,
        "to": result.end.timestamp,
        "hours": round(span / 3600, 2),
        "requested_hours": _whole(hours),
        "coverage": coverage_of(span / 3600, hours),
        # equal to "from" when the window is short because tracking started inside it
        "history_start": oldest.timestamp if oldest else None,
        "snapshots": result.snapshots,
        "method": method,
        "group_by": group_by,
        "nav_start": _round(result.start.nav, 2),
        "nav_end": _round(result.end.nav, 2),
        "delta_usd": _round(result.delta_usd, 2),
        "delta_pct": _round(result.delta_pct, 4) if result.delta_pct is not None else None,
        "buckets": {bucket: _round(amount, 2) for bucket, amount in result.buckets.items()},
        "contributors": contributors,
        "top_gainers": [row["key"] for row in gainers],
        "top_losers": [row["key"] for row in losers],
        "gainers": gainers,
        "losers": losers,
        "others": {
            "count": sum(1 for row in rest if abs(row["total"]) >= MIN_CONTRIBUTION_USD),
            "total": _round(sum(row["total"] for row in rest), 2),
        },
        "degraded_pairs": result.degraded_pairs,
        "identity_error": ({k: _round(v, 6) for k, v in result.identity_error.items()}
                           if result.identity_error else None),
    }


def _round(value: float, digits: int) -> float:
    """`round()` without the `-0.0` a tiny negative remainder would otherwise print."""
    return round(value, digits) + 0.0


def _whole(hours: float) -> float | int:
    """24.0 -> 24, so the JSON reads like the window that was asked for."""
    return int(hours) if float(hours).is_integer() else hours


def _rounded(row: dict) -> dict:
    out = dict(row)
    for bucket in (*BUCKETS, "total"):
        out[bucket] = _round(out[bucket], 2)
    out["share_of_move"] = _round(out["share_of_move"], 4)
    for name in ("weight_start", "price_change_pct"):
        if out[name] is not None:
            out[name] = _round(out[name], 4)
    return out


def _chunks(items: list, size: int) -> Iterable[list]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _utc_iso(moment: datetime) -> str:
    """Same shape as `utc_now_iso()`, so string comparison in SQLite orders correctly."""
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()
