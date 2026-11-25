"""Fetch every source and aggregate the holdings into the per-asset view."""
from __future__ import annotations

from models import (
    BINANCE_SOURCES,
    MANUAL,
    ONCHAIN,
    SOURCE_LABELS,
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
    for row in grouped.values():
        # the same symbol can arrive from several sources at slightly different prices
        # (a CEX ticker vs a DEX quote); the average over the *priced* quantity keeps the
        # Price column consistent with Value, and stays 0 when nothing could be priced
        quantity = priced.get(row.symbol, 0.0)
        row.price_usd = (row.value_usd / quantity) if quantity > 0 else 0.0
        row.pct = (row.value_usd / total * 100) if total > 0 else 0.0
    kept, dust = split_dust(grouped.values(), min_value, value=lambda r: r.value_usd)

    portfolio.rows = sorted(kept, key=lambda r: (-r.value_usd, r.symbol))
    portfolio.dust_value_usd = dust["value_usd"]
    portfolio.dust_assets = dust["assets"]
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


def split_dust(rows, min_value: float, value=lambda row: row["value_usd"]) -> tuple[list, dict]:
    """The one dust rule of every list: a row worth less than `min_value` is not shown.

    Returns the rows kept (order preserved) and `{"assets": n, "value_usd": total}` for the
    rest, unrounded. The Coins lists apply it per symbol, the Wallets views per row inside a
    wallet, so "N balances under $1" means the same thing on every tab and in every view.
    """
    kept, dust = [], {"assets": 0, "value_usd": 0.0}
    for row in rows:
        worth = value(row)
        if worth >= min_value:
            kept.append(row)
        else:
            dust["assets"] += 1
            dust["value_usd"] += worth
    return kept, dust


def _rounded(dust: dict) -> dict:
    return {"assets": dust["assets"], "value_usd": round(dust["value_usd"], 2)}


def tab_dust(holdings: list[Holding], sources, min_value: float) -> dict:
    """What a tab's Coins list folds away: symbols worth less than `min_value` *in `sources`*.

    Compared at the cent, as the list shows it, so a coin is either listed or counted here.
    """
    values: dict[str, float] = {}
    for holding in holdings:
        if holding.quantity > 0 and holding.source in sources:
            values[holding.symbol] = values.get(holding.symbol, 0.0) + holding.value_usd
    _, dust = split_dust(values.values(), min_value, value=lambda v: round(v, 2))
    return _rounded(dust)


def _pct(value: float, total: float) -> float:
    return round(value / total * 100, 4) if total > 0 else 0.0


def _priced(row: dict) -> dict:
    row["price_usd"] = row["value_usd"] / row["quantity"] if row["quantity"] > 0 else 0.0
    return row


def binance_wallets(holdings: list[Holding], min_value: float = 0.0) -> list[dict]:
    """The Binance tab seen by account: one row per account (Spot, Earn, Funding).

    Earn stays one row whatever the product, its Flexible/Locked `split` kept for the detail.
    `value_usd`, `split` and `pct` (share of the Binance total) count every balance; `assets`
    lists the rows worth `min_value` or more and the rest is summed in `dust`.
    """
    accounts: dict[str, dict] = {}
    for holding in holdings:
        if holding.quantity <= 0 or holding.source not in BINANCE_SOURCES:
            continue
        account = accounts.setdefault(holding.source, {
            "id": holding.source, "label": SOURCE_LABELS[holding.source], "value_usd": 0.0, "rows": {},
        })
        product = (holding.detail or None) if holding.source == "binance_earn" else None
        row = account["rows"].setdefault((holding.symbol, product), {
            "symbol": holding.symbol, "product": product, "quantity": 0.0, "value_usd": 0.0,
        })
        row["quantity"] += holding.quantity
        row["value_usd"] += holding.value_usd
        account["value_usd"] += holding.value_usd

    total = sum(account["value_usd"] for account in accounts.values())
    out = []
    for account in accounts.values():
        rows = sorted((_priced(row) for row in account.pop("rows").values()),
                      key=lambda r: (-r["value_usd"], r["symbol"], r["product"] or ""))
        if account["id"] == "binance_earn":
            split: dict[str, float] = {}
            for row in rows:
                key = row["product"] or "other"
                split[key] = split.get(key, 0.0) + row["value_usd"]
            account["split"] = {key: round(value, 2) for key, value in split.items()}
        shown, dust = split_dust(rows, min_value)
        for row in shown:
            row["value_usd"] = round(row["value_usd"], 2)
        account["assets"] = shown
        account["dust"] = _rounded(dust)
        account["asset_count"] = len({row["symbol"] for row in rows})
        account["pct"] = _pct(account["value_usd"], total)
        account["value_usd"] = round(account["value_usd"], 2)
        out.append(account)
    return sorted(out, key=lambda w: (-w["value_usd"], w["label"].casefold()))


def personal_wallets(holdings: list[Holding], wallet_labels=(), manual_assets=(),
                     min_value: float = 0.0) -> list[dict]:
    """The Personal tab seen by wallet: every self-custody balance (on-chain + manual), one row
    per *physical* wallet. Display only: nothing here feeds PnL, TWR or snapshots.

    An on-chain holding (`detail = <chain>/<label>`) belongs to the wallet `<label>`, whatever the
    chain: one row per device, not per (chain, address). A manual asset joins the wallet its
    `linked_wallet` names (the BTC on the Ledger whose EVM address is tracked) and is listed in
    its `manual` rows, apart from the on-chain `assets`, so a declared balance never passes for
    an RPC read. Without a link it is a row of its own (`type = "manual"`), even when its label
    equals a wallet label - only an explicit link merges.

    `value_usd` is the wallet's whole worth (on-chain + linked manual, split in
    `onchain_value_usd` / `manual_value_usd`), `pct` its share of the Personal total. Rows worth
    less than `min_value`, on-chain or manual, are summed in `dust` (`split_dust`).
    """
    configured = set(wallet_labels)
    by_label = {asset.label: asset for asset in manual_assets}
    wallets: dict[str, dict] = {}
    rows: dict[str, dict[tuple, dict]] = {}

    def wallet(kind: str, label: str) -> dict:
        key = f"{kind}:{label}"
        if key not in wallets:
            wallets[key] = {
                "id": key, "label": label, "type": kind,
                "configured": kind == "wallet" and label in configured,
                "manual_kind": None, "linked_manual": [], "chains": {},
                "value_usd": 0.0, "onchain_value_usd": 0.0, "manual_value_usd": 0.0,
            }
            rows[key] = {}
        return wallets[key]

    for holding in holdings:
        if holding.quantity <= 0 or holding.source not in (ONCHAIN, MANUAL):
            continue
        if holding.source == ONCHAIN:
            chain, _, label = holding.detail.partition("/")
            if not label:  # pre-v2 row without a wallet: nothing to group it by
                chain, label = "", holding.detail or "On-chain"
            card = wallet("wallet", label)
            if chain:
                card["chains"][chain] = card["chains"].get(chain, 0.0) + holding.value_usd
            row_key = (ONCHAIN, holding.symbol, chain)
            fields = {"chain": chain or None}
            card["onchain_value_usd"] += holding.value_usd
        else:
            asset = by_label.get(holding.detail)
            if asset is not None and asset.linked_wallet:
                card = wallet("wallet", asset.linked_wallet)
                if asset.label not in card["linked_manual"]:
                    card["linked_manual"].append(asset.label)
            else:
                card = wallet("manual", holding.detail or "Manual")
                card["manual_kind"] = asset.kind if asset is not None else None
            row_key = (MANUAL, holding.symbol, holding.detail)
            fields = {"label": holding.detail or None, "kind": asset.kind if asset is not None else None}
            card["manual_value_usd"] += holding.value_usd
        card["value_usd"] += holding.value_usd
        row = rows[card["id"]].setdefault(row_key, {
            "source": holding.source, "symbol": holding.symbol, **fields, "quantity": 0.0, "value_usd": 0.0,
        })
        row["quantity"] += holding.quantity
        row["value_usd"] += holding.value_usd

    total = sum(card["value_usd"] for card in wallets.values())
    out = []
    for key, card in wallets.items():
        every = sorted((_priced(row) for row in rows[key].values()),
                       key=lambda r: (-r["value_usd"], r["symbol"], r.get("chain") or r.get("label") or ""))
        shown, dust = split_dust(every, min_value)
        for row in shown:
            row["value_usd"] = round(row["value_usd"], 2)
        card["assets"] = [{k: v for k, v in row.items() if k != "source"} for row in shown if row["source"] == ONCHAIN]
        card["manual"] = [{k: v for k, v in row.items() if k != "source"} for row in shown if row["source"] == MANUAL]
        card["dust"] = _rounded(dust)
        card["asset_count"] = len({row["symbol"] for row in every})
        card["chains"] = {chain: round(value, 2) for chain, value in card["chains"].items()}
        card["pct"] = _pct(card["value_usd"], total)
        for field_name in ("value_usd", "onchain_value_usd", "manual_value_usd"):
            card[field_name] = round(card[field_name], 2)
        out.append(card)
    return sorted(out, key=lambda c: (-c["value_usd"], c["label"].casefold()))


def source_wallets(holdings: list[Holding], wallet_labels=(), manual_assets=(), min_value: float = 0.0) -> dict:
    """The "Wallets" side of the Coins | Wallets switch, for each tab that has one."""
    return {
        "binance": binance_wallets(holdings, min_value),
        "personal": personal_wallets(holdings, wallet_labels, manual_assets, min_value),
    }
