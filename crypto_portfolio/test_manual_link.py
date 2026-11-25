"""Locations classified by source (fix) and manual assets linked to a configured wallet (feature).

    .venv/bin/pytest test_manual_link.py -q

Part A: the dashboard used to guess where a location came from by its spelling (`chain/label`
means on-chain, anything else Binance), so the cold-wallet label "My Ledgy" showed under the
Binance tab. The backend now sends `locations_by_source` and the frontend filters on it.

Part B: `manual_assets[].linked_wallet` names a configured wallet that is the same device. The
Wallets view shows both in one card; data and analytics still keep `source = 'manual'` apart.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as config_module
from config import ManualAsset, ManualHolding, load_config, parse_manual_assets, resolve_linked_wallet
from models import BINANCE_EARN, BINANCE_SPOT, MANUAL, ONCHAIN, Holding, holding_sources
from services.db import Database
from services.manual import fetch_manual
from services.portfolio import aggregate, personal_wallets, to_dict
from test_api import EVM_A, EVM_B, SOL_A, fake_fetcher, make_client

ROOT = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: False)
    for name in (*config_module.ENV_OVERRIDES, "PORTFOLIO_WEB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


# The situation reported by Jean: Earn BTC on Binance, BTC declared on the Ledger, and an EVM
# wallet with the very same label.
def jean_holdings() -> list[Holding]:
    return [
        Holding("BTC", BINANCE_EARN, 0.1325, 60000.0, detail="flexible"),
        Holding("BTC", MANUAL, 0.0254, 60000.0, detail="My Ledgy"),
        Holding("ETH", ONCHAIN, 1.5, 3000.0, detail="ethereum/My Ledgy"),
        Holding("USDC", ONCHAIN, 40.0, 1.0, detail="base/My Ledgy"),
        Holding("ETH", ONCHAIN, 0.2, 3000.0, detail="ethereum/New Trust"),
        Holding("SHIB", ONCHAIN, 10.0, 0.00001, detail="ethereum/My Ledgy"),   # dust
        Holding("USDT", BINANCE_SPOT, 100.0, 1.0),
    ]


WALLETS = {
    "evm": [
        {"address": EVM_A, "label": "My Ledgy", "chains": ["ethereum", "base"]},
        {"address": EVM_B, "label": "New Trust", "chains": ["ethereum"]},
    ],
    "solana": [{"address": SOL_A, "label": "My Sol"}],
}
LEDGER_BTC = {"label": "My Ledgy", "kind": "cold_wallet", "linked_wallet": "My Ledgy",
              "holdings": [{"symbol": "BTC", "quantity": 0.0254}]}


# == Part A: locations by source =================================================================
def test_aggregate_keeps_locations_per_source():
    portfolio = aggregate(jean_holdings())
    btc = next(row for row in portfolio.rows if row.symbol == "BTC")
    assert btc.details_by_source == {BINANCE_EARN: ["flexible"], MANUAL: ["My Ledgy"]}
    eth = next(row for row in portfolio.rows if row.symbol == "ETH")
    assert eth.details_by_source == {ONCHAIN: ["ethereum/My Ledgy", "ethereum/New Trust"]}


def test_to_dict_exposes_locations_by_source_and_keeps_flat_locations():
    btc = next(a for a in to_dict(aggregate(jean_holdings()))["assets"] if a["symbol"] == "BTC")
    assert btc["locations_by_source"] == {BINANCE_EARN: ["flexible"], MANUAL: ["My Ledgy"]}
    assert btc["locations"] == ["flexible", "My Ledgy"]   # `show --json` schema unchanged


def test_same_label_on_two_sources_is_listed_under_each():
    holdings = [Holding("BTC", MANUAL, 1.0, 10.0, detail="X"), Holding("BTC", BINANCE_SPOT, 1.0, 10.0, detail="X")]
    row = aggregate(holdings).rows[0]
    assert row.details == ["X"]
    assert row.details_by_source == {MANUAL: ["X"], BINANCE_SPOT: ["X"]}


def test_stored_snapshot_keeps_locations_by_source(tmp_path):
    from services.portfolio import portfolio_from_db
    database = Database(tmp_path / "h.db")
    database.record(aggregate(jean_holdings()))
    btc = next(a for a in to_dict(portfolio_from_db(database))["assets"] if a["symbol"] == "BTC")
    assert btc["locations_by_source"][MANUAL] == ["My Ledgy"]
    assert "My Ledgy" not in btc["locations_by_source"][BINANCE_EARN]


def test_api_portfolio_never_lists_the_manual_label_under_binance(tmp_path):
    client = make_client(tmp_path, sections={"wallets": WALLETS, "manual_assets": [LEDGER_BTC]},
                         fetcher=fake_fetcher(jean_holdings()))
    data = client.post("/api/portfolio/refresh").json()
    btc = next(a for a in data["assets"] if a["symbol"] == "BTC")
    binance = [name for source in ("binance_spot", "binance_earn", "binance_funding")
               for name in btc["locations_by_source"].get(source, [])]
    assert "My Ledgy" not in binance
    assert btc["locations_by_source"]["manual"] == ["My Ledgy"]
    assert btc["by_source"] == {"binance_earn": 0.1325, "manual": 0.0254}


def test_frontend_drops_the_slash_heuristic():
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "l.includes('/')" not in js
    assert "locations_by_source" in js
    assert "TAB_SOURCES" in js


def _assets_locations_fn() -> str:
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function assetLocations\(asset, sources\) \{.*?\n      \}\n", js, re.S)
    assert match, "assetLocations() not found in app.js"
    return match.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("sources, expected", [
    (["binance_spot", "binance_earn", "binance_funding"], ["flexible"]),
    (["onchain"], []),
    (["binance_spot", "binance_earn", "binance_funding", "onchain", "manual"], ["flexible", "My Ledgy (manual)"]),
])
def test_frontend_asset_locations_follow_the_source(sources, expected):
    asset = {"locations": ["flexible", "My Ledgy"],
             "locations_by_source": {"binance_earn": ["flexible"], "manual": ["My Ledgy"]}}
    script = (_assets_locations_fn()
              + f"process.stdout.write(JSON.stringify(assetLocations({json.dumps(asset)}, {json.dumps(sources)})));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == expected


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_frontend_asset_locations_without_split_does_not_guess():
    script = _assets_locations_fn() + "process.stdout.write(JSON.stringify(assetLocations({locations: ['My Ledgy']}, ['binance_spot'])));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == []


# == Part B: schema and validation ===============================================================
def test_parse_linked_wallet_matches_case_insensitively_and_keeps_wallet_spelling():
    raw = [{"label": "Cold", "kind": "cold_wallet", "linked_wallet": " my ledgy ", "holdings": []}]
    assets, errors = parse_manual_assets(raw, ["My Ledgy", "New Trust"])
    assert errors == []
    assert assets[0].linked_wallet == "My Ledgy"


def test_parse_unknown_linked_wallet_is_reported_and_asset_kept_unlinked():
    raw = [{"label": "Cold", "linked_wallet": "My Ledgyy", "holdings": [{"symbol": "BTC", "quantity": 1}]}]
    assets, errors = parse_manual_assets(raw, ["My Ledgy"])
    assert len(assets) == 1 and assets[0].linked_wallet == ""
    assert assets[0].holdings[0].quantity == 1   # still valued: dropping it would fake a withdrawal
    assert len(errors) == 1
    assert "linked_wallet 'My Ledgyy' is not a configured wallet" in errors[0] and "'My Ledgy'" in errors[0]


def test_parse_linked_wallet_with_no_wallet_configured():
    _, errors = parse_manual_assets([{"label": "Cold", "linked_wallet": "Ghost"}], [])
    assert "none configured" in errors[0]


def test_parse_linked_wallet_must_be_text():
    assets, errors = parse_manual_assets([{"label": "Cold", "linked_wallet": ["My Ledgy"]}], ["My Ledgy"])
    assert assets[0].linked_wallet == "" and "linked_wallet must be a wallet label" in errors[0]


def test_parse_without_wallet_labels_keeps_the_link_unchecked():
    assets, errors = parse_manual_assets([{"label": "Cold", "linked_wallet": "Anything"}])
    assert errors == [] and assets[0].linked_wallet == "Anything"


def test_parse_empty_linked_wallet_is_standalone():
    assets, errors = parse_manual_assets([{"label": "Cold", "linked_wallet": "  "}], ["My Ledgy"])
    assert errors == [] and assets[0].linked_wallet == ""


def test_to_raw_round_trips_linked_wallet():
    asset = ManualAsset("Cold", "cold_wallet", (ManualHolding("BTC", 0.5),), linked_wallet="My Ledgy")
    raw = asset.to_raw()
    assert raw["linked_wallet"] == "My Ledgy"
    assert list(raw)[:3] == ["label", "kind", "linked_wallet"]
    assert parse_manual_assets([raw], ["My Ledgy"])[0][0] == asset
    assert "linked_wallet" not in ManualAsset("Cold", "other", ()).to_raw()


def test_resolve_linked_wallet():
    assert resolve_linked_wallet("NEW TRUST", ["My Ledgy", "New Trust"]) == "New Trust"
    assert resolve_linked_wallet("nope", ["My Ledgy"]) is None


def test_config_validates_links_against_evm_and_solana_labels(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"wallets": WALLETS, "manual_assets": [
        LEDGER_BTC,
        {"label": "Phantom stake", "kind": "staking", "linked_wallet": "my sol", "holdings": []},
        {"label": "Typo", "linked_wallet": "Ledgy", "holdings": []},
    ]}), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.wallet_labels == ["My Ledgy", "New Trust", "My Sol"]
    links = {asset.label: asset.linked_wallet for asset in cfg.manual}
    assert links == {"My Ledgy": "My Ledgy", "Phantom stake": "My Sol", "Typo": ""}
    assert len(cfg.manual_errors) == 1 and "'Ledgy'" in cfg.manual_errors[0]


def test_wallet_labels_list_a_shared_label_once(tmp_path):
    path = tmp_path / "config.yaml"
    wallets = {"evm": [{"address": EVM_A, "label": "Ledger"}], "solana": [{"address": SOL_A, "label": "Ledger"}]}
    path.write_text(yaml.safe_dump({"wallets": wallets}), encoding="utf-8")
    assert load_config(path).wallet_labels == ["Ledger"]


def test_link_does_not_change_data_keys(tmp_path):
    """snapshot_sources, holdings detail and cash-flow accounts still key on the manual label."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"wallets": WALLETS, "manual_assets": [
        {**LEDGER_BTC, "label": "Ledger BTC", "holdings": [{"symbol": "BTC", "quantity": 0.0254, "price_usd": 1.0}]},
    ]}), encoding="utf-8")
    cfg = load_config(path)

    class Ticker:
        def price_usd(self, symbol, stablecoins):
            return 0.0

    class Llama:
        def price(self, holdings):
            return None

    holdings, errors, expected = fetch_manual(cfg, binance_client=Ticker(), onchain_client=Llama())
    assert errors == []
    assert expected == ["manual:Ledger BTC"]
    assert [(h.source, h.detail) for h in holdings] == [(MANUAL, "Ledger BTC")]
    assert holding_sources(holdings) == {"manual:Ledger BTC"}


