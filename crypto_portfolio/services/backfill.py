"""P0.8: reconstruct five years of monthly history (60 checkpoints) from public Binance klines.

The collector only started recording in November 2026, so the 1Y / 5Y / ALL views of the
dashboard would be (almost) empty. This module rebuilds one snapshot on the first day of each
month from 2021-11 to 2026-10 by asking one question: *what would today's portfolio have been
worth back then?*

Method ("current weights, past prices"):

* The base is the newest **fetched** snapshot in the database: every holding with its quantity,
  price and value at that time (`V_now`).
* For each month `t`, every holding whose asset has a historical price `p(t)` is revalued at
  `quantity x p(t)`. Prices are the **open** of Binance's monthly `<ASSET>USDT` candle, i.e. the
  price at 00:00 UTC on the first of the month. USDT is the quote currency, so it is worth 1.0;
  the other USD stablecoins are pegged to it and wrapped tokens follow their underlying
  (`WBTC -> BTC`, `WETH -> ETH`, ...).
* A holding with no price for `t` (an asset outside the fetched list, or not yet listed) moves
  like the priced part of the portfolio: its value is `value_now x k(t)` with
  `k(t) = sum(quantity x p(t)) / sum(value_now)` over the priced holdings. In weight terms the
  checkpoint is `NAV(t) = NAV_now x sum_i w_i x p_i(t) / p_i(now)` with the weights `w_i`
  renormalised over the priced assets. Such rows are stored with `price_id = 'backfill:proxy'`.

Each checkpoint is written with `snapshots.is_backfilled = 1` and `trigger = 'backfill'`, keeps
the per-source split of today's portfolio, and stores its per-asset rows (`detail` and
`price_id` say how each price was obtained). Re-running replaces the previous set atomically.
Checkpoints at or after the first fetched snapshot are skipped: real data always wins.

    .venv/bin/python -m services.backfill                 # 2021-11 .. 2026-10, BTC ETH SOL USDT
    .venv/bin/python -m services.backfill --dry-run       # print the checkpoints, write nothing
    .venv/bin/python -m services.backfill --symbols BTC ETH SOL BNB LINK
    .venv/bin/python main.py backfill                     # same, through the main CLI
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import requests

if __package__ in (None, ""):  # `python services/backfill.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clients.binance import PUBLIC_HOSTS
from models import Holding, Portfolio
from services.db import Database
from services.portfolio import aggregate

log = logging.getLogger(__name__)

KLINES_PATH = "/api/v3/klines"
INTERVAL = "1M"
QUOTE = "USDT"
DEFAULT_START = date(2021, 11, 1)
DEFAULT_END = date(2026, 10, 1)       # inclusive: 60 monthly checkpoints
CORE_SYMBOLS = ("BTC", "ETH", "SOL")  # + USDT, which is the quote (always 1.0)
MAX_KLINES = 1000                     # Binance caps `limit` at 1000 candles per call
TIMEOUT = 20

# Valued at the USDT price (1.0). Depegs are ignored: a monthly checkpoint cannot see them anyway.
STABLECOINS = frozenset({"USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI", "USDP", "PYUSD", "USDE"})
# Wrapped / bridged tokens priced as their underlying asset.
ALIASES = {"WBTC": "BTC", "BTCB": "BTC", "WETH": "ETH", "BETH": "ETH", "WSOL": "SOL"}

PROXY_PRICE_ID = "backfill:proxy"
BACKFILL_DETAIL = "backfill"


class BackfillError(RuntimeError):
    """The history cannot be rebuilt (no base snapshot, missing prices, unreachable API)."""


@dataclass
class BackfillResult:
    checkpoints: list[Portfolio]
    snapshot_ids: list[int] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # month keys covered by real snapshots
    base_timestamp: str = ""
    base_total_usd: float = 0.0
    coverage: float = 0.0  # share of the base value that has its own historical price
    dry_run: bool = False


# -- calendar --------------------------------------------------------------------
def month_starts(start: date, end: date) -> list[datetime]:
    """First day (00:00 UTC) of every month from `start` to `end`, both inclusive."""
    if (end.year, end.month) < (start.year, start.month):
        raise ValueError(f"backfill range is empty: {start:%Y-%m} > {end:%Y-%m}")
    months, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(datetime(year, month, 1, tzinfo=timezone.utc))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def month_key(moment: datetime) -> str:
    return f"{moment:%Y-%m}"


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def parse_month(value: str) -> date:
    """`YYYY-MM` (or `YYYY-MM-DD`) -> the first day of that month."""
    try:
        parsed = datetime.strptime(value[:7], "%Y-%m")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {value!r}") from exc
    return date(parsed.year, parsed.month, 1)


# -- prices ----------------------------------------------------------------------
def price_key(symbol: str) -> str:
    """The asset whose price series values `symbol` (`USDC -> USDT`, `WETH -> ETH`)."""
    symbol = symbol.upper()
    if symbol in STABLECOINS:
        return QUOTE
    return ALIASES.get(symbol, symbol)


def fetch_monthly_opens(symbol: str, months: Sequence[datetime],
                        session: requests.Session | None = None,
                        hosts: Sequence[str] = PUBLIC_HOSTS) -> dict[str, float]:
    """Open price of the monthly `<symbol>USDT` candle for each of `months` -> `{YYYY-MM: price}`.

    Public market data: no API key. Hosts are tried in order (the `binance.vision` mirror first,
    since api.binance.com answers 451 from some regions). Months before the pair was listed are
    simply absent from the result.
    """
    if len(months) > MAX_KLINES:
        raise BackfillError(f"at most {MAX_KLINES} months per call, got {len(months)}")
    session = session or requests.Session()
    wanted = {_ms(m): month_key(m) for m in months}
    params = {
        "symbol": f"{symbol.upper()}{QUOTE}",
        "interval": INTERVAL,
        "startTime": _ms(months[0]),
        "endTime": _ms(months[-1]),
        "limit": len(months),
    }
    errors: list[str] = []
    for host in hosts:
        try:
            response = session.get(f"{host}{KLINES_PATH}", params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            errors.append(f"{host}: {exc}")
            continue
        if not response.ok:
            errors.append(f"{host}: HTTP {response.status_code} {response.text[:160]}")
            continue
        try:
            candles = response.json()
        except ValueError:
            errors.append(f"{host}: response is not JSON")
            continue
        return _parse_klines(params["symbol"], candles, wanted)
    raise BackfillError(f"klines {params['symbol']}: every host failed ({'; '.join(errors)})")


def _parse_klines(pair: str, candles: object, wanted: dict[int, str]) -> dict[str, float]:
    if not isinstance(candles, list):
        raise BackfillError(f"klines {pair}: unexpected payload {str(candles)[:160]}")
    prices: dict[str, float] = {}
    for candle in candles:
        try:
            open_time, open_price = int(candle[0]), float(candle[1])
        except (TypeError, ValueError, IndexError) as exc:
            raise BackfillError(f"klines {pair}: malformed candle {candle!r}") from exc
        key = wanted.get(open_time)
        if key is None:  # not one of our checkpoints (should not happen with startTime/endTime)
            continue
        if open_price <= 0:
            raise BackfillError(f"klines {pair}: non-positive open price for {key}")
        prices[key] = open_price
    return prices


def fetch_price_history(symbols: Iterable[str], months: Sequence[datetime],
                        session: requests.Session | None = None,
                        required: Iterable[str] = CORE_SYMBOLS,
                        hosts: Sequence[str] = PUBLIC_HOSTS) -> dict[str, dict[str, float]]:
    """`{asset: {YYYY-MM: usd}}` for `symbols`, plus USDT at 1.0 for every month.

    An asset of `required` with any month missing is an error: the checkpoints must form an
    unbroken monthly series. Other assets may start later (listed after `months[0]`).
    """
    session = session or requests.Session()
    keys = [month_key(m) for m in months]
    history: dict[str, dict[str, float]] = {QUOTE: {key: 1.0 for key in keys}}
    required = {price_key(s) for s in required} - {QUOTE}
    for asset in dict.fromkeys(price_key(s) for s in symbols):  # ordered, deduplicated
        if asset == QUOTE:
            continue
        history[asset] = fetch_monthly_opens(asset, months, session=session, hosts=hosts)
        missing = [key for key in keys if key not in history[asset]]
        if missing and asset in required:
            raise BackfillError(
                f"{asset}{QUOTE}: no monthly price for {len(missing)} checkpoint(s) "
                f"({', '.join(missing[:6])}{', ...' if len(missing) > 6 else ''})"
            )
        if missing:
            log.info("%s%s: no price for %d month(s), proxied", asset, QUOTE, len(missing))
    return history


# -- reconstruction ----------------------------------------------------------------
def base_holdings(database: Database) -> tuple[str, list[Holding]]:
    """Timestamp and priced, non-empty holdings of the newest fetched snapshot."""
    snapshot = database.latest()
    if snapshot is None:
        raise BackfillError("no fetched snapshot to backfill from; run `main.py record` first")
    holdings = [
        Holding(symbol=row["symbol"], source=row["source"], quantity=float(row["quantity"]),
                price_usd=float(row["price_usd"]), detail=row.get("detail") or "",
                price_id=row.get("price_id") or "")
        for row in database.assets(snapshot.id)
    ]
    holdings = [h for h in holdings if h.quantity > 0 and h.price_usd > 0]
    if not holdings:
        raise BackfillError(f"snapshot {snapshot.timestamp} has no priced holding to backfill from")
    return snapshot.timestamp, holdings


def reconstruct(holdings: Sequence[Holding], prices: dict[str, dict[str, float]],
                months: Sequence[datetime]) -> list[Portfolio]:
    """One portfolio per month: today's quantities at that month's prices (see module doc)."""
    checkpoints = []
    for moment in months:
        key = month_key(moment)
        priced_now = priced_then = 0.0
        for holding in holdings:
            price = prices.get(price_key(holding.symbol), {}).get(key)
            if price is not None:
                priced_now += holding.value_usd
                priced_then += holding.quantity * price
        if priced_now <= 0:
            raise BackfillError(
                f"{key}: none of the current holdings has a historical price; "
                f"add their symbols with --symbols"
            )
        factor = priced_then / priced_now

        rebuilt = []
        for holding in holdings:
            asset = price_key(holding.symbol)
            price = prices.get(asset, {}).get(key)
            if price is None:
                price, price_id = holding.price_usd * factor, PROXY_PRICE_ID
            else:
                price_id = f"backfill:binance:{asset}{QUOTE}" if asset != QUOTE else f"backfill:peg:{QUOTE}"
            rebuilt.append(Holding(symbol=holding.symbol, source=holding.source,
                                   quantity=holding.quantity, price_usd=price,
                                   detail=holding.detail or BACKFILL_DETAIL, price_id=price_id))
        portfolio = aggregate(rebuilt, 0.0)
        portfolio.timestamp = moment.isoformat()
        checkpoints.append(portfolio)
    return checkpoints


def coverage(holdings: Sequence[Holding], prices: dict[str, dict[str, float]],
             months: Sequence[datetime]) -> float:
    """Share of the current value whose asset has a (fetched or pegged) price for every month."""
    keys = [month_key(m) for m in months]
    total = sum(h.value_usd for h in holdings)
    priced = sum(h.value_usd for h in holdings
                 if all(key in prices.get(price_key(h.symbol), {}) for key in keys))
    return priced / total if total > 0 else 0.0


def run_backfill(database: Database, start: date = DEFAULT_START, end: date = DEFAULT_END,
                 symbols: Iterable[str] = CORE_SYMBOLS, session: requests.Session | None = None,
                 dry_run: bool = False, hosts: Sequence[str] = PUBLIC_HOSTS) -> BackfillResult:
    """Fetch the prices, rebuild the checkpoints and (unless `dry_run`) store them.

    Every month must get a price for `CORE_SYMBOLS` before anything is written; a failure
    leaves the database untouched. Months at or after the first fetched snapshot are skipped.
    """
    base_ts, holdings = base_holdings(database)
    months = month_starts(start, end)
    oldest = database.snapshot_after("")  # oldest fetched snapshot (backfilled ones excluded)
    first_real = oldest.dt if oldest else None
    skipped = [month_key(m) for m in months if first_real and m >= first_real]
    months = [m for m in months if not (first_real and m >= first_real)]
    if not months:
        raise BackfillError(f"every month of {start:%Y-%m}..{end:%Y-%m} already has fetched snapshots")

    prices = fetch_price_history(list(CORE_SYMBOLS) + list(symbols), months, session=session,
                                 hosts=hosts)
    checkpoints = reconstruct(holdings, prices, months)
    result = BackfillResult(
        checkpoints=checkpoints, skipped=skipped, base_timestamp=base_ts,
        base_total_usd=sum(h.value_usd for h in holdings), coverage=coverage(holdings, prices, months),
        dry_run=dry_run,
    )
    if not dry_run:
        result.snapshot_ids = database.replace_backfill(checkpoints)
        log.info("%s: stored %d backfilled checkpoint(s) %s..%s", database.path, len(checkpoints),
                 month_key(months[0]), month_key(months[-1]))
    return result


# -- CLI ---------------------------------------------------------------------------
def render(result: BackfillResult) -> str:
    lines = [
        f"base: snapshot {result.base_timestamp}  ${result.base_total_usd:,.2f}  "
        f"(full price history for {result.coverage:.1%} of it; the rest is proxied where missing)",
        f"{'month':<8} {'total_usd':>14} {'vs base':>9}",
    ]
    for portfolio in result.checkpoints:
        ratio = portfolio.total_usd / result.base_total_usd if result.base_total_usd else 0.0
        lines.append(f"{portfolio.timestamp[:7]:<8} {portfolio.total_usd:>14,.2f} {ratio:>8.1%}")
    if result.skipped:
        lines.append(f"skipped (already fetched): {', '.join(result.skipped)}")
    verb = "would store" if result.dry_run else "stored"
    lines.append(f"{verb} {len(result.checkpoints)} backfilled checkpoint(s)")
    return "\n".join(lines)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start", type=parse_month, default=DEFAULT_START,
                        help=f"first checkpoint month, YYYY-MM (default {DEFAULT_START:%Y-%m})")
    parser.add_argument("--end", type=parse_month, default=DEFAULT_END,
                        help=f"last checkpoint month, YYYY-MM (default {DEFAULT_END:%Y-%m})")
    parser.add_argument("--symbols", nargs="+", default=list(CORE_SYMBOLS), metavar="SYM",
                        help="assets to fetch as <SYM>USDT monthly klines; BTC ETH SOL are always "
                             "fetched, USDT and stablecoins are pegged at 1.0")
    parser.add_argument("--dry-run", action="store_true", help="print the checkpoints, write nothing")


def backfill_command(database: Database, args: argparse.Namespace) -> int:
    try:
        result = run_backfill(database, start=args.start, end=args.end, symbols=args.symbols,
                              dry_run=args.dry_run)
    except BackfillError as exc:
        print(f"backfill failed: {exc}", file=sys.stderr)
        return 1
    print(render(result))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m services.backfill",
        description="Rebuild monthly history checkpoints from public Binance klines (P0.8).",
    )
    parser.add_argument("--db", default=None, help="SQLite file (default: db_path from config.yaml)")
    add_arguments(parser)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.db:
        db_path = Path(args.db)
    else:
        from config import load_config
        db_path = load_config(None).db_path
    return backfill_command(Database(db_path), args)


if __name__ == "__main__":
    sys.exit(main())
