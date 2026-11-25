"""Manual assets: balances no API can read (ROADMAP_LEDGER Epic M6, §9.3).

BTC on a hardware wallet, ETH staked with a validator, coins on another exchange: declared in
``config.yaml`` under ``manual_assets:`` (decision D13), then treated like any other source.

**Valuation (M6.2).** Each holding becomes a :class:`Holding` with ``source = 'manual'`` and
``detail = <label>``, priced through the same pipeline as the rest of the portfolio: the Binance
ticker first (BTC -> BTCUSDT), then DefiLlama when a ``price_id`` is given, and the fixed
``price_usd`` of the config only as the last resort. A holding nobody can price keeps its quantity,
is worth 0 in that snapshot and marks ``manual:<label>`` as ``error`` in ``snapshot_sources``, so
attribution books the gap as ``data_gap`` rather than as a price move. The snapshot stores the
total in ``snapshots.manual_usd`` (schema v5).

**Cash flows (M6.3).** Editing a quantity is not profit. After each snapshot,
:func:`record_manual_flows` compares the manual rows of the new snapshot with those of the previous
fetched snapshot; every change becomes a ``cash_flows`` row (``source = 'manual'``,
``detection_method = 'manual'``, ``account = <label>``) stamped at the new snapshot, so the PnL and
the TWR read it as money moved, not as performance. An asset declared for the first time is a
``deposit`` of its whole quantity. ``external_id = manual:<label>:<symbol>:<snapshot_after>`` keeps
a re-run from duplicating anything.

**Internal transfers.** Moving 0.1 BTC from Binance to the Ledger shows up twice: a Binance
withdrawal and a manual increase. :func:`match_manual_transfers` pairs them (and the reverse,
cold wallet -> Binance deposit) when the asset matches, the amounts agree within
``cash_flows.amount_tolerance_pct`` once the withdrawal fee is taken off, and the Binance flow falls
between the two snapshots that bracket the manual change, widened by ``cash_flows.in_transit_hours``.
Both rows then get ``is_internal_transfer = 1`` and point at each other, so the net external flow is
unchanged. Pairing is greedy one-to-one, best match first (ROADMAP_LEDGER §3.6).
"""
from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta

from config import CashFlowSettings, ManualAsset, ManualHolding
from models import MANUAL, Holding, manual_source
from services.db import Database, parse_timestamp, utc_now_iso

log = logging.getLogger(__name__)

DETECTION_MANUAL = "manual"
CHANNEL_MANUAL = "manual"
MATCH_HEURISTIC = "heuristic"
QUANTITY_EPSILON = 1e-12  # float noise from YAML round-trips is not a flow


# -- holdings and pricing (M6.2) --------------------------------------------------------
def build_holdings(assets: Iterable[ManualAsset]) -> list[tuple[Holding, ManualHolding]]:
    """One unpriced Holding per non-zero manual balance, with the spec it came from."""
    out = []
    for asset in assets:
        for spec in asset.holdings:
            if spec.quantity <= 0:
                continue
            holding = Holding(symbol=spec.symbol, source=MANUAL, quantity=spec.quantity,
                              detail=asset.label, price_id=spec.price_id)
            out.append((holding, spec))
    return out


def price_holdings(pairs: list[tuple[Holding, ManualHolding]], *,
                   ticker: Callable[[str], float],
                   llama: Callable[[list[Holding]], None] | None = None,
                   stablecoins: Iterable[str] = ()) -> list[str]:
    """Fill ``price_usd`` in place; returns one error per holding left without a price.

    Order: stablecoin pin ($1.00), Binance ticker, DefiLlama (`price_id`), fixed `price_usd`.
    A fixed price is a deliberate choice, so ``price_usd: 0`` prices the holding at zero without
    an error. Errors read ``manual:<label> <symbol>: ...`` (see `models.classify_error`).
    """
    pinned = {s.upper() for s in stablecoins}
    for holding, _spec in pairs:
        if holding.symbol in pinned:
            holding.price_usd = 1.0
            continue
        try:
            holding.price_usd = float(ticker(holding.symbol) or 0.0)
        except Exception as exc:  # noqa: BLE001 - a pricing failure degrades one source only
            log.debug("manual ticker %s failed: %s", holding.symbol, exc)
            holding.price_usd = 0.0

    by_llama = [h for h, _ in pairs if h.price_usd <= 0 and h.price_id]
    if by_llama and llama is not None:
        try:
            llama(by_llama)
        except Exception as exc:  # noqa: BLE001
            log.debug("manual DefiLlama lookup failed: %s", exc)

    errors = []
    for holding, spec in pairs:
        if holding.price_usd > 0:
            continue
        if spec.price_usd is not None:
            holding.price_usd = float(spec.price_usd)
            continue
        holding.price_usd = 0.0
        tried = "Binance ticker" + (f", DefiLlama {spec.price_id}" if spec.price_id else "")
        errors.append(f"{manual_source(holding.detail)} {holding.symbol}: no market price ({tried}); "
                      "add price_id or price_usd in manual_assets")
    return errors