# == Part B: display grouping ====================================================================
def _assets(*entries) -> list[ManualAsset]:
    return parse_manual_assets(list(entries), ["My Ledgy", "New Trust", "My Sol"])[0]


def test_personal_wallets_merge_a_linked_manual_asset_into_its_wallet():
    wallets = personal_wallets(jean_holdings(), ["My Ledgy", "New Trust", "My Sol"], _assets(LEDGER_BTC), min_value=1.0)
    ledger = next(w for w in wallets if w["label"] == "My Ledgy")
    assert ledger["type"] == "wallet" and ledger["id"] == "wallet:My Ledgy" and ledger["configured"]
    assert ledger["linked_manual"] == ["My Ledgy"]
    assert list(ledger["chains"]) == ["ethereum", "base"]
    assert {(a["symbol"], a["chain"]) for a in ledger["assets"]} == {("ETH", "ethereum"), ("USDC", "base")}
    btc = ledger["manual"][0]
    assert btc["symbol"] == "BTC" and btc["label"] == "My Ledgy" and btc["kind"] == "cold_wallet"
    assert btc["quantity"] == pytest.approx(0.0254)
    # totals stay split by source inside the wallet
    assert ledger["onchain_value_usd"] == pytest.approx(1.5 * 3000 + 40 + 10 * 0.00001, abs=0.01)
    assert ledger["manual_value_usd"] == pytest.approx(0.0254 * 60000, abs=0.01)
    assert ledger["value_usd"] == pytest.approx(ledger["onchain_value_usd"] + ledger["manual_value_usd"], abs=0.01)
    assert ledger["dust"] == {"assets": 1, "value_usd": 0.0}
    # no standalone row for the linked asset, no Binance row anywhere
    assert [w["id"] for w in wallets] == ["wallet:My Ledgy", "wallet:New Trust"]


