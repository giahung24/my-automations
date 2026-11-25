"""API tests for web_api.py - FastAPI TestClient, temp config/DB, mocked HTTP. No network.

    .venv/bin/pytest test_api.py -q

Reuses the fake requests sessions from test_all.py, so the Binance and RPC clients run their
real code paths against canned responses.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as config_module
import main as cli
import web_api
from models import BINANCE_EARN, BINANCE_SPOT, ONCHAIN, Holding
from services.db import Database
from services.portfolio import aggregate
from test_all import FakeResponse, FakeSession, binance_session, onchain_session

EVM_A = "0x" + "ab" * 20
EVM_B = "0x" + "cd" * 20
SOL_A = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
SECRET = "s3cr3t-value-that-must-never-leak-9f3a"
API_KEY = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


# -- fixtures ------------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """No real .env, no env overrides, no access token unless a test sets one."""
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: False)
    for name in (*config_module.ENV_OVERRIDES, "PORTFOLIO_WEB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def write_config(path: Path, **sections) -> Path:
    base = {"settings": {"db_path": str(path.parent / "history.db")}}
    base.update(sections)
    path.write_text(yaml.safe_dump(base), encoding="utf-8")
    return path


def sample_holdings(scale: float = 1.0) -> list[Holding]:
    return [
        Holding("BTC", BINANCE_SPOT, 0.5, 60000.0 * scale),
        Holding("BTC", BINANCE_EARN, 0.1, 60000.0 * scale, detail="flexible"),
        Holding("ETH", ONCHAIN, 2.0, 3000.0 * scale, detail="ethereum/main"),
        Holding("USDT", BINANCE_SPOT, 250.0, 1.0),
        Holding("DUST", BINANCE_SPOT, 1000.0, 0.0001),
    ]


def fake_fetcher(holdings=None, errors=None):
    calls = []

    def fetch(config, min_value):
        calls.append(min_value)
        portfolio = aggregate(holdings if holdings is not None else sample_holdings(), min_value)
        portfolio.errors = list(errors or [])
        return portfolio

    fetch.calls = calls
    return fetch


def make_client(tmp_path, *, sections=None, fetcher=None, session=None, min_value=None) -> TestClient:
    cfg = write_config(tmp_path / "config.yaml", **(sections or {}))
    app = web_api.create_app(
        cfg,
        fetcher=fetcher or fake_fetcher(),
        http_session_factory=(lambda: session) if session is not None else None,
        min_value=min_value,
    )
    return TestClient(app)


CONFIGURED = {
    "binance": {"enabled": True, "api_key": API_KEY, "api_secret": SECRET},
    "wallets": {"evm": [{"address": EVM_A, "label": "main", "chains": ["ethereum"]}]},
}


def seed(db_path: Path, when: datetime, scale: float) -> int:
    portfolio = aggregate(sample_holdings(scale))
    portfolio.timestamp = when.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return Database(db_path).record(portfolio)


# -- static / PWA --------------------------------------------------------------
def test_index_and_pwa_assets_are_served(tmp_path):
    client = make_client(tmp_path)
    index = client.get("/")
    assert index.status_code == 200
    assert 'rel="manifest"' in index.text
    assert 'name="apple-mobile-web-app-capable"' in index.text
    assert 'name="apple-mobile-web-app-status-bar-style"' in index.text
    assert index.headers["cache-control"] == "no-cache"

    manifest = client.get("/manifest.json")
    assert manifest.status_code == 200
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    data = manifest.json()
    assert data["display"] == "standalone" and data["start_url"] == "/"
    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]

    worker = client.get("/sw.js")
    assert worker.status_code == 200
    assert worker.headers["service-worker-allowed"] == "/"
    assert "javascript" in worker.headers["content-type"]

    for path in ("/static/app.js", "/static/style.css", "/static/vendor/vue.global.prod.js",
                 "/static/vendor/lightweight-charts.standalone.production.js",
                 "/static/vendor/tailwindcss-browser.js", "/static/fonts/manrope-latin-wght-normal.woff2",
                 "/static/icons/apple-touch-icon.png", "/static/coins/btc.svg", "/favicon.ico"):
        assert client.get(path).status_code == 200, path


def test_service_worker_precaches_only_files_that_exist(tmp_path):
    client = make_client(tmp_path)
    source = client.get("/sw.js").text
    shell = json.loads(source.split("const SHELL = ", 1)[1].split(";", 1)[0].replace("'", '"').replace(",\n]", "\n]"))
    for path in shell:
        assert client.get(path).status_code == 200, path


def test_health(tmp_path):
    assert make_client(tmp_path).get("/api/health").json() == {"ok": True}


# -- GET /api/portfolio ----------------------------------------------------------
def test_portfolio_empty_when_nothing_configured_or_recorded(tmp_path):
    data = make_client(tmp_path).get("/api/portfolio").json()
    assert data["origin"] == "empty"
    assert data["configured"] is False
    assert data["total_usd"] == 0
    assert data["assets"] == []
    assert data["change_24h"] is None
    assert data["groups"] == {"all": 0, "binance": 0, "onchain": 0, "manual": 0, "wallets": 0}  # manual: M6.2
    assert data["wallet_groups"] == []


def test_portfolio_reads_latest_snapshot_without_fetching(tmp_path):
    fetcher = fake_fetcher()
    client = make_client(tmp_path, sections=CONFIGURED, fetcher=fetcher)
    seed(tmp_path / "history.db", datetime.now(timezone.utc) - timedelta(hours=2), 1.0)

    data = client.get("/api/portfolio").json()
    assert fetcher.calls == []                      # GET never hits the network
    assert data["origin"] == "snapshot"
    assert data["configured"] is True
    assert data["total_usd"] == pytest.approx(36000 + 6000 + 250 + 0.1, abs=0.01)  # BTC spot+earn, ETH, USDT, dust
    symbols = [a["symbol"] for a in data["assets"]]
    assert symbols == ["BTC", "ETH", "USDT"]        # sorted by value, dust hidden
    assert data["dust"] == {"assets": 1, "value_usd": 0.1}

    btc = data["assets"][0]
    assert btc["binance_quantity"] == pytest.approx(0.6)
    assert btc["binance_value_usd"] == pytest.approx(36000)
    assert btc["onchain_quantity"] == 0
    assert btc["value_by_source"] == {"binance_spot": 30000.0, "binance_earn": 6000.0}
    eth = data["assets"][1]
    assert eth["onchain_value_usd"] == pytest.approx(6000)
    assert data["groups"]["binance"] == pytest.approx(36250.1, abs=0.01)
    assert data["groups"]["onchain"] == pytest.approx(6000)
    assert data["source_labels"]["binance_earn"] == "Binance Earn"


def test_min_value_override_changes_the_dust_filter(tmp_path):
    client = make_client(tmp_path, sections=CONFIGURED, min_value=0.0)
    seed(tmp_path / "history.db", datetime.now(timezone.utc), 1.0)
    data = client.get("/api/portfolio").json()
    assert "DUST" in [a["symbol"] for a in data["assets"]]
    assert data["min_value"] == 0.0


# -- change_24h ---------------------------------------------------------------------
def test_change_24h_uses_snapshot_at_or_before_the_cutoff(tmp_path):
    client = make_client(tmp_path, sections=CONFIGURED)
    now = datetime.now(timezone.utc)
    db = tmp_path / "history.db"
    seed(db, now - timedelta(hours=30), 0.5)        # older than the cutoff: skipped
    seed(db, now - timedelta(hours=25), 1.0)        # newest at/before the cutoff: the reference
    seed(db, now - timedelta(hours=3), 1.1)
    seed(db, now, 1.2)

    data = client.get("/api/portfolio").json()
    change = data["change_24h"]
    reference = aggregate(sample_holdings(1.0)).total_usd
    current = aggregate(sample_holdings(1.2)).total_usd
    assert change["usd"] == pytest.approx(current - reference, abs=0.01)
    assert change["pct"] == pytest.approx((current - reference) / reference * 100, abs=0.001)
    assert change["hours"] == pytest.approx(25, abs=0.05)


def test_change_24h_with_short_history_reports_the_real_window(tmp_path):
    client = make_client(tmp_path, sections=CONFIGURED)
    now = datetime.now(timezone.utc)
    seed(tmp_path / "history.db", now - timedelta(hours=6), 1.0)
    seed(tmp_path / "history.db", now, 1.0)
    change = client.get("/api/portfolio").json()["change_24h"]
    assert change["usd"] == pytest.approx(0, abs=0.01)
    assert change["hours"] == pytest.approx(6, abs=0.05)


def test_change_24h_is_none_with_a_single_snapshot(tmp_path):
    client = make_client(tmp_path, sections=CONFIGURED)
    seed(tmp_path / "history.db", datetime.now(timezone.utc), 1.0)
    assert client.get("/api/portfolio").json()["change_24h"] is None


# -- POST /api/portfolio/refresh -----------------------------------------------------
def test_refresh_fetches_records_and_serves_live_data(tmp_path):
    fetcher = fake_fetcher()
    client = make_client(tmp_path, sections=CONFIGURED, fetcher=fetcher)
    response = client.post("/api/portfolio/refresh")
    assert response.status_code == 200
    data = response.json()
    assert fetcher.calls == [1.0]
    assert data["origin"] == "live"
    assert data["ok"] is True
    assert data["snapshot_id"] == 1
    assert Database(tmp_path / "history.db").count() == 1
    # the dust row is hidden from `assets` but stored: history must not lie about the total
    stored = {row["symbol"] for row in Database(tmp_path / "history.db").latest_assets()}
    assert "DUST" in stored

    again = client.get("/api/portfolio").json()
    assert again["origin"] == "live"
    assert again["assets"][1]["locations"] == ["ethereum/main"]   # live data keeps wallet detail


def test_refresh_without_sources_is_a_400(tmp_path):
    response = make_client(tmp_path).post("/api/portfolio/refresh")
    assert response.status_code == 400
    assert "Settings" in response.json()["detail"]


def test_refresh_where_every_source_failed_records_nothing(tmp_path):
    fetcher = fake_fetcher(holdings=[], errors=["binance spot: HTTP 401", "ethereum/main: refused"])
    client = make_client(tmp_path, sections=CONFIGURED, fetcher=fetcher)
    response = client.post("/api/portfolio/refresh")
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["errors"] == ["binance spot: HTTP 401", "ethereum/main: refused"]
    assert "nothing was recorded" in detail["message"]
    assert Database(tmp_path / "history.db").count() == 0


def test_refresh_with_partial_failure_still_records(tmp_path):
    fetcher = fake_fetcher(errors=["polygon/main: HTTP 429 (rate limited)"])
    client = make_client(tmp_path, sections=CONFIGURED, fetcher=fetcher)
    data = client.post("/api/portfolio/refresh").json()
    assert data["ok"] is False
    assert data["errors"] == ["polygon/main: HTTP 429 (rate limited)"]
    assert Database(tmp_path / "history.db").count() == 1


def test_live_cache_is_ignored_once_a_newer_snapshot_exists(tmp_path):
    client = make_client(tmp_path, sections=CONFIGURED)
    client.post("/api/portfolio/refresh")
    seed(tmp_path / "history.db", datetime.now(timezone.utc) + timedelta(minutes=1), 2.0)  # e.g. cron
    data = client.get("/api/portfolio").json()
    assert data["origin"] == "snapshot"
    assert data["snapshot_id"] == 2


# -- GET /api/portfolio/history ------------------------------------------------------
def test_history_filters_by_days_and_orders_oldest_first(tmp_path):
    client = make_client(tmp_path, sections=CONFIGURED)
    now = datetime.now(timezone.utc)
    db = tmp_path / "history.db"
    seed(db, now - timedelta(days=40), 0.8)
    seed(db, now - timedelta(days=5), 0.9)
    seed(db, now - timedelta(hours=1), 1.0)

    month = client.get("/api/portfolio/history", params={"days": 30}).json()
    assert month["count"] == 2
    times = [p["time"] for p in month["points"]]
    assert times == sorted(times)
    point = month["points"][-1]
    assert set(point) >= {"time", "timestamp", "total_usd", "binance_usd", "onchain_usd"}
    assert point["binance_usd"] + point["onchain_usd"] == pytest.approx(point["total_usd"], abs=0.02)

    assert client.get("/api/portfolio/history", params={"days": 0}).json()["count"] == 3
    assert client.get("/api/portfolio/history").json()["days"] == 30   # default window
    assert client.get("/api/portfolio/history", params={"days": 1}).json()["count"] == 1


def test_history_collapses_snapshots_in_the_same_second(tmp_path):
    client = make_client(tmp_path, sections=CONFIGURED)
    when = datetime.now(timezone.utc) - timedelta(minutes=5)
    seed(tmp_path / "history.db", when, 1.0)
    seed(tmp_path / "history.db", when, 2.0)
    points = client.get("/api/portfolio/history").json()["points"]
    assert len(points) == 1                          # a chart needs strictly increasing times
    assert points[0]["total_usd"] == pytest.approx(aggregate(sample_holdings(2.0)).total_usd, abs=0.01)


def test_history_rejects_bad_days(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/api/portfolio/history", params={"days": -1}).status_code == 422
    assert client.get("/api/portfolio/history", params={"days": "abc"}).status_code == 422


def test_history_empty(tmp_path):
    assert make_client(tmp_path).get("/api/portfolio/history").json() == {"days": 30, "count": 0, "points": []}


# -- GET /api/settings ----------------------------------------------------------------
def test_settings_mask_credentials_and_list_wallets(tmp_path):
    sections = {
        "binance": {"enabled": True, "api_key": API_KEY, "api_secret": SECRET},
        "wallets": {
            "evm": [{"address": EVM_A, "label": "ledger", "chains": ["base", "ethereum"]},
                    {"address": EVM_B}],
            "solana": [{"address": SOL_A, "label": "phantom"}],
        },
    }
    response = make_client(tmp_path, sections=sections).get("/api/settings")
    assert SECRET not in response.text and API_KEY not in response.text
    data = response.json()
    binance = data["binance"]
    assert binance["api_secret_masked"] == "****" + SECRET[-4:]
    assert binance["api_key_masked"] == API_KEY[:4] + "****" + API_KEY[-4:]
    assert binance["has_api_key"] and binance["has_api_secret"] and binance["configured"]
    assert binance["env_override"] is False

    wallets = {w["address"]: w for w in data["wallets"]}
    assert wallets[EVM_A]["chains"] == ["ethereum", "base"]          # canonical chain order
    assert wallets[EVM_A]["name"] == "ledger" and wallets[EVM_A]["type"] == "evm"
    assert wallets[EVM_B]["chains"] == ["ethereum", "bsc", "arbitrum", "base", "polygon"]  # none = all
    assert wallets[SOL_A]["type"] == "solana" and wallets[SOL_A]["chains"] == ["solana"]
    assert [c["id"] for c in data["chains"]] == ["ethereum", "bsc", "arbitrum", "base", "polygon", "solana"]
    assert data["config_exists"] is True
    assert data["auth_required"] is False


def test_settings_report_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "from-env-key")
    monkeypatch.setenv("EVM_WALLETS", EVM_B)
    data = make_client(tmp_path).get("/api/settings").json()
    assert data["binance"]["env_override"] is True
    assert data["wallets_env_override"] is True
    assert "BINANCE_API_KEY" in data["env_overrides"]


def test_mask_helpers():
    assert web_api.mask_secret("") == ""
    assert web_api.mask_secret("short") == "****"
    assert web_api.mask_secret("0123456789abcdef") == "****cdef"
    assert web_api.mask_key("abcdefghijklmnop") == "abcd****mnop"
    assert web_api.mask_key("tiny") == "****"


# -- POST /api/settings ---------------------------------------------------------------
def test_save_settings_writes_yaml_and_keeps_other_sections(tmp_path):
    sections = {
        "binance": {"enabled": True, "api_key": "old-key", "api_secret": "old-secret", "recv_window": 7000},
        "wallets": {"evm": [{"address": EVM_A, "label": "old", "tokens": {"base": [{"symbol": "AERO", "address": EVM_B}]}}]},
        "chains": {"ethereum": {"tokens": [{"symbol": "USDC", "address": EVM_B, "decimals": 6}]}},
    }
    client = make_client(tmp_path, sections=sections)
    body = {
        "binance": {"enabled": True, "api_key": API_KEY, "api_secret": SECRET},
        "wallets": [
            {"name": "Ledger", "address": EVM_A, "chains": ["base", "ethereum"]},
            {"name": "", "address": SOL_A, "chains": ["solana"]},
        ],
    }
    response = client.post("/api/settings", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["saved"] is True
    assert SECRET not in response.text

    path = tmp_path / "config.yaml"
    raw = yaml.safe_load(path.read_text())
    assert raw["binance"] == {"enabled": True, "api_key": API_KEY, "api_secret": SECRET, "recv_window": 7000}
    assert raw["wallets"]["evm"] == [{
        "address": EVM_A, "label": "Ledger", "chains": ["ethereum", "base"],
        "tokens": {"base": [{"symbol": "AERO", "address": EVM_B}]},    # carried over, not lost
    }]
    assert raw["wallets"]["solana"] == [{"address": SOL_A, "label": SOL_A[:6] + ".." + SOL_A[-4:]}]
    assert raw["chains"] == sections["chains"]
    assert raw["settings"]["db_path"].endswith("history.db")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    backup = tmp_path / "config.yaml.bak"
    assert backup.is_file() and "old-secret" in backup.read_text()

    # the saved file loads back as a working config, addresses intact (quoted, not ints)
    loaded = config_module.load_config(path)
    assert loaded.binance_enabled
    assert loaded.evm_wallets[0]["address"] == EVM_A
    assert loaded.solana_wallets[0]["address"] == SOL_A


def test_save_settings_keeps_credentials_left_blank_or_masked(tmp_path):
    client = make_client(tmp_path, sections=CONFIGURED)
    for body in ({"binance": {"enabled": True}},
                 {"binance": {"enabled": True, "api_key": None, "api_secret": None}},
                 {"binance": {"enabled": True, "api_key": "AbCd****6789", "api_secret": "****9f3a"}}):
        assert client.post("/api/settings", json=body).status_code == 200
        raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert raw["binance"]["api_key"] == API_KEY
        assert raw["binance"]["api_secret"] == SECRET
    # wallets untouched when the body has no `wallets`
    assert raw["wallets"]["evm"][0]["address"] == EVM_A


def test_save_settings_empty_string_clears_credentials(tmp_path):
    client = make_client(tmp_path, sections=CONFIGURED)
    data = client.post("/api/settings", json={"binance": {"enabled": False, "api_key": "", "api_secret": ""}}).json()
    assert data["binance"]["has_api_key"] is False
    assert data["binance"]["has_api_secret"] is False
    assert data["binance"]["enabled"] is False
    assert data["binance"]["configured"] is False


def test_save_settings_creates_a_missing_config_file(tmp_path):
    cfg = tmp_path / "fresh" / "config.yaml"
    client = TestClient(web_api.create_app(cfg, fetcher=fake_fetcher()))
    assert client.get("/api/settings").json()["config_exists"] is False
    data = client.post("/api/settings", json={"wallets": [{"name": "w", "address": EVM_A, "chains": []}]}).json()
    assert data["config_exists"] is True
    assert data["wallets"][0]["chains"] == ["ethereum", "bsc", "arbitrum", "base", "polygon"]
    assert yaml.safe_load(cfg.read_text())["wallets"]["evm"][0]["chains"] == [
        "ethereum", "bsc", "arbitrum", "base", "polygon"]


@pytest.mark.parametrize("wallets, message", [
    ([{"address": "not-an-address"}], "not a valid"),
    ([{"address": EVM_A}, {"address": EVM_A.upper().replace("0X", "0x")}], "listed twice"),
    ([{"address": EVM_A, "chains": ["solana"]}], "Solana"),
    ([{"address": SOL_A, "chains": ["ethereum"]}], "Solana"),
    ([{"address": EVM_A, "chains": ["dogechain"]}], "Unknown chain"),
])
def test_save_settings_rejects_bad_wallets_without_writing(tmp_path, wallets, message):
    client = make_client(tmp_path, sections=CONFIGURED)
    before = (tmp_path / "config.yaml").read_text()
    response = client.post("/api/settings", json={"wallets": wallets})
    assert response.status_code == 422
    assert message in response.json()["detail"]
    assert (tmp_path / "config.yaml").read_text() == before


def test_save_settings_validates_body_shape(tmp_path):
    client = make_client(tmp_path)
    assert client.post("/api/settings", json={"wallets": [{"name": "x" * 41, "address": EVM_A}]}).status_code == 422
    assert client.post("/api/settings", json={"wallets": "nope"}).status_code == 422


def test_save_settings_drops_the_live_cache(tmp_path):
    client = make_client(tmp_path, sections=CONFIGURED)
    client.post("/api/portfolio/refresh")
    assert client.get("/api/portfolio").json()["origin"] == "live"
    client.post("/api/settings", json={"wallets": []})
    assert client.get("/api/portfolio").json()["origin"] == "snapshot"


def test_settings_on_invalid_yaml_is_a_clear_500(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("- just\n- a list\n")
    client = TestClient(web_api.create_app(cfg, fetcher=fake_fetcher()))
    response = client.get("/api/settings")
    assert response.status_code == 500
    assert "invalid" in response.json()["detail"]


# -- POST /api/settings/test-binance ---------------------------------------------------
RESTRICTIONS = {
    "ipRestrict": False, "enableReading": True, "enableWithdrawals": False,
    "enableSpotAndMarginTrading": False, "enableFutures": False, "enableMargin": False,
    "enableInternalTransfer": False,
}


def test_binance_check_with_a_read_only_key(tmp_path):
    session = binance_session(**{"/sapi/v1/account/apiRestrictions": {**RESTRICTIONS, "ipRestrict": True}})
    client = make_client(tmp_path, session=session)
    data = client.post("/api/settings/test-binance", json={"api_key": "k" * 20, "api_secret": "s" * 20}).json()
    assert data["ok"] is True and data["status"] == "connected"
    assert data["permissions"]["reading"] is True
    assert data["warnings"] == []
    signed = [c for c in session.calls if "apiRestrictions" in c[1]]
    assert signed and "signature" in signed[0][2]
    assert session.headers["X-MBX-APIKEY"] == "k" * 20


def test_binance_check_warns_about_trading_and_withdrawals(tmp_path):
    session = binance_session(**{"/sapi/v1/account/apiRestrictions": {
        **RESTRICTIONS, "enableWithdrawals": True, "enableSpotAndMarginTrading": True}})
    data = make_client(tmp_path, session=session).post(
        "/api/settings/test-binance", json={"api_key": "k" * 20, "api_secret": "s" * 20}).json()
    assert data["ok"] is True
    assert "withdrawals" in data["warnings"][0] and "spot & margin trading" in data["warnings"][0]
    assert any("IP-restricted" in w for w in data["warnings"])


def test_binance_check_without_read_permission_fails(tmp_path):
    session = binance_session(**{"/sapi/v1/account/apiRestrictions": {**RESTRICTIONS, "enableReading": False}})
    data = make_client(tmp_path, session=session).post(
        "/api/settings/test-binance", json={"api_key": "k" * 20, "api_secret": "s" * 20}).json()
    assert data["ok"] is False and data["status"] == "no_read"


def test_binance_check_falls_back_to_stored_credentials(tmp_path):
    session = binance_session(**{"/sapi/v1/account/apiRestrictions": RESTRICTIONS})
    client = make_client(tmp_path, sections=CONFIGURED, session=session)
    data = client.post("/api/settings/test-binance", json={"api_key": None, "api_secret": "****9f3a"}).json()
    assert data["ok"] is True
    assert session.headers["X-MBX-APIKEY"] == API_KEY
    assert client.post("/api/settings/test-binance").json()["ok"] is True   # no body at all


def test_binance_check_needs_credentials(tmp_path):
    response = make_client(tmp_path).post("/api/settings/test-binance", json={})
    assert response.status_code == 400


class RejectingSession(FakeSession):
    """Binance answering 401 / -2015 to every signed call."""

    def request(self, method, url, params=None, timeout=None):
        if "/sapi/" in url:
            self.calls.append((method, url, params or {}))
            return FakeResponse({"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}, status=401)
        return super().request(method, url, params, timeout)


def test_binance_check_explains_a_rejected_key(tmp_path):
    session = RejectingSession({"/api/v3/time": {"serverTime": 1_700_000_000_000}})
    data = make_client(tmp_path, session=session).post(
        "/api/settings/test-binance", json={"api_key": "k" * 20, "api_secret": "s" * 20}).json()
    assert data["ok"] is False and data["status"] == "rejected"
    assert "Enable Reading" in data["message"]


def test_binance_check_reports_an_unreachable_api(tmp_path):
    session = FakeSession({"binance": requests.ConnectionError("no route")})
    data = make_client(tmp_path, session=session).post(
        "/api/settings/test-binance", json={"api_key": "k" * 20, "api_secret": "s" * 20}).json()
    assert data["ok"] is False and data["status"] == "unreachable"


# -- POST /api/settings/test-wallet ----------------------------------------------------
TEST_CHAINS = {"chains": {
    "ethereum": {"rpcs": ["https://rpc-a.test"]},
    "base": {"rpcs": ["https://rpc-b.test"]},
    "arbitrum": {"rpcs": ["https://rpc-dead.test"]},
    "solana": {"rpcs": ["https://sol.test"]},
}}


def test_wallet_check_reads_native_balance_per_chain(tmp_path):
    client = make_client(tmp_path, sections=TEST_CHAINS, session=onchain_session())
    data = client.post("/api/settings/test-wallet", json={"address": EVM_A, "chains": ["base", "ethereum"]}).json()
    assert data["ok"] is True and data["type"] == "evm"
    assert data["reachable"] == data["total"] == 2
    by_chain = {r["chain"]: r for r in data["results"]}
    assert list(by_chain) == ["ethereum", "base"]
    for row in by_chain.values():
        assert row["ok"] and row["balance"] == pytest.approx(2.0) and row["symbol"] == "ETH"
        assert row["value_usd"] == pytest.approx(6000.0)
        assert "_price_id" not in row


def test_wallet_check_reports_a_dead_chain_without_failing_the_others(tmp_path):
    session = onchain_session(fail_rpcs=("rpc-dead.test",))
    client = make_client(tmp_path, sections=TEST_CHAINS, session=session)
    data = client.post("/api/settings/test-wallet", json={"address": EVM_A, "chains": ["ethereum", "arbitrum"]}).json()
    assert data["ok"] is False
    assert data["reachable"] == 1 and data["total"] == 2
    dead = next(r for r in data["results"] if r["chain"] == "arbitrum")
    assert dead["ok"] is False and "refused" in dead["error"]


def test_wallet_check_solana(tmp_path):
    client = make_client(tmp_path, sections=TEST_CHAINS, session=onchain_session())
    data = client.post("/api/settings/test-wallet", json={"address": SOL_A}).json()
    assert data["type"] == "solana"
    assert data["results"][0]["balance"] == pytest.approx(3.0)
    assert data["results"][0]["value_usd"] == pytest.approx(450.0)


def test_wallet_check_rejects_bad_input(tmp_path):
    client = make_client(tmp_path, sections=TEST_CHAINS, session=onchain_session())
    assert client.post("/api/settings/test-wallet", json={"address": "0x123"}).status_code == 422
    assert client.post("/api/settings/test-wallet", json={"address": EVM_A, "chains": ["solana"]}).status_code == 422
    assert client.post("/api/settings/test-wallet", json={}).status_code == 422


def test_address_kind():
    assert web_api.address_kind(EVM_A) == "evm"
    assert web_api.address_kind(SOL_A) == "solana"
    assert web_api.address_kind("0x" + "zz" * 20) is None
    assert web_api.address_kind("O0Il" * 10) is None        # not base58
    assert web_api.address_kind("") is None


# -- access token ---------------------------------------------------------------------
def test_token_locks_the_api_but_not_the_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_WEB_TOKEN", "letmein")
    client = make_client(tmp_path, sections=CONFIGURED)
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    for path in ("/api/portfolio", "/api/settings", "/api/portfolio/history"):
        assert client.get(path).status_code == 401, path
    assert client.post("/api/portfolio/refresh").status_code == 401
    assert client.get("/api/portfolio", headers={"Authorization": "Bearer letmein"}).status_code == 200
    assert client.get("/api/portfolio", headers={"X-Portfolio-Token": "letmein"}).status_code == 200
    assert client.get("/api/portfolio", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_auth_endpoint_sets_a_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_WEB_TOKEN", "letmein")
    client = make_client(tmp_path)
    assert client.post("/api/auth", json={"token": "wrong"}).status_code == 401
    response = client.post("/api/auth", json={"token": "letmein"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/settings").json()["auth_required"] is True


def test_token_in_url_becomes_a_cookie_and_leaves_the_url(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_WEB_TOKEN", "letmein")
    client = make_client(tmp_path)
    response = client.get("/?token=letmein", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert "portfolio_token=letmein" in response.headers["set-cookie"]
    assert client.get("/api/portfolio").status_code == 200

    fresh = make_client(tmp_path)
    bad = fresh.get("/?token=nope", follow_redirects=False)
    assert bad.status_code == 303 and "set-cookie" not in bad.headers


def test_token_from_config_file(tmp_path):
    client = make_client(tmp_path, sections={"settings": {"db_path": str(tmp_path / "h.db"), "web_token": "cfg-token"}})
    assert client.get("/api/portfolio").status_code == 401
    assert client.get("/api/portfolio", headers={"Authorization": "Bearer cfg-token"}).status_code == 200


def test_auth_without_a_token_configured_is_a_no_op(tmp_path):
    response = make_client(tmp_path).post("/api/auth", json={"token": "anything"})
    assert response.json() == {"ok": True, "required": False}
    assert "set-cookie" not in response.headers


# -- CLI `serve` ------------------------------------------------------------------------
def test_cli_serve_parses_and_launches_uvicorn(tmp_path, monkeypatch, capsys):
    import uvicorn

    launched = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: launched.update(app=app, **kw))
    cfg = write_config(tmp_path / "config.yaml")
    assert cli.main(["serve", "--config", str(cfg), "--host", "0.0.0.0", "--port", "8181", "--min-value", "0"]) == 0
    assert launched["host"] == "0.0.0.0" and launched["port"] == 8181
    assert launched["app"].state.config_path == cfg
    assert launched["app"].state.min_value == 0.0
    assert "without an access token" in capsys.readouterr().err    # LAN exposure is called out

    launched.clear()
    assert cli.main(["serve", "--config", str(cfg)]) == 0
    assert launched["host"] == "127.0.0.1" and launched["port"] == 8080
    assert launched["app"].state.min_value is None                   # config value, re-read per request
    assert "without an access token" not in capsys.readouterr().err


def test_module_level_app_for_uvicorn_cli():
    assert isinstance(web_api.app, web_api.FastAPI)
    paths = {route.path for route in web_api.app.routes}
    assert {"/api/portfolio", "/api/portfolio/refresh", "/api/portfolio/history", "/api/settings",
            "/api/settings/test-binance", "/api/settings/test-wallet", "/", "/static"} <= paths


def test_load_config_missing_ok(tmp_path):
    with pytest.raises(FileNotFoundError):
        config_module.load_config(tmp_path / "nope.yaml")
    assert config_module.load_config(tmp_path / "nope.yaml", missing_ok=True).path is None


def test_history_resampled_puts_one_point_per_bucket(tmp_path):
    client = make_client(tmp_path, sections=CONFIGURED)
    now = datetime.now(timezone.utc)
    db = tmp_path / "history.db"
    seed(db, now - timedelta(days=20), 0.8)
    for minutes in (90, 60, 30, 5):
        seed(db, now - timedelta(minutes=minutes), 1.0)

    raw = client.get("/api/portfolio/history", params={"days": 30}).json()
    assert raw["count"] == 5 and "interval" not in raw            # default stays every snapshot

    body = client.get("/api/portfolio/history", params={"days": 30, "interval": "1d"}).json()
    assert body["interval"] == "1d"
    points = body["points"]
    assert 20 <= len(points) <= 22                                 # one per day, not 2 + 4 hourly
    assert points[0]["filled"] is False and points[-1]["filled"] is False
    assert points[-1]["time"] == raw["points"][-1]["time"]        # still ends on the newest snapshot
    assert sum(not p["filled"] for p in points) <= 3


def test_history_rejects_bad_interval(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/api/portfolio/history", params={"interval": "2d"}).status_code == 422
    assert client.get("/api/portfolio/history", params={"tz_offset": 9999}).status_code == 422