def fetch_manual(config, binance_client=None, onchain_client=None) -> tuple[list[Holding], list[str], list[str]]:
    """Holdings, errors and expected ``snapshot_sources`` keys of every valid manual asset.

    Reuses the Binance client of the run (its ticker table is already loaded) when given; else a
    keyless client reads the public ticker. Never raises.
    """
    assets = getattr(config, "manual", None) or []
    if not assets:
        return [], [], []
    expected = [manual_source(asset.label) for asset in assets]
    pairs = build_holdings(assets)
    if not pairs:
        return [], [], expected

    if binance_client is None:
        from clients.binance import BinanceClient
        binance_client = BinanceClient("", "", timeout=config.timeout)
    if onchain_client is None:
        from clients.onchain import OnChainClient
        onchain_client = OnChainClient(config.chains, timeout=config.timeout, max_workers=1)

    stablecoins = config.stablecoins
    errors = price_holdings(
        pairs,
        ticker=lambda symbol: binance_client.price_usd(symbol, stablecoins),
        llama=onchain_client.price,
        stablecoins=stablecoins,
    )
    return [holding for holding, _ in pairs], errors, expected


# -- cash flows (M6.3) ------------------------------------------------------------------
@dataclass(frozen=True)
class ManualChange:
    label: str
    symbol: str
    before: float
    after: float
    price_usd: float

    @property
    def delta(self) -> float:
        return self.after - self.before


