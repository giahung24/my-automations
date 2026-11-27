"""Activity timeline over ``cash_flows`` (docs/SPEC.md Epic M4, Tab 3 of the PWA).

Reads SQLite only. Three entry points back the three routes of ``web_api.py``:

* :func:`activity_report` - ``GET /api/cash-flows``: one item per thing that happened, newest
  first. A paired internal transfer (two rows pointing at each other through ``matched_flow_id``)
  is **one** ``kind = 'transfer'`` item with ``from`` / ``to``. Earn rewards are folded into one
  item per local day (``kind = 'reward'``, ``group = true``) with a per-asset breakdown, so five
  years of daily interest do not drown the deposits and withdrawals. Items carry
  ``needs_review`` (see :func:`needs_review`); ``facets`` count the items per source and per kind
  so the filter chips can show numbers.
* :func:`transfer_candidates` - ``GET /api/cash-flows/{id}/candidates``: the flows that could be
  the other leg of an unmatched deposit/withdrawal, best first.
* :func:`confirm_flow` - ``PATCH /api/cash-flows/{id}``: the user's decision. It sets
  ``classified_by = 'user'`` and ``match_method = 'manual'``, so no sync or matcher touches the
  row again (they all skip ``classified_by = 'user'``). Pairing or unpairing updates both legs in
  one transaction.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from config import CashFlowSettings
from services.cash_flows import (
    CHANNEL_CRYPTO,
    CHANNEL_EARN,
    CHANNEL_ONCHAIN,
    CLASSIFIED_AUTO,
    CLASSIFIED_USER,
    COMPLETED,
    IN,
    MATCH_HEURISTIC,
    MATCH_MANUAL,
    OUT,
    sync_states,
)
from services.db import Database, parse_timestamp, utc_now_iso
from services.reconcile import inferred_items

SOURCE_GROUPS = ("binance", "onchain", "manual")
KINDS = ("deposit", "withdrawal", "transfer", "reward", "fee", "trade", "unexplained")
REVIEW_CONFIDENCE = 0.9      # a heuristic pairing below this is shown for confirmation
CANDIDATE_WINDOW_DAYS = 7    # how far apart two legs of one transfer can plausibly be
CANDIDATE_LIMIT = 10
REVIEWABLE_CHANNELS = (CHANNEL_CRYPTO, CHANNEL_ONCHAIN)   # fiat / P2P are external by nature
MAX_LIMIT = 200


class ActivityError(Exception):
    """A request the data cannot satisfy; ``status`` is the HTTP code to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# -- classification --------------------------------------------------------------------
def source_group(source: str) -> str:
    """``binance`` | ``onchain`` (``evm:*`` and ``solana``) | ``manual``."""
    source = str(source or "")
    if source == "binance":
        return "binance"
    if source == "manual":
        return "manual"
    return "onchain"


def kind_of(row) -> str:
    flow_type = row["flow_type"]
    if flow_type in ("reward", "airdrop"):
        return "reward"
    if flow_type == "fee":
        return "fee"
    if row["is_internal_transfer"]:
        return "transfer"
    if flow_type in ("deposit", "withdrawal"):
        return flow_type
    return "deposit" if row["direction"] == IN else "withdrawal"   # adjustment


def needs_review(row, now: datetime, transit: timedelta) -> bool:
    """True when the system is unsure and only the user can say (docs/SPEC.md §9.1).

    (a) a completed crypto deposit/withdrawal, past the in-transit window, matched with nothing
    and never looked at by the user; or (b) a heuristic pairing below 0.9 confidence.
    """
    if row["classified_by"] != CLASSIFIED_AUTO or row["status"] != COMPLETED:
        return False
    if row["flow_type"] not in ("deposit", "withdrawal"):
        return False
    if row["is_internal_transfer"]:
        return row["match_method"] == MATCH_HEURISTIC and float(row["match_confidence"] or 0) < REVIEW_CONFIDENCE
    if row["matched_flow_id"] is not None or row["channel"] not in REVIEWABLE_CHANNELS:
        return False
    return now - parse_timestamp(row["timestamp"]) > transit


def _short(address: str) -> str:
    address = str(address or "")
    return address if len(address) <= 12 else f"{address[:6]}…{address[-4:]}"


def _label(row) -> str:
    return row["account_label"] or _short(row["account"]) or source_group(row["source"]).title()


def _counterparty(row) -> str:
    return row["counterparty_label"] or _short(row["counterparty_address"]) or ""


