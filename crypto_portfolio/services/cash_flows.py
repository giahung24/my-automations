"""Cash-flow engine: money entering or leaving the portfolio (ROADMAP_LEDGER Epic F2).

A quantity change is either profit (price moves, Earn interest) or a cash flow (a deposit, a
withdrawal). The flows come from Binance's read-only history endpoints:

* crypto deposits / withdrawals  - ``/sapi/v1/capital/deposit/hisrec``, ``.../withdraw/history``
* fiat bank deposits / withdrawals - ``/sapi/v1/fiat/orders``
* crypto bought / sold with fiat   - ``/sapi/v1/fiat/payments``
* P2P buy / sell orders            - ``/sapi/v1/c2c/orderMatch/listUserOrderHistory``
* Simple Earn rewards (income)     - ``/sapi/v1/simple-earn/{flexible,locked}/history/rewardsRecord``

and land in the ``cash_flows`` table (schema v4), one row per Binance record, upserted on
``(source, flow_type, external_id)`` so re-reading a range never duplicates anything. Earn rewards
are ``flow_type = 'reward'`` (channel ``earn``): income, so never part of the external cash flow.

**Internal transfers.** A Binance withdrawal whose destination ``address`` is one of the wallets
configured in ``wallets:`` - on a chain that wallet is tracked on - does not leave the portfolio:
the coins reappear in the wallet's on-chain balance. Such a row gets ``is_internal_transfer = 1``
(``match_method = 'address'``, confidence 1.0) and is not a cash flow. The same holds for a
deposit whose ``sourceAddress`` (when Binance provides it) is an owned wallet. Withdrawing to an
owned address on a chain the wallet is *not* tracked on does take the coins out of the tracked
portfolio, so it stays external. Fiat and P2P orders are always external.

**Pairing (M4).** :func:`match_transfers` is the pure matcher of ROADMAP_LEDGER §3.6: it pairs an
``out`` flow with the ``in`` flow on another account that received it - by owned address (tier 1),
by identical tx hash (tier 2) or by amount + time (tier 3, heuristic, confidence in [0.6, 1)). Both
legs then point at each other through ``matched_flow_id``. It needs a flow on each side, so it pays
off once wallet-side flows exist (on-chain deltas, F2.5); a Binance withdrawal to an owned wallet is
already internal on its own through tier 1.

**Sync.** ``sync()`` reads each stream from its ``sync_state`` cursor minus ``overlap_days``
(pending -> completed transitions), or ``backfill_days`` back on the first run (5 years by
default), and commits the cursor after every time chunk, so an interrupted backfill resumes. A
second marker, ``backfill.binance.<stream>``, records how far back a stream is known; raising
``backfill_days`` (or a database synced before the 5-year default) back-fills only the missing
older range, newest chunk first, moving that marker back after each chunk. It runs after the
snapshot is committed (``main.py record``) and its errors never fail the snapshot.

**Earn retention.** Simple Earn rewards arrive several times a day per asset, so they are synced
and *kept* only ``earn_backfill_days`` back (1 year by default, never more than
``backfill_days``), while deposits / withdrawals / fiat / P2P keep the full ``backfill_days``.
Every sync deletes the ``reward`` rows older than that and moves the Earn streams' backfill
marker up to the cut-off, so the pruned range is never fetched again (:func:`prune_earn_rewards`).

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
CHANNEL_EARN = "earn"
CHANNEL_ONCHAIN = "onchain"

DEPOSIT, WITHDRAWAL, REWARD = "deposit", "withdrawal", "reward"
EXTERNAL_FLOW_TYPES = (DEPOSIT, WITHDRAWAL)
IN, OUT = "in", "out"
PENDING, COMPLETED, FAILED = "pending", "completed", "failed"
CLASSIFIED_AUTO, CLASSIFIED_USER = "auto", "user"
MATCH_ADDRESS = "address"
MATCH_TX_HASH = "tx_hash"
MATCH_HEURISTIC = "heuristic"
MATCH_MANUAL = "manual"
HEURISTIC_MIN_SCORE = 0.6

CRYPTO_STREAMS = ("deposit", "withdraw")
FIAT_STREAMS = ("fiat_deposit", "fiat_withdraw", "fiat_buy", "fiat_sell")
P2P_STREAMS = ("p2p_buy", "p2p_sell")
EARN_STREAMS = ("earn_flexible", "earn_flexible_bonus", "earn_locked")
BACKFILL_PREFIX = "backfill."

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
    paired: int = 0  # Binance flows paired with a manual-asset change (services/manual.py)
    matched: int = 0  # out/in pairs made by the transfer matcher (match_transfers)
    pruned: int = 0  # Earn reward rows older than earn_backfill_days, deleted (prune_earn_rewards)

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


def parse_earn_flexible(row: dict) -> CashFlow | None:
    """One Simple Earn flexible reward (``flexible/history/rewardsRecord``): income, never a flow.

    Binance gives no id; asset + product + reward type + time identify a record.
    """
    asset = str(row.get("asset") or "").upper()
    amount = _float(row.get("rewards")) or _float(row.get("amount"))
    timestamp = to_iso_utc(row.get("time"))
    if not asset or amount <= 0 or not timestamp:
        return None
    kind = str(row.get("type") or "").upper()
    project = str(row.get("projectId") or row.get("productId") or asset)
    return CashFlow(
        timestamp=timestamp, asset=asset, direction=IN, amount=amount, flow_type=REWARD,
        external_id=f"earnflex:{kind}:{project}:{_int(row.get('time'), 0)}",
        channel=CHANNEL_EARN, raw_json=_raw(row), note=f"Simple Earn flexible {kind.lower()}".strip(),
    )


def parse_earn_locked(row: dict) -> CashFlow | None:
    """One Simple Earn locked reward (``locked/history/rewardsRecord``)."""
    asset = str(row.get("asset") or row.get("rewardAsset") or "").upper()
    amount = _float(row.get("amount")) or _float(row.get("rewards"))
    timestamp = to_iso_utc(row.get("time"))
    if not asset or amount <= 0 or not timestamp:
        return None
    position = str(row.get("positionId") or row.get("projectId") or "")
    return CashFlow(
        timestamp=timestamp, asset=asset, direction=IN, amount=amount, flow_type=REWARD,
        external_id=f"earnlock:{position}:{asset}:{_int(row.get('time'), 0)}",
        channel=CHANNEL_EARN, raw_json=_raw(row),
        note=f"Simple Earn locked {row.get('lockPeriod')}d" if row.get("lockPeriod") else "Simple Earn locked",
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
    "earn_flexible": parse_earn_flexible,
    "earn_flexible_bonus": parse_earn_flexible,
    "earn_locked": parse_earn_locked,
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


# -- internal transfer matching (tiers 1-3: pairing an out leg with its in leg) -------
@dataclass(frozen=True)
class TransferMatch:
    """One out/in pair: both legs become ``is_internal_transfer = 1`` and point at each other."""

    out_id: int
    in_id: int
    method: str
    confidence: float


def flow_interval(row) -> tuple[datetime, datetime]:
    """When the flow happened: a point for an API record, ``(window_start, timestamp]`` for a
    flow inferred from two snapshots."""
    end = _moment(row["timestamp"])
    start = _moment(row["window_start"]) if _get(row, "window_start") else None
    return (start if start is not None and start <= end else end), end


def _moment(value) -> datetime:
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _get(row, name, default=None):
    try:
        value = row[name]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def interval_gap_seconds(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> float:
    """Distance between two time intervals, 0 when they overlap."""
    return max(0.0, (b[0] - a[1]).total_seconds(), (a[0] - b[1]).total_seconds())


def amount_gap(sent: float, fee: float, received: float) -> float:
    """Relative gap between what left and what arrived (fee taken off or not, the closer wins).

    Whether a Binance withdrawal ``amount`` already excludes ``transactionFee`` is unverified
    (module header), so both readings are tried. Receiving more than was sent is a gap too.
    """
    if sent <= 0:
        return float("inf")
    return min(abs(sent - fee - received), abs(sent - received)) / sent


def heuristic_score(out_row, in_row, tolerance: float, window_seconds: float) -> float | None:
    """Tier-3 score of ROADMAP_LEDGER §3.6, or ``None`` when the pair is not a candidate.

    ``s = 1 - 0.25 * min(1, gap / tolerance) - 0.25 * min(1, distance / window)``, where ``gap``
    is :func:`amount_gap` and ``distance`` the time between the two flow intervals.
    """
    gap = amount_gap(float(out_row["amount"]), float(_get(out_row, "fee_amount", 0.0)),
                     float(in_row["amount"]))
    if gap > tolerance + 1e-12:
        return None
    distance = interval_gap_seconds(flow_interval(out_row), flow_interval(in_row))
    if distance > window_seconds:
        return None
    amount_penalty = min(1.0, gap / tolerance) if tolerance > 0 else 0.0
    time_penalty = min(1.0, distance / window_seconds) if window_seconds > 0 else 0.0
    return 1.0 - 0.25 * amount_penalty - 0.25 * time_penalty


def _chain_of(row) -> str:
    source = str(_get(row, "source", ""))
    if source.startswith("evm:"):
        return source[4:]
    if source == SOLANA_CHAIN:
        return SOLANA_CHAIN
    return chain_for_network(_get(row, "network", ""))


def _address_pair(out_row, in_row, owned: dict[str, OwnedWallet]) -> bool:
    """Tier 1: a Binance leg whose counterparty address is the other leg's (owned) account."""
    for exchange, wallet_leg in ((out_row, in_row), (in_row, out_row)):
        if _get(exchange, "source") != SOURCE_BINANCE:
            continue
        address = normalize_address(_get(exchange, "counterparty_address", ""))
        if not address or address != normalize_address(_get(wallet_leg, "account", "")):
            continue
        wallet = owned.get(address)
        chain = chain_for_network(_get(exchange, "network", ""))
        if wallet is not None and chain and chain in wallet.chains and chain == _chain_of(wallet_leg):
            return True
    return False


