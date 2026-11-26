"""Multi-chain on-chain balance client - public addresses and public RPCs only.

EVM chains (Ethereum, BSC, Arbitrum, Base, Polygon) are read over plain JSON-RPC
(`eth_getBalance` for the native coin, `eth_call`/`balanceOf` for ERC-20s). Solana uses
`getBalance` plus `getTokenAccountsByOwner` for SPL tokens. Nothing is ever signed and no
private key is read, so the worst a leaked config can do is reveal which addresses you watch.

Pricing comes from DefiLlama's free endpoint (https://coins.llama.fi) - no API key, no quota.
"""
from __future__ import annotations

import itertools
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from models import ONCHAIN, Holding

LLAMA_PRICES = "https://coins.llama.fi/prices/current/"
LLAMA_BATCH = 60          # keys per pricing request, keeps the URL well under any limit
RPC_RETRIES = 2
RATE_LIMIT_PAUSE = 0.5    # seconds to wait before retrying a throttled endpoint

# ERC-20 function selectors (first 4 bytes of keccak256 of the signature).
SEL_BALANCE_OF = "0x70a08231"
SEL_BALANCE_OF_UNDERLYING = "0x3af9e669"  # balanceOfUnderlying(address)
SEL_DECIMALS = "0x313ce567"
SEL_SYMBOL = "0x95d89b41"

# Both SPL token programs; Token-2022 mints live under the second one.
SPL_PROGRAMS = [
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
]
EVM_CHAINS = ("ethereum", "bsc", "arbitrum", "base", "polygon")


class RpcError(RuntimeError):
    """An RPC endpoint answered with a JSON-RPC error object."""


