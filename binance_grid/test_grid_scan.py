from grid_scan import candidates, fit


def sym(base, status="TRADING", quote="USDC"):
    return {"symbol": base + quote, "baseAsset": base, "quoteAsset": quote, "status": status}


def test_candidates_filters_and_sorts():
    symbols = [sym("BTC"), sym("BTCUP"), sym("JUP"), sym("FDUSD"), sym("ETH"),
               sym("SOL", status="BREAK"), sym("XRP", quote="USDT"), sym("DOGE")]
    volumes = [9e6, 9e6, 3e6, 9e6, 5e6, 9e6, 9e6, 1e6]
    tickers = [{"symbol": s["symbol"], "quoteVolume": str(v)} for s, v in zip(symbols, volumes)]
    # BTCUP leveraged, FDUSD stable, SOL not trading, XRP not USDC, DOGE under the volume floor
    assert [s["symbol"] for s, _ in candidates(symbols, tickers, 2e6)] == ["BTCUSDC", "ETHUSDC", "JUPUSDC"]


def test_fit():
    ideal = {"adx14": 0, "efficiency_ratio_30d": 0, "sma50_slope_20d": 0, "range30_position": 0.5}
    edge = {"adx14": 22, "efficiency_ratio_30d": 0.35, "sma50_slope_20d": -0.05, "range30_position": 0.25}
    assert fit(ideal) == 1 and fit(edge) == 0
