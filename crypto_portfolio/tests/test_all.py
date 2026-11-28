"""Test suite: every module imports and behaves, with mocked HTTP - no network, no keys.

    .venv/bin/pytest test_all.py -q        (or: .venv/bin/python test_all.py)

Covers the self-review checklist: unreachable network / dropped RPCs, Simple Earn
pagination, dust filtering, zero balances, and chart generation with 0 and 1 snapshots.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as config_module
import main as cli
from clients.binance import BinanceClient, BinanceError
from clients.onchain import OnChainClient, RpcError, _decode_abi_string, _hex_to_int
from models import BINANCE_EARN, BINANCE_FUNDING, BINANCE_SPOT, ONCHAIN, Holding
from services import db as db_module
from services.db import (
    MIGRATIONS,
    SCHEMA,
    SCHEMA_VERSION,
    Database,
    MigrationError,
    migrate,
    schema_version,
    split_statements,
)
from services.portfolio import aggregate, to_dict
from services.visualizer import bar, generate, sparkline, text_report


# -- fakes ---------------------------------------------------------------------
class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.ok = 200 <= status < 300
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Routes requests by substring match; records every call for assertions."""

    def __init__(self, routes: dict, post_routes: dict | None = None):
        self.routes = routes
        self.post_routes = post_routes or {}
        self.headers = {}
        self.calls: list[tuple[str, str, dict]] = []

    def _match(self, table, url):
        for fragment, payload in table.items():
            if fragment in url:
                return payload
        return None

    def request(self, method, url, params=None, timeout=None):
        self.calls.append((method, url, params or {}))
        table = self.post_routes if method == "POST" and self._match(self.post_routes, url) else self.routes
        payload = self._match(table, url)
        if payload is None:
            return FakeResponse({"code": -1121, "msg": "unknown"}, status=404)
        if isinstance(payload, Exception):
            raise payload
        if callable(payload):
            payload = payload(params or {})
        return FakeResponse(payload)

    def get(self, url, timeout=None, **kwargs):
        return self.request("GET", url, kwargs.get("params"), timeout)

    def post(self, url, json=None, timeout=None, **kwargs):
        self.calls.append(("POST", url, json or {}))
        payload = self._match(self.routes, url)
        if isinstance(payload, Exception):
            raise payload
        if callable(payload):
            payload = payload(json or {})
        if payload is None:
            return FakeResponse(None, status=502)
        return FakeResponse(payload)


TICKERS = [
    {"symbol": "BTCUSDT", "price": "60000.0"},
    {"symbol": "ETHUSDT", "price": "3000.0"},
    {"symbol": "SOLUSDT", "price": "150.0"},
    {"symbol": "DUSTUSDT", "price": "0.0001"},
    {"symbol": "ALTBTC", "price": "0.01"},
]


def binance_session(**overrides):
    routes = {
        "/api/v3/time": {"serverTime": 1_700_000_000_000},
        "/api/v3/ticker/price": TICKERS,
        "/api/v3/account": {"balances": [
            {"asset": "BTC", "free": "0.5", "locked": "0.1"},
            {"asset": "USDT", "free": "250.0", "locked": "0"},
            {"asset": "ZERO", "free": "0", "locked": "0"},          # zero balance: dropped
            {"asset": "DUST", "free": "1000", "locked": "0"},       # $0.10: dust
        ]},
        "/simple-earn/flexible/position": {"rows": [
            {"asset": "ETH", "totalAmount": "2.0"},
            {"asset": "USDT", "totalAmount": "0"},                  # zero position: dropped
        ], "total": 2},
        "/simple-earn/locked/position": {"rows": [{"asset": "SOL", "amount": "10.0"}], "total": 1},
        "/asset/get-funding-asset": [
            {"asset": "USDT", "free": "100", "locked": "0", "freeze": "10", "withdrawing": "0"},
        ],
    }
    routes.update(overrides)
    return FakeSession(routes)


def make_client(session=None):
    return BinanceClient("key", "secret", session=session or binance_session())


# -- config --------------------------------------------------------------------
def test_config_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "missing.yaml")
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    cfg = config_module.load_config()
    assert cfg.min_value == 1.0
    assert "ethereum" in cfg.chains and cfg.chains["ethereum"]["rpcs"]
    assert not cfg.binance_enabled  # enabled, but no keys -> nothing to call


