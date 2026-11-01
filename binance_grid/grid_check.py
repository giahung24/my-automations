"""Binance Spot Grid regime check and parameter calculator (see SPEC.md).

Uses only public Binance klines, never places orders. Writes runs/<pair>_<date>.json per pair,
prints all results as a JSON list and a one-line summary per pair on stderr.

    python grid_check.py --pairs ETHUSDC BTCUSDC SOLUSDC LINKUSDC --fee 0.001 --days 21 --capital 1000
"""
import argparse
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# Binance's public market-data endpoint; api.binance.com answers 451 to US IPs (cloud runners)
API = "https://data-api.binance.vision/api/v3"
PAIRS = ["ETHUSDC", "BTCUSDC", "SOLUSDC", "LINKUSDC"]
RUNS = Path(__file__).parent / "runs"
DAILY, H4 = 300, 360  # closed candles: 300 days, 60 days of 4h
MAX_GRIDS = 170  # Binance Spot Grid FAQ: up to 170 grids per bot; confirm in the UI
MIN_NOTIONAL_MARGIN = 2  # each order must be >= 2x the exchange minimum


def get(path, **params):
    r = requests.get(f"{API}/{path}", params=params, timeout=15)
    if not r.ok:  # Binance puts the reason (invalid symbol, rate limit) in the body
        raise requests.HTTPError(f"{r.status_code} on {path} {params}: {r.text[:200]}")
    return r.json()


def klines(symbol, interval, n):
    """Last n closed candles; the still-forming one is dropped."""
    now_ms = time.time() * 1000
    rows = [r for r in get("klines", symbol=symbol, interval=interval, limit=n + 1) if r[6] < now_ms][-n:]
    df = pd.DataFrame([r[1:5] for r in rows], columns=["open", "high", "low", "close"]).astype(float)
    df.insert(0, "open_time", pd.to_datetime([r[0] for r in rows], unit="ms", utc=True))
    return df


def wilder(x, n):
    """Wilder smoothing: SMA of the first n values, then y += (x - y) / n."""
    x = x.dropna()
    s = x.iloc[n - 1:].copy()
    s.iloc[0] = x.iloc[:n].mean()
    return s.ewm(alpha=1 / n, adjust=False).mean()


def true_range(df):
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1)
    return tr.max(axis=1).iloc[1:]


def atr(df, n=14):
    return wilder(true_range(df), n)


def adx(df, n=14):
    up, down = df["high"].diff(), -df["low"].diff()
    plus = up.where((up > down) & (up > 0), 0.0).iloc[1:]
    minus = down.where((down > up) & (down > 0), 0.0).iloc[1:]
    tr = atr(df, n)
    plus_di, minus_di = 100 * wilder(plus, n) / tr, 100 * wilder(minus, n) / tr
    return wilder(100 * (plus_di - minus_di).abs() / (plus_di + minus_di), n)


def efficiency_ratio(close, n=30):
    c = close.iloc[-n - 1:]
    return abs(c.iloc[-1] - c.iloc[0]) / c.diff().abs().sum()


def ann_vol(close, n):
    return np.log(close).diff().iloc[-n:].std() * math.sqrt(365)


def compute_metrics(daily, h4, btc):
    c = daily["close"]
    price = c.iloc[-1]
    sma50 = c.rolling(50).mean()
    last30 = daily.iloc[-30:]
    lo, hi = last30["low"].min(), last30["high"].max()
    vol30 = ann_vol(c, 30)
    return {
        "pair_vs_sma200": price / c.rolling(200).mean().iloc[-1] - 1,
        "btc_vs_sma200": btc["close"].iloc[-1] / btc["close"].rolling(200).mean().iloc[-1] - 1,
        "sma50_slope_20d": sma50.iloc[-1] / sma50.iloc[-21] - 1,
        "adx14": adx(daily).iloc[-1],
        "efficiency_ratio_30d": efficiency_ratio(c),
        "vol30_ann": vol30,
        "vol_ratio_30_180": vol30 / ann_vol(c, 180),
        "atr14_daily_pct": atr(daily).iloc[-1] / price,
        "atr14_4h_pct": atr(h4).iloc[-1] / h4["close"].iloc[-1],
        "range30": [lo, hi],
        "range30_position": (price - lo) / (hi - lo),
    }


