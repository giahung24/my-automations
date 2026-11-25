"""Config loading: ``config.yaml`` merged over built-in defaults, then env overrides.

Secrets never have to live in the YAML file - ``BINANCE_API_KEY`` / ``BINANCE_API_SECRET``
in the environment (or a ``.env`` next to this file) always win over it.
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

try:  # python-dotenv is in requirements.txt, but the tool still runs without it
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised only on a partial install
    def load_dotenv(*_a, **_k):
        return False

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_PATH = ROOT / "config.example.yaml"

# Public RPCs, first one that answers wins; each list was probed live, healthiest first.
# Public endpoints come and go (Ankr now wants a key, llamarpc stopped resolving), which is
# why every chain has spares - and why `chains.<name>.rpcs` in config.yaml can replace them
# outright with your own Alchemy/Infura URL. `native_price_id` and `llama_chain` are
# DefiLlama keys (https://coins.llama.fi) - the free pricing API, no key needed.
DEFAULT_CHAINS = {
    "ethereum": {
        "rpcs": ["https://ethereum-rpc.publicnode.com", "https://rpc.mevblocker.io",
                 "https://rpc.flashbots.net", "https://cloudflare-eth.com"],
        "native_symbol": "ETH",
        "native_price_id": "coingecko:ethereum",
        "llama_chain": "ethereum",
        "explorer": "https://etherscan.io",
        "tokens": [
            {"symbol": "LINK", "address": "0x514910771af9ca656af840dff83e8264ecf986ca", "decimals": 18},
            {"symbol": "USDC", "address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "decimals": 6},
            {"symbol": "USDT", "address": "0xdac17f958d2ee523a2206206994597c13d831ec7", "decimals": 6},
            {"symbol": "WBTC", "address": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599", "decimals": 8},
            {"symbol": "DAI", "address": "0x6b175474e89094c44da98b954eedeac495271d0f", "decimals": 18},
            {"symbol": "UNI", "address": "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984", "decimals": 18},
            {"symbol": "AAVE", "address": "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9", "decimals": 18},
        ],
    },
    "bsc": {
        "rpcs": ["https://bsc-dataseed.binance.org", "https://bsc-rpc.publicnode.com",
                 "https://bsc-dataseed1.defibit.io"],
        "native_symbol": "BNB",
        "native_price_id": "coingecko:binancecoin",
        "llama_chain": "bsc",
        "explorer": "https://bscscan.com",
        "tokens": [
            {"symbol": "USDT", "address": "0x55d398326f99059ff775485246999027b3197955", "decimals": 18},
            {"symbol": "USDC", "address": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", "decimals": 18},
            {"symbol": "BTCB", "address": "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c", "decimals": 18},
            {"symbol": "ETH", "address": "0x2170ed0880ac9a755fd29b2688956bd959f933f8", "decimals": 18},
            {"symbol": "LINK", "address": "0xf8a0bf9cf54bb92f17374d9e9a321e6a111a51bd", "decimals": 18},
        ],
    },
    "arbitrum": {
        "rpcs": ["https://arb1.arbitrum.io/rpc", "https://arbitrum-one-rpc.publicnode.com",
                 "https://arbitrum.drpc.org"],
        "native_symbol": "ETH",
        "native_price_id": "coingecko:ethereum",
        "llama_chain": "arbitrum",
        "explorer": "https://arbiscan.io",
        "tokens": [
            {"symbol": "USDC", "address": "0xaf88d065e77c8cc2239327c5edb3a432268e5831", "decimals": 6},
            {"symbol": "USDT", "address": "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", "decimals": 6},
            {"symbol": "ARB", "address": "0x912ce59144191c1204e64559fe8253a0e49e6548", "decimals": 18},
            {"symbol": "LINK", "address": "0xf97f4df75117a78c1a5a0dbb814af92458539fb4", "decimals": 18},
        ],
    },
    "base": {
        "rpcs": ["https://mainnet.base.org", "https://base-rpc.publicnode.com",
                 "https://base.drpc.org"],
        "native_symbol": "ETH",
        "native_price_id": "coingecko:ethereum",
        "llama_chain": "base",
        "explorer": "https://basescan.org",
        "tokens": [
            {"symbol": "USDC", "address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "decimals": 6},
            {"symbol": "AERO", "address": "0x940181a94a35a4569e4529a3cdfb74e38fd98631", "decimals": 18},
            {"symbol": "LINK", "address": "0x88fb150bdc53a65fe94dea0c9ba0a6daf8c6e196", "decimals": 18},
        ],
    },
    "polygon": {
        # MATIC was renamed POL in Sept 2024; the CoinGecko id is still `matic-network`.
        "rpcs": ["https://1rpc.io/matic", "https://polygon-rpc.com",
                 "https://polygon-bor-rpc.publicnode.com"],
        "native_symbol": "POL",
        "native_price_id": "coingecko:matic-network",
        "llama_chain": "polygon",
        "explorer": "https://polygonscan.com",
        "tokens": [
            {"symbol": "USDC", "address": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359", "decimals": 6},
            {"symbol": "USDT", "address": "0xc2132d05d31c914a87c6611c10748aeb04b58e8f", "decimals": 6},
            {"symbol": "LINK", "address": "0x53e0bca35ec356bd5dddfebbd1fc0fd03fabad39", "decimals": 18},
            {"symbol": "WETH", "address": "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619", "decimals": 18},
        ],
    },
    "solana": {
        "rpcs": ["https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com"],
        "native_symbol": "SOL",
        "native_price_id": "coingecko:solana",
        "llama_chain": "solana",
        "explorer": "https://solscan.io",
    },
}

DEFAULTS = {
    "binance": {"enabled": True, "api_key": "", "api_secret": "", "recv_window": 5000},
    "wallets": {"evm": [], "solana": []},
    "chains": deepcopy(DEFAULT_CHAINS),
    "settings": {
        "min_value": 1.0,          # dust filter, USD
        "db_path": "portfolio_history.db",
        "charts_dir": "./charts",
        "timeout": 20,             # seconds per HTTP request
        "max_workers": 8,          # parallel chain / wallet fetches
        "stablecoins": ["USDT", "USDC", "FDUSD", "DAI", "TUSD", "USDP", "PYUSD", "USDE"],
    },
    # Epic F2 (ROADMAP_LEDGER §3.8): deposit / withdrawal / fiat / P2P history from Binance.
    "cash_flows": {
        "enabled": True,
        "backfill_days": 1827,     # history kept in sync: 5 years (the maximum); raising it later back-fills the gap
        "earn_backfill_days": 365,  # Earn rewards only: synced AND kept this far back (older rows are pruned)
        "overlap_days": 2,         # later syncs re-read this much, to catch pending -> completed
        "sync_fiat": True,         # /sapi/v1/fiat/orders + /sapi/v1/fiat/payments
        "sync_p2p": True,          # /sapi/v1/c2c/orderMatch/listUserOrderHistory
        "sync_earn": True,         # Simple Earn flexible + locked rewards (income, M4)
        "in_transit_hours": 2,     # a Binance withdrawal this old can still pair with a manual change
        "amount_tolerance_pct": 0.5,  # |withdrawn - fee - received| allowed when pairing, % of withdrawn
        "match_window_minutes": 30,   # heuristic transfer matching: time slack around a flow (§3.6)
    },
    # Epic M6 (ROADMAP_LEDGER §9.3): balances with no API - BTC on a hardware wallet, staking
    # elsewhere. Validated by `parse_manual_assets`; a bad entry is skipped, never fatal.
    "manual_assets": [],
    # Epic M7 (ROADMAP_LEDGER §4.5, §9.4): target weights + rebalancing suggestions. Off until
    # the user turns it on (Settings or the Allocation card); validated by `parse_allocation`.
    "allocation": {"enabled": False},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursive dict merge; lists and scalars from `override` replace wholesale."""
    out = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _split(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


# Binance history is synced at most 5 years back (M4.activity_tab): 5 x 365 + 2 leap days.
MAX_BACKFILL_DAYS = 1827
# Simple Earn rewards land several times a day per asset: a year is plenty to look back on, and
# five years of them would bury the deposits / withdrawals in the Activity tab.
DEFAULT_EARN_BACKFILL_DAYS = 365


@dataclass(frozen=True)
class CashFlowSettings:
    """The validated `cash_flows:` block. Anything missing or malformed falls back to the default."""

    enabled: bool = True
    backfill_days: int = MAX_BACKFILL_DAYS
    earn_backfill_days: int = DEFAULT_EARN_BACKFILL_DAYS  # sync window AND retention of Earn rewards
    overlap_days: int = 2
    sync_fiat: bool = True
    sync_p2p: bool = True
    sync_earn: bool = True
    in_transit_hours: int = 2
    amount_tolerance_pct: float = 0.5
    match_window_minutes: int = 30

    @classmethod
    def from_raw(cls, raw) -> "CashFlowSettings":
        raw = raw if isinstance(raw, dict) else {}
        default = cls()

        def flag(name: str) -> bool:
            value = raw.get(name, getattr(default, name))
            if isinstance(value, str):
                return _as_bool(value)
            return bool(value)

        def days(name: str, low: int, high: int) -> int:
            try:
                value = int(raw.get(name, getattr(default, name)))
            except (TypeError, ValueError):
                return getattr(default, name)
            return min(max(value, low), high)

        def percent(name: str, low: float, high: float) -> float:
            try:
                value = float(raw.get(name, getattr(default, name)))
            except (TypeError, ValueError):
                return getattr(default, name)
            if value != value:  # NaN
                return getattr(default, name)
            return min(max(value, low), high)

        return cls(
            enabled=flag("enabled"),
            backfill_days=days("backfill_days", 1, MAX_BACKFILL_DAYS),
            earn_backfill_days=days("earn_backfill_days", 1, MAX_BACKFILL_DAYS),
            overlap_days=days("overlap_days", 0, 30),
            sync_fiat=flag("sync_fiat"),
            sync_p2p=flag("sync_p2p"),
            sync_earn=flag("sync_earn"),
            in_transit_hours=days("in_transit_hours", 0, 720),
            amount_tolerance_pct=percent("amount_tolerance_pct", 0.0, 10.0),
            match_window_minutes=days("match_window_minutes", 1, 1440),
        )


# -- manual assets (Epic M6) -----------------------------------------------------------
MANUAL_KINDS = ("cold_wallet", "staking", "other")
MANUAL_LABEL_MAX = 40
MANUAL_HOLDINGS_MAX = 50


@dataclass(frozen=True)
class ManualHolding:
    """One balance of a manual asset. Priced like any holding: Binance ticker, then DefiLlama
    (`price_id`), and `price_usd` only as the last resort for coins with no market price."""

    symbol: str
    quantity: float
    price_id: str = ""
    price_usd: float | None = None
    liquid: bool = True


@dataclass(frozen=True)
class ManualAsset:
    """A place holding coins no API can read: a hardware wallet, a validator, another exchange.

    `label` is unique and is the key everywhere (`snapshot_assets.detail`, `manual:<label>` in
    `snapshot_sources`, `cash_flows.account`). `updated_at` is the day the quantities were last
    edited (set by the settings screen), '' when unknown.

    `linked_wallet` is the label of a configured on-chain wallet (`wallets.evm` / `wallets.solana`)
    that is the same physical device - the BTC on the Ledger whose EVM address is also tracked.
    It only changes how the dashboard *groups* the asset; data and analytics still key on `label`
    and `source = 'manual'`. '' when the asset stands alone.
    """

    label: str
    kind: str
    holdings: tuple[ManualHolding, ...]
    updated_at: str = ""
    linked_wallet: str = ""

    def to_raw(self) -> dict:
        """The config.yaml form of this asset (what `parse_manual_assets` reads back)."""
        entry: dict = {"label": self.label, "kind": self.kind}
        if self.linked_wallet:
            entry["linked_wallet"] = self.linked_wallet
        if self.updated_at:
            entry["updated_at"] = self.updated_at
        entry["holdings"] = []
        for holding in self.holdings:
            item: dict = {"symbol": holding.symbol, "quantity": holding.quantity}
            if holding.price_id:
                item["price_id"] = holding.price_id
            if holding.price_usd is not None:
                item["price_usd"] = holding.price_usd
            if not holding.liquid:
                item["liquid"] = False
            entry["holdings"].append(item)
        return entry


def _number(value) -> float | None:
    """A finite float, or None. Booleans are not numbers here (`quantity: yes` is a typo)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def resolve_linked_wallet(value, wallet_labels: Iterable[str]) -> str | None:
    """The configured wallet label `value` names (case-insensitive), or None when none matches."""
    wanted = str(value).strip().casefold()
    for label in wallet_labels:
        if label.casefold() == wanted:
            return label
    return None


def parse_manual_assets(raw, wallet_labels: Iterable[str] | None = None) -> tuple[list[ManualAsset], list[str]]:
    """Validate the `manual_assets:` block. Returns ``(assets, errors)``, never raises.

    Each problem yields one readable message and only the faulty part is dropped: a holding
    with a negative quantity is skipped but its siblings are kept; an asset with a missing or
    duplicate label is skipped whole (the label is its key). A missing block is ``([], [])``.

    `wallet_labels` are the labels of the configured wallets. A `linked_wallet` must name one of
    them (case-insensitive, stored with the wallet's own spelling); one that does not is reported
    and the asset is kept *unlinked* - dropping it would book its whole balance as a withdrawal.
    ``None`` skips that check (the caller validates later).
    """
    wallet_labels = None if wallet_labels is None else list(wallet_labels)
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], [f"manual_assets: expected a list of assets, got {type(raw).__name__}"]

    assets: list[ManualAsset] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw, start=1):
        where = f"manual_assets #{index}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: expected a mapping with label, kind and holdings")
            continue
        label = str(entry.get("label") or "").strip()
        if not label:
            errors.append(f"{where}: label is empty")
            continue
        if len(label) > MANUAL_LABEL_MAX:
            errors.append(f"{where}: label '{label[:20]}…' is longer than {MANUAL_LABEL_MAX} characters")
            continue
        where = f"manual_assets '{label}'"
        if label.casefold() in seen:
            errors.append(f"{where}: duplicate label (labels must be unique)")
            continue
        seen.add(label.casefold())

        kind = str(entry.get("kind") or "other").strip().lower()
        if kind not in MANUAL_KINDS:
            errors.append(f"{where}: unknown kind '{kind}' (expected {' | '.join(MANUAL_KINDS)}); using 'other'")
            kind = "other"

        raw_holdings = entry.get("holdings")
        if raw_holdings is None:
            raw_holdings = []
        if not isinstance(raw_holdings, list):
            errors.append(f"{where}: holdings must be a list")
            raw_holdings = []
        holdings: list[ManualHolding] = []
        symbols: set[str] = set()
        for position, item in enumerate(raw_holdings[:MANUAL_HOLDINGS_MAX], start=1):
            spot = f"{where} holding #{position}"
            if not isinstance(item, dict):
                errors.append(f"{spot}: expected {{symbol, quantity}}")
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol:
                errors.append(f"{spot}: symbol is empty")
                continue
            spot = f"{where} {symbol}"
            if symbol in symbols:
                errors.append(f"{spot}: listed twice in this asset")
                continue
            quantity = _number(item.get("quantity"))
            if quantity is None:
                errors.append(f"{spot}: quantity must be a number")
                continue
            if quantity < 0:
                errors.append(f"{spot}: quantity {quantity:g} is negative")
                continue
            price_usd = None
            if item.get("price_usd") is not None:
                price_usd = _number(item.get("price_usd"))
                if price_usd is None:
                    errors.append(f"{spot}: price_usd must be a number")
                    continue
                if price_usd < 0:
                    errors.append(f"{spot}: price_usd {price_usd:g} is negative")
                    continue
            liquid = item.get("liquid", True)
            liquid = _as_bool(liquid) if isinstance(liquid, str) else bool(liquid)
            symbols.add(symbol)
            holdings.append(ManualHolding(symbol=symbol, quantity=quantity,
                                          price_id=str(item.get("price_id") or "").strip(),
                                          price_usd=price_usd, liquid=liquid))
        if len(raw_holdings) > MANUAL_HOLDINGS_MAX:
            errors.append(f"{where}: only the first {MANUAL_HOLDINGS_MAX} holdings are read")
        updated_at = entry.get("updated_at") or ""
        linked_wallet = ""
        raw_link = entry.get("linked_wallet")
        if raw_link is not None and not isinstance(raw_link, str):
            errors.append(f"{where}: linked_wallet must be a wallet label (text); ignored")
        elif raw_link and raw_link.strip():
            if wallet_labels is None:
                linked_wallet = raw_link.strip()
            else:
                linked_wallet = resolve_linked_wallet(raw_link, wallet_labels) or ""
                if not linked_wallet:
                    known = ", ".join(f"'{name}'" for name in wallet_labels) or "none configured"
                    errors.append(f"{where}: linked_wallet '{raw_link.strip()}' is not a configured wallet "
                                  f"(wallets.evm / wallets.solana labels: {known}); shown unlinked")
        assets.append(ManualAsset(label=label, kind=kind, holdings=tuple(holdings),
                                  updated_at=str(updated_at).strip(), linked_wallet=linked_wallet))
    return assets, errors


# -- asset allocation (Epic M7) ---------------------------------------------------------
REBALANCE_MODES = ("target", "band_edge")
ALLOCATION_CATCH_ALL = "*"
ALLOCATION_STABLECOINS = "@stablecoins"
# Sources a dry-powder rule may name (models.SOURCES, repeated here to keep config import-light).
DRY_POWDER_SOURCES = ("binance_spot", "binance_earn", "binance_funding", "onchain", "manual")
# The targets used when `allocation.buckets` is absent (ROADMAP_LEDGER §9.4).
DEFAULT_ALLOCATION_BUCKETS = {
    "BTC": {"target": 50, "symbols": ["BTC", "WBTC", "BTCB"]},
    "ETH": {"target": 25, "symbols": ["ETH", "WETH", "STETH", "WSTETH"]},
    "Stablecoin": {"target": 15, "symbols": ALLOCATION_STABLECOINS},
    "Altcoins": {"target": 10, "symbols": ALLOCATION_CATCH_ALL},
}


@dataclass(frozen=True)
class AllocationBucket:
    """One target bucket. `catch_all` takes every symbol no other bucket names;
    `stablecoins` marks the bucket built from `settings.stablecoins` (it absorbs rounding)."""

    name: str
    target: float
    symbols: tuple[str, ...] = ()
    catch_all: bool = False
    stablecoins: bool = False


@dataclass(frozen=True)
class DryPowderRule:
    """Holdings set aside as "dry powder" (cash waiting to buy a dip): kept out of the weights,
    the drift alerts and the suggested sells. Empty `sources` means every source; `max_usd`
    caps how much of the matching value is set aside (None = all of it)."""

    symbols: tuple[str, ...]
    sources: tuple[str, ...] = ()
    max_usd: float | None = None


@dataclass(frozen=True)
class AllocationSettings:
    """The validated `allocation:` block (ROADMAP_LEDGER §4.5.1)."""

    enabled: bool = False
    tolerance_pp: float = 5.0
    rebalance_to: str = "target"
    min_trade_usd: float = 20.0
    fee_rate: float = 0.001
    buckets: tuple[AllocationBucket, ...] = ()
    dry_powder: tuple[DryPowderRule, ...] = ()
    default_targets: bool = False  # True when `buckets` was not configured

    def bucket_of(self, symbol: str) -> str | None:
        """Name of the bucket holding `symbol`, the catch-all one otherwise, or None."""
        symbol = symbol.upper()
        catch_all = None
        for bucket in self.buckets:
            if symbol in bucket.symbols:
                return bucket.name
            if bucket.catch_all:
                catch_all = bucket.name
        return catch_all


def _symbol_list(value) -> list[str] | None:
    """`BTC` or `[BTC, wbtc]` -> upper-case symbols; None when it is neither."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return None
    out = []
    for item in value:
        if isinstance(item, bool) or item is None:
            return None
        text = str(item).strip().upper()
        if text and text not in out:
            out.append(text)
    return out


def parse_allocation(raw, stablecoins=()) -> tuple[AllocationSettings, list[str]]:
    """Validate the `allocation:` block. Returns ``(settings, errors)`` and never raises.

    Errors (one readable message each) mean the targets cannot be trusted: the endpoint
    answers 422 with them while the rest of the dashboard keeps working. A missing block is
    valid: allocation is off, and turning it on uses `DEFAULT_ALLOCATION_BUCKETS`.
    """
    errors: list[str] = []
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return AllocationSettings(), [f"allocation: expected a mapping, got {type(raw).__name__}"]
    default = AllocationSettings()

    enabled = raw.get("enabled", default.enabled)
    enabled = _as_bool(enabled) if isinstance(enabled, str) else bool(enabled)

    tolerance = _number(raw.get("tolerance_pp", default.tolerance_pp))
    if tolerance is None or not 0 < tolerance < 50:
        errors.append(f"allocation.tolerance_pp: must be a number strictly between 0 and 50, "
                      f"got {raw.get('tolerance_pp')!r}")
        tolerance = default.tolerance_pp

    rebalance_to = str(raw.get("rebalance_to", default.rebalance_to) or "").strip().lower()
    if rebalance_to not in REBALANCE_MODES:
        errors.append(f"allocation.rebalance_to: '{rebalance_to}' is not one of "
                      f"{' | '.join(REBALANCE_MODES)}")
        rebalance_to = default.rebalance_to

    min_trade = _number(raw.get("min_trade_usd", default.min_trade_usd))
    if min_trade is None or min_trade < 0:
        errors.append(f"allocation.min_trade_usd: must be a number >= 0, got {raw.get('min_trade_usd')!r}")
        min_trade = default.min_trade_usd

    fee_rate = _number(raw.get("fee_rate", default.fee_rate))
    if fee_rate is None or not 0 <= fee_rate < 0.1:
        errors.append(f"allocation.fee_rate: must be a number in [0, 0.1), got {raw.get('fee_rate')!r}")
        fee_rate = default.fee_rate

    raw_buckets = raw.get("buckets")
    default_targets = raw_buckets is None
    if default_targets:
        raw_buckets = DEFAULT_ALLOCATION_BUCKETS
    buckets = _parse_buckets(raw_buckets, stablecoins, errors)
    dry_powder = _parse_dry_powder(raw.get("dry_powder"), errors)

    return AllocationSettings(
        enabled=enabled, tolerance_pp=tolerance, rebalance_to=rebalance_to,
        min_trade_usd=min_trade, fee_rate=fee_rate, buckets=tuple(buckets),
        dry_powder=tuple(dry_powder), default_targets=default_targets,
    ), errors


def _parse_buckets(raw, stablecoins, errors: list[str]) -> list[AllocationBucket]:
    if not isinstance(raw, dict) or not raw:
        errors.append("allocation.buckets: expected a mapping of bucket name -> {target, symbols}")
        return []
    stable = sorted({str(s).strip().upper() for s in stablecoins if str(s).strip()})
    buckets: list[AllocationBucket] = []
    owner: dict[str, str] = {}
    catch_all: list[str] = []
    total = 0.0
    for name, spec in raw.items():
        name = str(name).strip()
        where = f"allocation.buckets.{name}"
        if not name:
            errors.append("allocation.buckets: a bucket has an empty name")
            continue
        if not isinstance(spec, dict):
            errors.append(f"{where}: expected {{target, symbols}}")
            continue
        target = _number(spec.get("target"))
        if target is None or target < 0 or target > 100:
            errors.append(f"{where}.target: must be a number between 0 and 100, got {spec.get('target')!r}")
            continue
        total += target
        raw_symbols = spec.get("symbols")
        is_catch_all = isinstance(raw_symbols, str) and raw_symbols.strip() == ALLOCATION_CATCH_ALL
        is_stable = isinstance(raw_symbols, str) and raw_symbols.strip().lower() == ALLOCATION_STABLECOINS
        if is_catch_all:
            symbols: list[str] = []
            catch_all.append(name)
        elif is_stable:
            symbols = list(stable)
        else:
            symbols = _symbol_list(raw_symbols)
            if not symbols:
                errors.append(f"{where}.symbols: expected a list of symbols, "
                              f"'{ALLOCATION_STABLECOINS}' or '{ALLOCATION_CATCH_ALL}'")
                continue
        for symbol in symbols:
            if symbol in owner:
                errors.append(f"{where}: {symbol} is already in bucket '{owner[symbol]}' "
                              f"(a symbol belongs to exactly one bucket)")
            else:
                owner[symbol] = name
        buckets.append(AllocationBucket(name=name, target=target, symbols=tuple(symbols),
                                        catch_all=is_catch_all, stablecoins=is_stable))
    if len(catch_all) > 1:
        errors.append(f"allocation.buckets: only one bucket may use '{ALLOCATION_CATCH_ALL}', "
                      f"found {', '.join(catch_all)}")
    if buckets and abs(total - 100) > 0.01:
        errors.append(f"allocation.buckets: targets add up to {total:g}, they must add up to 100")
    return buckets


def _parse_dry_powder(raw, errors: list[str]) -> list[DryPowderRule]:
    if raw is None:
        return []
    if isinstance(raw, dict):  # a single rule written without the list dash
        raw = [raw]
    if not isinstance(raw, list):
        errors.append("allocation.dry_powder: expected a list of {symbols, sources, max_usd}")
        return []
    rules = []
    for index, item in enumerate(raw, start=1):
        where = f"allocation.dry_powder #{index}"
        if not isinstance(item, dict):
            errors.append(f"{where}: expected {{symbols, sources, max_usd}}")
            continue
        symbols = _symbol_list(item.get("symbols", item.get("symbol")))
        if not symbols:
            errors.append(f"{where}: needs at least one symbol")
            continue
        raw_sources = item.get("sources", item.get("source")) or []
        if isinstance(raw_sources, str):
            raw_sources = [raw_sources]
        sources = [str(s).strip().lower() for s in raw_sources if str(s).strip()] \
            if isinstance(raw_sources, list) else None
        if sources is None:
            errors.append(f"{where}.sources: expected a list")
            continue
        unknown = [s for s in sources if s not in DRY_POWDER_SOURCES]
        if unknown:
            errors.append(f"{where}.sources: unknown {', '.join(unknown)} "
                          f"(expected {' | '.join(DRY_POWDER_SOURCES)})")
            continue
        max_usd = None
        if item.get("max_usd") is not None:
            max_usd = _number(item.get("max_usd"))
            if max_usd is None or max_usd < 0:
                errors.append(f"{where}.max_usd: must be a number >= 0")
                continue
        rules.append(DryPowderRule(symbols=tuple(symbols), sources=tuple(sources), max_usd=max_usd))
    return rules


@dataclass
class Config:
    binance: dict = field(default_factory=dict)
    wallets: dict = field(default_factory=dict)
    chains: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    path: Path | None = None
    cash_flows: dict = field(default_factory=dict)
    manual_assets: list = field(default_factory=list)  # raw `manual_assets:` block
    allocation: dict = field(default_factory=dict)     # raw `allocation:` block

    # -- convenience accessors -------------------------------------------------
    @property
    def binance_enabled(self) -> bool:
        return bool(self.binance.get("enabled")) and bool(self.binance.get("api_key")) and bool(
            self.binance.get("api_secret")
        )

    @property
    def min_value(self) -> float:
        return float(self.settings.get("min_value", 1.0))

    @property
    def timeout(self) -> int:
        return int(self.settings.get("timeout", 20))

    @property
    def max_workers(self) -> int:
        return max(1, int(self.settings.get("max_workers", 8)))

    @property
    def stablecoins(self) -> set[str]:
        return {s.upper() for s in self.settings.get("stablecoins", [])}

    def resolve(self, value: str) -> Path:
        """Resolve a configured path relative to the project root, not the shell cwd."""
        path = Path(value).expanduser()
        return path if path.is_absolute() else (ROOT / path)

    @property
    def db_path(self) -> Path:
        return self.resolve(self.settings.get("db_path", "portfolio_history.db"))

    @property
    def charts_dir(self) -> Path:
        return self.resolve(self.settings.get("charts_dir", "./charts"))

    def chain(self, name: str) -> dict:
        return self.chains.get(name, {})

    @property
    def evm_wallets(self) -> list[dict]:
        return [w for w in self.wallets.get("evm", []) if w.get("address")]

    @property
    def solana_wallets(self) -> list[dict]:
        return [w for w in self.wallets.get("solana", []) if w.get("address")]

    @property
    def has_wallets(self) -> bool:
        return bool(self.evm_wallets or self.solana_wallets)

    @property
    def cash_flow_settings(self) -> CashFlowSettings:
        return CashFlowSettings.from_raw(self.cash_flows)

    @property
    def wallet_labels(self) -> list[str]:
        """Labels of the configured wallets, EVM then Solana, each once (a `linked_wallet` target).

        One label on both lists is one device holding both kinds of keys (a Ledger)."""
        labels: list[str] = []
        for wallet in self.evm_wallets + self.solana_wallets:
            label = str(wallet.get("label") or "")
            if label and label not in labels:
                labels.append(label)
        return labels

    @property
    def manual(self) -> list[ManualAsset]:
        """The valid manual assets (invalid entries are dropped, see `manual_errors`)."""
        return parse_manual_assets(self.manual_assets, self.wallet_labels)[0]

    @property
    def manual_errors(self) -> list[str]:
        return parse_manual_assets(self.manual_assets, self.wallet_labels)[1]

    @property
    def has_manual_assets(self) -> bool:
        return any(asset.holdings for asset in self.manual)

    @property
    def allocation_settings(self) -> AllocationSettings:
        return parse_allocation(self.allocation, self.settings.get("stablecoins", []))[0]

    @property
    def allocation_errors(self) -> list[str]:
        return parse_allocation(self.allocation, self.settings.get("stablecoins", []))[1]

    @property
    def has_sources(self) -> bool:
        """Anything at all to fetch or value: Binance, a wallet or a manual asset."""
        return self.binance_enabled or self.has_wallets or self.has_manual_assets


def _as_address(value) -> str:
    """Normalise one address.

    YAML 1.1 parses an *unquoted* `0xdac17f95...` as an integer - every EVM address is a
    valid hex literal - so recover the canonical 40-hex-digit form instead of silently
    querying a nonsense address. Quoted addresses pass through untouched.
    """
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        return f"0x{value:040x}"
    return str(value).strip()


def _normalize_wallets(raw) -> list[dict]:
    """Accept either `- "0xabc"` or `- {address: 0xabc, label: main, chains: [...]}`."""
    wallets = []
    for entry in raw or []:
        if isinstance(entry, (str, int)):
            entry = {"address": entry}
        if not isinstance(entry, dict):
            continue
        address = _as_address(entry.get("address"))
        if not address:
            continue
        wallets.append(
            {
                "address": address,
                "label": str(entry.get("label") or address[:6] + ".." + address[-4:]),
                "chains": list(entry.get("chains") or []),
                "tokens": entry.get("tokens") or {},
            }
        )
    return wallets


ENV_OVERRIDES = ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_ENABLED", "EVM_WALLETS",
                 "SOLANA_WALLETS", "PORTFOLIO_DB", "PORTFOLIO_CHARTS_DIR", "PORTFOLIO_MIN_VALUE")


def active_env_overrides() -> list[str]:
    """Names of the environment variables currently overriding config.yaml."""
    return [name for name in ENV_OVERRIDES if os.environ.get(name)]


def read_raw_config(path: str | Path | None = None) -> dict:
    """The YAML file exactly as written - no defaults, no env - for read-modify-write."""
    config_path = Path(path).expanduser() if path else CONFIG_PATH
    if not config_path.is_file():
        return {}
    with open(config_path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path} must contain a YAML mapping, got {type(raw).__name__}")
    return raw


def write_raw_config(raw: dict, path: str | Path | None = None) -> Path:
    """Atomically replace config.yaml with `raw`, keeping the previous file as .bak.

    The file holds an API secret, so it is created owner-read/write only (0600). YAML
    comments do not survive a rewrite - the backup keeps the hand-edited original.
    """
    config_path = Path(path).expanduser() if path else CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, default_flow_style=False)
    if config_path.is_file():
        backup = config_path.with_name(config_path.name + ".bak")
        backup.write_bytes(config_path.read_bytes())
        os.chmod(backup, 0o600)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".yaml", dir=config_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, config_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return config_path


