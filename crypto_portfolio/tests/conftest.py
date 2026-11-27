"""Suite-wide test isolation from the developer's real environment.

``config.load_config()`` calls ``load_dotenv()`` on every load, so a real ``.env`` (holding
``PORTFOLIO_WEB_TOKEN``, Binance keys, wallet overrides...) would otherwise leak into every
test - e.g. an endpoint suddenly answering 401 because a real access token is set.
"""
from __future__ import annotations

import os

import pytest

import config as config_module

# Everything load_config() / web_api read from the environment, plus the token gate.
SECRET_ENV_VARS = (*config_module.ENV_OVERRIDES, "PORTFOLIO_WEB_TOKEN")


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """No real .env, no env overrides, no access token unless a test sets one itself.

    Clearing the variables is not enough on its own: load_config() would re-read the real
    .env mid-test and put them straight back, so load_dotenv is neutralised too. Tests that
    exercise the authenticated path keep calling ``monkeypatch.setenv`` - that runs after
    this fixture, so it wins.
    """
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: False)
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name in [n for n in os.environ if n.startswith("GATEWAY_")]:
        monkeypatch.delenv(name, raising=False)
