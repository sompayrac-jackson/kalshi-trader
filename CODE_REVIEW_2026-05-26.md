# Full Stack Code Review — 2026-05-26

Four parallel agents reviewed exit/risk management, signal scanning, probability models, and infrastructure. Results compiled below, false positives removed, and ranked by actual severity.

---

## CRITICAL — Fix before next trading session

### C1. Baseball model: half-inning remaining count is off-by-one
**File:** `models/baseball_model.py` ~lines 196–241  
**Agent:** Model accuracy

The remaining half-inning counts for home and away are computed incorrectly. When `is_bottom=True` (home batting), the model assigns `home_halves_left = 9 - inning`, but the home team is currently using one of those half-innings. Both teams get the same count, which overcounts away's remaining opportunities.

**Impact:** Inflates underdog (away) recovery probability in mid-game → explains the 20%+ edge bucket's 38.7% WR. The model consistently overestimates away comeback chances.

**Correct accounting:**
```python
if is_bottom:
    # Away completed 'inning' full innings; home completed 'inning - 1' + currently batting
    away_remaining = 9 - inning        # top of inning+1 through 9
    home_remaining = 9 - inning        # rest of current + future (same count, different logic)
else:
    # Away completed 'inning - 1' innings, now batting top of current
    away_remaining = 9 - inning + 1   # rest of current + future
    home_remaining = 9 - inning        # future only
```
Add unit test: inning=5, is_bottom=True → away should have exactly 4 half-innings (top 6–9).

---

### C2. Tennis model: serve probability fallback to league average drives overconfidence
**File:** `models/tennis_model.py`, `models/serve_stats.py`  
**Agent:** Model accuracy

When a player has <50 recorded serve points (most mid-rank or emerging players), `serve_pct()` returns ATP_AVG (64%) or WTA_AVG (58%). Both players in a match often fall through to the same default, making the Markov chain symmetric regardless of the actual score state.

**Impact:** When a player leads 1–0 sets, 4–2 games, the model predicts ~70% win. But with symmetric serve probabilities, it systematically underweights the trailing player's recovery difficulty. Real-world actual WR in the 70–80% prediction zone is 40–47% — this is the primary cause.

The fix path: `tennis_model.py` already has `serve_win_prob_from_elo()` but it's never called from `live_scanner.py`. Use it as the fallback instead of the league constant.

```python
# live_scanner.py ~line 282, in tennis_signal():
p1_serve = serve_pct(p1_name, tour)
if p1_serve == ATP_AVG or p1_serve == WTA_AVG:
    p1_serve = serve_win_prob_from_elo(p1_elo, p2_elo, tour)  # already implemented
```

---

### C3. `kelly_bet()` division by zero when ask = 1.0
**File:** `live_scanner.py` ~line 254  
**Agent:** Signal scanner

```python
b = (1 - ask) / ask   # crashes if ask == 1.0 or ask == 0.0
```

If Kalshi ever returns `yes_ask_dollars = 1.0` (market fully priced in), this raises `ZeroDivisionError` and crashes the scanner thread.

**Fix (2 lines):**
```python
def kelly_bet(prob, ask, bankroll_usd, fraction=KELLY_FRACTION):
    if ask <= 0 or ask >= 1:
        return 0.0
    b = (1 - ask) / ask
    ...
```

---

### C4. `_ticker_game_date()` uncaught ValueError on invalid dates
**File:** `live_scanner.py` ~line 382  
**Agent:** Signal scanner

If a Kalshi ticker ever encodes an invalid date (e.g., Feb 30), `date(2026, 2, 30)` raises `ValueError`. This propagates out of the list comprehension and crashes the entire scan cycle.

**Fix (wrap in try/except):**
```python
def _ticker_game_date(ticker: str):
    m = re.search(r'(\d{2})(JAN|...|DEC)(\d{2})', ticker)
    if not m:
        return None
    try:
        return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))
    except ValueError:
        return None
```

---

## HIGH — Fix this week

### H1. `_sl_cooldown` lost on scanner restart
**File:** `order_executor.py`, `_load_open_tickers()`  
**Agent:** Exit system

