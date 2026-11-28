"""FastAPI backend for the mobile dashboard, plus the PWA it serves from ./static.

    python main.py serve --port 8080                 (127.0.0.1 only)
    python main.py serve --host 0.0.0.0 --port 8080  (reachable from your phone on the LAN)
    python -m uvicorn web_api:app --port 8080

The API re-reads config.yaml on every request, so a save from the settings screen takes
effect on the next refresh without a restart. Reads are cheap: `GET /api/portfolio` serves
the last live fetch or the newest SQLite snapshot and never touches the network; only
`POST /api/portfolio/refresh` and the two `test-*` endpoints call Binance or an RPC.

Access control: when PORTFOLIO_WEB_TOKEN (or `settings.web_token` in config.yaml) is set,
every /api route except /api/auth needs it - as an `Authorization: Bearer` header, an
`X-Portfolio-Token` header, or the `portfolio_session` cookie the app's unlock screen gets from
`POST /api/auth`. The cookie is a signed, expiring session, never the token itself, and wrong
credentials are rate limited per client. Without a token the API is open to whoever can reach the port,
which is why `serve` binds 127.0.0.1 unless told otherwise.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi import Path as FastPath
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from clients.binance import BinanceClient, BinanceError
from clients.onchain import EVM_CHAINS, OnChainClient, RpcError
from config import (
    MANUAL_KINDS,
    ROOT,
    Config,
    ManualAsset,
    ManualHolding,
    _normalize_wallets,
    keep_unmanaged_keys,
    parse_allocation,
    parse_manual_assets,
    resolve_linked_wallet,
    active_env_overrides,
    load_config,
    read_raw_config,
    write_raw_config,
)
from models import (
    BINANCE_EARN,
    BINANCE_FUNDING,
    BINANCE_SPOT,
    MANUAL,
    ONCHAIN,
    SELF_CUSTODY_SOURCES,
    SOURCE_LABELS,
    SOURCES,
    Holding,
    Portfolio,
)
from services.activity import ActivityError, activity_report, confirm_flow, reset_flow, transfer_candidates
from services.analytics.allocation import allocation_report
from services.analytics.attribution import (
    MAX_WINDOW_HOURS,
    attribution_report,
    coverage_of,
    reference_snapshot,
)
from services.analytics.cost_basis import cost_basis_report
from services.analytics.earn import earn_report
from services.analytics.pnl import add_net_deposited, pnl_report
from services.analytics.quant import quant_report
from services.db import TRIGGER_REFRESH, Database, parse_timestamp
from services.manual import after_snapshot, last_changes
from services.peg import peg_warnings
from services.portfolio import fetch_portfolio, portfolio_from_db, source_wallets, tab_dust, to_dict
from services.resample import TooManyBuckets, resample

STATIC_DIR = ROOT / "static"
BINANCE_SOURCES = (BINANCE_SPOT, BINANCE_EARN, BINANCE_FUNDING)
SESSION_COOKIE = "portfolio_session"
SESSION_MAX_AGE = 90 * 86400
LEGACY_TOKEN_COOKIE = "portfolio_token"  # older builds stored the raw token here; cleared on sight
AUTH_FAILURE_LIMIT = 20       # wrong credentials per client ...
AUTH_FAILURE_WINDOW = 300.0   # ... within this many seconds, then 429 until the window slides
TEST_TIMEOUT = 15  # seconds; a settings-screen check should fail fast, not hang the UI
MASK = "****"
# A staking balance untouched this long has probably grown with rewards (M6.4 AC3).
MANUAL_REVIEW_DAYS = 90
SYMBOL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,19}$"

# Display metadata for every chain the on-chain client can read, in settings-screen order.
CHAIN_META = {
    "ethereum": {"name": "Ethereum", "short": "ETH"},
    "bsc": {"name": "BNB Chain", "short": "BSC"},
    "arbitrum": {"name": "Arbitrum", "short": "ARB"},
    "base": {"name": "Base", "short": "BASE"},
    "polygon": {"name": "Polygon", "short": "POL"},
    "solana": {"name": "Solana", "short": "SOL"},
}

EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")  # base58, 32-byte pubkey

# Binance error codes worth translating for someone staring at a settings form.
BINANCE_HINTS = {
    -1021: "Your computer's clock is out of sync with Binance. Sync the system time and retry.",
    -1022: "Signature rejected: the API secret does not match this key.",
    -2008: "Binance does not recognise this API key.",
    -2014: "The API key format is invalid. Copy it again from Binance > API Management.",
    -2015: "Key rejected: it is wrong, deleted, restricted to other IPs, or lacks 'Enable Reading'.",
}


# -- request bodies -----------------------------------------------------------
class BinanceSettingsIn(BaseModel):
    enabled: bool = True
    # None (or the masked value echoed back) keeps what is stored; "" clears it.
    api_key: str | None = Field(default=None, max_length=256)
    api_secret: str | None = Field(default=None, max_length=256)


class WalletIn(BaseModel):
    name: str = Field(default="", max_length=40)
    address: str = Field(min_length=1, max_length=100)
    chains: list[str] = Field(default_factory=list)


class ManualHoldingIn(BaseModel):
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    quantity: float = Field(ge=0, allow_inf_nan=False)
    price_id: str = Field(default="", max_length=120)
    price_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    liquid: bool = True


class ManualAssetIn(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    kind: Literal["cold_wallet", "staking", "other"] = "cold_wallet"
    # label of a configured wallet that is the same device (shown together); None/"" = standalone.
    # Left out of the request entirely = keep the stored link (see `_manual_section`).
    linked_wallet: str | None = Field(default=None, max_length=40)
    holdings: list[ManualHoldingIn] = Field(default_factory=list, max_length=50)


class SettingsIn(BaseModel):
    binance: BinanceSettingsIn | None = None
    wallets: list[WalletIn] | None = Field(default=None, max_length=50)
    manual_assets: list[ManualAssetIn] | None = Field(default=None, max_length=50)


class AllocationToggleIn(BaseModel):
    enabled: bool


class CashFlowPatchIn(BaseModel):
    is_internal_transfer: bool = False
    # True: forget the user's decision (the "Undo" of the Activity tab), back to automatic
    reset: bool = False
    matched_flow_id: int | None = Field(default=None, ge=1)
    note: str | None = Field(default=None, max_length=200)


class NoteIn(BaseModel):
    body: str = Field(default="", max_length=2000)   # empty deletes the note


class TestBinanceIn(BaseModel):
    api_key: str | None = Field(default=None, max_length=256)
    api_secret: str | None = Field(default=None, max_length=256)


class TestWalletIn(BaseModel):
    address: str = Field(min_length=1, max_length=100)
    chains: list[str] = Field(default_factory=list)


class AuthIn(BaseModel):
    token: str = Field(min_length=1, max_length=512)


# -- helpers ------------------------------------------------------------------
def auth_token(config: Config) -> str:
    """The access token guarding the API, or "" when the API is open."""
    return (os.environ.get("PORTFOLIO_WEB_TOKEN") or str(config.settings.get("web_token") or "")).strip()


def sign_session(token: str, *, now: float | None = None, max_age: int = SESSION_MAX_AGE) -> str:
    """A session cookie value: `v1.<expiry>.<HMAC-SHA256(token, expiry)>`.

    Stateless, so sessions survive a restart; keyed by the access token, so changing the token
    signs every device out. A leaked cookie expires and does not hand over the token itself
    (short of brute-forcing a weak one offline - use a long random token).
    """
    expires = int((time.time() if now is None else now) + max_age)
    return f"v1.{expires}.{_session_mac(token, expires)}"


def session_valid(value: str, token: str, *, now: float | None = None) -> bool:
    version, _, rest = (value or "").partition(".")
    expires, _, mac = rest.partition(".")
    if version != "v1" or not expires.isascii() or not expires.isdigit() or not mac.isascii():
        return False
    if int(expires) <= (time.time() if now is None else now):
        return False
    return hmac.compare_digest(mac, _session_mac(token, int(expires)))


def _session_mac(token: str, expires: int) -> str:
    return hmac.new(token.encode("utf-8"), f"portfolio-session:{expires}".encode("ascii"),
                    hashlib.sha256).hexdigest()


class FailureLimiter:
    """Counts wrong credentials per client in a sliding window; in memory, bounded."""

    def __init__(self, limit: int = AUTH_FAILURE_LIMIT, window: float = AUTH_FAILURE_WINDOW,
                 max_clients: int = 4096):
        self.limit, self.window, self.max_clients = limit, window, max_clients
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, client: str) -> int:
        """Seconds this client must wait before presenting credentials again; 0 = go ahead."""
        now = time.monotonic()
        with self._lock:
            recent = self._recent(client, now)
            if len(recent) < self.limit:
                return 0
            return max(1, int(recent[0] + self.window - now) + 1)

    def record(self, client: str) -> None:
        now = time.monotonic()
        with self._lock:
            recent = self._recent(client, now)
            if not recent and len(self._failures) >= self.max_clients:
                self._failures.clear()  # flood of distinct clients: forget rather than grow
            recent.append(now)
            self._failures[client] = recent

    def _recent(self, client: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(client, ()) if t > now - self.window]
        if recent:
            self._failures[client] = recent
        else:
            self._failures.pop(client, None)
        return recent


def mask_secret(value: str) -> str:
    """`****wxyz` - enough to recognise which secret is stored, useless to anyone else."""
    value = value or ""
    if not value:
        return ""
    return MASK + value[-4:] if len(value) > 8 else MASK


def mask_key(value: str) -> str:
    value = value or ""
    if not value:
        return ""
    return f"{value[:4]}{MASK}{value[-4:]}" if len(value) > 12 else MASK


def _is_masked(value: str) -> bool:
    return MASK in value


def address_kind(address: str) -> str | None:
    """'evm', 'solana' or None. EVM wins ties: a 0x address is never valid base58 anyway."""
    address = (address or "").strip()
    if EVM_ADDRESS.match(address):
        return "evm"
    if SOLANA_ADDRESS.match(address):
        return "solana"
    return None


def resolve_chains(kind: str, requested: list[str]) -> list[str]:
    """Validate a chain selection against the address type. Empty means "every chain"."""
    requested = [str(c).strip().lower() for c in requested if str(c).strip()]
    unknown = [c for c in requested if c not in CHAIN_META]
    if unknown:
        raise HTTPException(422, f"Unknown chain: {', '.join(unknown)}.")
    if kind == "solana":
        wrong = [c for c in requested if c != "solana"]
        if wrong:
            raise HTTPException(422, "A Solana address can only be read on Solana.")
        return ["solana"]
    if "solana" in requested:
        raise HTTPException(422, "A 0x address cannot be read on Solana. Untick SOL.")
    ordered = [c for c in EVM_CHAINS if c in requested]
    return ordered or list(EVM_CHAINS)


def _binance_message(exc: Exception) -> str:
    text = str(exc)
    match = re.search(r'"code"\s*:\s*(-?\d+)', text)
    if match and int(match.group(1)) in BINANCE_HINTS:
        return BINANCE_HINTS[int(match.group(1))]
    if "HTTP 451" in text:
        return "Binance refuses connections from this server's location (HTTP 451)."
    if "HTTP 429" in text or "HTTP 418" in text:
        return "Binance is rate-limiting this IP. Wait a minute and try again."
    return f"Binance rejected the request: {text[:160]}"


def change_since(database: Database, current_total: float, current_ts: str,
                 hours: int = 24) -> dict | None:
    """Value change against the snapshot nearest `hours` ago.

    The reference is the snapshot closest to the cutoff, before or after it (the same pick as
    the attribution window, so the chip and the Analytics tab agree). The real `hours` covered
    and a `coverage` verdict are reported, so the UI can say "6h" instead of pretending it is
    a 24h figure.
    """
    now = parse_timestamp(current_ts)
    reference = reference_snapshot(database, now - timedelta(hours=hours), now)
    if reference is None or reference.dt >= now:
        return None
    delta = current_total - reference.total_portfolio_usd
    span = (now - reference.dt).total_seconds() / 3600
    return {
        "usd": round(delta, 2),
        "pct": round(delta / reference.total_portfolio_usd * 100, 4)
        if reference.total_portfolio_usd > 0 else None,
        "since": reference.timestamp,
        "hours": round(span, 2),
        "requested_hours": hours,
        "coverage": coverage_of(span, hours),
        "reference_usd": round(reference.total_portfolio_usd, 2),
        "has_attribution": True,  # GET /api/portfolio/attribution can explain this move
    }


def portfolio_payload(portfolio: Portfolio, *, origin: str, database: Database,
                      config: Config, snapshot_id: int | None = None) -> dict:
    """`show --json`'s schema plus what the dashboard needs to filter by source group."""
    data = to_dict(portfolio)
    values: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    manual_labels: dict[str, list[str]] = defaultdict(list)
    for holding in portfolio.holdings:
        if holding.quantity > 0:
            values[holding.symbol][holding.source] += holding.value_usd
            if holding.source == MANUAL and holding.detail and holding.detail not in manual_labels[holding.symbol]:
                manual_labels[holding.symbol].append(holding.detail)

    for asset in data["assets"]:
        by_value = values.get(asset["symbol"], {})
        by_qty = asset["by_source"]
        asset["value_by_source"] = {source: round(v, 2) for source, v in by_value.items()}
        asset["binance_quantity"] = sum(by_qty.get(s, 0.0) for s in BINANCE_SOURCES)
        asset["binance_value_usd"] = round(sum(by_value.get(s, 0.0) for s in BINANCE_SOURCES), 2)
        asset["onchain_quantity"] = by_qty.get(ONCHAIN, 0.0)
        asset["onchain_value_usd"] = round(by_value.get(ONCHAIN, 0.0), 2)
        asset["manual_quantity"] = by_qty.get(MANUAL, 0.0)
        asset["manual_value_usd"] = round(by_value.get(MANUAL, 0.0), 2)
        asset["manual_labels"] = manual_labels.get(asset["symbol"], [])
        # the Personal tab's Coins view: on-chain and manual read as one self-custody balance
        asset["personal_quantity"] = sum(by_qty.get(s, 0.0) for s in SELF_CUSTODY_SOURCES)
        asset["personal_value_usd"] = round(sum(by_value.get(s, 0.0) for s in SELF_CUSTODY_SOURCES), 2)
        asset.setdefault("locations_by_source", {})

    totals = portfolio.totals
    cex = sum(totals.get(s, 0.0) for s in BINANCE_SOURCES)
    self_custody = sum(totals.get(s, 0.0) for s in SELF_CUSTODY_SOURCES)
    custody_total = cex + self_custody
    data.update(
        origin=origin,
        snapshot_id=snapshot_id,
        configured=config.has_sources,
        min_value=config.min_value,
        # one total per asset-list tab; Personal = every self-custody balance (on-chain + manual)
        groups={
            "all": round(portfolio.total_usd, 2),
            "binance": round(cex, 2),
            "personal": round(self_custody, 2),
        },
        # what each tab's Coins view folds into its "N balances under $1" line
        tab_dust={
            "all": data["dust"],
            "binance": tab_dust(portfolio.holdings, BINANCE_SOURCES, config.min_value),
            "personal": tab_dust(portfolio.holdings, SELF_CUSTODY_SOURCES, config.min_value),
        },
        # the Wallets side of the Coins | Wallets switch: per Binance account / per physical
        # wallet, `pct` of that tab's total, small rows folded into `dust`. Display only: PnL,
        # TWR and snapshot_sources keep on-chain and manual apart.
        wallets=source_wallets(portfolio.holdings, config.wallet_labels, config.manual, config.min_value),
        # Exchange vs your own keys; a manual asset (cold wallet, validator) is self-custody.
        custody={
            "cex_usd": round(cex, 2),
            "self_custody_usd": round(self_custody, 2),
            "cex_pct": round(cex / custody_total * 100, 4) if custody_total > 0 else None,
            "self_custody_pct": round(self_custody / custody_total * 100, 4) if custody_total > 0 else None,
        },
        has_manual=bool(config.manual) or totals.get(MANUAL, 0.0) > 0,
        config_errors=config.manual_errors,
        source_labels=SOURCE_LABELS,
        change_24h=change_since(database, portfolio.total_usd, portfolio.timestamp)
        if origin != "empty" else None,
        peg=peg_warnings(portfolio.peg_checks),  # N3: empty while every stablecoin held is on peg
        # N2.4: snapshots of the last 24h in which a source failed (the holes of the chart)
        gaps_24h=database.failed_snapshots_since(portfolio.timestamp, hours=24) if origin != "empty" else 0,
    )
    return data


