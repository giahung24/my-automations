"""Shared data structures for the portfolio tracker.

Every client returns a list of :class:`Holding` plus a list of error strings, so a
dead RPC or an expired API key degrades one source instead of killing the run.
"""
from __future__ import annotations

from dataclasses import dataclass, field

BINANCE_SPOT = "binance_spot"
BINANCE_EARN = "binance_earn"
BINANCE_FUNDING = "binance_funding"
ONCHAIN = "onchain"
SOURCES = (BINANCE_SPOT, BINANCE_EARN, BINANCE_FUNDING, ONCHAIN)
SOURCE_LABELS = {
    BINANCE_SPOT: "Binance Spot",
    BINANCE_EARN: "Binance Earn",
    BINANCE_FUNDING: "Binance Funding",
    ONCHAIN: "On-chain",
}


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
    detail: str = ""  # chain / wallet label, shown for on-chain rows
    price_id: str = ""

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
    details: list[str] = field(default_factory=list)


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

    @property
    def total_usd(self) -> float:
        return sum(self.totals.values())

    @property
    def ok(self) -> bool:
        """True when nothing failed; ``record`` refuses to snapshot an all-failed run."""
        return not self.errors
