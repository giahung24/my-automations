"""N1: what Simple Earn actually pays (``GET /api/analytics/earn``). Reads SQLite only.

Per asset currently in Earn:

* **listed APR** - the rate Binance advertised in the newest snapshot (``snapshot_assets.listed_apr``):
  the base rate plus the tier bonus pro rata of the balance inside its band, i.e. what the position
  earns overall (``clients.binance.effective_apr``). ``listed_base_apr`` / ``listed_tiers`` are the
  breakdown (base rate alone, bonus bands), so 2.60% can be read as 2.02% + 3% on the first 5,000;
* **realised APR** over 7 and 30 days - rewards received (``cash_flows``) over the average Earn
  balance of the fetched snapshots, scaled to a year:

      APR = rewards / mean(balance) x 365 / span_days

  It is an interest rate, not a portfolio return, so annualising a short window is the point.
  The window must really be covered: snapshots spanning at least ``MIN_COVERAGE`` of it with no
  gap above ``MAX_GAP_HOURS``, else ``apr`` is ``None`` and ``sufficient`` is false. Snapshots whose
  Earn fetch failed are left out (a failed source is stored as 0, not as an empty account).

**Lifetime income** needs no balance: it is the plain sum of the rewards, valued in USD when each
was received, by year. It reaches as far back as ``cash_flows.earn_backfill_days`` was synced.

``realised_apr()`` is pure; ``earn_report()`` is the loader.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from models import BINANCE_EARN, STATUS_ERROR, STATUS_INFERRED_ERROR
from services.cash_flows import CHANNEL_EARN, COMPLETED, REWARD, SOURCE_BINANCE
from services.db import Database, parse_timestamp

WINDOWS = (7, 30)
MIN_COVERAGE = 0.9   # share of the window the snapshots must span
MAX_GAP_HOURS = 6    # a longer silence between two snapshots hides balance changes
YEAR_DAYS = 365


def realised_apr(balances: Sequence[tuple[datetime, float]], rewards: Iterable[tuple[datetime, float]],
                 window_days: int) -> dict:
    """APR of one asset over one window. ``balances`` are (time, Earn quantity), oldest first."""
    result = {"apr": None, "n_obs": len(balances), "sufficient": False, "days": 0.0}
    if len(balances) < 2:
        return result
    first, last = balances[0][0], balances[-1][0]
    span_days = (last - first).total_seconds() / 86400
    result["days"] = round(span_days, 2)
    gap = max((b[0] - a[0] for a, b in zip(balances, balances[1:])), default=timedelta(0))
    # shortcut: plain mean of the snapshots, not time-weighted; fine at a steady hourly cadence,
    # weight by interval if the collector ever runs irregularly.
    mean = sum(quantity for _, quantity in balances) / len(balances)
    if span_days < MIN_COVERAGE * window_days or gap > timedelta(hours=MAX_GAP_HOURS) or mean <= 0:
        return result
    earned = sum(amount for when, amount in rewards if first < when <= last)
    result.update(apr=earned / mean * YEAR_DAYS / span_days, sufficient=True)
    return result


def earn_report(database: Database, stablecoins: Iterable[str] = ()) -> dict:
    stable = {str(s).upper() for s in stablecoins}
    latest = database.latest()
    payload = {"as_of": latest.timestamp if latest else None, "windows": list(WINDOWS), "assets": [],
               "usd_per_day": None, "lifetime": _lifetime(database, stable)}
    if latest is None:
        return payload
    end = latest.dt
    start = (end - timedelta(days=max(WINDOWS))).isoformat()
    with database.connect() as connection:
        snapshots = connection.execute(
            """SELECT s.id, s.timestamp FROM snapshots s
               WHERE s.is_backfilled = 0 AND s.timestamp >= ? AND s.timestamp <= ?
                 AND NOT EXISTS (SELECT 1 FROM snapshot_sources x WHERE x.snapshot_id = s.id
                                 AND x.source = ? AND x.status IN (?, ?))
               ORDER BY s.timestamp, s.id""",
            (start, latest.timestamp, BINANCE_EARN, STATUS_ERROR, STATUS_INFERRED_ERROR),
        ).fetchall()
        held = connection.execute(
            """SELECT a.snapshot_id, a.symbol, SUM(a.quantity) AS quantity
               FROM snapshot_assets a JOIN snapshots s ON s.id = a.snapshot_id
               WHERE a.source = ? AND s.is_backfilled = 0 AND s.timestamp >= ? AND s.timestamp <= ?
               GROUP BY a.snapshot_id, a.symbol""",
            (BINANCE_EARN, start, latest.timestamp),
        ).fetchall()
        paid = connection.execute(
            "SELECT timestamp, asset, amount, value_usd FROM cash_flows "
            "WHERE source = ? AND channel = ? AND flow_type = ? AND status = ? AND timestamp > ?",
            (SOURCE_BINANCE, CHANNEL_EARN, REWARD, COMPLETED, start),
        ).fetchall()
    quantity = {(row["snapshot_id"], row["symbol"]): float(row["quantity"] or 0) for row in held}
    times = [(row["id"], parse_timestamp(row["timestamp"])) for row in snapshots]
    rewards: dict[str, list[tuple[datetime, float, float]]] = defaultdict(list)
    for row in paid:
        rewards[row["asset"]].append((parse_timestamp(row["timestamp"]), float(row["amount"]), float(row["value_usd"])))

    positions: dict[str, dict] = {}
    for row in database.assets(latest.id):
        if row["source"] != BINANCE_EARN or row["quantity"] <= 0:
            continue
        position = positions.setdefault(row["symbol"], {"quantity": 0.0, "value_usd": 0.0, "rated": 0.0, "weight": 0.0,
                                                        "base": 0.0, "base_weight": 0.0, "tiers": []})
        position["quantity"] += row["quantity"]
        position["value_usd"] += row["value_usd"]
        if row.get("listed_apr") is not None:  # weighted by quantity across flexible + locked
            position["rated"] += row["listed_apr"] * row["quantity"]
            position["weight"] += row["quantity"]
        if row.get("listed_base_apr") is not None:
            position["base"] += row["listed_base_apr"] * row["quantity"]
            position["base_weight"] += row["quantity"]
        position["tiers"] = position["tiers"] or row.get("listed_tiers") or []  # flexible only: one row has them

    total_per_day = 0.0
    for symbol, position in sorted(positions.items(), key=lambda item: -item[1]["value_usd"]):
        realised, per_day = {}, None
        for days in WINDOWS:
            since = end - timedelta(days=days)
            balances = [(when, quantity.get((sid, symbol), 0.0)) for sid, when in times if when >= since]
            realised[f"{days}d"] = realised_apr(balances, [(w, a) for w, a, _ in rewards[symbol]], days)
            if days == min(WINDOWS) and len(balances) >= 2:
                span = (balances[-1][0] - balances[0][0]).total_seconds() / 86400
                if span >= 1:  # under a day, one payout more or less would swing the figure
                    per_day = sum(v for w, _, v in rewards[symbol] if balances[0][0] < w <= balances[-1][0]) / span
        total_per_day += per_day or 0.0
        payload["assets"].append({
            "symbol": symbol,
            "quantity": position["quantity"],
            "value_usd": round(position["value_usd"], 2),
            "listed_apr": position["rated"] / position["weight"] if position["weight"] > 0 else None,
            "listed_base_apr": position["base"] / position["base_weight"] if position["base_weight"] > 0 else None,
            "listed_tiers": position["tiers"],
            "realised": realised,
            "usd_per_day": round(per_day, 4) if per_day is not None else None,
        })
    if any(asset["usd_per_day"] is not None for asset in payload["assets"]):
        payload["usd_per_day"] = round(total_per_day, 4)
    return payload


def _lifetime(database: Database, stable: set[str]) -> dict:
    """Every Earn reward ever synced, in USD at the time it was paid, by year."""
    with database.connect() as connection:
        rows = connection.execute(
            """SELECT substr(timestamp, 1, 4) AS year, asset, SUM(amount) AS amount,
                      SUM(value_usd) AS usd, SUM(value_usd <= 0) AS unpriced, MIN(timestamp) AS since
               FROM cash_flows WHERE source = ? AND channel = ? AND flow_type = ? AND status = ?
               GROUP BY year, asset""",
            (SOURCE_BINANCE, CHANNEL_EARN, REWARD, COMPLETED),
        ).fetchall()
    years: dict[str, dict] = {}
    assets: dict[str, dict] = {}
    for row in rows:
        usd = float(row["usd"] or 0)
        year = years.setdefault(row["year"], {"year": row["year"], "usd": 0.0, "stable_usd": 0.0})
        year["usd"] += usd
        year["stable_usd"] += usd if row["asset"] in stable else 0.0
        asset = assets.setdefault(row["asset"], {"asset": row["asset"], "amount": 0.0, "usd": 0.0})
        asset["amount"] += float(row["amount"] or 0)
        asset["usd"] += usd
    for item in (*years.values(), *assets.values()):
        for key in ("usd", "stable_usd"):
            if key in item:
                item[key] = round(item[key], 2)
    return {
        "since": min((row["since"] for row in rows), default=None),
        "total_usd": round(sum(y["usd"] for y in years.values()), 2),
        "stable_usd": round(sum(y["stable_usd"] for y in years.values()), 2),
        # rewards no price could be found for count as $0: say how many rather than hide it
        "unpriced": int(sum(row["unpriced"] or 0 for row in rows)),
        "by_year": sorted(years.values(), key=lambda y: y["year"]),
        "by_asset": sorted(assets.values(), key=lambda a: -a["usd"]),
    }
