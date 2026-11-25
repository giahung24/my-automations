"""Binance read-only client: balances (Spot, Simple Earn, Funding), USDT pricing, and the
deposit / withdrawal / fiat / P2P / Earn-reward history used by the cash-flow engine (Epic F2, M4).

Signed endpoints use HMAC-SHA256 over the query string with the API secret, exactly as
documented, and the client only ever issues GET/POST calls that *read* balances - nothing
here can place, cancel or transfer anything. The API key only needs "Enable Reading".

    client = BinanceClient(api_key, api_secret)
    holdings, errors = client.fetch_all()
"""
from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from urllib.parse import urlencode

import requests

from models import BINANCE_EARN, BINANCE_FUNDING, BINANCE_SPOT, Holding

# Signed calls must go to the account host. `data-api.binance.vision` serves the public
# market endpoints and, unlike api.binance.com, answers from US/cloud IPs (which get 451).
SIGNED_HOSTS = ["https://api.binance.com", "https://api1.binance.com", "https://api2.binance.com"]
PUBLIC_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]

PAGE_SIZE = 100          # Simple Earn position endpoints cap `size` at 100
MAX_PAGES = 100          # hard stop so a misbehaving `total` cannot spin forever
RETRIES = 3
# USD proxies tried in order when `<ASSET>USDT` does not exist, then crypto bridges.
USD_QUOTES = ["USDT", "FDUSD", "USDC", "BUSD", "TUSD"]
BRIDGE_QUOTES = ["BTC", "ETH", "BNB"]


DAY_MS = 86_400_000


@dataclass(frozen=True)
class HistoryStream:
    """One read-only history endpoint: how to page it and how wide one query may be.

    Binance refuses a time range wider than `window_days`, so a long range is cut into
    consecutive chunks; each chunk is then paged (`offset` + `limit`, or `page` + `rows`).
    """

    path: str
    window_days: int
    time_keys: tuple[str, str]           # (start, end) parameter names, epoch ms
    paging: str                          # "offset" (plain list) | "page" ({"data": [...]})
                                         # | "current" ({"rows": [...], "total": n}, Simple Earn)
    page_size: int
    params: dict = field(default_factory=dict)


# ROADMAP_LEDGER §3.4. [to verify against the live API docs] the window caps of the fiat and
# C2C endpoints are not documented as firmly as the 90 days of the capital ones; 90 / 30 days
# are the conservative values. Every one of them is a GET and only reads.
HISTORY_STREAMS: dict[str, HistoryStream] = {
    "deposit": HistoryStream("/sapi/v1/capital/deposit/hisrec", 90, ("startTime", "endTime"),
                             "offset", 1000),
    "withdraw": HistoryStream("/sapi/v1/capital/withdraw/history", 90, ("startTime", "endTime"),
                              "offset", 1000),
    "fiat_deposit": HistoryStream("/sapi/v1/fiat/orders", 90, ("beginTime", "endTime"),
                                  "page", 500, {"transactionType": 0}),
    "fiat_withdraw": HistoryStream("/sapi/v1/fiat/orders", 90, ("beginTime", "endTime"),
                                   "page", 500, {"transactionType": 1}),
    "fiat_buy": HistoryStream("/sapi/v1/fiat/payments", 90, ("beginTime", "endTime"),
                              "page", 500, {"transactionType": 0}),
    "fiat_sell": HistoryStream("/sapi/v1/fiat/payments", 90, ("beginTime", "endTime"),
                               "page", 500, {"transactionType": 1}),
    "p2p_buy": HistoryStream("/sapi/v1/c2c/orderMatch/listUserOrderHistory", 30,
                             ("startTimestamp", "endTimestamp"), "page", 100, {"tradeType": "BUY"}),
    "p2p_sell": HistoryStream("/sapi/v1/c2c/orderMatch/listUserOrderHistory", 30,
                              ("startTimestamp", "endTimestamp"), "page", 100, {"tradeType": "SELL"}),
    # Simple Earn income (M4). [to verify against the live API docs] the 3-month window cap and
    # whether `type=ALL` is accepted; the two documented types are queried one by one instead.
    "earn_flexible": HistoryStream("/sapi/v1/simple-earn/flexible/history/rewardsRecord", 90,
                                   ("startTime", "endTime"), "current", 100, {"type": "REALTIME"}),
    "earn_flexible_bonus": HistoryStream("/sapi/v1/simple-earn/flexible/history/rewardsRecord", 90,
                                         ("startTime", "endTime"), "current", 100, {"type": "BONUS"}),
    "earn_locked": HistoryStream("/sapi/v1/simple-earn/locked/history/rewardsRecord", 90,
                                 ("startTime", "endTime"), "current", 100),
}


