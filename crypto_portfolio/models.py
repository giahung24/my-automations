"""Shared data structures for the portfolio tracker.

Every client returns a list of :class:`Holding` plus a list of error strings, so a
dead RPC or an expired API key degrades one source instead of killing the run.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

BINANCE_SPOT = "binance_spot"
BINANCE_EARN = "binance_earn"
BINANCE_FUNDING = "binance_funding"
ONCHAIN = "onchain"
MANUAL = "manual"  # Epic M6: declared in config.yaml (cold wallet, staking), priced by market
SOURCES = (BINANCE_SPOT, BINANCE_EARN, BINANCE_FUNDING, ONCHAIN, MANUAL)
SOURCE_LABELS = {
    BINANCE_SPOT: "Binance Spot",
    BINANCE_EARN: "Binance Earn",
    BINANCE_FUNDING: "Binance Funding",
    ONCHAIN: "On-chain",
    MANUAL: "Cold / Manual",
}
# Where the coins sit, for custody ratios: on an exchange, or under your own keys.
SELF_CUSTODY_SOURCES = (ONCHAIN, MANUAL)
BINANCE_SOURCES = (BINANCE_SPOT, BINANCE_EARN, BINANCE_FUNDING)

# Per-snapshot health of each data source (table `snapshot_sources`). The keys are finer than
# the four `*_usd` totals: on-chain is split per (chain, wallet), because a dead RPC only
# loses part of the on-chain total.
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_DISABLED = "disabled"
STATUS_INFERRED_ERROR = "inferred_error"  # backfilled by heuristic on snapshots older than v2
UNKNOWN_SOURCE = "unknown"  # an error we could not attribute to any source

# Binance error prefixes, as written by `BinanceClient.fetch_all`, -> the source they hit.
_BINANCE_SECTIONS = {
    "binance spot:": (BINANCE_SPOT,),
    "binance simple earn flexible:": (BINANCE_EARN,),
    "binance simple earn locked:": (BINANCE_EARN,),
    "binance funding:": (BINANCE_FUNDING,),
}


def onchain_source(name: str) -> str:
    """`ethereum/main` -> `onchain:ethereum/main`, the `snapshot_sources.source` of a wallet."""
    return f"{ONCHAIN}:{name}"


def is_onchain_source(source: str) -> bool:
    return source == ONCHAIN or source.startswith(f"{ONCHAIN}:")


def manual_source(label: str) -> str:
    """`Ledger Nano X` -> `manual:Ledger Nano X`, the `snapshot_sources.source` of a manual asset."""
    return f"{MANUAL}:{label}"


def is_manual_source(source: str) -> bool:
    return source == MANUAL or source.startswith(f"{MANUAL}:")


@dataclass
class Holding:
    """One asset balance from one source.

    ``price_id`` is the DefiLlama key (``ethereum:0x..``, ``coingecko:solana``) used to
    price on-chain holdings; Binance holdings are priced from the Binance ticker instead.
    """

    symbol: str
    source: str
    quantity: float
    price_usd: float = 0.0
    detail: str = ""  # chain / wallet label (on-chain rows), asset label (manual rows)
    price_id: str = ""
    # Simple Earn only, as fractions. `listed_apr` is what the position earns overall: the base
    # rate plus each tier bonus on the part of the balance inside its band (clients/binance.py
    # `effective_apr`). The breakdown keeps Binance's own figures: `listed_base_apr` is
    # `latestAnnualPercentageRate`, `listed_tiers` the parsed `tierAnnualPercentageRate` bands,
    # [{"min": 0.0, "max": 5000.0, "apr": 0.03}] (empty when there is no bonus).
    listed_apr: float | None = None
    listed_base_apr: float | None = None
    listed_tiers: list[dict] = field(default_factory=list)

    @property
    def value_usd(self) -> float:
        return self.quantity * self.price_usd


@dataclass
class AssetRow:
    """One asset aggregated across every source, as rendered in the table."""

    symbol: str
    by_source: dict[str, float] = field(default_factory=dict)  # source -> quantity
    quantity: float = 0.0
    price_usd: float = 0.0
    value_usd: float = 0.0
    pct: float = 0.0
    details: list[str] = field(default_factory=list)  # every location, all sources mixed
    # source -> locations read from it (`ethereum/main` on-chain, the asset label manual): what
    # a source filter must use - a location string alone does not say where it came from
    details_by_source: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Portfolio:
    """A full fetch: every holding, the per-source totals and any partial failures."""

    timestamp: str
    holdings: list[Holding] = field(default_factory=list)
    rows: list[AssetRow] = field(default_factory=list)
    totals: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    dust_value_usd: float = 0.0
    dust_assets: int = 0
    # snapshot_sources key -> status; filled by `fetch_portfolio` and `portfolio_from_db`
    sources: dict[str, SourceStatus] = field(default_factory=dict)
    # N3: one dict per (stablecoin held, reference): symbol, reference, price, error
    peg_checks: list[dict] = field(default_factory=list)

    @property
    def total_usd(self) -> float:
        return sum(self.totals.values())

    @property
    def ok(self) -> bool:
        """True when nothing failed; ``record`` refuses to snapshot an all-failed run."""
        return not self.errors

    def source_statuses(self) -> dict[str, SourceStatus]:
        """`sources` when the fetch filled it, else a best effort from holdings and errors."""
        if self.sources:
            return self.sources
        return source_statuses(self.errors, expected=holding_sources(self.holdings))


@dataclass
class SourceStatus:
    """Outcome of one source in one fetch: ``ok``, ``error`` or ``disabled``.

    ``error`` holds every message attributed to the source, one per line.
    """

    status: str
    error: str = ""


def classify_error(error: str, onchain_sources: Iterable[str] = (),
                   manual_sources: Iterable[str] = ()) -> list[str]:
    """The `snapshot_sources` keys an error message (as produced by the clients) is about.

    A pricing failure zeroes every row of its half, so it hits every source of that half.
    `onchain_sources` (``onchain:<chain>/<label>`` keys) lets a wallet label that contains
    spaces be matched exactly; without them the name is cut at the first space or colon.
    `manual_sources` (``manual:<label>``) do the same for manual assets.
    """
    text = error.strip()
    lowered = text.lower()
    onchain_sources = list(onchain_sources)
    for prefix, sources in _BINANCE_SECTIONS.items():
        if lowered.startswith(prefix):
            return list(sources)
    if lowered.startswith(f"{MANUAL}:"):
        # `manual:<label> <symbol>: ...`; longest label wins, as for wallets below
        for source in sorted(manual_sources, key=len, reverse=True):
            if text.startswith(source) and text[len(source):len(source) + 1] in (":", " "):
                return [source]
        return [MANUAL]
    if lowered.startswith("binance"):  # `binance prices:`, `binance: <client failure>`, no key...
        return list(BINANCE_SOURCES)
    if lowered.startswith("defillama") or lowered.startswith(f"{ONCHAIN}:"):
        return onchain_sources or [ONCHAIN]

    # `<chain>/<label>: ...` or `<chain>/<label> <token>: ...`; longest name wins, so the label
    # `main` does not swallow an error of the wallet `main 2`
    for source in sorted(onchain_sources, key=len, reverse=True):
        name = source.split(":", 1)[1]
        if text.startswith(name) and text[len(name):len(name) + 1] in (":", " "):
            return [source]
    head = text.split(":", 1)[0].split(" ", 1)[0]
    if "/" in head:
        return [onchain_source(head)]
    return [UNKNOWN_SOURCE]


def source_statuses(errors: Iterable[str], expected: Iterable[str] = (),
                    disabled: Iterable[str] = ()) -> dict[str, SourceStatus]:
    """Status of every source: `disabled`, else `error` if a message names it, else `ok`.

    `expected` are the sources that were queried; a source only named by an error is added.
    """
    statuses = {source: SourceStatus(STATUS_DISABLED) for source in disabled}
    statuses.update({source: SourceStatus(STATUS_OK) for source in expected})
    onchain = [source for source in statuses if source.startswith(f"{ONCHAIN}:")]
    manual = [source for source in statuses if source.startswith(f"{MANUAL}:")]
    for error in errors:
        for source in classify_error(error, onchain, manual):
            status = statuses.setdefault(source, SourceStatus(STATUS_OK))
            status.status = STATUS_ERROR
            status.error = f"{status.error}\n{error}" if status.error else error
    return statuses


def holding_sources(holdings: Iterable[Holding]) -> set[str]:
    """The `snapshot_sources` keys that holdings were read from (`onchain:<detail>` on-chain)."""
    sources = set()
    for holding in holdings:
        if holding.source == ONCHAIN:
            sources.add(onchain_source(holding.detail) if holding.detail else ONCHAIN)
        elif holding.source == MANUAL:
            sources.add(manual_source(holding.detail) if holding.detail else MANUAL)
        else:
            sources.add(holding.source)
    return sources
