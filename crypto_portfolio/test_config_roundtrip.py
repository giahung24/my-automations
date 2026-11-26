"""Every config.yaml write keeps what it does not edit - `manual_assets[].linked_wallet` first.

    .venv/bin/pytest test_config_roundtrip.py -q

Regression (2026-11-25 23:44): the web server had been started before `linked_wallet` existed.
The browser, served the new app.js from disk, saved the settings; the stale backend rebuilt every
`manual_assets` entry from a whitelist of the fields it knew and silently dropped
`linked_wallet: My Ledgy`. A save now (a) keeps the stored link when the request does not mention
it, and (b) carries over any key of an entry - or of a holding - that the form does not manage, so
an older server can no longer erase a field added by a newer version.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as config_module
from config import load_config, read_raw_config, write_raw_config
from test_api import EVM_A, EVM_B, SOL_A, make_client

WALLETS = {
    "evm": [
        {"address": EVM_A, "label": "My Ledgy", "chains": ["ethereum", "bsc"]},
        {"address": EVM_B, "label": "New Trust", "chains": ["ethereum"]},
    ],
    "solana": [{"address": SOL_A, "label": "My Sol"}],
}
LEDGER_BTC = {"label": "My Ledgy", "kind": "cold_wallet", "linked_wallet": "My Ledgy",
              "updated_at": "2026-11-25", "holdings": [{"symbol": "BTC", "quantity": 0.02540344}]}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: False)
    for name in (*config_module.ENV_OVERRIDES, "PORTFOLIO_WEB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def saved_manual(tmp_path) -> list[dict]:
    return yaml.safe_load((tmp_path / "config.yaml").read_text())["manual_assets"]


def form_payload(client) -> list[dict]:
    """What the settings form posts back for the manual assets it was given (app.js shape)."""
    return [{"label": a["label"], "kind": a["kind"], "linked_wallet": a["linked_wallet"],
             "holdings": [{k: h[k] for k in ("symbol", "quantity", "price_id", "price_usd", "liquid")
                           if k in h} for h in a["holdings"]]}
            for a in client.get("/api/settings").json()["manual_assets"]]


# -- the writer itself --------------------------------------------------------------------------
def test_write_raw_config_round_trips_linked_wallet(tmp_path):
    path = tmp_path / "config.yaml"
    write_raw_config({"wallets": WALLETS, "manual_assets": [LEDGER_BTC]}, path)
    write_raw_config(read_raw_config(path), path)          # load -> save -> load
    assert read_raw_config(path)["manual_assets"] == [LEDGER_BTC]
    assert load_config(path).manual[0].linked_wallet == "My Ledgy"


# -- POST /api/settings -------------------------------------------------------------------------
def test_settings_form_round_trip_keeps_the_manual_block_byte_for_byte(tmp_path):
    client = make_client(tmp_path, sections={"wallets": WALLETS, "manual_assets": [LEDGER_BTC]})
    response = client.post("/api/settings", json={"manual_assets": form_payload(client)})
    assert response.status_code == 200, response.text
    assert saved_manual(tmp_path) == [LEDGER_BTC]
    assert load_config(tmp_path / "config.yaml").manual[0].linked_wallet == "My Ledgy"


def test_save_without_linked_wallet_in_the_request_keeps_the_stored_link(tmp_path):
    """The exact failure: a client that does not send `linked_wallet` (older app.js, curl, script)."""
    client = make_client(tmp_path, sections={"wallets": WALLETS, "manual_assets": [LEDGER_BTC]})
    body = {"manual_assets": [{"label": "My Ledgy", "kind": "cold_wallet",
                               "holdings": [{"symbol": "BTC", "quantity": 0.03}]}]}
    response = client.post("/api/settings", json=body)
    assert response.status_code == 200, response.text
    assert saved_manual(tmp_path)[0]["linked_wallet"] == "My Ledgy"
    assert saved_manual(tmp_path)[0]["holdings"] == [{"symbol": "BTC", "quantity": 0.03}]
    assert response.json()["manual_assets"][0]["linked_wallet"] == "My Ledgy"


def test_kept_link_matches_the_stored_label_case_insensitively(tmp_path):
    client = make_client(tmp_path, sections={"wallets": WALLETS, "manual_assets": [LEDGER_BTC]})
    body = {"manual_assets": [{"label": "my ledgy", "holdings": [{"symbol": "BTC", "quantity": 0.02540344}]}]}
    assert client.post("/api/settings", json=body).status_code == 200
    assert saved_manual(tmp_path)[0]["linked_wallet"] == "My Ledgy"


@pytest.mark.parametrize("cleared", [None, ""])
def test_an_explicit_null_or_empty_link_still_unlinks(tmp_path, cleared):
    client = make_client(tmp_path, sections={"wallets": WALLETS, "manual_assets": [LEDGER_BTC]})
    body = {"manual_assets": [{**LEDGER_BTC, "linked_wallet": cleared}]}
    assert client.post("/api/settings", json=body).status_code == 200
    assert "linked_wallet" not in saved_manual(tmp_path)[0]


def test_a_kept_link_is_still_validated_against_the_wallets_of_the_request(tmp_path):
    client = make_client(tmp_path, sections={"wallets": WALLETS, "manual_assets": [LEDGER_BTC]})
    before = (tmp_path / "config.yaml").read_text()
    body = {"wallets": [{"name": "New Trust", "address": EVM_B, "chains": ["ethereum"]}],
            "manual_assets": [{"label": "My Ledgy", "holdings": [{"symbol": "BTC", "quantity": 0.0254}]}]}
    response = client.post("/api/settings", json=body)
    assert response.status_code == 422
    assert "My Ledgy" in response.json()["detail"]
    assert (tmp_path / "config.yaml").read_text() == before


def test_keys_the_form_does_not_manage_survive_a_save(tmp_path):
    """Forward compatibility: what an older server does not know, it must not erase."""
    entry = {**LEDGER_BTC, "note": "seed in the safe", "future_flag": True,
             "holdings": [{"symbol": "BTC", "quantity": 0.02540344, "acquired": "2021-03-01"}]}
    client = make_client(tmp_path, sections={"wallets": WALLETS, "manual_assets": [entry]})
    body = {"manual_assets": [{"label": "My Ledgy", "kind": "cold_wallet", "linked_wallet": "My Ledgy",
                               "holdings": [{"symbol": "BTC", "quantity": 0.03},
                                            {"symbol": "ETH", "quantity": 1.0}]}]}
    assert client.post("/api/settings", json=body).status_code == 200
    saved = saved_manual(tmp_path)[0]
    assert saved["note"] == "seed in the safe" and saved["future_flag"] is True
    assert saved["holdings"] == [{"symbol": "BTC", "quantity": 0.03, "acquired": "2021-03-01"},
                                 {"symbol": "ETH", "quantity": 1.0}]


def test_saving_only_binance_or_wallets_leaves_manual_assets_untouched(tmp_path):
    client = make_client(tmp_path, sections={"wallets": WALLETS, "manual_assets": [LEDGER_BTC]})
    assert client.post("/api/settings", json={"binance": {"enabled": False}}).status_code == 200
    assert saved_manual(tmp_path) == [LEDGER_BTC]
    wallets = [{"name": "My Ledgy", "address": EVM_A, "chains": ["ethereum"]},
               {"name": "My Sol", "address": SOL_A, "chains": []}]
    assert client.post("/api/settings", json={"wallets": wallets}).status_code == 200
    assert saved_manual(tmp_path) == [LEDGER_BTC]


def test_allocation_toggle_leaves_manual_assets_untouched(tmp_path):
    client = make_client(tmp_path, sections={"wallets": WALLETS, "manual_assets": [LEDGER_BTC]})
    assert client.post("/api/settings/allocation", json={"enabled": True}).status_code == 200
    assert saved_manual(tmp_path) == [LEDGER_BTC]


# -- scripts that load the config must not write it ----------------------------------------------
def test_backfill_script_never_writes_config(tmp_path):
    source = (Path(__file__).resolve().parent / "scripts" / "backfill_manual_btc.py").read_text()
    assert "write_raw_config" not in source and "safe_dump" not in source
