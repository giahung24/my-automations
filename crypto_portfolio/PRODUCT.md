# Product
<!-- impeccable:product-schema 1 -->

> Written without an interview: this session had no way to ask questions. Facts marked
> *(inferred)* come from the build brief and the existing code, not from a confirmed answer.

## Platform
web

## Stack
FastAPI backend (`web_api.py`, launched by `main.py serve`) over the existing Python services;
mobile-first PWA in `static/` built with Vue 3, Tailwind CSS (browser build) and TradingView
Lightweight Charts, all vendored under `static/vendor/` so the app needs no CDN. Chosen in the
brief.

## Users
One person tracking their own crypto holdings *(inferred)*: checks net worth and the day's
move from a phone, often several times a day, and occasionally edits API keys and wallet
addresses. Also uses the CLI (`show`, `record`, cron) on the machine running the server.

## Product Purpose
One number for everything held across Binance (Spot, Simple Earn, Funding) and self-custody
wallets on Ethereum, BSC, Arbitrum, Base, Polygon and Solana, with a history built from
SQLite snapshots. Success: the total and the 24h move are readable within a second of opening
the app, and every figure traces back to a source.

## Positioning
Self-hosted and read-only by construction: the Binance key needs "Enable Reading" only, wallets
are public addresses, pricing is keyless (Binance ticker, DefiLlama). No account, no third-party
service holds the keys or the balances.

## Operating Context
- Runs on a personal machine or home server; the phone reaches it over the LAN *(inferred)*.
- History comes from `main.py record` in cron plus every refresh from the app.
- Secrets may live in `.env`/environment, which override `config.yaml` - the UI must say so.

## Capabilities and Constraints
- Sources: Binance Spot, Simple Earn (flexible + locked), Funding; EVM native coins plus
  configured ERC-20s; Solana SOL plus SPL tokens.
- Dust (default under $1) is hidden from lists but always counted in totals and history.
- Partial failures are normal (public RPCs drop); the product reports them instead of failing.
- 24h change is snapshot-based: deposits and withdrawals show up as change *(inferred
  trade-off; price-based P&L is undecided)*.
- An access token (`PORTFOLIO_WEB_TOKEN`) guards the API; it is mandatory when served beyond localhost.

## Brand Commitments
Pinned by the brief: CoinStats-like layout, deep dark background `#0B0E14`, card surfaces
`#161B22`, neon green/red for gains and losses, privacy toggle that blurs balances.

## Evidence on Hand
No real portfolio data in the repo; the app must render sensible empty and first-run states.
Coin logos: CC0 set from spothq/cryptocurrency-icons in `static/coins/` (partial coverage).

## Product Principles
1. The total is never wrong silently: failures, dust and stale data are stated where the number is.
2. Read-only, local-first: nothing leaves the machine except balance and price queries.
3. Glanceable first, inspectable second: hero number, then chart, then per-asset detail.
4. Settings never echo secrets back; stored credentials are shown masked.

## Accessibility & Inclusion
Touch targets at least 44px, WCAG AA contrast on the dark surfaces, gains and losses carry a
sign and an arrow as well as a colour *(inferred)*.