def decide(m):
    """Verdict rules of SPEC step 1, first match wins."""
    if (m["pair_vs_sma200"] < 0 and m["sma50_slope_20d"] < 0) or m["btc_vs_sma200"] < 0:
        return "NO-GO", "downtrend: price < SMA200 with falling SMA50, or BTC < SMA200"
    if m["adx14"] > 30 or m["efficiency_ratio_30d"] > 0.5:
        return "NO-GO", "strong trend: ADX > 30 or ER > 0.5, use the trend bot"
    if m["vol_ratio_30_180"] > 1.5:
        return "NO-GO", "volatility spike: vol30/vol180 > 1.5"
    go = {
        "ADX<22": m["adx14"] < 22,
        "ER<0.35": m["efficiency_ratio_30d"] < 0.35,
        "|slope|<5%": abs(m["sma50_slope_20d"]) < 0.05,
        "price mid-range": 0.25 <= m["range30_position"] <= 0.75,
    }
    if all(go.values()):
        return "GO", ", ".join(go)
    return "CAUTION", "not met: " + ", ".join(k for k, ok in go.items() if not ok)


def net_profit_per_grid(lower, upper, grids, fee):
    """Binance geometric formula, net of fees."""
    return (1 - fee) * (upper / lower) ** (1 / grids) - 1 - fee


def grid_params(m, price, verdict, fee, days, capital, min_notional):
    """Parameter formulas of SPEC step 2. Returns (params, worst_case_loss_pct, warnings)."""
    warnings = []
    atr_d = m["atr14_daily_pct"]
    lo30, hi30 = m["range30"]
    move = (1.5 if verdict == "GO" else 2) * m["vol30_ann"] / math.sqrt(365) * math.sqrt(days)
    lower = min(price * math.exp(-move), lo30 * (1 - 0.5 * atr_d))
    upper = max(price * math.exp(move), hi30 * (1 + 0.5 * atr_d))

    floor, cap = max(0.006, 6 * fee), atr_d / 2
    if floor > cap:
        warnings.append(f"spacing floor {floor:.2%} > cap {cap:.2%} (ATR daily / 2): floor used")
    spacing = max(min(m["atr14_4h_pct"], cap), floor)
    span = math.log(upper / lower)
    # round() alone can push the real spacing under the fee floor; cap N so it can't
    grids = min(round(span / math.log(1 + spacing)), math.floor(span / math.log(1 + floor)))
    if not 2 <= grids <= MAX_GRIDS:
        warnings.append(f"{grids} grids outside Binance limit 2-{MAX_GRIDS}")

    investment = capital * (0.5 if verdict == "CAUTION" else 1)
    per_order = investment / grids
    if per_order < MIN_NOTIONAL_MARGIN * min_notional:
        warnings.append(f"{per_order:.2f} per order < {MIN_NOTIONAL_MARGIN}x exchange minimum {min_notional}")

    stop = lower * (1 - atr_d)
    ratio = (upper / lower) ** (1 / grids)
    # Worst case: price falls straight to the stop with every buy level filled.
    # Levels above the current price were bought at creation, at the current price.
    held = sum(per_order / min(lower * ratio**i, price) for i in range(grids))
    # ponytail: prices rounded to 0.01, fine for pairs above ~$1; use the symbol's tickSize for cheaper coins
    params = {
        "mode": "geometric",
        "lower": round(lower, 2),
        "upper": round(upper, 2),
        "grids": grids,
        "net_profit_per_grid": round(net_profit_per_grid(lower, upper, grids, fee), 5),
        "investment_usdc": investment,
        "stop_loss": round(stop, 2),
        "take_profit": None,
        "take_profit_optional": round(upper * (1 + atr_d), 2),
        "trailing_up": False,
    }
    return params, round(held * stop / investment - 1, 4), warnings