def load_config(path: str | Path | None = None, missing_ok: bool = False) -> Config:
    """Load defaults -> config.yaml -> environment. Missing config.yaml is not an error.

    An explicitly requested file that is missing *is* an error, unless `missing_ok` - the
    web settings page points at a file it is about to create on the first save.
    """
    load_dotenv(ROOT / ".env")
    load_dotenv()  # also honour a .env in the shell's cwd

    config_path = Path(path).expanduser() if path else CONFIG_PATH
    raw = {}
    if config_path.is_file():
        with open(config_path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{config_path} must contain a YAML mapping, got {type(raw).__name__}")
    elif path and not missing_ok:
        raise FileNotFoundError(f"config file not found: {config_path}")

    merged = _deep_merge(DEFAULTS, raw)

    # Environment always wins, so secrets can stay out of the YAML entirely.
    env = os.environ
    if env.get("BINANCE_API_KEY"):
        merged["binance"]["api_key"] = env["BINANCE_API_KEY"].strip()
    if env.get("BINANCE_API_SECRET"):
        merged["binance"]["api_secret"] = env["BINANCE_API_SECRET"].strip()
    if env.get("BINANCE_ENABLED"):
        merged["binance"]["enabled"] = _as_bool(env["BINANCE_ENABLED"])
    if env.get("EVM_WALLETS"):
        merged["wallets"]["evm"] = _split(env["EVM_WALLETS"])
    if env.get("SOLANA_WALLETS"):
        merged["wallets"]["solana"] = _split(env["SOLANA_WALLETS"])
    if env.get("PORTFOLIO_DB"):
        merged["settings"]["db_path"] = env["PORTFOLIO_DB"]
    if env.get("PORTFOLIO_CHARTS_DIR"):
        merged["settings"]["charts_dir"] = env["PORTFOLIO_CHARTS_DIR"]
    if env.get("PORTFOLIO_MIN_VALUE"):
        try:
            merged["settings"]["min_value"] = float(env["PORTFOLIO_MIN_VALUE"])
        except ValueError:
            pass  # a typo in an env var should not break the run

    merged["wallets"]["evm"] = _normalize_wallets(merged["wallets"].get("evm"))
    merged["wallets"]["solana"] = _normalize_wallets(merged["wallets"].get("solana"))
    for chain in merged["chains"].values():
        for token in (chain.get("tokens") or []):
            if isinstance(token, dict):
                token["address"] = _as_address(token.get("address"))

    return Config(
        binance=merged["binance"],
        wallets=merged["wallets"],
        chains=merged["chains"],
        settings=merged["settings"],
        path=config_path if config_path.is_file() else None,
        cash_flows=merged["cash_flows"] if isinstance(merged.get("cash_flows"), dict) else {},
        manual_assets=merged.get("manual_assets") if merged.get("manual_assets") is not None else [],
        allocation=merged["allocation"] if isinstance(merged.get("allocation"), dict) else
        ({} if merged.get("allocation") is None else merged["allocation"]),
    )