def match_transfers(flows: Iterable, owned: dict[str, OwnedWallet],
                    settings: CashFlowSettings | None = None) -> list[TransferMatch]:
    """Pair out flows with the in flows that received them (pure: no I/O; ROADMAP_LEDGER §3.6).

    ``flows`` are ``cash_flows`` rows (dicts or ``sqlite3.Row``) of crypto deposits/withdrawals.
    A pair needs the same asset, opposite directions and two different accounts, then:

    1. **address** (1.0): a Binance leg's counterparty address is the other leg's owned wallet,
       on the chain that wallet is tracked on, within ``in_transit_hours`` and the amount tolerance;
    2. **tx_hash** (1.0): both legs carry the same transaction hash;
    3. **heuristic** (score >= 0.6): amounts agree within ``amount_tolerance_pct`` (fee aware) and
       the flows lie within ``match_window_minutes`` of each other.

    Each flow is used at most once: candidates are taken greedily, best confidence first.
    """
    settings = settings or CashFlowSettings()
    tolerance = settings.amount_tolerance_pct / 100.0
    window = settings.match_window_minutes * 60.0
    transit = max(settings.in_transit_hours * 3600.0, window)
    rows = [row for row in flows
            if _get(row, "flow_type") in EXTERNAL_FLOW_TYPES and _get(row, "status", COMPLETED) != FAILED]
    outs = [row for row in rows if row["direction"] == OUT]
    ins = [row for row in rows if row["direction"] == IN]
    tier_rank = {MATCH_TX_HASH: 0, MATCH_ADDRESS: 1, MATCH_HEURISTIC: 2}

    candidates: list[tuple[float, int, int, int, str]] = []
    for out_row in outs:
        for in_row in ins:
            if str(out_row["asset"]).upper() != str(in_row["asset"]).upper():
                continue
            if (_get(out_row, "source"), normalize_address(_get(out_row, "account", ""))) == \
                    (_get(in_row, "source"), normalize_address(_get(in_row, "account", ""))):
                continue
            out_hash = str(_get(out_row, "tx_hash", "")).strip().lower()
            if out_hash and out_hash == str(_get(in_row, "tx_hash", "")).strip().lower():
                candidates.append((1.0, tier_rank[MATCH_TX_HASH], out_row["id"], in_row["id"], MATCH_TX_HASH))
                continue
            if _address_pair(out_row, in_row, owned):
                if heuristic_score(out_row, in_row, tolerance, transit) is not None:
                    candidates.append((1.0, tier_rank[MATCH_ADDRESS], out_row["id"], in_row["id"], MATCH_ADDRESS))
                continue
            score = heuristic_score(out_row, in_row, tolerance, window)
            if score is not None and score >= HEURISTIC_MIN_SCORE:
                candidates.append((min(score, 0.99), tier_rank[MATCH_HEURISTIC], out_row["id"], in_row["id"],
                                   MATCH_HEURISTIC))

    candidates.sort(key=lambda c: (-c[0], c[1], c[2], c[3]))
    used: set[int] = set()
    matches = []
    for confidence, _, out_id, in_id, method in candidates:
        if out_id in used or in_id in used:
            continue
        used.update((out_id, in_id))
        matches.append(TransferMatch(out_id, in_id, method, round(confidence, 4)))
    return matches