def _fee_usd(row) -> float:
    fee = float(row["fee_amount"] or 0.0)
    if fee <= 0 or (row["fee_asset"] and row["fee_asset"] != row["asset"]):
        return 0.0
    return round(fee * float(row["price_usd"] or 0.0), 2)


def _local_day(timestamp: str, tz_offset: int) -> str:
    return (parse_timestamp(timestamp) + timedelta(minutes=tz_offset)).date().isoformat()


def _match(row) -> dict | None:
    if not row["is_internal_transfer"] and row["matched_flow_id"] is None and row["classified_by"] == CLASSIFIED_AUTO:
        return None
    return {
        "method": row["match_method"] or None,
        "confidence": round(float(row["match_confidence"] or 0.0), 4),
        "matched_flow_id": row["matched_flow_id"],
        "classified_by": row["classified_by"],
    }


def flow_item(row, *, partner=None, now: datetime, transit: timedelta, tz_offset: int = 0) -> dict:
    """One timeline item for a single row, or for a paired transfer when ``partner`` is given."""
    kind = kind_of(row)
    if partner is not None:   # both legs known: `row` is the out leg
        origin, target = _label(row), _label(partner)
        sources = sorted({source_group(row["source"]), source_group(partner["source"])})
    else:
        if row["direction"] == OUT:
            origin = _label(row)
            target = _counterparty(row) or ("Own wallet" if kind == "transfer" else "External")
        else:
            origin = _counterparty(row) or ("Own wallet" if kind == "transfer" else "External")
            target = _label(row)
        sources = [source_group(row["source"])]
    return {
        "id": row["id"],
        "key": f"f{row['id']}",
        "kind": kind,
        "group": False,
        "timestamp": row["timestamp"],
        "day": _local_day(row["timestamp"], tz_offset),
        "asset": row["asset"],
        "amount": float(row["amount"]),
        "value_usd": round(float(row["value_usd"] or 0.0), 2),
        "direction": None if partner is not None else row["direction"],
        "from": origin,
        "to": target,
        "fee_amount": float(row["fee_amount"] or 0.0),
        "fee_asset": row["fee_asset"] or "",
        "fee_usd": _fee_usd(row),
        "channel": row["channel"],
        "network": row["network"] or "",
        "status": row["status"],
        "source": sources[0] if len(sources) == 1 else "mixed",
        "sources": sources,
        "tx_hash": row["tx_hash"] or "",
        "counterparty_address": row["counterparty_address"] or "",
        "match": _match(row),
        "needs_review": needs_review(row, now, transit)
        or (partner is not None and needs_review(partner, now, transit)),
        "note": row["note"] or "",
        "partner_id": partner["id"] if partner is not None else None,
    }


def earn_item(day: str, rows: list, tz_offset: int = 0) -> dict:
    """All Earn rewards of one local day as a single collapsible item."""
    per_asset: dict[str, dict] = {}
    for row in rows:
        entry = per_asset.setdefault(row["asset"], {"asset": row["asset"], "amount": 0.0, "value_usd": 0.0, "count": 0})
        entry["amount"] += float(row["amount"])
        entry["value_usd"] += float(row["value_usd"] or 0.0)
        entry["count"] += 1
    assets = sorted(per_asset.values(), key=lambda a: (-a["value_usd"], a["asset"]))
    for entry in assets:
        entry["value_usd"] = round(entry["value_usd"], 2)
    newest = max(rows, key=lambda r: (r["timestamp"], r["id"]))
    return {
        "id": f"earn:{day}",
        "key": f"earn:{day}",
        "kind": "reward",
        "group": True,
        "timestamp": newest["timestamp"],
        "day": day,
        "asset": assets[0]["asset"] if len(assets) == 1 else "",
        "amount": assets[0]["amount"] if len(assets) == 1 else None,
        "value_usd": round(sum(a["value_usd"] for a in assets), 2),
        "direction": IN,
        "from": "Binance Earn",
        "to": "Binance",
        "count": len(rows),
        "assets": assets,
        "source": "binance",
        "sources": sorted({source_group(r["source"]) for r in rows}),
        "channel": CHANNEL_EARN,
        "status": COMPLETED,
        "match": None,
        "needs_review": False,
        "_last_id": newest["id"],
    }


# -- report ------------------------------------------------------------------------------
def _cursor_key(item: dict) -> tuple[str, int]:
    return item["timestamp"], int(item["_last_id"] if "_last_id" in item else item["id"])


