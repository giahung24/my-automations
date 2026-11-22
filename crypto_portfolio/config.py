"""Config loading: ``config.yaml`` merged over built-in defaults, then env overrides.

Secrets never have to live in the YAML file - ``BINANCE_API_KEY`` / ``BINANCE_API_SECRET``
in the environment (or a ``.env`` next to this file) always win over it.
"""
from __future__ import annotations

import os
import tempfile
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

try:  # python-dotenv is in requirements.txt, but the tool still runs without it
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised only on a partial install
    def load_dotenv(*_a, **_k):
        return False

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_PATH = ROOT / "config.example.yaml"

# Public RPCs, first one that answers wins; each list was probed live, healthiest first.
# Public endpoints come and go (Ankr now wants a key, llamarpc stopped resolving), which is
# why every chain has spares - and why `chains.<name>.rpcs` in config.yaml can replace them
# outright with your own Alchemy/Infura URL. `native_price_id` and `llama_chain` are
# DefiLlama keys (https://coins.llama.fi) - the free pricing API, no key needed.
DEFAULT_CHAINS = {
    "ethereum": {
        "rpcs": ["https://ethereum-rpc.publicnode.com", "https://rpc.mevblocker.io",
                 "https://rpc.flashbots.net", "https://cloudflare-eth.com"],
        "native_symbol": "ETH",
        "native_price_id": "coingecko:ethereum",
        "llama_chain": "ethereum",
        "explorer": "https://etherscan.io",
        "tokens": [
            {"symbol": "LINK", "address": "0x514910771af9ca656af840dff83e8264ecf986ca", "decimals": 18},
            {"symbol": "USDC", "address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "decimals": 6},
            {"symbol": "USDT", "address": "0xdac17f958d2ee523a2206206994597c13d831ec7", "decimals": 6},
            {"symbol": "WBTC", "address": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599", "decimals": 8},
            {"symbol": "DAI", "address": "0x6b175474e89094c44da98b954eedeac495271d0f", "decimals": 18},
            {"symbol": "UNI", "address": "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984", "decimals": 18},
            {"symbol": "AAVE", "address": "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9", "decimals": 18},
        ],
    },
    "bsc": {
        "rpcs": ["https://bsc-dataseed.binance.org", "https://bsc-rpc.publicnode.com",
                 "https://bsc-dataseed1.defibit.io"],
        "native_symbol": "BNB",
        "native_price_id": "coingecko:binancecoin",
        "llama_chain": "bsc",
        "explorer": "https://bscscan.com",
        "tokens": [
            {"symbol": "USDT", "address": "0x55d398326f99059ff775485246999027b3197955", "decimals": 18},
            {"symbol": "USDC", "address": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", "decimals": 18},
            {"symbol": "BTCB", "address": "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c", "decimals": 18},
            {"symbol": "ETH", "address": "0x2170ed0880ac9a755fd29b2688956bd959f933f8", "decimals": 18},
            {"symbol": "LINK", "address": "0xf8a0bf9cf54bb92f17374d9e9a321e6a111a51bd", "decimals": 18},
        ],
    },
    "arbitrum": {
        "rpcs": ["https://arb1.arbitrum.io/rpc", "https://arbitrum-one-rpc.publicnode.com",
                 "https://arbitrum.drpc.org"],
        "native_symbol": "ETH",
        "native_price_id": "coingecko:ethereum",
        "llama_chain": "arbitrum",
        "explorer": "https://arbiscan.io",
        "tokens": [
            {"symbol": "USDC", "address": "0xaf88d065e77c8cc2239327c5edb3a432268e5831", "decimals": 6},
            {"symbol": "USDT", "address": "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", "decimals": 6},
            {"symbol": "ARB", "address": "0x912ce59144191c1204e64559fe8253a0e49e6548", "decimals": 18},
            {"symbol": "LINK", "address": "0xf97f4df75117a78c1a5a0dbb814af92458539fb4", "decimals": 18},
        ],
    },
    "base": {
        "rpcs": ["https://mainnet.base.org", "https://base-rpc.publicnode.com",
                 "https://base.drpc.org"],
        "native_symbol": "ETH",
        "native_price_id": "coingecko:ethereum",
        "llama_chain": "base",
        "explorer": "https://basescan.org",
        "tokens": [
            {"symbol": "USDC", "address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "decimals": 6},
            {"symbol": "AERO", "address": "0x940181a94a35a4569e4529a3cdfb74e38fd98631", "decimals": 18},
            {"symbol": "LINK", "address": "0x88fb150bdc53a65fe94dea0c9ba0a6daf8c6e196", "decimals": 18},
        ],
    },
    "polygon": {
        # MATIC was renamed POL in Sept 2024; the CoinGecko id is still `matic-network`.
        "rpcs": ["https://1rpc.io/matic", "https://polygon-rpc.com",
                 "https://polygon-bor-rpc.publicnode.com"],
        "native_symbol": "POL",
        "native_price_id": "coingecko:matic-network",
        "llama_chain": "polygon",
        "explorer": "https://polygonscan.com",
        "tokens": [
            {"symbol": "USDC", "address": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359", "decimals": 6},
            {"symbol": "USDT", "address": "0xc2132d05d31c914a87c6611c10748aeb04b58e8f", "decimals": 6},
            {"symbol": "LINK", "address": "0x53e0bca35ec356bd5dddfebbd1fc0fd03fabad39", "decimals": 18},
            {"symbol": "WETH", "address": "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619", "decimals": 18},
        ],
    },
    "solana": {
        "rpcs": ["https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com"],
        "native_symbol": "SOL",
        "native_price_id": "coingecko:solana",
        "llama_chain": "solana",
        "explorer": "https://solscan.io",
    },
}

DEFAULTS = {
    "binance": {"enabled": True, "api_key": "", "api_secret": "", "recv_window": 5000},
    "wallets": {"evm": [], "solana": []},
    "chains": deepcopy(DEFAULT_CHAINS),
    "settings": {
        "min_value": 1.0,          # dust filter, USD
        "db_path": "portfolio_history.db",
        "charts_dir": "./charts",
        "timeout": 20,             # seconds per HTTP request
        "max_workers": 8,          # parallel chain / wallet fetches
        "stablecoins": ["USDT", "USDC", "FDUSD", "DAI", "TUSD", "USDP", "PYUSD", "USDE"],
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursive dict merge; lists and scalars from `override` replace wholesale."""
    out = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _split(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass
class Config:
    binance: dict = field(default_factory=dict)
    wallets: dict = field(default_factory=dict)
    chains: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    path: Path | None = None

    # -- convenience accessors -------------------------------------------------
    @property
    def binance_enabled(self) -> bool:
        return bool(self.binance.get("enabled")) and bool(self.binance.get("api_key")) and bool(
            self.binance.get("api_secret")
        )

    @property
    def min_value(self) -> float:
        return float(self.settings.get("min_value", 1.0))

    @property
    def timeout(self) -> int:
        return int(self.settings.get("timeout", 20))

    @property
    def max_workers(self) -> int:
        return max(1, int(self.settings.get("max_workers", 8)))

    @property
    def stablecoins(self) -> set[str]:
        return {s.upper() for s in self.settings.get("stablecoins", [])}

    def resolve(self, value: str) -> Path:
        """Resolve a configured path relative to the project root, not the shell cwd."""
        path = Path(value).expanduser()
        return path if path.is_absolute() else (ROOT / path)

    @property
    def db_path(self) -> Path:
        return self.resolve(self.settings.get("db_path", "portfolio_history.db"))

    @property
    def charts_dir(self) -> Path:
        return self.resolve(self.settings.get("charts_dir", "./charts"))

    def chain(self, name: str) -> dict:
        return self.chains.get(name, {})

    @property
    def evm_wallets(self) -> list[dict]:
        return [w for w in self.wallets.get("evm", []) if w.get("address")]

    @property
    def solana_wallets(self) -> list[dict]:
        return [w for w in self.wallets.get("solana", []) if w.get("address")]

    @property
    def has_wallets(self) -> bool:
        return bool(self.evm_wallets or self.solana_wallets)


def _as_address(value) -> str:
    """Normalise one address.

    YAML 1.1 parses an *unquoted* `0xdac17f95...` as an integer - every EVM address is a
    valid hex literal - so recover the canonical 40-hex-digit form instead of silently
    querying a nonsense address. Quoted addresses pass through untouched.
    """
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        return f"0x{value:040x}"
    return str(value).strip()


def _normalize_wallets(raw) -> list[dict]:
    """Accept either `- "0xabc"` or `- {address: 0xabc, label: main, chains: [...]}`."""
    wallets = []
    for entry in raw or []:
        if isinstance(entry, (str, int)):
            entry = {"address": entry}
        if not isinstance(entry, dict):
            continue
        address = _as_address(entry.get("address"))
        if not address:
            continue
        wallets.append(
            {
                "address": address,
                "label": str(entry.get("label") or address[:6] + ".." + address[-4:]),
                "chains": list(entry.get("chains") or []),
                "tokens": entry.get("tokens") or {},
            }
        )
    return wallets


ENV_OVERRIDES = ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_ENABLED", "EVM_WALLETS",
                 "SOLANA_WALLETS", "PORTFOLIO_DB", "PORTFOLIO_CHARTS_DIR", "PORTFOLIO_MIN_VALUE")


def active_env_overrides() -> list[str]:
    """Names of the environment variables currently overriding config.yaml."""
    return [name for name in ENV_OVERRIDES if os.environ.get(name)]


def read_raw_config(path: str | Path | None = None) -> dict:
    """The YAML file exactly as written - no defaults, no env - for read-modify-write."""
    config_path = Path(path).expanduser() if path else CONFIG_PATH
    if not config_path.is_file():
        return {}
    with open(config_path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path} must contain a YAML mapping, got {type(raw).__name__}")
    return raw


def write_raw_config(raw: dict, path: str | Path | None = None) -> Path:
    """Atomically replace config.yaml with `raw`, keeping the previous file as .bak.

    The file holds an API secret, so it is created owner-read/write only (0600). YAML
    comments do not survive a rewrite - the backup keeps the hand-edited original.
    """
    config_path = Path(path).expanduser() if path else CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, default_flow_style=False)
    if config_path.is_file():
        backup = config_path.with_name(config_path.name + ".bak")
        backup.write_bytes(config_path.read_bytes())
        os.chmod(backup, 0o600)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".yaml", dir=config_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, config_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return config_path


def load_config(path: str | Path | None = None, missing_ok: bool = False) -> Config:
    """Load defaults -> config.yaml -> environment. Missing config.yaml is not an error.

    An explicitly requested file that is missing *is* an error, unless `missing_ok` - the
    web settings page points at a file it is about to create on the first save.
    """
    load_dotenv(ROOT / ".env")
    load_dotenv()  # also honour a .env in the shell's cwd

    config_path = Path(path).expanduser() if path else CONFIG_PATH
    raw = {}
    if config_path.is_file():
        with open(config_path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{config_path} must contain a YAML mapping, got {type(raw).__name__}")
    elif path and not missing_ok:
        raise FileNotFoundError(f"config file not found: {config_path}")

    merged = _deep_merge(DEFAULTS, raw)

    # Environment always wins, so secrets can stay out of the YAML entirely.
    env = os.environ
    if env.get("BINANCE_API_KEY"):
        merged["binance"]["api_key"] = env["BINANCE_API_KEY"].strip()
    if env.get("BINANCE_API_SECRET"):
        merged["binance"]["api_secret"] = env["BINANCE_API_SECRET"].strip()
    if env.get("BINANCE_ENABLED"):
        merged["binance"]["enabled"] = _as_bool(env["BINANCE_ENABLED"])
    if env.get("EVM_WALLETS"):
        merged["wallets"]["evm"] = _split(env["EVM_WALLETS"])
    if env.get("SOLANA_WALLETS"):
        merged["wallets"]["solana"] = _split(env["SOLANA_WALLETS"])
    if env.get("PORTFOLIO_DB"):
        merged["settings"]["db_path"] = env["PORTFOLIO_DB"]
    if env.get("PORTFOLIO_CHARTS_DIR"):
        merged["settings"]["charts_dir"] = env["PORTFOLIO_CHARTS_DIR"]
    if env.get("PORTFOLIO_MIN_VALUE"):
        try:
            merged["settings"]["min_value"] = float(env["PORTFOLIO_MIN_VALUE"])
        except ValueError:
            pass  # a typo in an env var should not break the run

    merged["wallets"]["evm"] = _normalize_wallets(merged["wallets"].get("evm"))
    merged["wallets"]["solana"] = _normalize_wallets(merged["wallets"].get("solana"))
    for chain in merged["chains"].values():
        for token in (chain.get("tokens") or []):
            if isinstance(token, dict):
                token["address"] = _as_address(token.get("address"))

    return Config(
        binance=merged["binance"],
        wallets=merged["wallets"],
        chains=merged["chains"],
        settings=merged["settings"],
        path=config_path if config_path.is_file() else None,
    )
