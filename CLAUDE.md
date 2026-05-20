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
- `PUSHOVER_USER_KEY` — Pushover user key (for push notifications)
- `PUSHOVER_APP_TOKEN` — Pushover app token (create app at pushover.net/apps/build)

## File Map

| File | Purpose |
|------|---------|
| `config.py` | Loads secrets from `.env` / env vars; `_require()` raises on missing |
| `kalshi_client.py` | Signed API client (RSA-PSS); market/order/portfolio + `sell_order()` |
| `live_scanner.py` | Background scanner: tennis + baseball signals, Kelly sizing |
| `arb_scanner.py` | Arbitrage scan using The Odds API |
| `order_executor.py` | Entry + exit order logic, deduplication, file routing |
| `notifier.py` | Pushover push notifications (live trades only) |
| `dashboard.py` | Flask dashboard: all API routes + background thread |
| `runner.py` | CLI entry point for headless scanner |
| `models/tennis_model.py` | Markov chain tennis win probability |
| `models/baseball_model.py` | Baseball win probability model |
| `DEPLOY.md` | Full deployment guide (systemd, nginx, scp secrets) |

## Log Files (all gitignored via `*.jsonl`)

| File | Contents |
|------|---------|
| `orders_dry.jsonl` | Paper trading buy orders |
| `orders_live.jsonl` | Real buy orders |
| `exits_dry.jsonl` | Paper trading exits |
| `exits_live.jsonl` | Real exits |
| `perf_cache_dry.json` | Settlement resolution cache for paper orders |
| `perf_cache_live.json` | Settlement resolution cache for live orders |

## Order Executor Key Behaviours

- **One position per market** — `_open_tickers` set prevents re-buying same ticker every 30s. Rebuilt from log files on scanner start via `_load_open_tickers()`.
- **Min ask filter** — `MIN_ASK=0.05`: skips markets where YES < 5¢ (player nearly eliminated, model stale). Configurable in Settings.
- **Exit logic** — `_check_exits()` runs every scan cycle. Stop-loss: sell if bid drops `stop_loss_pct` below entry. Profit-take: sell if bid rises `profit_take_pct` above entry. Both configurable.
- **Notifications** — `notifier.notify_buy()` / `notifier.notify_sell()` called on real (non-dry-run) fills only. Stop-loss uses Pushover priority=1 (bypasses quiet hours).

## Critical Kalshi API Quirks

### Baseball `occurrence_datetime` is the settlement deadline, NOT game start
Kalshi sets `occurrence_datetime` to ~3 hours after first pitch (the settlement cutoff). **Never filter `occurrence_datetime < now` for baseball** — it will exclude all in-progress games. Use ESPN to confirm liveness instead.

Fixed in: `dashboard.py` (`_build_live_games`) and `live_scanner.py` (`baseball_signal`).

### `get_market()` must unwrap the response
`GET /markets/{ticker}` returns `{"market": {...}}`. The client calls `.get("market", {})` to unwrap. Without this, all field reads (`yes_ask_size_fp`, `yes_bid_dollars`, etc.) return None/0.

### `sell_order()` for exits
`kalshi_client.sell_order()` sends `action: "sell"` with `yes_price` set to current bid cents. Use `yes_bid_dollars * 100` rounded to get fill-ready price.

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

No auth required. `competitions[0].status.type.state == "in"` = live.

### MLB team name matching
Kalshi uses abbreviations (ATH, SD, CWS). ESPN uses full names. `MLB_ALIASES` dict in `live_scanner.py` maps all 30 teams. `find_baseball_game()` does substring match on expanded names.

## Dashboard Tabs

1. **Signals** — live scanner output, dry-run toggle
2. **Games** — Live Now (ESPN-confirmed) + Upcoming (next 48h from Kalshi)
3. **Positions** — current Kalshi portfolio positions
4. **Orders** — Paper / Live sub-tabs. Columns: entry price, current bid (from `_price_cache`, updated each scan), SL↓/PT↑ targets, Sell button (open orders only)
5. **Performance** — Paper / Live sub-tabs. Resolves orders against settled markets; edge bucket win-rate analysis; separate cache files per mode
6. **Log** — tail of scanner log
7. **Settings** — sliders for min_edge, min_ask, max_bet_usd, kelly_fraction, live_interval_sec, arb_interval_sec; Exit Rules (stop_loss_pct, profit_take_pct); Notifications (Pushover toggle + Send Test); Performance Logs (Archive/Clear per mode)

## Dashboard API Endpoints

| Endpoint | Notes |
|----------|-------|
| `GET /api/orders?mode=dry\|live` | Returns orders enriched with current_bid_cents, stop_loss_cents, profit_take_cents |
| `GET /api/performance?mode=dry\|live` | P&L summary + edge buckets + resolved list |
| `POST /api/sell` | `{ticker, side, contracts, entry_cents}` — executes exit at current bid |
| `POST /api/notifications/test` | Fires test Pushover notification |
| `POST /api/notifications/toggle` | `{enabled: bool}` |
| `POST /api/logs/archive` | `{mode: dry\|live\|all}` — renames files with UTC timestamp |
| `POST /api/logs/clear` | `{mode: dry\|live\|all}` — deletes files |

## Known Issues / TODO

- Duplicate baseball game cards in Live Now: ESPN substring matching can return multiple hits for common city/team names. Needs deduplication by canonical team pair.
- Odds API free tier is 500 req/month — warn if arb_interval_sec < 21600 (6h).
- Performance tab for live mode resolves against Kalshi settlement but doesn't yet account for partial fills or exit P&L from exits_live.jsonl.
