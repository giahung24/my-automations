# Crypto Portfolio

Self-hosted crypto net-worth tracker: Binance (Spot, Simple Earn, Funding), on-chain wallets
across Ethereum, BSC, Arbitrum, Base, Polygon and Solana, and assets declared by hand (cold
wallets, external staking). One number, its history, an explanation of every move, and an
allocation assistant - from a CLI, a mobile PWA and a one-route public gateway.

**Read-only by construction.** The Binance key needs *Enable Reading* and nothing else; the
client only issues balance queries. On-chain, only public addresses are used - no private key
or seed phrase is ever read, so the worst a leaked `config.yaml` reveals is which addresses
you watch. Pricing is free and keyless: the Binance ticker for CEX assets, DefiLlama for
on-chain ones.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml     # then fill in keys and addresses
```

Secrets can stay out of the YAML - `BINANCE_API_KEY` / `BINANCE_API_SECRET` in the
environment or in a `.env` (see `.env.example`) override it. Both files are git-ignored.

## Usage

```bash
.venv/bin/python main.py show                    # formatted table
.venv/bin/python main.py show --json             # JSON for bots and agents
.venv/bin/python main.py show --text-charts      # table + ASCII charts
.venv/bin/python main.py record                  # silent snapshot into SQLite (for cron)
.venv/bin/python main.py chart --type all        # PNGs into ./charts
.venv/bin/python main.py chart --from-db --type text    # ASCII report, no network
.venv/bin/python main.py history --days 30       # recorded snapshots
.venv/bin/python main.py backfill               # 60 monthly checkpoints 2021-11..2026-10 (see below)
.venv/bin/python main.py pnl                     # profit and loss net of deposits/withdrawals
.venv/bin/python main.py serve --port 8080       # mobile web dashboard (see below)
```

`--min-value <float>` sets the dust filter (default `$1.00`); `--min-value 0` shows
everything. It works on either side of the subcommand. Dust is hidden from the table and the
charts but **always counted in the total and always stored** - filtering is a display choice
and must not make the history lie.

Build the time series from cron:

```cron
0 * * * * /path/to/crypto_portfolio/.venv/bin/python /path/to/crypto_portfolio/main.py record
```

No `cd` needed: relative `db_path` and `charts_dir` resolve against the project directory,
not the shell's working directory.

After each snapshot `record` also books changes of manual assets as cash flows and syncs the
Binance deposit / withdrawal / fiat / P2P / Earn-reward history, which is what lets the
analytics tell profit from money moved.

`record` prints nothing on success. Partial failures go to stderr (so cron mails them) and
the snapshot is still written; if *every* source fails it refuses to record rather than
store a fake `$0`.

### Exit codes

| code | meaning |
|------|---------|
| 0 | success |
| 1 | nothing configured, config error, or every source failed |
| 2 | partial success - some source failed, output is incomplete |

## Web dashboard

```bash
.venv/bin/python main.py serve --port 8080                     # this machine only
PORTFOLIO_WEB_TOKEN=<long-random> .venv/bin/python main.py serve --host 0.0.0.0 --port 8080  # needs the token
.venv/bin/python -m uvicorn web_api:app --port 8080            # same app, plain uvicorn
```

A mobile-first PWA (Vue 3, Tailwind, TradingView Lightweight Charts, all vendored in
`static/vendor/`, so there are no CDN calls) served by a FastAPI backend that reuses the CLI's
clients, aggregation and SQLite history. Three tabs:

- **Portfolio** - net worth with a privacy toggle that blurs every balance, the 24h change, a
  touch-scrubbable history chart (24H / 7D / 1M / 1Y / 5Y / ALL), a "Why it moved" card that
  splits the move into price, quantity and data-gap effects, and the per-asset list under
  All / Binance / Personal. Personal is everything off the exchange (on-chain wallets, cold
  wallets, staking); Binance and Personal have a Coins | Wallets switch, and balances under
  the dust filter fold into one line.
- **Analytics** - time-weighted return, base-100 comparison with BTC and ETH, drawdown and
  volatility, and the allocation assistant (drift against your targets and the orders that
  would close it; off until enabled).
  The Earn card's "listed" rate is what a Simple Earn position earns overall: Binance's base
  rate (`latestAnnualPercentageRate`) plus each tier bonus (`tierAnnualPercentageRate`, bands
  such as `{"0-5000USDC": "0.03"}`) pro rata of the balance inside its band. 25,949 USDC at
  2.02% + 3% on the first 5,000 lists 2.60%, not 5.02%; a balance under 5,000 lists 5.02%.
  Hover the rate for the breakdown. A position with no tier lists the base rate.
- **Activity** - deposits, withdrawals, internal transfers and Earn rewards as a timeline,
  with a queue of the items that need your confirmation.

The settings sheet edits the Binance key (with a permissions check), the wallet list (with a
per-chain balance check) and the manual assets, saved to `config.yaml`.

On iOS, open it in Safari and use Share > Add to Home Screen.

**Access.** `serve` binds `127.0.0.1` unless told otherwise, and refuses any other address
until `PORTFOLIO_WEB_TOKEN` (or `settings.web_token`) is set - without a token, whoever
reaches the port can read balances and rewrite the config. With a token, every `/api` route
needs it: type it on the unlock screen once per device (it is traded for a signed, HttpOnly,
SameSite=Strict session cookie that lasts 90 days and never contains the token), or send it
as `Authorization: Bearer`. Changing the token signs every device out. Wrong credentials are
rate limited (20 per 5 minutes per client, then `429`).

**Recommended setup: systemd on a private network.** Run the dashboard as a user service bound
to `0.0.0.0`, reachable from the home LAN and over Tailscale, token required on both:

```bash
cp deploy/crypto-web.service ~/.config/systemd/user/     # adjust the paths inside first
systemctl --user daemon-reload && systemctl --user enable --now crypto-web
loginctl enable-linger "$USER"                            # once: keep it running without a login
```

This is plain HTTP, acceptable only because the LAN and the tailnet are private networks. The
PWA's service worker needs HTTPS, so there is no offline shell in this setup. Opening the
dashboard to the internet is a separate, parked design: [`docs/AUTH_SPEC.md`](docs/AUTH_SPEC.md).

**Data freshness.** `GET /api/portfolio` never touches the network: it serves the last live
refresh or the newest snapshot. The app refreshes in the background when the data is more
than 10 minutes old, and the refresh button forces it. Every refresh records a snapshot, as
`record` does. The 24h change compares against the snapshot closest to 24 hours ago, so
deposits and withdrawals count as change (the PnL figures and the analytics net them out), and with a shorter history it labels the real
window (for example "past 6h").

**Saving settings** rewrites `config.yaml` (mode 0600) and keeps the previous file as
`config.yaml.bak`. YAML comments do not survive the rewrite. Other sections (`chains`,
`settings`, per-wallet `tokens`) are preserved. Stored secrets are never sent back to the
browser, only masked (`****9f3a`). Values set through the environment or `.env`
(`BINANCE_API_KEY`, `EVM_WALLETS`, ...) still win over the file, and the settings sheet says
so.

The API is listed in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#35-dashboard--web_apipy-static)
and, interactively, at `/api/docs`. Every `GET` is served from SQLite; only
`POST /api/portfolio/refresh` and the two settings checks reach Binance or an RPC.

## Public gateway

`api_gateway.py` is a separate, read-only process for one consumer-facing number:
`GET /v1/crypto/portfolio/total?currency=USD` with an `X-API-Key` header. It imports nothing
from the dashboard, never loads Binance credentials, opens SQLite read-only and binds loopback
only (meant to sit behind a Cloudflare Tunnel). Anything but an authorised request gets the
same `404`. Unit: `deploy/crypto-api-gateway.service`; the key is generated on first start
(`python api_gateway.py show-key`).

## Output

`chart` writes three dark-themed PNGs:

- `history_trend.png` - net worth over time as a stacked area by source (parts sum to the
  total, baseline zero). With no snapshots it renders an empty state; with one, or several
  within the same second, a hero figure - a one-point line chart is not a chart.
- `token_allocation.png` - donut of the top assets by share of USD, tail folded into
  "Others". Capped at six segments, because part-to-whole stops being readable past that.
- `source_distribution.png` - horizontal bar per source. A bar rather than a donut: these
  values are compared, not apportioned.

Colours come from a categorical palette validated for colour-blind separation and contrast
against the dark surface. Hues are bound to the entity, not to its rank, so sorting never
repaints a series, and identity is never carried by colour alone (every chart is also
labelled). "Others" takes neutral grey - it is not an entity.

## Layout

| path | role |
|------|------|
| `main.py` | CLI: `show`, `record`, `chart`, `history`, `backfill`, `pnl`, `serve` |
| `config.py`, `models.py` | defaults -> `config.yaml` -> environment; `Holding`, `AssetRow`, `Portfolio` |
| `clients/` | signed read-only Binance client; EVM + Solana RPC with DefiLlama pricing |
| `services/` | aggregation, SQLite, manual assets, cash flows, activity timeline, backfill, charts |
| `services/analytics/` | attribution, PnL, quant (TWR, drawdown, volatility), allocation |
| `web_api.py`, `static/` | dashboard: JSON API + PWA |
| `api_gateway.py` | public read-only gateway |
| `deploy/` | systemd user units for the dashboard and the gateway |
| `tests/` | the whole suite, fully mocked - no network, no keys |
| `scripts/archive/` | one-off scripts that have already run, kept for reference |
| `docs/` | [`ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`SPEC.md`](docs/SPEC.md), [`LEDGER.md`](docs/LEDGER.md), [`AUTH_SPEC.md`](docs/AUTH_SPEC.md) |

