"""Cash-flow engine: money entering or leaving the portfolio (ROADMAP_LEDGER Epic F2).

A quantity change is either profit (price moves, Earn interest) or a cash flow (a deposit, a
withdrawal). The flows come from Binance's read-only history endpoints:

* crypto deposits / withdrawals  - ``/sapi/v1/capital/deposit/hisrec``, ``.../withdraw/history``
* fiat bank deposits / withdrawals - ``/sapi/v1/fiat/orders``
* crypto bought / sold with fiat   - ``/sapi/v1/fiat/payments``
* P2P buy / sell orders            - ``/sapi/v1/c2c/orderMatch/listUserOrderHistory``

and land in the ``cash_flows`` table (schema v4), one row per Binance record, upserted on
``(source, flow_type, external_id)`` so re-reading a range never duplicates anything.

**Internal transfers.** A Binance withdrawal whose destination ``address`` is one of the wallets
configured in ``wallets:`` - on a chain that wallet is tracked on - does not leave the portfolio:
the coins reappear in the wallet's on-chain balance. Such a row gets ``is_internal_transfer = 1``
(``match_method = 'address'``, confidence 1.0) and is not a cash flow. The same holds for a
deposit whose ``sourceAddress`` (when Binance provides it) is an owned wallet. Withdrawing to an
owned address on a chain the wallet is *not* tracked on does take the coins out of the tracked
portfolio, so it stays external. Fiat and P2P orders are always external.

**Sync.** ``sync()`` reads each stream from its ``sync_state`` cursor minus ``overlap_days``
(pending -> completed transitions), or ``backfill_days`` back on the first run, and commits the
cursor after every time chunk, so an interrupted backfill resumes. It runs after the snapshot is
committed (``main.py record``) and its errors never fail the snapshot.

[to verify against the live API docs] (ROADMAP_LEDGER §3.4): the status codes below, whether a
withdrawal ``amount`` is net of ``transactionFee``, and the ``sourceAddress`` field of deposits.
"""
from __future__ import annotations

import bisect
import json
import logging
import re
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone

import requests

from clients.binance import DAY_MS, HISTORY_STREAMS, BinanceClient, BinanceError
from config import CashFlowSettings
from services.db import Database, utc_now_iso

log = logging.getLogger(__name__)

SOURCE_BINANCE = "binance"
DETECTION_BINANCE_API = "binance_api"

CHANNEL_CRYPTO = "crypto"
CHANNEL_FIAT = "fiat"
CHANNEL_FIAT_PAYMENT = "fiat_payment"
CHANNEL_P2P = "p2p"

DEPOSIT, WITHDRAWAL = "deposit", "withdrawal"
EXTERNAL_FLOW_TYPES = (DEPOSIT, WITHDRAWAL)
IN, OUT = "in", "out"
PENDING, COMPLETED, FAILED = "pending", "completed", "failed"
CLASSIFIED_AUTO, CLASSIFIED_USER = "auto", "user"
MATCH_ADDRESS = "address"

CRYPTO_STREAMS = ("deposit", "withdraw")
FIAT_STREAMS = ("fiat_deposit", "fiat_withdraw", "fiat_buy", "fiat_sell")
P2P_STREAMS = ("p2p_buy", "p2p_sell")

# Binance network name -> chain id used in config.yaml (ROADMAP_LEDGER F2.4). An unknown
# network maps to nothing, so it can never produce an address match.
NETWORK_TO_CHAIN = {
    "ETH": "ethereum",
    "BSC": "bsc",
    "ARBITRUM": "arbitrum",
    "BASE": "base",
    "MATIC": "polygon",
    "POL": "polygon",
    "POLYGON": "polygon",
    "SOL": "solana",
}
SOLANA_CHAIN = "solana"
EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Binance status codes (ROADMAP_LEDGER §3.4). Deposit 6 = "credited but cannot withdraw yet":
# the coins are already in the balance, so for the portfolio it is completed.
DEPOSIT_STATUS = {1: COMPLETED, 6: COMPLETED, 2: FAILED, 7: FAILED}            # else pending (0, 8)
WITHDRAW_STATUS = {6: COMPLETED, 1: FAILED, 3: FAILED, 5: FAILED}              # else pending (0, 2, 4)
FIAT_STATUS = {"successful": COMPLETED, "finished": COMPLETED, "completed": COMPLETED,
               "failed": FAILED, "refunded": FAILED, "refund failed": FAILED, "expired": FAILED,
               "canceled": FAILED, "cancelled": FAILED}                         # else pending
