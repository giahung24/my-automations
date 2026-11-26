#!/usr/bin/env python3
"""Multi-source crypto portfolio tracker - Binance (read-only) + on-chain wallets.

    python main.py show                      formatted table
    python main.py show --json               machine-readable JSON for bots/agents
    python main.py record                    silent snapshot into SQLite (for cron)
    python main.py chart --type all          write PNG charts into ./charts
    python main.py history                   past snapshots as a table
    python main.py backfill                  rebuild 60 monthly checkpoints (2021-11..2026-10)
    python main.py pnl                       real PnL 7d/30d, net of deposits/withdrawals
    python main.py pnl --sync --json         sync Binance deposit/withdraw/fiat/P2P first
    python main.py serve --port 8080         mobile web dashboard (FastAPI + PWA)

Add `--min-value 0` to any command to disable the $1.00 dust filter.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import sys

from config import load_config
from models import BINANCE_EARN, BINANCE_FUNDING, BINANCE_SPOT, MANUAL, ONCHAIN, SOURCE_LABELS, SOURCES
from services.db import TRIGGER_CRON, Database
from services.portfolio import fetch_portfolio, to_dict

COLUMNS = [
    ("Asset", 10, "<"),
    ("Spot", 16, ">"),
    ("Earn", 16, ">"),
    ("Funding", 14, ">"),
    ("On-chain", 16, ">"),
    ("Manual", 14, ">"),
    ("Total Qty", 16, ">"),
    ("Price", 14, ">"),
    ("Value USD", 14, ">"),
    ("%", 7, ">"),
]
SOURCE_ORDER = [BINANCE_SPOT, BINANCE_EARN, BINANCE_FUNDING, ONCHAIN, MANUAL]


def _fmt_row(cells: list[str]) -> str:
    return "  ".join(f"{cell:{align}{width}}" for cell, (_, width, align) in zip(cells, COLUMNS))


def render_table(portfolio) -> str:
    """The portfolio as a fixed-width table: one line per asset, totals at the bottom."""
    from services.visualizer import money, qty

    width = sum(w for _, w, _ in COLUMNS) + 2 * (len(COLUMNS) - 1)
    heading = f"PORTFOLIO  {money(portfolio.total_usd)}"
    lines = [
        "",
        "  " + heading + portfolio.timestamp.rjust(max(width - len(heading), 1)),
        "  " + "─" * width,
        "  " + _fmt_row([name for name, _, _ in COLUMNS]),
        "  " + "─" * width,
    ]
    for row in portfolio.rows:
        lines.append("  " + _fmt_row([
            row.symbol[:10],
            *[qty(row.by_source[s]) if row.by_source.get(s) else "-" for s in SOURCE_ORDER],
            qty(row.quantity),
            money(row.price_usd, 4) if row.price_usd else "n/a",
            money(row.value_usd),
            f"{row.pct:.2f}%",
        ]))
    if not portfolio.rows:
        lines.append("  (no holdings above the dust filter)")

    lines += ["  " + "─" * width]
    totals = "   ".join(
        f"{SOURCE_LABELS[s]} {money(portfolio.totals.get(s, 0.0), 0)}"
        for s in SOURCES if portfolio.totals.get(s, 0.0) > 0
    )
    if totals:
        lines.append("  " + totals)
    lines.append(f"  TOTAL {money(portfolio.total_usd)}")
    if portfolio.dust_assets:
        lines.append(
            f"  ({portfolio.dust_assets} dust assets hidden, {money(portfolio.dust_value_usd)} "
            "- included in the total; use --min-value 0 to show them)"
        )
    return "\n".join(lines) + "\n"


def render_history(snapshots) -> str:
    from services.visualizer import money, sparkline

    if not snapshots:
        return "\n  No snapshots recorded yet. Run:  python main.py record\n"
    header = (f"  {'Timestamp':<22}{'Spot':>14}{'Earn':>14}{'Funding':>12}{'On-chain':>14}{'Manual':>12}"
              f"{'Total':>16}{'Change':>14}")
    lines = ["", header, "  " + "─" * (len(header) - 2)]
    previous = None
    for snapshot in snapshots:
        change = "-" if previous is None else f"{snapshot.total_portfolio_usd - previous:+,.2f}"
        lines.append(
            f"  {snapshot.dt:%Y-%m-%d %H:%M}      "
            f"{money(snapshot.binance_spot_usd, 0):>14}{money(snapshot.binance_earn_usd, 0):>14}"
            f"{money(snapshot.binance_funding_usd, 0):>12}{money(snapshot.onchain_usd, 0):>14}"
            f"{money(getattr(snapshot, 'manual_usd', 0.0), 0):>12}"
            f"{money(snapshot.total_portfolio_usd):>16}{change:>14}"
            + ("  (backfilled)" if snapshot.is_backfilled else "")
        )
        previous = snapshot.total_portfolio_usd
    if len(snapshots) > 1:
        lines += ["", f"  {sparkline([s.total_portfolio_usd for s in snapshots])}"]
    return "\n".join(lines) + "\n"


def warn_errors(portfolio, stream=None) -> None:
    """Partial failures go to stderr so `show` stays pipeable and cron mails the warnings."""
    for error in portfolio.errors:
        print(f"  warning: {error}", file=stream or sys.stderr)


def check_sources(config, stream=None) -> None:
    """A run with nothing configured is a setup mistake, not an empty portfolio.

    Invalid `manual_assets` entries are skipped, not fatal; each one is reported on stderr.
    """
    for error in getattr(config, "manual_errors", []):
        print(f"  warning: config: {error}", file=stream or sys.stderr)
    if not config.binance_enabled and not config.has_wallets and not getattr(config, "has_manual_assets", False):
        example = "config.example.yaml"
        raise SystemExit(
            "No sources configured: set Binance API keys, wallet addresses and/or manual_assets.\n"
            f"  cp {example} config.yaml   (or export BINANCE_API_KEY / BINANCE_API_SECRET)"
        )


# -- commands ------------------------------------------------------------------
def cmd_show(args, config) -> int:
    check_sources(config)
    portfolio = fetch_portfolio(config, args.min_value)
    if args.json:
        print(json.dumps(to_dict(portfolio), indent=2, default=float))
        return 0 if portfolio.ok else 2
    print(render_table(portfolio))
    if args.text_charts:
        from services.visualizer import text_report
        snapshots = Database(config.db_path).history(limit=args.limit) if args.limit else []
        print(text_report(portfolio, snapshots))
    warn_errors(portfolio)
    return 0 if portfolio.ok else 2


def cmd_record(args, config) -> int:
    """Fetch and commit a snapshot. Silent on success - built to live in a crontab."""
    check_sources(config)
    portfolio = fetch_portfolio(config, min_value=0.0)  # history keeps everything, dust included
    if not portfolio.holdings and portfolio.errors:
        warn_errors(portfolio)
        print("refusing to record an empty snapshot: every source failed", file=sys.stderr)
        return 1
    database = Database(config.db_path)
    snapshot_id = database.record(portfolio, trigger=TRIGGER_CRON)
    warn_errors(portfolio)  # partial failures are recorded, but still reported on stderr
    # Manual-asset quantity changes (M6.3), then deposits/withdrawals (Epic F2): after the commit,
    # and their errors never fail the snapshot.
    from services.cash_flows import sync_after_record
    from services.manual import after_snapshot
    after_snapshot(config, database, snapshot_id)
    sync_after_record(config, database)
    if args.verbose:
        from services.visualizer import money
        print(f"snapshot #{snapshot_id} recorded: {money(portfolio.total_usd)} at {portfolio.timestamp}")
    return 0 if portfolio.ok else 2


def cmd_chart(args, config) -> int:
    from services.visualizer import generate, text_report

    database = Database(config.db_path)
    snapshots = database.history(days=args.days)
    output_dir = config.resolve(args.output) if args.output else config.charts_dir

    if args.from_db:
        portfolio = _portfolio_from_db(database, args.min_value)
        if portfolio is None:
            print("no snapshots in the database yet - run `python main.py record` first", file=sys.stderr)
            return 1
    else:
        check_sources(config)
        portfolio = fetch_portfolio(config, args.min_value)
        warn_errors(portfolio)

    if args.type == "text":
        print(text_report(portfolio, snapshots))
        return 0

    written = generate(portfolio, snapshots, output_dir, kinds=(args.type,))
    for path in written:
        print(f"wrote {path}")
    if args.text:
        print(text_report(portfolio, snapshots))
    return 0


def cmd_history(args, config) -> int:
    snapshots = Database(config.db_path).history(limit=args.limit, days=args.days)
    if args.json:
        print(json.dumps([vars(s) for s in snapshots], indent=2, default=str))
    else:
        print(render_history(snapshots))
    return 0


def cmd_backfill(args, config) -> int:
    """Rebuild monthly history checkpoints from public Binance klines (P0.8)."""
    from services.backfill import backfill_command

    return backfill_command(Database(config.db_path), args)


def render_pnl(report: dict) -> str:
    from services.visualizer import money

    lines = [""]
    if not report["flow_adjusted"]:
        lines.append("  deposits/withdrawals not tracked yet: PnL below is the raw NAV change "
                     "(run `python main.py pnl --sync`)")
    elif report["flows_stale"]:
        lines.append(f"  flows synced up to {report['flows_synced_until']} only")
    header = f"  {'Window':<8}{'NAV change':>16}{'Net flow':>16}{'PnL':>16}{'Return':>10}"
    lines += [header, "  " + "─" * (len(header) - 2)]
    for name, window in report["windows"].items():
        if not window.get("available"):
            lines.append(f"  {name:<8}  n/a ({window.get('reason')})")
            continue
        ret = "n/a" if window["return_pct"] is None else f"{window['return_pct']:+.2f}%"
        lines.append(
            f"  {name:<8}{money(window['nav_change_usd']):>16}{money(window['net_flow_usd']):>16}"
            f"{money(window['pnl_usd']):>16}{ret:>10}"
            + ("" if window["complete"] else "  (history shorter than the window)")
        )
    return "\n".join(lines) + "\n"


def cmd_pnl(args, config) -> int:
    """Real PnL over 7d / 30d: the NAV change minus net external deposits and withdrawals."""
    from services.analytics.pnl import pnl_report
    from services.cash_flows import sync

    database = Database(config.db_path)
    if args.sync:
        report = sync(config, database)
        if report.skipped:
            print(f"cash-flow sync skipped: {report.skipped}", file=sys.stderr)
        if report.pruned:
            print(f"cash-flow sync: pruned {report.pruned} Earn reward rows older than "
                  f"{config.cash_flow_settings.earn_backfill_days} days", file=sys.stderr)
        for error in report.errors:
            print(f"  warning: {error}", file=sys.stderr)
    try:
        payload = pnl_report(database, windows=args.windows)
    except ValueError as exc:
        print(f"pnl: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render_pnl(payload))
    return 0


def _portfolio_from_db(database, min_value: float):
    """Rebuild the latest portfolio from stored rows, so charts work with no network."""
    from services.portfolio import portfolio_from_db

    return portfolio_from_db(database, min_value)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def cmd_serve(args, config) -> int:
    """Run the mobile web dashboard: FastAPI JSON API + the PWA in ./static."""
    try:
        import uvicorn
    except ImportError:
        print("the web dashboard needs fastapi and uvicorn: pip install -r requirements.txt",
              file=sys.stderr)
        return 1
    from web_api import auth_token, create_app

    if not _is_loopback(args.host) and not auth_token(config):
        print(
            f"error: refusing to serve on {args.host} without an access token - anyone who can reach\n"
            "  that address could read your balances and change your settings. Set\n"
            "  PORTFOLIO_WEB_TOKEN (or settings.web_token in config.yaml) to a long random value,\n"
            "  e.g. `python -c 'import secrets; print(secrets.token_urlsafe(32))'`, then type it on\n"
            "  the app's unlock screen once per device. Or keep the default 127.0.0.1 and put\n"
            "  `tailscale serve` (or another authenticating proxy) in front.",
            file=sys.stderr,
        )
        return 1
    app = create_app(config_path=args.config, min_value=args.min_value_override)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level,
                proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    return 0


# -- CLI -----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Multi-source crypto portfolio tracker (Binance read-only + on-chain wallets).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", help="path to config.yaml (default: ./config.yaml)")
    parser.add_argument("--min-value", type=float, default=None,
                        help="dust filter in USD; assets worth less are hidden (default: 1.0)")

    # Repeated on every subcommand so the flags work on either side of it. SUPPRESS means
    # "only set when actually typed", so a flag given before the subcommand is not clobbered.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--min-value", type=float, default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="command", required=True)

    show = subparsers.add_parser("show", parents=[common], help="print the portfolio as a table")
    show.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    show.add_argument("--text-charts", action="store_true", help="append the ASCII chart report")
    show.add_argument("--limit", type=int, default=60, help="snapshots to include in the ASCII history")
    show.set_defaults(func=cmd_show)

    record = subparsers.add_parser("record", parents=[common], help="commit a snapshot to SQLite (for cron)")
    record.add_argument("-v", "--verbose", action="store_true", help="print the recorded total")
    record.set_defaults(func=cmd_record)

    chart = subparsers.add_parser("chart", parents=[common], help="render PNG charts")
    chart.add_argument("--type", choices=["all", "history", "allocation", "sources", "text"],
                       default="all")
    chart.add_argument("--output", help="output directory (default: ./charts)")
    chart.add_argument("--days", type=int, default=None, help="limit the history to the last N days")
    chart.add_argument("--from-db", action="store_true",
                       help="use the last stored snapshot instead of fetching live balances")
    chart.add_argument("--text", action="store_true", help="also print the ASCII chart report")
    chart.set_defaults(func=cmd_chart)

    history = subparsers.add_parser("history", parents=[common], help="list recorded snapshots")
    history.add_argument("--limit", type=int, default=30)
    history.add_argument("--days", type=int, default=None)
    history.add_argument("--json", action="store_true")
    history.set_defaults(func=cmd_history)

    from services.backfill import add_arguments as add_backfill_arguments

    backfill = subparsers.add_parser(
        "backfill", parents=[common],
        help="rebuild monthly history from Binance klines (current holdings, past prices)")
    add_backfill_arguments(backfill)
    backfill.set_defaults(func=cmd_backfill)

    pnl = subparsers.add_parser("pnl", parents=[common],
                                help="real PnL over 7d/30d, net of deposits and withdrawals")
    pnl.add_argument("--sync", action="store_true",
                     help="pull Binance deposit/withdraw/fiat/P2P history first")
    pnl.add_argument("--windows", type=int, nargs="+", default=[7, 30], metavar="DAYS",
                     help="window lengths in days (default: 7 30)")
    pnl.add_argument("--json", action="store_true")
    pnl.set_defaults(func=cmd_pnl)

    serve = subparsers.add_parser("serve", parents=[common], help="run the mobile web dashboard")
    serve.add_argument("--host", default="127.0.0.1",
                       help="interface to bind (default: 127.0.0.1; anything else needs an access token)")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--log-level", default="info",
                       choices=["critical", "error", "warning", "info", "debug"])
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    args.min_value_override = args.min_value  # only what was typed; `serve` re-reads config
    if args.min_value is not None:
        config.settings["min_value"] = args.min_value
    if getattr(args, "min_value", None) is None:
        args.min_value = config.min_value
    try:
        return args.func(args, config)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