`_sl_cooldown` is in-memory only. If the scanner restarts (service restart, crash, settings change), all cooldowns are lost and SL'd positions can be immediately re-entered.

**Fix:** Extend `_load_open_tickers()` to reconstruct cooldowns from `exits_live.jsonl`:
```python
_sl_cooldown = {}
for f in (EXITS_DRY_FILE, EXITS_LIVE_FILE):
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                x = json.loads(line)
                if x.get("reason") == "stop_loss":
                    t = x.get("ticker", "")
                    ts = x.get("ts", "")
                    if t and ts:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        _sl_cooldown[t] = max(_sl_cooldown.get(t, 0), dt.timestamp())
            except Exception:
                pass
```

---

### H2. Double-down (`execute_addon`) bypasses SL cooldown
**File:** `order_executor.py`, `execute_addon()`  
**Agent:** Exit system

`execute_addon()` bypasses `_open_tickers` dedup (intentional), but it also bypasses the SL cooldown check (not intentional). A position that was stopped out and re-entered could receive an addon in the same cycle as re-entry.

**Fix:** Add the same cooldown guard as in `execute_live()`:
```python
def execute_addon(...):
    sl_ts = _sl_cooldown.get(ticker, 0)
    if time.time() - sl_ts < SL_COOLDOWN_SEC:
        remaining = int(SL_COOLDOWN_SEC - (time.time() - sl_ts))
        return _skip(ts, ticker, player, side, ask_dollars, kelly_usd, edge, "live",
                     f"SL cooldown active — addon blocked ({remaining}s remaining)")
```

---

### H3. Stale pruning can remove unfilled buy orders
**File:** `dashboard.py`, scanner loop ~line 542  
**Agent:** Exit system

The stale-pruning logic removes a ticker from `_open_tickers` when it's absent from `get_positions()`. But a newly placed buy order that hasn't filled yet also won't appear in `get_positions()`, so it gets pruned. Next scan places a duplicate order.

**Fix:** Don't prune tickers that were ordered in the last 5 minutes:
```python
recently_bought = set()
if ORDERS_LIVE_FILE.exists():
    cutoff = time.time() - 300
    for line in ORDERS_LIVE_FILE.read_text(encoding="utf-8").splitlines():
        try:
            o = json.loads(line)
            dt = datetime.fromisoformat(o.get("ts","").replace("Z","+00:00"))
            if dt.timestamp() > cutoff:
                recently_bought.add(o.get("ticker",""))
        except Exception:
            pass

stale = {t for t in executor._open_tickers
         if t not in active_on_kalshi and t not in recently_bought}
```

---

### H4. Baseball doubleheader ambiguity — same team, two games same day
**File:** `live_scanner.py`, `find_baseball_game()`  
**Agent:** Signal scanner

When a team plays a doubleheader, `find_baseball_game()` returns the first ESPN match regardless of which game the Kalshi ticker refers to. The ticker includes game time (`HHMM`), but it's not used in matching.

**Fix:** Extract game time from ticker and match ESPN games by time proximity:
```python
def _ticker_game_time(ticker: str) -> int | None:
    """Returns HHMM as int, e.g. 1810 for an 18:10 game."""
    m = re.search(r'[A-Z]{3}\d{2}(\d{4})', ticker)
    return int(m.group(1)) if m else None
```

Then in `find_baseball_game()`, if multiple games match, pick the one whose ESPN start time is closest to the ticker's `HHMM`.

---

### H5. Tennis markets have no date filter (unlike baseball)
**File:** `live_scanner.py`, `scan_live()`  
**Agent:** Signal scanner

Baseball markets now filter to today + yesterday via `_ticker_game_date()`. Tennis markets only filter by `occurrence_datetime < now`, which uses the settlement deadline timestamp. If Kalshi keeps past tournament markets open without a result set, stale tennis markets could match live ESPN players.

