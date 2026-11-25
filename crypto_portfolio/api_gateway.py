"""Public, read-only gateway for api.tytony.link (served through Cloudflare Tunnel).

    GET /v1/crypto/portfolio/total?currency=USD        header  X-API-Key: <key>

Separate from web_api.py on purpose: this process imports nothing from the dashboard, never
loads Binance credentials, opens SQLite with ``mode=ro`` + ``PRAGMA query_only`` and has
exactly one route. Anything that is not an authorised ``GET`` on that route - wrong path,
wrong method, wrong Host, missing or wrong key - gets the same ``404 {"detail":"Not Found"}``
FastAPI returns for an unknown URL, so probing reveals nothing.

Run (binds loopback only; cloudflared is the only intended client):
    .venv/bin/python api_gateway.py

Config: ``.env.gateway`` next to this file (override the path with GATEWAY_ENV_FILE). It is
created on first start with a fresh 256-bit key and mode 0600. Environment variables with
the same names win over the file. Print the key with:  .venv/bin/python api_gateway.py show-key
"""
from __future__ import annotations

import ipaddress
import logging
import os
import secrets
import sqlite3
import stat
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from dotenv import dotenv_values
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

ROOT = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = ROOT / ".env.gateway"
ENDPOINT = "/v1/crypto/portfolio/total"
SUPPORTED_CURRENCIES = ("USD",)
MIN_KEY_LENGTH = 32

# (response id, snapshots column) in display order.
SOURCES = (
    ("binance-spot", "binance_spot_usd"),
    ("binance-earn", "binance_earn_usd"),
    ("binance-funding", "binance_funding_usd"),
    ("onchain", "onchain_usd"),
)

SECURITY_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
CENT = Decimal("0.01")

log = logging.getLogger("api_gateway")


# --------------------------------------------------------------------------- config
@dataclass(frozen=True)
class Settings:
    api_key: str
    db_path: Path
    host: str = "127.0.0.1"
    port: int = 8090
    stale_after: timedelta = timedelta(minutes=30)
    rate_limit_per_minute: int = 30
    allowed_hosts: frozenset[str] = frozenset({"api.tytony.link", "127.0.0.1", "localhost", "::1"})


ENV_TEMPLATE = """\
# api_gateway.py configuration. Private: mode 0600, git-ignored. Env vars override these.
GATEWAY_API_KEY={key}
GATEWAY_HOST=127.0.0.1
GATEWAY_PORT=8090
GATEWAY_DB_PATH=portfolio_history.db
GATEWAY_STALE_AFTER_SECONDS=1800
GATEWAY_RATE_LIMIT_PER_MINUTE=30
GATEWAY_ALLOWED_HOSTS=api.tytony.link,127.0.0.1,localhost,::1
"""


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)  # 256 bits, header-safe alphabet


def ensure_env_file(path: Path) -> None:
    """Create the env file with a fresh key, add a key if it lacks one, and lock its mode."""
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(ENV_TEMPLATE.format(key=generate_api_key()))
        log.warning("Created %s with a new API key (show it with: api_gateway.py show-key)", path)
        return

    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        path.chmod(0o600)
        log.warning("%s was readable by group/others (mode %o); reset to 600", path, mode)

    if not (dotenv_values(path).get("GATEWAY_API_KEY") or "").strip():
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\nGATEWAY_API_KEY={generate_api_key()}\n")
        log.warning("Added a new GATEWAY_API_KEY to %s", path)