What comes next: [`ROADMAP.md`](ROADMAP.md).

### Storage

`portfolio_history.db` is created on first use, in WAL mode and with mode 0600. `snapshots`
holds one row per run, `snapshot_assets` one row per holding, `snapshot_sources` the status of
each source, `cash_flows` every deposit, withdrawal, reward and transfer. The schema is
versioned (`PRAGMA user_version`): upgrades run on open, in one transaction, after a backup
copy of the file.

### Backfilled history

`main.py backfill` (or `python -m services.backfill --db <file>`) adds 60 monthly checkpoints,
one on the 1st of each month from 2021-11 to 2026-10, so the 1Y / 5Y / ALL charts are not
empty. Each one is **today's holdings at that month's prices**: the open of Binance's public
monthly `<ASSET>USDT` kline (no API key) for BTC, ETH and SOL; USDT and the other USD
stablecoins at 1.0; wrapped tokens follow their underlying. Assets without a price series move
like the priced part of the portfolio (`price_id = backfill:proxy`); add them with
`--symbols BNB LINK ...` to price them for real. The rows carry `snapshots.is_backfilled = 1`
and `trigger = 'backfill'`: they are drawn on the chart but never used as the latest snapshot,
as a 24h-change reference, or by the hole heuristic. Re-running replaces them; months that
already have fetched snapshots are skipped. `--dry-run` prints the series without writing.