**Fix:** Add a date window to tennis as well. Since tennis `occurrence_datetime` is match start time (unlike baseball where it's settlement deadline), the existing filter `< now` is correct but add an upper-bound for upcoming matches:
```python
tennis_markets = [
    m for m in client.get_tennis_markets(status="open")
    if not m.get("result")
    and m.get("yes_ask_dollars")
    and m.get("occurrence_datetime")
    and (now - timedelta(hours=6))
       < datetime.fromisoformat(m["occurrence_datetime"].replace("Z", "+00:00"))
       < (now + timedelta(hours=1))
]
```

---

### H6. No 429 rate-limit handling in `KalshiClient`
**File:** `kalshi_client.py`  
**Agent:** Infrastructure

All HTTP methods call `raise_for_status()` unconditionally. A 429 crashes the scan cycle with an unhandled exception. With N open positions each requiring a `get_market()` call, 20+ open positions = 20+ API calls per 30-second cycle, which can trigger throttling.

**Fix:** Add retry with backoff:
```python
def _get(self, path: str, params=None) -> dict:
    for attempt in range(4):
        resp = self._session.get(self._url(path), headers=self._auth_headers(), params=params)
        if resp.status_code == 429:
            wait = 2 ** attempt
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Rate limited after retries: {path}")
```

---

### H7. `_open_tickers` accessed from multiple threads without a lock
**File:** `dashboard.py`, `order_executor.py`  
**Agent:** Infrastructure

The scanner thread and Flask request threads (e.g., `/api/sell`, `/api/start`) both read and write `executor._open_tickers` without synchronization. In CPython, set operations are not atomic, and concurrent modification can corrupt the set.

**Fix:** Add `executor._tickers_lock = threading.Lock()` and wrap all `_open_tickers` reads/writes. The highest-risk spot is `_check_exits()` iterating while the scanner loop prunes — they run in the same thread today but `api_sell` runs in a Flask thread.

---

## MEDIUM — Address in next sprint

### M1. Kelly fraction is too aggressive given known miscalibration
**File:** `live_scanner.py`, line 24  
**Agent:** Model accuracy

`KELLY_FRACTION = 0.5` (half-Kelly) is appropriate for a well-calibrated model. With the confirmed 70–80% zone overconfidence (actual WR 40–47% vs. 70–80% predicted), the effective Kelly should be much smaller. Reduce to 0.25 until the baseball model half-inning bug and tennis serve fallback are fixed.

---

### M2. Store `model_prob` explicitly in order logs
**File:** `order_executor.py`, `dashboard.py`  
**Agent:** Exit system

`_check_exits()` reconstructs `model_prob` as `edge + ask_price` when no live signal is available. This heuristic is wrong: edge=10%, ask=30% → model_prob estimated as 0.40 (clamped to 0.50), when true value might be 0.65. The reconstruction affects PT scaling.

**Fix:** Add `model_prob: float = 0.5` to `OrderResult` dataclass, populate it in `execute_live()`, and store it in the log. Then `_check_exits()` reads it directly.

---

### M3. Extra innings modeled as 50/50
**File:** `models/baseball_model.py` ~line 262  
**Agent:** Model accuracy

When a game is tied after 9 innings, the model assigns `p_home_wins += 0.5`. Historically, home teams win extra innings ~54% of the time, and post-2020 MLB rules (runner on 2B to start extras) change run-scoring dramatically.

**Fix:** Use empirical rate: `p_home_wins += p_combo * 0.54`

---

### M4. Tiebreak probability uses naive average
**File:** `models/tennis_model.py` ~lines 54–73  
**Agent:** Model accuracy

The tiebreak uses `p_avg = (p1_serve + p2_receive) / 2` which treats it as a symmetric fixed-probability game. Real tiebreaks alternate serve every 2 points, compounding the serve advantage.

---

### M5. Auth-disabled warning missing
**File:** `dashboard.py` ~line 73  
**Agent:** Infrastructure

If `DASHBOARD_PASS` is empty/missing, auth is silently bypassed. Add a startup warning log.

---

### M6. Notifier swallows all exceptions silently
**File:** `notifier.py`  
**Agent:** Infrastructure

All Pushover errors return `False` with no log entry. Add `logging.warning(f"Pushover failed: {e}")`.

---

### M7. Doubleheader dedup needed in `scan_live()` return
**File:** `live_scanner.py`  
**Agent:** Signal scanner

If Kalshi returns the same ticker twice (data issue), two signals are generated. The second is skipped by `_open_tickers` after the first executes, but it pollutes the signal log and analysis. De-duplicate by ticker before returning, keeping highest absolute edge.

---

### M8. Edge bucket "3-5%" includes 0% and negative edges
**File:** `dashboard.py` ~line 1529  
**Agent:** Infrastructure

Label says "3-5%" but condition is `edge < 0.05`, including any edge below 5% (could be 0% or negative). Change lower bound to `0.03 <= edge < 0.05`.

---

### M9. Config parser doesn't strip key whitespace
**File:** `config.py`  
**Agent:** Infrastructure

A line like `KALSHI_API_KEY = abc123` produces a key `"KALSHI_API_KEY "` (trailing space). Add `key = key.strip()` after `key, _, val = line.partition("=")`.

---

## LOW — Improvements

| ID | Issue | File | Notes |
|----|-------|------|-------|
| L1 | Serve stats uses league avg for inactive/retired players | `serve_stats.py` | Fallback should prefer last-year data over league avg |
| L2 | `parse_inning()` extra innings don't adjust model's 9-inning assumption | `live_scanner.py` | Pass `total_innings=inning` to `prob_home_wins_simple` for inning > 9 |
| L3 | Tennis model output not clipped to [0, 1] | `tennis_model.py` | Baseball model clips; tennis doesn't. Add `max(0, min(1, ...))` |
| L4 | Baseball convolution has no NaN guard | `baseball_model.py` | If dist sums to 0, add fallback `[1.0, 0, 0...]` |
| L5 | Entry map overwrite is silent | `dashboard.py` | Log when `entry_map[t]` is overwritten (re-entry detected) |
| L6 | `execute_addon()` has no internal DD condition validation | `order_executor.py` | Caller validates today; add defensive checks as belt-and-suspenders |
| L7 | Frontend wSum division-by-zero in sparkline bars | `dashboard.py` | Add `th.wSum > 0` guard in JS |

---

## False Positive from Agents

**"P&L calculation bug"** — Agent 4 flagged `pnl = (o["contracts"] - cost) if won else -cost`. This is correct: each Kalshi YES contract settles to $1.00, so `o["contracts"]` in contracts = `o["contracts"]` in dollars payout. `contracts − cost_usd` is the right P&L formula.

---

## Prioritized Fix Plan

### Implement today (all mechanical, 1–3 lines each)
1. ✅ `kelly_bet()` div-by-zero guard — `live_scanner.py`
2. ✅ `_ticker_game_date()` ValueError catch — `live_scanner.py`
3. ✅ `_sl_cooldown` persistence in `_load_open_tickers()` — `order_executor.py`
4. ✅ Double-down SL cooldown in `execute_addon()` — `order_executor.py`
5. ⚠️ Stale pruning recently-bought guard — `dashboard.py` (not yet verified)
6. ⚠️ Auth-disabled warning at startup — `dashboard.py` (not yet verified)
7. ⚠️ Config key whitespace strip — `config.py` (not yet verified)
8. ⚠️ Notifier exception logging — `notifier.py` (not yet verified)

### Implement this week (moderate complexity)
9. ⚠️ Doubleheader time-matching in `find_baseball_game()` — `live_scanner.py`
10. ⚠️ Tennis date window upper-bound filter — `live_scanner.py`
11. ⚠️ 429 retry in `KalshiClient` — `kalshi_client.py`
12. ⚠️ Kelly fraction to 0.25 — `live_scanner.py` (still at 0.5)
13. ✅ `model_prob` stored in order logs — `order_executor.py` (`OrderResult.model_prob` field populated in `execute_live()`)

### Implement after stable baseline (model changes, need careful testing)
14. ⚠️ Baseball half-inning off-by-one — `models/baseball_model.py` (top-half case still undercounts home future trips by 1)
15. ⚠️ Tennis serve fallback → Elo-adjusted prior — `live_scanner.py` (`serve_win_prob_from_elo()` exists in `tennis_model.py` but is not wired as fallback)
16. ⚠️ Extra innings home advantage (~54%) — `models/baseball_model.py`
17. ⚠️ Tiebreak serve formula — `models/tennis_model.py`
