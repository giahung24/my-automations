"""FastAPI backend for the mobile dashboard, plus the PWA it serves from ./static.

    python main.py serve --port 8080                 (127.0.0.1 only)
    python main.py serve --host 0.0.0.0 --port 8080  (reachable from your phone on the LAN)
    python -m uvicorn web_api:app --port 8080

The API re-reads config.yaml on every request, so a save from the settings screen takes
effect on the next refresh without a restart. Reads are cheap: `GET /api/portfolio` serves
the last live fetch or the newest SQLite snapshot and never touches the network; only
`POST /api/portfolio/refresh` and the two `test-*` endpoints call Binance or an RPC.

Access control: when PORTFOLIO_WEB_TOKEN (or `settings.web_token` in config.yaml) is set,
every /api route except /api/auth needs it - as a `portfolio_token` cookie (set by opening
`/?token=<it>` once, or by the app's unlock screen), an `Authorization: Bearer` header or an
`X-Portfolio-Token` header. Without a token the API is open to whoever can reach the port,
which is why `serve` binds 127.0.0.1 unless told otherwise.
"""
from __future__ import annotations

import os
import re
import secrets
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from clients.binance import BinanceClient, BinanceError
from clients.onchain import EVM_CHAINS, OnChainClient, RpcError
from config import (
    ROOT,
    Config,
    _normalize_wallets,
    active_env_overrides,
    load_config,
    read_raw_config,
    write_raw_config,
)
from models import (
    BINANCE_EARN,
    BINANCE_FUNDING,
    BINANCE_SPOT,
    ONCHAIN,
    SOURCE_LABELS,
    SOURCES,
    Holding,
    Portfolio,
)
from services.db import Database, parse_timestamp
from services.portfolio import fetch_portfolio, portfolio_from_db, to_dict

STATIC_DIR = ROOT / "static"
BINANCE_SOURCES = (BINANCE_SPOT, BINANCE_EARN, BINANCE_FUNDING)
TOKEN_COOKIE = "portfolio_token"
TOKEN_COOKIE_MAX_AGE = 365 * 86400
TEST_TIMEOUT = 15  # seconds; a settings-screen check should fail fast, not hang the UI
MASK = "****"

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


class SettingsIn(BaseModel):
    binance: BinanceSettingsIn | None = None
    wallets: list[WalletIn] | None = Field(default=None, max_length=50)


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