class OnChainClient:
    def __init__(self, chains: dict, timeout: int = 20, max_workers: int = 8,
                 session: requests.Session | None = None):
        self.chains = chains
        self.timeout = timeout
        self.max_workers = max(1, max_workers)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "crypto-portfolio/1.0"})
        self.errors: list[str] = []
        self._token_meta: dict[tuple[str, str], tuple[str, int]] = {}

    # -- transport -------------------------------------------------------------
    def rpc(self, chain: str, method: str, params: list) -> object:
        """JSON-RPC call with failover across every configured endpoint for the chain."""
        endpoints = self.chains.get(chain, {}).get("rpcs") or []
        if not endpoints:
            raise RpcError(f"no RPC configured for chain '{chain}'")
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        last_error: Exception = RpcError(f"{chain}: no endpoint reachable")
        for url in endpoints:
            for _ in range(RPC_RETRIES):
                try:
                    response = self.session.post(url, json=payload, timeout=self.timeout)
                except requests.RequestException as exc:
                    last_error = exc
                    break  # endpoint down, try the next URL rather than hammering it
                if response.status_code in (429, 503):
                    # public endpoints throttle hard; an immediate retry just burns the quota
                    last_error = RpcError(f"{url}: HTTP {response.status_code} (rate limited)")
                    time.sleep(RATE_LIMIT_PAUSE)
                    continue
                if not response.ok:
                    last_error = RpcError(f"{url}: HTTP {response.status_code} {response.text[:120]}")
                    break
                try:
                    body = response.json()
                except ValueError as exc:
                    last_error = RpcError(f"{url}: non-JSON response ({exc})")
                    break
                if isinstance(body, dict) and body.get("error"):
                    # Public nodes answer 200-with-error for their own troubles (-32603
                    # internal, -32005 limit exceeded) as readily as for a bad request, and
                    # the two are not reliably distinguishable - so fail over rather than
                    # give up, and only surface the error once every endpoint has refused.
                    last_error = RpcError(f"{url} {method}: {body['error']}")
                    break
                return (body or {}).get("result")
        raise last_error

    # -- EVM -------------------------------------------------------------------
    def evm_native(self, chain: str, address: str) -> float:
        wei = _hex_to_int(self.rpc(chain, "eth_getBalance", [address, "latest"]))
        return wei / 1e18  # every supported EVM chain uses an 18-decimal native coin

    def evm_token_balance(self, chain: str, address: str, token: str, decimals: int,
                          *, underlying: bool = False) -> float:
        """ERC-20 balance of `address`. With `underlying`, ask the contract what the position
        is worth in the asset it wraps (`balanceOfUnderlying`), e.g. the ETH behind the shares
        of a staking pool, rewards included, instead of the number of shares."""
        selector = SEL_BALANCE_OF_UNDERLYING if underlying else SEL_BALANCE_OF
        data = selector + address.lower().removeprefix("0x").rjust(64, "0")
        raw = _hex_to_int(self.rpc(chain, "eth_call", [{"to": token, "data": data}, "latest"]))
        return raw / (10 ** decimals)

    def evm_token_meta(self, chain: str, token: str) -> tuple[str, int]:
        """(symbol, decimals) read from the contract; cached. Raises if decimals are unknown.

        Guessing the decimals is not an option: assuming 18 for a 6-decimal token like USDC
        reports the balance 10^12 times too large. A token whose `decimals()` cannot be read
        is skipped by the caller instead.
        """
        key = (chain, token.lower())
        if key in self._token_meta:
            return self._token_meta[key]
        decimals = _hex_to_int(self.rpc(chain, "eth_call", [{"to": token, "data": SEL_DECIMALS}, "latest"]))
        if not 0 <= decimals <= 36:
            raise RpcError(f"{token} on {chain}: implausible decimals ({decimals})")
        try:
            symbol = _decode_abi_string(self.rpc(chain, "eth_call", [{"to": token, "data": SEL_SYMBOL}, "latest"]))
        except (RpcError, requests.RequestException):
            symbol = ""
        symbol = symbol or f"{token[:6]}..{token[-4:]}"
        self._token_meta[key] = (symbol, decimals)
        return symbol, decimals

    def fetch_evm(self, chain: str, wallet: dict) -> list[Holding]:
        """Native coin + every configured ERC-20 for one wallet on one chain."""
        config = self.chains.get(chain, {})
        address = wallet["address"]
        label = f"{chain}/{wallet['label']}"
        holdings: list[Holding] = []

        quantity = self.evm_native(chain, address)
        if quantity > 0:
            holdings.append(
                Holding(symbol=config.get("native_symbol", chain.upper()), source=ONCHAIN,
                        quantity=quantity, detail=label, price_id=config.get("native_price_id", ""))
            )

        llama_chain = config.get("llama_chain", chain)
        for token in _tokens_for(config, wallet, chain):
            token_address = str(token.get("address", "")).strip()
            if not token_address:
                continue
            symbol, decimals = token.get("symbol"), token.get("decimals")
            if symbol is None or decimals is None:
                try:
                    discovered_symbol, discovered_decimals = self.evm_token_meta(chain, token_address)
                except (RpcError, requests.RequestException) as exc:
                    self.errors.append(f"{label} {symbol or token_address}: unreadable token metadata ({exc})")
                    continue
                symbol = symbol or discovered_symbol
                decimals = discovered_decimals if decimals is None else decimals
            try:
                quantity = self.evm_token_balance(chain, address, token_address, int(decimals),
                                                  underlying=bool(token.get("underlying")))
            except (RpcError, requests.RequestException) as exc:
                self.errors.append(f"{label} {symbol}: {exc}")
                continue
            if quantity > 0:
                # `price_id` overrides the contract's own price: pool shares have no market,
                # but the underlying they are counted in (ETH) does.
                price_id = str(token.get("price_id") or "").strip() or f"{llama_chain}:{token_address.lower()}"
                holdings.append(
                    Holding(symbol=str(symbol).upper(), source=ONCHAIN, quantity=quantity,
                            detail=label, price_id=price_id)
                )
        return holdings

    # -- Solana ----------------------------------------------------------------
    def solana_native(self, address: str) -> float:
        lamports = self.rpc("solana", "getBalance", [address])
        return _to_float(_unwrap(lamports)) / 1e9  # 1 SOL = 1e9 lamports

    def fetch_solana(self, wallet: dict) -> list[Holding]:
        """SOL balance + every SPL token account with a non-zero balance."""
        config = self.chains.get("solana", {})
        address = wallet["address"]
        label = f"solana/{wallet['label']}"
        holdings: list[Holding] = []

        quantity = self.solana_native(address)
        if quantity > 0:
            holdings.append(
                Holding(symbol=config.get("native_symbol", "SOL"), source=ONCHAIN, quantity=quantity,
                        detail=label, price_id=config.get("native_price_id", "coingecko:solana"))
            )

        overrides = {str(k).lower(): v for k, v in (wallet.get("tokens") or {}).items()}
        for program in SPL_PROGRAMS:
            try:
                result = self.rpc(
                    "solana", "getTokenAccountsByOwner",
                    [address, {"programId": program}, {"encoding": "jsonParsed"}],
                )
            except (RpcError, requests.RequestException) as exc:
                self.errors.append(f"{label} SPL ({program[:4]}..): {exc}")
                continue
            for account in (_unwrap(result) or []):
                try:
                    info = account["account"]["data"]["parsed"]["info"]
                    mint = info["mint"]
                    quantity = _to_float(info["tokenAmount"].get("uiAmountString")
                                         or info["tokenAmount"].get("uiAmount"))
                except (KeyError, TypeError):
                    continue
                if quantity <= 0:
                    continue
                holdings.append(
                    Holding(symbol=str(overrides.get(mint.lower(), mint[:4] + ".." + mint[-4:])).upper(),
                            source=ONCHAIN, quantity=quantity, detail=label,
                            price_id=f"solana:{mint}")
                )
        return holdings

    # -- pricing ---------------------------------------------------------------
    def price(self, holdings: list[Holding]) -> None:
        """Fill `price_usd` in place from DefiLlama, batching the lookups."""
        keys = sorted({h.price_id for h in holdings if h.price_id})
        if not keys:
            return
        prices: dict[str, dict] = {}
        for start in range(0, len(keys), LLAMA_BATCH):
            chunk = keys[start:start + LLAMA_BATCH]
            try:
                response = self.session.get(LLAMA_PRICES + ",".join(chunk), timeout=self.timeout)
                response.raise_for_status()
                prices.update(response.json().get("coins", {}))
            except (requests.RequestException, ValueError) as exc:
                self.errors.append(f"defillama prices ({len(chunk)} keys): {exc}")

        lowered = {k.lower(): v for k, v in prices.items()}
        for holding in holdings:
            coin = prices.get(holding.price_id) or lowered.get(holding.price_id.lower()) or {}
            holding.price_usd = _to_float(coin.get("price"))
            # DefiLlama knows the real ticker; prefer it over a truncated mint/contract
            symbol = coin.get("symbol")
            if symbol and ".." in holding.symbol:
                holding.symbol = str(symbol).upper()

    # -- orchestration ---------------------------------------------------------
    def _jobs(self, evm_wallets: list[dict], solana_wallets: list[dict]) -> list[tuple]:
        """`(name, fetch, args)` per wallet and chain; `name` is `<chain>/<label>`."""
        jobs = []
        for wallet in evm_wallets:
            chains = [c for c in (wallet.get("chains") or EVM_CHAINS) if c in self.chains]
            for chain in chains:
                jobs.append((f"{chain}/{wallet['label']}", self.fetch_evm, (chain, wallet)))
        for wallet in solana_wallets:
            jobs.append((f"solana/{wallet['label']}", self.fetch_solana, (wallet,)))
        return jobs

    def sources(self, evm_wallets: list[dict], solana_wallets: list[dict]) -> tuple[list[str], list[str]]:
        """`(queried, disabled)` `<chain>/<label>` names for `fetch_all` with these wallets.

        `disabled` are EVM chains a wallet lists explicitly but that have no chain configured,
        so `fetch_all` skips them.
        """
        queried = [name for name, _, _ in self._jobs(evm_wallets, solana_wallets)]
        disabled = [
            f"{chain}/{wallet['label']}"
            for wallet in evm_wallets
            for chain in (wallet.get("chains") or [])
            if chain not in self.chains
        ]
        return queried, disabled

    def fetch_all(self, evm_wallets: list[dict], solana_wallets: list[dict]) -> tuple[list[Holding], list[str]]:
        """Every wallet on every chain, fetched in parallel and priced in one pass."""
        self.errors = []
        jobs = self._jobs(evm_wallets, solana_wallets)

        holdings: list[Holding] = []
        if jobs:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(jobs))) as pool:
                futures = {pool.submit(fn, *args): name for name, fn, args in jobs}
                for future, name in futures.items():
                    try:
                        holdings.extend(future.result())
                    except (RpcError, requests.RequestException, KeyError, TypeError, ValueError) as exc:
                        self.errors.append(f"{name}: {exc}")

        self.price(holdings)
        return holdings, list(self.errors)