## Failure behaviour

Every source degrades on its own. A dead RPC, a geo-blocked host, an expired key or a
DefiLlama timeout is reported on stderr and leaves the other sources intact.

- **Binance** - host failover (`api.binance.com` → mirrors; public market data via
  `data-api.binance.vision`, which answers from IPs that get `451` elsewhere), backoff on
  `429`/`418`/`5xx`, and an automatic clock re-sync on error `-1021`.
- **EVM / Solana** - several public RPCs per chain, tried in order. A node answering
  `200`-with-error (`-32603`, rate limits) fails over to the next endpoint rather than
  aborting. Public endpoints rot - point `chains.<name>.rpcs` at your own Alchemy/Infura URL
  for anything serious.
- **Token metadata** - an ERC-20 whose `decimals()` cannot be read is **skipped**, not
  guessed. Assuming 18 decimals for a 6-decimal token like USDC would overstate the balance
  by a factor of 10¹².
- **Unpriced assets** - kept at `$0` with the quantity intact, never priced by guesswork.

## Tests

```bash
.venv/bin/python -m pytest -q                  # everything under tests/
```

All HTTP is mocked, so the suite runs offline in a few seconds.

## Notes

- YAML 1.1 parses an *unquoted* `0xdac17f95…` as an integer, since every EVM address is a
  valid hex literal. The config loader recovers the canonical 40-hex-digit address, but
  quoting them is still clearer.
- The same symbol arriving from several sources (USDC on Ethereum and on Solana) merges into
  one row; the price shown is the value-weighted average over the *priced* quantity, so
  Price × Qty reconciles with Value.
- `MATIC` was renamed `POL` in September 2024; Polygon's native coin is reported as `POL`.
