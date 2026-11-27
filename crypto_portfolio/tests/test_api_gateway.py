"""Tests for api_gateway.py:  .venv/bin/python -m pytest test_api_gateway.py -q"""
from __future__ import annotations

import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_gateway
from api_gateway import ENDPOINT, Settings, connect_readonly, create_app, load_settings

KEY = "k" * 43
NOT_FOUND = {"detail": "Not Found"}

SCHEMA = """
CREATE TABLE snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
    total_portfolio_usd REAL NOT NULL DEFAULT 0, binance_spot_usd REAL NOT NULL DEFAULT 0,
    binance_earn_usd REAL NOT NULL DEFAULT 0, binance_funding_usd REAL NOT NULL DEFAULT 0,
    onchain_usd REAL NOT NULL DEFAULT 0);
"""


def make_db(path: Path, *rows: tuple) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.executemany(
        "INSERT INTO snapshots (timestamp, total_portfolio_usd, binance_spot_usd, binance_earn_usd,"
        " binance_funding_usd, onchain_usd) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    connection.commit()
    connection.close()
    return path


def iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) - delta).replace(microsecond=0).isoformat()


def client_for(db_path: Path, **overrides) -> TestClient:
    settings = Settings(
        api_key=KEY,
        db_path=db_path,
        allowed_hosts=frozenset({"testserver", "api.tytony.link"}),
        **overrides,
    )
    return TestClient(create_app(settings), raise_server_exceptions=False)


@pytest.fixture
def db(tmp_path):
    return make_db(
        tmp_path / "p.db",
        (iso(timedelta(hours=5)), 1.0, 0, 0, 0, 1.0),
        (iso(timedelta(minutes=2)), 51234.555, 12345.674, 23456.775, 1234.56, 14197.546),
    )


@pytest.fixture
def client(db):
    return client_for(db)


# -- concealment ------------------------------------------------------------------------
@pytest.mark.parametrize(
    "headers",
    [{}, {"X-API-Key": ""}, {"X-API-Key": "wrong"}, {"X-API-Key": KEY + "x"}, {"X-API-Key": ("é" * 43).encode()}],
)
def test_bad_or_missing_key_is_plain_404(client, headers):
    response = client.get(ENDPOINT, headers=headers)
    assert response.status_code == 404
    assert response.json() == NOT_FOUND


def test_404_is_identical_to_unknown_route(client):
    a = client.get(ENDPOINT, headers={"X-API-Key": "nope"})
    b = client.get("/does-not-exist")
    assert (a.status_code, a.content) == (b.status_code, b.content)


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect", "/", "/api/settings"])
def test_no_docs_or_other_routes_even_with_key(client, path):
    response = client.get(path, headers={"X-API-Key": KEY})
    assert response.status_code == 404 and response.json() == NOT_FOUND


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_other_methods_are_404_not_405(client, method):
    response = client.request(method, ENDPOINT, headers={"X-API-Key": KEY})
    assert response.status_code == 404


def test_invalid_params_without_key_do_not_leak_422(client):
    response = client.get(ENDPOINT, params={"currency": "x" * 50})
    assert response.status_code == 404


def test_disallowed_host_is_404(client):
    response = client.get(ENDPOINT, headers={"X-API-Key": KEY, "Host": "evil.example"})
    assert response.status_code == 404


# -- happy path -------------------------------------------------------------------------
def test_returns_latest_snapshot(client):
    response = client.get(ENDPOINT, params={"currency": "USD"}, headers={"X-API-Key": KEY})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["value"] == "51234.56"
    assert body["currency"] == "USD"
    assert body["as_of"].endswith("Z") and "+" not in body["as_of"]
    assert body["stale"] is False
    assert body["sources"] == [
        {"id": "binance-spot", "status": "ok", "value": "12345.67"},
        {"id": "binance-earn", "status": "ok", "value": "23456.78"},
        {"id": "binance-funding", "status": "ok", "value": "1234.56"},
        {"id": "onchain", "status": "ok", "value": "14197.55"},
    ]