def _tokens_for(chain_config: dict, wallet: dict, chain: str) -> list[dict]:
    """Tokens configured on the chain, plus any extras pinned to this wallet."""
    per_wallet = (wallet.get("tokens") or {}).get(chain) if isinstance(wallet.get("tokens"), dict) else None
    return [t for t in itertools.chain(chain_config.get("tokens") or [], per_wallet or []) if isinstance(t, dict)]


def _hex_to_int(value) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.startswith("0x"):
        try:
            return int(value, 16)
        except ValueError:
            return 0
    return 0


def _decode_abi_string(value) -> str:
    """Decode a `string` return value; some old tokens (MKR, SAI) return a raw bytes32."""
    if not isinstance(value, str) or not value.startswith("0x"):
        return ""
    try:
        raw = bytes.fromhex(value[2:])
    except ValueError:
        return ""
    if len(raw) == 32:  # bytes32, right-padded with NULs
        return raw.rstrip(b"\x00").decode("utf-8", "ignore").strip()
    if len(raw) >= 64:  # ABI string: 32-byte offset, 32-byte length, then the bytes
        offset = int.from_bytes(raw[:32], "big")
        if offset + 32 > len(raw):
            return ""
        length = int.from_bytes(raw[offset:offset + 32], "big")
        return raw[offset + 32:offset + 32 + length].decode("utf-8", "ignore").strip()
    return ""


def _unwrap(result):
    """Solana wraps most results in {"context": .., "value": ..}."""
    if isinstance(result, dict) and "value" in result:
        return result["value"]
    return result


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