def test_config_env_overrides_yaml(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        "binance:\n  api_key: from_yaml\n  api_secret: yaml_secret\n"
        "settings:\n  min_value: 5.0\n"
        'wallets:\n  evm:\n    - "0xabc"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BINANCE_API_KEY", "from_env")
    monkeypatch.setenv("PORTFOLIO_MIN_VALUE", "2.5")
    cfg = config_module.load_config(path)
    assert cfg.binance["api_key"] == "from_env"      # env wins
    assert cfg.binance["api_secret"] == "yaml_secret"  # yaml kept where env is silent
    assert cfg.min_value == 2.5
    assert cfg.evm_wallets[0]["address"] == "0xabc"  # bare string normalised to a dict
    assert cfg.binance_enabled


def test_config_unquoted_hex_address_is_recovered(tmp_path, monkeypatch):
    """YAML turns a bare 0x… address into an integer; config must hand back the address."""
    monkeypatch.delenv("EVM_WALLETS", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(
        "wallets:\n  evm:\n    - 0xdac17f958d2ee523a2206206994597c13d831ec7\n"
        "chains:\n  ethereum:\n    tokens:\n"
        "      - { symbol: USDC, address: 0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48, decimals: 6 }\n",
        encoding="utf-8",
    )
    cfg = config_module.load_config(path)
    assert cfg.evm_wallets[0]["address"] == "0xdac17f958d2ee523a2206206994597c13d831ec7"
    assert cfg.chains["ethereum"]["tokens"][0]["address"] == "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def test_config_missing_explicit_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        config_module.load_config(tmp_path / "nope.yaml")


def test_config_bad_min_value_env_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "missing.yaml")
    monkeypatch.setenv("PORTFOLIO_MIN_VALUE", "not-a-number")
    assert config_module.load_config().min_value == 1.0


# -- binance -------------------------------------------------------------------
def test_binance_signature_and_headers():
    session = binance_session()
    client = make_client(session)
    client.spot_balances()
    signed = [c for c in session.calls if "/api/v3/account" in c[1]][0]
    assert len(signed[2]["signature"]) == 64          # hex sha256
    assert "timestamp" in signed[2] and "recvWindow" in signed[2]
    assert session.headers["X-MBX-APIKEY"] == "key"


def test_binance_spot_skips_zero_balances():
    holdings = make_client().spot_balances()
    symbols = {h.symbol for h in holdings}
    assert symbols == {"BTC", "USDT", "DUST"}
    assert next(h for h in holdings if h.symbol == "BTC").quantity == pytest.approx(0.6)
    assert all(h.source == BINANCE_SPOT for h in holdings)


def test_binance_earn_pagination_walks_every_page():
    """100 rows then 40: the client must ask for page 2 and stop at page 3."""
    def paged(params):
        page = int(params.get("current", 1))
        if page == 1:
            return {"rows": [{"asset": "ETH", "totalAmount": "1"} for _ in range(100)], "total": 140}
        if page == 2:
            return {"rows": [{"asset": "ETH", "totalAmount": "1"} for _ in range(40)], "total": 140}
        raise AssertionError("asked for a page past the end")

    session = binance_session(**{"/simple-earn/flexible/position": paged})
    holdings = make_client(session).earn_flexible()
    assert len(holdings) == 140
    pages = [c[2]["current"] for c in session.calls if "flexible/position" in c[1]]
    assert pages == [1, 2]


def test_binance_earn_pagination_stops_on_short_page():
    session = binance_session(**{
        "/simple-earn/flexible/position": {"rows": [{"asset": "ETH", "totalAmount": "1"}], "total": 999}
    })
    client = make_client(session)
    assert len(client.earn_flexible()) == 1  # `total` lies; the short page is authoritative


def test_binance_locked_accepts_amount_or_total_amount():
    session = binance_session(**{"/simple-earn/locked/position": {"rows": [
        {"asset": "SOL", "amount": "3"},
        {"asset": "DOT", "totalAmount": "7"},
        {"asset": "ADA", "amount": "0"},
    ], "total": 3}})
    holdings = make_client(session).earn_locked()
    assert {(h.symbol, h.quantity) for h in holdings} == {("SOL", 3.0), ("DOT", 7.0)}
    assert all(h.source == BINANCE_EARN for h in holdings)


def test_binance_funding_sums_every_bucket():
    holdings = make_client().funding_balances()
    assert len(holdings) == 1
    assert holdings[0].source == BINANCE_FUNDING
    assert holdings[0].quantity == pytest.approx(110.0)  # free + freeze


def test_binance_pricing_stablecoins_and_bridges():
    client = make_client()
    assert client.price_usd("USDT") == 1.0
    assert client.price_usd("FDUSD") == 1.0
    assert client.price_usd("BTC") == 60000.0
    assert client.price_usd("ALT") == pytest.approx(0.01 * 60000)  # priced via ALTBTC
    assert client.price_usd("NOSUCHCOIN") == 0.0                   # unknown, never a guess


def test_binance_fetch_all_prices_everything():
    holdings, errors = make_client().fetch_all({"USDT", "USDC", "FDUSD"})
    assert errors == []
    total = sum(h.value_usd for h in holdings)
    # 0.6 BTC + 250 USDT + 1000 DUST spot, 2 ETH + 10 SOL earn, 110 USDT funding
    assert total == pytest.approx(0.6 * 60000 + 250 + 1000 * 0.0001 + 2 * 3000 + 10 * 150 + 110)


def test_binance_network_down_reports_errors_without_raising():
    session = FakeSession({"": requests.ConnectionError("network is unreachable")})
    holdings, errors = BinanceClient("k", "s", session=session).fetch_all()
    assert holdings == []
    assert len(errors) >= 4  # one per section, plus pricing
    assert all("unreachable" in e for e in errors)


def test_binance_bad_key_raises_binance_error():
    # no /api/v3/account route -> the stub answers 404, which must not be retried
    session = FakeSession({"/api/v3/time": {"serverTime": 1}})
    with pytest.raises(BinanceError):
        BinanceClient("k", "s", session=session).spot_balances()
    assert len([c for c in session.calls if "/api/v3/account" in c[1]]) == 1


def test_binance_retries_on_rate_limit(monkeypatch):
    monkeypatch.setattr("clients.binance.time.sleep", lambda _s: None)
    state = {"n": 0}

    class RateLimited(FakeSession):
        def request(self, method, url, params=None, timeout=None):
            if "/api/v3/account" in url:
                state["n"] += 1
                if state["n"] == 1:
                    return FakeResponse({"code": -1003, "msg": "too many"}, 429, {"Retry-After": "0"})
            return super().request(method, url, params, timeout)

    client = BinanceClient("k", "s", session=RateLimited(binance_session().routes))
    assert client.spot_balances()        # recovered on the retry
    assert state["n"] == 2


def test_binance_clock_skew_triggers_resync(monkeypatch):
    monkeypatch.setattr("clients.binance.time.sleep", lambda _s: None)
    state = {"n": 0}

    class Skewed(FakeSession):
        def request(self, method, url, params=None, timeout=None):
            if "/api/v3/account" in url:
                state["n"] += 1
                if state["n"] == 1:
                    return FakeResponse({"code": -1021, "msg": "timestamp ahead"}, 400)
            return super().request(method, url, params, timeout)

    assert BinanceClient("k", "s", session=Skewed(binance_session().routes)).spot_balances()
    assert state["n"] == 2


# -- on-chain ------------------------------------------------------------------
CHAINS = {
    "ethereum": {"rpcs": ["https://rpc-a.test", "https://rpc-b.test"], "native_symbol": "ETH",
                 "native_price_id": "coingecko:ethereum", "llama_chain": "ethereum",
                 "tokens": [{"symbol": "USDC", "address": "0xA0b8", "decimals": 6}]},
    "solana": {"rpcs": ["https://sol.test"], "native_symbol": "SOL",
               "native_price_id": "coingecko:solana", "llama_chain": "solana"},
}


def evm_result(method, params):
    if method == "eth_getBalance":
        return hex(2 * 10 ** 18)                       # 2 ETH
    if method == "eth_call":
        data = params[0]["data"]
        if data.startswith("0x70a08231"):
            return hex(1500 * 10 ** 6)                 # 1500 USDC
        if data.startswith("0x313ce567"):
            return hex(6)
        if data.startswith("0x95d89b41"):
            return "0x" + "USDC".encode().hex().ljust(64, "0")  # bytes32 symbol
    return "0x0"


def onchain_session(sol_accounts=None, fail_rpcs=()):
    def evm(payload):
        return {"result": evm_result(payload["method"], payload.get("params", []))}

    def solana(payload):
        if payload["method"] == "getBalance":
            return {"result": {"value": 3 * 10 ** 9}}  # 3 SOL
        if payload["method"] == "getTokenAccountsByOwner":
            return {"result": {"value": sol_accounts or []}}
        return {"result": None}

    routes = {"rpc-a.test": evm, "rpc-b.test": evm, "sol.test": solana,
              "coins.llama.fi": {"coins": {
                  "coingecko:ethereum": {"price": 3000.0, "symbol": "ETH"},
                  "coingecko:solana": {"price": 150.0, "symbol": "SOL"},
                  "ethereum:0xa0b8": {"price": 1.0, "symbol": "USDC"},
                  "solana:Mint1111": {"price": 2.5, "symbol": "JUP"},
              }}}
    for fragment in fail_rpcs:
        routes[fragment] = requests.ConnectionError(f"{fragment} refused")
    return FakeSession(routes)


def test_onchain_evm_native_and_token():
    client = OnChainClient(CHAINS, session=onchain_session())
    holdings = client.fetch_evm("ethereum", {"address": "0xdead", "label": "main", "chains": [], "tokens": {}})
    by_symbol = {h.symbol: h for h in holdings}
    assert by_symbol["ETH"].quantity == pytest.approx(2.0)
    assert by_symbol["USDC"].quantity == pytest.approx(1500.0)
    assert by_symbol["USDC"].price_id == "ethereum:0xa0b8"
    assert all(h.source == ONCHAIN for h in holdings)


def test_onchain_rpc_failover_to_second_endpoint():
    client = OnChainClient(CHAINS, session=onchain_session(fail_rpcs=["rpc-a.test"]))
    assert client.evm_native("ethereum", "0xdead") == pytest.approx(2.0)


def test_onchain_all_rpcs_down_is_reported_not_raised():
    client = OnChainClient(CHAINS, session=onchain_session(fail_rpcs=["rpc-a.test", "rpc-b.test"]))
    holdings, errors = client.fetch_all([{"address": "0xdead", "label": "main", "chains": ["ethereum"],
                                          "tokens": {}}], [])
    assert holdings == []
    assert errors and "refused" in errors[0]


def test_onchain_json_rpc_error_falls_over_to_the_next_endpoint():
    """A node answering 200-with-error (-32603) must not abort the remaining endpoints."""
    session = onchain_session()
    session.routes["rpc-a.test"] = lambda payload: {"error": {"code": -32603, "message": "Internal error"}}
    client = OnChainClient(CHAINS, session=session)
    assert client.evm_native("ethereum", "0xdead") == pytest.approx(2.0)


def test_onchain_json_rpc_error_everywhere_is_reported():
    session = onchain_session()
    failure = lambda payload: {"error": {"code": -32603, "message": "Internal error"}}  # noqa: E731
    session.routes["rpc-a.test"] = session.routes["rpc-b.test"] = failure
    with pytest.raises(RpcError, match="Internal error"):
        OnChainClient(CHAINS, session=session).evm_native("ethereum", "0xdead")


def test_onchain_unknown_chain_raises_rpc_error():
    with pytest.raises(RpcError):
        OnChainClient(CHAINS, session=onchain_session()).rpc("fantom", "eth_getBalance", [])


def test_onchain_solana_native_and_spl():
    accounts = [{"account": {"data": {"parsed": {"info": {
        "mint": "Mint1111", "tokenAmount": {"uiAmountString": "42.5", "decimals": 6}}}}}}]
    client = OnChainClient(CHAINS, session=onchain_session(sol_accounts=accounts))
    holdings, errors = client.fetch_all([], [{"address": "SoLaddr", "label": "phantom", "chains": [], "tokens": {}}])
    assert errors == []
    by_symbol = {h.symbol: h for h in holdings}
    assert by_symbol["SOL"].quantity == pytest.approx(3.0)
    assert by_symbol["JUP"].quantity == pytest.approx(42.5)   # symbol resolved by DefiLlama
    assert by_symbol["JUP"].price_usd == 2.5


def test_onchain_pricing_failure_leaves_zero_prices():
    session = onchain_session()
    session.routes["coins.llama.fi"] = requests.Timeout("llama timed out")
    client = OnChainClient(CHAINS, session=session)
    holdings, errors = client.fetch_all([{"address": "0xdead", "label": "m", "chains": ["ethereum"],
                                          "tokens": {}}], [])
    assert holdings and all(h.price_usd == 0.0 for h in holdings)
    assert any("llama" in e for e in errors)


def test_onchain_skips_token_with_unreadable_decimals():
    """Guessing 18 decimals for a 6-decimal token would inflate the balance 10^12x."""
    chains = {"ethereum": dict(CHAINS["ethereum"],
                               tokens=[{"address": "0xA0b8"}])}  # no symbol/decimals configured
    session = onchain_session()
    session.routes["rpc-a.test"] = session.routes["rpc-b.test"] = (
        lambda payload: {"result": hex(2 * 10 ** 18)} if payload["method"] == "eth_getBalance"
        else {"error": {"code": -32603, "message": "Internal error"}})
    client = OnChainClient(chains, session=session)
    holdings = client.fetch_evm("ethereum", {"address": "0xdead", "label": "m", "chains": [], "tokens": {}})
    assert [h.symbol for h in holdings] == ["ETH"]          # the token is dropped, not guessed
    assert any("unreadable token metadata" in e for e in client.errors)


def test_onchain_configured_decimals_skip_the_contract_call():
    """A token with symbol+decimals in config must not need the metadata calls at all."""
    session = onchain_session()
    client = OnChainClient(CHAINS, session=session)
    client.fetch_evm("ethereum", {"address": "0xdead", "label": "m", "chains": [], "tokens": {}})
    calls = [c[2] for c in session.calls if c[0] == "POST" and c[2].get("method") == "eth_call"]
    selectors = [call["params"][0]["data"][:10] for call in calls]
    assert selectors == ["0x70a08231"]  # balanceOf only - decimals()/symbol() never called


def test_onchain_helpers():
    assert _hex_to_int("0x10") == 16
    assert _hex_to_int("garbage") == 0 and _hex_to_int(None) == 0
    assert _decode_abi_string("0x" + "MKR".encode().hex().ljust(64, "0")) == "MKR"
    offset = (32).to_bytes(32, "big") + (4).to_bytes(32, "big") + b"USDC".ljust(32, b"\0")
    assert _decode_abi_string("0x" + offset.hex()) == "USDC"
    assert _decode_abi_string("not hex") == ""


# -- aggregation ---------------------------------------------------------------
def sample_holdings():
    return [
        Holding("BTC", BINANCE_SPOT, 0.5, 60000.0),
        Holding("BTC", ONCHAIN, 0.1, 60000.0, detail="ethereum/main"),
        Holding("ETH", BINANCE_EARN, 2.0, 3000.0),
        Holding("USDT", BINANCE_FUNDING, 110.0, 1.0),
        Holding("DUST", BINANCE_SPOT, 1000.0, 0.0001),   # $0.10
    ]


def test_aggregate_merges_sources_and_percentages():
    portfolio = aggregate(sample_holdings(), min_value=1.0)
    btc = next(r for r in portfolio.rows if r.symbol == "BTC")
    assert btc.quantity == pytest.approx(0.6)
    assert btc.by_source[BINANCE_SPOT] == 0.5 and btc.by_source[ONCHAIN] == 0.1
    assert btc.details == ["ethereum/main"]
    assert sum(r.pct for r in portfolio.rows) == pytest.approx(100.0, abs=0.01)
    assert portfolio.rows[0].symbol == "BTC"  # sorted by value desc


def test_dust_is_hidden_but_still_counted():
    portfolio = aggregate(sample_holdings(), min_value=1.0)
    assert "DUST" not in {r.symbol for r in portfolio.rows}
    assert portfolio.dust_assets == 1
    assert portfolio.dust_value_usd == pytest.approx(0.1)
    assert portfolio.total_usd == pytest.approx(36000 + 6000 + 110 + 0.1)


def test_min_value_zero_shows_everything():
    assert "DUST" in {r.symbol for r in aggregate(sample_holdings(), min_value=0.0).rows}


def test_aggregate_handles_empty_and_zero_quantities():
    empty = aggregate([], min_value=1.0)
    assert empty.rows == [] and empty.total_usd == 0.0
    zeros = aggregate([Holding("X", BINANCE_SPOT, 0.0, 10.0)], min_value=1.0)
    assert zeros.rows == [] and zeros.total_usd == 0.0


def test_price_is_value_weighted_across_sources():
    """Two sources quoting the same symbol differently must still reconcile Price x Qty."""
    rows = aggregate([Holding("USDC", BINANCE_SPOT, 100.0, 1.00),
                      Holding("USDC", ONCHAIN, 100.0, 0.98)], min_value=0.0).rows
    assert rows[0].value_usd == pytest.approx(198.0)
    assert rows[0].price_usd == pytest.approx(0.99)
    assert rows[0].price_usd * rows[0].quantity == pytest.approx(rows[0].value_usd)


def test_price_ignores_unpriced_quantity():
    """An unpriced leg must not drag the displayed price toward zero."""
    row = aggregate([Holding("ETH", BINANCE_SPOT, 2.0, 3000.0),
                     Holding("ETH", ONCHAIN, 1.0, 0.0)], min_value=0.0).rows[0]
    assert row.quantity == pytest.approx(3.0)
    assert row.price_usd == pytest.approx(3000.0)   # not 6000/3 = 2000
    assert row.value_usd == pytest.approx(6000.0)


def test_unpriced_asset_does_not_crash_percentages():
    portfolio = aggregate([Holding("WEIRD", ONCHAIN, 5.0, 0.0)], min_value=0.0)
    assert portfolio.rows[0].value_usd == 0.0 and portfolio.rows[0].pct == 0.0


def test_to_dict_is_json_serialisable():
    payload = to_dict(aggregate(sample_holdings(), 1.0))
    text = json.dumps(payload)
    assert json.loads(text)["total_usd"] == pytest.approx(42110.1)
    assert payload["ok"] is True and payload["dust"]["assets"] == 1


# -- database ------------------------------------------------------------------
def test_db_roundtrip(tmp_path):
    database = Database(tmp_path / "t.db")
    portfolio = aggregate(sample_holdings(), 1.0)
    snapshot_id = database.record(portfolio)
    snapshot = database.latest()
    assert snapshot.id == snapshot_id
    assert snapshot.total_portfolio_usd == pytest.approx(portfolio.total_usd)
    assert snapshot.binance_spot_usd == pytest.approx(30000.1)
    rows = database.assets(snapshot_id)
    assert len(rows) == len(portfolio.holdings)        # dust is stored, not dropped
    assert rows[0]["symbol"] == "BTC"


def test_db_created_in_missing_directory(tmp_path):
    database = Database(tmp_path / "nested" / "deep" / "history.db")
    assert database.count() == 0 and database.latest() is None
    assert database.latest_assets() == []


def test_db_history_is_oldest_first(tmp_path):
    database = Database(tmp_path / "t.db")
    for day in range(3):
        portfolio = aggregate(sample_holdings(), 1.0)
        portfolio.timestamp = f"2026-01-0{day + 1}T00:00:00+00:00"
        database.record(portfolio)
    history = database.history()
    assert [s.timestamp[:10] for s in history] == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert database.history(limit=1)[0].timestamp.startswith("2026-01-03")
    assert database.count() == 3


def test_db_schema_is_idempotent(tmp_path):
    path = tmp_path / "t.db"
    Database(path)
    assert Database(path).count() == 0  # re-opening must not fail on CREATE TABLE



# -- schema migrations (PRAGMA user_version) -------------------------------------
def make_v0_db(path, snapshots=16, assets_per_snapshot=3):
    """A database as written before versioning: the original schema, user_version = 0."""
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    for i in range(snapshots):
        cursor = connection.execute(
            "INSERT INTO snapshots (timestamp, total_portfolio_usd, binance_spot_usd, binance_earn_usd,"
            " binance_funding_usd, onchain_usd) VALUES (?, ?, ?, ?, ?, ?)",
            (f"2026-11-{1 + i:02d}T00:00:00+00:00", 1000.0 + i, 400.0 + i, 300.0, 100.0, 200.0),
        )
        connection.executemany(
            "INSERT INTO snapshot_assets (snapshot_id, symbol, source, quantity, price_usd, value_usd)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(cursor.lastrowid, f"T{j}", BINANCE_SPOT, 1.5 + j, 10.0 * (i + 1), 15.0 + j) for j in range(assets_per_snapshot)],
        )
    connection.commit()
    connection.close()
    assert schema_version(path) == 0
    return path


def dump_v0_rows(path):
    """Every v0 column of every row, so a migration can be checked to have lost nothing."""
    connection = sqlite3.connect(path)
    try:
        snapshots = connection.execute(
            "SELECT id, timestamp, total_portfolio_usd, binance_spot_usd, binance_earn_usd,"
            " binance_funding_usd, onchain_usd FROM snapshots ORDER BY id"
        ).fetchall()
        assets = connection.execute(
            "SELECT id, snapshot_id, symbol, source, quantity, price_usd, value_usd"
            " FROM snapshot_assets ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    return snapshots, assets


def columns(path, table):
    connection = sqlite3.connect(path)
    try:
        return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
    finally:
        connection.close()


def tables(path):
    connection = sqlite3.connect(path)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        connection.close()


def backups(path):
    return sorted(path.parent.glob(f"{path.name}.bak_v*"))


def test_migrations_list_defines_v3():
    assert SCHEMA_VERSION == len(MIGRATIONS) == 11  # v8: trades, v9-v10: analyst views, v11: notes


def test_fresh_db_is_created_at_latest_version_without_backup(tmp_path):
    path = tmp_path / "t.db"
    database = Database(path)
    assert database.schema_version() == SCHEMA_VERSION
    assert {"snapshots", "snapshot_assets", "snapshot_sources"} <= tables(path)
    assert {"detail", "price_id"} <= set(columns(path, "snapshot_assets"))
    assert "trigger" in columns(path, "snapshots")
    assert backups(path) == []  # nothing to back up in a brand-new file


def test_upgrade_v0_to_latest_keeps_every_row(tmp_path):
    path = make_v0_db(tmp_path / "portfolio_history.db")
    before = dump_v0_rows(path)

    database = Database(path)

    assert database.schema_version() == SCHEMA_VERSION
    assert dump_v0_rows(path) == before
    assert database.count() == 16 and len(before[1]) == 48
    # Old rows get the column defaults, and the read API still works on them.
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT DISTINCT trigger FROM snapshots").fetchall() == [("unknown",)]
    assert connection.execute("SELECT DISTINCT detail, price_id FROM snapshot_assets").fetchall() == [("", "")]
    connection.close()
    assert database.latest().total_portfolio_usd == 1015.0
    assert len(database.latest_assets()) == 3


def test_upgrade_v0_to_v1_then_v1_to_v2_keeps_every_row(tmp_path):
    path = make_v0_db(tmp_path / "t.db")
    before = dump_v0_rows(path)

    assert migrate(path, MIGRATIONS[:1]) == 1
    assert schema_version(path) == 1
    assert dump_v0_rows(path) == before
    assert "detail" not in columns(path, "snapshot_assets")  # v2 not applied yet
    assert [b.name.split(".bak_")[1][:3] for b in backups(path)] == ["v0_"]

    assert Database(path).schema_version() == SCHEMA_VERSION
    assert dump_v0_rows(path) == before
    assert "snapshot_sources" in tables(path)
    assert sorted(b.name.split(".bak_")[1][:3] for b in backups(path)) == ["v0_", "v1_"]


def test_backup_is_a_full_copy_of_the_old_version(tmp_path):
    path = make_v0_db(tmp_path / "portfolio_history.db")
    before = dump_v0_rows(path)
    Database(path)

    (backup,) = backups(path)
    assert backup.name.startswith("portfolio_history.db.bak_v0_")
    assert len(backup.name.split("bak_v0_")[1]) == len("YYYYmmdd_HHMMSS")
    assert schema_version(backup) == 0
    assert dump_v0_rows(backup) == before
    assert "snapshot_sources" not in tables(backup)


def test_second_run_is_a_noop(tmp_path):
    path = make_v0_db(tmp_path / "t.db")
    Database(path)
    after_first = dump_v0_rows(path), columns(path, "snapshots"), backups(path)

    assert migrate(path) == SCHEMA_VERSION
    Database(path)

    assert schema_version(path) == SCHEMA_VERSION
    assert (dump_v0_rows(path), columns(path, "snapshots"), backups(path)) == after_first


def test_failed_migration_rolls_back_everything(tmp_path, monkeypatch):
    path = make_v0_db(tmp_path / "t.db")
    before = dump_v0_rows(path)
    broken = MIGRATIONS + [
        "ALTER TABLE snapshots ADD COLUMN half_done TEXT;\n"
        "CREATE TABLE half_done_table (x INTEGER);\n"
        "ALTER TABLE no_such_table ADD COLUMN boom TEXT;"
    ]
    monkeypatch.setattr(db_module, "MIGRATIONS", broken)

    with pytest.raises(MigrationError, match=f"v{SCHEMA_VERSION + 1}"):
        Database(path)

    # every shipped step and the first half of the broken one ran in this attempt; none of it
    # may survive.
    assert schema_version(path) == 0
    assert dump_v0_rows(path) == before
    assert "half_done" not in columns(path, "snapshots")
    assert "trigger" not in columns(path, "snapshots")
    assert "detail" not in columns(path, "snapshot_assets")
    assert tables(path) & {"half_done_table", "snapshot_sources"} == set()
    assert len(backups(path)) == 1  # the backup was taken before the attempt

    monkeypatch.setattr(db_module, "MIGRATIONS", MIGRATIONS)
    assert Database(path).schema_version() == SCHEMA_VERSION  # fixing the step lets it go through
    assert dump_v0_rows(path) == before


def test_database_newer_than_code_is_refused(tmp_path):
    path = tmp_path / "t.db"
    Database(path)
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.close()
    with pytest.raises(MigrationError, match="newer"):
        Database(path)
    assert schema_version(path) == SCHEMA_VERSION + 1


def test_record_after_upgrade_uses_new_defaults(tmp_path):
    path = make_v0_db(tmp_path / "t.db")
    database = Database(path)
    snapshot_id = database.record(aggregate(sample_holdings(), 0.0))
    assert database.count() == 17
    assert database.latest().id == snapshot_id
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT trigger FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone() == ("unknown",)
    connection.close()


def test_gateway_reads_migrated_db_read_only(tmp_path):
    import api_gateway

    path = make_v0_db(tmp_path / "t.db")
    Database(path)
    row = api_gateway.read_latest_snapshot(path)
    assert row["timestamp"] == "2026-11-16T00:00:00+00:00"
    assert row["total_portfolio_usd"] == 1015.0


def test_split_statements_respects_quotes_and_comments():
    script = """
    -- a comment; with a semicolon
    CREATE TABLE t (a TEXT DEFAULT 'x;y');  -- trailing; comment
    /* block; comment */ INSERT INTO t VALUES ('it''s;');
    """
    statements = split_statements(script)
    assert len(statements) == 2
    assert statements[0].endswith("DEFAULT 'x;y');")
    assert "'it''s;'" in statements[1]
    assert split_statements("") == []
    assert len(split_statements(MIGRATIONS[1])) == 4
    with pytest.raises(ValueError):
        split_statements("CREATE TABLE t (a INTEGER)")


# -- visualizer ----------------------------------------------------------------
def fake_snapshots(n, start=1000.0):
    class Stub:
        def __init__(self, index):
            from datetime import datetime, timedelta, timezone
            self.id = index + 1
            self.dt = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index * 3)
            self.timestamp = self.dt.isoformat()
            self.total_portfolio_usd = start + index * 120
            self.binance_spot_usd = self.total_portfolio_usd * 0.5
            self.binance_earn_usd = self.total_portfolio_usd * 0.3
            self.binance_funding_usd = self.total_portfolio_usd * 0.1
            self.onchain_usd = self.total_portfolio_usd * 0.1
    return [Stub(i) for i in range(n)]


@pytest.mark.parametrize("count", [0, 1, 2, 5, 40])
def test_charts_render_for_any_history_length(tmp_path, count):
    portfolio = aggregate(sample_holdings(), 1.0)
    written = generate(portfolio, fake_snapshots(count), tmp_path, kinds=("all",))
    assert len(written) == 3
    for path in written:
        assert path.exists() and path.stat().st_size > 1000  # a real PNG, not a stub


def test_history_chart_handles_identical_timestamps(tmp_path):
    """Two snapshots in the same second would otherwise give a zero-width time axis."""
    snapshots = fake_snapshots(2)
    snapshots[1].dt = snapshots[0].dt
    snapshots[1].timestamp = snapshots[0].timestamp
    path = generate(aggregate(sample_holdings(), 1.0), snapshots, tmp_path, kinds=("history",))[0]
    assert path.exists() and path.stat().st_size > 1000


def test_charts_render_with_empty_portfolio(tmp_path):
    written = generate(aggregate([], 1.0), [], tmp_path, kinds=("all",))
    assert all(p.exists() for p in written)


def test_chart_kind_selection(tmp_path):
    written = generate(aggregate(sample_holdings(), 1.0), fake_snapshots(3), tmp_path, kinds=("history",))
    assert [p.name for p in written] == ["history_trend.png"]


def test_allocation_folds_tail_into_others(tmp_path):
    from services.visualizer import MAX_SLICES, _top_slices
    many = [Holding(f"T{i}", BINANCE_SPOT, 1.0, float(50 - i)) for i in range(20)]
    slices = _top_slices(aggregate(many, 0.0).rows)
    assert len(slices) == MAX_SLICES
    assert slices[-1][0] == "Others"
    assert sum(v for _, v in slices) == pytest.approx(sum(h.value_usd for h in many))


def test_text_report_and_sparkline():
    report = text_report(aggregate(sample_holdings(), 1.0), fake_snapshots(5))
    assert "ALLOCATION" in report and "SOURCES" in report and "HISTORY" in report
    assert "BTC" in report
    assert sparkline([1, 2, 3, 4, 5])
    assert sparkline([]) == ""
    assert len(set(sparkline([7, 7, 7]))) == 1          # flat series: no fake variation
    assert len(sparkline(list(range(500)), width=20)) == 20
    assert bar(0.5, 10) == "█" * 5 + "·" * 5
    assert bar(-3, 10).count("·") == 10 and bar(9, 10).count("█") == 10


def test_text_report_with_one_snapshot_says_so():
    assert "one snapshot" in text_report(aggregate(sample_holdings(), 1.0), fake_snapshots(1))


# -- CLI -----------------------------------------------------------------------
class StubConfig:
    """Minimal config object - the CLI only touches these attributes."""

    def __init__(self, db_path, charts_dir):
        self.settings = {"min_value": 1.0}
        self.binance = {"enabled": False}
        self.binance_enabled = False
        self.db_path = Path(db_path)
        self.charts_dir = Path(charts_dir)
        self.has_wallets = True
        self.min_value = 1.0

    def resolve(self, value):
        return Path(value)


def test_cli_show_table(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "fetch_portfolio", lambda cfg, mv: aggregate(sample_holdings(), mv))
    args = cli.build_parser().parse_args(["show"])
    args.min_value = 1.0
    assert cli.cmd_show(args, StubConfig(tmp_path / "t.db", tmp_path)) == 0
    out = capsys.readouterr().out
    assert "BTC" in out and "Value USD" in out and "TOTAL" in out
    assert "DUST" not in out and "dust assets hidden" in out


def test_cli_show_json_is_valid(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "fetch_portfolio", lambda cfg, mv: aggregate(sample_holdings(), mv))
    args = cli.build_parser().parse_args(["show", "--json"])
    args.min_value = 1.0
    cli.cmd_show(args, StubConfig(tmp_path / "t.db", tmp_path))
    payload = json.loads(capsys.readouterr().out)
    assert payload["assets"][0]["symbol"] == "BTC"
    assert set(payload) == {"timestamp", "total_usd", "sources", "assets", "dust", "errors", "ok"}


def test_cli_show_reports_partial_failure_exit_code(tmp_path, monkeypatch, capsys):
    def failing(cfg, mv):
        portfolio = aggregate(sample_holdings(), mv)
        portfolio.errors = ["onchain: rpc down"]
        return portfolio

    monkeypatch.setattr(cli, "fetch_portfolio", failing)
    args = cli.build_parser().parse_args(["show"])
    args.min_value = 1.0
    assert cli.cmd_show(args, StubConfig(tmp_path / "t.db", tmp_path)) == 2
    assert "rpc down" in capsys.readouterr().err


def test_cli_record_writes_a_snapshot(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "fetch_portfolio", lambda cfg, min_value: aggregate(sample_holdings(), min_value))
    config = StubConfig(tmp_path / "t.db", tmp_path)
    args = cli.build_parser().parse_args(["record"])
    assert cli.cmd_record(args, config) == 0
    assert capsys.readouterr().out == ""        # silent: it runs from cron
    assert Database(config.db_path).count() == 1


def test_cli_record_refuses_empty_snapshot_when_everything_failed(tmp_path, monkeypatch, capsys):
    def dead(cfg, min_value):
        portfolio = aggregate([], min_value)
        portfolio.errors = ["binance spot: network unreachable"]
        return portfolio

    monkeypatch.setattr(cli, "fetch_portfolio", dead)
    config = StubConfig(tmp_path / "t.db", tmp_path)
    assert cli.cmd_record(cli.build_parser().parse_args(["record"]), config) == 1
    assert "refusing" in capsys.readouterr().err
    assert Database(config.db_path).count() == 0


def test_cli_chart_from_db_needs_no_network(tmp_path, monkeypatch, capsys):
    config = StubConfig(tmp_path / "t.db", tmp_path / "charts")
    Database(config.db_path).record(aggregate(sample_holdings(), 0.0))
    monkeypatch.setattr(cli, "fetch_portfolio", lambda *a, **k: pytest.fail("must not hit the network"))
    args = cli.build_parser().parse_args(["chart", "--from-db", "--output", str(tmp_path / "charts")])
    args.min_value = 1.0
    assert cli.cmd_chart(args, config) == 0
    assert sorted(p.name for p in (tmp_path / "charts").glob("*.png")) == [
        "history_trend.png", "source_distribution.png", "token_allocation.png"]


def test_cli_chart_from_empty_db_fails_cleanly(tmp_path, capsys):
    config = StubConfig(tmp_path / "t.db", tmp_path / "charts")
    args = cli.build_parser().parse_args(["chart", "--from-db"])
    args.min_value = 1.0
    assert cli.cmd_chart(args, config) == 1
    assert "no snapshots" in capsys.readouterr().err


def test_cli_history_renders(tmp_path, capsys):
    config = StubConfig(tmp_path / "t.db", tmp_path)
    database = Database(config.db_path)
    for _ in range(2):
        database.record(aggregate(sample_holdings(), 0.0))
    cli.cmd_history(cli.build_parser().parse_args(["history"]), config)
    assert "Total" in capsys.readouterr().out


def test_cli_history_empty_message(tmp_path, capsys):
    cli.cmd_history(cli.build_parser().parse_args(["history"]), StubConfig(tmp_path / "t.db", tmp_path))
    assert "No snapshots recorded yet" in capsys.readouterr().out


def test_cli_no_sources_configured_is_an_error(tmp_path):
    config = StubConfig(tmp_path / "t.db", tmp_path)
    config.has_wallets = False
    with pytest.raises(SystemExit):
        cli.check_sources(config)


def test_cli_min_value_accepted_on_either_side_of_the_subcommand():
    assert cli.build_parser().parse_args(["--min-value", "0", "show"]).min_value == 0.0
    assert cli.build_parser().parse_args(["show", "--min-value", "5"]).min_value == 5.0
    assert cli.build_parser().parse_args(["show"]).min_value is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