def test_personal_wallets_same_name_without_link_stays_separate():
    unlinked = {**LEDGER_BTC, "linked_wallet": None}
    wallets = personal_wallets(jean_holdings(), ["My Ledgy"], _assets(unlinked))
    ids = [w["id"] for w in wallets]
    assert "wallet:My Ledgy" in ids and "manual:My Ledgy" in ids
    manual = next(w for w in wallets if w["id"] == "manual:My Ledgy")
    assert manual["type"] == "manual" and manual["manual_kind"] == "cold_wallet" and not manual["configured"]
    assert [m["symbol"] for m in manual["manual"]] == ["BTC"] and manual["assets"] == []
    wallet = next(w for w in wallets if w["id"] == "wallet:My Ledgy")
    assert wallet["manual"] == [] and wallet["linked_manual"] == []


def test_personal_wallets_link_without_onchain_holdings_still_lists_the_wallet():
    holdings = [Holding("BTC", MANUAL, 1.0, 100.0, detail="Cold")]
    asset = _assets({"label": "Cold", "linked_wallet": "My Sol", "holdings": [{"symbol": "BTC", "quantity": 1}]})
    wallets = personal_wallets(holdings, ["My Sol"], asset)
    assert [(w["id"], w["linked_manual"]) for w in wallets] == [("wallet:My Sol", ["Cold"])]
    assert wallets[0]["chains"] == {} and wallets[0]["assets"] == [] and wallets[0]["pct"] == 100.0


