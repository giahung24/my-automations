"""Real profit and loss over 7d / 30d: the NAV change minus net deposits and withdrawals.

    PnL = (NAV_end - NAV_start) - NetFlow,    NetFlow = deposits_in - withdrawals_out

NetFlow sums the **external** cash flows (``services/cash_flows.py``) with
``start < timestamp <= end``: completed deposits / withdrawals that are not internal transfers.
An internal transfer (Binance -> an owned wallet) moves value inside the portfolio, so it is not
a flow; the fee it cost stays in the PnL, where a real loss belongs. A pending flow is not counted
yet. Each flow is valued at its price when it happened (``cash_flows.value_usd``).

The return is Modified Dietz, so money added late in the window weighs less than money that was
there from the start:

    return = PnL / (NAV_start + sum(F_i * w_i)),    w_i = (t_end - t_i) / (t_end - t_start)

The window ends at the newest fetched snapshot and starts at the newest fetched snapshot at or
before ``end - days`` (else the oldest one after it, with ``complete = false``) - the same anchors
as the attribution engine. Backfilled monthly checkpoints are never used.

Before any Binance history was synced, the flows are unknown, not zero: the payload then says
``flow_adjusted = false`` and the PnL equals the raw NAV change (ROADMAP_LEDGER §4, "F_j = 0") -
except for manual-asset quantity changes (Epic M6), which are recorded by the collector itself
and so are always known: they are subtracted either way.

`flow_adjusted_pnl()` is pure; `pnl_report()` reads SQLite and builds the ``GET /api/portfolio/pnl``
payload.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from models import MANUAL
from services.cash_flows import COMPLETED, CRYPTO_STREAMS, EXTERNAL_FLOW_TYPES, IN, flows_between, stream_key
from services.db import Database, Snapshot, parse_timestamp

DEFAULT_WINDOWS = (7, 30)
MAX_WINDOW_DAYS = 365


@dataclass(frozen=True)
class Flow:
    """One external cash flow: when, and its signed USD value (+in / -out)."""

    timestamp: datetime
    amount_usd: float


@dataclass(frozen=True)
class PnlResult:
    nav_start_usd: float
    nav_end_usd: float
    nav_change_usd: float
    deposits_usd: float
    withdrawals_usd: float
    net_flow_usd: float
    pnl_usd: float
    return_pct: float | None

    def to_dict(self) -> dict:
        return {
            "nav_start_usd": round(self.nav_start_usd, 2),
            "nav_end_usd": round(self.nav_end_usd, 2),
            "nav_change_usd": round(self.nav_change_usd, 2),
            "deposits_usd": round(self.deposits_usd, 2),
            "withdrawals_usd": round(self.withdrawals_usd, 2),
            "net_flow_usd": round(self.net_flow_usd, 2),
            "pnl_usd": round(self.pnl_usd, 2),
            "return_pct": None if self.return_pct is None else round(self.return_pct, 4),
        }


def flow_adjusted_pnl(nav_start: float, nav_end: float, start: datetime, end: datetime,
                      flows: Iterable[Flow]) -> PnlResult:
    """PnL and Modified Dietz return of one window. Pure: no I/O.

    `flows` must already be the external flows of ``(start, end]``; their signs are
    +deposit / -withdrawal. The return is None when the capital base is not positive.
    """
    flows = list(flows)
    deposits = sum(f.amount_usd for f in flows if f.amount_usd > 0)
    withdrawals = -sum(f.amount_usd for f in flows if f.amount_usd < 0)
    net_flow = deposits - withdrawals
    nav_change = nav_end - nav_start
    pnl = nav_change - net_flow

    duration = (end - start).total_seconds()
    weighted = 0.0
    for flow in flows:
        weight = (end - flow.timestamp).total_seconds() / duration if duration > 0 else 0.0
        weighted += flow.amount_usd * min(max(weight, 0.0), 1.0)
    base = nav_start + weighted
    return_pct = pnl / base * 100 if base > 0 else None
    return PnlResult(nav_start, nav_end, nav_change, deposits, withdrawals, net_flow, pnl, return_pct)


# -- loaders ---------------------------------------------------------------------------
def flow_tracking(database: Database) -> dict:
    """Whether crypto deposits *and* withdrawals have been synced, and up to when.

    ``synced_until`` is the older of the two cursors: flows after it are not known yet.
    """
    with database.connect() as connection:
        rows = connection.execute("SELECT key, value FROM sync_state").fetchall()
    cursors = {row["key"]: int(row["value"]) for row in rows if str(row["value"]).isdigit()}
    required = [stream_key(stream) for stream in CRYPTO_STREAMS]
    tracked = all(key in cursors for key in required)
    until = min(cursors[key] for key in required) if tracked else None
    return {
        "enabled": tracked,
        "synced_until": _iso(datetime.fromtimestamp(until / 1000, timezone.utc)) if until else None,
        "streams": {key: _iso(datetime.fromtimestamp(value / 1000, timezone.utc))
                    for key, value in sorted(cursors.items())},
    }


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _snapshot_dict(snapshot: Snapshot) -> dict:
    return {"snapshot_id": snapshot.id, "timestamp": snapshot.timestamp,
            "total_usd": round(snapshot.total_portfolio_usd, 2)}


def _signed(row: dict) -> float:
    value = float(row.get("value_usd") or 0.0)
    return value if row.get("direction") == IN else -value


def window_pnl(database: Database, days: int, latest: Snapshot | None = None,
               flow_adjusted: bool = True) -> dict:
    """The PnL of the last `days` days ending at the newest fetched snapshot."""
    if not 0 < int(days) <= MAX_WINDOW_DAYS:
        raise ValueError(f"window must be between 1 and {MAX_WINDOW_DAYS} days, got {days}")
    days = int(days)
    latest = latest or database.latest()
    payload = {"window": f"{days}d", "days": days}
    if latest is None:
        return {**payload, "available": False, "reason": "no snapshot recorded yet"}
    cutoff = latest.dt - timedelta(days=days)
    reference = database.snapshot_before(_iso(cutoff)) or database.snapshot_after(_iso(cutoff))
    if reference is None or reference.id == latest.id or reference.dt >= latest.dt:
        return {**payload, "available": False, "reason": "need two snapshots in the window",
                "end": _snapshot_dict(latest)}

    rows = flows_between(database, reference.timestamp, latest.timestamp)
    if not flow_adjusted:  # only the flows the collector records itself are known
        rows = [row for row in rows if row["source"] == MANUAL]
    external = [row for row in rows if row["status"] == COMPLETED and not row["is_internal_transfer"]
                and row["flow_type"] in EXTERNAL_FLOW_TYPES]
    flows = [Flow(parse_timestamp(row["timestamp"]), _signed(row)) for row in external]
    result = flow_adjusted_pnl(reference.total_portfolio_usd, latest.total_portfolio_usd,
                               reference.dt, latest.dt, flows)
    return {
        **payload,
        "available": True,
        "complete": reference.dt <= cutoff,  # false: history shorter than the window
        "start": _snapshot_dict(reference),
        "end": _snapshot_dict(latest),
        **result.to_dict(),
        "flow_count": len(external),
        "internal_transfer_count": sum(1 for row in rows if row["is_internal_transfer"]),
        "pending_count": sum(1 for row in rows if row["status"] == "pending"
                             and not row["is_internal_transfer"]),
        "unpriced_count": sum(1 for row in external if float(row["value_usd"] or 0) <= 0),
    }


def pnl_report(database: Database, windows: Sequence[int] = DEFAULT_WINDOWS) -> dict:
    """The ``GET /api/portfolio/pnl`` payload: real PnL for each window (default 7d and 30d)."""
    tracking = flow_tracking(database)
    latest = database.latest()
    stale = bool(latest and tracking["synced_until"]
                 and parse_timestamp(tracking["synced_until"]) < latest.dt)
    return {
        "as_of": latest.timestamp if latest else None,
        "flow_adjusted": tracking["enabled"],
        "flows_synced_until": tracking["synced_until"],
        "flows_stale": stale,
        "sync_state": tracking["streams"],
        "windows": {f"{int(days)}d": window_pnl(database, days, latest, tracking["enabled"])
                    for days in windows},
    }
