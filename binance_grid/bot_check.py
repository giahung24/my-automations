"""Check running Spot Grid bots against the stop/keep rules of the guide's step 4 (see SPEC.md).

Bots are listed in bots.json next to this script (local only: *.json is gitignored):

    [{"pair": "LINKUSDC", "lower": 8.47, "upper": 16.03, "stop_loss": 7.95,
      "sell_on_stop": false, "created": "2026-11-02T08:00:00Z", "days": 21}]

    python bot_check.py

Public market data only. Prints a JSON list and one summary line per bot on stderr.
"""
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from grid_check import check_pair, get

BOTS = Path(__file__).parent / "bots.json"
NO_GO_ADVICE = {
    "downtrend": "stop the bot and sell the coin back to USDC",
    "volatility": "stop, or recreate with the CAUTION setup",
}


def action(bot, price, low, elapsed_days, verdict, reason):
    """Step 4 rules, first match wins. Returns (action, why)."""
    if low <= bot["stop_loss"]:
        return "STOPPED", "price touched the stop-loss, the bot has stopped: do not recreate until grid_check says GO"
    if verdict == "NO-GO":
        advice = next((a for k, a in NO_GO_ADVICE.items() if reason.startswith(k)),
                      "stop the bot; trend down -> sell the coin to USDC, trend up -> leave it to the trend bot")
        return "STOP", f"regime NO-GO ({reason}): {advice}"
    if price > bot["upper"]:
        return "STOP", "price above the upper limit, the bot sold everything and idles: stop, rerun grid_check first"
    if elapsed_days >= bot["days"]:
        if verdict == "GO":
            return "KEEP", "planned duration reached, regime still GO: keep the params"
        return "STOP", f"planned duration reached, regime is {verdict}: stop"
    return "KEEP", f"regime {verdict}, no stop rule matched: keep the bot, do not change params"


def check_bot(bot):
    pair = bot["pair"]
    if not bot["stop_loss"] < bot["lower"] < bot["upper"]:
        raise ValueError("need stop_loss < lower < upper")
    created = pd.Timestamp(bot["created"])
    created = created.tz_localize("UTC") if created.tzinfo is None else created
    with ThreadPoolExecutor() as ex:
        regime = ex.submit(check_pair, pair, 0.001, bot["days"], 1000)
        # 1h candles since creation, the forming one included: a touch of the stop-loss counts
        candles = ex.submit(get, "klines", symbol=pair, interval="1h", startTime=int(created.timestamp() * 1000), limit=1000)
        ticker = ex.submit(get, "ticker/price", symbol=pair)
        regime, candles, price = regime.result(), candles.result(), float(ticker.result()["price"])
    low = min(float(c[3]) for c in candles)
    elapsed = (pd.Timestamp.now(tz="UTC") - created).total_seconds() / 86400
    act, why = action(bot, price, low, elapsed, regime["verdict"], regime["reason"])

    notes = []
    if not bot["sell_on_stop"]:
        notes.append("sell on stop is off: if the stop-loss fires the coin stays in the wallet, the loss is not capped")
    if price < bot["lower"]:
        notes.append("price below the range: the bot now holds only coin")
    if elapsed > 1000 / 24:
        notes.append("bot older than 1000 h: the stop-loss touch check only covers its first 1000 h")
    return {
        "pair": pair,
        "action": act,
        "why": why,
        "price": price,
        "range_position": round(math.log(price / bot["lower"]) / math.log(bot["upper"] / bot["lower"]), 4),
        "drop_to_stop_loss": round(bot["stop_loss"] / price - 1, 4),
        "low_since_created": low,
        "elapsed_days": round(elapsed, 1),
        "planned_days": bot["days"],
        "regime": {"verdict": regime["verdict"], "reason": regime["reason"]},
        "notes": notes + regime["warnings"],
    }


def main():
    if not BOTS.exists():
        sys.exit(f"{BOTS} not found, see the format in this script's docstring")
    bots = json.loads(BOTS.read_text())
    with ThreadPoolExecutor() as ex:
        futures = [(b.get("pair", "?"), ex.submit(check_bot, b)) for b in bots]
    results = []
    for pair, f in futures:
        try:
            results.append(f.result())
        except Exception as e:  # one bad entry must not hide the other bots
            results.append({"pair": pair, "error": f"{type(e).__name__}: {e}"})

    print(json.dumps(results, indent=2))
    for r in results:
        print(f"{r['pair']:<9} {r.get('action', 'ERROR'):<7} {r.get('why') or r['error']}", file=sys.stderr)
    return 1 if any("error" in r for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
