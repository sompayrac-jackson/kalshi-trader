# Kalshi + ESPN API Dictionary

All fields verified against live API responses. Fields marked **[UNUSED]** are available but not currently captured.

---

## Kalshi API

**Base URL**: `https://api.elections.kalshi.com/trade-api/v2`  
**Auth**: RSA-PSS signed headers per request

### Market Object — `GET /markets/{ticker}`

```python
client.get_market(ticker)  # unwraps {"market": {...}}
```

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| `ticker` | str | `"KXMLBGAME-26JUN032010PITHOU-PIT"` | Encodes sport, date, time, matchup, team |
| `event_ticker` | str | `"KXMLBGAME-26JUN032010PITHOU"` | Parent event (game); both teams share one |
| `status` | str | `"active"` / `"finalized"` | Open = active, settled = finalized |
| `result` | str | `""` / `"yes"` / `"no"` | Empty while in-play; set at settlement |
| `yes_ask_dollars` | str→float | `"0.5200"` | Current best ask for YES (cost to buy) |
| `yes_bid_dollars` | str→float | `"0.4500"` | Current best bid for YES (exit price) |
| `no_ask_dollars` | str→float | `"0.5500"` | NO side ask (= 1 - yes_bid approx) |
| `no_bid_dollars` | str→float | `"0.4800"` | NO side bid |
| `yes_ask_size_fp` | str→float | `"50.00"` | Contracts available at best ask — used in `execute()` liquidity check |
| **[UNUSED]** `yes_bid_size_fp` | str→float | `"469.00"` | Contracts available at best bid |
| **[UNUSED]** `volume_fp` | str→float | `"842653.02"` | Total contracts traded all-time |
| **[UNUSED]** `volume_24h_fp` | str→float | `"842188.73"` | 24h volume — proxy for market efficiency |
| **[UNUSED]** `open_interest_fp` | str→float | `"518807.92"` | Outstanding contracts (buyers haven't exited) |
| **[UNUSED]** `liquidity_dollars` | str→float | `"0.0000"` | Kalshi liquidity metric |
| **[UNUSED]** `last_price_dollars` | str→float | `"0.0100"` | Last traded price |
| **[UNUSED]** `previous_price_dollars` | str→float | `"0.5100"` | Prior session close price — baseline comparison |
| **[UNUSED]** `previous_yes_ask_dollars` | str→float | `"0.5100"` | Prior close ask |
| **[UNUSED]** `previous_yes_bid_dollars` | str→float | `"0.5000"` | Prior close bid |
| `occurrence_datetime` | str (ISO) | `"2026-06-07T05:10:00Z"` | **BASEBALL**: settlement deadline ~3h after first pitch. **TENNIS**: match start time |
| `open_time` | str (ISO) | `"2026-06-04T02:25:00Z"` | When market opened for trading |
| `close_time` | str (ISO) | `"2026-06-10T02:10:00Z"` | Max close time (usually much earlier via early close) |
| `expected_expiration_time` | str (ISO) | `"2026-06-07T05:10:00Z"` | Expected settlement time |
| **[UNUSED]** `settlement_ts` | str (ISO) | `"2026-06-04T02:48:16Z"` | Exact settlement timestamp (finalized only) |
| **[UNUSED]** `settlement_value_dollars` | str→float | `"0.0000"` | `1.0000` if YES won, `0.0000` if NO |
| **[UNUSED]** `expiration_value` | str | `"St. Louis"` | Winning team/player name at settlement |
| `yes_sub_title` | str | `"San Diego"` | Team/player this YES market refers to |
| `no_sub_title` | str | `"New York M"` | Opposing team/player |
| `title` | str | `"New York M vs San Diego Winner?"` | Human-readable title |
| `rules_primary` | str | `"If San Diego wins..."` | Contains full team names + game date |
| `market_type` | str | `"binary"` | Always binary for these markets |
| **[UNUSED]** `settlement_timer_seconds` | int | `120` | Delay after result before settlement fires |
| **[UNUSED]** `notional_value_dollars` | str→float | `"1.0000"` | Always 1.00 — YES pays $1 per contract |
| **[UNUSED]** `can_close_early` | bool | `true` | Always true for sports markets |
| **[UNUSED]** `custom_strike.baseball_team` | str (UUID) | `"807fa239-..."` | Kalshi internal team ID |
| **[UNUSED]** `custom_strike.tennis_competitor` | str (UUID) | `"05040645-..."` | Kalshi internal player ID |

### Orderbook — `GET /markets/{ticker}/orderbook?depth=N`

```python
client.get_orderbook(ticker, depth=10)
```

Returns `orderbook_fp` with YES and NO ladders:

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| `orderbook_fp.yes_dollars` | list[[price, size]] | `[["0.65", "1106.00"], ...]` | Ascending bids (best bid last) |
| `orderbook_fp.no_dollars` | list[[price, size]] | `[["0.26", "1647.00"], ...]` | Ascending offers |

**Derived insights**:
- Depth at each price level shows how much you can trade without moving the market
- Spread = yes best ask − yes best bid
- Asymmetric depth (huge ask size, thin bid size) = market maker heavy on one side

### Portfolio Positions — `GET /portfolio/positions`

```python
client.get_positions()
```

Returns `market_positions[]` and `event_positions[]`:

**market_positions item:**

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| `ticker` | str | `"KXMLBGAME-26JUN041335BALBOS-BOS"` | Market ticker |
| `position_fp` | str→float | `"48.00"` | Net contracts held (0 = closed) |
| `market_exposure_dollars` | str→float | `"24.960000"` | Current mark-to-market value |
| **[UNUSED]** `fees_paid_dollars` | str→float | `"0.838700"` | **Total fees paid on this position** |
| **[UNUSED]** `realized_pnl_dollars` | str→float | `"-12.540000"` | P&L already realized (from partial sells) |
| **[UNUSED]** `total_traded_dollars` | str→float | `"45.540000"` | Total dollar value bought + sold |
| **[UNUSED]** `resting_orders_count` | int | `0` | Pending limit orders on this market |
| **[UNUSED]** `last_updated_ts` | str (ISO) | `"2026-06-04T02:35:22Z"` | Last activity timestamp |

**event_positions item** (aggregated per game, both teams):

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| `event_ticker` | str | `"KXMLBGAME-26JUN032010PITHOU"` | Game-level ticker |
| `event_exposure_dollars` | str→float | `"15.930000"` | Net exposure on this game |
| **[UNUSED]** `total_cost_dollars` | str→float | `"25.530000"` | Total amount spent on this game |
| **[UNUSED]** `fees_paid_dollars` | str→float | `"0.596600"` | Fees for all markets in this game |
| **[UNUSED]** `realized_pnl_dollars` | str→float | `"-1.600000"` | Realized P&L for settled side |

### Portfolio Orders — `GET /portfolio/orders`

```python
client.get_orders(limit=100)
```

**order item:**

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| `order_id` | str (UUID) | `"446fba1b-..."` | Unique order ID |
| `ticker` | str | `"KXMLBGAME-..."` | Market ticker |
| `action` | str | `"buy"` / `"sell"` | Direction |
| `side` | str | `"yes"` | YES or NO side |
| `status` | str | `"executed"` / `"resting"` | Order state |
| `type` | str | `"limit"` | Always limit for our orders |
| `yes_price_dollars` | str→float | `"0.5800"` | Limit price in dollars |
| `no_price_dollars` | str→float | `"0.4200"` | NO-equivalent price |
| `initial_count_fp` | str→float | `"29.00"` | Contracts ordered |
| `fill_count_fp` | str→float | `"29.00"` | Contracts filled |
| `remaining_count_fp` | str→float | `"0.00"` | Unfilled contracts |
| **[UNUSED]** `book_side` | str | `"bid"` / `"ask"` | `bid` = maker, `ask` = taker |
| **[UNUSED]** `taker_fees_dollars` | str→float | `"0.494600"` | Taker fee (charged when crossing spread) |
| **[UNUSED]** `maker_fees_dollars` | str→float | `"0.091000"` | Maker fee (rebate when providing liquidity) |
| **[UNUSED]** `taker_fill_cost_dollars` | str→float | `"16.820000"` | Total taker fill cost |
| **[UNUSED]** `maker_fill_cost_dollars` | str→float | `"11.550000"` | Total maker fill cost |
| `created_time` | str (ISO) | `"2026-06-04T02:11:59Z"` | Order placement time |
| **[UNUSED]** `last_update_time` | str (ISO) | `"2026-06-04T02:11:59Z"` | Last fill/update time |
| **[UNUSED]** `outcome_side` | str | `"yes"` / `"no"` | Which side is winning side |

### Balance — `GET /portfolio/balance`

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| `balance` | int | `4814` | Cash balance in cents |
| `balance_dollars` | str→float | `"48.1475"` | Cash balance in dollars |
| **[UNUSED]** `portfolio_value` | int | `17936` | Total portfolio value in cents (cash + positions) |
| **[UNUSED]** `balance_breakdown[].balance` | str→float | `"48.1475"` | Per-exchange breakdown |

---

## ESPN API

**Base URL**: `https://site.api.espn.com/apis/site/v2/sports`  
**Auth**: None required

### Tennis Competition Object

**Endpoint**: `GET /tennis/{atp|wta}/scoreboard`  
**Path in response**: `events[].groupings[].competitions[]`

| Field | Path | Type | Example | Notes |
|-------|------|------|---------|-------|
| `state` | `status.type.state` | str | `"in"` / `"pre"` / `"post"` | **Gate for live signal** |
| `current_set` | `status.period` | int | `2` | Current set number (1-indexed). **Available directly** — we compute this from linescores instead |
| `detail` | `status.type.detail` | str | `"2nd Set, Sinner leads 1-0"` | Human-readable live status |
| `completed` | `status.type.completed` | bool | `false` | True only post-match |
| `best_of` | `format.regulation.periods` | int | `3` or `5` | Best-of sets (5 for Slam men's) |
| **[UNUSED]** `court` | `venue.court` | str | `"Court Philippe Chatrier"` | **Named court — affects conditions** |
| `city` | `venue.fullName` | str | `"Paris, France"` | Tournament city |
| **[UNUSED]** `round` | `round.displayName` | str | `"Semifinal"` | Tournament round — QF/SF/F have better players |
| **[UNUSED]** `match_type` | `type.text` | str | `"Men's Singles"` | Could also be Doubles |
| **[UNUSED]** `was_suspended` | `wasSuspended` | bool | `false` | If match had a suspension |
| `p1_name` | `competitors[order=1].athlete.displayName` | str | `"Carlos Alcaraz"` | Primary competitor |
| `p2_name` | `competitors[order=2].athlete.displayName` | str | `"Novak Djokovic"` | Secondary competitor |
| `p1_serving` | `competitors[order=1].possession` | bool | `true` | **True if this player is currently serving** (live only) |
| **[UNUSED]** `p1_country` | `competitors[order=1].athlete.flag.alt` | str | `"Spain"` | Nationality |
| **[UNUSED]** `p1_espn_id` | `competitors[order=1].athlete.guid` | str (UUID) | `"75e42130-..."` | Unique ESPN player ID |
| `sets_p1` | computed from `competitors[order=1].linescores[:-1]` | int | `1` | Sets won by p1 (we compute this) |
| `games_p1_current` | `competitors[order=1].linescores[-1].value` | float | `4.0` | Games in current set |
| **[UNUSED]** `set_winner_p1` | `competitors[order=1].linescores[i].winner` | bool | `true` | Who won each completed set — score in each set |
| **[UNUSED]** `p1_winner` | `competitors[order=1].winner` | bool | `false` | Match winner (post only) |

**Currently captured**: state, best_of, p1/p2 names, sets, games, serving (possession)  
**NOT captured**: court name, round, nationality, per-set winner flags, set_count directly from status.period

---

### Baseball Competition Object

**Endpoint**: `GET /baseball/mlb/scoreboard`  
**Path in response**: `events[].competitions[]`

| Field | Path | Type | Example | Notes |
|-------|------|------|---------|-------|
| `state` | `status.type.state` | str | `"in"` / `"pre"` / `"post"` | Gate for live signal |
| `inning` | `status.period` | int | `5` | Current inning number |
| `inning_detail` | `status.type.detail` | str | `"Top 5th"` / `"Bot 7th"` / `"End 9th"` | Exact half + inning as string |
| `inning_short` | `status.type.shortDetail` | str | `"Top 5"` / `"Bot 7"` | Compact version |
| `outs` | `situation.outs` | int | `1` | 0, 1, or 2 outs — captured in `LiveSignal.outs`; `SKIP_TWO_OUTS` filter uses this |
| **[UNUSED]** `balls` | `situation.balls` | int | `2` | Ball count |
| **[UNUSED]** `strikes` | `situation.strikes` | int | `1` | Strike count |
| `on_first` | `situation.onFirst` | bool | `false` | Runner on first — captured in `LiveSignal.on_first` |
| `on_second` | `situation.onSecond` | bool | `true` | Runner on second — captured in `LiveSignal.on_second` |
| `on_third` | `situation.onThird` | bool | `false` | Runner on third — captured in `LiveSignal.on_third` |
| **[UNUSED]** `last_play` | `situation.lastPlay.text` | str | `"Single to right"` | Description of last play |
| `scoring_prob_1plus` | `situation.situationNotes[0].text` | str | `"27.65%"` | ESPN inning scoring probability — captured in `LiveSignal.scoring_1plus` |
| **[UNUSED]** `scoring_prob_2plus` | `situation.situationNotes[1].text` | str | `"13.03%"` | ESPN 2+ run probability |
| `home_team` | `competitors[homeAway=home].team.displayName` | str | `"Chicago Cubs"` | Full team name |
| `away_team` | `competitors[homeAway=away].team.displayName` | str | `"Athletics"` | Full team name |
| `home_abbrev` | `competitors[homeAway=home].team.abbreviation` | str | `"CHC"` | Team abbreviation |
| `score_home` | `competitors[homeAway=home].score` | str→int | `"4"` | Current home runs |
| `score_away` | `competitors[homeAway=away].score` | str→int | `"4"` | Current away runs |
| `linescores` | `competitors[].linescores[period=N].value` | float | `2.0` | **Runs per inning** — full scoring history available |
| **[UNUSED]** `home_record_overall` | `competitors[homeAway=home].records[type=total].summary` | str | `"31-32"` | Season W-L record |
| **[UNUSED]** `home_record_home` | `competitors[homeAway=home].records[type=home].summary` | str | `"12-20"` | Home W-L |
| **[UNUSED]** `home_record_road` | `competitors[homeAway=home].records[type=road].summary` | str | `"19-12"` | Road W-L |
| **[UNUSED]** `venue` | `venue.fullName` | str | `"Wrigley Field"` | Ballpark name |
| **[UNUSED]** `venue_indoor` | `venue.indoor` | bool | `false` | Indoor ballpark |
| **[UNUSED]** `neutral_site` | `neutralSite` | bool | `false` | Neutral venue |
| **[UNUSED]** `attendance` | `attendance` | int | `32450` | Game attendance |
| **[UNUSED]** `play_by_play` | `playByPlayAvailable` | bool | `true` | PBP data available (advanced endpoint) |

**Currently captured**: state, inning, score_home, score_away, home/away team names, game_id (for summary fetch), outs, on_first/second/third, scoring_1plus  
**NOT captured**: balls/strikes, per-inning line score, season records, venue, scoring_2plus

---

## ESPN Summary Endpoint — Baseball Only

**Endpoint**: `GET https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/summary?event={game_id}`  
**Auth**: None. Game ID comes from `competitions[].id` in the scoreboard response.  
**Tennis**: This endpoint returns an error for tennis — not available.

### `winprobability` array (game-level, per play) — **ACTIVE**

```python
summary["winprobability"][-1]  # last entry = current probability
```

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| `homeWinPercentage` | float | `0.416` | **Current home team win probability** (0.0–1.0) — captured as `LiveSignal.espn_win_prob` |
| `tiePercentage` | float | `0.0` | Always 0 for baseball |
| `playId` | str | `"401815611..."` | Associated play |

- Array grows by one entry per play (pitch result, stolen base, scoring play, etc.)
- ESPN's model accounts for score, inning, outs, runners, and historical scoring rates
- **76 entries** in a 9-inning game = roughly one per significant event
- Used as 30% weight in `_consensus_prob()` blend

### `pickcenter` array (DraftKings live + historical odds — **FREE, no API key**) — **ACTIVE**

```python
summary["pickcenter"][0]  # DraftKings = provider.id "100"
```

| Field | Path | Example | Notes |
|-------|------|---------|-------|
| `moneyLine.home.open.odds` | str | `"-126"` | Pre-game opening line — captured as `LiveSignal.vegas_open_prob` |
| `moneyLine.home.close.odds` | str | `"-136"` | Pre-game closing line |
| `moneyLine.home.live.odds` | str | `"-135"` | **Current live Vegas moneyline** — captured as `LiveSignal.vegas_live_prob` |
| `moneyLine.away.open.odds` | str | `"+104"` | Away opening |
| `moneyLine.away.live.odds` | str | `"+104"` | Away live |
| `homeTeamOdds.favoriteAtOpen` | bool | `false` | Was home team favored pre-game? |
| `awayTeamOdds.favoriteAtOpen` | bool | `true` | Was away team favored pre-game? |
| `spread` | float | `-1.5` | Run line |
| `overUnder` | float | `8.5` | Total runs line |

**Eliminates the need for the Odds API for baseball live odds.** DraftKings live moneyline is available directly from ESPN at no cost and no rate limit. Used as 50% weight in `_consensus_prob()` blend. Provider filtered by `provider.id == "100"` (hardcoded — silent failure risk if ESPN renumbers).

Converting American odds to implied probability:
```python
def american_to_prob(odds: int) -> float:
    if odds > 0: return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)
# -136 → 57.6%,  +104 → 49.0%
```

### `plays` array (per-play game state)

Each play entry:

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| `period` | int | `9` | Inning |
| `awayScore` | int | `5` | Away runs at this play |
| `homeScore` | int | `4` | Home runs at this play |
| `outs` | int | `2` | Outs at this moment |
| `onFirst` | obj/null | `{"athlete": {"id": "..."}}` | Runner on first (obj = runner present) |
| `onSecond` | obj/null | — | Runner on second |
| `onThird` | bool/null | `false` | Runner on third |
| `scoringPlay` | bool | `false` | Did this play score a run |
| `scoreValue` | int | `1` | Runs scored on this play |
| `wallclock` | str (ISO) | `"2026-06-04T03:00:44Z"` | Real time of this play |
| `pitchCoordinate` | obj | `{x, y}` | Pitch location (Statcast-style) |
| `hitCoordinate` | obj | `{x, y}` | Ball landing location |
| `trajectory` | str | `"line_drive"` | Ball trajectory type |

---

## What `situation` Gives Us (Baseball — HIGH VALUE)

The `situation` object is only present when `state == "in"`. It is the single biggest untapped data source:

```
situation = {
  balls: 0-3,
  strikes: 0-2,
  outs: 0-2,                    ← changes model probability dramatically
  onFirst: bool,                ← base state affects scoring probability
  onSecond: bool,
  onThird: bool,
  lastPlay: { text: "..." },    ← context for why price just moved
  situationNotes: [
    { text: "Chance of scoring 1+ runs this inning (N outs, X on Y): XX.XX%" },
    { text: "Chance of scoring 2+ runs this inning: XX.XX%" }
  ]
}
```

**Key insight**: ESPN already computes an inning-level scoring probability. We can:
1. Cross-validate against our Markov model
2. Use ESPN's probability directly as a model input or signal filter
3. Log it to understand if we're entering with/against the situational probability

---

## Data Gaps Summary

### Highest-Value Missing Captures

| Data Point | Source | Why It Matters |
|-----------|--------|----------------|
| `venue.court` | ESPN tennis | Court conditions affect serve/rally dynamics |
| `round.displayName` | ESPN tennis | Semifinal/Final = higher caliber players |
| `open_interest_fp` | Kalshi | Market efficiency indicator; thin markets have more edge |
| `volume_24h_fp` | Kalshi | Trading activity proxy for pricing accuracy |
| `previous_price_dollars` | Kalshi | Yesterday's close = pre-game baseline for drift measurement |
| `fees_paid_dollars` | Kalshi portfolio | Real cost of trading; missing from P&L calculation |
| `season W-L records` | ESPN baseball | Team form; home/road performance split |
| `linescores[]` (all innings) | ESPN baseball | Scoring pattern; big-inning context |

### Currently Well-Captured

- Market ask/bid price at entry (`price_cents`, `bid_cents`, `spread_cents`)
- Model probability and edge at entry (`model_prob`, `markov_prob`, `espn_win_prob`, `vegas_live_prob`, `vegas_open_prob`)
- Score state and score differential
- Sport, player/team identifiers
- Order status and cost
- Portfolio equity curve (portfolio_snapshots.jsonl)
- Base-out situation (`outs`, `on_first`, `on_second`, `on_third`, `scoring_1plus`)
- Liquidity depth at ask (`yes_ask_size_fp` used in `execute()` pre-order check)
