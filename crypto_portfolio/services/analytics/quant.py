"""Quantitative intelligence: TWR, Base-100 benchmark vs BTC/ETH, Max Drawdown, volatility.

ROADMAP_LEDGER §4.1-§4.4 (tasks F3.S1, F3.T1, F3.B3, F3.R1, F3.R2), in one module.

Daily series (§4.1)
    ``close_d`` = the newest **healthy** snapshot of UTC day ``d`` (no source in ``error`` /
    ``inferred_error``, P0.2). A day without one is a gap: the return between the two valid
    closes around it is kept as **one** observation with ``gap_days > 1``.

Flow-adjusted daily return (Modified Dietz, §4.1.2)
    r_d = (V_d - V_{d-1} - sum F_j) / (V_{d-1} + sum w_j F_j),   w_j = (t_d - t_j) / (t_d - t_{d-1})

    ``F_j`` are the external flows (signed, +deposit / -withdrawal) of ``(t_{d-1}, t_d]``. A base
    of at most $1 or 1% of ``V_{d-1}`` (almost everything withdrawn) makes ``r_d`` meaningless:
    it is ``None`` with ``unstable = true``. Before any flow history was synced the flows are
    unknown, so ``F_j = 0`` and the payload says ``flow_adjusted = false``.

TWR index (§4.1.3, §4.2.1)
    I_0 = 100, I_d = I_{d-1} (1 + r_d);  TWR = I_T / I_0 - 1.
    Annualised only when the period spans at least 365 days (GIPS).
    Deposits and withdrawals do not move I, so a withdrawal never reads as a drawdown.

Base 100 vs BTC / ETH (§4.3.2)
    B_d = 100 * P_d / P_s on the same UTC-day calendar as the portfolio, starting at the first
    day both have a value. Benchmark prices come from the ``snapshot_assets`` rows of each close
    snapshot (Binance ticker / DefiLlama prices recorded with the portfolio), so the endpoint
    never touches the network; a benchmark the portfolio never priced is ``available = false``.

Max Drawdown & underwater (§4.4.1)
    HWM_d = max_{s<=d} I_s,  DD_d = I_d / HWM_d - 1,  MDD = min DD_d  - on I, never on the raw NAV.

Annualised volatility (§4.4.2)
    sigma_annual = std(r, ddof=1) * sqrt(365), over the 1-day, stable observations only
    (gap and unstable observations are excluded). ``sufficient = n_obs >= min_observations``.

Every engine is a pure function over plain data; ``load_*`` and ``quant_report()`` read SQLite.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import numpy as np

from models import MANUAL, STATUS_ERROR, STATUS_INFERRED_ERROR
from services.analytics.pnl import Flow, flow_tracking
from services.cash_flows import COMPLETED, EXTERNAL_FLOW_TYPES, IN, flows_between
from services.db import Database, Snapshot, parse_timestamp

BASE = 100.0
ANNUALIZATION_DAYS = 365
MIN_OBSERVATIONS = 30
UNSTABLE_BASE_USD = 1.0       # Modified Dietz base at or under $1 ...
UNSTABLE_BASE_RATIO = 0.01    # ... or 1% of V_{d-1}: r_d is not meaningful
BENCHMARKS = ("BTC", "ETH")
# Wrapped / bridged tokens that carry the benchmark's price when the plain symbol is not held.
BENCHMARK_ALIASES = {"BTC": ("BTC", "WBTC", "BTCB"), "ETH": ("ETH", "WETH", "BETH")}

PERIODS = ("7d", "30d", "90d", "180d", "1y", "all")
_PERIOD_RE = re.compile(r"^(?:(\d{1,4})d|(\d{1,2})y|all)$")
MAX_PERIOD_DAYS = 3650

DEGRADED_STATUSES = (STATUS_ERROR, STATUS_INFERRED_ERROR)


# -- data ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Close:
    """The NAV close of one UTC day: the snapshot chosen for it."""

    day: date
    timestamp: datetime
    nav: float
    snapshot_id: int | None = None


@dataclass(frozen=True)
class Observation:
    """One flow-adjusted return between two consecutive closes, ending on `day`."""

    day: date
    r: float | None            # None when the Modified Dietz base is not meaningful
    gap_days: int = 1          # calendar days spanned (> 1 after a missing day)
    unstable: bool = False
    net_flow_usd: float = 0.0

    @property
    def usable_for_risk(self) -> bool:
        """σ assumes one observation per day: gaps and unstable points are left out (§4.4)."""
        return self.r is not None and self.gap_days == 1 and not self.unstable


@dataclass
class Series:
    """Daily closes, their returns and the TWR index ``I`` (``index[0]`` = 100 at ``closes[0]``)."""

    closes: list[Close]
    observations: list[Observation] = field(default_factory=list)
    index: list[float] = field(default_factory=list)

    @property
    def days(self) -> list[date]:
        return [close.day for close in self.closes]


# -- pure engines ---------------------------------------------------------------------
def daily_closes(snapshots: Iterable[tuple[datetime, float, int | None]],
                 degraded: Iterable[int] = ()) -> list[Close]:
    """Newest healthy snapshot of each UTC day, oldest day first.

    `snapshots` are ``(timestamp, nav, snapshot_id)``; ids in `degraded` (a source failed) are
    never picked, so a day whose only snapshots are degraded becomes a gap.
    """
    skip = set(degraded)
    by_day: dict[date, Close] = {}
    for moment, nav, snapshot_id in snapshots:
        if snapshot_id is not None and snapshot_id in skip:
            continue
        moment = moment.astimezone(timezone.utc)
        day = moment.date()
        current = by_day.get(day)
        if current is None or moment >= current.timestamp:
            by_day[day] = Close(day, moment, float(nav), snapshot_id)
    return [by_day[day] for day in sorted(by_day)]


def modified_dietz(nav_start: float, nav_end: float, start: datetime, end: datetime,
                   flows: Iterable[Flow]) -> tuple[float | None, float]:
    """``(r, net_flow)`` of one step; ``r`` is None when the capital base is not meaningful."""
    flows = list(flows)
    net = sum(flow.amount_usd for flow in flows)
    duration = (end - start).total_seconds()
    weighted = 0.0
    for flow in flows:
        weight = (end - flow.timestamp).total_seconds() / duration if duration > 0 else 0.0
        weighted += flow.amount_usd * min(max(weight, 0.0), 1.0)
    base = nav_start + weighted
    if base <= UNSTABLE_BASE_USD or base <= UNSTABLE_BASE_RATIO * nav_start:
        return None, net
    return (nav_end - nav_start - net) / base, net


def daily_returns(closes: Sequence[Close], flows: Iterable[Flow] = ()) -> list[Observation]:
    """Flow-adjusted return of each step between consecutive closes (§4.1.2).

    A flow belongs to the step whose half-open interval ``(t_{d-1}, t_d]`` contains it; flows
    outside the series are ignored.
    """
    flows = sorted(flows, key=lambda flow: flow.timestamp)
    observations: list[Observation] = []
    position = 0
    for previous, current in zip(closes, closes[1:]):
        while position < len(flows) and flows[position].timestamp <= previous.timestamp:
            position += 1
        step: list[Flow] = []
        while position < len(flows) and flows[position].timestamp <= current.timestamp:
            step.append(flows[position])
            position += 1
        r, net = modified_dietz(previous.nav, current.nav, previous.timestamp, current.timestamp, step)
        observations.append(Observation(day=current.day, r=r, gap_days=(current.day - previous.day).days,
                                        unstable=r is None, net_flow_usd=net))
    return observations


def twr_index(returns: Sequence[float | None], base: float = BASE) -> list[float]:
    """``I_0 = base, I_d = I_{d-1} (1 + r_d)``; a ``None`` return carries the index forward."""
    index = [base]
    for r in returns:
        index.append(index[-1] * (1.0 + r) if r is not None else index[-1])
    return index


def build_series(closes: Sequence[Close], flows: Iterable[Flow] = ()) -> Series:
    observations = daily_returns(closes, flows)
    return Series(list(closes), observations, twr_index([o.r for o in observations]) if closes else [])


def time_weighted_return(index: Sequence[float], days: float) -> dict:
    """TWR over the index; ``annualized_pct`` only when the period spans >= 365 days (GIPS)."""
    if len(index) < 2 or index[0] <= 0:
        return {"twr_pct": None, "days": days}
    twr = index[-1] / index[0] - 1.0
    payload = {"twr_pct": twr * 100, "days": days}
    if days >= ANNUALIZATION_DAYS and twr > -1.0:
        payload["annualized_pct"] = ((1.0 + twr) ** (ANNUALIZATION_DAYS / days) - 1.0) * 100
    return payload


def rebase(values: Sequence[float | None], base: float = BASE) -> list[float | None]:
    """Base-100 series: ``base * v_d / v_s`` with ``s`` the first positive value (§4.3.2).

    Values before ``s``, and missing or non-positive values, stay ``None``.
    """
    start = next((v for v in values if v is not None and v > 0), None)
    if start is None:
        return [None] * len(values)
    out: list[float | None] = []
    started = False
    for value in values:
        if value is not None and value > 0 and (started or value == start):
            started = True
            out.append(base * value / start)
        else:
            out.append(None)
    return out


def drawdown(days: Sequence[date], index: Sequence[float]) -> dict:
    """Max drawdown of the index with its peak / trough / recovery, and the underwater series.

    ``recovery`` is the first day after the trough where ``I`` is back at the peak's high-water
    mark (None if not yet); ``duration_days`` runs from peak to recovery (to the last day when not
    recovered). Drawdowns are fractions (``-0.25`` = -25%).
    """
    if not index:
        return {"max_drawdown": None, "current_drawdown": None, "peak": None, "trough": None,
                "recovery": None, "duration_days": None, "recovered": None, "underwater": []}
    values = np.asarray(index, dtype=float)
    hwm = np.maximum.accumulate(values)
    dd = np.where(hwm > 0, values / hwm - 1.0, 0.0)
    trough = int(np.argmin(dd))
    mdd = float(dd[trough])
    underwater = [{"date": d.isoformat(), "drawdown": float(x)} for d, x in zip(days, dd)]
    if mdd >= 0:
        return {"max_drawdown": 0.0, "current_drawdown": float(dd[-1]), "peak": None, "trough": None,
                "recovery": None, "duration_days": 0, "recovered": True, "underwater": underwater}
    peak = int(np.argmax(values[: trough + 1]))   # first index reaching the HWM at the trough
    recovery = next((i for i in range(trough + 1, len(values)) if values[i] >= hwm[trough]), None)
    end = recovery if recovery is not None else len(values) - 1
    return {
        "max_drawdown": mdd,
        "current_drawdown": float(dd[-1]),
        "peak": days[peak].isoformat(),
        "trough": days[trough].isoformat(),
        "recovery": days[recovery].isoformat() if recovery is not None else None,
        "recovered": recovery is not None,
        "duration_days": (days[end] - days[peak]).days,
        "underwater": underwater,
    }


def annualized_volatility(observations: Sequence[Observation],
                          min_observations: int = MIN_OBSERVATIONS) -> dict:
    """``std(r, ddof=1) * sqrt(365)`` over the usable 1-day observations (§4.4.2)."""
    returns = [o.r for o in observations if o.usable_for_risk]
    n = len(returns)
    excluded = len(observations) - n
    if n < 2:
        return {"daily": None, "annualized": None, "n_obs": n, "excluded_obs": excluded,
                "sufficient": False}
    daily = float(np.std(np.asarray(returns, dtype=float), ddof=1))
    return {"daily": daily, "annualized": daily * math.sqrt(ANNUALIZATION_DAYS), "n_obs": n,
            "excluded_obs": excluded, "sufficient": n >= min_observations}


# -- loaders --------------------------------------------------------------------------
def parse_period(period: str) -> int | None:
    """``'30d'`` -> 30, ``'1y'`` -> 365, ``'all'`` -> None (the whole history)."""
    match = _PERIOD_RE.match((period or "").strip().lower())
    if not match:
        raise ValueError(f"period must look like 30d, 1y or all, got {period!r}")
    if match.group(1):
        days = int(match.group(1))
    elif match.group(2):
        days = int(match.group(2)) * 365
    else:
        return None
    if not 0 < days <= MAX_PERIOD_DAYS:
        raise ValueError(f"period must be between 1 and {MAX_PERIOD_DAYS} days, got {period!r}")
    return days


def degraded_snapshot_ids(database: Database) -> set[int]:
    """Snapshots where at least one source failed: never a daily close (P0.2)."""
    marks = ",".join("?" for _ in DEGRADED_STATUSES)
    with database.connect() as connection:
        rows = connection.execute(
            f"SELECT DISTINCT snapshot_id FROM snapshot_sources WHERE status IN ({marks})",
            DEGRADED_STATUSES).fetchall()
    return {int(row[0]) for row in rows}


def benchmark_prices(database: Database, snapshot_ids: Sequence[int],
                     symbols: Sequence[str] = BENCHMARKS) -> dict[str, dict[int, float]]:
    """``symbol -> {snapshot_id: price}`` from the rows recorded with each snapshot.

    The plain symbol wins over its wrapped aliases; the highest-value row breaks ties.
    """
    out: dict[str, dict[int, float]] = {symbol: {} for symbol in symbols}
    if not snapshot_ids:
        return out
    wanted = {alias: symbol for symbol in symbols for alias in BENCHMARK_ALIASES.get(symbol, (symbol,))}
    rank: dict[tuple[str, int], tuple[int, float]] = {}
    ids = list(snapshot_ids)
    with database.connect() as connection:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            rows = connection.execute(
                f"SELECT snapshot_id, symbol, price_usd, value_usd FROM snapshot_assets "
                f"WHERE price_usd > 0 AND snapshot_id IN ({','.join('?' for _ in chunk)}) "
                f"AND symbol IN ({','.join('?' for _ in wanted)})",
                [*chunk, *wanted]).fetchall()
            for row in rows:
                symbol = wanted[row["symbol"]]
                key = (symbol, int(row["snapshot_id"]))
                score = (int(row["symbol"] == symbol), float(row["value_usd"] or 0.0))
                if key not in rank or score > rank[key]:
                    rank[key] = score
                    out[symbol][key[1]] = float(row["price_usd"])
    return out


def load_flows(database: Database, start: datetime, end: datetime,
               manual_only: bool = False) -> list[Flow]:
    """Completed external deposits / withdrawals of ``(start, end]``, signed (+in / -out).

    `manual_only` keeps only the manual-asset quantity changes (Epic M6): before any Binance
    history is synced those are the only flows known, and they are known exactly.
    """
    rows = flows_between(database, _iso(start), _iso(end))
    flows = []
    for row in rows:
        if manual_only and row["source"] != MANUAL:
            continue
        if row["status"] != COMPLETED or row["is_internal_transfer"] \
                or row["flow_type"] not in EXTERNAL_FLOW_TYPES:
            continue
        value = float(row.get("value_usd") or 0.0)
        flows.append(Flow(parse_timestamp(row["timestamp"]), value if row["direction"] == IN else -value))
    return flows


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def _pct(value: float | None) -> float | None:
    return None if value is None else round(value * 100, 4)


def quant_report(database: Database, period: str = "all", include_backfilled: bool = False,
                 benchmarks: Sequence[str] = BENCHMARKS,
                 min_observations: int = MIN_OBSERVATIONS) -> dict:
    """The ``GET /api/analytics/quant`` payload. Reads SQLite only, never the network."""
    days_back = parse_period(period)
    benchmarks = [symbol.upper() for symbol in benchmarks]
    latest = database.latest()
    payload: dict = {"as_of": latest.timestamp if latest else None, "period": period,
                     "resolution": "daily", "day_boundary_tz": "UTC",
                     "include_backfilled": include_backfilled, "min_observations": min_observations}
    snapshots: list[Snapshot] = database.history(include_backfilled=include_backfilled)
    if latest is not None:
        snapshots = [s for s in snapshots if s.dt <= latest.dt]
    if days_back is not None and latest is not None:
        cutoff = (latest.dt - timedelta(days=days_back)).date()
        snapshots = [s for s in snapshots if s.dt.date() >= cutoff]
    closes = daily_closes(((s.dt, s.total_portfolio_usd, s.id) for s in snapshots),
                          degraded_snapshot_ids(database))
    tracking = flow_tracking(database)
    flow_adjusted = bool(tracking["enabled"])
    payload["flow_adjusted"] = flow_adjusted
    payload["flows_synced_until"] = tracking["synced_until"]
    if len(closes) < 2:
        return {**payload, "available": False, "n_obs": max(len(closes) - 1, 0), "sufficient": False,
                "reason": "need daily closes on at least two days"}

    flows = load_flows(database, closes[0].timestamp, closes[-1].timestamp,
                       manual_only=not flow_adjusted)
    series = build_series(closes, flows)
    span_days = (closes[-1].day - closes[0].day).days
    vol = annualized_volatility(series.observations, min_observations)
    dd = drawdown(series.days, series.index)
    twr = time_weighted_return(series.index, span_days)
    prices = benchmark_prices(database, [c.snapshot_id for c in closes if c.snapshot_id is not None],
                              benchmarks)

    chart = {"dates": [d.isoformat() for d in series.days],
             "portfolio": [round(v, 4) for v in series.index]}
    bench_stats = {}
    for symbol in benchmarks:
        raw = [prices.get(symbol, {}).get(c.snapshot_id) for c in closes]
        rebased = rebase(raw)
        present = [i for i, v in enumerate(rebased) if v is not None]
        chart[symbol] = [None if v is None else round(v, 4) for v in rebased]
        if len(present) < 2:
            bench_stats[symbol] = {"available": False, "n_points": len(present),
                                   "reason": "no recorded price for this benchmark"}
            continue
        first, last = present[0], present[-1]
        bench_return = rebased[last] / BASE - 1.0
        portfolio_return = series.index[last] / series.index[first] - 1.0  # same window as the benchmark
        bench_stats[symbol] = {
            "available": True,
            "n_points": len(present),
            "start": series.days[first].isoformat(),
            "end": series.days[last].isoformat(),
            "return_pct": _pct(bench_return),
            "portfolio_return_pct": _pct(portfolio_return),
            "excess_pct": _pct(portfolio_return - bench_return),
            "max_drawdown_pct": _pct(drawdown([series.days[i] for i in present],
                                              [rebased[i] for i in present])["max_drawdown"]),
        }

    n_obs = len(series.observations)
    nav_end, nav_start = closes[-1].nav, closes[0].nav
    return {
        **payload,
        "available": True,
        "n_obs": n_obs,
        "sufficient": n_obs >= min_observations,
        "start": closes[0].day.isoformat(),
        "end": closes[-1].day.isoformat(),
        "span_days": span_days,
        "gap_obs": sum(1 for o in series.observations if o.gap_days > 1),
        "unstable_obs": sum(1 for o in series.observations if o.unstable),
        "flow_count": len(flows),
        "nav_start_usd": round(nav_start, 2),
        "nav_end_usd": round(nav_end, 2),
        "net_flow_usd": round(sum(f.amount_usd for f in flows), 2),
        "twr": {"twr_pct": _round(twr["twr_pct"]), "days": span_days,
                **({"annualized_pct": _round(twr["annualized_pct"])} if "annualized_pct" in twr else {})},
        "drawdown": {
            "max_drawdown_pct": _pct(dd["max_drawdown"]),
            "current_drawdown_pct": _pct(dd["current_drawdown"]),
            "peak": dd["peak"], "trough": dd["trough"], "recovery": dd["recovery"],
            "recovered": dd["recovered"], "duration_days": dd["duration_days"],
            "underwater": [{"date": p["date"], "drawdown_pct": round(p["drawdown"] * 100, 4)}
                           for p in dd["underwater"]],
        },
        "volatility": {
            "daily_pct": _pct(vol["daily"]), "annualized_pct": _pct(vol["annualized"]),
            "n_obs": vol["n_obs"], "excluded_obs": vol["excluded_obs"],
            "sufficient": vol["sufficient"],
        },
        "benchmarks": bench_stats,
        "chart": chart,
    }