def parse_cursor(cursor: str | None) -> tuple[str, int] | None:
    if not cursor:
        return None
    # an unencoded "+00:00" arrives as " 00:00" in a query string
    timestamp, sep, item_id = cursor.replace(" ", "+").rpartition("|")
    if not sep or not timestamp:
        raise ActivityError(422, "Invalid cursor.")
    try:
        return timestamp, int(item_id)
    except ValueError as exc:
        raise ActivityError(422, "Invalid cursor.") from exc


def activity_report(database: Database, *, settings: CashFlowSettings | None = None, days: int = 90,
                    source: str = "all", kind: str = "all", asset: str = "", review: bool = False,
                    cursor: str | None = None, limit: int = 50, tz_offset: int = 0,
                    now: datetime | None = None) -> dict:
    """The timeline page plus counts for the filter chips. ``days = 0`` means all history.

    Besides the recorded flows it carries the inferred trades and the unexplained quantity
    changes of ``services/reconcile.py`` (``inferred: true``).
    """
    settings = settings or CashFlowSettings()
    now = now or datetime.now(timezone.utc)
    transit = timedelta(hours=settings.in_transit_hours)
    limit = max(1, min(int(limit), MAX_LIMIT))
    after = parse_cursor(cursor)

    query, params = "SELECT * FROM cash_flows WHERE status <> 'failed'", []
    if days:
        query += " AND timestamp >= ?"
        params.append((now - timedelta(days=days)).replace(microsecond=0).isoformat())
    asset = asset.strip().upper()
    if asset:
        query += " AND asset = ?"
        params.append(asset)
    with database.connect() as connection:
        rows = connection.execute(query + " ORDER BY timestamp DESC, id DESC", params).fetchall()
        by_id = {row["id"]: row for row in rows}
        missing = sorted({row["matched_flow_id"] for row in rows
                          if row["matched_flow_id"] is not None and row["matched_flow_id"] not in by_id})
        partners = dict(by_id)
        if missing:   # the other leg fell just outside the window: still name it
            marks = ",".join("?" for _ in missing)
            for row in connection.execute(f"SELECT * FROM cash_flows WHERE id IN ({marks})", missing):
                partners[row["id"]] = row

    items: list[dict] = []
    earn_days: dict[str, list] = defaultdict(list)
    shown: set[int] = set()
    for row in rows:
        if row["id"] in shown:
            continue
        if kind_of(row) == "reward":
            earn_days[_local_day(row["timestamp"], tz_offset)].append(row)
            continue
        partner = partners.get(row["matched_flow_id"]) if row["matched_flow_id"] is not None else None
        if partner is not None and row["is_internal_transfer"] and partner["matched_flow_id"] == row["id"]:
            out_leg, in_leg = (row, partner) if row["direction"] == OUT else (partner, row)
            shown.update((row["id"], partner["id"]))
            item = flow_item(out_leg, partner=in_leg, now=now, transit=transit, tz_offset=tz_offset)
            item["timestamp"] = max(row["timestamp"], partner["timestamp"]) if partner["id"] in by_id else row["timestamp"]
            item["day"] = _local_day(item["timestamp"], tz_offset)
            item["id"] = row["id"] if row["timestamp"] == item["timestamp"] else partner["id"]
            item["partner_id"] = partner["id"] if item["id"] == row["id"] else row["id"]
            item["key"] = f"f{item['id']}"
            items.append(item)
            continue
        shown.add(row["id"])
        items.append(flow_item(row, now=now, transit=transit, tz_offset=tz_offset))
    items.extend(earn_item(day, day_rows, tz_offset) for day, day_rows in earn_days.items())
    # N2: quantity changes no flow explains, read from the snapshots (inferred, never stored)
    since = (now - timedelta(days=days)).replace(microsecond=0).isoformat() if days else None
    for item in inferred_items(database, since, tz_offset):
        legs = [*item.get("sold", []), *item.get("bought", []), *item.get("assets", [])]
        if not asset or any(leg["asset"] == asset for leg in legs):
            items.append(item)
    items.sort(key=_cursor_key, reverse=True)

    def source_ok(item: dict) -> bool:
        return source == "all" or source in item["sources"]

    def kind_ok(item: dict) -> bool:
        return kind == "all" or item["kind"] == kind

    def review_ok(item: dict) -> bool:
        return not review or item["needs_review"]

    facets = {
        "source": {group: sum(1 for i in items if group in i["sources"] and kind_ok(i) and review_ok(i))
                   for group in SOURCE_GROUPS},
        "kind": {k: sum(1 for i in items if i["kind"] == k and source_ok(i) and review_ok(i)) for k in KINDS},
    }
    facets["source"]["all"] = sum(1 for i in items if kind_ok(i) and review_ok(i))
    facets["kind"]["all"] = sum(1 for i in items if source_ok(i) and review_ok(i))

    selected = [i for i in items if source_ok(i) and kind_ok(i) and review_ok(i)]
    if after is not None:
        selected = [i for i in selected if _cursor_key(i) < after]
    page = selected[:limit]
    next_cursor = None
    if len(selected) > limit:
        last = _cursor_key(page[-1])
        next_cursor = f"{last[0]}|{last[1]}"

    external = 0.0
    earned = 0.0
    for row in rows:
        if not source_ok({"sources": [source_group(row["source"])]}):
            continue
        if row["status"] == COMPLETED and row["flow_type"] in ("deposit", "withdrawal") and not row["is_internal_transfer"]:
            external += float(row["value_usd"] or 0.0) * (1 if row["direction"] == IN else -1)
        elif kind_of(row) == "reward":
            earned += float(row["value_usd"] or 0.0)

    for item in page:
        item.pop("_last_id", None)
    states = sync_states(database)
    return {
        "days": days or None,
        "count": len(page),
        "total": len(selected),
        "next_cursor": next_cursor,
        "net_external_usd": round(external, 2),
        "earned_usd": round(earned, 2),
        "needs_review": sum(1 for i in items if i["needs_review"] and source_ok(i)),
        "facets": facets,
        "items": page,
        "last_sync": states,
        "synced": bool(states),
    }