def _snapshot_row(connection, snapshot_id: int):
    return connection.execute(
        "SELECT id, timestamp, is_backfilled FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()


def _previous_snapshot(connection, current):
    """The fetched snapshot right before `current` (backfilled checkpoints never count)."""
    return connection.execute(
        "SELECT id, timestamp FROM snapshots WHERE is_backfilled = 0 AND id <> ? "
        "AND (timestamp < ? OR (timestamp = ? AND id < ?)) "
        "ORDER BY timestamp DESC, id DESC LIMIT 1",
        (current["id"], current["timestamp"], current["timestamp"], current["id"]),
    ).fetchone()


def _positions(connection, snapshot_id: int) -> dict[tuple[str, str], tuple[float, float]]:
    """``(label, symbol) -> (quantity, price)`` of the manual rows of one snapshot."""
    rows = connection.execute(
        "SELECT detail, symbol, SUM(quantity) AS q, MAX(price_usd) AS p FROM snapshot_assets "
        "WHERE snapshot_id = ? AND source = ? GROUP BY detail, symbol",
        (snapshot_id, MANUAL),
    ).fetchall()
    return {(row["detail"], row["symbol"]): (float(row["q"] or 0.0), float(row["p"] or 0.0))
            for row in rows}


def manual_changes(database: Database, snapshot_id: int) -> tuple[list[ManualChange], object, object]:
    """Quantity changes of manual holdings between `snapshot_id` and the fetched snapshot before it.

    Returns ``(changes, previous_row, current_row)``; no changes when there is no previous
    snapshot (the first snapshot *is* the starting capital, nothing moved).
    """
    with database.connect() as connection:
        current = _snapshot_row(connection, snapshot_id)
        if current is None or current["is_backfilled"]:
            return [], None, current
        previous = _previous_snapshot(connection, current)
        if previous is None:
            return [], None, current
        after = _positions(connection, snapshot_id)
        before = _positions(connection, previous["id"])
    changes = []
    for key in sorted(set(before) | set(after)):
        q0, p0 = before.get(key, (0.0, 0.0))
        q1, p1 = after.get(key, (0.0, 0.0))
        if abs(q1 - q0) <= QUANTITY_EPSILON * max(1.0, abs(q0), abs(q1)):
            continue
        changes.append(ManualChange(label=key[0], symbol=key[1], before=q0, after=q1,
                                    price_usd=p1 if p1 > 0 else p0))
    return changes, previous, current


def external_id(label: str, symbol: str, snapshot_after: int) -> str:
    return f"manual:{label}:{symbol}:{snapshot_after}"


def record_manual_flows(database: Database, snapshot_id: int) -> int:
    """Write one ``cash_flows`` row per manual quantity change; returns how many were new.

    Idempotent: the row key ``(manual, flow_type, manual:<label>:<symbol>:<snapshot>)`` is unique,
    so running it twice on the same snapshot inserts nothing the second time.
    """
    changes, previous, current = manual_changes(database, snapshot_id)
    if not changes:
        return 0
    now = utc_now_iso()
    inserted = 0
    with database.connect() as connection:
        for change in changes:
            incoming = change.delta > 0
            amount = abs(change.delta)
            note = (f"{change.symbol} declared in '{change.label}'" if change.before <= 0
                    else f"{change.symbol} removed from '{change.label}'" if change.after <= 0
                    else f"quantity {change.before:g} -> {change.after:g}")
            cursor = connection.execute(
                """INSERT OR IGNORE INTO cash_flows
                       (timestamp, window_start, source, account, account_label, asset, network,
                        channel, direction, amount, price_usd, value_usd, flow_type, status,
                        detection_method, classified_by, snapshot_id_before, snapshot_id_after,
                        external_id, raw_json, created_at, updated_at, note)
                   VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, 'completed', ?, 'auto', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    current["timestamp"], previous["timestamp"], MANUAL, change.label, change.label,
                    change.symbol, CHANNEL_MANUAL, "in" if incoming else "out", amount,
                    change.price_usd, amount * change.price_usd,
                    "deposit" if incoming else "withdrawal", DETECTION_MANUAL,
                    previous["id"], current["id"],
                    external_id(change.label, change.symbol, current["id"]),
                    f'{{"before": {change.before!r}, "after": {change.after!r}}}', now, now, note,
                ),
            )
            inserted += max(cursor.rowcount, 0)
    return inserted


# -- internal transfer pairing ----------------------------------------------------------
def _amount_gap(sent: float, fee: float, received: float) -> float:
    """Relative gap between what left and what arrived, with or without the fee taken off.

    Whether Binance's withdrawal ``amount`` already excludes ``transactionFee`` is unverified
    (cash_flows.py header), so both readings are tried and the closer one wins.
    """
    if sent <= 0:
        return float("inf")
    return min(abs(sent - fee - received), abs(sent - received)) / sent


def match_manual_transfers(database: Database, settings: CashFlowSettings | None = None) -> int:
    """Pair manual quantity changes with opposite Binance crypto flows; returns pairs made.

    Only automatic, unpaired, completed rows take part, so a pairing is made once and a user
    decision (``classified_by = 'user'``) is never touched.
    """
    settings = settings or CashFlowSettings()
    tolerance = settings.amount_tolerance_pct / 100.0
    transit = timedelta(hours=settings.in_transit_hours)
    with database.connect() as connection:
        manual_rows = connection.execute(
            "SELECT * FROM cash_flows WHERE source = ? AND detection_method = ? "
            "AND status = 'completed' AND is_internal_transfer = 0 AND matched_flow_id IS NULL "
            "AND classified_by = 'auto' AND flow_type IN ('deposit', 'withdrawal')",
            (MANUAL, DETECTION_MANUAL),
        ).fetchall()
        if not manual_rows:
            return 0
        assets = sorted({row["asset"] for row in manual_rows})
        marks = ",".join("?" for _ in assets)
        exchange_rows = connection.execute(
            f"SELECT * FROM cash_flows WHERE source = 'binance' AND channel = 'crypto' "
            f"AND status = 'completed' AND is_internal_transfer = 0 AND matched_flow_id IS NULL "
            f"AND classified_by = 'auto' AND flow_type IN ('deposit', 'withdrawal') "
            f"AND asset IN ({marks})",
            assets,
        ).fetchall()

        candidates = []
        for manual in manual_rows:
            seen_at = parse_timestamp(manual["timestamp"])
            since = parse_timestamp(manual["window_start"] or manual["timestamp"])
            low, high = since - transit, seen_at + transit
            for exchange in exchange_rows:
                if exchange["asset"] != manual["asset"] or exchange["direction"] == manual["direction"]:
                    continue
                moment = parse_timestamp(exchange["timestamp"])
                if not low <= moment <= high:
                    continue
                if manual["direction"] == "in":  # Binance -> cold wallet
                    gap = _amount_gap(float(exchange["amount"]), float(exchange["fee_amount"] or 0.0),
                                      float(manual["amount"]))
                else:                            # cold wallet -> Binance
                    gap = _amount_gap(float(manual["amount"]), 0.0, float(exchange["amount"]))
                if gap > tolerance:
                    continue
                # distance of the Binance flow outside the bracketing snapshots, 0 when inside
                outside = max((since - moment).total_seconds(), (moment - seen_at).total_seconds(), 0.0)
                score = (1.0
                         - 0.25 * (min(1.0, gap / tolerance) if tolerance > 0 else 0.0)
                         - 0.25 * (min(1.0, outside / transit.total_seconds()) if transit.total_seconds() > 0 else 0.0))
                candidates.append((score, manual["id"], exchange["id"], manual["account_label"]))

        candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
        used: set[int] = set()
        now = utc_now_iso()
        pairs = 0
        for score, manual_id, exchange_id, label in candidates:
            if manual_id in used or exchange_id in used:
                continue
            used.update((manual_id, exchange_id))
            for row_id, other, counterparty in ((manual_id, exchange_id, "Binance"),
                                                (exchange_id, manual_id, label)):
                connection.execute(
                    "UPDATE cash_flows SET is_internal_transfer = 1, matched_flow_id = ?, "
                    "match_method = ?, match_confidence = ?, counterparty_label = ?, updated_at = ? "
                    "WHERE id = ?",
                    (other, MATCH_HEURISTIC, round(score, 4), counterparty, now, row_id),
                )
            pairs += 1
    return pairs


def reprice_manual_flows(database: Database, stablecoins: Iterable[str] = ()) -> int:
    """Price manual flows stored at $0 (the asset was unpriced in both snapshots) once a later
    snapshot knows the price. Uses the snapshot history only, never the network."""
    from services.cash_flows import PriceBook

    prices = PriceBook(database, stablecoins)
    repriced = 0
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT id, asset, timestamp, amount FROM cash_flows WHERE source = ? AND price_usd <= 0",
            (MANUAL,)).fetchall()
        for row in rows:
            price = prices.price(row["asset"], row["timestamp"])
            if price > 0:
                connection.execute(
                    "UPDATE cash_flows SET price_usd = ?, value_usd = ?, updated_at = ? WHERE id = ?",
                    (price, float(row["amount"]) * price, utc_now_iso(), row["id"]))
                repriced += 1
    return repriced


def after_snapshot(config, database: Database, snapshot_id: int, stream=None) -> dict | None:
    """The collector hook: manual flows for `snapshot_id`, then pairing. Never raises.

    Runs after the snapshot is committed (``main.py record``, ``POST /api/portfolio/refresh``),
    so nothing here can lose a snapshot; problems are printed to `stream` (stderr).
    """
    stream = stream or sys.stderr
    try:
        inserted = record_manual_flows(database, snapshot_id)
        repriced = reprice_manual_flows(database, getattr(config, "stablecoins", ()) or ())
        paired = match_manual_transfers(database, getattr(config, "cash_flow_settings", None))
    except Exception as exc:  # noqa: BLE001 - the snapshot is already safe; report and move on
        print(f"  warning: manual-asset flows failed: {exc}", file=stream)
        return None
    return {"inserted": inserted, "repriced": repriced, "paired": paired}


def last_changes(database: Database) -> dict[str, str]:
    """``label -> timestamp`` of the newest recorded quantity change of each manual asset."""
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT account, MAX(timestamp) AS ts FROM cash_flows WHERE source = ? GROUP BY account",
            (MANUAL,)).fetchall()
    return {row["account"]: row["ts"] for row in rows}