def empty_payload(config: Config, database: Database) -> dict:
    portfolio = Portfolio(timestamp="", totals={source: 0.0 for source in SOURCES})
    return portfolio_payload(portfolio, origin="empty", database=database, config=config)


# -- app ----------------------------------------------------------------------
def create_app(
    config_path: str | Path | None = None,
    *,
    static_dir: str | Path = STATIC_DIR,
    min_value: float | None = None,
    fetcher: Callable[..., Portfolio] = fetch_portfolio,
    http_session_factory: Callable[[], requests.Session] | None = None,
) -> FastAPI:
    """Build the app. Every argument exists so tests can point it at temp files and fakes.

    `fetcher(config, min_value)` produces a live Portfolio (default: hit every source);
    `http_session_factory` supplies the requests session the connection tests use.
    """
    static_dir = Path(static_dir)
    app = FastAPI(
        title="Crypto Portfolio",
        version="1.0.0",
        description="Read-only Binance + on-chain portfolio tracker.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.config_path = Path(config_path).expanduser() if config_path else None
    app.state.min_value = min_value
    app.state.fetcher = fetcher
    app.state.http_session_factory = http_session_factory
    app.state.refresh_lock = threading.Lock()
    app.state.settings_lock = threading.Lock()
    app.state.auth_failures = FailureLimiter()
    app.state.live = None  # (Portfolio, snapshot_id) of the last successful live refresh

    def config() -> Config:
        try:
            loaded = load_config(app.state.config_path, missing_ok=True)
        except ValueError as exc:  # malformed YAML is a server-side problem the user must fix
            raise HTTPException(500, f"config.yaml is invalid: {exc}") from exc
        if app.state.min_value is not None:
            loaded.settings["min_value"] = app.state.min_value
        return loaded

    def config_file() -> Path:
        return app.state.config_path or (ROOT / "config.yaml")

    def session() -> requests.Session | None:
        factory = app.state.http_session_factory
        return factory() if factory else None

    # -- access control ----------------------------------------------------------
    def _header_token(request: Request) -> str:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return request.headers.get("x-portfolio-token") or ""

    def _same(presented: str, expected: str) -> bool:
        return secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))

    def _client(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def _too_many(retry_after: int) -> JSONResponse:
        return JSONResponse({"detail": "Too many wrong attempts. Try again in a few minutes."},
                            status_code=429, headers={"Retry-After": str(retry_after)})

    def _drop_cookies(response, request: Request) -> None:
        for name in (SESSION_COOKIE, LEGACY_TOKEN_COOKIE):
            if name in request.cookies:
                response.delete_cookie(name, path="/")

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path != "/api/auth":
            try:
                expected = auth_token(config())
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            if expected:
                header = _header_token(request)
                cookie = request.cookies.get(SESSION_COOKIE, "")
                if header or cookie:
                    client = _client(request)
                    wait = app.state.auth_failures.retry_after(client)
                    if wait:
                        return _too_many(wait)
                    if not (_same(header, expected) if header else session_valid(cookie, expected)):
                        app.state.auth_failures.record(client)
                        header = cookie = ""
                if not (header or cookie):
                    response = JSONResponse({"detail": "Locked: enter the access token."}, status_code=401)
                    _drop_cookies(response, request)  # a dead session must not be re-sent (and re-counted)
                    return response
        return await call_next(request)

    @app.post("/api/auth")
    def authenticate(body: AuthIn, request: Request):
        """Trade the access token for an HttpOnly session cookie (the unlock screen calls this)."""
        expected = auth_token(config())
        if not expected:
            return JSONResponse({"ok": True, "required": False})
        client = _client(request)
        wait = app.state.auth_failures.retry_after(client)
        if wait:
            return _too_many(wait)
        if not _same(body.token.strip(), expected):
            app.state.auth_failures.record(client)
            raise HTTPException(401, "Wrong access token.")
        response = JSONResponse({"ok": True, "required": True})
        response.set_cookie(
            SESSION_COOKIE, sign_session(expected), max_age=SESSION_MAX_AGE, httponly=True,
            samesite="strict", secure=request.url.scheme == "https", path="/",
        )
        if LEGACY_TOKEN_COOKIE in request.cookies:
            response.delete_cookie(LEGACY_TOKEN_COOKIE, path="/")
        return response

    @app.get("/api/health")
    def health():
        return {"ok": True}

    # -- portfolio -----------------------------------------------------------------
    @app.get("/api/portfolio")
    def get_portfolio():
        """Latest known portfolio, no network: the last live refresh, else the newest snapshot."""
        cfg = config()
        database = Database(cfg.db_path)
        latest = database.latest()
        live = app.state.live
        if live and latest is not None and live[1] == latest.id:
            portfolio, snapshot_id = live
            return portfolio_payload(portfolio, origin="live", database=database, config=cfg,
                                     snapshot_id=snapshot_id)
        if latest is not None:
            portfolio = portfolio_from_db(database, cfg.min_value)
            return portfolio_payload(portfolio, origin="snapshot", database=database, config=cfg,
                                     snapshot_id=latest.id)
        return empty_payload(cfg, database)

    @app.post("/api/portfolio/refresh")
    def refresh_portfolio():
        """Fetch every source now, store the snapshot, return the fresh portfolio."""
        cfg = config()
        if not cfg.has_sources:
            raise HTTPException(400, "Nothing to refresh yet: add Binance API keys, a wallet or a "
                                     "manual asset in Settings.")
        with app.state.refresh_lock:  # two taps on refresh must not double-record
            portfolio = app.state.fetcher(cfg, cfg.min_value)
            if not portfolio.holdings and portfolio.errors:
                raise HTTPException(502, {
                    "message": "Every source failed, so nothing was recorded.",
                    "errors": portfolio.errors,
                })
            database = Database(cfg.db_path)
            snapshot_id = database.record(portfolio, trigger=TRIGGER_REFRESH)
            after_snapshot(cfg, database, snapshot_id)  # manual quantity changes -> cash_flows (M6.3)
            app.state.live = (portfolio, snapshot_id)
        return portfolio_payload(portfolio, origin="live", database=database, config=cfg,
                                 snapshot_id=snapshot_id)

    @app.get("/api/portfolio/history")
    def portfolio_history(
        days: int = Query(30, ge=0, le=36500, description="Look-back window in days; 0 = everything"),
        interval: Literal["raw", "1h", "4h", "1d", "1w", "1mo"] = Query(
            "raw", description="raw = every snapshot; otherwise one point per bucket (see services/resample.py)"),
        tz_offset: int = Query(0, ge=-14 * 60, le=14 * 60,
                               description="Viewer's clock in minutes east of UTC; sets day/week/month edges"),
    ):
        """Snapshot totals oldest-first, ready for a time-series chart.

        `raw` keeps one point per second. A bucketed `interval` puts exactly one point in every
        bucket (empty ones interpolated and flagged `filled`), so a chart that spaces points by
        index, like Lightweight Charts, shows time proportionally on its X axis.
        """
        cfg = config()
        database = Database(cfg.db_path)
        snapshots = database.history(days=days or None)
        points: dict[int, dict] = {}
        for snapshot in snapshots:
            second = int(snapshot.dt.timestamp())
            binance = snapshot.binance_spot_usd + snapshot.binance_earn_usd + snapshot.binance_funding_usd
            points[second] = {  # same second twice: the later row wins
                "time": second,
                "timestamp": snapshot.timestamp,
                "total_usd": round(snapshot.total_portfolio_usd, 2),
                "binance_usd": round(binance, 2),
                "onchain_usd": round(snapshot.onchain_usd, 2),
                "manual_usd": round(snapshot.manual_usd, 2),
                "binance_spot_usd": round(snapshot.binance_spot_usd, 2),
                "binance_earn_usd": round(snapshot.binance_earn_usd, 2),
                "binance_funding_usd": round(snapshot.binance_funding_usd, 2),
                # monthly checkpoint rebuilt from past prices (services/backfill.py), not fetched
                "is_backfilled": snapshot.is_backfilled,
            }
        ordered = [points[key] for key in sorted(points)]
        if interval != "raw":
            try:
                ordered = resample(ordered, interval, tz_offset)
            except TooManyBuckets as error:
                raise HTTPException(422, str(error)) from error
            add_net_deposited(database, ordered)
            return {"days": days or None, "interval": interval, "count": len(ordered), "points": ordered}
        add_net_deposited(database, ordered)
        return {"days": days or None, "count": len(ordered), "points": ordered}

    @app.get("/api/portfolio/attribution")
    def portfolio_attribution(
        window: str = Query("24h", pattern=r"^\d{1,3}[hd]$",
                            description=f"Look-back window, e.g. 24h or 7d; at most {MAX_WINDOW_HOURS // 24}d"),
        group_by: Literal["symbol", "source", "symbol_source"] = Query("symbol"),
        method: Literal["chained", "endpoint"] = Query("chained"),
        top: int = Query(5, ge=1, le=20, description="Gainers and losers listed on each side"),
    ):
        """Why the net worth moved: price / quantity / interaction effect per coin, from SQLite only.

        Splits the NAV change over `window` (docs/SPEC.md §2.2) and lists the coins that pulled
        it up and down. Never touches the network.
        """
        cfg = config()
        database = Database(cfg.db_path)
        try:
            report = attribution_report(database, window=window, method=method, group_by=group_by, top=top)
        except ValueError as exc:  # 0h or over 365d: the regex cannot say it
            raise HTTPException(422, str(exc)) from exc
        if report.get("from") and report.get("to"):   # N9: what was written down during the period
            # a note is filed under the writer's local day, the window ends on a UTC instant: one day
            # of slack keeps "today's" note in a window that ends at the last snapshot
            until = (parse_timestamp(report["to"]) + timedelta(days=1)).strftime("%Y-%m-%d")
            report["notes"] = database.notes(report["from"][:10], until)
        return report

    @app.get("/api/portfolio/pnl")
    def portfolio_pnl():
        """Real PnL over 7d and 30d: NAV change minus net external deposits/withdrawals.

        Reads SQLite only (`cash_flows` is filled by `main.py record`), never the network.
        `flow_adjusted: false` means no deposit/withdrawal history was synced yet.
        """
        return pnl_report(Database(config().db_path))

    @app.get("/api/analytics/cost-basis")
    def analytics_cost_basis():
        """Average entry and unrealised PnL per coin, plus the latest buys and how they did since.

        Reads SQLite only (`trades` and `cash_flows`, filled by the collector). `tracked` is the
        share of a position the ledger explains; a coin below 1% has no figure.
        """
        cfg = config()
        return cost_basis_report(Database(cfg.db_path), cfg.stablecoins, cfg.min_value)

    @app.get("/api/analytics/earn")
    def analytics_earn():
        """Simple Earn: listed vs realised APR per asset (7d / 30d) and lifetime income by year.

        Reads SQLite only. A window the snapshots do not cover has `apr: null`, `sufficient: false`.
        """
        cfg = config()
        return earn_report(Database(cfg.db_path), cfg.stablecoins)

    @app.get("/api/analytics/quant")
    def analytics_quant(
        period: str = Query("all", pattern=r"^(?:\d{1,4}d|\d{1,2}y|all)$",
                            description="Look-back period: 7d, 30d, 90d, 1y or all"),
        include_backfilled: bool = Query(False, description="Also use the monthly backfilled checkpoints"),
    ):
        """TWR, Base-100 vs BTC/ETH, Max Drawdown + underwater series, annualised volatility.

        Built from the daily closes in SQLite (docs/SPEC.md §4.1-§4.4); never touches the network.
        Every metric comes with `n_obs` and `sufficient`.
        """
        try:
            return quant_report(Database(config().db_path), period=period,
                                include_backfilled=include_backfilled)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/analytics/allocation")
    def analytics_allocation(
        contribution: float | None = Query(
            None, gt=0, le=1e12, allow_inf_nan=False,
            description="New money to invest (USD): suggest BUY orders only, adding up to it"),
        source_bucket: str | None = Query(
            None, min_length=1, max_length=100,
            description="Partial / DCA funding: the bucket to take the money from"),
        funding_amount_usd: float | None = Query(
            None, gt=0, le=1e12, allow_inf_nan=False,
            description="Partial / DCA funding: USD moved from `source_bucket` to the "
                        "under-weight buckets (the portfolio value does not change)"),
        strategy: Literal["greedy", "proportional"] = Query(
            "greedy", description="Partial funding: deepest deficit first, or in proportion "
                                  "to each bucket's deficit"),
        include_dry_powder: bool = Query(
            False, description="Count the `allocation.dry_powder` holdings in the weights too"),
    ):
        """Target vs actual weight per bucket, drift, and suggested orders (docs/SPEC.md §4.5).

        Off until turned on (`allocation.enabled`, or POST /api/settings/allocation): then the
        answer is `{"enabled": false}`. Invalid targets -> 422 with one message per problem.
        `contribution` and partial funding (`source_bucket` + `funding_amount_usd`) exclude
        each other (422). Suggestions only - nothing is ever traded. Reads SQLite, never the
        network.
        """
        funding = source_bucket is not None or funding_amount_usd is not None
        if funding and contribution is not None:
            raise HTTPException(422, {"message": "Use either new money (contribution) or partial "
                                                 "funding (source_bucket + funding_amount_usd), not both."})
        if funding and (source_bucket is None or funding_amount_usd is None):
            raise HTTPException(422, {"message": "Partial funding needs both source_bucket and "
                                                 "funding_amount_usd."})
        cfg = config()
        status = allocation_status(cfg)
        if not status["enabled"]:
            return {**status, "available": False, "reason": "disabled", "buckets": [], "actions": []}
        if status["errors"]:
            raise HTTPException(422, {"message": "The allocation targets in config.yaml are invalid.",
                                      "errors": status["errors"]})
        try:
            report = allocation_report(Database(cfg.db_path), cfg.allocation_settings, cfg.manual,
                                       contribution=contribution, source_bucket=source_bucket,
                                       funding_amount=funding_amount_usd, strategy=strategy,
                                       include_dry_powder=include_dry_powder)
        except ValueError as exc:  # unknown source bucket and the like
            raise HTTPException(422, {"message": str(exc)}) from exc
        return {**report, "configured": status["configured"], "errors": []}

    def allocation_status(cfg: Config) -> dict:
        settings, errors = parse_allocation(cfg.allocation, cfg.settings.get("stablecoins", []))
        return {
            "enabled": settings.enabled,
            "configured": not settings.default_targets,
            "default_targets": settings.default_targets,
            "errors": errors,
            "targets": [{"name": b.name, "target_pct": b.target} for b in settings.buckets],
            "dry_powder_rules": len(settings.dry_powder),
        }

    @app.post("/api/settings/allocation")
    def toggle_allocation(body: AllocationToggleIn):
        """Turn the Allocation assistant on or off; every other `allocation` key is kept."""
        path = config_file()
        with app.state.settings_lock:
            try:
                raw = read_raw_config(path)
            except (ValueError, OSError) as exc:
                raise HTTPException(500, f"Cannot read {path.name}: {exc}") from exc
            section = raw.get("allocation") if isinstance(raw.get("allocation"), dict) else {}
            section["enabled"] = body.enabled
            raw["allocation"] = section
            try:
                write_raw_config(raw, path)
            except OSError as exc:
                raise HTTPException(500, f"Cannot write {path.name}: {exc}") from exc
        return {**allocation_status(config()), "saved": True}

    # -- activity (Epic M4) ---------------------------------------------------------
    @app.get("/api/cash-flows")
    def cash_flows_timeline(
        days: int = Query(90, ge=0, le=36500, description="Look-back window in days; 0 = everything"),
        source: Literal["all", "binance", "onchain", "manual"] = Query("all"),
        type: Literal["all", "deposit", "withdrawal", "transfer", "reward", "fee", "trade", "unexplained"] = Query("all"),
        asset: str = Query("", max_length=20, pattern=r"^[A-Za-z0-9._-]*$"),
        review: bool = Query(False, description="Only the items waiting for a confirmation"),
        cursor: str | None = Query(None, max_length=80, description="`next_cursor` of the previous page"),
        limit: int = Query(50, ge=1, le=200),
        tz_offset: int = Query(0, ge=-14 * 60, le=14 * 60,
                               description="Viewer's clock in minutes east of UTC; sets the Earn day edges"),
    ):
        """Deposits, withdrawals, internal transfers and Earn rewards, newest first.

        A paired transfer is one `kind=transfer` item; Earn rewards are one item per day. Reads
        SQLite only (`cash_flows`, filled by the collector's Binance sync), never the network.
        """
        cfg = config()
        try:
            return activity_report(Database(cfg.db_path), settings=cfg.cash_flow_settings, days=days,
                                   source=source, kind=type, asset=asset, review=review, cursor=cursor,
                                   limit=limit, tz_offset=tz_offset)
        except ActivityError as exc:
            raise HTTPException(exc.status, exc.message) from exc

    @app.put("/api/notes/{day}")
    def put_note(body: NoteIn, day: str = FastPath(pattern=r"^\d{4}-\d{2}-\d{2}$")):
        """Write the diary note of one day (N9); an empty body deletes it. Plain text only."""
        try:
            note = Database(config().db_path).set_note(day, body.body)
        except ValueError as exc:
            raise HTTPException(422, f"Not a calendar day: {day}") from exc
        return note or {"day": day, "body": ""}

    @app.get("/api/cash-flows/{flow_id}/candidates")
    def cash_flow_candidates(flow_id: int):
        """Flows that could be the other leg of this deposit/withdrawal, best match first."""
        try:
            return transfer_candidates(Database(config().db_path), flow_id)
        except ActivityError as exc:
            raise HTTPException(exc.status, exc.message) from exc

    @app.patch("/api/cash-flows/{flow_id}")
    def cash_flow_confirm(flow_id: int, body: CashFlowPatchIn):
        """The user's verdict: internal transfer (optionally paired) or external. Undo = the opposite."""
        try:
            if body.reset:
                return reset_flow(Database(config().db_path), flow_id)
            return confirm_flow(Database(config().db_path), flow_id, internal=body.is_internal_transfer,
                                matched_flow_id=body.matched_flow_id, note=body.note)
        except ActivityError as exc:
            raise HTTPException(exc.status, exc.message) from exc

    # -- settings ------------------------------------------------------------------
    def settings_payload(cfg: Config) -> dict:
        overrides = active_env_overrides()
        wallets = [
            {
                "id": f"evm:{w['address'].lower()}",
                "name": w["label"],
                "address": w["address"],
                "type": "evm",
                "chains": [c for c in EVM_CHAINS if c in (w.get("chains") or EVM_CHAINS)],
            }
            for w in cfg.evm_wallets
        ] + [
            {
                "id": f"solana:{w['address']}",
                "name": w["label"],
                "address": w["address"],
                "type": "solana",
                "chains": ["solana"],
            }
            for w in cfg.solana_wallets
        ]
        key, secret = cfg.binance.get("api_key") or "", cfg.binance.get("api_secret") or ""
        return {
            "binance": {
                "enabled": bool(cfg.binance.get("enabled")),
                "configured": cfg.binance_enabled,
                "has_api_key": bool(key),
                "has_api_secret": bool(secret),
                "api_key_masked": mask_key(key),
                "api_secret_masked": mask_secret(secret),
                "env_override": any(n.startswith("BINANCE_") for n in overrides),
            },
            "wallets": wallets,
            "wallets_env_override": any(n in ("EVM_WALLETS", "SOLANA_WALLETS") for n in overrides),
            "chains": [
                {"id": chain_id, **meta, "type": "solana" if chain_id == "solana" else "evm",
                 "native_symbol": cfg.chain(chain_id).get("native_symbol", meta["short"])}
                for chain_id, meta in CHAIN_META.items()
            ],
            "manual_assets": manual_payload(cfg),
            "manual_errors": cfg.manual_errors,
            "manual_kinds": list(MANUAL_KINDS),
            "allocation": allocation_status(cfg),
            "env_overrides": overrides,
            "config_path": str(config_file()),
            "config_exists": config_file().is_file(),
            "min_value": cfg.min_value,
            "auth_required": bool(auth_token(cfg)),
        }

    def manual_payload(cfg: Config) -> list[dict]:
        """Manual assets for the settings form, with when their quantities last changed."""
        try:
            changed = last_changes(Database(cfg.db_path)) if cfg.manual else {}
        except Exception:  # noqa: BLE001 - a missing/locked DB must not hide the settings
            changed = {}
        return [manual_asset_view(asset, changed.get(asset.label, "")) for asset in cfg.manual]

    @app.get("/api/settings")
    def get_settings():
        return settings_payload(config())

    @app.post("/api/settings")
    def save_settings(body: SettingsIn):
        """Merge Binance credentials and the wallet list into config.yaml, keep everything else."""
        path = config_file()
        with app.state.settings_lock:
            try:
                raw = read_raw_config(path)
            except (ValueError, OSError) as exc:
                raise HTTPException(500, f"Cannot read {path.name}: {exc}") from exc

            if body.binance is not None:
                section = raw.get("binance") if isinstance(raw.get("binance"), dict) else {}
                section["enabled"] = body.binance.enabled
                for field in ("api_key", "api_secret"):
                    value = getattr(body.binance, field)
                    if value is None or _is_masked(value):
                        continue  # untouched in the form: keep the stored credential
                    section[field] = value.strip()
                raw["binance"] = section

            if body.wallets is not None:
                raw["wallets"] = _wallet_sections(body.wallets, raw.get("wallets"))

            labels = _file_wallet_labels(raw)
            if {"EVM_WALLETS", "SOLANA_WALLETS"} & set(active_env_overrides()):
                labels = config().wallet_labels  # the wallets that actually run come from the env
            if body.manual_assets is not None:
                section = _manual_section(body.manual_assets, raw.get("manual_assets"), labels)
                if section:
                    raw["manual_assets"] = section
                else:
                    raw.pop("manual_assets", None)
            elif body.wallets is not None:
                # a renamed or removed wallet must not leave a manual asset pointing at nothing
                dangling = [error for error in parse_manual_assets(raw.get("manual_assets"), labels)[1]
                            if "linked_wallet" in error]
                if dangling:
                    raise HTTPException(422, dangling[0] + ". Link the manual asset to another wallet "
                                        "or unlink it first.")

            try:
                write_raw_config(raw, path)
            except OSError as exc:
                raise HTTPException(500, f"Cannot write {path.name}: {exc}") from exc
            app.state.live = None  # sources changed: the cached live fetch no longer applies
        payload = settings_payload(config())
        payload["saved"] = True
        return payload

    @app.post("/api/settings/test-binance")
    def test_binance(body: TestBinanceIn | None = None):
        """Check a key pair against Binance and report what it is allowed to do."""
        cfg = config()
        body = body or TestBinanceIn()
        key = body.api_key.strip() if body.api_key and not _is_masked(body.api_key) else cfg.binance.get("api_key", "")
        secret = (body.api_secret.strip() if body.api_secret and not _is_masked(body.api_secret)
                  else cfg.binance.get("api_secret", ""))
        if not key or not secret:
            raise HTTPException(400, "Enter both the API key and the secret first.")

        client = BinanceClient(key, secret, timeout=min(cfg.timeout, TEST_TIMEOUT),
                               recv_window=int(cfg.binance.get("recv_window", 5000)), session=session())
        try:
            restrictions = client.api_restrictions()
        except BinanceError as exc:
            return {"ok": False, "status": "rejected", "message": _binance_message(exc),
                    "permissions": {}, "warnings": []}
        except requests.RequestException as exc:
            return {"ok": False, "status": "unreachable",
                    "message": f"Could not reach Binance: {type(exc).__name__}.",
                    "permissions": {}, "warnings": []}

        permissions = {
            "reading": bool(restrictions.get("enableReading")),
            "spot_trading": bool(restrictions.get("enableSpotAndMarginTrading")),
            "margin": bool(restrictions.get("enableMargin")),
            "futures": bool(restrictions.get("enableFutures")),
            "withdrawals": bool(restrictions.get("enableWithdrawals")),
            "internal_transfer": bool(restrictions.get("enableInternalTransfer")),
            "ip_restricted": bool(restrictions.get("ipRestrict")),
        }
        risky = [label for flag, label in (
            ("withdrawals", "withdrawals"), ("spot_trading", "spot & margin trading"),
            ("futures", "futures"), ("margin", "margin"), ("internal_transfer", "internal transfers"),
        ) if permissions[flag]]
        warnings = []
        if risky:
            warnings.append(f"This key also allows {', '.join(risky)}. The tracker only reads: "
                            "create a key with 'Enable Reading' only.")
        if not permissions["ip_restricted"]:
            warnings.append("The key is not IP-restricted. Binance recommends locking it to your IP.")
        if not permissions["reading"]:
            return {"ok": False, "status": "no_read", "permissions": permissions, "warnings": warnings,
                    "message": "Connected, but the key lacks 'Enable Reading', so balances cannot be read."}
        return {"ok": True, "status": "connected", "permissions": permissions, "warnings": warnings,
                "message": "Connected. The key can read balances."}

    @app.post("/api/settings/test-wallet")
    def test_wallet(body: TestWalletIn):
        """Read the native balance of an address on each selected chain."""
        cfg = config()
        address = body.address.strip()
        kind = address_kind(address)
        if kind is None:
            raise HTTPException(422, "Not a wallet address: expected 0x + 40 hex characters (EVM) "
                                     "or a base58 Solana address.")
        chains = resolve_chains(kind, body.chains)
        client = OnChainClient(cfg.chains, timeout=min(cfg.timeout, TEST_TIMEOUT),
                               max_workers=cfg.max_workers, session=session())

        def probe(chain: str) -> dict:
            meta = cfg.chain(chain)
            symbol = meta.get("native_symbol", CHAIN_META[chain]["short"])
            try:
                quantity = client.solana_native(address) if chain == "solana" else client.evm_native(chain, address)
            except (RpcError, requests.RequestException, ValueError, TypeError) as exc:
                return {"chain": chain, "ok": False, "symbol": symbol, "balance": None,
                        "value_usd": None, "error": str(exc)[:200]}
            return {"chain": chain, "ok": True, "symbol": symbol, "balance": quantity,
                    "value_usd": None, "error": None, "_price_id": meta.get("native_price_id", "")}

        with ThreadPoolExecutor(max_workers=min(cfg.max_workers, len(chains))) as pool:
            results = list(pool.map(probe, chains))

        priced = [r for r in results if r["ok"] and r["balance"] and r.get("_price_id")]
        if priced:
            holdings = [Holding(symbol=r["symbol"], source=ONCHAIN, quantity=r["balance"],
                                price_id=r["_price_id"]) for r in priced]
            client.price(holdings)  # pricing failure leaves value_usd empty, never fails the test
            for result, holding in zip(priced, holdings):
                if holding.price_usd > 0:
                    result["value_usd"] = round(holding.value_usd, 2)
        for result in results:
            result.pop("_price_id", None)

        reachable = sum(1 for r in results if r["ok"])
        return {
            "ok": reachable == len(results),
            "address": address,
            "type": kind,
            "reachable": reachable,
            "total": len(results),
            "results": results,
            "message": (f"Read on all {len(results)} chain{'s' if len(results) > 1 else ''}."
                        if reachable == len(results)
                        else f"Read on {reachable} of {len(results)} chains; see the errors below."),
        }

    # -- PWA shell -----------------------------------------------------------------
    no_cache = {"Cache-Control": "no-cache"}

    @app.get("/", include_in_schema=False)
    def index(token: str | None = None):
        if token is not None:
            # `/?token=...` no longer unlocks anything (a token in a URL ends up in access logs
            # and browser history); an old bookmark is just sent to the unlock screen.
            return RedirectResponse("/", status_code=303)
        return FileResponse(static_dir / "index.html", headers=no_cache)

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        # served from the root so its scope covers the whole app, not just /static/
        return FileResponse(static_dir / "sw.js", media_type="application/javascript",
                            headers={**no_cache, "Service-Worker-Allowed": "/"})

    @app.get("/manifest.json", include_in_schema=False)
    def manifest():
        return FileResponse(static_dir / "manifest.json", media_type="application/manifest+json",
                            headers=no_cache)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(static_dir / "icons" / "favicon-32.png", media_type="image/png")

    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app


def _wallet_sections(wallets: list[WalletIn], existing) -> dict:
    """Split the flat UI wallet list into config.yaml's `evm` / `solana` sections.

    Per-wallet `tokens` (extra ERC-20s, SPL mint names) are not editable in the UI, so they
    are carried over from the existing entry with the same address instead of being lost.
    """
    existing = existing if isinstance(existing, dict) else {}
    kept_tokens = {}
    for kind in ("evm", "solana"):
        for wallet in _normalize_wallets(existing.get(kind)):
            key = wallet["address"].lower() if kind == "evm" else wallet["address"]
            if wallet.get("tokens"):
                kept_tokens[(kind, key)] = wallet["tokens"]

    sections: dict[str, list] = {"evm": [], "solana": []}
    seen: set[tuple[str, str]] = set()
    for wallet in wallets:
        address = wallet.address.strip()
        kind = address_kind(address)
        if kind is None:
            raise HTTPException(422, f"'{address[:20]}' is not a valid EVM or Solana address.")
        key = address.lower() if kind == "evm" else address
        if (kind, key) in seen:
            raise HTTPException(422, f"Wallet {address[:6]}..{address[-4:]} is listed twice.")
        seen.add((kind, key))
        chains = resolve_chains(kind, wallet.chains)  # validates both kinds, 422 on a mismatch
        entry = {
            "address": address,
            "label": wallet.name.strip() or f"{address[:6]}..{address[-4:]}",
        }
        if kind == "evm":  # Solana entries have a single implicit chain, nothing to store
            entry["chains"] = chains
        if (kind, key) in kept_tokens:
            entry["tokens"] = kept_tokens[(kind, key)]
        sections[kind].append(entry)
    return sections


def manual_asset_view(asset: ManualAsset, last_change: str = "", today: datetime | None = None) -> dict:
    """One manual asset as the settings screen shows it.

    `updated_at` is the day the quantities were last edited in Settings, else the newest manual
    cash flow of the asset. A staking position unchanged for `MANUAL_REVIEW_DAYS` gets a hint:
    rewards have most likely grown the balance since (docs/SPEC.md M6.4 AC3).
    """
    today = today or datetime.now(timezone.utc)
    updated = asset.updated_at or last_change
    age_days = None
    if updated:
        age_days = max((today - parse_timestamp(updated)).days, 0)
    review = asset.kind == "staking" and age_days is not None and age_days > MANUAL_REVIEW_DAYS
    return {
        "label": asset.label,
        "kind": asset.kind,
        "linked_wallet": asset.linked_wallet or None,
        "holdings": [
            {"symbol": h.symbol, "quantity": h.quantity, "price_id": h.price_id,
             "price_usd": h.price_usd, "liquid": h.liquid}
            for h in asset.holdings
        ],
        "updated_at": updated or None,
        "age_days": age_days,
        "review_hint": ("Check the balance again: staking rewards have probably changed it."
                        if review else None),
    }


def _file_wallet_labels(raw: dict) -> list[str]:
    """Labels of the wallets in a raw config.yaml mapping (EVM then Solana, each once)."""
    wallets = raw.get("wallets") if isinstance(raw.get("wallets"), dict) else {}
    labels: list[str] = []
    for kind in ("evm", "solana"):
        for wallet in _normalize_wallets(wallets.get(kind)):
            if wallet["label"] not in labels:
                labels.append(wallet["label"])
    return labels


def _manual_section(assets: list[ManualAssetIn], existing, wallet_labels=None) -> list[dict]:
    """The `manual_assets:` block to write. Labels must be unique (case-insensitive).

    `updated_at` moves to today only for an asset whose quantities changed (or is new); an
    asset saved untouched keeps its date, so the staking reminder is not reset by accident.
    A `linked_wallet` must name one of `wallet_labels` (422 otherwise) and is stored with the
    wallet's own spelling.

    The block is rebuilt from the request, so nothing stored may be lost by omission: an asset
    whose request leaves `linked_wallet` out keeps its stored link (an explicit null or "" still
    unlinks), and keys the form does not manage are carried over (`keep_unmanaged_keys`).
    """
    wallet_labels = list(wallet_labels or [])
    previous = {asset.label.casefold(): asset for asset in parse_manual_assets(existing)[0]}
    previous_raw = {}
    for entry in existing if isinstance(existing, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("label"), str):
            previous_raw.setdefault(entry["label"].strip().casefold(), entry)
    today = datetime.now(timezone.utc).date().isoformat()
    section, seen = [], set()
    for item in assets:
        label = item.label.strip()
        if not label:
            raise HTTPException(422, "A manual asset needs a name.")
        if label.casefold() in seen:
            raise HTTPException(422, f"Manual asset '{label}' is listed twice. Names must be unique.")
        seen.add(label.casefold())
        holdings, symbols = [], set()
        for h in item.holdings:
            symbol = h.symbol.strip().upper()
            if symbol in symbols:
                raise HTTPException(422, f"{symbol} is listed twice in '{label}'.")
            symbols.add(symbol)
            holdings.append(ManualHolding(symbol=symbol, quantity=float(h.quantity),
                                          price_id=h.price_id.strip(), price_usd=h.price_usd,
                                          liquid=h.liquid))
        old = previous.get(label.casefold())
        same = old is not None and {(x.symbol, x.quantity) for x in old.holdings} == \
            {(x.symbol, x.quantity) for x in holdings}
        updated_at = old.updated_at if same and old.updated_at else ("" if same else today)
        stored = previous_raw.get(label.casefold())
        wanted = item.linked_wallet
        if "linked_wallet" not in item.model_fields_set and isinstance(stored, dict):
            wanted = stored.get("linked_wallet") if isinstance(stored.get("linked_wallet"), str) else None
        linked = ""
        if wanted and wanted.strip():
            linked = resolve_linked_wallet(wanted, wallet_labels) or ""
            if not linked:
                known = ", ".join(f"'{name}'" for name in wallet_labels) or "none configured"
                raise HTTPException(422, f"Manual asset '{label}' is linked to wallet "
                                         f"'{wanted.strip()}', which is not configured "
                                         f"(wallets: {known}).")
        entry = ManualAsset(label=label, kind=item.kind, holdings=tuple(holdings),
                            updated_at=updated_at, linked_wallet=linked).to_raw()
        section.append(keep_unmanaged_keys(entry, stored))
    return section


app = create_app()
