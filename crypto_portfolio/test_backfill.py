"""P0.8: five years of monthly checkpoints rebuilt from public Binance klines.

Offline: the klines endpoint is a fake that serves deterministic monthly candles.

    .venv/bin/pytest test_backfill.py -q
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_gateway
import main as cli
import services.backfill as backfill
import web_api
from models import BINANCE_EARN, BINANCE_FUNDING, BINANCE_SPOT, ONCHAIN, STATUS_INFERRED_ERROR, Holding
from services.backfill import (
    CORE_SYMBOLS,
    DEFAULT_END,
    DEFAULT_START,
    PROXY_PRICE_ID,
    BackfillError,
    fetch_monthly_opens,
    fetch_price_history,
    month_key,
    month_starts,
    price_key,
    reconstruct,
    run_backfill,
)
from services.db import (
    BACKFILL_SCHEMA_VERSION,
    SCHEMA_VERSION,
    TRIGGER_BACKFILL,
    TRIGGER_CRON,
    Database,
    schema_version,
)
from services.portfolio import aggregate, portfolio_from_db
from test_all import FakeResponse, FakeSession, make_v0_db
from test_api import isolated_env, write_config  # noqa: F401  (autouse: no real .env)

BASE_PRICES = {"BTC": 20000.0, "ETH": 1500.0, "SOL": 100.0, "BNB": 300.0}
NOW_PRICES = {"BTC": 60000.0, "ETH": 3000.0, "SOL": 150.0, "BNB": 600.0, "LINK": 15.0,
              "USDT": 1.0, "USDC": 1.0}


# -- fakes ---------------------------------------------------------------------
def month_index(moment: datetime) -> int:
    return moment.year * 12 + moment.month - 1


def kline_price(asset: str, moment: datetime) -> float:
    """Deterministic, distinct per asset and month: base x (1 + 1% per month since 2020)."""
    return BASE_PRICES[asset] * (1 + 0.01 * (month_index(moment) - 2020 * 12))


def candle(moment: datetime, price: float) -> list:
    open_ms = int(moment.timestamp() * 1000)
    return [open_ms, f"{price:.8f}", f"{price * 1.2:.8f}", f"{price * 0.8:.8f}", f"{price * 1.1:.8f}",
            "1000.0", open_ms + 2_591_999_999, "0", 1, "0", "0", "0"]


def klines_payload(listed_from: dict[str, datetime] | None = None):
    """Callable route for FakeSession: monthly candles between startTime and endTime."""
    listed_from = listed_from or {}

    def respond(params: dict) -> list:
        pair = params["symbol"]
        asset = pair[: -len("USDT")]
        if asset not in BASE_PRICES:
            raise AssertionError(f"unexpected pair {pair}")
        assert params["interval"] == "1M"
        start = datetime.fromtimestamp(params["startTime"] / 1000, timezone.utc)
        end = datetime.fromtimestamp(params["endTime"] / 1000, timezone.utc)
        months = [m for m in month_starts(start.date(), end.date())
                  if m >= listed_from.get(asset, m)][: params["limit"]]
        return [candle(m, kline_price(asset, m)) for m in months]

    return respond


def klines_session(**kwargs) -> FakeSession:
    return FakeSession({"/api/v3/klines": klines_payload(**kwargs)})


class RecordingSession:
    """Answers per host: an Exception is raised, a FakeResponse returned, else candles."""

    def __init__(self, by_host: dict):
        self.by_host = by_host
        self.headers = {}
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        for host, answer in self.by_host.items():
            if url.startswith(host):
                if isinstance(answer, Exception):
                    raise answer
                if isinstance(answer, FakeResponse):
                    return answer
                return FakeResponse(klines_payload()(params))
        raise AssertionError(f"unexpected url {url}")


def holdings() -> list[Holding]:
    return [
        Holding("BTC", BINANCE_EARN, 0.5, NOW_PRICES["BTC"]),
        Holding("WBTC", ONCHAIN, 0.1, NOW_PRICES["BTC"], detail="ethereum/main"),
        Holding("ETH", ONCHAIN, 2.0, NOW_PRICES["ETH"], detail="ethereum/main"),
        Holding("SOL", BINANCE_SPOT, 10.0, NOW_PRICES["SOL"]),
        Holding("USDC", BINANCE_EARN, 5000.0, 1.0),
        Holding("USDT", BINANCE_FUNDING, 250.0, 1.0),
        Holding("LINK", ONCHAIN, 100.0, NOW_PRICES["LINK"], detail="ethereum/main"),
        Holding("DUST", BINANCE_SPOT, 0.0, 5.0),        # empty: ignored
        Holding("NOPRICE", BINANCE_SPOT, 10.0, 0.0),    # unpriced: ignored
    ]


def seed_real(db_path: Path, when: datetime, items: list[Holding] | None = None) -> int:
    portfolio = aggregate(items if items is not None else holdings(), 0.0)
    portfolio.timestamp = when.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return Database(db_path).record(portfolio, trigger=TRIGGER_CRON)


def priced(items: list[Holding]) -> list[Holding]:
    return [h for h in items if h.quantity > 0 and h.price_usd > 0]


def recent_range(months: int = 60) -> tuple[date, date]:
    """The `months` full months before the current one (keeps the API tests time-proof)."""
    now = datetime.now(timezone.utc)
    end_index = now.year * 12 + now.month - 2
    start_index = end_index - months + 1
    return (date(start_index // 12, start_index % 12 + 1, 1), date(end_index // 12, end_index % 12 + 1, 1))


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "history.db")
    seed_real(database.path, datetime(2026, 11, 22, 2, 32, 10, tzinfo=timezone.utc))
    return database


# -- calendar ------------------------------------------------------------------
def test_default_range_is_60_consecutive_month_starts():
    months = month_starts(DEFAULT_START, DEFAULT_END)
    assert len(months) == 60
    assert months[0] == datetime(2021, 11, 1, tzinfo=timezone.utc)
    assert months[-1] == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert all(m.day == 1 and m.hour == 0 and m.tzinfo == timezone.utc for m in months)
    assert [month_index(b) - month_index(a) for a, b in zip(months, months[1:])] == [1] * 59


def test_month_starts_rejects_an_inverted_range():
    with pytest.raises(ValueError):
        month_starts(date(2026, 1, 1), date(2025, 12, 1))


def test_parse_month():
    assert backfill.parse_month("2021-11") == date(2021, 11, 1)
    assert backfill.parse_month("2026-10-15") == date(2026, 10, 1)


# -- prices --------------------------------------------------------------------
def test_price_key_pegs_stablecoins_and_unwraps_tokens():
    assert price_key("USDT") == price_key("usdc") == price_key("FDUSD") == "USDT"
    assert price_key("WBTC") == price_key("BTCB") == "BTC"
    assert price_key("WETH") == "ETH" and price_key("WSOL") == "SOL"
    assert price_key("LINK") == "LINK"


def test_fetch_uses_public_monthly_klines_without_api_key():
    months = month_starts(DEFAULT_START, DEFAULT_END)
    session = klines_session()
    prices = fetch_monthly_opens("BTC", months, session=session)

    ((method, url, params),) = session.calls
    assert method == "GET" and url == "https://data-api.binance.vision/api/v3/klines"
    assert params == {"symbol": "BTCUSDT", "interval": "1M", "limit": 60,
                      "startTime": 1635724800000,   # 2021-11-01T00:00:00Z
                      "endTime": 1790812800000}     # 2026-10-01T00:00:00Z
    assert "X-MBX-APIKEY" not in session.headers and "signature" not in params
    assert list(prices) == [month_key(m) for m in months]
    assert prices["2021-11"] == pytest.approx(kline_price("BTC", months[0]))  # the candle's open


def test_fetch_fails_over_to_the_next_public_host():
    months = month_starts(date(2024, 1, 1), date(2024, 3, 1))
    session = RecordingSession({
        "https://data-api.binance.vision": requests.ConnectionError("dns"),
        "https://api.binance.com": None,
    })
    assert len(fetch_monthly_opens("ETH", months, session=session)) == 3
    assert [url.rsplit("/api/v3", 1)[0] for url, _ in session.calls] == [
        "https://data-api.binance.vision", "https://api.binance.com"]


def test_fetch_raises_when_every_host_fails():
    session = RecordingSession({
        "https://data-api.binance.vision": FakeResponse({"code": 0, "msg": "restricted"}, status=451),
        "https://api.binance.com": requests.Timeout("slow"),
    })
    with pytest.raises(BackfillError, match="every host failed"):
        fetch_monthly_opens("BTC", month_starts(date(2024, 1, 1), date(2024, 2, 1)), session=session)


@pytest.mark.parametrize("payload", [{"code": -1121}, [[1704067200000]], [[1704067200000, "0"]]])
def test_fetch_rejects_malformed_candles(payload):
    session = FakeSession({"/api/v3/klines": payload})
    with pytest.raises(BackfillError):
        fetch_monthly_opens("BTC", month_starts(date(2024, 1, 1), date(2024, 1, 1)), session=session)


def test_price_history_pegs_usdt_and_fetches_each_asset_once():
    months = month_starts(DEFAULT_START, DEFAULT_END)
    session = klines_session()
    history = fetch_price_history(["BTC", "ETH", "SOL", "USDT", "WBTC", "USDC"], months, session=session)
    assert set(history) == {"BTC", "ETH", "SOL", "USDT"}
    assert set(history["USDT"].values()) == {1.0} and len(history["USDT"]) == 60
    assert all(len(history[a]) == 60 for a in CORE_SYMBOLS)
    assert sorted(params["symbol"] for _, _, params in session.calls) == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_price_history_requires_every_month_for_core_assets():
    months = month_starts(DEFAULT_START, DEFAULT_END)
    late = klines_session(listed_from={"SOL": datetime(2022, 3, 1, tzinfo=timezone.utc)})
    with pytest.raises(BackfillError, match=r"SOLUSDT: no monthly price for 4 checkpoint\(s\) \(2021-11"):
        fetch_price_history(CORE_SYMBOLS, months, session=late)


def test_price_history_tolerates_late_listing_of_extra_assets():
    months = month_starts(DEFAULT_START, DEFAULT_END)
    late = klines_session(listed_from={"BNB": datetime(2023, 5, 1, tzinfo=timezone.utc)})
    history = fetch_price_history([*CORE_SYMBOLS, "BNB"], months, session=late)
    assert "2023-04" not in history["BNB"] and "2023-05" in history["BNB"]
    bnb = [Holding("BNB", BINANCE_SPOT, 1.0, 600.0), Holding("BTC", BINANCE_SPOT, 0.01, 60000.0)]
    assert backfill.coverage(bnb, history, months) == pytest.approx(0.5)  # BNB lacks early months
    checkpoints = reconstruct(bnb, history, months)
    assert checkpoints[0].holdings[0].price_id == PROXY_PRICE_ID              # 2021-11: proxied
    assert checkpoints[-1].holdings[0].price_id == "backfill:binance:BNBUSDT"  # 2026-10: real price


# -- reconstruction ------------------------------------------------------------
def test_reconstruct_revalues_current_quantities_at_past_prices():
    months = month_starts(DEFAULT_START, DEFAULT_END)
    prices = fetch_price_history(CORE_SYMBOLS, months, session=klines_session())
    base = priced(holdings())
    checkpoints = reconstruct(base, prices, months)

    assert len(checkpoints) == 60
    for moment, portfolio in zip(months, checkpoints):
        assert portfolio.timestamp == moment.isoformat()
        p = {asset: kline_price(asset, moment) for asset in CORE_SYMBOLS}
        priced_then = 0.6 * p["BTC"] + 2.0 * p["ETH"] + 10.0 * p["SOL"] + 5000.0 + 250.0
        priced_now = 0.6 * 60000 + 2.0 * 3000 + 10.0 * 150 + 5250.0
        factor = priced_then / priced_now
        link_then = 100.0 * 15.0 * factor  # LINK has no series: moves like the priced basket

        by_symbol = {h.symbol: h for h in portfolio.holdings}
        assert by_symbol["BTC"].price_usd == pytest.approx(p["BTC"])
        assert by_symbol["WBTC"].price_usd == pytest.approx(p["BTC"])
        assert by_symbol["USDC"].price_usd == by_symbol["USDT"].price_usd == 1.0
        assert by_symbol["LINK"].price_id == PROXY_PRICE_ID
        assert by_symbol["BTC"].price_id == "backfill:binance:BTCUSDT"
        assert by_symbol["USDC"].price_id == "backfill:peg:USDT"
        assert portfolio.total_usd == pytest.approx(priced_then + link_then)
        assert {h.quantity for h in portfolio.holdings} == {h.quantity for h in base}


def test_nav_equals_current_nav_times_weighted_price_relatives():
    """NAV(t) = NAV_now x sum(w_i x p_i(t)/p_i(now)), weights renormalised over priced assets."""
    months = month_starts(date(2022, 6, 1), date(2022, 6, 1))
    prices = fetch_price_history(CORE_SYMBOLS, months, session=klines_session())
    base = priced(holdings())
    nav_now = sum(h.value_usd for h in base)
    covered = [h for h in base if prices.get(price_key(h.symbol))]
    weight_total = sum(h.value_usd for h in covered)
    relative = sum(h.value_usd / weight_total * prices[price_key(h.symbol)]["2022-06"] / h.price_usd
                   for h in covered)
    (checkpoint,) = reconstruct(base, prices, months)
    assert checkpoint.total_usd == pytest.approx(nav_now * relative)


def test_reconstruct_keeps_the_source_split_consistent():
    months = month_starts(date(2023, 1, 1), date(2023, 3, 1))
    prices = fetch_price_history(CORE_SYMBOLS, months, session=klines_session())
    for portfolio in reconstruct(priced(holdings()), prices, months):
        for source in (BINANCE_SPOT, BINANCE_EARN, BINANCE_FUNDING, ONCHAIN):
            expected = sum(h.value_usd for h in portfolio.holdings if h.source == source)
            assert portfolio.totals[source] == pytest.approx(expected)
        assert sum(portfolio.totals.values()) == pytest.approx(portfolio.total_usd)


def test_reconstruct_needs_at_least_one_priced_holding():
    months = month_starts(date(2023, 1, 1), date(2023, 1, 1))
    with pytest.raises(BackfillError, match="none of the current holdings"):
        reconstruct([Holding("LINK", ONCHAIN, 10.0, 15.0)], {"USDT": {"2023-01": 1.0}}, months)


# -- storage -------------------------------------------------------------------
def test_migration_v3_adds_is_backfilled_defaulting_to_zero(tmp_path):
    path = make_v0_db(tmp_path / "old.db")
    database = Database(path)
    assert schema_version(path) == SCHEMA_VERSION >= BACKFILL_SCHEMA_VERSION
    with sqlite3.connect(path) as connection:
        assert {row[0] for row in connection.execute("SELECT DISTINCT is_backfilled FROM snapshots")} == {0}
    assert database.count(backfilled=True) == 0 and database.count(backfilled=False) == database.count()


def test_record_refuses_the_backfill_trigger(tmp_path):
    with pytest.raises(ValueError, match="backfill"):
        Database(tmp_path / "x.db").record(aggregate(holdings()), trigger=TRIGGER_BACKFILL)


def test_run_backfill_stores_60_flagged_monthly_checkpoints(db):
    result = run_backfill(db, session=klines_session())

    assert len(result.snapshot_ids) == 60 and not result.skipped and not result.dry_run
    assert db.count(backfilled=True) == 60 and db.count(backfilled=False) == 1
    backfilled = [s for s in db.history() if s.is_backfilled]
    assert [s.timestamp for s in backfilled] == [m.isoformat() for m in month_starts(DEFAULT_START, DEFAULT_END)]
    assert {s.trigger for s in backfilled} == {TRIGGER_BACKFILL}
    assert all(s.total_portfolio_usd > 0 for s in backfilled)
    assert all(db.sources(s.id) == {} for s in backfilled)  # nothing was fetched
    rows = db.assets(backfilled[0].id)
    assert {r["symbol"] for r in rows} == {"BTC", "WBTC", "ETH", "SOL", "USDC", "USDT", "LINK"}
    assert all(r["price_id"].startswith("backfill:") for r in rows)
    assert result.coverage == pytest.approx(1 - 1500.0 / sum(h.value_usd for h in priced(holdings())))


def test_backfill_does_not_hijack_the_latest_snapshot(db):
    real = db.latest()
    run_backfill(db, session=klines_session())
    assert db.latest() == real and not real.is_backfilled
    assert portfolio_from_db(db).total_usd == pytest.approx(real.total_portfolio_usd)
    # a 24h-change reference is always a fetched snapshot, never a monthly checkpoint
    assert db.snapshot_before("2026-11-21T00:00:00+00:00") is None
    assert db.snapshot_after("2000-01-01T00:00:00+00:00") == real
    assert len(db.history(include_backfilled=False)) == 1 and len(db.history()) == 61


def test_rerun_replaces_instead_of_duplicating(db):
    first = run_backfill(db, session=klines_session()).snapshot_ids
    second = run_backfill(db, session=klines_session()).snapshot_ids
    assert db.count(backfilled=True) == 60
    assert not set(first) & set(second)
    with sqlite3.connect(db.path) as connection:  # cascaded: no orphan asset rows
        orphans = connection.execute(
            "SELECT COUNT(*) FROM snapshot_assets WHERE snapshot_id NOT IN (SELECT id FROM snapshots)"
        ).fetchone()[0]
    assert orphans == 0


def test_dry_run_writes_nothing(db):
    result = run_backfill(db, session=klines_session(), dry_run=True)
    assert len(result.checkpoints) == 60 and result.snapshot_ids == []
    assert db.count() == 1


def test_failed_price_fetch_leaves_previous_backfill_intact(db):
    run_backfill(db, session=klines_session())
    before = [s.id for s in db.history() if s.is_backfilled]
    late = klines_session(listed_from={"ETH": datetime(2022, 1, 1, tzinfo=timezone.utc)})
    with pytest.raises(BackfillError):
        run_backfill(db, session=late)
    assert [s.id for s in db.history() if s.is_backfilled] == before


def test_months_covered_by_real_snapshots_are_skipped(tmp_path):
    database = Database(tmp_path / "h.db")
    seed_real(database.path, datetime(2026, 9, 15, tzinfo=timezone.utc))
    seed_real(database.path, datetime(2026, 11, 1, tzinfo=timezone.utc))
    result = run_backfill(database, session=klines_session())
    assert result.skipped == ["2026-10"]
    assert database.count(backfilled=True) == 59
    assert max(s.timestamp for s in database.history() if s.is_backfilled) == "2026-09-01T00:00:00+00:00"


def test_backfill_needs_a_fetched_snapshot(tmp_path):
    with pytest.raises(BackfillError, match="record"):
        run_backfill(Database(tmp_path / "empty.db"), session=klines_session())


def test_hole_heuristic_ignores_backfilled_checkpoints(db):
    run_backfill(db, session=klines_session())
    assert db.backfill_inferred_errors() == 0
    with sqlite3.connect(db.path) as connection:
        inferred = connection.execute(
            "SELECT COUNT(*) FROM snapshot_sources WHERE status = ?", (STATUS_INFERRED_ERROR,)
        ).fetchone()[0]
    assert inferred == 0


# -- charts --------------------------------------------------------------------
def make_web_client(tmp_path) -> tuple[TestClient, Database]:
    cfg = write_config(tmp_path / "config.yaml")
    database = Database(tmp_path / "history.db")
    seed_real(database.path, datetime.now(timezone.utc) - timedelta(minutes=5))
    start, end = recent_range()
    run_backfill(database, start=start, end=end, session=klines_session())
    return TestClient(web_api.create_app(cfg, fetcher=lambda *a: aggregate(holdings()))), database


@pytest.mark.parametrize("days, at_least_backfilled", [(365, 11), (1827, 59), (0, 60)])
def test_long_range_views_are_not_empty(tmp_path, days, at_least_backfilled):
    client, _ = make_web_client(tmp_path)
    body = client.get("/api/portfolio/history", params={"days": days}).json()
    flagged = [p for p in body["points"] if p["is_backfilled"]]
    assert len(flagged) >= at_least_backfilled
    assert body["points"][-1]["is_backfilled"] is False  # the newest point is the real snapshot
    times = [p["time"] for p in body["points"]]
    assert times == sorted(times) and len(set(times)) == len(times)
    flagged_times = [p["time"] for p in flagged]
    gaps = [b - a for a, b in zip(flagged_times, flagged_times[1:])]
    assert all(28 * 86400 <= gap <= 31 * 86400 for gap in gaps)  # one point per month, no hole


def test_portfolio_endpoint_still_serves_the_real_snapshot(tmp_path):
    client, database = make_web_client(tmp_path)
    data = client.get("/api/portfolio").json()
    assert data["snapshot_id"] == database.latest().id
    assert data["total_usd"] == pytest.approx(database.latest().total_portfolio_usd, abs=0.01)


def test_gateway_still_reports_the_real_latest_snapshot(tmp_path):
    _, database = make_web_client(tmp_path)
    settings = api_gateway.Settings(api_key="k" * 43, db_path=database.path,
                                    allowed_hosts=frozenset({"testserver"}))
    client = TestClient(api_gateway.create_app(settings))
    body = client.get(api_gateway.ENDPOINT, headers={"X-API-Key": "k" * 43}).json()
    assert float(body["value"]) == pytest.approx(database.latest().total_portfolio_usd, abs=0.01)
    assert body["stale"] is False


def test_dashboard_offers_1y_5y_and_all_ranges():
    app_js = (Path(__file__).resolve().parent / "static" / "app.js").read_text(encoding="utf-8")
    for range_id in ("'1Y', days: 365", "'5Y', days: 1827", "'ALL', days: 0"):
        assert f"id: {range_id}" in app_js


# -- CLI -----------------------------------------------------------------------
def test_main_backfill_command(tmp_path, monkeypatch, capsys):
    cfg = write_config(tmp_path / "config.yaml")
    seed_real(tmp_path / "history.db", datetime(2026, 11, 22, tzinfo=timezone.utc))
    monkeypatch.setattr(backfill.requests, "Session", lambda: klines_session())

    assert cli.main(["--config", str(cfg), "backfill", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "2021-11" in out and "2026-10" in out and "would store 60" in out
    assert Database(tmp_path / "history.db").count(backfilled=True) == 0

    assert cli.main(["--config", str(cfg), "backfill", "--start", "2025-01", "--end", "2025-12"]) == 0
    assert "stored 12" in capsys.readouterr().out
    assert Database(tmp_path / "history.db").count(backfilled=True) == 12

    assert cli.main(["--config", str(cfg), "history", "--limit", "3"]) == 0
    assert "(backfilled)" in capsys.readouterr().out


def test_main_backfill_reports_failure(tmp_path, capsys):
    cfg = write_config(tmp_path / "config.yaml")  # empty database: nothing to backfill from
    assert cli.main(["--config", str(cfg), "backfill"]) == 1
    assert "backfill failed" in capsys.readouterr().err


def test_module_entry_point(tmp_path, monkeypatch, capsys):
    seed_real(tmp_path / "m.db", datetime(2026, 11, 22, tzinfo=timezone.utc))
    monkeypatch.setattr(backfill.requests, "Session", lambda: klines_session())
    assert backfill.main(["--db", str(tmp_path / "m.db"), "--start", "2026-01", "--end", "2026-03"]) == 0
    assert "stored 3" in capsys.readouterr().out
