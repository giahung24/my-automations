"""Scan Binance USDC pairs for Spot Grid candidates with grid_check's rules (see SPEC.md).

Keeps TRADING spot pairs quoted in USDC with enough 24h volume, drops stablecoins and
leveraged tokens, runs the regime check on each (pairs under 300 days of history are skipped)
and ranks GO, then CAUTION, pairs by how comfortably they pass the GO rules.

    python grid_scan.py --min-volume 2000000
"""
import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from grid_check import CAPITAL, DAILY, DAYS, FEE, GO_ADX, GO_ER, GO_RANGE, GO_SLOPE, check_pair, get, klines

STABLES = {"USDT", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1", "BFUSD", "PYUSD", "RLUSD",
           "EUR", "EURI", "AEUR"}
STABLE_VOL = 0.05  # 30-day annualised volatility under 5%: a stablecoin missing from the list
MIN_VOLUME = 2e6  # 24h USDC volume; 14 pairs pass on 2026-11-02, LINKUSDC (2.5M) among them


def candidates(symbols, tickers, min_volume):
    """USDC spot pairs worth checking, most liquid first: [(exchangeInfo entry, 24h volume)]."""
    bases = {s["baseAsset"] for s in symbols}

    def leveraged(base):  # BTCUP, ETHDOWN: the prefix is itself a listed coin (JUP, SYRUP are not)
        m = re.fullmatch(r"(.+)(UP|DOWN|BULL|BEAR)", base)
        return bool(m) and m[1] in bases

    usdc = {s["symbol"]: s for s in symbols if s["quoteAsset"] == "USDC" and s["status"] == "TRADING"
            and s["baseAsset"] not in STABLES and not leveraged(s["baseAsset"])}
    vols = {t["symbol"]: float(t["quoteVolume"]) for t in tickers if t["symbol"] in usdc}
    return sorted(((usdc[p], v) for p, v in vols.items() if v >= min_volume), key=lambda sv: -sv[1])


def fit(m):
    """How comfortably the GO rules pass: 1 = ideal, 0 = on a threshold, below 0 = failed."""
    mid, half = sum(GO_RANGE) / 2, (GO_RANGE[1] - GO_RANGE[0]) / 2
    margins = [1 - m["adx14"] / GO_ADX, 1 - m["efficiency_ratio_30d"] / GO_ER,
               1 - abs(m["sma50_slope_20d"]) / GO_SLOPE, 1 - abs(m["range30_position"] - mid) / half]
    return round(sum(margins) / len(margins), 3)


def progress(i, n):
    print(f"\r[{'#' * (30 * i // n):<30}] {i}/{n}", end="\n" if i == n else "", file=sys.stderr, flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-volume", type=float, default=MIN_VOLUME, help="minimum 24h volume in USDC (default 2M)")
    a = ap.parse_args(argv)

    with ThreadPoolExecutor() as ex:
        info, tickers = ex.submit(get, "exchangeInfo", permissions="SPOT"), ex.submit(get, "ticker/24hr")
        btc = ex.submit(klines, "BTCUSDC", "1d", DAILY)
        pairs = candidates(info.result()["symbols"], tickers.result(), a.min_volume)
        btc = btc.result()

    ranked, no_go, skipped = [], [], []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(check_pair, s["symbol"], FEE, DAYS, CAPITAL, s, btc): (s["symbol"], v) for s, v in pairs}
        for i, f in enumerate(as_completed(futures), 1):
            pair, volume = futures[f]
            try:
                r = f.result()
            except Exception as e:  # short history, network: skip the pair, keep scanning
                skipped.append({"pair": pair, "reason": f"{type(e).__name__}: {e}"})
            else:
                if r["metrics"]["vol30_ann"] < STABLE_VOL:
                    skipped.append({"pair": pair, "reason": "stablecoin-like volatility"})
                elif r["verdict"] == "NO-GO":
                    no_go.append({"pair": pair, "reason": r["reason"]})
                else:
                    ranked.append({"pair": pair, "verdict": r["verdict"], "fit": fit(r["metrics"]),
                                   "volume_24h_usdc": round(volume),
                                   **{k: r[k] for k in ("price", "reason", "params", "worst_case_loss_pct", "warnings")}})
            progress(i, len(futures))
    ranked.sort(key=lambda r: (r["verdict"] != "GO", -r["fit"], -r["volume_24h_usdc"]))

    print(json.dumps({"min_volume_usdc": a.min_volume, "checked": len(pairs), "ranked": ranked,
                      "no_go": no_go, "skipped": skipped}, indent=2))
    for r in ranked:
        print(f"{r['pair']:<10} {r['verdict']:<7} fit {r['fit']:>5.2f}  vol {r['volume_24h_usdc'] / 1e6:>5.1f}M  {r['reason']}",
              file=sys.stderr)
    print(f"{len(ranked)} GO/CAUTION, {len(no_go)} NO-GO ({', '.join(n['pair'] for n in no_go)}), "
          f"{len(skipped)} skipped ({', '.join(s['pair'] for s in skipped)}) of {len(pairs)} pairs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