def test_personal_wallets_two_manual_assets_on_one_wallet_and_sorting():
    holdings = jean_holdings() + [Holding("DOT", MANUAL, 10.0, 5.0, detail="Ledger stake")]
    assets = _assets(LEDGER_BTC, {"label": "Ledger stake", "kind": "staking", "linked_wallet": "My Ledgy",
                                  "holdings": [{"symbol": "DOT", "quantity": 10}]})
    ledger = personal_wallets(holdings, ["My Ledgy"], assets)[0]
    assert ledger["linked_manual"] == ["My Ledgy", "Ledger stake"]
    for rows in (ledger["assets"], ledger["manual"]):
        values = [r["value_usd"] for r in rows]
        assert values == sorted(values, reverse=True)


def test_personal_wallets_ignore_old_rows_without_wallet_detail():
    wallets = personal_wallets([Holding("ETH", ONCHAIN, 1.0, 10.0, detail="")], [], [])
    assert wallets[0]["label"] == "On-chain" and wallets[0]["chains"] == {}


# == Part B: API =================================================================================
def test_api_portfolio_sends_personal_wallets_and_total(tmp_path):
    client = make_client(tmp_path, sections={"wallets": WALLETS, "manual_assets": [LEDGER_BTC]},
                         fetcher=fake_fetcher(jean_holdings()))
    data = client.post("/api/portfolio/refresh").json()
    ledger = next(w for w in data["wallets"]["personal"] if w["label"] == "My Ledgy")
    assert [m["symbol"] for m in ledger["manual"]] == ["BTC"] and ledger["assets"]
    assert data["groups"]["personal"] == pytest.approx(data["sources"]["onchain"] + data["sources"]["manual"], abs=0.02)
    # data layer untouched: per-source totals still split
    assert data["sources"]["manual"] == pytest.approx(0.0254 * 60000, abs=0.01)


def test_api_portfolio_reports_a_dangling_link_in_config_errors(tmp_path):
    bad = {**LEDGER_BTC, "linked_wallet": "Gone"}
    client = make_client(tmp_path, sections={"wallets": WALLETS, "manual_assets": [bad]})
    data = client.get("/api/portfolio").json()
    assert any("linked_wallet 'Gone'" in error for error in data["config_errors"])


def test_settings_lists_linked_wallet(tmp_path):
    client = make_client(tmp_path, sections={"wallets": WALLETS, "manual_assets": [LEDGER_BTC]})
    data = client.get("/api/settings").json()
    assert data["manual_assets"][0]["linked_wallet"] == "My Ledgy"
    assert {w["name"] for w in data["wallets"]} == {"My Ledgy", "New Trust", "My Sol"}


