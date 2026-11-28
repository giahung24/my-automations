# Home-screen widget with Widgy

A widget that shows how the portfolio is doing **without showing how much it is**: every
figure is a percentage. The data comes from the public gateway's `/widget` route.

## 1. Switch the route on (once, on the server)

Add your net-worth goal to `.env.gateway` and restart the gateway:

```
GATEWAY_WIDGET_TARGET_USD=150000
```

```bash
systemctl --user restart crypto-api-gateway
python api_gateway.py show-key          # the key Widgy needs
curl -s -H "X-API-Key: <key>" https://api.tytony.link/v1/crypto/portfolio/widget
```

The target stays on the server; only `progress_pct` (portfolio ÷ target) is sent. Anyone who
knows the target can work the portfolio back out of that percentage: pick one you do not share.
Without the variable the route does not exist (same `404` as any unknown path).

## 2. What it answers

```json
{
  "progress_pct": 36.57,
  "change_24h_pct": -0.88, "change_7d_pct": 2.1, "change_30d_pct": 5.4,
  "btc_30d_pct": 4.0, "vs_btc_30d_pp": 1.4,
  "drawdown_pct": -8.02,
  "health": "ok", "health_reasons": [],
  "as_of": "2026-11-28T09:00:05Z", "age_minutes": 12,
  "text": {
    "progress": "37%",
    "change_24h": "▼ 0.88%", "change_7d": "▲ 2.10%", "change_30d": "▲ 5.40%",
    "btc_30d": "▲ 4.00%", "vs_btc_30d": "+1.4 pp vs BTC",
    "drawdown": "−8.0% from peak",
    "health": "OK"
  }
}
```

| Field | Meaning |
|---|---|
| `progress_pct` | Portfolio as a share of the target. Can pass 100. |
| `change_*_pct` | Change **net of deposits and withdrawals**: money you add is not a gain. 7d and 30d are the dashboard's time-weighted return; `null` until the history reaches that far back. |
| `btc_30d_pct`, `vs_btc_30d_pp` | What BTC did over the same 30 days, and your lead over it in percentage points. |
| `drawdown_pct` | Distance below the highest point of the time-weighted index (0 at the peak). |
| `health` | `ok` or `warn`. Reasons: `stale` (no snapshot for 130 minutes: the cron job has probably stopped), `source_failed`, `peg` (a stablecoin more than 0.3% off), `backup` (last backup failed or is over 48 h old). |
| `age_minutes`, `as_of` | Age of the newest snapshot when the request was answered. |
| `text.*` | The same figures already formatted, with an arrow as well as a sign, for a plain text layer. `—` where the figure is `null`. |

## 3. Build it in Widgy

Menu names differ a little between Widgy versions; the steps are the same.

1. Create a widget (small or medium) and add a **JSON / API data source**:
   - URL: `https://api.tytony.link/v1/crypto/portfolio/widget`
   - Header: name `X-API-Key`, value the key from step 1
2. Add layers and bind each to a JSON path:

   | Layer | JSON path | Suggestion |
   |---|---|---|
   | Ring or bar gauge | `progress_pct` (range 0–100) | The centrepiece |
   | Large text | `text.progress` | Inside the ring |
   | Three small texts | `text.change_24h`, `text.change_7d`, `text.change_30d` | Labelled 24h · 7d · 30d |
   | Small text | `text.vs_btc_30d` | Medium size only |
   | Small text | `text.drawdown` | Medium size only |
   | Dot or small text | `health` / `text.health` | Green when `ok`, amber when `warn` |
   | Caption | `age_minutes` | "12 min ago" |

3. Colour: bind a text layer's colour to the sign of the matching `change_*_pct` number if your
   Widgy version has conditional colours; the arrow in `text.*` carries the direction either way.

```
┌──────────────────────────────┐
│   ◯ 37%        24h  ▼ 0.88%  │
│  of target      7d  ▲ 2.10%  │
│                30d  ▲ 5.40%  │
│  +1.4 pp vs BTC · −8% peak ● │
└──────────────────────────────┘
```

## 4. Good to know

- iOS decides when a widget refreshes, typically every 15 to 60 minutes. `age_minutes` is the age
  at the last refresh, not right now.
- The gateway allows 30 requests a minute per client: far more than a widget makes.
- Lost the phone, or the key leaked? Put a new `GATEWAY_API_KEY` in `.env.gateway` and restart.
  The key reads `/total` (amounts in USD) as well as `/widget`.
