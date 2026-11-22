"""Fetch every source and aggregate the holdings into the per-asset view."""
from __future__ import annotations

from models import SOURCES, AssetRow, Holding, Portfolio
from services.db import utc_now_iso


def aggregate(holdings: list[Holding], min_value: float = 0.0) -> Portfolio:
    """Group holdings by symbol, price them out and apply the dust filter.

    Dust is removed from `rows` but kept in `holdings` and in the totals - hiding a $0.40
    row is a display choice, and it must not change what the portfolio is worth.
    """
    portfolio = Portfolio(timestamp=utc_now_iso(), holdings=list(holdings))
    portfolio.totals = {source: 0.0 for source in SOURCES}

    grouped: dict[str, AssetRow] = {}
    priced: dict[str, float] = {}  # symbol -> quantity that actually got a price
    for holding in holdings:
        if holding.quantity <= 0:
            continue
        row = grouped.setdefault(holding.symbol, AssetRow(symbol=holding.symbol))
        row.by_source[holding.source] = row.by_source.get(holding.source, 0.0) + holding.quantity
        row.quantity += holding.quantity
        row.value_usd += holding.value_usd
        if holding.price_usd > 0:
            priced[holding.symbol] = priced.get(holding.symbol, 0.0) + holding.quantity
        if holding.detail and holding.detail not in row.details:
            row.details.append(holding.detail)
        if holding.source in portfolio.totals:
            portfolio.totals[holding.source] += holding.value_usd

    total = portfolio.total_usd
    kept, dust_value, dust_count = [], 0.0, 0
    for row in grouped.values():
        # the same symbol can arrive from several sources at slightly different prices
        # (a CEX ticker vs a DEX quote); the average over the *priced* quantity keeps the
        # Price column consistent with Value, and stays 0 when nothing could be priced
        quantity = priced.get(row.symbol, 0.0)
        row.price_usd = (row.value_usd / quantity) if quantity > 0 else 0.0
        row.pct = (row.value_usd / total * 100) if total > 0 else 0.0
        if row.value_usd >= min_value:
            kept.append(row)
        else:
            dust_value += row.value_usd
            dust_count += 1

    portfolio.rows = sorted(kept, key=lambda r: (-r.value_usd, r.symbol))
    portfolio.dust_value_usd = dust_value
    portfolio.dust_assets = dust_count
    return portfolio


def fetch_portfolio(config, min_value: float | None = None) -> Portfolio:
    """Fetch Binance + every configured wallet, then aggregate. Never raises on a dead source."""
    # imported here so `--help` and the chart commands work without network libraries warm
    from clients.binance import BinanceClient
    from clients.onchain import OnChainClient

    holdings: list[Holding] = []
    errors: list[str] = []

    if config.binance_enabled:
        client = BinanceClient(
            config.binance["api_key"],
            config.binance["api_secret"],
            timeout=config.timeout,
            recv_window=int(config.binance.get("recv_window", 5000)),
        )
        try:
            binance_holdings, binance_errors = client.fetch_all(config.stablecoins)
        except Exception as exc:  # a client-level failure must not sink the on-chain half
            binance_holdings, binance_errors = [], [f"binance: {exc}"]
        holdings.extend(binance_holdings)
        errors.extend(binance_errors)
    elif config.binance.get("enabled"):
        errors.append("binance: enabled but no API key/secret configured")

    if config.has_wallets:
        onchain = OnChainClient(config.chains, timeout=config.timeout, max_workers=config.max_workers)
        try:
            onchain_holdings, onchain_errors = onchain.fetch_all(config.evm_wallets, config.solana_wallets)
        except Exception as exc:
            onchain_holdings, onchain_errors = [], [f"onchain: {exc}"]
        holdings.extend(onchain_holdings)
        errors.extend(onchain_errors)

    portfolio = aggregate(holdings, config.min_value if min_value is None else min_value)
    portfolio.errors = errors
    return portfolio


def portfolio_from_db(database, min_value: float = 0.0) -> Portfolio | None:
    """Rebuild the latest stored portfolio, so charts and the web view work with no network.

    Stored rows carry no chain/wallet detail, so `locations` comes back empty.
    """
    snapshot = database.latest()
    if snapshot is None:
        return None
    holdings = [
        Holding(symbol=row["symbol"], source=row["source"], quantity=row["quantity"],
                price_usd=row["price_usd"])
        for row in database.assets(snapshot.id)
    ]
    portfolio = aggregate(holdings, min_value)
    portfolio.timestamp = snapshot.timestamp
    return portfolio


def to_dict(portfolio: Portfolio) -> dict:
    """Stable JSON schema for bots and agents consuming `show --json`."""
    return {
        "timestamp": portfolio.timestamp,
        "total_usd": round(portfolio.total_usd, 2),
        "sources": {source: round(value, 2) for source, value in portfolio.totals.items()},
        "assets": [
            {
                "symbol": row.symbol,
                "quantity": row.quantity,
                "price_usd": row.price_usd,
                "value_usd": round(row.value_usd, 2),
                "pct": round(row.pct, 4),
                "by_source": {source: qty for source, qty in row.by_source.items()},
                "locations": row.details,
            }
            for row in portfolio.rows
        ],
        "dust": {"assets": portfolio.dust_assets, "value_usd": round(portfolio.dust_value_usd, 2)},
        "errors": portfolio.errors,
        "ok": portfolio.ok,
    }