def _utc_iso(moment: datetime) -> str:
    """Same shape as utc_now_iso(), so string comparison in SQLite orders correctly."""
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


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

    Prefers the newest snapshot at or before the cutoff. With a shorter history it falls
    back to the oldest snapshot inside the window and reports the real `hours` covered,
    so the UI can say "6h" instead of pretending it is a 24h figure.
    """
    now = parse_timestamp(current_ts)
    cutoff = _utc_iso(now - timedelta(hours=hours))
    reference = database.snapshot_before(cutoff) or database.snapshot_after(cutoff)
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
        "reference_usd": round(reference.total_portfolio_usd, 2),
    }


def portfolio_payload(portfolio: Portfolio, *, origin: str, database: Database,
                      config: Config, snapshot_id: int | None = None) -> dict:
    """`show --json`'s schema plus what the dashboard needs to filter by source group."""
    data = to_dict(portfolio)
    values: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for holding in portfolio.holdings:
        if holding.quantity > 0:
            values[holding.symbol][holding.source] += holding.value_usd

    for asset in data["assets"]:
        by_value = values.get(asset["symbol"], {})
        by_qty = asset["by_source"]
        asset["value_by_source"] = {source: round(v, 2) for source, v in by_value.items()}
        asset["binance_quantity"] = sum(by_qty.get(s, 0.0) for s in BINANCE_SOURCES)
        asset["binance_value_usd"] = round(sum(by_value.get(s, 0.0) for s in BINANCE_SOURCES), 2)
        asset["onchain_quantity"] = by_qty.get(ONCHAIN, 0.0)
        asset["onchain_value_usd"] = round(by_value.get(ONCHAIN, 0.0), 2)

    totals = portfolio.totals
    data.update(
        origin=origin,
        snapshot_id=snapshot_id,
        configured=config.binance_enabled or config.has_wallets,
        min_value=config.min_value,
        groups={
            "all": round(portfolio.total_usd, 2),
            "binance": round(sum(totals.get(s, 0.0) for s in BINANCE_SOURCES), 2),
            "onchain": round(totals.get(ONCHAIN, 0.0), 2),
        },
        source_labels=SOURCE_LABELS,
        change_24h=change_since(database, portfolio.total_usd, portfolio.timestamp)
        if origin != "empty" else None,
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
    def _presented_token(request: Request) -> str:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return request.headers.get("x-portfolio-token") or request.cookies.get(TOKEN_COOKIE) or ""

    def _token_cookie(response, token: str, request: Request) -> None:
        response.set_cookie(
            TOKEN_COOKIE, token, max_age=TOKEN_COOKIE_MAX_AGE, httponly=True,
            samesite="strict", secure=request.url.scheme == "https", path="/",
        )

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path != "/api/auth":
            try:
                expected = auth_token(config())
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            if expected and not secrets.compare_digest(_presented_token(request), expected):
                return JSONResponse({"detail": "Locked: enter the access token."}, status_code=401)
        return await call_next(request)

    @app.post("/api/auth")
    def authenticate(body: AuthIn, request: Request):
        """Trade the access token for an HttpOnly cookie (the unlock screen calls this)."""
        expected = auth_token(config())
        if expected and not secrets.compare_digest(body.token.strip(), expected):
            raise HTTPException(401, "Wrong access token.")
        response = JSONResponse({"ok": True, "required": bool(expected)})
        if expected:
            _token_cookie(response, expected, request)
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
        if not cfg.binance_enabled and not cfg.has_wallets:
            raise HTTPException(400, "Nothing to refresh yet: add Binance API keys or a wallet in Settings.")
        with app.state.refresh_lock:  # two taps on refresh must not double-record
            portfolio = app.state.fetcher(cfg, cfg.min_value)
            if not portfolio.holdings and portfolio.errors:
                raise HTTPException(502, {
                    "message": "Every source failed, so nothing was recorded.",
                    "errors": portfolio.errors,
                })
            database = Database(cfg.db_path)
            snapshot_id = database.record(portfolio)
            app.state.live = (portfolio, snapshot_id)
        return portfolio_payload(portfolio, origin="live", database=database, config=cfg,
                                 snapshot_id=snapshot_id)

    @app.get("/api/portfolio/history")
    def portfolio_history(days: int = Query(30, ge=0, le=36500,
                                            description="Look-back window in days; 0 = everything")):
        """Snapshot totals oldest-first, one point per second, ready for a time-series chart."""
        cfg = config()
        snapshots = Database(cfg.db_path).history(days=days or None)
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
                "binance_spot_usd": round(snapshot.binance_spot_usd, 2),
                "binance_earn_usd": round(snapshot.binance_earn_usd, 2),
                "binance_funding_usd": round(snapshot.binance_funding_usd, 2),
            }
        ordered = [points[key] for key in sorted(points)]
        return {"days": days or None, "count": len(ordered), "points": ordered}

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
            "env_overrides": overrides,
            "config_path": str(config_file()),
            "config_exists": config_file().is_file(),
            "min_value": cfg.min_value,
            "auth_required": bool(auth_token(cfg)),
        }

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
    def index(request: Request, token: str | None = None):
        if token is not None:  # `/?token=...` bookmark: swap it for a cookie, drop it from the URL
            expected = auth_token(config())
            response = RedirectResponse("/", status_code=303)
            if expected and secrets.compare_digest(token.strip(), expected):
                _token_cookie(response, expected, request)
            return response
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


app = create_app()
