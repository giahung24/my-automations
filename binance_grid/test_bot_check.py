import pytest

from bot_check import action

BOT = {"lower": 8.47, "upper": 16.03, "stop_loss": 7.95, "days": 21}


@pytest.mark.parametrize("price, low, elapsed, verdict, reason, expected, why", [
    (11.0, 10.0, 1, "CAUTION", "not met: ADX<22", "KEEP", "regime CAUTION"),
    (11.0, 7.95, 1, "GO", "ok", "STOPPED", "price touched"),  # stop-loss wins over everything
    (11.0, 10.0, 1, "NO-GO", "downtrend: x", "STOP", "regime NO-GO (downtrend: x): stop the bot and sell"),
    (11.0, 10.0, 1, "NO-GO", "volatility spike: x", "STOP", "regime NO-GO (volatility spike: x): stop, or"),
    (11.0, 10.0, 1, "NO-GO", "strong trend: x", "STOP", "regime NO-GO (strong trend: x): stop the bot; trend"),
    (16.5, 10.0, 1, "GO", "ok", "STOP", "price above"),
    (11.0, 10.0, 21, "GO", "ok", "KEEP", "planned duration reached, regime still GO"),
    (11.0, 10.0, 21, "CAUTION", "x", "STOP", "planned duration reached, regime is CAUTION"),
])
def test_action(price, low, elapsed, verdict, reason, expected, why):
    act, text = action(BOT, price, low, elapsed, verdict, reason)
    assert act == expected and text.startswith(why)