def report(m, price, fee, days, capital, min_notional):
    verdict, reason = decide(m)
    out = {"verdict": verdict, "reason": reason, "params": None, "worst_case_loss_pct": None, "warnings": []}
    if verdict == "NO-GO":
        return out
    params, worst, out["warnings"] = grid_params(m, price, verdict, fee, days, capital, min_notional)
    net = params["net_profit_per_grid"]
    if round(net, 4) < 4 * fee:  # 2x round-trip fee; Binance shows the % with 2 decimals
        out.update(verdict="NO-GO", reason=f"net profit per grid {net:.2%} < 2x round-trip fee {4 * fee:.2%}")
        return out
    out.update(params=params, worst_case_loss_pct=worst)
    return out


def data_warnings(name, df, n, step):
    gaps = int((df["open_time"].diff().iloc[1:] != step).sum())
    return [f"{name}: {len(df)}/{n} candles, {gaps} gaps"] if gaps or len(df) < n else []


def check_pair(pair, fee, days, capital):
    """Full check of one pair; saves runs/<pair>_<date>.json and returns the result."""
    with ThreadPoolExecutor() as ex:
        info, daily, h4 = (ex.submit(get, "exchangeInfo", symbol=pair),
                           ex.submit(klines, pair, "1d", DAILY), ex.submit(klines, pair, "4h", H4))
        sym = info.result()["symbols"][0]
        btc_pair = "BTC" + sym["quoteAsset"]
        btc = ex.submit(klines, btc_pair, "1d", DAILY)
        daily, h4, btc = daily.result(), h4.result(), btc.result()

    m = compute_metrics(daily, h4, btc)
    bad = [k for k, v in m.items() if k != "range30" and not math.isfinite(v)]
    if bad:
        raise ValueError(f"not enough data to compute: {', '.join(bad)}")
    min_notional = float(next(f["minNotional"] for f in sym["filters"] if f["filterType"] == "NOTIONAL"))
    price = float(daily["close"].iloc[-1])
    day = pd.Timedelta(days=1)
    as_of = (daily["open_time"].iloc[-1] + day).strftime("%Y-%m-%dT%H:%M:%SZ")

    out = {
        "pair": pair,
        "as_of": as_of,
        "price": price,
        "metrics": {k: [round(x, 2) for x in v] if k == "range30" else round(float(v), 4) for k, v in m.items()},
        **report(m, price, fee, days, capital, min_notional),
    }
    out["warnings"] = (data_warnings(pair + " 1d", daily, DAILY, day) + data_warnings(pair + " 4h", h4, H4, day / 6)
                       + data_warnings(btc_pair + " 1d", btc, DAILY, day)
                       + out["warnings"])

    RUNS.mkdir(exist_ok=True)
    (RUNS / f"{pair}_{as_of[:10]}.json").write_text(json.dumps(out, indent=2))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pairs", "--pair", nargs="+", default=PAIRS, help=f"default: {' '.join(PAIRS)}")
    ap.add_argument("--fee", type=float, default=0.001, help="fee per side, 0.001 = 0.1%%")
    ap.add_argument("--days", type=int, default=21, help="planned bot duration in days (14-30)")
    ap.add_argument("--capital", type=float, default=1000, help="grid capital per pair, in the quote asset")
    a = ap.parse_args(argv)
    if not 14 <= a.days <= 30:
        ap.error("--days must be between 14 and 30")
    if not 0 <= a.fee < 0.01:
        ap.error("--fee is a fraction per side, e.g. 0.001")
    if a.capital <= 0:
        ap.error("--capital must be positive")

    with ThreadPoolExecutor() as ex:
        futures = {p: ex.submit(check_pair, p, a.fee, a.days, a.capital) for p in dict.fromkeys(map(str.upper, a.pairs))}
    results = []
    for pair, f in futures.items():
        try:
            results.append(f.result())
        except Exception as e:  # one bad pair (typo, missing history) must not hide the others
            results.append({"pair": pair, "error": f"{type(e).__name__}: {e}"})

    print(json.dumps(results, indent=2))
    for r in results:
        print(f"{r['pair']:<9} {r.get('verdict', 'ERROR'):<7} {r.get('reason') or r['error']}", file=sys.stderr)
    return 1 if any("error" in r for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