def test_save_settings_stores_the_link_with_the_wallet_spelling(tmp_path):
    client = make_client(tmp_path, sections={"wallets": WALLETS})
    body = {"manual_assets": [{"label": "Ledger BTC", "kind": "cold_wallet", "linked_wallet": "my ledgy",
                               "holdings": [{"symbol": "BTC", "quantity": 0.0254}]}]}
    response = client.post("/api/settings", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["manual_assets"][0]["linked_wallet"] == "My Ledgy"
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert saved["manual_assets"][0]["linked_wallet"] == "My Ledgy"


def test_save_settings_rejects_an_unknown_linked_wallet_without_writing(tmp_path):
    client = make_client(tmp_path, sections={"wallets": WALLETS})
    before = (tmp_path / "config.yaml").read_text()
    body = {"manual_assets": [{"label": "Cold", "linked_wallet": "Ledgy", "holdings": []}]}
    response = client.post("/api/settings", json=body)
    assert response.status_code == 422
    assert "'Ledgy'" in response.json()["detail"] and "not configured" in response.json()["detail"]
    assert (tmp_path / "config.yaml").read_text() == before


def test_save_settings_validates_the_link_against_the_wallets_of_the_same_request(tmp_path):
    client = make_client(tmp_path, sections={"wallets": WALLETS, "manual_assets": [LEDGER_BTC]})
    body = {
        "wallets": [{"name": "Ledger", "address": EVM_A, "chains": ["ethereum"]}],
        "manual_assets": [{**LEDGER_BTC, "linked_wallet": "Ledger"}],
    }
    response = client.post("/api/settings", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["manual_assets"][0]["linked_wallet"] == "Ledger"


def test_save_settings_refuses_to_orphan_a_link_when_only_wallets_change(tmp_path):
    client = make_client(tmp_path, sections={"wallets": WALLETS, "manual_assets": [LEDGER_BTC]})
    before = (tmp_path / "config.yaml").read_text()
    body = {"wallets": [{"name": "New Trust", "address": EVM_B, "chains": ["ethereum"]}]}
    response = client.post("/api/settings", json=body)
    assert response.status_code == 422
    assert "My Ledgy" in response.json()["detail"]
    assert (tmp_path / "config.yaml").read_text() == before


def test_save_settings_standalone_asset_has_no_link(tmp_path):
    client = make_client(tmp_path, sections={"wallets": WALLETS})
    body = {"manual_assets": [{"label": "Kraken", "kind": "other", "linked_wallet": "", "holdings": []}]}
    assert client.post("/api/settings", json=body).status_code == 200
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert "linked_wallet" not in saved["manual_assets"][0]


def test_index_has_kind_and_linked_wallet_dropdowns_and_personal_tab(tmp_path):
    client = make_client(tmp_path)
    html = client.get("/").text
    assert '<select id="manual-kind" v-model="manualDraft.kind">' in html
    assert '<select id="manual-link" v-model="manualDraft.linked_wallet"' in html
    assert "— None / standalone name —" in html
    assert 'v-for="w in walletOptions"' in html
    # one Personal tab with its Coins | Wallets switch; the Others cards are gone
    assert 'v-for="w in visibleWallets"' in html
    assert "walletGroups" not in html and "tabId === 'others'" not in html
    assert '<dust-note :dust="w.dust"' in html                 # every wallet detail folds its dust
    assert '<dust-note v-if="!loading && !walletView" :dust="tabDust"' in html   # same line as Coins
    js = client.get("/static/app.js").text
    assert "{ id: 'personal', label: 'Personal'" in js
    assert "id: 'onchain', label: 'On-chain', hint" not in js and "id: 'others'" not in js
    assert "VIEW_SWITCH_TABS = ['binance', 'personal']" in js
    assert "personal: ['onchain', 'manual']" in js
    assert "RETIRED_TABS = { onchain: 'personal', others: 'personal', wallets: 'personal' }" in js
    assert "app.component('dust-note'" in js
    assert "{ id: 'wallets', label: 'Wallets'" in js
    assert "linked_wallet: a.linked_wallet || null" in js   # sent back on save