P2P_STATUS = {"COMPLETED": COMPLETED, "CANCELLED": FAILED, "CANCELLED_BY_SYSTEM": FAILED}

USD_PEGGED = frozenset({"USD"})  # fiat USD itself is never a ticker


# -- data ------------------------------------------------------------------------
@dataclass(frozen=True)
class CashFlow:
    """One row of ``cash_flows``, before it has an id."""

    timestamp: str
    asset: str
    direction: str
    amount: float
    flow_type: str
    external_id: str
    status: str = COMPLETED
    channel: str = CHANNEL_CRYPTO
    source: str = SOURCE_BINANCE
    account: str = SOURCE_BINANCE
    account_label: str = "Binance"
    network: str = ""
    fee_amount: float = 0.0
    fee_asset: str = ""
    price_usd: float = 0.0
    tx_hash: str = ""
    counterparty_address: str = ""
    counterparty_label: str = ""
    is_internal_transfer: bool = False
    match_method: str = ""
    match_confidence: float = 0.0
    detection_method: str = DETECTION_BINANCE_API
    classified_by: str = CLASSIFIED_AUTO
    raw_json: str = "{}"
    note: str = ""

    @property
    def value_usd(self) -> float:
        return self.amount * self.price_usd

    @property
    def signed_value_usd(self) -> float:
        """+value for money in, -value for money out (ledger §3.3 sign convention)."""
        return self.value_usd if self.direction == IN else -self.value_usd

    @property
    def is_external(self) -> bool:
        """Counts as a cash flow of the portfolio: a completed deposit/withdrawal, not internal."""
        return (self.status == COMPLETED and self.flow_type in EXTERNAL_FLOW_TYPES
                and not self.is_internal_transfer)


@dataclass(frozen=True)
class OwnedWallet:
    address: str
    label: str
    chains: frozenset[str]


@dataclass
class SyncReport:
    """What one `sync()` run did. `errors` are per stream; the other streams still ran."""

    skipped: str = ""
    fetched: dict[str, int] = field(default_factory=dict)
    inserted: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    reclassified: int = 0
    repriced: int = 0

    @property
    def total_inserted(self) -> int:
        return sum(self.inserted.values())

    @property
    def total_updated(self) -> int:
        return sum(self.updated.values())

    def to_dict(self) -> dict:
        return {**asdict(self), "total_inserted": self.total_inserted,
                "total_updated": self.total_updated}


# -- normalisation (F2.4) ----------------------------------------------------------
def chain_for_network(network: str) -> str:
    """Binance network name -> config chain id (``ETH`` -> ``ethereum``); '' when unknown."""
    return NETWORK_TO_CHAIN.get(str(network or "").strip().upper(), "")


def normalize_address(address) -> str:
    """EVM addresses are case-insensitive: lowercase them. Solana (base58) is case-sensitive."""
    text = str(address or "").strip()
    return text.lower() if EVM_ADDRESS_RE.match(text) else text


