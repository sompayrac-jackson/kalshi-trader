# Kalshi Trader — Project Context for Claude Code

## What This Is

Automated prediction market trading bot targeting Kalshi sports markets (tennis ATP/WTA, MLB baseball). Runs a live signal scanner + Flask web dashboard hosted on a Digital Ocean droplet.

## Infrastructure

| Item | Value |
|------|-------|
| Droplet IP | `104.248.62.36` |
| Droplet user | `root` |
| App directory | `/opt/kalshi_trader` |
| systemd service | `kalshi-dashboard.service` |
| Dashboard port | `5000` (nginx proxies from `80`) |
| GitHub repo | `https://github.com/sompayrac-jackson/kalshi-trader.git` |

**Deploy flow:**
```bash
git push origin master
ssh root@104.248.62.36 "cd /opt/kalshi_trader && git pull && systemctl restart kalshi-dashboard"
```

## Secrets (never commit, live in `.env` on each machine)

- `.env` is gitignored — copy `.env.example` to `.env` and fill in values
- `private_key.pem` is gitignored — upload via `scp` only
- Loaded by `config.py` (custom dotenv parser, no external deps)

Keys used:
- `KALSHI_API_KEY` — Kalshi RSA key ID
- `ODDS_API_KEY` — The Odds API (free tier: **500 req/month** — keep `arb_interval_sec` at 6h+ or 21600+)
- `PRIVATE_KEY_PATH` — path to RSA private key PEM (default: `private_key.pem`)
- `DASHBOARD_USER` / `DASHBOARD_PASS` — HTTP Basic Auth for the web dashboard

## File Map

| File | Purpose |
|------|---------|
| `config.py` | Loads secrets from `.env` / env vars; `_require()` raises on missing |
| `kalshi_client.py` | Signed API client (RSA-PSS); all market/order/portfolio methods |
| `live_scanner.py` | Background scanner: tennis + baseball signals, Kelly sizing, order execution |
| `arb_scanner.py` | Arbitrage scan using The Odds API |
| `dashboard.py` | Flask dashboard (~1400 lines): all API routes + background thread |
| `runner.py` | CLI entry point for headless scanner |
| `order_executor.py` | Order placement logic |
| `models/tennis_model.py` | Markov chain tennis win probability |
| `models/baseball_model.py` | Baseball win probability model |
| `DEPLOY.md` | Full deployment guide (systemd, nginx, scp secrets) |

## Critical Kalshi API Quirks

### Baseball `occurrence_datetime` is the settlement deadline, NOT game start
Kalshi sets `occurrence_datetime` to ~3 hours after first pitch (the settlement cutoff). **Never filter `occurrence_datetime < now` for baseball** — it will exclude all in-progress games. Use ESPN to confirm liveness instead.

Fixed in: `dashboard.py` (`_build_live_games`) and `live_scanner.py` (`baseball_signal`).

### `get_market()` must unwrap the response
`GET /markets/{ticker}` returns `{"market": {...}}`. The client calls `.get("market", {})` to unwrap. Without this, all field reads (yes_ask_size_fp, etc.) return None/0 and every order is skipped for "insufficient liquidity."

### Kalshi series tickers
- `KXATPMATCH` — ATP tennis
- `KXWTAMATCH` — WTA tennis
- `KXMLBGAME` — MLB baseball

### Auth
RSA-PSS signing. Message = `f"{timestamp_ms}{METHOD}{/trade-api/v2/path}"`. Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`.

## ESPN API

Base: `https://site.api.espn.com/apis/site/v2/sports`
- Tennis: `/tennis/atp/scoreboard`, `/tennis/wta/scoreboard`
- Baseball: `/baseball/mlb/scoreboard`

No auth required. Status codes in `competitions[0].status.type.name`: `STATUS_IN_PROGRESS` = live.

### MLB team name matching
Kalshi uses abbreviations (ATH, SD, CWS). ESPN uses full names. `MLB_ALIASES` dict in `live_scanner.py` maps all 30 teams. `find_baseball_game()` does substring match on expanded names.

## Dashboard Tabs

1. **Signals** — live scanner output, dry-run toggle
2. **Games** — Live Now (ESPN-confirmed in-progress) + Upcoming (next 48h from Kalshi)
3. **Positions** — current Kalshi portfolio positions
4. **Orders** — order history from `orders.jsonl`
5. **Performance** — dry-run P&L tracker; resolves orders against settled markets; edge bucket win-rate analysis; cached in `perf_cache.json`
6. **Log** — tail of scanner log
7. **Settings** — sliders for min_edge, max_bet_usd, kelly_fraction, live_interval_sec, arb_interval_sec (1h–24h; note free-tier Odds API limit)

## Known Issues / TODO

- Duplicate baseball game cards in Live Now: ESPN substring matching can return multiple hits if a team name appears in more than one active game context (e.g., "Philadelphia" matches both CLE/PHI and CIN/PHI). Tighten matching logic to deduplicate by canonical team pair.
- Odds API free tier is 500 req/month — warn user if arb_interval_sec is set below 21600 (6h).