# -- manual confirmation (M4.2) ----------------------------------------------------------
def _row(connection, flow_id: int):
    row = connection.execute("SELECT * FROM cash_flows WHERE id = ?", (flow_id,)).fetchone()
    if row is None:
        raise ActivityError(404, f"No cash flow with id {flow_id}.")
    return row


def _amount_gap(out_row, in_row) -> float:
    sent = float(out_row["amount"])
    if sent <= 0:
        return float("inf")
    fee = float(out_row["fee_amount"] or 0.0) if (out_row["fee_asset"] or out_row["asset"]) == out_row["asset"] else 0.0
    received = float(in_row["amount"])
    return min(abs(sent - fee - received), abs(sent - received)) / sent


def transfer_candidates(database: Database, flow_id: int, *, limit: int = CANDIDATE_LIMIT) -> dict:
    """Flows that could be the other leg of ``flow_id``: same asset, opposite direction, unpaired."""
    with database.connect() as connection:
        row = _row(connection, flow_id)
        if row["flow_type"] not in ("deposit", "withdrawal"):
            raise ActivityError(422, "Only deposits and withdrawals can be part of a transfer.")
        moment = parse_timestamp(row["timestamp"])
        window = timedelta(days=CANDIDATE_WINDOW_DAYS)
        others = connection.execute(
            "SELECT * FROM cash_flows WHERE id <> ? AND asset = ? AND direction <> ? "
            "AND flow_type IN ('deposit', 'withdrawal') AND status = 'completed' "
            "AND matched_flow_id IS NULL AND timestamp >= ? AND timestamp <= ?",
            (flow_id, row["asset"], row["direction"],
             (moment - window).isoformat(), (moment + window).isoformat()),
        ).fetchall()
    now = datetime.now(timezone.utc)
    out = []
    for other in others:
        if (other["source"], other["account"]) == (row["source"], row["account"]):
            continue
        out_row, in_row = (row, other) if row["direction"] == OUT else (other, row)
        gap = _amount_gap(out_row, in_row)
        hours = abs((parse_timestamp(other["timestamp"]) - moment).total_seconds()) / 3600
        score = max(0.0, 1.0 - min(gap, 1.0) * 2 - min(hours / (CANDIDATE_WINDOW_DAYS * 24), 1.0) * 0.4)
        item = flow_item(other, now=now, transit=timedelta(0))
        item.update(amount_gap_pct=round(gap * 100, 3), hours_apart=round(hours, 2), score=round(score, 4))
        out.append(item)
    out.sort(key=lambda c: (-c["score"], c["hours_apart"], c["id"]))
    return {"flow_id": flow_id, "asset": row["asset"], "direction": row["direction"], "candidates": out[:limit]}


