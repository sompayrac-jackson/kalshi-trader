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
| `live_scanner.py` | Background scanner: tennis + baseball signals; three-signal baseball model (Markov + ESPN win prob + DK Vegas odds); Kelly sizing |
| `arb_scanner.py` | Arbitrage scan using The Odds API |
| `order_executor.py` | Entry + exit order logic, deduplication, strategy filters, SL cooldown, file routing |
| `notifier.py` | Pushover push notifications (live trades only) |
| `dashboard.py` | Flask dashboard: all API routes + background thread |
| `runner.py` | CLI entry point for headless scanner |
| `models/tennis_model.py` | Markov chain tennis win probability |
| `models/baseball_model.py` | Baseball win probability via Markov chain over 24 base-out states |
| `models/serve_stats.py` | Per-player serve win % from Jeff Sackmann ATP/WTA CSVs; fuzzy name matching |
| `deepdive5.py`, `deepdive6.py` | Post-session analysis scripts (run locally against JSONL logs) |
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
- **One position per game** — `_open_events` set blocks buying both sides of the same game (event_ticker deduplication).
- **Min ask filter** — `MIN_ASK=0.05`: skips markets where YES < 5¢ (player nearly eliminated, model stale). Configurable in Settings.
- **Max entry price** — `MAX_ENTRY_PRICE=0.65`: skips YES entries above 65¢. Above this level, MLB order books gap on decisive turns — stop-loss slippage averages 50%+ vs the 35% trigger price rather than the intended 35%.
- **Set filter (tennis)** — `MIN_TENNIS_SET=3` in `live_scanner.py`: skips Set 1 and Set 2 entries. Historical WR: Set 1 = 36% (52 entries), Set 2 = 41.7% — Markov model has too little differentiated information at that stage.
- **Baseball strategy filters** — Applied in `execute_live()` before any order:
  - `REQUIRE_ESPN_OR_VEGAS=True`: skips Markov-only baseball signals (blocks entries when both ESPN win prob and Vegas odds are unavailable)
  - `SKIP_TWO_OUTS=True`: skips entries with 2 outs — historical WR 27%
  - `SKIP_LATE_SLIM_LEAD=True`: skips inning 6+ with exactly +1 run lead — historical WR 14–18%
- **Exit logic** — Disabled by default (`EXITS_ENABLED=False`); bot holds to Kalshi settlement. When enabled: stop-loss fires if bid drops `stop_loss_pct` below entry; profit-take fires if bid rises `profit_take_pct` above entry.
- **SL cooldown** — After any stop-loss exit, re-entry on the same ticker is blocked for `SL_COOLDOWN_SEC=3600` (60 min). Cooldown is persisted in `exits_*.jsonl` and reconstructed on restart via `_load_open_tickers()`, so service restarts don't clear it.
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
- Baseball summary: `https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/summary?event={game_id}`

No auth required. No rate limit. `competitions[0].status.type.state == "in"` = live.

### Baseball summary endpoint

`game_id` comes from `competitions[].id` in the scoreboard response. Called once per live game per scan cycle in `fetch_espn_baseball_summaries()`. Returns:

