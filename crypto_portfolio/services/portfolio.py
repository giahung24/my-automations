"""Fetch every source and aggregate the holdings into the per-asset view."""
from __future__ import annotations

from models import (
    BINANCE_SOURCES,
    MANUAL,
    SOURCES,
    STATUS_ERROR,
    STATUS_INFERRED_ERROR,
    AssetRow,
    Holding,
    Portfolio,
    onchain_source,
    source_statuses,
)
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
    """Fetch Binance + every configured wallet + the manual assets, then aggregate.

    Never raises on a dead source.
    """
    # imported here so `--help` and the chart commands work without network libraries warm
    from clients.binance import BinanceClient
    from clients.onchain import OnChainClient
    from services.manual import fetch_manual

    holdings: list[Holding] = []
    errors: list[str] = []
    expected: list[str] = []  # snapshot_sources keys queried this run
    disabled: list[str] = []
    client = None

    if config.binance_enabled:
        expected.extend(BINANCE_SOURCES)
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
        expected.extend(BINANCE_SOURCES)
        errors.append("binance: enabled but no API key/secret configured")
    else:
        disabled.extend(BINANCE_SOURCES)

    if config.has_wallets:
        onchain = OnChainClient(config.chains, timeout=config.timeout, max_workers=config.max_workers)
        queried, skipped = onchain.sources(config.evm_wallets, config.solana_wallets)
        expected.extend(onchain_source(name) for name in queried)
        disabled.extend(onchain_source(name) for name in skipped)
        try:
            onchain_holdings, onchain_errors = onchain.fetch_all(config.evm_wallets, config.solana_wallets)
        except Exception as exc:
            onchain_holdings, onchain_errors = [], [f"onchain: {exc}"]
        holdings.extend(onchain_holdings)
        errors.extend(onchain_errors)

    # Manual assets (M6.2): priced from the Binance ticker already loaded above, when there is one.
    try:
        manual_holdings, manual_errors, manual_expected = fetch_manual(config, binance_client=client)
    except Exception as exc:  # noqa: BLE001 - a manual-asset bug must not sink the other sources
        manual_holdings, manual_errors, manual_expected = [], [f"{MANUAL}: {exc}"], []
    holdings.extend(manual_holdings)
    errors.extend(manual_errors)
    expected.extend(manual_expected)

    portfolio = aggregate(holdings, config.min_value if min_value is None else min_value)
    portfolio.errors = errors
    portfolio.sources = source_statuses(errors, expected=expected, disabled=disabled)
    return portfolio


def portfolio_from_db(database, min_value: float = 0.0) -> Portfolio | None:
    """Rebuild the latest stored portfolio, so charts and the web view work with no network.

    Chain/wallet `detail` and `price_id` come back from schema v2 rows (older rows have them
    empty, so their `locations` is empty), and so do the per-source statuses and `errors`.
    """
    snapshot = database.latest()
    if snapshot is None:
        return None
    holdings = [
        Holding(symbol=row["symbol"], source=row["source"], quantity=row["quantity"],
                price_usd=row["price_usd"], detail=row.get("detail") or "",
                price_id=row.get("price_id") or "")
        for row in database.assets(snapshot.id)
    ]
    portfolio = aggregate(holdings, min_value)
    portfolio.timestamp = snapshot.timestamp
    portfolio.sources = database.sources(snapshot.id)
    portfolio.errors = stored_errors(portfolio.sources)
    return portfolio


def stored_errors(sources: dict) -> list[str]:
    """The fetch errors of a stored snapshot, one per message, in source order, deduplicated.

    One message can be stored on several sources (a pricing failure hits a whole half).
    """
    errors: list[str] = []
    for source, status in sorted(sources.items()):
        if status.status == STATUS_ERROR:
            messages = status.error.splitlines() or [f"{source}: error"]
        elif status.status == STATUS_INFERRED_ERROR:
            messages = [f"{source}: {status.error or 'probable fetch error (inferred)'}"]
        else:
            continue
        errors.extend(message for message in messages if message not in errors)
    return errors


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
