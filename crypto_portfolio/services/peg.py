"""N3: is every stablecoin we hold still worth $1?

Stablecoins are pinned at $1.00 for display and NAV, so a de-peg would be invisible. Each
snapshot therefore stores what the stablecoins held were worth against two references:

* ``binance:<SYM>USDT`` - the spot ticker already loaded for pricing. It measures the coin against
  USDT, not USD, so USDT itself has no such reference and a second one is mandatory;
* ``defillama:coingecko:<id>`` - a USD price from DefiLlama's keyless endpoint.

A failed reference is stored with its error; it never blocks ``record``.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

import requests

from clients.onchain import LLAMA_PRICES

THRESHOLD = 0.003  # |price - 1| above this is shown as off peg (ROADMAP N3)

LLAMA_IDS = {
    "USDT": "coingecko:tether",
    "USDC": "coingecko:usd-coin",
    "FDUSD": "coingecko:first-digital-usd",
    "DAI": "coingecko:dai",
    "TUSD": "coingecko:true-usd",
    "USDP": "coingecko:paxos-standard",
    "PYUSD": "coingecko:paypal-usd",
    "USDE": "coingecko:ethena-usde",
}


def llama_prices(keys: list[str], timeout: int = 20) -> dict[str, float]:
    """``{key: usd price}`` for DefiLlama keys; raises on a network or payload failure."""
    response = requests.get(LLAMA_PRICES + ",".join(keys), timeout=timeout)
    response.raise_for_status()
    coins = response.json().get("coins", {})
    return {key: float(coin["price"]) for key, coin in coins.items() if coin.get("price")}


def check_pegs(symbols: Iterable[str], tickers: dict[str, float] | None,
               fetch_llama: Callable[[list[str]], dict[str, float]]) -> list[dict]:
    """One row per (stablecoin, reference): ``symbol``, ``reference``, ``price``, ``error``.

    ``tickers`` is Binance's ``{pair: price}`` map, or ``None`` when it could not be loaded.
    """
    symbols = sorted({s.upper() for s in symbols})
    rows: list[dict] = []

    def row(symbol: str, reference: str, price: float | None = None, error: str = "") -> None:
        rows.append({"symbol": symbol, "reference": reference, "price": price, "error": error})

    for symbol in symbols:
        pair = f"{symbol}USDT"
        if symbol == "USDT":
            continue
        if tickers is None:
            row(symbol, f"binance:{pair}", error="Binance tickers unavailable")
        elif tickers.get(pair):
            row(symbol, f"binance:{pair}", float(tickers[pair]))

    keys = {symbol: LLAMA_IDS[symbol] for symbol in symbols if symbol in LLAMA_IDS}
    prices, failure = {}, ""
    if keys:
        try:
            prices = fetch_llama(sorted(keys.values()))
        except Exception as exc:  # noqa: BLE001 - any failure is "this reference did not answer"
            failure = f"DefiLlama: {exc}"
    for symbol in symbols:
        key = keys.get(symbol)
        if key is None:
            row(symbol, "defillama", error="no DefiLlama id for this coin")
        elif prices.get(key):
            row(symbol, f"defillama:{key}", float(prices[key]))
        else:
            row(symbol, f"defillama:{key}", error=failure or "DefiLlama returned no price")
    return rows


def peg_warnings(rows: Iterable[dict], threshold: float = THRESHOLD) -> list[dict]:
    """What the dashboard shows: nothing for a coin on peg, else ``off`` or ``unchecked``.

    The deviation of a coin is its worst usable reference. ``unchecked`` means every reference
    of that coin failed.
    """
    by_symbol: dict[str, list[dict]] = {}
    for item in rows:
        by_symbol.setdefault(item["symbol"], []).append(item)
    warnings = []
    for symbol, checks in sorted(by_symbol.items()):
        usable = [c["price"] - 1.0 for c in checks if not c.get("error") and c.get("price")]
        if not usable:
            warnings.append({"symbol": symbol, "status": "unchecked", "deviation_pct": None})
            continue
        worst = max(usable, key=abs)
        if abs(worst) > threshold:
            warnings.append({"symbol": symbol, "status": "off", "deviation_pct": round(worst * 100, 2)})
    return warnings
