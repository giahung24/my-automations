"""P0.2 + P0.3: per-source status of each snapshot, `snapshots.trigger`, and the `detail` /
`price_id` of each asset row. Offline: every HTTP call goes to the fakes of test_all.py.

    .venv/bin/pytest test_snapshot_sources.py -q
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import clients.onchain as onchain_module
import main as cli
from models import (
    BINANCE_EARN,
    BINANCE_FUNDING,
    BINANCE_SPOT,
    ONCHAIN,
    STATUS_DISABLED,
    STATUS_ERROR,
    STATUS_INFERRED_ERROR,
    STATUS_OK,
    UNKNOWN_SOURCE,
    Holding,
    SourceStatus,
    classify_error,
    holding_sources,
    source_statuses,
)
from services.db import (
    TRIGGER_CRON,
    TRIGGER_REFRESH,
    TRIGGER_UNKNOWN,
    Database,
    is_hole,
    schema_version,
)
from services.portfolio import aggregate, fetch_portfolio, portfolio_from_db, stored_errors, to_dict
from test_all import CHAINS, FakeResponse, FakeSession, StubConfig, make_v0_db, onchain_session
from test_all import sample_holdings as all_sample_holdings

BINANCE_ALL = [BINANCE_SPOT, BINANCE_EARN, BINANCE_FUNDING]


# -- fakes ---------------------------------------------------------------------
class Http503Session(FakeSession):
    """`onchain_session`, except that the RPC hosts in `down` answer HTTP 503."""

    def __init__(self, base: FakeSession, down: tuple[str, ...]):
        super().__init__(base.routes, base.post_routes)
        self.down = down

    def post(self, url, json=None, timeout=None, **kwargs):
        if any(host in url for host in self.down):
            self.calls.append(("POST", url, json or {}))
            return FakeResponse({"error": "service unavailable"}, status=503)
        return super().post(url, json=json, timeout=timeout, **kwargs)


class FetchConfig:
    """The attributes `fetch_portfolio` reads; Binance off unless a test turns it on."""

    def __init__(self, evm_wallets=(), solana_wallets=(), binance_enabled=False, binance=None):
        self.binance_enabled = binance_enabled
        self.binance = binance if binance is not None else {"enabled": False}
        self.evm_wallets = list(evm_wallets)
        self.solana_wallets = list(solana_wallets)
        self.has_wallets = bool(self.evm_wallets or self.solana_wallets)
        self.chains = CHAINS
        self.timeout = 5
        self.max_workers = 4
        self.min_value = 0.0
        self.stablecoins = {"USDT", "USDC"}


@pytest.fixture
def onchain_with(monkeypatch):
    """Make `fetch_portfolio` build its OnChainClient on a given fake session."""
    monkeypatch.setattr(onchain_module.time, "sleep", lambda *_: None)  # 503 back-off

    def install(session):
        real = onchain_module.OnChainClient

        class Client(real):
            def __init__(self, chains, timeout=20, max_workers=8):
                super().__init__(chains, timeout=timeout, max_workers=max_workers, session=session)

        monkeypatch.setattr(onchain_module, "OnChainClient", Client)

    return install


def wallets():
    evm = [{"address": "0xdead", "label": "main", "chains": ["ethereum", "fantom"], "tokens": {}}]
    sol = [{"address": "SoLaddr", "label": "phantom", "chains": [], "tokens": {}}]
    return evm, sol


def rows(path, query, params=()):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


# -- schema v2 -----------------------------------------------------------------
def test_fresh_db_has_v2_tables_and_columns(tmp_path):
    path = tmp_path / "t.db"
    Database(path)
    assert schema_version(path) >= 2
    assert {"detail", "price_id"} <= {r[1] for r in rows(path, "PRAGMA table_info(snapshot_assets)")}
    assert "trigger" in {r[1] for r in rows(path, "PRAGMA table_info(snapshots)")}
    assert [r[1] for r in rows(path, "PRAGMA table_info(snapshot_sources)")] == [
        "snapshot_id", "source", "status", "error"]


def test_snapshot_sources_rows_are_deleted_with_their_snapshot(tmp_path):
    database = Database(tmp_path / "t.db")
    snapshot_id = database.record(aggregate(all_sample_holdings(), 0.0))
    assert database.sources(snapshot_id)
    with database.connect() as connection:
        connection.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))
    assert database.sources(snapshot_id) == {}


# -- error -> source classification ---------------------------------------------
@pytest.mark.parametrize("error, expected", [
    ("binance spot: HTTP 401", [BINANCE_SPOT]),
    ("binance simple earn flexible: timeout", [BINANCE_EARN]),
    ("binance simple earn locked: timeout", [BINANCE_EARN]),
    ("binance funding: HTTP 500", [BINANCE_FUNDING]),
    ("binance prices: unreachable", BINANCE_ALL),          # unpriced: all three are zero
    ("binance: enabled but no API key/secret configured", BINANCE_ALL),
    ("polygon/main: HTTP 429 (rate limited)", ["onchain:polygon/main"]),
    ("ethereum/main USDC: execution reverted", ["onchain:ethereum/main"]),
    ("defillama prices (3 keys): timeout", [ONCHAIN]),
    ("onchain: boom", [ONCHAIN]),
    ("something odd happened", [UNKNOWN_SOURCE]),
])
def test_classify_error_without_known_sources(error, expected):
    assert sorted(classify_error(error)) == sorted(expected)


def test_classify_error_uses_known_wallets_longest_label_first():
    known = ["onchain:ethereum/main", "onchain:ethereum/main 2", "onchain:solana/phantom"]
    assert classify_error("ethereum/main 2 USDC: timeout", known) == ["onchain:ethereum/main 2"]
    assert classify_error("ethereum/main: refused", known) == ["onchain:ethereum/main"]
    assert classify_error("ethereum/main USDC: refused", known) == ["onchain:ethereum/main"]
    # a DefiLlama outage zeroes every on-chain row, so every wallet is in error
    assert sorted(classify_error("defillama prices (2 keys): timeout", known)) == sorted(known)


def test_source_statuses_marks_ok_error_and_disabled():
    statuses = source_statuses(
        ["ethereum/main: refused", "ethereum/main USDC: reverted", "binance spot: HTTP 401"],
        expected=["binance_spot", "binance_earn", "onchain:ethereum/main", "onchain:solana/phantom"],
        disabled=["onchain:fantom/main"],
    )
    assert statuses["binance_spot"] == SourceStatus(STATUS_ERROR, "binance spot: HTTP 401")
    assert statuses["binance_earn"] == SourceStatus(STATUS_OK)
    assert statuses["onchain:ethereum/main"].status == STATUS_ERROR
    assert statuses["onchain:ethereum/main"].error.splitlines() == [
        "ethereum/main: refused", "ethereum/main USDC: reverted"]
    assert statuses["onchain:solana/phantom"] == SourceStatus(STATUS_OK)
    assert statuses["onchain:fantom/main"] == SourceStatus(STATUS_DISABLED)


def test_holding_sources_split_onchain_by_wallet():
    holdings = [Holding("ETH", ONCHAIN, 1, detail="ethereum/main"), Holding("BTC", BINANCE_SPOT, 1),
                Holding("X", ONCHAIN, 1)]
    assert holding_sources(holdings) == {"onchain:ethereum/main", BINANCE_SPOT, ONCHAIN}


# -- P0.2 AC (a): a chain answering 503 leaves an `error` row -----------------------
def test_fetch_sets_per_wallet_status_when_a_chain_is_503(onchain_with):
    onchain_with(Http503Session(onchain_session(), down=("rpc-a.test", "rpc-b.test")))
    evm, sol = wallets()
    portfolio = fetch_portfolio(FetchConfig(evm, sol))

    sources = portfolio.sources
    assert sources["onchain:ethereum/main"].status == STATUS_ERROR
    assert "503" in sources["onchain:ethereum/main"].error
    assert sources["onchain:solana/phantom"] == SourceStatus(STATUS_OK)
    assert sources["onchain:fantom/main"] == SourceStatus(STATUS_DISABLED)  # no RPC configured
    assert all(sources[s] == SourceStatus(STATUS_DISABLED) for s in BINANCE_ALL)
    assert portfolio.errors and not portfolio.ok


def test_snapshot_taken_while_a_chain_is_503_records_the_error(tmp_path, onchain_with):
    onchain_with(Http503Session(onchain_session(), down=("rpc-a.test", "rpc-b.test")))
    evm, sol = wallets()
    database = Database(tmp_path / "t.db")
    snapshot_id = database.record(fetch_portfolio(FetchConfig(evm, sol)), trigger=TRIGGER_CRON)

    stored = dict(((source, status) for source, status, _ in rows(
        tmp_path / "t.db", "SELECT source, status, error FROM snapshot_sources WHERE snapshot_id = ?",
        (snapshot_id,))))
    assert stored == {
        "binance_spot": STATUS_DISABLED,
        "binance_earn": STATUS_DISABLED,
        "binance_funding": STATUS_DISABLED,
        "onchain:ethereum/main": STATUS_ERROR,
        "onchain:fantom/main": STATUS_DISABLED,
        "onchain:solana/phantom": STATUS_OK,
    }
    assert "503" in database.sources(snapshot_id)["onchain:ethereum/main"].error


def test_fetch_with_everything_up_is_all_ok(onchain_with):
    onchain_with(onchain_session())
    evm = [{"address": "0xdead", "label": "main", "chains": ["ethereum"], "tokens": {}}]
    portfolio = fetch_portfolio(FetchConfig(evm, []))
    assert portfolio.errors == []
    assert portfolio.sources["onchain:ethereum/main"] == SourceStatus(STATUS_OK)


def test_binance_enabled_without_key_is_an_error_not_disabled(onchain_with):
    portfolio = fetch_portfolio(FetchConfig(binance={"enabled": True}))
    assert all(portfolio.sources[s].status == STATUS_ERROR for s in BINANCE_ALL)
    assert "no API key" in portfolio.sources[BINANCE_SPOT].error


def test_record_without_fetch_statuses_infers_them_from_holdings_and_errors(tmp_path):
    """Callers that only set `errors` (tests, fakes) still get a status row per source."""
    portfolio = aggregate(all_sample_holdings(), 0.0)
    portfolio.errors = ["polygon/main: HTTP 429 (rate limited)"]
    database = Database(tmp_path / "t.db")
    sources = database.sources(database.record(portfolio))
    assert sources["onchain:polygon/main"].status == STATUS_ERROR
    assert sources["onchain:ethereum/main"].status == STATUS_OK
    assert {sources[s].status for s in BINANCE_ALL} == {STATUS_OK}


# -- snapshots.trigger ------------------------------------------------------------
def test_record_stores_the_trigger(tmp_path):
    database = Database(tmp_path / "t.db")
    cron_id = database.record(aggregate(all_sample_holdings(), 0.0), trigger=TRIGGER_CRON)
    refresh_id = database.record(aggregate(all_sample_holdings(), 0.0), trigger=TRIGGER_REFRESH)
    default_id = database.record(aggregate(all_sample_holdings(), 0.0))
    triggers = dict(rows(tmp_path / "t.db", "SELECT id, trigger FROM snapshots"))
    assert triggers == {cron_id: "cron", refresh_id: "refresh", default_id: "unknown"}
    assert [s.trigger for s in database.history()] == ["cron", "refresh", "unknown"]


def test_record_rejects_an_unknown_trigger(tmp_path):
    database = Database(tmp_path / "t.db")
    with pytest.raises(ValueError, match="trigger"):
        database.record(aggregate(all_sample_holdings(), 0.0), trigger="manual")
    assert database.count() == 0


def test_cli_record_is_tagged_cron(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "fetch_portfolio", lambda cfg, min_value: aggregate(all_sample_holdings(), 0.0))
    config = StubConfig(tmp_path / "t.db", tmp_path)
    assert cli.cmd_record(cli.build_parser().parse_args(["record"]), config) == 0
    assert Database(config.db_path).latest().trigger == TRIGGER_CRON


def test_old_snapshots_keep_trigger_unknown_after_upgrade(tmp_path):
    path = make_v0_db(tmp_path / "t.db")
    assert {s.trigger for s in Database(path).history()} == {TRIGGER_UNKNOWN}


# -- P0.3: detail + price_id ------------------------------------------------------
def test_every_onchain_row_of_a_new_snapshot_has_a_detail(tmp_path, onchain_with):
    """P0.3 AC: a new snapshot has a non-empty `detail` on every on-chain row."""
    accounts = [{"account": {"data": {"parsed": {"info": {
        "mint": "Mint1111", "tokenAmount": {"uiAmountString": "42.5", "decimals": 6}}}}}}]
    onchain_with(onchain_session(sol_accounts=accounts))
    evm, sol = wallets()
    database = Database(tmp_path / "t.db")
    snapshot_id = database.record(fetch_portfolio(FetchConfig(evm, sol)))

    stored = rows(tmp_path / "t.db",
                  "SELECT symbol, detail, price_id FROM snapshot_assets WHERE snapshot_id = ? AND source = ?",
                  (snapshot_id, ONCHAIN))
    assert {symbol for symbol, _, _ in stored} == {"ETH", "USDC", "SOL", "JUP"}
    assert all(detail for _, detail, _ in stored)
    assert dict((s, (d, p)) for s, d, p in stored) == {
        "ETH": ("ethereum/main", "coingecko:ethereum"),
        "USDC": ("ethereum/main", "ethereum:0xa0b8"),
        "SOL": ("solana/phantom", "coingecko:solana"),
        "JUP": ("solana/phantom", "solana:Mint1111"),
    }


def test_assets_return_detail_and_price_id(tmp_path):
    database = Database(tmp_path / "t.db")
    portfolio = aggregate([Holding("ETH", ONCHAIN, 2.0, 3000.0, detail="base/cold",
                                   price_id="coingecko:ethereum")], 0.0)
    snapshot_id = database.record(portfolio)
    assert database.assets(snapshot_id) == [{
        "symbol": "ETH", "source": ONCHAIN, "quantity": 2.0, "price_usd": 3000.0,
        "value_usd": 6000.0, "detail": "base/cold", "price_id": "coingecko:ethereum"}]


def test_portfolio_from_db_returns_locations(tmp_path):
    database = Database(tmp_path / "t.db")
    database.record(aggregate([
        Holding("ETH", ONCHAIN, 1.0, 3000.0, detail="ethereum/main", price_id="coingecko:ethereum"),
        Holding("ETH", ONCHAIN, 1.0, 3000.0, detail="base/cold", price_id="coingecko:ethereum"),
        Holding("ETH", BINANCE_SPOT, 1.0, 3000.0),
    ], 0.0))
    portfolio = portfolio_from_db(database)
    eth = to_dict(portfolio)["assets"][0]
    assert eth["symbol"] == "ETH"
    assert eth["locations"] == ["ethereum/main", "base/cold"]
    assert {h.price_id for h in portfolio.holdings if h.source == ONCHAIN} == {"coingecko:ethereum"}


def test_upgraded_rows_get_empty_detail_and_price_id(tmp_path):
    path = make_v0_db(tmp_path / "t.db", snapshots=1)
    database = Database(path)
    assert {(r["detail"], r["price_id"]) for r in database.latest_assets()} == {("", "")}
    assert to_dict(portfolio_from_db(database))["assets"][0]["locations"] == []


# -- P0.2 AC (b): errors survive the round trip through the DB ------------------------
def test_portfolio_from_db_returns_the_stored_errors(tmp_path):
    portfolio = aggregate(all_sample_holdings(), 0.0)
    portfolio.errors = ["binance prices: unreachable", "polygon/main: HTTP 429 (rate limited)"]
    database = Database(tmp_path / "t.db")
    database.record(portfolio)

    restored = portfolio_from_db(database)
    # `binance prices` is stored on all three Binance sources but reported once
    assert sorted(restored.errors) == sorted(portfolio.errors)
    assert restored.ok is False
    assert restored.sources["onchain:polygon/main"].status == STATUS_ERROR


def test_portfolio_from_db_has_no_errors_when_everything_was_ok(tmp_path):
    database = Database(tmp_path / "t.db")
    database.record(aggregate(all_sample_holdings(), 0.0))
    restored = portfolio_from_db(database)
    assert restored.errors == [] and restored.ok
    assert restored.sources and all(s.status == STATUS_OK for s in restored.sources.values())


def test_get_portfolio_from_db_reports_errors(tmp_path):
    from test_api import CONFIGURED, fake_fetcher, make_client

    fetcher = fake_fetcher(errors=["polygon/main: HTTP 429 (rate limited)"])
    make_client(tmp_path, sections=CONFIGURED, fetcher=fetcher).post("/api/portfolio/refresh")

    # a fresh app has no live cache: GET must rebuild from the snapshot, errors included
    data = make_client(tmp_path, sections=CONFIGURED).get("/api/portfolio").json()
    assert data["origin"] == "snapshot"
    assert data["errors"] == ["polygon/main: HTTP 429 (rate limited)"]
    assert data["ok"] is False
    assert Database(tmp_path / "history.db").latest().trigger == TRIGGER_REFRESH


def test_stored_errors_reports_inferred_holes():
    errors = stored_errors({
        "onchain": SourceStatus(STATUS_INFERRED_ERROR, "inferred: onchain_usd 200.00 -> 0.00 -> 200.00"),
        "binance_spot": SourceStatus(STATUS_OK),
    })
    assert errors == ["onchain: inferred: onchain_usd 200.00 -> 0.00 -> 200.00"]


# -- P0.2 AC (c): backfill heuristic on a synthetic series with a hole ------------------
@pytest.mark.parametrize("before, value, after, expected", [
    (200.0, 0.0, 200.0, True),       # the classic hole: recorded as 0, back next time
    (200.0, 10.0, 195.0, True),      # -95% then back
    (200.0, 25.0, 200.0, False),     # -87.5%: not a > 90% drop
    (200.0, 0.0, 150.0, False),      # back by only 75% of the drop
    (200.0, 0.0, 0.0, False),        # really sold: no recovery
    (0.0, 0.0, 0.0, False),          # never had anything
    (0.0, 0.0, 100.0, False),        # a deposit, not a hole
])
def test_is_hole(before, value, after, expected):
    assert is_hole(before, value, after) is expected


def make_series(path, onchain_values, spot_values=None):
    """A v0 database whose onchain_usd (and binance_spot_usd) follow the given series."""
    path = make_v0_db(path, snapshots=len(onchain_values), assets_per_snapshot=1)
    spot_values = spot_values or [400.0] * len(onchain_values)
    connection = sqlite3.connect(path)
    for snapshot_id, (onchain, spot) in enumerate(zip(onchain_values, spot_values), start=1):
        connection.execute("UPDATE snapshots SET onchain_usd = ?, binance_spot_usd = ? WHERE id = ?",
                           (onchain, spot, snapshot_id))
    connection.commit()
    connection.close()
    return path


def test_upgrade_backfills_inferred_errors_on_a_synthetic_hole(tmp_path):
    #            id:  1      2      3    4      5      6      7     8
    onchain = [200.0, 210.0, 0.0, 205.0, 205.0, 205.0, 0.0, 0.0]   # hole at 3; 7-8 is a real exit
    spot = [400.0, 400.0, 400.0, 400.0, 5.0, 400.0, 400.0, 400.0]  # hole at 5
    path = make_series(tmp_path / "t.db", onchain, spot)

    database = Database(path)  # v0 -> latest runs the backfill once
    marked = rows(path, "SELECT snapshot_id, source, status FROM snapshot_sources ORDER BY snapshot_id")
    assert marked == [(3, ONCHAIN, STATUS_INFERRED_ERROR), (5, BINANCE_SPOT, STATUS_INFERRED_ERROR)]
    assert "210.00 -> 0.00 -> 205.00" in database.sources(3)[ONCHAIN].error
    assert database.count() == len(onchain)  # nothing lost


def test_backfill_is_idempotent_and_skips_snapshots_with_a_status(tmp_path):
    path = make_series(tmp_path / "t.db", [200.0, 0.0, 200.0])
    database = Database(path)
    assert database.backfill_inferred_errors() == 0   # already done during the upgrade
    assert len(rows(path, "SELECT * FROM snapshot_sources")) == 1

    # a v2 snapshot that drops to 0 has a real status; the heuristic must not touch it
    hole = aggregate([Holding("BTC", BINANCE_SPOT, 0.5, 60000.0)], 0.0)
    hole.timestamp = "2026-12-01T00:00:00+00:00"
    hole_id = database.record(hole)
    back = aggregate([Holding("BTC", BINANCE_SPOT, 0.5, 60000.0),
                      Holding("ETH", ONCHAIN, 1.0, 200.0, detail="ethereum/main")], 0.0)
    back.timestamp = "2026-12-02T00:00:00+00:00"
    database.record(back)
    database.backfill_inferred_errors()
    assert STATUS_INFERRED_ERROR not in {s.status for s in database.sources(hole_id).values()}


def test_backfill_does_not_run_when_opening_an_up_to_date_db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    Database(path)
    monkeypatch.setattr(Database, "backfill_inferred_errors",
                        lambda self: pytest.fail("backfill must only run on the v1 -> v2 upgrade"))
    Database(path)
