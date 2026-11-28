"""Public, read-only gateway for api.tytony.link (served through Cloudflare Tunnel).

    GET /v1/crypto/portfolio/total?currency=USD        header  X-API-Key: <key>
    GET /v1/crypto/portfolio/widget                    same header; off unless a target is set

Separate from web_api.py on purpose: this process imports nothing from the dashboard, never
loads Binance credentials, opens SQLite with ``mode=ro`` + ``PRAGMA query_only`` and has
exactly these routes. Anything that is not an authorised ``GET`` on one of them - wrong path,
wrong method, wrong Host, missing or wrong key - gets the same ``404 {"detail":"Not Found"}``
FastAPI returns for an unknown URL, so probing reveals nothing.

``/widget`` feeds a home-screen widget and carries **percentages only**: progress towards
``GATEWAY_WIDGET_TARGET_USD`` (the target itself never leaves this process), flow-adjusted
change over 24h / 7d / 30d, the same against BTC, the drawdown from the peak, and a health
flag. No amount in any currency, no quantity, no address. It exists only while that variable
is set to a positive number.

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
WIDGET_ENDPOINT = "/v1/crypto/portfolio/widget"
WIDGET_STALE = timedelta(minutes=130)   # two missed hourly snapshots, as on the dashboard
WIDGET_BACKUP_MAX_AGE = timedelta(hours=48)
PEG_THRESHOLD = 0.003
SUPPORTED_CURRENCIES = ("USD",)
MIN_KEY_LENGTH = 32

# (response id, snapshots column) in display order.
SOURCES = (
    ("binance-spot", "binance_spot_usd"),
    ("binance-earn", "binance_earn_usd"),
    ("binance-funding", "binance_funding_usd"),
    ("onchain", "onchain_usd"),
    ("manual", "manual_usd"),  # schema v5 (M6.2); skipped on a database that predates it
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
    widget_target_usd: float = 0.0   # 0: the /widget route does not exist
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
# Set a net-worth goal in USD to switch on /v1/crypto/portfolio/widget (percentages only).
# GATEWAY_WIDGET_TARGET_USD=150000
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
        target = float(get("GATEWAY_WIDGET_TARGET_USD", "0"))
    except ValueError as exc:
        raise SystemExit(f"Invalid numeric setting: {exc}") from exc
    if not (1 <= port <= 65535 and stale_seconds > 0 and rate_limit > 0):
        raise SystemExit("GATEWAY_PORT, GATEWAY_STALE_AFTER_SECONDS and GATEWAY_RATE_LIMIT_PER_MINUTE must be positive")
    if not (0 <= target < float("inf")):   # also refuses NaN
        raise SystemExit("GATEWAY_WIDGET_TARGET_USD must be a positive number of USD (or unset)")

    hosts = frozenset(h.strip().lower() for h in get("GATEWAY_ALLOWED_HOSTS", ",".join(Settings.allowed_hosts)).split(",") if h.strip())

    return Settings(
        api_key=api_key,
        db_path=db_path,
        host=host,
        port=port,
        stale_after=timedelta(seconds=stale_seconds),
        rate_limit_per_minute=rate_limit,
        widget_target_usd=target,
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
            # `*` rather than a column list: this read-only reader must keep working on a
            # database the collector has not migrated yet (no manual_usd before schema v5).
            "SELECT * FROM snapshots ORDER BY timestamp DESC, id DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()


def _try(connection: sqlite3.Connection, query: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Rows of a query, or none when the database predates the table or view it needs."""
    try:
        return connection.execute(query, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _arrow(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{'▲' if value > 0 else '▼' if value < 0 else '•'} {abs(value):.2f}%"


def widget_payload(db_path: Path, target_usd: float, now: datetime | None = None) -> dict | None:
    """Everything the home-screen widget shows, in percent. None when there is no snapshot.

    Changes are net of deposits and withdrawals: 7d and 30d chain the daily Modified Dietz
    returns of ``v_daily_returns`` (the dashboard's TWR), 24h is the NAV change minus the
    external flows of the interval. A window the history does not reach is ``null``. Figures
    come from the newest snapshot in which no source failed, so one dead RPC is a health
    warning, not a -30% day.
    """
    now = now or datetime.now(timezone.utc)
    connection = connect_readonly(db_path)
    try:
        newest = connection.execute("SELECT * FROM snapshots ORDER BY timestamp DESC, id DESC LIMIT 1").fetchone()
        if newest is None:
            return None
        healthy = _try(connection, """
            SELECT * FROM snapshots s WHERE s.is_backfilled = 0 AND NOT EXISTS (
                SELECT 1 FROM snapshot_sources x WHERE x.snapshot_id = s.id AND x.status IN ('error', 'inferred_error'))
            ORDER BY s.timestamp DESC, s.id DESC LIMIT 1""")
        last = healthy[0] if healthy else newest
        nav, stamp = float(last["total_portfolio_usd"] or 0), last["timestamp"]

        # -- change, net of flows --------------------------------------------------------------
        day_ago = (parse_utc(stamp) - timedelta(hours=24)).isoformat()
        base = _try(connection, """
            SELECT timestamp, total_portfolio_usd FROM snapshots s WHERE s.is_backfilled = 0 AND s.timestamp <= ?
              AND NOT EXISTS (SELECT 1 FROM snapshot_sources x WHERE x.snapshot_id = s.id
                              AND x.status IN ('error', 'inferred_error'))
            ORDER BY s.timestamp DESC, s.id DESC LIMIT 1""", (day_ago,))
        change_24h = None
        if base and float(base[0]["total_portfolio_usd"] or 0) > 0:
            flows = _try(connection, "SELECT COALESCE(SUM(signed_usd), 0) FROM v_external_flows "
                                     "WHERE timestamp > ? AND timestamp <= ?", (base[0]["timestamp"], stamp))
            net = float(flows[0][0]) if flows else 0.0
            start = float(base[0]["total_portfolio_usd"])
            change_24h = (nav - start - net) / start * 100

        closes = _try(connection, "SELECT d.day, d.return, n.snapshot_id FROM v_daily_returns d "
                                  "JOIN v_nav_daily n ON n.day = d.day ORDER BY d.day")
        last_day = datetime.fromisoformat(closes[-1]["day"]).date() if closes else None

        def window(days: int) -> tuple[float | None, int | None]:
            """(chained return in %, snapshot id of the close the window starts from)."""
            if last_day is None:
                return None, None
            cutoff = (last_day - timedelta(days=days)).isoformat()
            before = [row for row in closes if row["day"] <= cutoff]
            if not before:   # the history does not reach back that far: no figure
                return None, None
            growth = 1.0
            for row in closes:
                if row["day"] > cutoff and row["return"] is not None:
                    growth *= 1.0 + row["return"]
            return (growth - 1.0) * 100, before[-1]["snapshot_id"]

        change_7d, _ = window(7)
        change_30d, month_base = window(30)

        btc_30d = None
        if month_base is not None:
            prices = [_try(connection, "SELECT MAX(price_usd) FROM snapshot_assets WHERE snapshot_id = ? AND symbol = 'BTC'",
                           (snapshot_id,)) for snapshot_id in (month_base, closes[-1]["snapshot_id"])]
            then, today = (float(rows[0][0] or 0) if rows else 0.0 for rows in prices)
            if then > 0 and today > 0:
                btc_30d = (today / then - 1.0) * 100

        index, peak, drawdown = 1.0, 1.0, None
        for row in closes:
            if row["return"] is not None:
                index *= 1.0 + row["return"]
            peak = max(peak, index)
            drawdown = (index / peak - 1.0) * 100

        # -- health ------------------------------------------------------------------------------
        reasons = []
        age = now - parse_utc(newest["timestamp"])
        if age > WIDGET_STALE:
            reasons.append("stale")
        if healthy and healthy[0]["id"] != newest["id"]:
            reasons.append("source_failed")
        if _try(connection, "SELECT 1 FROM peg_checks WHERE snapshot_id = ? AND error = '' AND price IS NOT NULL "
                            "GROUP BY symbol HAVING MAX(ABS(price - 1.0)) > ?", (newest["id"], PEG_THRESHOLD)):
            reasons.append("peg")
        backup = _try(connection, "SELECT value, updated_at FROM sync_state WHERE key = 'backup.status'")
        if backup and ('"error": ""' not in backup[0]["value"]
                       or now - parse_utc(backup[0]["updated_at"]) > WIDGET_BACKUP_MAX_AGE):
            reasons.append("backup")
    finally:
        connection.close()

    rounded = lambda value: None if value is None else round(value, 2)  # noqa: E731
    progress = nav / target_usd * 100
    versus = None if change_30d is None or btc_30d is None else change_30d - btc_30d
    return {
        "progress_pct": rounded(progress),
        "change_24h_pct": rounded(change_24h), "change_7d_pct": rounded(change_7d), "change_30d_pct": rounded(change_30d),
        "btc_30d_pct": rounded(btc_30d), "vs_btc_30d_pp": rounded(versus),
        "drawdown_pct": rounded(drawdown),
        "health": "warn" if reasons else "ok", "health_reasons": reasons,
        "as_of": parse_utc(newest["timestamp"]).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "age_minutes": max(int(age.total_seconds() // 60), 0),
        # ready to drop into a text layer: the widget app then needs no formatting rules
        "text": {
            "progress": f"{progress:.0f}%",
            "change_24h": _arrow(change_24h), "change_7d": _arrow(change_7d), "change_30d": _arrow(change_30d),
            "btc_30d": _arrow(btc_30d),
            "vs_btc_30d": "—" if versus is None else f"{'+' if versus >= 0 else '−'}{abs(versus):.1f} pp vs BTC",
            "drawdown": "—" if drawdown is None else ("at the peak" if drawdown > -0.005 else f"−{abs(drawdown):.1f}% from peak"),
            "health": "OK" if not reasons else "Check: " + ", ".join(r.replace("_", " ") for r in reasons),
        },
    }


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
    # the widget route is not merely refused without a target: it is not a route at all
    routes = {ENDPOINT, WIDGET_ENDPOINT} if settings.widget_target_usd > 0 else {ENDPOINT}

    def is_authorised(request: Request) -> bool:
        provided = request.headers.get("x-api-key", "").encode("latin-1")  # Starlette decodes headers as latin-1
        key_ok = secrets.compare_digest(provided, expected_key)  # evaluated unconditionally: no early exit
        return (
            key_ok
            and request.method == "GET"
            and request.url.path in routes
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
            if column in row.keys()
        ]
        body = {
            "value": money(row["total_portfolio_usd"]),
            "currency": code,
            "as_of": as_of.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stale": stale,
            "sources": sources,
        }
        return JSONResponse(body, headers=SECURITY_HEADERS)

    @app.get(WIDGET_ENDPOINT)
    def portfolio_widget():
        try:
            body = widget_payload(settings.db_path, settings.widget_target_usd)
        except (sqlite3.Error, ValueError):
            log.exception("Cannot build the widget payload from %s", settings.db_path)
            return _error(503, "data_unavailable")
        if body is None:
            return _error(503, "no_data")
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