def apply_transfer_matches(database: Database, owned: dict[str, OwnedWallet],
                           settings: CashFlowSettings | None = None) -> int:
    """Run :func:`match_transfers` over every automatic, unpaired crypto flow; returns pairs made.

    Manual assets have their own matcher (``services/manual.py``); fiat, P2P and Earn rows never
    move between owned accounts. A user decision is never touched.
    """
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM cash_flows WHERE classified_by = 'auto' AND matched_flow_id IS NULL "
            "AND status = 'completed' AND flow_type IN ('deposit', 'withdrawal') "
            "AND channel IN ('crypto', 'onchain') AND source <> 'manual'"
        ).fetchall()
        if not rows:
            return 0
        matches = match_transfers(rows, owned, settings)
        by_id = {row["id"]: row for row in rows}
        now = utc_now_iso()
        for match in matches:
            out_row, in_row = by_id[match.out_id], by_id[match.in_id]
            for row, other in ((out_row, in_row), (in_row, out_row)):
                label = other["account_label"] or other["account"]
                connection.execute(
                    "UPDATE cash_flows SET is_internal_transfer = 1, matched_flow_id = ?, match_method = ?, "
                    "match_confidence = ?, counterparty_label = ?, updated_at = ? WHERE id = ?",
                    (other["id"], match.method, match.confidence, label, now, row["id"]),
                )
    return len(matches)


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
    refreshed only while ``classified_by = 'auto'`` and the row is not paired to another flow
    (``matched_flow_id``, set by the manual-asset matcher) - a user override always survives.
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
            if existing["classified_by"] == CLASSIFIED_AUTO and existing["matched_flow_id"] is None:
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
            "SELECT * FROM cash_flows WHERE classified_by = 'auto' AND source = ? AND detection_method = ? "
            "AND matched_flow_id IS NULL",  # a paired row keeps its pairing (services/manual.py)
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