def time_chunks(start_ms: int, end_ms: int, window_days: int) -> list[tuple[int, int]]:
    """Cut `[start_ms, end_ms]` into consecutive inclusive ranges no wider than `window_days`.

    365 days with a 90-day cap gives 5 chunks. Each chunk spans at most `window_days` minus one
    millisecond, so an endpoint that rejects a range of exactly the cap still accepts it.
    """
    start_ms, end_ms = int(start_ms), int(end_ms)
    if end_ms < start_ms:
        return []
    span = max(int(window_days), 1) * DAY_MS - 1
    chunks = []
    cursor = start_ms
    while cursor <= end_ms:
        chunk_end = min(cursor + span, end_ms)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + 1
    return chunks


class BinanceError(RuntimeError):
    """Binance answered, but with an error payload (bad key, bad signature, rate limit)."""


class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, timeout: int = 20, recv_window: int = 5000,
                 session: requests.Session | None = None):
        self.api_key = api_key
        self.api_secret = (api_secret or "").encode()
        self.timeout = timeout
        self.recv_window = recv_window
        self.session = session or requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key, "User-Agent": "crypto-portfolio/1.0"})
        self._time_offset_ms = 0
        self._synced = False
        self._prices: dict[str, float] | None = None

    # -- transport -------------------------------------------------------------
    def _request(self, method: str, path: str, hosts: list[str], params: dict | None = None,
                 signed: bool = False) -> object:
        """One API call with host failover and backoff. Raises on definitive failure."""
        params = dict(params or {})
        last_error: Exception = BinanceError(f"no host tried for {path}")
        for host in hosts:
            for attempt in range(RETRIES):
                query = dict(params)
                if signed:
                    query["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
                    query["recvWindow"] = self.recv_window
                    encoded = urlencode(query)
                    query["signature"] = hmac.new(self.api_secret, encoded.encode(), hashlib.sha256).hexdigest()
                try:
                    response = self.session.request(
                        method, f"{host}{path}", params=query, timeout=self.timeout
                    )
                except requests.RequestException as exc:  # DNS, TLS, timeout, reset
                    last_error = exc
                    break  # host looks unreachable - move to the next one
                if response.ok:
                    return response.json()
                last_error = BinanceError(
                    f"HTTP {response.status_code} on {path}: {response.text[:200]}"
                )
                if response.status_code in (429, 418):  # rate limited / banned
                    wait = float(response.headers.get("Retry-After", 2 ** attempt))
                    time.sleep(min(wait, 10))
                    continue
                if response.status_code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                if response.status_code == 451:  # geo-blocked host, try the next
                    break
                if self._is_clock_skew(response) and attempt == 0:
                    self.sync_time(force=True)
                    continue
                raise last_error  # 400/401/403: retrying will not help
        raise last_error

    @staticmethod
    def _is_clock_skew(response: requests.Response) -> bool:
        """-1021 = "timestamp ahead of server time": re-sync and retry once."""
        try:
            return response.json().get("code") == -1021
        except ValueError:
            return False

    def _get(self, path, params=None, signed=False):
        return self._request("GET", path, SIGNED_HOSTS if signed else PUBLIC_HOSTS, params, signed)

    def _post(self, path, params=None, signed=True):
        return self._request("POST", path, SIGNED_HOSTS, params, signed)

    def sync_time(self, force: bool = False) -> int:
        """Align our clock with Binance's; a few seconds of drift otherwise rejects every call."""
        if self._synced and not force:
            return self._time_offset_ms
        try:
            server_ms = int(self._get("/api/v3/time")["serverTime"])
            self._time_offset_ms = server_ms - int(time.time() * 1000)
        except (requests.RequestException, BinanceError, KeyError, ValueError, TypeError):
            self._time_offset_ms = 0  # best effort: a correct local clock still works
        self._synced = True
        return self._time_offset_ms

    # -- pricing ---------------------------------------------------------------
    def prices(self, refresh: bool = False) -> dict[str, float]:
        """All spot tickers as {symbol: price}; one call covers every asset we hold."""
        if self._prices is None or refresh:
            rows = self._get("/api/v3/ticker/price")
            self._prices = {}
            for row in rows or []:
                try:
                    self._prices[row["symbol"]] = float(row["price"])
                except (KeyError, TypeError, ValueError):
                    continue
        return self._prices

    def price_usd(self, asset: str, stablecoins: set[str] | None = None) -> float:
        """USD price for one asset. Stablecoins are pinned at $1.00; 0.0 means "unknown"."""
        asset = asset.upper()
        stablecoins = stablecoins or {"USDT", "USDC", "FDUSD"}
        if asset in stablecoins:
            return 1.0
        try:
            tickers = self.prices()
        except (requests.RequestException, BinanceError):
            return 0.0
        for quote in USD_QUOTES:
            price = tickers.get(f"{asset}{quote}")
            if price:
                return price
        for quote in BRIDGE_QUOTES:  # e.g. an alt only quoted in BTC
            leg = tickers.get(f"{asset}{quote}")
            bridge = tickers.get(f"{quote}USDT")
            if leg and bridge:
                return leg * bridge
        return 0.0

    # -- key check -------------------------------------------------------------
    def api_restrictions(self) -> dict:
        """GET /sapi/v1/account/apiRestrictions - what this key is allowed to do.

        Fails with BinanceError on a bad key or signature, so it doubles as a credentials
        check that costs one request and touches no balance.
        """
        self.sync_time()
        data = self._get("/sapi/v1/account/apiRestrictions", signed=True)
        return data if isinstance(data, dict) else {}

    # -- balances --------------------------------------------------------------
    def spot_balances(self) -> list[Holding]:
        """GET /api/v3/account - free + locked per asset."""
        self.sync_time()
        data = self._get("/api/v3/account", signed=True)
        holdings = []
        for row in (data or {}).get("balances", []):
            quantity = _to_float(row.get("free")) + _to_float(row.get("locked"))
            if quantity > 0:
                holdings.append(Holding(symbol=row["asset"].upper(), source=BINANCE_SPOT, quantity=quantity))
        return holdings

    def _paged(self, path: str, params: dict | None = None) -> list[dict]:
        """Walk a Simple Earn `rows`/`total` paginated endpoint to the end."""
        self.sync_time()
        collected: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            payload = self._get(path, {**(params or {}), "current": page, "size": PAGE_SIZE}, signed=True)
            rows = (payload or {}).get("rows") or []
            collected.extend(rows)
            total = _to_int((payload or {}).get("total"))
            if len(rows) < PAGE_SIZE or (total and len(collected) >= total):
                break
        return collected

    def earn_flexible(self) -> list[Holding]:
        """GET /sapi/v1/simple-earn/flexible/position - every active flexible position."""
        holdings = []
        for row in self._paged("/sapi/v1/simple-earn/flexible/position"):
            quantity = _to_float(row.get("totalAmount"))
            if quantity > 0:
                holdings.append(
                    Holding(symbol=str(row.get("asset", "")).upper(), source=BINANCE_EARN,
                            quantity=quantity, detail="flexible")
                )
        return holdings

    def earn_locked(self) -> list[Holding]:
        """GET /sapi/v1/simple-earn/locked/position - every active locked staking position."""
        holdings = []
        for row in self._paged("/sapi/v1/simple-earn/locked/position"):
            # locked rows expose `amount`; some product variants return `totalAmount` instead
            quantity = _to_float(row.get("amount")) or _to_float(row.get("totalAmount"))
            if quantity > 0:
                # `asset` is the staked asset, `rewardAsset` is what it pays out
                symbol = row.get("asset") or row.get("rewardAsset") or ""
                holdings.append(
                    Holding(symbol=str(symbol).upper(), source=BINANCE_EARN,
                            quantity=quantity, detail="locked")
                )
        return holdings

    def funding_balances(self) -> list[Holding]:
        """POST /sapi/v1/asset/get-funding-asset - free + locked + freeze + withdrawing."""
        self.sync_time()
        rows = self._post("/sapi/v1/asset/get-funding-asset") or []
        holdings = []
        for row in rows:
            quantity = sum(
                _to_float(row.get(field)) for field in ("free", "locked", "freeze", "withdrawing")
            )
            if quantity > 0:
                holdings.append(
                    Holding(symbol=str(row.get("asset", "")).upper(), source=BINANCE_FUNDING, quantity=quantity)
                )
        return holdings

    # -- cash-flow history (Epic F2) ---------------------------------------------
    def history_chunks(self, stream: str, start_ms: int, end_ms: int) -> Iterator[tuple[int, list[dict]]]:
        """Yield `(chunk_end_ms, rows)` for every time chunk of one history stream, oldest first.

        Yielding per chunk lets the caller commit a sync cursor after each one, so a long
        backfill that hits a rate limit half way resumes where it stopped.
        """
        for _, chunk_end, rows in self.history_windows(stream, start_ms, end_ms):
            yield chunk_end, rows

    def history_windows(self, stream: str, start_ms: int, end_ms: int,
                        newest_first: bool = False) -> Iterator[tuple[int, int, list[dict]]]:
        """Yield `(chunk_start_ms, chunk_end_ms, rows)` for every time chunk of one stream.

        `newest_first` walks backwards in time: a back-fill that extends the history further
        into the past (5 years, M4) can then record how far back it got after each chunk.
        """
        spec = HISTORY_STREAMS[stream]
        self.sync_time()
        chunks = time_chunks(start_ms, end_ms, spec.window_days)
        if newest_first:
            chunks.reverse()
        for chunk_start, chunk_end in chunks:
            params = {**spec.params, spec.time_keys[0]: chunk_start, spec.time_keys[1]: chunk_end}
            yield chunk_start, chunk_end, self._history_pages(spec, params)

    def history(self, stream: str, start_ms: int, end_ms: int) -> list[dict]:
        """Every row of one history stream between two epoch-ms instants."""
        rows: list[dict] = []
        for _, chunk in self.history_chunks(stream, start_ms, end_ms):
            rows.extend(chunk)
        return rows

    def deposit_history(self, start_ms: int, end_ms: int) -> list[dict]:
        """GET /sapi/v1/capital/deposit/hisrec - crypto deposits into Binance."""
        return self.history("deposit", start_ms, end_ms)

    def withdraw_history(self, start_ms: int, end_ms: int) -> list[dict]:
        """GET /sapi/v1/capital/withdraw/history - crypto withdrawals out of Binance."""
        return self.history("withdraw", start_ms, end_ms)

    def fiat_orders(self, transaction_type: int, start_ms: int, end_ms: int) -> list[dict]:
        """GET /sapi/v1/fiat/orders - bank deposits (0) / withdrawals (1) of fiat."""
        return self.history("fiat_withdraw" if int(transaction_type) else "fiat_deposit", start_ms, end_ms)

    def fiat_payments(self, transaction_type: int, start_ms: int, end_ms: int) -> list[dict]:
        """GET /sapi/v1/fiat/payments - crypto bought (0) / sold (1) with a card or bank."""
        return self.history("fiat_sell" if int(transaction_type) else "fiat_buy", start_ms, end_ms)

    def p2p_orders(self, trade_type: str, start_ms: int, end_ms: int) -> list[dict]:
        """GET /sapi/v1/c2c/orderMatch/listUserOrderHistory - P2P BUY / SELL orders."""
        return self.history("p2p_sell" if str(trade_type).upper() == "SELL" else "p2p_buy",
                            start_ms, end_ms)

    def _history_pages(self, spec: HistoryStream, params: dict) -> list[dict]:
        """Every page of one time chunk. A full page means there may be another one."""
        collected: list[dict] = []
        for page in range(MAX_PAGES):
            if spec.paging == "offset":
                payload = self._get(spec.path, {**params, "limit": spec.page_size,
                                                "offset": page * spec.page_size}, signed=True)
                if not isinstance(payload, list):
                    raise BinanceError(f"unexpected payload on {spec.path}: {str(payload)[:200]}")
                rows = payload
            elif spec.paging == "current":
                payload = self._get(spec.path, {**params, "current": page + 1, "size": spec.page_size},
                                    signed=True)
                if not isinstance(payload, dict):
                    raise BinanceError(f"unexpected payload on {spec.path}: {str(payload)[:200]}")
                rows = payload.get("rows") or []
                total = _to_int(payload.get("total"))
                collected.extend(row for row in rows if isinstance(row, dict))
                if len(rows) < spec.page_size or (total and len(collected) >= total):
                    break
                continue
            else:
                payload = self._get(spec.path, {**params, "page": page + 1, "rows": spec.page_size},
                                    signed=True)
                if not isinstance(payload, dict):
                    raise BinanceError(f"unexpected payload on {spec.path}: {str(payload)[:200]}")
                if payload.get("success") is False:
                    raise BinanceError(f"{spec.path}: {payload.get('message') or payload.get('code')}")
                rows = payload.get("data") or []
            collected.extend(row for row in rows if isinstance(row, dict))
            if len(rows) < spec.page_size:
                break
        return collected

    def fetch_all(self, stablecoins: set[str] | None = None) -> tuple[list[Holding], list[str]]:
        """Every wallet, priced. A failing section is reported, not raised."""
        holdings: list[Holding] = []
        errors: list[str] = []
        sections = (
            ("spot", self.spot_balances),
            ("simple earn flexible", self.earn_flexible),
            ("simple earn locked", self.earn_locked),
            ("funding", self.funding_balances),
        )
        for name, fetch in sections:
            try:
                holdings.extend(fetch())
            except (requests.RequestException, BinanceError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"binance {name}: {exc}")

        try:
            self.prices()
        except (requests.RequestException, BinanceError) as exc:
            errors.append(f"binance prices: {exc}")

        cache: dict[str, float] = {}
        for holding in holdings:
            if holding.symbol not in cache:
                cache[holding.symbol] = self.price_usd(holding.symbol, stablecoins)
            holding.price_usd = cache[holding.symbol]
        return holdings, errors


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
