"""Activity tab (Epic M4): GET /api/cash-flows, candidates, PATCH confirmation. No network.

    .venv/bin/pytest test_activity.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

import web_api
from services.cash_flows import CashFlow, upsert_flows
from services.db import Database

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def no_fetch(config, min_value):
    raise AssertionError("the activity routes must never fetch")


@pytest.fixture
def env(tmp_path):
    db_path = tmp_path / "history.db"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"settings": {"db_path": str(db_path)}}), encoding="utf-8")
    client = TestClient(web_api.create_app(cfg, fetcher=no_fetch))
    return client, Database(db_path)


def iso(delta: timedelta) -> str:
    return (NOW - delta).isoformat()


def flow(external_id: str, *, ago=timedelta(days=1), asset="ETH", direction="out", amount=1.0,
         flow_type=None, price=2000.0, **extra) -> CashFlow:
    flow_type = flow_type or ("withdrawal" if direction == "out" else "deposit")
    return CashFlow(timestamp=iso(ago), asset=asset, direction=direction, amount=amount, flow_type=flow_type,
                    external_id=external_id, price_usd=price, **extra)


def ids(database: Database) -> dict[str, int]:
    with database.connect() as connection:
        return {row["external_id"]: row["id"] for row in connection.execute("SELECT id, external_id FROM cash_flows")}


def row(database: Database, flow_id: int):
    with database.connect() as connection:
        return dict(connection.execute("SELECT * FROM cash_flows WHERE id = ?", (flow_id,)).fetchone())


def pair(database: Database, a: int, b: int, method="heuristic", confidence=0.95) -> None:
    with database.connect() as connection:
        for x, y in ((a, b), (b, a)):
            connection.execute("UPDATE cash_flows SET is_internal_transfer = 1, matched_flow_id = ?, "
                               "match_method = ?, match_confidence = ? WHERE id = ?", (y, method, confidence, x))


# -- GET /api/cash-flows ---------------------------------------------------------------
def test_empty_database_answers_an_empty_timeline(env):
    client, _ = env
    data = client.get("/api/cash-flows").json()
    assert data["items"] == [] and data["next_cursor"] is None
    assert data["needs_review"] == 0 and data["synced"] is False
    assert data["facets"]["kind"]["all"] == 0


def test_paired_transfer_is_one_item_and_not_an_external_flow(env):
    client, database = env
    upsert_flows(database, [
        flow("w1", direction="out", amount=0.5, account_label="Binance"),
        CashFlow(timestamp=iso(timedelta(hours=23)), asset="ETH", direction="in", amount=0.499, flow_type="deposit",
                 external_id="m1", source="manual", account="My Ledgy", account_label="My Ledgy",
                 channel="manual", price_usd=2000.0, detection_method="manual"),
        flow("d1", direction="in", amount=1000, asset="USDT", price=1.0, channel="p2p"),
    ])
    found = ids(database)
    pair(database, found["w1"], found["m1"])

    data = client.get("/api/cash-flows").json()
    transfers = [i for i in data["items"] if i["kind"] == "transfer"]
    assert len(transfers) == 1 and len(data["items"]) == 2
    assert transfers[0]["from"] == "Binance" and transfers[0]["to"] == "My Ledgy"
    assert transfers[0]["sources"] == ["binance", "manual"] and transfers[0]["needs_review"] is False
    assert data["net_external_usd"] == 1000.0  # the transfer moved money between my own accounts
    assert data["facets"]["kind"]["transfer"] == 1 and data["facets"]["kind"]["deposit"] == 1


def test_earn_rewards_fold_into_one_item_per_day(env):
    client, database = env
    upsert_flows(database, [
        flow(f"r{i}", direction="in", asset=asset, amount=amount, price=price, flow_type="reward", channel="earn",
             ago=timedelta(days=1, minutes=i))
        for i, (asset, amount, price) in enumerate([("USDC", 2.0, 1.0), ("BTC", 0.00001, 60000.0), ("USDC", 0.31, 1.0)])
    ] + [flow("r9", direction="in", asset="USDC", amount=1.0, price=1.0, flow_type="reward", channel="earn",
              ago=timedelta(days=3))])

    data = client.get("/api/cash-flows?type=reward").json()
    assert [i["group"] for i in data["items"]] == [True, True]
    day = data["items"][0]
    assert day["count"] == 3 and day["value_usd"] == pytest.approx(2.91)
    assert [a["asset"] for a in day["assets"]] == ["USDC", "BTC"]
    assert day["assets"][0]["amount"] == pytest.approx(2.31)
    assert data["earned_usd"] == pytest.approx(3.91)


def test_unmatched_withdrawal_needs_review_and_review_filter(env):
    client, database = env
    upsert_flows(database, [
        flow("old", direction="out", ago=timedelta(days=2), counterparty_address="0x" + "9f" * 20),
        flow("fresh", direction="out", ago=timedelta(minutes=10)),            # still in transit
        flow("fiat", direction="in", asset="EUR", channel="fiat", price=1.1),  # fiat is external by nature
    ])
    data = client.get("/api/cash-flows").json()
    flagged = [i for i in data["items"] if i["needs_review"]]
    assert data["needs_review"] == 1 and len(flagged) == 1
    assert flagged[0]["to"] == "0x9f9f…9f9f"

    only = client.get("/api/cash-flows?review=1").json()
    assert [i["id"] for i in only["items"]] == [flagged[0]["id"]]


def test_low_confidence_pairing_needs_review(env):
    client, database = env
    upsert_flows(database, [flow("a", direction="out"), flow("b", direction="in", source="evm:ethereum",
                                                                account="0x" + "ab" * 20, account_label="main")])
    found = ids(database)
    pair(database, found["a"], found["b"], confidence=0.7)
    item = client.get("/api/cash-flows").json()["items"][0]
    assert item["kind"] == "transfer" and item["needs_review"] is True


def test_source_filter_groups_evm_and_solana_as_onchain(env):
    client, database = env
    upsert_flows(database, [
        flow("bn", direction="in"),
        flow("evm", direction="in", source="evm:base", account="0x" + "ab" * 20, channel="onchain"),
        flow("sol", direction="in", asset="SOL", source="solana", account="So1", channel="onchain"),
    ])
    data = client.get("/api/cash-flows?source=onchain").json()
    assert sorted(i["asset"] for i in data["items"]) == ["ETH", "SOL"]
    assert data["facets"]["source"] == {"binance": 1, "onchain": 2, "manual": 0, "all": 3}


def test_cursor_walks_every_item_once(env):
    client, database = env
    upsert_flows(database, [flow(f"f{i}", direction="in" if i % 2 else "out", ago=timedelta(hours=i // 3 + 1),
                                 channel="fiat") for i in range(120)])
    seen, cursor = [], None
    for _ in range(10):
        url = "/api/cash-flows?limit=50" + (f"&cursor={cursor}" if cursor else "")
        data = client.get(url).json()
        seen += [i["id"] for i in data["items"]]
        cursor = data["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 120 and len(set(seen)) == 120


def test_days_window_and_bad_parameters(env):
    client, database = env
    upsert_flows(database, [flow("recent", ago=timedelta(days=5)), flow("old", ago=timedelta(days=200))])
    assert client.get("/api/cash-flows?days=30").json()["total"] == 1
    assert client.get("/api/cash-flows?days=0").json()["total"] == 2
    for bad in ("source=kraken", "type=trade", "limit=500", "days=-1", "cursor=nope"):
        assert client.get(f"/api/cash-flows?{bad}").status_code == 422, bad


def test_token_guards_the_activity_routes(env, monkeypatch):
    client, _ = env
    monkeypatch.setenv("PORTFOLIO_WEB_TOKEN", "t0k3n")
    assert client.get("/api/cash-flows").status_code == 401
    assert client.patch("/api/cash-flows/1", json={"is_internal_transfer": False}).status_code == 401


# -- candidates + PATCH ------------------------------------------------------------------
def seed_transfer(database):
    upsert_flows(database, [
        flow("w", direction="out", amount=0.5, fee_amount=0.001, fee_asset="ETH", ago=timedelta(days=2)),
        CashFlow(timestamp=iso(timedelta(days=2) - timedelta(hours=1)), asset="ETH", direction="in", amount=0.499,
                 flow_type="deposit", external_id="m", source="manual", account="My Ledgy",
                 account_label="My Ledgy", channel="manual", price_usd=2000.0, detection_method="manual"),
        flow("other-in", direction="in", amount=3.0, ago=timedelta(days=4), source="evm:ethereum",
             account="0x" + "ab" * 20, account_label="main"),
        flow("same-account", direction="in", amount=0.5, ago=timedelta(days=2)),  # Binance -> Binance: no
        flow("same-way", direction="out", amount=0.5, ago=timedelta(days=2, hours=2)),
        flow("btc-in", direction="in", asset="BTC", amount=0.5, ago=timedelta(days=2)),
    ])
    return ids(database)


def test_candidates_are_opposite_direction_same_asset_best_first(env):
    client, database = env
    found = seed_transfer(database)
    data = client.get(f"/api/cash-flows/{found['w']}/candidates").json()
    assert [c["id"] for c in data["candidates"]] == [found["m"], found["other-in"]]
    assert data["candidates"][0]["amount_gap_pct"] == pytest.approx(0.0)
    assert client.get("/api/cash-flows/999/candidates").status_code == 404


def test_patch_pairs_both_legs_and_survives_a_resync(env):
    client, database = env
    found = seed_transfer(database)
    response = client.patch(f"/api/cash-flows/{found['w']}",
                            json={"is_internal_transfer": True, "matched_flow_id": found["m"], "note": "to Ledger"})
    assert response.status_code == 200 and sorted(response.json()["changed"]) == sorted([found["w"], found["m"]])
    for flow_id, mate in ((found["w"], found["m"]), (found["m"], found["w"])):
        stored = row(database, flow_id)
        assert stored["is_internal_transfer"] == 1 and stored["matched_flow_id"] == mate
        assert stored["classified_by"] == "user" and stored["match_method"] == "manual"
    assert row(database, found["w"])["note"] == "to Ledger"

    # the next sync re-reads the withdrawal with an automatic classification: the verdict stays
    upsert_flows(database, [flow("w", direction="out", amount=0.5, fee_amount=0.001, fee_asset="ETH",
                                 ago=timedelta(days=2))])
    assert row(database, found["w"])["is_internal_transfer"] == 1

    items = client.get("/api/cash-flows").json()["items"]
    assert sum(1 for i in items if i["kind"] == "transfer") == 1


def test_patch_rejects_impossible_pairs(env):
    client, database = env
    found = seed_transfer(database)
    w = f"/api/cash-flows/{found['w']}"
    assert client.patch(w, json={"is_internal_transfer": True, "matched_flow_id": found["same-way"]}).status_code == 409
    assert client.patch(w, json={"is_internal_transfer": True, "matched_flow_id": found["btc-in"]}).status_code == 409
    assert client.patch(w, json={"is_internal_transfer": True, "matched_flow_id": found["w"]}).status_code == 409
    pair(database, found["m"], found["same-way"])  # not a real pair, but it makes `m` taken
    assert client.patch(w, json={"is_internal_transfer": True, "matched_flow_id": found["m"]}).status_code == 409
    assert client.patch("/api/cash-flows/999", json={"is_internal_transfer": False}).status_code == 404


def test_patch_external_undoes_a_pairing_on_both_legs(env):
    client, database = env
    found = seed_transfer(database)
    pair(database, found["w"], found["m"])
    assert client.patch(f"/api/cash-flows/{found['w']}", json={"is_internal_transfer": False}).status_code == 200
    for flow_id in (found["w"], found["m"]):
        stored = row(database, flow_id)
        assert stored["is_internal_transfer"] == 0 and stored["matched_flow_id"] is None
        assert stored["classified_by"] == "user"
    item = next(i for i in client.get("/api/cash-flows").json()["items"] if i["id"] == found["w"])
    assert item["kind"] == "withdrawal" and item["needs_review"] is False


def test_patch_internal_without_a_mate_clears_the_review(env):
    client, database = env
    found = seed_transfer(database)
    assert client.patch(f"/api/cash-flows/{found['same-way']}", json={"is_internal_transfer": True}).status_code == 200
    item = next(i for i in client.get("/api/cash-flows").json()["items"] if i["id"] == found["same-way"])
    assert item["kind"] == "transfer" and item["needs_review"] is False and item["to"] == "Own wallet"


def test_patch_reset_returns_both_legs_to_automatic(env):
    client, database = env
    found = seed_transfer(database)
    client.patch(f"/api/cash-flows/{found['w']}", json={"is_internal_transfer": True, "matched_flow_id": found["m"]})
    assert client.patch(f"/api/cash-flows/{found['m']}", json={"reset": True}).status_code == 200
    for flow_id in (found["w"], found["m"]):
        stored = row(database, flow_id)
        assert stored["classified_by"] == "auto" and stored["matched_flow_id"] is None
        assert stored["is_internal_transfer"] == 0 and stored["match_method"] == ""
    item = next(i for i in client.get("/api/cash-flows").json()["items"] if i["id"] == found["w"])
    assert item["needs_review"] is True


def test_transfer_item_names_the_other_leg(env):
    client, database = env
    found = seed_transfer(database)
    pair(database, found["w"], found["m"], confidence=0.7)
    item = next(i for i in client.get("/api/cash-flows").json()["items"] if i["kind"] == "transfer")
    assert {item["id"], item["partner_id"]} == {found["w"], found["m"]}


# -- PWA markup ------------------------------------------------------------------------
def test_index_wires_the_three_tabs_allocation_and_manual_form(env):
    client, _ = env
    html = client.get("/").text
    assert 'grid-cols-3' in html and 'grid-cols-2" role="tablist"' not in html
    for tab in ("portfolio", "analytics", "activity"):
        assert f'id="tab-{tab}"' in html and f'id="panel-{tab}"' in html
    assert 'role="tabpanel" aria-labelledby="tab-activity"' in html
    assert 'id="alloc-title"' in html and 'role="switch" :aria-checked="allocEnabled"' in html
    assert 'id="manual-title"' in html and 'v-model="manualDraft.label"' in html
    js = client.get("/static/app.js").text
    assert "const SECTIONS = ['portfolio', 'analytics', 'activity'];" in js
    assert "/api/cash-flows" in js