def get_floor(database: Database, stream: str) -> int | None:
    """Epoch ms from which a stream's history is known to be complete (``None``: unknown)."""
    return get_cursor(database, floor_key(stream))


def set_floor(database: Database, stream: str, value_ms: int) -> None:
    """Move a stream's floor back in time (never forward: known history is never forgotten)."""
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = CASE WHEN CAST(excluded.value AS INTEGER) < CAST(sync_state.value AS INTEGER)
                                THEN excluded.value ELSE sync_state.value END,
                   updated_at = excluded.updated_at""",
            (floor_key(stream), str(max(int(value_ms), 0)), utc_now_iso()),
        )


def prune_earn_rewards(database: Database, cutoff_ms: int) -> int:
    """Enforce the Earn retention: delete Binance Earn ``reward`` rows older than ``cutoff_ms``.

    Only ``source = 'binance'``, ``channel = 'earn'``, ``flow_type = 'reward'`` rows are touched;
    deposits, withdrawals, fiat and P2P rows never are. The backfill marker of every Earn stream
    that reaches further back is moved *forward* to the cut-off (unlike :func:`set_floor`), so
    the next sync does not re-fetch the range that was just dropped. Returns the rows deleted.
    """
    cutoff_ms = max(int(cutoff_ms), 0)
    cutoff_iso = to_iso_utc(cutoff_ms)
    now = utc_now_iso()
    with database.connect() as connection:
        deleted = connection.execute(
            "DELETE FROM cash_flows WHERE source = ? AND channel = ? AND flow_type = ? AND timestamp < ?",
            (SOURCE_BINANCE, CHANNEL_EARN, REWARD, cutoff_iso),
        ).rowcount
        for stream in EARN_STREAMS:
            connection.execute(
                "UPDATE sync_state SET value = ?, updated_at = ? "
                "WHERE key = ? AND CAST(value AS INTEGER) < ?",
                (str(cutoff_ms), now, floor_key(stream), cutoff_ms),
            )
    return max(int(deleted or 0), 0)


def sync_states(database: Database) -> dict[str, str]:
    """Every forward cursor as ``{key: ISO UTC time it is synced up to}``."""
    with database.connect() as connection:
        rows = connection.execute("SELECT key, value FROM sync_state ORDER BY key").fetchall()
    return {row["key"]: to_iso_utc(_int(row["value"], 0)) for row in rows
            if not row["key"].startswith(BACKFILL_PREFIX)}


def history_floors(database: Database) -> dict[str, str]:
    """How far back each stream is synced: ``{'binance.deposit': ISO UTC}``."""
    with database.connect() as connection:
        rows = connection.execute("SELECT key, value FROM sync_state WHERE key LIKE ? ORDER BY key",
                                  (BACKFILL_PREFIX + "%",)).fetchall()
    return {row["key"][len(BACKFILL_PREFIX):]: to_iso_utc(_int(row["value"], 0)) or "1970-01-01T00:00:00+00:00"
            for row in rows}


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


def floor_key(stream: str) -> str:
    return f"{BACKFILL_PREFIX}{stream_key(stream)}"


def stream_backfill_days(settings, stream: str) -> int:
    """How far back ``stream`` is synced (and, for Earn, kept): Earn reward streams use
    ``earn_backfill_days`` capped at ``backfill_days``; every other stream ``backfill_days``."""
    if stream in EARN_STREAMS:
        return min(settings.earn_backfill_days, settings.backfill_days)
    return settings.backfill_days


def enabled_streams(settings) -> list[str]:
    streams = list(CRYPTO_STREAMS)
    if settings.sync_fiat:
        streams += FIAT_STREAMS
    if settings.sync_p2p:
        streams += P2P_STREAMS
    if getattr(settings, "sync_earn", True):
        streams += EARN_STREAMS
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

    Each stream starts at its cursor minus ``overlap_days`` (or its backfill target on the first
    run: ``backfill_days`` ago, ``earn_backfill_days`` for Earn rewards) and ends now; it never
    starts before that target. Earn rewards older than their target are pruned first. The cursor is committed after each time chunk. A failing stream
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

    def target_for(stream: str) -> int:
        return max(now_ms - stream_backfill_days(settings, stream) * DAY_MS, 0)

    # Earn retention runs even with sync_earn off: the rows already stored still age out.
    report.pruned = prune_earn_rewards(database, target_for(EARN_STREAMS[0]))
    for stream in enabled_streams(settings):
        key = stream_key(stream)
        target_ms = target_for(stream)
        cursor = get_cursor(database, key)
        first_run = cursor is None
        start_ms = target_ms if first_run else \
            max(min(cursor, now_ms) - settings.overlap_days * DAY_MS, target_ms)
        report.fetched[stream] = report.inserted[stream] = report.updated[stream] = 0

        def store(rows: list[dict]) -> None:
            flows = [classify(prices.apply(flow), owned) for flow in parse_rows(stream, rows)]
            inserted, updated = upsert_flows(database, flows) if flows else (0, 0)
            report.fetched[stream] += len(rows)
            report.inserted[stream] += inserted
            report.updated[stream] += updated

        try:
            # forward: from the cursor (minus the overlap) up to now, oldest chunk first
            for _, chunk_end, rows in client.history_windows(stream, start_ms, now_ms):
                store(rows)
                set_cursor(database, key, chunk_end)
                if first_run:  # known from `start_ms` on, contiguously up to the cursor
                    set_floor(database, stream, start_ms)
                    first_run = False
            # backward: the older range not synced yet (5-year history, or a raised backfill_days)
            floor = get_floor(database, stream)
            gap_end = (floor if floor is not None else start_ms) - 1
            if gap_end >= target_ms:
                for chunk_start, _, rows in client.history_windows(stream, target_ms, gap_end,
                                                                    newest_first=True):
                    store(rows)
                    set_floor(database, stream, chunk_start)
        except (requests.RequestException, BinanceError, KeyError, TypeError, ValueError) as exc:
            report.errors.append(f"binance {stream.replace('_', ' ')} history: {exc}")

    report.reclassified = reclassify(database, owned)
    report.repriced = reprice_missing(database, prices)
    report.matched = apply_transfer_matches(database, owned, settings)
    # A withdrawal to the cold wallet only becomes known now: pair it with the manual change.
    from services.manual import match_manual_transfers
    report.paired = match_manual_transfers(database, settings)
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