def to_iso_utc(value) -> str:
    """Epoch ms/s (int or numeric string), ``YYYY-MM-DD HH:MM:SS`` (UTC) or ISO8601 -> ISO UTC.

    The output has the shape of the snapshot timestamps (``2026-11-25T03:00:00+00:00``), so
    the two compare correctly as strings. Returns '' when the value is missing or unreadable.
    """
    if value is None or isinstance(value, bool) or value == "":
        return ""
    moment: datetime | None = None
    number = None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and re.fullmatch(r"\s*\d+(\.\d+)?\s*", value):
        number = float(value)
    if number is not None:
        if number <= 0:
            return ""
        seconds = number / 1000 if number > 1e11 else number
        try:
            moment = datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return ""
    else:
        try:
            moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return ""
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)  # Binance's "applyTime" strings are UTC
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _float(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number else 0.0  # NaN -> 0


def _int(value, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _raw(row: dict) -> str:
    return json.dumps(row, sort_keys=True, default=str)


# -- parsers: one Binance row -> CashFlow (or None when unusable) ---------------------
def parse_deposit(row: dict) -> CashFlow | None:
    """A crypto deposit into Binance (``capital/deposit/hisrec``)."""
    asset = str(row.get("coin") or "").upper()
    amount = _float(row.get("amount"))
    timestamp = to_iso_utc(row.get("completeTime")) or to_iso_utc(row.get("insertTime"))
    external_id = str(row.get("id") or "") or (
        f"{row.get('txId', '')}:{asset}:{row.get('insertTime', '')}" if row.get("txId") else "")
    if not asset or amount <= 0 or not timestamp or not external_id:
        return None
    transfer_type = _int(row.get("transferType"), 0)
    return CashFlow(
        timestamp=timestamp, asset=asset, direction=IN, amount=amount, flow_type=DEPOSIT,
        external_id=external_id, status=DEPOSIT_STATUS.get(_int(row.get("status")), PENDING),
        network=str(row.get("network") or "").upper(), tx_hash=str(row.get("txId") or ""),
        counterparty_address=normalize_address(row.get("sourceAddress")),
        raw_json=_raw(row),
        note="binance internal transfer (off-chain)" if transfer_type == 1 else "",
    )


def parse_withdrawal(row: dict) -> CashFlow | None:
    """A crypto withdrawal out of Binance (``capital/withdraw/history``)."""
    asset = str(row.get("coin") or "").upper()
    amount = _float(row.get("amount"))
    timestamp = to_iso_utc(row.get("completeTime")) or to_iso_utc(row.get("applyTime"))
    external_id = str(row.get("id") or "")
    if not asset or amount <= 0 or not timestamp or not external_id:
        return None
    fee = max(_float(row.get("transactionFee")), 0.0)
    transfer_type = _int(row.get("transferType"), 0)
    return CashFlow(
        timestamp=timestamp, asset=asset, direction=OUT, amount=amount, flow_type=WITHDRAWAL,
        external_id=external_id, status=WITHDRAW_STATUS.get(_int(row.get("status")), PENDING),
        network=str(row.get("network") or "").upper(), tx_hash=str(row.get("txId") or ""),
        counterparty_address=normalize_address(row.get("address")),
        fee_amount=fee, fee_asset=asset if fee else "", raw_json=_raw(row),
        note="binance internal transfer (off-chain)" if transfer_type == 1 else "",
    )


def _fiat_status(value) -> str:
    return FIAT_STATUS.get(str(value or "").strip().lower(), PENDING)


def parse_fiat_order(row: dict, transaction_type: int) -> CashFlow | None:
    """A fiat bank deposit (0) or withdrawal (1) (``fiat/orders``): EUR, USD... in or out."""
    asset = str(row.get("fiatCurrency") or "").upper()
    amount = _float(row.get("amount")) or _float(row.get("indicatedAmount"))
    timestamp = to_iso_utc(row.get("updateTime")) or to_iso_utc(row.get("createTime"))
    order = str(row.get("orderNo") or "")
    if not asset or amount <= 0 or not timestamp or not order:
        return None
    fee = max(_float(row.get("totalFee")), 0.0)
    withdrawal = int(transaction_type) == 1
    return CashFlow(
        timestamp=timestamp, asset=asset, direction=OUT if withdrawal else IN, amount=amount,
        flow_type=WITHDRAWAL if withdrawal else DEPOSIT, external_id=f"fiat:{order}",
        status=_fiat_status(row.get("status")), channel=CHANNEL_FIAT,
        fee_amount=fee, fee_asset=asset if fee else "", raw_json=_raw(row),
        note=str(row.get("method") or ""),
    )


def parse_fiat_payment(row: dict, transaction_type: int) -> CashFlow | None:
    """Crypto bought (0) or sold (1) with fiat (``fiat/payments``, card / bank transfer)."""
    asset = str(row.get("cryptoCurrency") or "").upper()
    sell = int(transaction_type) == 1
    amount = _float(row.get("sourceAmount") if sell else row.get("obtainAmount"))
    timestamp = to_iso_utc(row.get("updateTime")) or to_iso_utc(row.get("createTime"))
    order = str(row.get("orderNo") or "")
    if not asset or amount <= 0 or not timestamp or not order:
        return None
    fee = max(_float(row.get("totalFee")), 0.0)
    fiat = str(row.get("fiatCurrency") or "").upper()
    return CashFlow(
        timestamp=timestamp, asset=asset, direction=OUT if sell else IN, amount=amount,
        flow_type=WITHDRAWAL if sell else DEPOSIT, external_id=f"fiatpay:{order}",
        status=_fiat_status(row.get("status")), channel=CHANNEL_FIAT_PAYMENT,
        fee_amount=fee, fee_asset=fiat if fee else "", raw_json=_raw(row),
        note=" ".join(part for part in (str(row.get("paymentMethod") or ""), fiat) if part),
    )


def parse_p2p_order(row: dict) -> CashFlow | None:
    """A P2P order (``c2c/orderMatch/listUserOrderHistory``): BUY brings crypto in, SELL takes it out."""
    asset = str(row.get("asset") or "").upper()
    amount = _float(row.get("amount"))
    timestamp = to_iso_utc(row.get("createTime"))
    order = str(row.get("orderNumber") or "")
    if not asset or amount <= 0 or not timestamp or not order:
        return None
    sell = str(row.get("tradeType") or "").upper() == "SELL"
    fee = max(_float(row.get("commission")), 0.0)
    fiat_total = _float(row.get("totalPrice"))
    fiat = str(row.get("fiat") or "").upper()
    return CashFlow(
        timestamp=timestamp, asset=asset, direction=OUT if sell else IN, amount=amount,
        flow_type=WITHDRAWAL if sell else DEPOSIT, external_id=f"p2p:{order}",
        status=P2P_STATUS.get(str(row.get("orderStatus") or "").upper(), PENDING),
        channel=CHANNEL_P2P, fee_amount=fee, fee_asset=asset if fee else "", raw_json=_raw(row),
        note=f"{fiat_total:.2f} {fiat}".strip() if fiat_total else "",
    )


PARSERS: dict[str, Callable[[dict], CashFlow | None]] = {
    "deposit": parse_deposit,
    "withdraw": parse_withdrawal,
    "fiat_deposit": lambda row: parse_fiat_order(row, 0),
    "fiat_withdraw": lambda row: parse_fiat_order(row, 1),
    "fiat_buy": lambda row: parse_fiat_payment(row, 0),
    "fiat_sell": lambda row: parse_fiat_payment(row, 1),
    "p2p_buy": parse_p2p_order,
    "p2p_sell": parse_p2p_order,
}
assert set(PARSERS) == set(HISTORY_STREAMS), "every history stream needs a parser"


def parse_rows(stream: str, rows: Iterable[dict]) -> list[CashFlow]:
    parser = PARSERS[stream]
    flows = []
    for row in rows:
        flow = parser(row) if isinstance(row, dict) else None
        if flow is None:
            log.debug("skipping unusable %s row: %s", stream, row)
            continue
        flows.append(flow)
    return flows


# -- internal transfer matching (tier 1: address) -----------------------------------
def owned_addresses(config) -> dict[str, OwnedWallet]:
    """Every configured wallet: normalised address -> label and the chains it is tracked on.

    An EVM wallet without an explicit ``chains`` list is tracked on every EVM chain of the
    config; a Solana wallet only on Solana. An address listed twice gets the union.
    """
    chains_config = getattr(config, "chains", {}) or {}
    evm_chains = frozenset(name for name in chains_config if name != SOLANA_CHAIN)
    owned: dict[str, OwnedWallet] = {}

    def add(address: str, label: str, chains: Iterable[str]) -> None:
        key = normalize_address(address)
        if not key:
            return
        chains = frozenset(str(c).strip().lower() for c in chains if str(c).strip())
        if key in owned:
            previous = owned[key]
            owned[key] = OwnedWallet(key, previous.label, previous.chains | chains)
        else:
            owned[key] = OwnedWallet(key, label or key, chains)

    for wallet in getattr(config, "evm_wallets", []) or []:
        add(wallet.get("address"), wallet.get("label", ""), wallet.get("chains") or evm_chains)
    for wallet in getattr(config, "solana_wallets", []) or []:
        add(wallet.get("address"), wallet.get("label", ""), [SOLANA_CHAIN])
    return owned


def classify(flow: CashFlow, owned: dict[str, OwnedWallet]) -> CashFlow:
    """Tier-1 matching (ROADMAP_LEDGER §3.6): is this Binance crypto flow an internal transfer?

    Internal when the counterparty address is an owned wallet **and** the Binance network maps
    to a chain that wallet is tracked on. A user decision (``classified_by = 'user'``) is kept.
    """
    if flow.classified_by == CLASSIFIED_USER:
        return flow
    external = replace(flow, is_internal_transfer=False, match_method="", match_confidence=0.0,
                       counterparty_label="")
    if flow.channel != CHANNEL_CRYPTO or flow.source != SOURCE_BINANCE:
        return external
    if flow.flow_type not in EXTERNAL_FLOW_TYPES:
        return external
    wallet = owned.get(normalize_address(flow.counterparty_address))
    chain = chain_for_network(flow.network)
    if wallet is None or not chain or chain not in wallet.chains:
        return external
    return replace(flow, is_internal_transfer=True, match_method=MATCH_ADDRESS,
                   match_confidence=1.0, counterparty_label=wallet.label)


# -- pricing -------------------------------------------------------------------------
class PriceBook:
    """USD price of an asset at a moment: the nearest snapshot price, else a live fallback.

    Uses the newest snapshot at or before the moment that priced the asset, else the oldest
    one after it, else `fallback(symbol)` (the live Binance ticker), else 0.0 ("unknown",
    re-tried on the next sync). Stablecoins and fiat USD are pinned at $1.00.
    """

    def __init__(self, database: Database, stablecoins: Iterable[str] = (),
                 fallback: Callable[[str], float] | None = None):
        self.database = database
        self.pinned = {s.upper() for s in stablecoins} | USD_PEGGED
        self.fallback = fallback
        self._series: dict[str, tuple[list[str], list[float]]] = {}
        self._live: dict[str, float] = {}

    def _load(self, symbol: str) -> tuple[list[str], list[float]]:
        if symbol not in self._series:
            with self.database.connect() as connection:
                rows = connection.execute(
                    """SELECT s.timestamp AS ts, AVG(a.price_usd) AS price
                       FROM snapshot_assets a JOIN snapshots s ON s.id = a.snapshot_id
                       WHERE a.symbol = ? AND a.price_usd > 0
                       GROUP BY s.id ORDER BY s.timestamp, s.id""",
                    (symbol,),
                ).fetchall()
            self._series[symbol] = ([r["ts"] for r in rows], [float(r["price"]) for r in rows])
        return self._series[symbol]

    def price(self, symbol: str, timestamp: str) -> float:
        symbol = str(symbol or "").upper()
        if not symbol:
            return 0.0
        if symbol in self.pinned:
            return 1.0
        times, prices = self._load(symbol)
        if times:
            index = bisect.bisect_right(times, timestamp)
            return prices[index - 1] if index > 0 else prices[0]
        if self.fallback is None:
            return 0.0
        if symbol not in self._live:
            try:
                self._live[symbol] = float(self.fallback(symbol) or 0.0)
            except (requests.RequestException, BinanceError, TypeError, ValueError):
                self._live[symbol] = 0.0
        return self._live[symbol]

    def apply(self, flow: CashFlow) -> CashFlow:
        return replace(flow, price_usd=self.price(flow.asset, flow.timestamp))


# -- storage ---------------------------------------------------------------------------
_COLUMNS = (
    "timestamp", "source", "account", "account_label", "asset", "network", "channel",
    "direction", "amount", "fee_amount", "fee_asset", "price_usd", "value_usd", "flow_type",
    "status", "tx_hash", "counterparty_address", "counterparty_label", "is_internal_transfer",
    "match_method", "match_confidence", "detection_method", "classified_by", "external_id",
    "raw_json", "note",
)
# Facts that come from the source of truth and are refreshed on every re-read.
_FACT_COLUMNS = ("timestamp", "status", "tx_hash", "amount", "fee_amount", "fee_asset",
                 "network", "counterparty_address", "raw_json")
# The classification: refreshed only while classified_by = 'auto'.
_CLASS_COLUMNS = ("is_internal_transfer", "match_method", "match_confidence", "counterparty_label")


def _row_values(flow: CashFlow) -> dict:
    return {
        **{name: getattr(flow, name) for name in _COLUMNS if name != "value_usd"},
        "value_usd": flow.value_usd,
        "is_internal_transfer": int(flow.is_internal_transfer),
    }


def upsert_flows(database: Database, flows: Iterable[CashFlow]) -> tuple[int, int]:
    """Insert new flows, refresh known ones; returns ``(inserted, updated)``.

    Keyed on ``(source, flow_type, external_id)``. On a known row, the facts (status, time,
    tx hash, amounts...) are refreshed; the price is kept once known; the classification is
    refreshed only while ``classified_by = 'auto'`` - a user override always survives.
    """
    inserted = updated = 0
    now = utc_now_iso()
    with database.connect() as connection:
        for flow in flows:
            values = _row_values(flow)
            existing = connection.execute(
                "SELECT * FROM cash_flows WHERE source = ? AND flow_type = ? AND external_id = ?",
                (flow.source, flow.flow_type, flow.external_id),
            ).fetchone()
            if existing is None:
                columns = (*_COLUMNS, "created_at", "updated_at")
                connection.execute(
                    f"INSERT INTO cash_flows ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    [values[name] for name in _COLUMNS] + [now, now],
                )
                inserted += 1
                continue
            changes = {name: values[name] for name in _FACT_COLUMNS if existing[name] != values[name]}
            price = float(existing["price_usd"]) if existing["price_usd"] > 0 else values["price_usd"]
            if price != existing["price_usd"]:
                changes["price_usd"] = price
            value = values["amount"] * price
            if abs(value - float(existing["value_usd"])) > 1e-9:
                changes["value_usd"] = value
            if existing["classified_by"] == CLASSIFIED_AUTO:
                changes.update({name: values[name] for name in _CLASS_COLUMNS
                                if existing[name] != values[name]})
            if not changes:
                continue
            changes["updated_at"] = now
            connection.execute(
                f"UPDATE cash_flows SET {', '.join(f'{name} = ?' for name in changes)} WHERE id = ?",
                [*changes.values(), existing["id"]],
            )
            updated += 1
    return inserted, updated


def _flow_from_row(row) -> CashFlow:
    return CashFlow(**{name: row[name] for name in _COLUMNS if name != "value_usd"}
                    | {"is_internal_transfer": bool(row["is_internal_transfer"])})


def reclassify(database: Database, owned: dict[str, OwnedWallet]) -> int:
    """Re-run the address matching on every automatic row; returns how many changed.

    Adding or removing a wallet in the config re-labels the whole history, not just the rows
    the next sync window happens to re-read.
    """
    changed = 0
    now = utc_now_iso()
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM cash_flows WHERE classified_by = 'auto' AND source = ? AND detection_method = ?",
            (SOURCE_BINANCE, DETECTION_BINANCE_API),
        ).fetchall()
        for row in rows:
            flow = classify(_flow_from_row(row), owned)
            values = _row_values(flow)
            changes = {name: values[name] for name in _CLASS_COLUMNS if row[name] != values[name]}
            if changes:
                connection.execute(
                    f"UPDATE cash_flows SET {', '.join(f'{name} = ?' for name in changes)}, "
                    "updated_at = ? WHERE id = ?",
                    [*changes.values(), now, row["id"]],
                )
                changed += 1
    return changed


def reprice_missing(database: Database, prices: PriceBook) -> int:
    """Give a price to rows stored with price 0 (unknown at the time); returns how many."""
    repriced = 0
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT id, asset, timestamp, amount FROM cash_flows WHERE price_usd <= 0").fetchall()
        for row in rows:
            price = prices.price(row["asset"], row["timestamp"])
            if price > 0:
                connection.execute(
                    "UPDATE cash_flows SET price_usd = ?, value_usd = ?, updated_at = ? WHERE id = ?",
                    (price, float(row["amount"]) * price, utc_now_iso(), row["id"]),
                )
                repriced += 1
    return repriced


def get_cursor(database: Database, key: str) -> int | None:
    with database.connect() as connection:
        row = connection.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    value = _int(row["value"], default=-1)
    return value if value >= 0 else None


def set_cursor(database: Database, key: str, value_ms: int) -> None:
    """Move a cursor forward (never back: a re-read of an older range must not rewind it)."""
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = CASE WHEN CAST(excluded.value AS INTEGER) > CAST(sync_state.value AS INTEGER)
                                THEN excluded.value ELSE sync_state.value END,
                   updated_at = excluded.updated_at""",
            (key, str(int(value_ms)), utc_now_iso()),
        )


def sync_states(database: Database) -> dict[str, str]:
    """Every cursor as ``{key: ISO UTC time it is synced up to}``."""
    with database.connect() as connection:
        rows = connection.execute("SELECT key, value FROM sync_state ORDER BY key").fetchall()
    return {row["key"]: to_iso_utc(_int(row["value"], 0)) for row in rows}


def flows_between(database: Database, start_iso: str, end_iso: str) -> list[dict]:
    """Deposit / withdrawal rows (any status, internal or not) with ``start < timestamp <= end``."""
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM cash_flows WHERE flow_type IN ('deposit', 'withdrawal') "
            "AND timestamp > ? AND timestamp <= ? ORDER BY timestamp, id",
            (start_iso, end_iso),
        ).fetchall()
    return [dict(row) for row in rows]


# -- sync ------------------------------------------------------------------------------
def stream_key(stream: str) -> str:
    return f"{SOURCE_BINANCE}.{stream}"


def enabled_streams(settings) -> list[str]:
    streams = list(CRYPTO_STREAMS)
    if settings.sync_fiat:
        streams += FIAT_STREAMS
    if settings.sync_p2p:
        streams += P2P_STREAMS
    return streams


def _settings(config) -> CashFlowSettings:
    settings = getattr(config, "cash_flow_settings", None)
    return settings if isinstance(settings, CashFlowSettings) else CashFlowSettings()


def _client(config) -> BinanceClient:
    return BinanceClient(
        config.binance.get("api_key", ""), config.binance.get("api_secret", ""),
        timeout=getattr(config, "timeout", 20),
        recv_window=int(config.binance.get("recv_window", 5000) or 5000),
    )


def sync(config, database: Database, client: BinanceClient | None = None,
         now_ms: int | None = None) -> SyncReport:
    """Pull every enabled Binance history stream into ``cash_flows``; never raises on API errors.

    Each stream starts at its cursor minus ``overlap_days`` (or ``backfill_days`` ago on the
    first run) and ends now. The cursor is committed after each time chunk. A failing stream
    (HTTP 401, missing permission, rate limit) is reported in ``errors`` and keeps its cursor;
    the other streams still run. Afterwards the whole history is re-classified against the
    current wallet list and rows still missing a price are re-priced.
    """
    report = SyncReport()
    settings = _settings(config)
    if not settings.enabled:
        report.skipped = "cash_flows.enabled is false"
        return report
    if not getattr(config, "binance_enabled", False):
        report.skipped = "Binance is not configured"
        return report

    client = client or _client(config)
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    owned = owned_addresses(config)
    prices = PriceBook(database, getattr(config, "stablecoins", ()) or (), fallback=client.price_usd)

    for stream in enabled_streams(settings):
        key = stream_key(stream)
        cursor = get_cursor(database, key)
        if cursor is None:
            start_ms = now_ms - settings.backfill_days * DAY_MS
        else:
            start_ms = min(cursor, now_ms) - settings.overlap_days * DAY_MS
        start_ms = max(start_ms, 0)
        report.fetched[stream] = report.inserted[stream] = report.updated[stream] = 0
        try:
            for chunk_end, rows in client.history_chunks(stream, start_ms, now_ms):
                flows = [classify(prices.apply(flow), owned) for flow in parse_rows(stream, rows)]
                inserted, updated = upsert_flows(database, flows)
                set_cursor(database, key, chunk_end)
                report.fetched[stream] += len(rows)
                report.inserted[stream] += inserted
                report.updated[stream] += updated
        except (requests.RequestException, BinanceError, KeyError, TypeError, ValueError) as exc:
            report.errors.append(f"binance {stream.replace('_', ' ')} history: {exc}")

    report.reclassified = reclassify(database, owned)
    report.repriced = reprice_missing(database, prices)
    return report


def sync_after_record(config, database: Database, stream=None) -> SyncReport | None:
    """The collector hook (``main.py record``): sync, print problems to stderr, never raise.

    Runs after the snapshot is committed, so nothing here can lose a snapshot.
    """
    stream = stream or sys.stderr
    try:
        report = sync(config, database)
    except Exception as exc:  # noqa: BLE001 - the snapshot is already safe; report and move on
        print(f"  warning: cash-flow sync failed: {exc}", file=stream)
        return None
    for error in report.errors:
        print(f"  warning: {error}", file=stream)
    return report