- **ESPN win probability** — `winprobability[-1].homeWinPercentage` (0.0–1.0, updated per play by ESPN's model)
- **DraftKings live moneyline** — `pickcenter[provider.id=="100"].moneyline.home.live.odds` (American format — **eliminates need for Odds API for baseball live odds**)
- **DraftKings opening line** — `.moneyline.home.open.odds` (pre-game baseline; logged but not used in consensus)
- **Base-out situation** — `situation.outs`, `situation.onFirst/onSecond/onThird`
- **Inning scoring probability** — `situation.situationNotes` ("Chance of scoring 1+ runs this inning: XX.XX%")

These feed `_consensus_prob()`: **Vegas 50%, ESPN 30%, Markov 20%** when all three are available. Falls back gracefully: Vegas-only → 70/30 blend; ESPN-only → 60/40 blend; Markov-only → Markov alone (and `REQUIRE_ESPN_OR_VEGAS=True` blocks the trade).

### MLB team name matching
Kalshi uses abbreviations (ATH, SD, CWS). ESPN uses full names. `MLB_ALIASES` dict in `live_scanner.py` maps all 30 teams. `find_baseball_game()` does substring match on expanded names.

## Dashboard Tabs

1. **Signals** — live scanner output, dry-run toggle
2. **Games** — Live Now (ESPN-confirmed) + Upcoming (next 48h from Kalshi)
3. **Positions** — current Kalshi portfolio positions
4. **Orders** — Paper / Live sub-tabs. Columns: entry price, current bid (from `_price_cache`, updated each scan), SL↓/PT↑ targets, price history sparkline, Sell button (open orders only)
5. **Performance** — Paper / Live sub-tabs. Resolves orders against settled markets; edge bucket win-rate analysis; model calibration chart; separate cache files per mode
6. **Analysis** — Signal log analytics: summary stats, by-sport, skip reasons, confidence distribution, entry timing by inning/set, missed trades, full signal table (most recent 500)
7. **Log** — tail of scanner log
8. **Settings** — sliders for min_edge, min_ask, min_model_prob, max_bet_usd, kelly_fraction, live_interval_sec, arb_interval_sec; Exit Rules (stop_loss_pct, profit_take_pct); Double Down settings; Notifications (Pushover toggle + Send Test); Performance Logs (Archive/Clear per mode)

## Dashboard API Endpoints

| Endpoint | Notes |
|----------|-------|
| `GET /api/orders?mode=dry\|live` | Returns orders enriched with current_bid_cents, stop_loss_cents, profit_take_cents |
| `GET /api/performance?mode=dry\|live` | P&L summary + edge buckets + model calibration + resolved list |
| `GET /api/analysis?mode=dry\|live` | Signal log analytics: summary, by_sport, skip_reasons, conf_buckets, timing, missed_trades, signals (last 500) |
| `GET /api/price_history?ticker=...` | Bid price history for open positions (omit ticker for grouped sparkline summary) |
| `POST /api/sell` | `{ticker, side, contracts, entry_cents}` — executes exit at current bid |
| `POST /api/notifications/test` | Fires test Pushover notification |
| `POST /api/notifications/toggle` | `{enabled: bool}` |
| `POST /api/logs/archive` | `{mode: dry\|live\|all}` — renames files with UTC timestamp |
| `POST /api/logs/clear` | `{mode: dry\|live\|all}` — deletes files |

## Log Files (extended)

| File | Contents |
|------|---------|
| `signals_dry.jsonl` | All evaluated signals per scan cycle (dry mode) — full context: model_prob, edge, score_state, exec_status |
| `signals_live.jsonl` | Same for live mode |
| `price_history.jsonl` | Bid price snapshots for open live positions, written every scan cycle |

## Analytics — How to Run a Model Review Session

When returning for analysis, paste the **Analysis tab (live mode)** and **Performance tab calibration** into the conversation. The dashboard endpoints return everything needed without SSH access.

### Key questions to investigate each session:

**1. Calibration refinement**
Check `GET /api/performance?mode=live` → `calibration` field. Compare `avg_model_prob` vs `actual_win_rate` per bucket. Goal: find the confidence floor where actual win rate consistently exceeds 50%. Adjust `min_model_prob` in Settings accordingly.

**2. Entry timing**
Check `GET /api/analysis?mode=live` → `timing` field. Look for innings/sets where win rate is below 50% with enough sample (n ≥ 5). If early game (innings 1–3, set 1) consistently underperforms, consider adding a `min_inning` filter to `live_scanner.py` baseball signal, or requiring higher `min_model_prob` early in games.

**3. Exit threshold calibration**
Pull price history sparklines for closed positions (settled or exited). Ask: did any winning trades temporarily dip to stop-loss before recovering? If yes → stop is too tight. Did stopped-out trades keep falling after exit? If yes → stop is appropriately set. Tune `stop_loss_pct` and `profit_take_pct` in Settings.

**4. Missed trade opportunity cost**
Check `GET /api/analysis?mode=live` → `missed_trades`. Focus on newly settled tickers. If liquidity-blocked trades (need N, available M) settled YES → real lost opportunities; consider relaxing contract count or accepting partial fills. If min-bet-blocked trades settled YES → Kelly sizing is too conservative at those price levels.

**5. Skip reason volume check**
Check `skip_reasons` in analysis. After enabling `min_model_prob`, confirm signal count at that threshold. If filtering >80% of buy signals → threshold too high. Target: filtering 30–60% of buy signals while keeping the high-confidence ones.

### Next model improvement hypotheses (to test as data accumulates):
- **Early vs late game confidence requirement** — require higher model_prob in innings 1–3 vs 7–9; the model has more variance early when the score hasn't differentiated
- **Score state integration** — model currently treats "Top 3, 1-0" same as "Top 3, 4-0"; the run differential should shift probability more aggressively in mid-game
- **Tennis three-signal model** — ESPN doesn't offer live win prob for tennis; no free Vegas source identified. Elo-adjusted serve priors (`serve_win_prob_from_elo()` in `tennis_model.py`) are implemented but not yet wired in as the serve stats fallback

## Known Issues / TODO

### Fixed — no longer apply
- Future game ticker contamination: date filter in `scan_live()` limits baseball to today/yesterday + 3.5h deadline cutoff guard
- Re-entry cascade after stop-loss: `SL_COOLDOWN_SEC=3600` blocks re-entry for 60 min, persisted across restarts
- `kelly_bet()` div-by-zero on ask ≤ 0 or ≥ 1
- `_ticker_game_date()` ValueError on malformed date strings in tickers
- Double-down bypassing SL cooldown: `execute_addon()` now checks cooldown before placing
- Scanner loop order: exits checked before new scan entries each cycle

### Open — Model
- **Baseball half-inning off-by-one** (`models/baseball_model.py` `prob_home_wins`): top-half case gives home one too few future half-innings (e.g. inning 5 top → home gets 4 future trips, should be 5). Diluted in practice since Markov is only 20% of consensus, but affects pure-Markov fallback paths.
- **Tennis serve fallback to league average** (`models/serve_stats.py`): players with <50 recorded serve points fall back to ATP_AVG (0.64) / WTA_AVG (0.58), making the Markov chain treat them as symmetric regardless of score. Primary cause of 70–80% model prob zone's 40–47% actual win rate. Fix: wire `serve_win_prob_from_elo()` (already in `tennis_model.py`) as fallback instead of the constant.
- **Tennis has no three-signal model**: `espn_win_prob` and `vegas_live_prob` are always 0.0 for tennis — no ESPN win prob endpoint exists for tennis, and no free Vegas source has been identified.
- **Extra innings modeled as 50/50** (`models/baseball_model.py`): home teams win extra innings ~54% historically; post-2020 runner-on-2B rule significantly raises scoring probability. Not yet updated.

### Open — Scanner / Executor
- **Tennis markets lack upper-bound date filter** (`live_scanner.py`): currently only filters `occurrence_datetime < now`. A stale Kalshi market without a result could match a live ESPN player. Needs `< now + 1h` upper bound.
- **Kelly fraction still 0.5** (`live_scanner.py`): code review recommended cutting to 0.25 until model calibration is confirmed on clean post-bug data. Still at 0.5.
- **DraftKings provider ID hardcoded** (`live_scanner.py`): `provider.id == "100"` — if ESPN renumbers providers, Vegas signal silently drops to 0.0 with no warning or log entry.
- **`_open_tickers` has no thread lock** (`order_executor.py`): Flask `/api/sell` runs in a separate thread from the scanner loop; concurrent read/write is unsynchronized.
- **Stale pruning can remove unfilled buy orders** (`dashboard.py`): a newly placed order that hasn't appeared in `get_positions()` yet gets pruned from `_open_tickers`, causing a duplicate order next cycle.
- **Doubleheader matching uses first game found** (`live_scanner.py` `find_baseball_game()`): ticker encodes game time (HHMM) but it's not used for disambiguation when a team plays twice in a day.

### Open — Infrastructure
- Odds API free tier is 500 req/month — warn if `arb_interval_sec` < 21600 (6h).
- Performance tab for live mode does not account for exit P&L from `exits_live.jsonl`.
- `min_model_prob` only filters `execute_live()` — arb signals have no `model_prob` field.
- Duplicate baseball game cards in Live Now (Games tab): ESPN substring matching can return multiple hits for common city/team names. Needs deduplication by canonical team pair.