def reset_flow(database: Database, flow_id: int) -> dict:
    """Undo a decision: the row (and its mate, if paired) go back to automatic and unpaired.

    The matchers and the address classification then treat them again on the next sync.
    """
    now = utc_now_iso()
    with database.connect() as connection:
        row = _row(connection, flow_id)
        targets = [flow_id]
        if row["matched_flow_id"] is not None:
            mate = connection.execute("SELECT id, matched_flow_id FROM cash_flows WHERE id = ?",
                                      (row["matched_flow_id"],)).fetchone()
            if mate is not None and mate["matched_flow_id"] == flow_id:
                targets.append(mate["id"])
        for target in targets:
            connection.execute(
                "UPDATE cash_flows SET is_internal_transfer = 0, matched_flow_id = NULL, match_method = '', "
                "match_confidence = 0, classified_by = ?, updated_at = ? WHERE id = ?",
                (CLASSIFIED_AUTO, now, target),
            )
    return {"ok": True, "changed": targets}


def confirm_flow(database: Database, flow_id: int, *, internal: bool, matched_flow_id: int | None = None,
                 note: str | None = None) -> dict:
    """Record the user's verdict on a deposit/withdrawal; returns the ids that changed.

    * ``internal = True, matched_flow_id = N``: the two rows are one transfer (both legs updated);
    * ``internal = True`` alone: moved to one of my wallets, no other leg recorded;
    * ``internal = False``: really left / entered the portfolio; any pairing is undone on both legs.

    409 when pairing two flows in the same direction, of different assets, or with a leg that is
    already paired elsewhere.
    """
    now = utc_now_iso()
    changed: list[int] = []
    with database.connect() as connection:
        row = _row(connection, flow_id)
        if row["flow_type"] not in ("deposit", "withdrawal"):
            raise ActivityError(422, "Only deposits and withdrawals can be confirmed.")
        if matched_flow_id is not None and not internal:
            raise ActivityError(422, "matched_flow_id only makes sense with is_internal_transfer = true.")
        note_sql = ", note = ?" if note is not None else ""
        note_args = [note.strip()[:200]] if note is not None else []

        def unpair(target_id: int) -> None:
            connection.execute(
                "UPDATE cash_flows SET is_internal_transfer = 0, matched_flow_id = NULL, match_method = ?, "
                "match_confidence = 0, classified_by = ?, updated_at = ? WHERE id = ?",
                (MATCH_MANUAL, CLASSIFIED_USER, now, target_id),
            )
            changed.append(target_id)

        if matched_flow_id is not None:
            if matched_flow_id == flow_id:
                raise ActivityError(409, "A flow cannot be paired with itself.")
            other = _row(connection, matched_flow_id)
            if other["flow_type"] not in ("deposit", "withdrawal"):
                raise ActivityError(409, "The other leg must be a deposit or a withdrawal.")
            if other["direction"] == row["direction"]:
                raise ActivityError(409, "Both flows go the same way: a transfer needs one out and one in.")
            if str(other["asset"]).upper() != str(row["asset"]).upper():
                raise ActivityError(409, f"Different assets ({row['asset']} vs {other['asset']}).")
            for leg, mate in ((row, other), (other, row)):
                if leg["matched_flow_id"] is not None and leg["matched_flow_id"] != mate["id"]:
                    raise ActivityError(409, f"Flow {leg['id']} is already paired with flow {leg['matched_flow_id']}.")
            for leg, mate in ((row, other), (other, row)):
                connection.execute(
                    "UPDATE cash_flows SET is_internal_transfer = 1, matched_flow_id = ?, match_method = ?, "
                    "match_confidence = 1, counterparty_label = ?, classified_by = ?, updated_at = ?"
                    f"{note_sql if leg is row else ''} WHERE id = ?",
                    [mate["id"], MATCH_MANUAL, mate["account_label"] or mate["account"], CLASSIFIED_USER, now,
                     *(note_args if leg is row else []), leg["id"]],
                )
                changed.append(leg["id"])
            return {"ok": True, "changed": changed}

        if row["matched_flow_id"] is not None:   # any decision without a mate drops the old pairing
            partner = connection.execute("SELECT id, matched_flow_id FROM cash_flows WHERE id = ?",
                                         (row["matched_flow_id"],)).fetchone()
            if partner is not None and partner["matched_flow_id"] == flow_id:
                unpair(partner["id"])
        connection.execute(
            "UPDATE cash_flows SET is_internal_transfer = ?, matched_flow_id = NULL, match_method = ?, "
            f"match_confidence = ?, classified_by = ?, updated_at = ?{note_sql} WHERE id = ?",
            [int(internal), MATCH_MANUAL, 1.0 if internal else 0.0, CLASSIFIED_USER, now, *note_args, flow_id],
        )
        changed.append(flow_id)
    return {"ok": True, "changed": changed}