def test_currency_defaults_to_usd_and_is_case_insensitive(client):
    assert client.get(ENDPOINT, headers={"X-API-Key": KEY}).json()["currency"] == "USD"
    assert client.get(ENDPOINT, params={"currency": "usd"}, headers={"X-API-Key": KEY}).status_code == 200


def test_unsupported_currency_is_400_once_authorised(client):
    response = client.get(ENDPOINT, params={"currency": "EUR"}, headers={"X-API-Key": KEY})
    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_currency"


def test_stale_flag_and_zero_source(tmp_path):
    db = make_db(tmp_path / "old.db", (iso(timedelta(minutes=31)), 100.0, 0.0, 100.0, 0.0, 0.0))
    body = client_for(db).get(ENDPOINT, headers={"X-API-Key": KEY}).json()
    assert body["stale"] is True
    assert body["sources"][0] == {"id": "binance-spot", "status": "empty", "value": "0.00"}


def test_naive_timestamp_is_treated_as_utc(tmp_path):
    db = make_db(tmp_path / "naive.db", ("2026-11-23T05:30:12", 1.0, 0, 0, 0, 1.0))
    assert client_for(db).get(ENDPOINT, headers={"X-API-Key": KEY}).json()["as_of"] == "2026-11-23T05:30:12Z"


# -- failure modes ----------------------------------------------------------------------
def test_empty_db_is_503(tmp_path):
    response = client_for(make_db(tmp_path / "empty.db")).get(ENDPOINT, headers={"X-API-Key": KEY})
    assert response.status_code == 503 and response.json()["error"] == "no_data"


def test_missing_db_is_503_and_never_created(tmp_path):
    missing = tmp_path / "missing.db"
    response = client_for(missing).get(ENDPOINT, headers={"X-API-Key": KEY})
    assert response.status_code == 503
    assert not missing.exists()


def test_connection_is_read_only(db):
    connection = connect_readonly(db)
    with pytest.raises(sqlite3.OperationalError):
        connection.execute("INSERT INTO snapshots (timestamp) VALUES ('x')")
    connection.close()


# -- rate limiting ----------------------------------------------------------------------
def test_rate_limit_429_for_valid_key_404_for_invalid(db):
    client = client_for(db, rate_limit_per_minute=3)
    ip = {"CF-Connecting-IP": "203.0.113.7"}
    for _ in range(3):
        assert client.get(ENDPOINT, headers={"X-API-Key": KEY, **ip}).status_code == 200
    limited = client.get(ENDPOINT, headers={"X-API-Key": KEY, **ip})
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
    assert client.get(ENDPOINT, headers={"X-API-Key": "bad", **ip}).status_code == 404
    other_ip = {"CF-Connecting-IP": "198.51.100.1"}
    assert client.get(ENDPOINT, headers={"X-API-Key": KEY, **other_ip}).status_code == 200


def test_limiter_stays_bounded():
    limiter = api_gateway.SlidingWindowLimiter(limit=1, max_clients=100)
    for i in range(1000):
        limiter.check(f"10.0.{i // 256}.{i % 256}")
    assert len(limiter._hits) <= 100


# -- config -----------------------------------------------------------------------------
def test_env_file_is_generated_private_and_stable(tmp_path, monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith("GATEWAY_"):
            monkeypatch.delenv(name)
    env_file = tmp_path / ".env.gateway"
    first = load_settings(env_file)
    assert len(first.api_key) >= 43
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert load_settings(env_file).api_key == first.api_key
    assert first.port == 8090 and first.host == "127.0.0.1"


def test_env_var_overrides_and_loopback_enforced(tmp_path, monkeypatch):
    env_file = tmp_path / ".env.gateway"
    monkeypatch.setenv("GATEWAY_API_KEY", KEY)
    monkeypatch.setenv("GATEWAY_PORT", "9000")
    assert load_settings(env_file).port == 9000
    assert not env_file.exists()  # key came from the environment: nothing generated
    monkeypatch.setenv("GATEWAY_HOST", "0.0.0.0")
    with pytest.raises(SystemExit):
        load_settings(env_file)


def test_short_key_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "short")
    with pytest.raises(SystemExit):
        load_settings(tmp_path / ".env.gateway")
