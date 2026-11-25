"""Fetch every source and aggregate the holdings into the per-asset view."""
from __future__ import annotations

from models import (
    BINANCE_SOURCES,
    MANUAL,
    ONCHAIN,
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
        if holding.detail:
            where = row.details_by_source.setdefault(holding.source, [])
            if holding.detail not in where:
                where.append(holding.detail)
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
                "locations_by_source": {source: list(names) for source, names in row.details_by_source.items()},
            }
            for row in portfolio.rows
        ],
        "dust": {"assets": portfolio.dust_assets, "value_usd": round(portfolio.dust_value_usd, 2)},
        "errors": portfolio.errors,
        "ok": portfolio.ok,
    }


def wallet_groups(holdings: list[Holding], wallet_labels=(), manual_assets=(),
                  min_value: float = 0.0) -> list[dict]:
    """Self-custody holdings grouped by *physical* wallet, for display only.

    An on-chain holding (`detail = <chain>/<label>`) belongs to the wallet `<label>`, whatever the
    chain: one card per device, not per (chain, address). A manual asset joins the card of the
    wallet its `linked_wallet` names (the BTC on the Ledger whose EVM address is tracked);
    without one it gets a card of its own, even when its label happens to equal a wallet label -
    only an explicit link merges. Every row keeps its `source`, so on-chain balances read by RPC
    and balances declared by hand stay distinguishable; nothing here feeds PnL, TWR or snapshots.

    On-chain rows worth less than `min_value` are folded into the card's `dust`; manual rows are
    always shown (a declared coin nobody could price must stay visible).
    """
    configured = list(wallet_labels)
    by_label = {asset.label: asset for asset in manual_assets}
    groups: dict[tuple[str, str], dict] = {}
    rows: dict[tuple[str, str], dict[tuple, dict]] = {}

    def group(kind: str, label: str) -> dict:
        key = (kind, label)
        if key not in groups:
            groups[key] = {
                "id": f"{kind}:{label}", "label": label, "type": kind,
                "configured": kind == "wallet" and label in configured,
                "manual_kind": None, "linked_manual": [], "chains": [],
                "value_usd": 0.0, "onchain_value_usd": 0.0, "manual_value_usd": 0.0,
                "holdings": [], "dust": {"assets": 0, "value_usd": 0.0},
            }
            rows[key] = {}
        return groups[key]

    for holding in holdings:
        if holding.quantity <= 0 or holding.source not in (ONCHAIN, MANUAL):
            continue
        if holding.source == ONCHAIN:
            chain, _, label = holding.detail.partition("/")
            if not label:  # pre-v2 row without a wallet: nothing to group it by
                chain, label = "", holding.detail or "On-chain"
            card = group("wallet", label)
            if chain and chain not in card["chains"]:
                card["chains"].append(chain)
            row_key = (ONCHAIN, holding.symbol, chain)
            extra = {"chain": chain or None, "manual_label": None, "manual_kind": None}
            card["onchain_value_usd"] += holding.value_usd
        else:
            asset = by_label.get(holding.detail)
            if asset is not None and asset.linked_wallet:
                card = group("wallet", asset.linked_wallet)
                if asset.label not in card["linked_manual"]:
                    card["linked_manual"].append(asset.label)
            else:
                card = group("manual", holding.detail or "Manual")
                card["manual_kind"] = asset.kind if asset is not None else None
            row_key = (MANUAL, holding.symbol, holding.detail)
            extra = {"chain": None, "manual_label": holding.detail or None,
                     "manual_kind": asset.kind if asset is not None else None}
            card["manual_value_usd"] += holding.value_usd
        card["value_usd"] += holding.value_usd
        row = rows[(card["type"], card["label"])].setdefault(row_key, {
            "symbol": holding.symbol, "source": holding.source, **extra,
            "quantity": 0.0, "value_usd": 0.0,
        })
        row["quantity"] += holding.quantity
        row["value_usd"] += holding.value_usd

    out = []
    for key, card in groups.items():
        shown = []
        for row in rows[key].values():
            row["price_usd"] = row["value_usd"] / row["quantity"] if row["quantity"] > 0 else 0.0
            if row["source"] == ONCHAIN and row["value_usd"] < min_value:
                card["dust"]["assets"] += 1
                card["dust"]["value_usd"] += row["value_usd"]
                continue
            row["value_usd"] = round(row["value_usd"], 2)
            shown.append(row)
        # on-chain first inside the same value, so the card reads "what RPC sees, then what you declared"
        card["holdings"] = sorted(shown, key=lambda r: (-r["value_usd"], r["source"] != ONCHAIN, r["symbol"]))
        for field_name in ("value_usd", "onchain_value_usd", "manual_value_usd"):
            card[field_name] = round(card[field_name], 2)
        card["dust"]["value_usd"] = round(card["dust"]["value_usd"], 2)
        out.append(card)
    return sorted(out, key=lambda c: (-c["value_usd"], c["label"].casefold()))
