"""F2.flow: cash-flow table, Binance deposit/withdraw/fiat/P2P sync, internal-transfer matching,
and the real 7d / 30d PnL net of deposits and withdrawals.

Offline: Binance is a fake session that serves the history endpoints from in-memory rows.

    .venv/bin/pytest test_cash_flows.py -q
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

import main as cli
import web_api
from clients.binance import DAY_MS, HISTORY_STREAMS, BinanceClient, BinanceError, time_chunks
from config import CashFlowSettings, load_config
from models import BINANCE_SPOT, ONCHAIN, Holding
from services import cash_flows as cf
from services.analytics.pnl import Flow, flow_adjusted_pnl, pnl_report, window_pnl
from services.cash_flows import (
    CashFlow,
    PriceBook,
    chain_for_network,
    classify,
    get_cursor,
    normalize_address,
    owned_addresses,
    parse_deposit,
    parse_fiat_order,
    parse_fiat_payment,
    parse_p2p_order,
    parse_withdrawal,
    reclassify,
    reprice_missing,
    set_cursor,
    sync,
    to_iso_utc,
    upsert_flows,
)
from services.db import CASH_FLOWS_SCHEMA_VERSION, SCHEMA_VERSION, Database, migrate, schema_version
from services.portfolio import aggregate
from test_all import FakeResponse
from test_api import isolated_env  # noqa: F401  (autouse: no real .env, no env overrides)

EVM_MAIN = "0xAbCdEf0123456789abcdef0123456789ABCDEF01"
EVM_COLD = "0x1111111111111111111111111111111111111111"
SOL_PHANTOM = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
STRANGER = "0x9999999999999999999999999999999999999999"

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)

# Every path the cash-flow sync may call. Anything else (an order, a transfer...) fails the test.
READ_ONLY_PATHS = {
    "/api/v3/time",
    "/api/v3/ticker/price",
    "/sapi/v1/capital/deposit/hisrec",
    "/sapi/v1/capital/withdraw/history",
    "/sapi/v1/fiat/orders",
    "/sapi/v1/fiat/payments",
    "/sapi/v1/c2c/orderMatch/listUserOrderHistory",
    "/sapi/v1/simple-earn/flexible/history/rewardsRecord",
    "/sapi/v1/simple-earn/locked/history/rewardsRecord",
}


# -- fakes -------------------------------------------------------------------------------
def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


class FakeBinance:
    """A requests.Session stand-in serving Binance history from in-memory rows.

    Rows are filtered by the request's time range and paged exactly like Binance does
    (offset/limit for capital, page/rows for fiat and C2C). `fail` maps a path fragment to an
    HTTP status to return instead; `fail_after` lets a path succeed N times first.
    """

    def __init__(self, deposits=(), withdrawals=(), fiat_orders=(), fiat_payments=(), p2p=(),
                 tickers=None, fail=None, fail_after=None, earn_flexible=(), earn_locked=()):
        self.deposits = list(deposits)
        self.withdrawals = list(withdrawals)
        self.fiat_orders = {0: [], 1: []}
        for row in fiat_orders:
            self.fiat_orders[row.pop("_type", 0)].append(row)
        self.fiat_payments = {0: [], 1: []}
        for row in fiat_payments:
            self.fiat_payments[row.pop("_type", 0)].append(row)
        self.p2p = list(p2p)
        self.earn_flexible = list(earn_flexible)   # rows carry "type": REALTIME | BONUS
        self.earn_locked = list(earn_locked)
        self.tickers = tickers or {"ETHUSDT": "3000", "BTCUSDT": "60000", "EURUSDT": "1.10"}
        self.fail = fail or {}
        self.fail_after = fail_after or {}
        self.headers = {}
        self.calls: list[tuple[str, str, dict]] = []

    @staticmethod
    def _in_range(moment_ms: int, start: int, end: int) -> bool:
        return start <= moment_ms <= end

    def request(self, method, url, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append((method, url, params))
        for fragment, status in self.fail.items():
            if fragment in url:
                allowed = self.fail_after.get(fragment, 0)
                done = sum(1 for _, u, _ in self.calls if fragment in u)
                if done > allowed:
                    return FakeResponse({"code": -2015, "msg": "Invalid API-key"}, status=status)
        if url.endswith("/api/v3/time"):
            return FakeResponse({"serverTime": NOW_MS})
        if url.endswith("/api/v3/ticker/price"):
            return FakeResponse([{"symbol": s, "price": p} for s, p in self.tickers.items()])
        if "/capital/deposit/hisrec" in url:
            rows = [r for r in self.deposits
                    if self._in_range(r["insertTime"], params["startTime"], params["endTime"])]
            return FakeResponse(self._offset(rows, params))
        if "/capital/withdraw/history" in url:
            rows = [r for r in self.withdrawals
                    if self._in_range(ms(datetime.fromisoformat(r["applyTime"] + "+00:00")),
                                      params["startTime"], params["endTime"])]
            return FakeResponse(self._offset(rows, params))
        if "/fiat/orders" in url:
            rows = [r for r in self.fiat_orders[params["transactionType"]]
                    if self._in_range(r["createTime"], params["beginTime"], params["endTime"])]
            return FakeResponse(self._paged(rows, params))
        if "/fiat/payments" in url:
            rows = [r for r in self.fiat_payments[params["transactionType"]]
                    if self._in_range(r["createTime"], params["beginTime"], params["endTime"])]
            return FakeResponse(self._paged(rows, params))
        if "/c2c/orderMatch/listUserOrderHistory" in url:
            rows = [r for r in self.p2p if r["tradeType"] == params["tradeType"]
                    and self._in_range(r["createTime"], params["startTimestamp"], params["endTimestamp"])]
            return FakeResponse(self._paged(rows, params))
        if "/simple-earn/flexible/history/rewardsRecord" in url:
            rows = [r for r in self.earn_flexible if r.get("type") == params["type"]
                    and self._in_range(r["time"], params["startTime"], params["endTime"])]
            return FakeResponse(self._current(rows, params))
        if "/simple-earn/locked/history/rewardsRecord" in url:
            rows = [r for r in self.earn_locked
                    if self._in_range(r["time"], params["startTime"], params["endTime"])]
            return FakeResponse(self._current(rows, params))
        return FakeResponse({"code": -1121, "msg": "unknown"}, status=404)

    @staticmethod
    def _current(rows, params):
        page, size = params["current"], params["size"]
        return {"rows": rows[(page - 1) * size: page * size], "total": len(rows)}

    @staticmethod
    def _offset(rows, params):
        offset, limit = params.get("offset", 0), params["limit"]
        return rows[offset:offset + limit]

    @staticmethod
    def _paged(rows, params):
        page, size = params["page"], params["rows"]
        return {"code": "000000", "message": "success", "data": rows[(page - 1) * size: page * size],
                "total": len(rows), "success": True}

    def paths(self, fragment: str = "") -> list[str]:
        return [url for _, url, _ in self.calls if fragment in url]


def client_for(session: FakeBinance) -> BinanceClient:
    return BinanceClient("k" * 64, "s" * 64, session=session)


def deposit_row(id_, coin, amount, when, status=1, network="ETH", source_address="", tx="0xdep"):
    return {"id": str(id_), "coin": coin, "amount": str(amount), "network": network, "status": status,
            "address": "0xbinancedepositaddr", "txId": tx, "insertTime": ms(when),
            "completeTime": ms(when + timedelta(minutes=2)), "transferType": 0,
            **({"sourceAddress": source_address} if source_address else {})}


def withdraw_row(id_, coin, amount, when, address, network="ETH", status=6, fee="0.0004"):
    return {"id": id_, "coin": coin, "amount": str(amount), "transactionFee": fee, "network": network,
            "status": status, "address": address, "txId": f"0xw{id_}", "transferType": 0,
            "applyTime": when.strftime("%Y-%m-%d %H:%M:%S"),
            "completeTime": (when + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")}


def make_config(tmp_path, cash_flows=None, binance=True, wallets=None):
    raw = {
        "settings": {"db_path": str(tmp_path / "history.db")},
        "wallets": wallets if wallets is not None else {
            "evm": [{"address": EVM_MAIN, "label": "main", "chains": ["ethereum", "arbitrum"]},
                    {"address": EVM_COLD, "label": "cold"}],
            "solana": [{"address": SOL_PHANTOM, "label": "phantom"}],
        },
        "binance": {"enabled": binance, "api_key": "k" * 64 if binance else "",
                    "api_secret": "s" * 64 if binance else ""},
    }
    if cash_flows is not None:
        raw["cash_flows"] = cash_flows
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_config(path)


def record_snapshot(database: Database, when: datetime, holdings: list[Holding]) -> int:
    portfolio = aggregate(holdings)
    portfolio.timestamp = iso(when)
    return database.record(portfolio)


def count_flows(database: Database) -> int:
    with database.connect() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM cash_flows").fetchone()[0])


def flow_rows(database: Database) -> list[dict]:
    with database.connect() as connection:
        return [dict(r) for r in connection.execute("SELECT * FROM cash_flows ORDER BY timestamp, id")]


def store(database: Database, *flows: CashFlow) -> None:
    upsert_flows(database, flows)


def flow(timestamp: datetime, asset="USDT", direction="in", amount=1000.0, price=1.0, **kw) -> CashFlow:
    defaults = {"flow_type": "deposit" if direction == "in" else "withdrawal",
                "external_id": f"x{timestamp.timestamp()}{asset}{direction}{amount}"}
    defaults.update(kw)
    return CashFlow(timestamp=iso(timestamp), asset=asset, direction=direction, amount=amount,
                    price_usd=price, **defaults)


# == F2.1: schema v4 =========================================================================
def test_schema_v4_creates_cash_flows_and_sync_state(tmp_path):
    database = Database(tmp_path / "h.db")
    assert SCHEMA_VERSION == database.schema_version() and CASH_FLOWS_SCHEMA_VERSION == 4
    with database.connect() as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"cash_flows", "sync_state"} <= tables
    assert {"ux_cash_flows_ext", "idx_cash_flows_ts", "idx_cash_flows_tx", "idx_cash_flows_asset"} <= indexes


def test_v3_database_upgrades_to_v4_keeping_snapshots(tmp_path):
    path = tmp_path / "h.db"
    migrate(path, cli_migrations := __import__("services.db", fromlist=["MIGRATIONS"]).MIGRATIONS[:3])
    assert schema_version(path) == 3 == len(cli_migrations)
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO snapshots (timestamp, total_portfolio_usd) VALUES ('2026-01-01T00:00:00+00:00', 42)")
    connection.commit()
    connection.close()
    database = Database(path)
    assert database.schema_version() == SCHEMA_VERSION  # v3 -> latest
    assert database.history()[0].total_portfolio_usd == 42
    assert list(tmp_path.glob("h.db.bak_v3_*"))  # backed up before the upgrade


def _insert_raw(database, **overrides):
    values = {"timestamp": "2026-01-01T00:00:00+00:00", "source": "binance", "account": "binance",
              "asset": "ETH", "direction": "in", "amount": 1.0, "flow_type": "deposit",
              "detection_method": "binance_api", "external_id": "1", "created_at": "x", "updated_at": "x"}
    values.update(overrides)
    with database.connect() as connection:
        connection.execute(f"INSERT INTO cash_flows ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                           list(values.values()))


@pytest.mark.parametrize("overrides", [
    {"amount": -1.0},
    {"direction": "sideways"},
    {"status": "maybe"},
    {"flow_type": "gift"},
    {"is_internal_transfer": 2},
    {"match_confidence": 1.5},
])
def test_check_constraints_reject_bad_rows(tmp_path, overrides):
    database = Database(tmp_path / "h.db")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(database, **overrides)


def test_duplicate_external_id_is_unique(tmp_path):
    database = Database(tmp_path / "h.db")
    _insert_raw(database)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(database)
    _insert_raw(database, flow_type="withdrawal", direction="out")  # other flow_type: allowed


def test_upsert_does_not_duplicate_and_updates_status(tmp_path):
    database = Database(tmp_path / "h.db")
    pending = flow(NOW, asset="ETH", amount=1.0, price=3000, status="pending", external_id="d1")
    assert upsert_flows(database, [pending]) == (1, 0)
    assert upsert_flows(database, [pending]) == (0, 0)  # nothing changed: not even updated_at
    done = flow(NOW + timedelta(minutes=3), asset="ETH", amount=1.0, price=3100, status="completed",
                external_id="d1")
    assert upsert_flows(database, [done]) == (0, 1)
    [row] = flow_rows(database)
    assert row["status"] == "completed" and row["timestamp"] == iso(NOW + timedelta(minutes=3))
    assert row["price_usd"] == 3000  # a known price is kept: the flow happened at that price


# == F2.2: Binance history client ===============================================================
def test_time_chunks_365_days_gives_5_chunks_of_at_most_90_days():
    start, end = NOW_MS - 365 * DAY_MS, NOW_MS
    chunks = time_chunks(start, end, 90)
    assert len(chunks) == 5
    assert chunks[0][0] == start and chunks[-1][1] == end
    for (a_start, a_end), (b_start, _) in zip(chunks, chunks[1:]):
        assert b_start == a_end + 1  # contiguous, no gap, no overlap
    assert all(e - s < 90 * DAY_MS for s, e in chunks)
    assert time_chunks(end, start, 90) == []


def test_deposit_backfill_365_days_queries_5_windows(tmp_path):
    session = FakeBinance()
    client = client_for(session)
    client.deposit_history(NOW_MS - 365 * DAY_MS, NOW_MS)
    calls = [params for _, url, params in session.calls if "deposit/hisrec" in url]
    assert len(calls) == 5
    assert all(c["endTime"] - c["startTime"] < 90 * DAY_MS for c in calls)
    assert all("signature" in c and "timestamp" in c for c in calls)


def test_full_page_fetches_the_next_page():
    when = NOW - timedelta(days=1)
    session = FakeBinance(deposits=[deposit_row(i, "USDT", 1, when) for i in range(1500)])
    rows = client_for(session).deposit_history(NOW_MS - 10 * DAY_MS, NOW_MS)
    assert len(rows) == 1500
    offsets = [params["offset"] for _, url, params in session.calls if "deposit/hisrec" in url]
    assert offsets == [0, 1000]


def test_page_paging_for_fiat_and_p2p():
    when = NOW - timedelta(days=1)
    orders = [{"orderNo": f"o{i}", "fiatCurrency": "EUR", "amount": "10", "status": "Successful",
               "createTime": ms(when), "updateTime": ms(when), "_type": 0} for i in range(501)]
    p2p = [{"orderNumber": f"p{i}", "tradeType": "BUY", "asset": "USDT", "amount": "5",
            "orderStatus": "COMPLETED", "createTime": ms(when)} for i in range(100)]
    session = FakeBinance(fiat_orders=orders, p2p=p2p)
    client = client_for(session)
    assert len(client.fiat_orders(0, NOW_MS - 5 * DAY_MS, NOW_MS)) == 501
    assert [p["page"] for _, u, p in session.calls if "fiat/orders" in u] == [1, 2]
    assert len(client.p2p_orders("BUY", NOW_MS - 5 * DAY_MS, NOW_MS)) == 100
    assert [p["page"] for _, u, p in session.calls if "c2c" in u] == [1, 2]  # 100 = full page


def test_p2p_unsuccessful_payload_raises():
    class Session(FakeBinance):
        def request(self, method, url, params=None, timeout=None):
            if "c2c" in url:
                self.calls.append((method, url, params))
                return FakeResponse({"code": "345", "message": "no permission", "success": False})
            return super().request(method, url, params, timeout)

    with pytest.raises(BinanceError, match="no permission"):
        client_for(Session()).p2p_orders("BUY", NOW_MS - DAY_MS, NOW_MS)


def test_history_streams_are_all_read_only_get_paths():
    assert {spec.path for spec in HISTORY_STREAMS.values()} <= READ_ONLY_PATHS


def test_sync_only_issues_read_only_get_requests(tmp_path):
    config = make_config(tmp_path)
    session = FakeBinance(deposits=[deposit_row(1, "ETH", 1, NOW - timedelta(days=3))])
    sync(config, Database(config.db_path), client=client_for(session), now_ms=NOW_MS)
    assert session.calls
    assert {method for method, _, _ in session.calls} == {"GET"}
    paths = {url.split("binance.com", 1)[-1].split("binance.vision", 1)[-1] for _, url, _ in session.calls}
    assert paths <= READ_ONLY_PATHS, paths - READ_ONLY_PATHS


# == F2.3: sync, cursors, idempotency =============================================================
def full_session(**overrides) -> FakeBinance:
    base = dict(
        deposits=[deposit_row(1, "ETH", 0.5, NOW - timedelta(days=40)),
                  deposit_row(2, "USDT", 250, NOW - timedelta(days=3))],
        withdrawals=[withdraw_row("w1", "ETH", 1.0, NOW - timedelta(days=5), EVM_MAIN.lower()),
                     withdraw_row("w2", "USDT", 100, NOW - timedelta(days=2), STRANGER, network="BSC")],
        fiat_orders=[{"orderNo": "f1", "fiatCurrency": "EUR", "amount": "1000", "totalFee": "1",
                      "status": "Successful", "method": "BankAccount", "createTime": ms(NOW - timedelta(days=10)),
                      "updateTime": ms(NOW - timedelta(days=10)), "_type": 0}],
        fiat_payments=[{"orderNo": "c1", "sourceAmount": "200", "fiatCurrency": "EUR", "obtainAmount": "0.003",
                        "cryptoCurrency": "BTC", "totalFee": "2", "status": "Completed",
                        "paymentMethod": "Card", "createTime": ms(NOW - timedelta(days=8)),
                        "updateTime": ms(NOW - timedelta(days=8)), "_type": 0}],
        p2p=[{"orderNumber": "p1", "tradeType": "BUY", "asset": "USDT", "amount": "500", "fiat": "EUR",
              "totalPrice": "460", "orderStatus": "COMPLETED", "createTime": ms(NOW - timedelta(days=6))},
             {"orderNumber": "p2", "tradeType": "SELL", "asset": "USDT", "amount": "120", "fiat": "EUR",
              "totalPrice": "110", "orderStatus": "COMPLETED", "createTime": ms(NOW - timedelta(days=1))}],
    )
    base.update(overrides)
    return FakeBinance(**base)


def test_sync_stores_every_channel(tmp_path):
    config = make_config(tmp_path)
    database = Database(config.db_path)
    report = sync(config, database, client=client_for(full_session()), now_ms=NOW_MS)
    assert not report.errors and not report.skipped
    rows = {r["external_id"]: r for r in flow_rows(database)}
    assert set(rows) == {"1", "2", "w1", "w2", "fiat:f1", "fiatpay:c1", "p2p:p1", "p2p:p2"}
    assert rows["fiat:f1"]["channel"] == "fiat" and rows["fiat:f1"]["asset"] == "EUR"
    assert rows["fiat:f1"]["value_usd"] == pytest.approx(1100)  # EURUSDT fallback 1.10
    assert rows["fiatpay:c1"]["asset"] == "BTC" and rows["fiatpay:c1"]["direction"] == "in"
    assert rows["p2p:p1"]["direction"] == "in" and rows["p2p:p2"]["direction"] == "out"
    assert rows["p2p:p2"]["flow_type"] == "withdrawal"
    assert report.total_inserted == 8


def test_sync_twice_inserts_nothing_new(tmp_path):
    config = make_config(tmp_path)
    database = Database(config.db_path)
    session = full_session()
    sync(config, database, client=client_for(session), now_ms=NOW_MS)
    before = flow_rows(database)
    report = sync(config, database, client=client_for(session), now_ms=NOW_MS + 3_600_000)
    assert report.total_inserted == 0 and report.total_updated == 0
    assert flow_rows(database) == before


def test_second_sync_starts_at_cursor_minus_overlap(tmp_path):
    config = make_config(tmp_path, cash_flows={"overlap_days": 2})
    database = Database(config.db_path)
    sync(config, database, client=client_for(FakeBinance()), now_ms=NOW_MS)
    assert get_cursor(database, "binance.deposit") == NOW_MS
    later = NOW_MS + DAY_MS
    session = FakeBinance()
    sync(config, database, client=client_for(session), now_ms=later)
    [params] = [p for _, u, p in session.calls if "deposit/hisrec" in u]
    assert params["startTime"] == NOW_MS - 2 * DAY_MS and params["endTime"] == later
    assert get_cursor(database, "binance.deposit") == later


def test_first_sync_backfills_configured_days(tmp_path):
    config = make_config(tmp_path, cash_flows={"backfill_days": 365, "sync_fiat": False, "sync_p2p": False})
    session = FakeBinance()
    sync(config, Database(config.db_path), client=client_for(session), now_ms=NOW_MS)
    deposit_calls = [p for _, u, p in session.calls if "deposit/hisrec" in u]
    assert len(deposit_calls) == 5 and deposit_calls[0]["startTime"] == NOW_MS - 365 * DAY_MS
    assert not session.paths("fiat") and not session.paths("c2c")  # streams switched off


def test_pending_deposit_becomes_completed(tmp_path):
    config = make_config(tmp_path)
    database = Database(config.db_path)
    when = NOW - timedelta(hours=1)
    sync(config, database, client=client_for(FakeBinance(deposits=[deposit_row(9, "USDT", 50, when, status=0)])),
         now_ms=NOW_MS)
    assert flow_rows(database)[0]["status"] == "pending"
    report = sync(config, database,
                  client=client_for(FakeBinance(deposits=[deposit_row(9, "USDT", 50, when, status=1)])),
                  now_ms=NOW_MS + 3_600_000)
    assert report.updated["deposit"] == 1 and report.total_inserted == 0
    assert [r["status"] for r in flow_rows(database)] == ["completed"]


def test_failing_stream_is_isolated_and_keeps_its_cursor(tmp_path):
    config = make_config(tmp_path)
    database = Database(config.db_path)
    session = full_session(fail={"/fiat/": 403})
    report = sync(config, database, client=client_for(session), now_ms=NOW_MS)
    assert any("fiat" in e and "403" in e for e in report.errors)
    assert get_cursor(database, "binance.deposit") == NOW_MS
    assert get_cursor(database, "binance.fiat_deposit") is None
    assert {r["channel"] for r in flow_rows(database)} == {"crypto", "p2p"}


def test_interrupted_backfill_resumes_from_last_chunk(tmp_path):
    config = make_config(tmp_path, cash_flows={"sync_fiat": False, "sync_p2p": False})
    database = Database(config.db_path)
    # deposit/hisrec answers twice, then 401 on the third 90-day chunk
    session = FakeBinance(fail={"deposit/hisrec": 401}, fail_after={"deposit/hisrec": 2})
    report = sync(config, database, client=client_for(session), now_ms=NOW_MS)
    assert any("deposit" in e for e in report.errors)
    chunks = time_chunks(NOW_MS - CashFlowSettings().backfill_days * DAY_MS, NOW_MS, 90)
    assert get_cursor(database, "binance.deposit") == chunks[1][1]
    assert get_cursor(database, "binance.withdraw") == NOW_MS


def test_sync_skips_when_disabled_or_without_binance(tmp_path):
    session = FakeBinance()
    disabled = make_config(tmp_path, cash_flows={"enabled": False})
    assert "enabled" in sync(disabled, Database(disabled.db_path), client=client_for(session)).skipped
    no_key = make_config(tmp_path, binance=False)
    assert "Binance" in sync(no_key, Database(no_key.db_path), client=client_for(session)).skipped
    assert session.calls == []


def test_record_keeps_snapshot_when_binance_history_401(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    holdings = [Holding("ETH", BINANCE_SPOT, 1.0, 3000.0)]
    monkeypatch.setattr(cli, "fetch_portfolio", lambda cfg, min_value=0.0: aggregate(holdings))
    session = FakeBinance(fail={"/sapi/": 401})
    monkeypatch.setattr(cf, "_client", lambda cfg: client_for(session))
    assert cli.cmd_record(cli.build_parser().parse_args(["record"]), config) == 0
    assert Database(config.db_path).count() == 1
    err = capsys.readouterr().err
    assert "401" in err and "deposit history" in err


def test_record_survives_an_unexpected_sync_crash(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    monkeypatch.setattr(cli, "fetch_portfolio",
                        lambda cfg, min_value=0.0: aggregate([Holding("ETH", BINANCE_SPOT, 1.0, 3000.0)]))

    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(cf, "sync", boom)
    assert cli.cmd_record(cli.build_parser().parse_args(["record"]), config) == 0
    assert Database(config.db_path).count() == 1
    assert "cash-flow sync failed: disk full" in capsys.readouterr().err


# == F2.4: normalisation ===========================================================================
@pytest.mark.parametrize("network,chain", [
    ("ETH", "ethereum"), ("BSC", "bsc"), ("ARBITRUM", "arbitrum"), ("BASE", "base"),
    ("MATIC", "polygon"), ("POL", "polygon"), ("SOL", "solana"), ("eth", "ethereum"),
    ("OPTIMISM", ""), ("BNB", ""), ("", ""),
])
def test_network_to_chain_table(network, chain):
    assert chain_for_network(network) == chain


def test_unknown_network_is_kept_and_never_matches(tmp_path):
    row = withdraw_row("w", "ETH", 1, NOW, EVM_MAIN, network="OPTIMISM")
    parsed = parse_withdrawal(row)
    assert parsed.network == "OPTIMISM"
    result = classify(parsed, owned_addresses(make_config(tmp_path)))
    assert not result.is_internal_transfer


def test_address_normalisation():
    assert normalize_address(EVM_MAIN) == EVM_MAIN.lower()
    assert normalize_address(f"  {EVM_MAIN} ") == EVM_MAIN.lower()
    assert normalize_address(SOL_PHANTOM) == SOL_PHANTOM  # base58 is case-sensitive
    assert normalize_address(None) == ""


@pytest.mark.parametrize("value,expected", [
    (1764072000000, "2025-11-25T12:00:00+00:00"),        # epoch ms
    ("1764072000000", "2025-11-25T12:00:00+00:00"),      # epoch ms as a string
    (1764072000, "2025-11-25T12:00:00+00:00"),           # epoch seconds
    ("2025-11-25 12:00:00", "2025-11-25T12:00:00+00:00"),  # withdraw applyTime (UTC)
    ("2025-11-25T13:00:00+01:00", "2025-11-25T12:00:00+00:00"),
    ("2025-11-25T12:00:00Z", "2025-11-25T12:00:00+00:00"),
    (0, ""), (None, ""), ("garbage", ""),
])
def test_to_iso_utc(value, expected):
    assert to_iso_utc(value) == expected


# == parsers ========================================================================================
def test_parse_deposit_and_withdrawal_statuses():
    assert parse_deposit(deposit_row(1, "ETH", 1, NOW, status=1)).status == "completed"
    assert parse_deposit(deposit_row(1, "ETH", 1, NOW, status=6)).status == "completed"
    assert parse_deposit(deposit_row(1, "ETH", 1, NOW, status=0)).status == "pending"
    assert parse_deposit(deposit_row(1, "ETH", 1, NOW, status=7)).status == "failed"
    w = parse_withdrawal(withdraw_row("a", "eth", 1, NOW, EVM_MAIN))
    assert (w.asset, w.direction, w.flow_type, w.status) == ("ETH", "out", "withdrawal", "completed")
    assert w.fee_amount == pytest.approx(0.0004) and w.fee_asset == "ETH"
    assert w.timestamp == iso(NOW + timedelta(minutes=5))  # completeTime wins over applyTime
    assert parse_withdrawal(withdraw_row("a", "ETH", 1, NOW, EVM_MAIN, status=4)).status == "pending"
    assert parse_withdrawal(withdraw_row("a", "ETH", 1, NOW, EVM_MAIN, status=1)).status == "failed"


def test_parsers_skip_unusable_rows():
    assert parse_deposit({"coin": "ETH", "amount": "0", "id": "1", "insertTime": NOW_MS}) is None
    assert parse_withdrawal({"coin": "", "amount": "1", "id": "1", "applyTime": "2026-01-01 00:00:00"}) is None
    assert parse_p2p_order({"asset": "USDT", "amount": "5", "createTime": NOW_MS}) is None  # no order no


def test_parse_fiat_and_p2p():
    order = parse_fiat_order({"orderNo": "1", "fiatCurrency": "eur", "amount": "100", "totalFee": "0.5",
                              "status": "Refunded", "createTime": NOW_MS}, 1)
    assert (order.asset, order.direction, order.flow_type, order.status, order.channel) == \
        ("EUR", "out", "withdrawal", "failed", "fiat")
    sell = parse_fiat_payment({"orderNo": "2", "sourceAmount": "0.01", "obtainAmount": "500",
                               "cryptoCurrency": "BTC", "fiatCurrency": "EUR", "status": "Processing",
                               "createTime": NOW_MS}, 1)
    assert (sell.asset, sell.amount, sell.direction, sell.status) == ("BTC", 0.01, "out", "pending")
    p2p = parse_p2p_order({"orderNumber": "3", "tradeType": "SELL", "asset": "USDT", "amount": "50",
                           "fiat": "VND", "totalPrice": "1300000", "orderStatus": "CANCELLED",
                           "createTime": NOW_MS})
    assert (p2p.direction, p2p.status, p2p.external_id) == ("out", "failed", "p2p:3")


# == internal transfer matching (address) ===============================================================
def test_owned_addresses_from_config(tmp_path):
    owned = owned_addresses(make_config(tmp_path))
    assert owned[EVM_MAIN.lower()].chains == {"ethereum", "arbitrum"}
    assert owned[EVM_COLD.lower()].chains == {"ethereum", "bsc", "arbitrum", "base", "polygon"}
    assert owned[SOL_PHANTOM].chains == {"solana"}


def test_withdrawal_to_owned_wallet_is_internal(tmp_path):
    owned = owned_addresses(make_config(tmp_path))
    mixed_case = EVM_MAIN.upper().replace("0X", "0x")
    result = classify(parse_withdrawal(withdraw_row("w", "ETH", 1, NOW, mixed_case, network="ARBITRUM")), owned)
    assert result.is_internal_transfer is True
    assert (result.match_method, result.match_confidence, result.counterparty_label) == ("address", 1.0, "main")
    sol = classify(parse_withdrawal(withdraw_row("s", "SOL", 3, NOW, SOL_PHANTOM, network="SOL")), owned)
    assert sol.is_internal_transfer and sol.counterparty_label == "phantom"


def test_withdrawal_on_untracked_chain_or_to_stranger_is_external(tmp_path):
    owned = owned_addresses(make_config(tmp_path))
    on_bsc = classify(parse_withdrawal(withdraw_row("w", "USDT", 5, NOW, EVM_MAIN, network="BSC")), owned)
    assert not on_bsc.is_internal_transfer  # `main` is tracked on ethereum/arbitrum only
    stranger = classify(parse_withdrawal(withdraw_row("w", "ETH", 1, NOW, STRANGER)), owned)
    assert not stranger.is_internal_transfer and stranger.match_method == ""
    assert stranger.is_external


def test_deposit_from_owned_wallet_is_internal(tmp_path):
    owned = owned_addresses(make_config(tmp_path))
    internal = classify(parse_deposit(deposit_row(1, "ETH", 1, NOW, source_address=EVM_COLD)), owned)
    assert internal.is_internal_transfer and internal.counterparty_label == "cold"
    external = classify(parse_deposit(deposit_row(2, "ETH", 1, NOW)), owned)
    assert not external.is_internal_transfer


def test_fiat_and_p2p_are_never_internal(tmp_path):
    owned = owned_addresses(make_config(tmp_path))
    p2p = parse_p2p_order({"orderNumber": "1", "tradeType": "BUY", "asset": "USDT", "amount": "5",
                           "orderStatus": "COMPLETED", "createTime": NOW_MS})
    assert not classify(replace_counterparty(p2p, EVM_MAIN, "ETH"), owned).is_internal_transfer


def replace_counterparty(flow_: CashFlow, address: str, network: str) -> CashFlow:
    from dataclasses import replace
    return replace(flow_, counterparty_address=address, network=network)


def test_sync_marks_internal_transfers(tmp_path):
    config = make_config(tmp_path)
    database = Database(config.db_path)
    sync(config, database, client=client_for(full_session()), now_ms=NOW_MS)
    rows = {r["external_id"]: r for r in flow_rows(database)}
    assert rows["w1"]["is_internal_transfer"] == 1 and rows["w1"]["match_method"] == "address"
    assert rows["w1"]["counterparty_label"] == "main"
    assert rows["w2"]["is_internal_transfer"] == 0


def test_user_override_survives_sync_and_reclassify(tmp_path):
    config = make_config(tmp_path)
    database = Database(config.db_path)
    session = full_session()
    sync(config, database, client=client_for(session), now_ms=NOW_MS)
    with database.connect() as connection:  # the user says w2 (to a stranger) was their own wallet
        connection.execute("UPDATE cash_flows SET is_internal_transfer = 1, match_method = 'manual', "
                           "match_confidence = 1, classified_by = 'user' WHERE external_id = 'w2'")
    sync(config, database, client=client_for(session), now_ms=NOW_MS + 60_000)
    row = next(r for r in flow_rows(database) if r["external_id"] == "w2")
    assert (row["is_internal_transfer"], row["match_method"], row["classified_by"]) == (1, "manual", "user")


def test_adding_a_wallet_reclassifies_history(tmp_path):
    no_wallets = make_config(tmp_path, wallets={"evm": [], "solana": []})
    database = Database(no_wallets.db_path)
    sync(no_wallets, database, client=client_for(full_session()), now_ms=NOW_MS)
    assert not any(r["is_internal_transfer"] for r in flow_rows(database))
    assert reclassify(database, owned_addresses(make_config(tmp_path))) == 1
    assert [r["external_id"] for r in flow_rows(database) if r["is_internal_transfer"]] == ["w1"]


# == pricing ===========================================================================================
def test_pricebook_uses_snapshot_before_then_after_then_fallback(tmp_path):
    database = Database(tmp_path / "h.db")
    record_snapshot(database, NOW - timedelta(days=10), [Holding("ETH", BINANCE_SPOT, 1, 2000)])
    record_snapshot(database, NOW - timedelta(days=2), [Holding("ETH", BINANCE_SPOT, 1, 2500)])
    book = PriceBook(database, {"USDT"}, fallback=lambda symbol: {"DOGE": 0.2}.get(symbol, 0))
    assert book.price("ETH", iso(NOW - timedelta(days=5))) == 2000
    assert book.price("ETH", iso(NOW)) == 2500
    assert book.price("ETH", iso(NOW - timedelta(days=30))) == 2000  # before any snapshot: oldest
    assert book.price("USDT", iso(NOW)) == 1.0 and book.price("USD", iso(NOW)) == 1.0
    assert book.price("DOGE", iso(NOW)) == 0.2
    assert book.price("NOPE", iso(NOW)) == 0.0


def test_reprice_missing_fills_unknown_prices(tmp_path):
    database = Database(tmp_path / "h.db")
    store(database, flow(NOW, asset="ETH", amount=2, price=0, external_id="z"))
    record_snapshot(database, NOW - timedelta(hours=1), [Holding("ETH", BINANCE_SPOT, 1, 3000)])
    assert reprice_missing(database, PriceBook(database)) == 1
    [row] = flow_rows(database)
    assert row["price_usd"] == 3000 and row["value_usd"] == 6000


# == config ==============================================================================================
def test_cash_flow_settings_defaults_and_validation(tmp_path):
    assert make_config(tmp_path).cash_flow_settings == CashFlowSettings()
    odd = make_config(tmp_path, cash_flows={"backfill_days": "lots", "overlap_days": 99,
                                            "enabled": "no", "sync_p2p": 0})
    settings = odd.cash_flow_settings
    assert settings.backfill_days == CashFlowSettings().backfill_days and settings.overlap_days == 30
    assert settings.enabled is False and settings.sync_p2p is False and settings.sync_fiat is True
    assert CashFlowSettings.from_raw(None) == CashFlowSettings()


# == PnL net of flows =====================================================================================
def test_pure_pnl_deposit_with_flat_market_is_zero():
    start, end = NOW - timedelta(days=7), NOW
    result = flow_adjusted_pnl(10_000, 11_000, start, end, [Flow(NOW - timedelta(days=1), 1000)])
    assert result.nav_change_usd == 1000 and result.net_flow_usd == 1000
    assert result.pnl_usd == 0 and result.return_pct == 0


def test_pure_pnl_modified_dietz():
    start, end = NOW - timedelta(days=10), NOW
    # 10k start, +5k deposited half way, ends at 16k: 1k profit on 10k + 5k*0.5 average capital
    result = flow_adjusted_pnl(10_000, 16_000, start, end, [Flow(NOW - timedelta(days=5), 5000)])
    assert result.pnl_usd == pytest.approx(1000)
    assert result.return_pct == pytest.approx(1000 / 12_500 * 100)
    out = flow_adjusted_pnl(10_000, 8_500, start, end, [Flow(NOW - timedelta(days=5), -2000)])
    assert out.withdrawals_usd == 2000 and out.pnl_usd == pytest.approx(500)
    assert flow_adjusted_pnl(0, 100, start, end, []).return_pct is None


def seed_history(database: Database):
    """NAV 10k 40 days ago, 10k 8 days ago, 12k now; the market made +500."""
    record_snapshot(database, NOW - timedelta(days=40), [Holding("USDT", BINANCE_SPOT, 10_000, 1)])
    record_snapshot(database, NOW - timedelta(days=8), [Holding("USDT", BINANCE_SPOT, 10_000, 1)])
    record_snapshot(database, NOW, [Holding("USDT", BINANCE_SPOT, 12_000, 1)])
    for key in ("binance.deposit", "binance.withdraw"):
        set_cursor(database, key, NOW_MS)


def test_report_7d_and_30d_subtract_their_own_flows(tmp_path):
    database = Database(tmp_path / "h.db")
    seed_history(database)
    store(database,
          flow(NOW - timedelta(days=3), amount=2000, external_id="in7"),          # in both windows
          flow(NOW - timedelta(days=20), amount=700, external_id="in30"),         # 30d only
          flow(NOW - timedelta(days=2), direction="out", amount=500, external_id="out7"),
          flow(NOW - timedelta(days=1), direction="out", amount=300, external_id="internal",
               is_internal_transfer=True, match_method="address", match_confidence=1.0),
          flow(NOW - timedelta(days=1), amount=999, status="pending", external_id="pend"),
          flow(NOW - timedelta(days=1), amount=888, status="failed", external_id="fail"))
    report = pnl_report(database)
    assert report["flow_adjusted"] is True and report["flows_stale"] is False
    week, month = report["windows"]["7d"], report["windows"]["30d"]
    assert week["nav_change_usd"] == 2000 and week["net_flow_usd"] == 1500 and week["pnl_usd"] == 500
    assert week["deposits_usd"] == 2000 and week["withdrawals_usd"] == 500
    assert week["flow_count"] == 2 and week["internal_transfer_count"] == 1 and week["pending_count"] == 1
    # 30d starts at the snapshot 40 days ago: the 700 deposit 20 days ago is in it as well
    assert month["net_flow_usd"] == 2200 and month["pnl_usd"] == -200
    assert month["start"]["timestamp"] == iso(NOW - timedelta(days=40)) and month["complete"]


def test_internal_transfer_leaves_pnl_untouched(tmp_path):
    """AC4: Binance -> own wallet moves value across sources; the external flow is 0."""
    config = make_config(tmp_path)
    database = Database(config.db_path)
    record_snapshot(database, NOW - timedelta(days=6), [Holding("ETH", BINANCE_SPOT, 2.0, 3000)])
    session = FakeBinance(withdrawals=[withdraw_row("t", "ETH", 1.0, NOW - timedelta(days=3), EVM_MAIN)])
    sync(config, database, client=client_for(session), now_ms=NOW_MS)
    record_snapshot(database, NOW, [Holding("ETH", BINANCE_SPOT, 1.0, 3000),
                                    Holding("ETH", ONCHAIN, 1.0, 3000, detail="ethereum/main")])
    week = pnl_report(database)["windows"]["7d"]
    assert week["net_flow_usd"] == 0 and week["pnl_usd"] == 0 and week["internal_transfer_count"] == 1


def test_external_withdrawal_is_not_a_loss(tmp_path):
    config = make_config(tmp_path)
    database = Database(config.db_path)
    record_snapshot(database, NOW - timedelta(days=6), [Holding("ETH", BINANCE_SPOT, 2.0, 3000)])
    session = FakeBinance(withdrawals=[withdraw_row("t", "ETH", 1.0, NOW - timedelta(days=3), STRANGER)])
    sync(config, database, client=client_for(session), now_ms=NOW_MS)
    record_snapshot(database, NOW, [Holding("ETH", BINANCE_SPOT, 1.0, 3300)])
    week = pnl_report(database)["windows"]["7d"]
    # NAV 6000 -> 3300 (-2700), 1 ETH (3000 at the time) left: the real PnL is +300
    assert week["nav_change_usd"] == -2700 and week["net_flow_usd"] == -3000 and week["pnl_usd"] == 300


def test_report_without_sync_is_not_flow_adjusted(tmp_path):
    database = Database(tmp_path / "h.db")
    record_snapshot(database, NOW - timedelta(days=8), [Holding("USDT", BINANCE_SPOT, 1000, 1)])
    record_snapshot(database, NOW, [Holding("USDT", BINANCE_SPOT, 3000, 1)])
    store(database, flow(NOW - timedelta(days=1), amount=2000, external_id="d"))
    report = pnl_report(database)
    assert report["flow_adjusted"] is False and report["flows_synced_until"] is None
    assert report["windows"]["7d"]["pnl_usd"] == 2000  # raw change: flows unknown, not zero


def test_short_history_and_stale_flags(tmp_path):
    database = Database(tmp_path / "h.db")
    record_snapshot(database, NOW - timedelta(days=3), [Holding("USDT", BINANCE_SPOT, 1000, 1)])
    record_snapshot(database, NOW, [Holding("USDT", BINANCE_SPOT, 1000, 1)])
    set_cursor(database, "binance.deposit", NOW_MS - DAY_MS)
    set_cursor(database, "binance.withdraw", NOW_MS)
    report = pnl_report(database)
    assert report["flows_stale"] is True
    assert report["flows_synced_until"] == iso(NOW - timedelta(days=1))
    assert report["windows"]["30d"]["complete"] is False
    assert report["windows"]["7d"]["start"]["timestamp"] == iso(NOW - timedelta(days=3))


def test_report_with_too_few_snapshots(tmp_path):
    database = Database(tmp_path / "h.db")
    assert pnl_report(database)["windows"]["7d"]["available"] is False
    record_snapshot(database, NOW, [Holding("USDT", BINANCE_SPOT, 1000, 1)])
    assert pnl_report(database)["windows"]["30d"]["reason"] == "need two snapshots in the window"
    with pytest.raises(ValueError):
        window_pnl(database, 0)


def test_backfilled_checkpoints_are_not_window_anchors(tmp_path):
    database = Database(tmp_path / "h.db")
    checkpoint = aggregate([Holding("USDT", BINANCE_SPOT, 1, 1)])
    checkpoint.timestamp = iso(NOW - timedelta(days=31))
    database.replace_backfill([checkpoint])
    record_snapshot(database, NOW - timedelta(days=5), [Holding("USDT", BINANCE_SPOT, 1000, 1)])
    record_snapshot(database, NOW, [Holding("USDT", BINANCE_SPOT, 1100, 1)])
    month = pnl_report(database)["windows"]["30d"]
    assert month["start"]["timestamp"] == iso(NOW - timedelta(days=5)) and month["complete"] is False


# == endpoint + CLI ==========================================================================================
def test_pnl_endpoint(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"settings": {"db_path": str(tmp_path / "h.db")}}), encoding="utf-8")
    database = Database(tmp_path / "h.db")
    seed_history(database)
    store(database, flow(NOW - timedelta(days=3), amount=2000, external_id="in"))
    client = TestClient(web_api.create_app(config_path))
    payload = client.get("/api/portfolio/pnl").json()
    assert payload["flow_adjusted"] is True
    assert payload["windows"]["7d"]["pnl_usd"] == 0 and set(payload["windows"]) == {"7d", "30d"}

    monkeypatch.setenv("PORTFOLIO_WEB_TOKEN", "t" * 32)
    locked = TestClient(web_api.create_app(config_path))
    assert locked.get("/api/portfolio/pnl").status_code == 401
    assert locked.get("/api/portfolio/pnl", headers={"Authorization": "Bearer " + "t" * 32}).status_code == 200


def test_cli_pnl_json_and_table(tmp_path, capsys):
    config = make_config(tmp_path, binance=False)
    database = Database(config.db_path)
    seed_history(database)
    store(database, flow(NOW - timedelta(days=3), amount=2000, external_id="in"))
    assert cli.main(["--config", str(tmp_path / "config.yaml"), "pnl", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["windows"]["7d"]["pnl_usd"] == 0
    assert cli.main(["--config", str(tmp_path / "config.yaml"), "pnl", "--windows", "7"]) == 0
    out = capsys.readouterr().out
    assert "7d" in out and "30d" not in out
    assert cli.main(["--config", str(tmp_path / "config.yaml"), "pnl", "--sync"]) == 0
    assert "skipped" in capsys.readouterr().err