def load_settings(env_file: Path | None = None) -> Settings:
    env_file = Path(env_file or os.environ.get("GATEWAY_ENV_FILE") or DEFAULT_ENV_FILE)
    if not os.environ.get("GATEWAY_API_KEY"):
        ensure_env_file(env_file)
    values = {k: v for k, v in dotenv_values(env_file).items() if v is not None} if env_file.exists() else {}
    values.update({k: v for k, v in os.environ.items() if k.startswith("GATEWAY_")})

    def get(name: str, default: str) -> str:
        return (values.get(name) or default).strip()

    api_key = get("GATEWAY_API_KEY", "")
    if len(api_key) < MIN_KEY_LENGTH or not api_key.isascii() or not api_key.isprintable() or " " in api_key:
        raise SystemExit(f"GATEWAY_API_KEY must be >= {MIN_KEY_LENGTH} printable ASCII chars without spaces")

    db_path = Path(get("GATEWAY_DB_PATH", "portfolio_history.db")).expanduser()
    if not db_path.is_absolute():
        db_path = ROOT / db_path

    host = get("GATEWAY_HOST", "127.0.0.1")
    if not _is_loopback(host):
        raise SystemExit(f"GATEWAY_HOST={host!r} is not a loopback address; this service must sit behind cloudflared")

    try:
        port = int(get("GATEWAY_PORT", "8090"))
        stale_seconds = int(get("GATEWAY_STALE_AFTER_SECONDS", "1800"))
        rate_limit = int(get("GATEWAY_RATE_LIMIT_PER_MINUTE", "30"))
    except ValueError as exc:
        raise SystemExit(f"Invalid numeric setting: {exc}") from exc
    if not (1 <= port <= 65535 and stale_seconds > 0 and rate_limit > 0):
        raise SystemExit("GATEWAY_PORT, GATEWAY_STALE_AFTER_SECONDS and GATEWAY_RATE_LIMIT_PER_MINUTE must be positive")

    hosts = frozenset(h.strip().lower() for h in get("GATEWAY_ALLOWED_HOSTS", ",".join(Settings.allowed_hosts)).split(",") if h.strip())

    return Settings(
        api_key=api_key,
        db_path=db_path,
        host=host,
        port=port,
        stale_after=timedelta(seconds=stale_seconds),
        rate_limit_per_minute=rate_limit,
        allowed_hosts=hosts,
    )


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# --------------------------------------------------------------------------- rate limiting
class SlidingWindowLimiter:
    """Per-client sliding window, in memory. Bounded: idle clients are evicted under pressure."""

    def __init__(self, limit: int, window_seconds: float = 60.0, max_clients: int = 10_000):
        self.limit = limit
        self.window = window_seconds
        self.max_clients = max_clients
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, client: str) -> tuple[bool, int]:
        """Record a hit. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            hits = self._hits.get(client)
            if hits is None:
                if len(self._hits) >= self.max_clients:
                    self._evict(cutoff)
                hits = self._hits[client] = deque()
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False, max(1, int(hits[0] + self.window - now) + 1)
            hits.append(now)
            return True, 0

    def _evict(self, cutoff: float) -> None:
        for client in [c for c, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[client]
        if len(self._hits) >= self.max_clients:
            # Flood of distinct clients: forget them rather than grow without bound.
            # The Cloudflare edge is the real volumetric defence.
            self._hits.clear()


# --------------------------------------------------------------------------- data access
def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open SQLite strictly read-only. Raises sqlite3.OperationalError if the file is missing
    (mode=ro never creates it).

    The collector keeps the file in WAL mode (services/db.py), so this reader sees the last
    committed snapshot while a write is in progress rather than `database is locked`. A WAL
    reader needs the `-shm` index next to the file: SQLite creates it if missing, which is
    why the systemd unit lists the project directory in ReadWritePaths."""
    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def read_latest_snapshot(db_path: Path) -> sqlite3.Row | None:
    connection = connect_readonly(db_path)
    try:
        return connection.execute(
            "SELECT timestamp, total_portfolio_usd, binance_spot_usd, binance_earn_usd,"
            "       binance_funding_usd, onchain_usd"
            "  FROM snapshots ORDER BY timestamp DESC, id DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def money(value: float | None) -> str:
    return str(Decimal(repr(float(value or 0.0))).quantize(CENT, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- app
def _hostname(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:8090
        return host[1 : host.find("]")]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _client_id(request: Request) -> str:
    # Bound to loopback, so every remote request arrives via cloudflared, which sets
    # CF-Connecting-IP to the real client address.
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")


def _not_found() -> JSONResponse:
    return JSONResponse({"detail": "Not Found"}, status_code=404, headers=SECURITY_HEADERS)


def _error(status: int, code: str, **extra) -> JSONResponse:
    return JSONResponse({"error": code, **extra}, status_code=status, headers=SECURITY_HEADERS)


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, swagger_ui_oauth2_redirect_url=None)
    expected_key = settings.api_key.encode("latin-1")
    limiter = SlidingWindowLimiter(settings.rate_limit_per_minute)

    def is_authorised(request: Request) -> bool:
        provided = request.headers.get("x-api-key", "").encode("latin-1")  # Starlette decodes headers as latin-1
        key_ok = secrets.compare_digest(provided, expected_key)  # evaluated unconditionally: no early exit
        return (
            key_ok
            and request.method == "GET"
            and request.url.path == ENDPOINT
            and _hostname(request.headers.get("host", "")) in settings.allowed_hosts
        )

    @app.middleware("http")
    async def gatekeeper(request: Request, call_next):
        authorised = is_authorised(request)
        allowed, retry_after = limiter.check(_client_id(request))
        if not authorised:
            return _not_found()
        if not allowed:
            response = _error(429, "rate_limited")
            response.headers["Retry-After"] = str(retry_after)
            return response
        return await call_next(request)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException):
        if exc.status_code in (404, 405):
            return _not_found()
        return _error(exc.status_code, "http_error")

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError):
        return _error(400, "invalid_request")

    @app.get(ENDPOINT)
    def portfolio_total(currency: str = Query("USD", max_length=8)):
        code = currency.strip().upper()
        if code not in SUPPORTED_CURRENCIES:
            return _error(400, "unsupported_currency", supported=list(SUPPORTED_CURRENCIES))

        try:
            row = read_latest_snapshot(settings.db_path)
        except sqlite3.Error:
            log.exception("Cannot read %s", settings.db_path)
            return _error(503, "data_unavailable")
        if row is None:
            return _error(503, "no_data")

        try:
            as_of = parse_utc(row["timestamp"])
        except ValueError:
            log.error("Unparseable snapshot timestamp %r", row["timestamp"])
            return _error(503, "data_unavailable")

        stale = datetime.now(timezone.utc) - as_of > settings.stale_after
        # The DB keeps per-source totals but not per-source fetch errors, so status can only
        # tell "has value" from "zero" - a failed source is recorded as 0 and shows "empty".
        sources = [
            {"id": source_id, "status": "ok" if (row[column] or 0) > 0 else "empty", "value": money(row[column])}
            for source_id, column in SOURCES
        ]
        body = {
            "value": money(row["total_portfolio_usd"]),
            "currency": code,
            "as_of": as_of.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stale": stale,
            "sources": sources,
        }
        return JSONResponse(body, headers=SECURITY_HEADERS)

    return app


# --------------------------------------------------------------------------- entry point
def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()

    if argv[1:] == ["show-key"]:
        print(settings.api_key)
        return 0
    if argv[1:]:
        print("usage: api_gateway.py [show-key]", file=sys.stderr)
        return 2

    if not settings.db_path.exists():
        log.warning("Database %s does not exist yet; requests will get 503 until it does", settings.db_path)

    import uvicorn

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        server_header=False,
        proxy_headers=False,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
