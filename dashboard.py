"""
Kalshi Trader Dashboard.

Flask web interface: signals feed, positions, order history, runner controls.

Usage:
    pip install flask
    python dashboard.py              # default port 5000
    python dashboard.py --port 8080  # custom port
"""

import hmac
import json
import logging
import re
import threading
import time
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, Response

import config
import notifier
from kalshi_client import KalshiClient
import live_scanner as live_mod
from live_scanner import scan_live
import order_executor as executor

try:
    from arb_scanner import scan as arb_scan
    HAS_ARB = True
except ImportError:
    HAS_ARB = False

KALSHI_API_KEY = config.KALSHI_API_KEY
ODDS_API_KEY   = config.ODDS_API_KEY
POSITIONS_FILE    = Path("positions.jsonl")
ORDERS_DRY_FILE   = Path("orders_dry.jsonl")
ORDERS_LIVE_FILE  = Path("orders_live.jsonl")
EXITS_DRY_FILE    = Path("exits_dry.jsonl")
EXITS_LIVE_FILE   = Path("exits_live.jsonl")
PERF_CACHE_DRY    = Path("perf_cache_dry.json")
PERF_CACHE_LIVE   = Path("perf_cache_live.json")
SIGNALS_DRY_FILE  = Path("signals_dry.jsonl")
SIGNALS_LIVE_FILE = Path("signals_live.jsonl")
PRICE_HISTORY_FILE    = Path("price_history.jsonl")
PORTFOLIO_SNAP_FILE   = Path("portfolio_snapshots.jsonl")
PREGAME_SNAP_FILE     = Path("pregame_snapshots.jsonl")
SETTLEMENTS_FILE      = Path("settlements.jsonl")

def _orders_file(mode: str) -> Path:
    return ORDERS_LIVE_FILE if mode == "live" else ORDERS_DRY_FILE

def _exits_file(mode: str) -> Path:
    return EXITS_LIVE_FILE if mode == "live" else EXITS_DRY_FILE

def _signals_file(mode: str) -> Path:
    return SIGNALS_LIVE_FILE if mode == "live" else SIGNALS_DRY_FILE

def _perf_cache_file(mode: str) -> Path:
    return PERF_CACHE_LIVE if mode == "live" else PERF_CACHE_DRY

app = Flask(__name__)


# ── HTTP Basic Auth ───────────────────────────────────────────────────────────

def _check_auth(username: str, password: str) -> bool:
    if not config.DASHBOARD_PASS:
        return True  # auth disabled — dev mode only
    ok_user = hmac.compare_digest(username.encode(), config.DASHBOARD_USER.encode())
    ok_pass = hmac.compare_digest(password.encode(), config.DASHBOARD_PASS.encode())
    return ok_user and ok_pass


def _require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not config.DASHBOARD_PASS:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return Response(
                "Authentication required.",
                401,
                {"WWW-Authenticate": 'Basic realm="Kalshi Trader"'},
            )
        return f(*args, **kwargs)
    return decorated

# ── Shared state ─────────────────────────────────────────────────────────────
# Current bid prices for open tickers, refreshed every scan cycle.
_price_cache: dict[str, float] = {}   # ticker -> yes_bid_dollars
_price_lock  = threading.Lock()

# ── Shared state ──────────────────────────────────────────────────────────────

_lock       = threading.Lock()
_stop_event = threading.Event()

# Pre-game Kalshi ask prices — ticker -> ask_dollars captured before game start.
# Used to measure how much the market moved from open to our live entry.
_pregame_cache:     dict[str, float] = {}
_pregame_cache_lock = threading.Lock()
_pregame_last_snap  = 0.0          # unix timestamp of last pregame snapshot
_scan_thread: threading.Thread | None = None
_client: KalshiClient | None = None

_state: dict = {
    "running":        False,
    "dry_run":        False,
    "last_live_scan": None,
    "last_arb_scan":  None,
    "signals":        [],
    "log":            [],
    "config": {
        "min_edge":              0.10,
        "max_bet_usd":           25.0,
        "kelly_fraction":        0.5,
        "arb_interval_sec":      300,
        "live_interval_sec":     30,
        "stop_loss_pct":         0.35,
        "profit_take_pct":       0.50,
        "min_ask":               0.05,
        "min_model_prob":        0.65,
        "max_entry_price":       0.65,
        "exits_enabled":         False,
        "double_down_enabled":   False,
        "double_down_min_conf":  0.75,
        "double_down_conf_gain": 0.10,
        "double_down_max_addons": 1,
        "double_down_max_total":  2.0,
    },
}


# ── Scanner background thread ─────────────────────────────────────────────────

def _log(msg: str):
    ts   = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with _lock:
        _state["log"].append(line)
        if len(_state["log"]) > 300:
            _state["log"] = _state["log"][-300:]


def _to_dict(sig, source: str) -> dict:
    d = asdict(sig)
    d["source"]    = source
    d["direction"] = "BUY" if sig.edge > 0 else "SKIP"
    d["ts"]        = datetime.now(timezone.utc).isoformat()
    # Normalise field names so the frontend only needs one code path
    if "book_prob" in d and "model_prob" not in d:
        d["model_prob"] = d["book_prob"]
    if "score_state" not in d:
        d["score_state"] = d.get("bookmaker", "")
    if "sport" not in d:
        d["sport"] = "arb"
    return d


def _sync_executor_config():
    """Push dashboard config sliders into scanner and executor module globals."""
    cfg = _state["config"]
    executor.DRY_RUN             = _state["dry_run"]
    executor.EXITS_ENABLED       = bool(cfg.get("exits_enabled", True))
    executor.MIN_EDGE            = cfg["min_edge"]
    executor.MAX_BET_USD         = cfg["max_bet_usd"]
    executor.STOP_LOSS_PCT       = cfg["stop_loss_pct"]
    executor.PROFIT_TAKE_PCT     = cfg["profit_take_pct"]
    executor.MIN_ASK             = cfg["min_ask"]
    executor.MIN_MODEL_PROB      = float(cfg.get("min_model_prob", 0.0))
    executor.MAX_ENTRY_PRICE     = float(cfg.get("max_entry_price", 0.65))
    executor.DOUBLE_DOWN_ENABLED    = bool(cfg.get("double_down_enabled", False))
    executor.DOUBLE_DOWN_MIN_CONF   = float(cfg.get("double_down_min_conf", 0.75))
    executor.DOUBLE_DOWN_CONF_GAIN  = float(cfg.get("double_down_conf_gain", 0.10))
    executor.DOUBLE_DOWN_MAX_ADDONS = int(cfg.get("double_down_max_addons", 1))
    executor.DOUBLE_DOWN_MAX_TOTAL  = float(cfg.get("double_down_max_total", 2.0))
    live_mod.MIN_EDGE            = cfg["min_edge"]
    live_mod.KELLY_FRACTION      = cfg["kelly_fraction"]


def _scale_exits(cost_usd: float, base_sl: float, base_pt: float, cfg: dict) -> tuple:
    """
    Scale SL/PT thresholds by position size.
    Small positions get wider bands (1.5× base); full-size gets tighter (0.75× base).
    Linear interpolation between the two extremes.
    """
    ceiling = max(float(cfg.get("max_bet_usd", 25.0)), 1.0)
    t = min(cost_usd / ceiling, 1.0)   # 0 = tiny, 1 = full-size or larger
    scale = 1.5 + t * (0.75 - 1.5)     # 1.5× → 0.75×
    return base_sl * scale, base_pt * scale


def _model_pt_scale(model_prob: float) -> float:
    """
    Scale PT threshold up based on model confidence so high-confidence positions
    hold toward settlement instead of exiting early.
    At 50% confidence → 1× (unchanged).
    At 70% → ~2.6×, at 85% → ~4.2×, at 95%+ → ~5× (effectively hold to settlement).
    SL is not affected — model confidence doesn't help a losing position.
    """
    confidence = max(0.0, model_prob - 0.5) * 2   # 0 at ≤50%, 1 at 100%
    return 1.0 + confidence * 4.0


def _check_exits(client: KalshiClient, live_positions: list[dict] | None = None):
    """
    Scan open positions (real in live mode, virtual from orders.jsonl in dry-run)
    and trigger stop-loss or profit-take sells when thresholds are breached.

    live_positions: pre-fetched from Kalshi by the scanner loop (avoids a redundant
    get_positions() call here). Pass None only when calling outside the scanner loop.
    """
    if not executor.EXITS_ENABLED:
        return
    cfg             = _state["config"]
    stop_loss_pct   = cfg["stop_loss_pct"]
    profit_take_pct = cfg["profit_take_pct"]
    dry_run         = _state["dry_run"]

    # Real positions: use the pre-fetched list when available, else fetch now.
    if live_positions is not None:
        real_positions = live_positions
    else:
        real_positions = []
        if not dry_run:
            try:
                real_positions = client.get_positions().get("market_positions", [])
            except Exception as e:
                _log(f"[EXIT] positions fetch failed: {e}")

    # Virtual positions in dry-run: orders_dry.jsonl entries not yet in exits_dry.jsonl
    virtual_positions: list[dict] = []
    orders_f = _orders_file("dry" if dry_run else "live")
    exits_f  = _exits_file("dry" if dry_run else "live")
    if dry_run and orders_f.exists():
        exited = set()
        if exits_f.exists():
            for line in exits_f.read_text(encoding="utf-8").splitlines():
                try:
                    exited.add(json.loads(line)["ticker"])
                except Exception:
                    pass
        seen = set()
        for line in orders_f.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
                t = o.get("ticker", "")
                if (t and t not in seen and t not in exited
                        and o.get("status") == "dry_run"
                        and o.get("contracts", 0) > 0):
                    seen.add(t)
                    virtual_positions.append({
                        "ticker":    t,
                        "position":  o["contracts"],
                        "side":      o.get("side", "yes"),
                        "entry_cents": o.get("price_cents", 0),
                        "_virtual":  True,
                    })
            except Exception:
                pass

    # Build entry-price map from BOTH order files — live first so real Kalshi
    # positions always have their correct entry price even when scanner is in
    # dry-run mode (orders_live.jsonl is authoritative for real positions).
    # Use the MOST RECENT non-addon base order per ticker so that re-entries
    # after a stop-loss use the new entry price, not the old one.
    entry_map: dict[str, dict] = {}
    for f in (_orders_file("live"), _orders_file("dry")):
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    o = json.loads(line)
                    t = o.get("ticker", "")
                    if not t or o.get("contracts", 0) <= 0:
                        continue
                    # Only track base (non-addon) orders for entry price reference.
                    # Always overwrite so the most recent base order wins.
                    if not o.get("is_addon"):
                        entry_map[t] = {
                            "entry_cents": o.get("price_cents", 0),
                            "contracts":   o.get("contracts", 0),
                            "side":        o.get("side", "yes"),
                            "edge":        o.get("edge", 0.0),
                        }
                except Exception:
                    pass

    # Current signal lookup for model-prob-based PT scaling
    sig_lookup = {s["ticker"]: s for s in _state.get("signals", [])}

    # Combine: real first, then virtual for tickers not already in real positions
    real_tickers = {p.get("ticker") for p in real_positions}
    to_check = list(real_positions) + [v for v in virtual_positions if v["ticker"] not in real_tickers]

    for pos in to_check:
        ticker   = pos.get("ticker", "")
        net_pos  = int(float(pos.get("position_fp") or pos.get("position") or 0))
        virtual  = pos.get("_virtual", False)

        if net_pos <= 0:
            continue

        # For real positions: skip if we've already exited this ticker locally.
        # execute_exit() removes from _open_tickers immediately. Without this guard
        # the sell order sits "resting" on Kalshi and get_positions() keeps returning
        # it, causing the exit to re-fire every scan cycle until the order settles.
        if not virtual and ticker not in executor._open_tickers:
            continue

        # Get entry price — virtual positions carry it directly; real ones come from entry_map
        if virtual:
            entry_cents = pos.get("entry_cents", 0)
            side        = pos.get("side", "yes")
        elif ticker in entry_map:
            entry_cents = entry_map[ticker]["entry_cents"]
            side        = entry_map[ticker]["side"]
        else:
            continue

        if entry_cents <= 0:
            continue

        # Cost-weighted exit thresholds
        if virtual:
            cost_usd = entry_cents / 100 * net_pos
        else:
            cost_usd = float(pos.get("market_exposure_dollars", 0)) or entry_cents / 100 * net_pos
        sl_pct, pt_pct = _scale_exits(cost_usd, stop_loss_pct, profit_take_pct, cfg)

        # Model-probability-based PT scaling: use current signal if available,
        # fall back to entry model_prob reconstructed from stored edge + ask
        cur_sig = sig_lookup.get(ticker)
        if cur_sig:
            model_prob = float(cur_sig.get("model_prob", 0.5))
        else:
            em = entry_map.get(ticker, {})
            model_prob = em.get("edge", 0.0) + (entry_cents / 100)
            model_prob = max(0.5, min(0.99, model_prob))
        pt_pct = pt_pct * _model_pt_scale(model_prob)

        with _price_lock:
            bid_dollars = _price_cache.get(ticker, 0.0)
        bid_cents = round(bid_dollars * 100)

        if bid_cents <= 0:
            continue

        entry_dollars = entry_cents / 100
        drop = (entry_dollars - bid_dollars) / entry_dollars
        rise = (bid_dollars - entry_dollars) / entry_dollars
        tag  = "[DRY] " if virtual else ""

        if drop >= sl_pct:
            _log(f"[EXIT] {tag}STOP-LOSS {ticker}: entry={entry_cents}¢ bid={bid_cents}¢ drop={drop:.1%} sl={sl_pct:.0%} (cost=${cost_usd:.2f})")
            r = executor.execute_exit(client, ticker, side, net_pos, entry_cents, bid_cents, "stop_loss", force_live=not virtual)
            _log(f"[EXIT] {r.status} pnl=${r.pnl_usd:.2f} {r.error or ''}")
        elif rise >= pt_pct:
            _log(f"[EXIT] {tag}PROFIT-TAKE {ticker}: entry={entry_cents}¢ bid={bid_cents}¢ rise={rise:.1%} pt={pt_pct:.0%} model={model_prob:.0%} (cost=${cost_usd:.2f})")
            r = executor.execute_exit(client, ticker, side, net_pos, entry_cents, bid_cents, "profit_take", force_live=not virtual)
            _log(f"[EXIT] {r.status} pnl=${r.pnl_usd:.2f} {r.error or ''}")


def _check_double_downs(client: KalshiClient):
    """
    After each live scan, check open positions for double-down eligibility.
    Adds to a position when model confidence has materially increased since entry.
    """
    cfg = _state["config"]
    if not cfg.get("double_down_enabled", False):
        return

    min_conf   = float(cfg.get("double_down_min_conf",  0.75))
    conf_gain  = float(cfg.get("double_down_conf_gain", 0.10))
    max_addons = int(cfg.get("double_down_max_addons",  1))
    max_total  = float(cfg.get("double_down_max_total", 2.0))
    max_bet    = float(cfg.get("max_bet_usd", 25.0))
    max_total_usd = max_total * max_bet

    with _lock:
        sig_by_ticker = {s["ticker"]: s for s in _state["signals"]}

    # Reconstruct entry model confidence from initial order log (non-addon entries only)
    dry_run  = _state["dry_run"]
    orders_f = executor.ORDERS_DRY_FILE if dry_run else executor.ORDERS_LIVE_FILE
    entry_conf: dict[str, float] = {}
    if orders_f.exists():
        for line in orders_f.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
                t = o.get("ticker", "")
                if (t and t not in entry_conf
                        and o.get("contracts", 0) > 0
                        and not o.get("is_addon")):
                    ec   = float(o.get("price_cents", 0)) / 100
                    edge = float(o.get("edge", 0.0))
                    entry_conf[t] = max(0.5, min(0.99, ec + edge))
            except Exception:
                pass

    for ticker in list(executor._open_tickers):
        sig = sig_by_ticker.get(ticker)
        if not sig:
            continue

        current_conf = float(sig.get("model_prob", 0))
        if current_conf < min_conf:
            continue

        base_conf = entry_conf.get(ticker, 0.5)
        if (current_conf - base_conf) < conf_gain:
            continue

        if executor._addon_counts.get(ticker, 0) >= max_addons:
            continue

        if executor._position_cost.get(ticker, 0.0) >= max_total_usd:
            continue

        edge = float(sig.get("edge", 0))
        if edge <= executor.MIN_EDGE:
            continue

        _log(f"[DD] {ticker}: conf={current_conf:.0%} (was {base_conf:.0%}) "
             f"addons={executor._addon_counts.get(ticker,0)} "
             f"cost=${executor._position_cost.get(ticker,0):.2f} — placing add-on")
        try:
            r = executor.execute_addon(
                client=client,
                ticker=ticker,
                player=sig.get("player", ticker),
                side="yes",
                ask_dollars=float(sig.get("kalshi_ask", 0)),
                kelly_usd=float(sig.get("kelly_usd", 0)),
                edge=edge,
            )
            _log(f"[DD] {ticker} add-on — {r.status} {r.error or ''}")
        except Exception as e:
            _log(f"[DD] execute_addon error {ticker}: {e}")


def _parse_period(score_state: str, sport: str) -> str:
    """Return a canonical period label from score_state, e.g. 'Inning 4' or 'Set 2'."""
    if sport == "baseball":
        m = re.match(r'(Top|Bot)\s+(\d+)', score_state or "", re.IGNORECASE)
        if m:
            return f"{'Top' if m.group(1).lower() == 'top' else 'Bot'} {m.group(2)}"
    elif sport == "tennis":
        m = re.match(r'(\d+)-(\d+)\s+sets', score_state or "", re.IGNORECASE)
        if m:
            completed = int(m.group(1)) + int(m.group(2))
            return f"Set {completed + 1}"
    return "Unknown"


def _log_price_history():
    """Append current cached bid prices for open live positions to price_history.jsonl."""
    with _price_lock:
        cache = dict(_price_cache)
    if not cache:
        return
    ts = datetime.now(timezone.utc).isoformat()
    player_map: dict[str, str] = {}
    if ORDERS_LIVE_FILE.exists():
        for line in ORDERS_LIVE_FILE.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
                t = o.get("ticker", "")
                if t and t not in player_map:
                    player_map[t] = o.get("player", "")
            except Exception:
                pass
    with PRICE_HISTORY_FILE.open("a", encoding="utf-8") as fh:
        for ticker, bid_dollars in cache.items():
            fh.write(json.dumps({
                "ts":        ts,
                "ticker":    ticker,
                "player":    player_map.get(ticker, ""),
                "bid_cents": round(bid_dollars * 100),
            }) + "\n")


def _log_portfolio_snapshot(cash_usd: float, live_positions: list[dict]):
    """Write one portfolio value snapshot per scan cycle to portfolio_snapshots.jsonl.

    Unrealized value = sum(bid_price × net_contracts) for all open Kalshi positions.
    Uses the refreshed _price_cache so this must run after the cache update.
    """
    with _price_lock:
        cache = dict(_price_cache)

    unrealized = 0.0
    for pos in live_positions:
        ticker = pos.get("ticker", "")
        net    = int(float(pos.get("position_fp") or pos.get("position") or 0))
        if net > 0 and ticker in cache:
            unrealized += cache[ticker] * net

    total = round(cash_usd + unrealized, 2)
    with PORTFOLIO_SNAP_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts":         datetime.now(timezone.utc).isoformat(),
            "cash_usd":   round(cash_usd, 2),
            "unrealized": round(unrealized, 2),
            "total_usd":  total,
        }) + "\n")


def _log_pregame_snapshots():
    """
    Every 10 minutes, snapshot Kalshi ask prices for all upcoming (pre-game)
    baseball and tennis markets and store them in _pregame_cache + pregame_snapshots.jsonl.
    These become the pregame_ask baseline for any subsequent live entry.
    """
    global _pregame_last_snap
    now = time.time()
    if now - _pregame_last_snap < 600:   # 10-minute throttle
        return
    _pregame_last_snap = now

    try:
        client = _client or KalshiClient(api_key_id=KALSHI_API_KEY)
        dt_now = datetime.now(timezone.utc)
        snaps = []
        for series in ("KXATPMATCH", "KXWTAMATCH", "KXMLBGAME"):
            try:
                resp = client._get("/markets", params={"limit": 200, "series_ticker": series, "status": "open"})
                for m in resp.get("markets", []):
                    if m.get("result") or not m.get("yes_ask_dollars"):
                        continue
                    odt = m.get("occurrence_datetime", "")
                    if not odt:
                        continue
                    try:
                        dt = datetime.fromisoformat(odt.replace("Z", "+00:00"))
                    except Exception:
                        continue
                    # Only snapshot truly pre-game markets (not yet started for tennis;
                    # for baseball use 30-min buffer before occurrence_datetime)
                    hours_until = (dt - dt_now).total_seconds() / 3600
                    if hours_until < 0:
                        continue
                    ticker = m.get("ticker", "")
                    ask    = float(m["yes_ask_dollars"])
                    with _pregame_cache_lock:
                        if ticker not in _pregame_cache:
                            _pregame_cache[ticker] = ask
                    snaps.append({
                        "ts":     dt_now.isoformat(),
                        "ticker": ticker,
                        "series": series,
                        "ask":    ask,
                        "hours_until": round(hours_until, 2),
                    })
            except Exception:
                pass
        if snaps:
            with PREGAME_SNAP_FILE.open("a", encoding="utf-8") as fh:
                for s in snaps:
                    fh.write(json.dumps(s) + "\n")
    except Exception as e:
        _log(f"[PREGAME] snapshot error: {e}")


def _scanner_loop():
    global _client
    _client  = KalshiClient(api_key_id=KALSHI_API_KEY)
    last_arb = 0.0
    executor._load_open_tickers()  # rebuild from logs on each scanner start

    while not _stop_event.is_set():
        cfg = _state["config"]
        now = time.time()
        signals: list[dict] = []
        _sync_executor_config()

        # Arb scan (throttled)
        if HAS_ARB and ODDS_API_KEY and (now - last_arb >= cfg["arb_interval_sec"]):
            try:
                _log("Running arb scan...")
                arb_signals = list(arb_scan(_client, ODDS_API_KEY))
                for s in arb_signals:
                    signals.append(_to_dict(s, "arb"))
                    if s.edge > 0:
                        try:
                            r = executor.execute_arb(_client, s)
                            _log(f"[ARB] {r.player} — {r.status} {r.error or ''}")
                        except Exception as e:
                            _log(f"[ARB] execute error: {e}")
                last_arb = now
                with _lock:
                    _state["last_arb_scan"] = datetime.now(timezone.utc).isoformat()
                _log(f"Arb scan done — {sum(1 for s in signals if s['source']=='arb')} signals")
            except Exception as e:
                _log(f"Arb scan error: {e}")

        # Fetch live positions once per cycle (live mode only).
        # Done BEFORE scan_live so that exits fire on stale positions before
        # the scanner has a chance to re-enter them in the same cycle.
        live_positions: list[dict] = []
        _cycle_cash_usd: float = 0.0
        if not _state["dry_run"]:
            try:
                live_positions = _client.get_positions().get("market_positions", [])
                _cycle_cash_usd = _client.get_balance().get("balance", 0) / 100
                active_on_kalshi = {
                    p["ticker"] for p in live_positions
                    if int(float(p.get("position_fp") or p.get("position") or 0)) > 0
                }
                recently_bought: set[str] = set()
                if executor.ORDERS_LIVE_FILE.exists():
                    _rb_cutoff = time.time() - 300
                    for _rb_line in executor.ORDERS_LIVE_FILE.read_text(encoding="utf-8").splitlines():
                        try:
                            _rb_o = json.loads(_rb_line)
                            _rb_dt = datetime.fromisoformat(_rb_o.get("ts", "").replace("Z", "+00:00"))
                            if _rb_dt.timestamp() > _rb_cutoff:
                                recently_bought.add(_rb_o.get("ticker", ""))
                        except Exception:
                            pass
                stale = {t for t in executor._open_tickers
                         if t not in active_on_kalshi and t not in recently_bought}
                if stale:
                    _log(f"[POSITIONS] pruning {len(stale)} settled tickers: {stale}")
                    executor._open_tickers -= stale
                    executor._open_events -= {executor._event_ticker(t) for t in stale}
                    for t in stale:
                        executor._addon_counts.pop(t, None)
                        executor._position_cost.pop(t, None)
                    with _price_lock:
                        for t in stale:
                            _price_cache.pop(t, None)
                    # Settlement journal — write one row per settled ticker
                    _settle_ts = datetime.now(timezone.utc).isoformat()
                    try:
                        _orders_map: dict[str, dict] = {}
                        if executor.ORDERS_LIVE_FILE.exists():
                            for _sl in executor.ORDERS_LIVE_FILE.read_text(encoding="utf-8").splitlines():
                                try:
                                    _so = json.loads(_sl)
                                    _st = _so.get("ticker", "")
                                    if _st and _st not in _orders_map and _so.get("contracts", 0) > 0:
                                        _orders_map[_st] = _so
                                except Exception:
                                    pass
                        # Dedup: skip tickers already written to avoid duplicate rows on restart
                        _already_settled: set[str] = set()
                        if SETTLEMENTS_FILE.exists():
                            for _line in SETTLEMENTS_FILE.read_text(encoding="utf-8").splitlines():
                                try:
                                    _already_settled.add(json.loads(_line)["ticker"])
                                except Exception:
                                    pass
                        with SETTLEMENTS_FILE.open("a", encoding="utf-8") as _sf:
                            for _st in stale:
                                if _st in _already_settled:
                                    continue
                                _so = _orders_map.get(_st, {})
                                try:
                                    _mkt = _client.get_market(_st)
                                    _res = _mkt.get("result", "")
                                except Exception:
                                    _res = ""
                                _side = _so.get("side", "yes")
                                _won  = (_side == "yes" and _res == "yes") or (_side == "no" and _res == "no")
                                _cost = float(_so.get("cost_usd", 0))
                                _conts = int(_so.get("contracts", 0))
                                _pnl  = round((_conts - _cost) if _won else -_cost, 2) if _cost else 0.0
                                _sf.write(json.dumps({
                                    "ts":           _settle_ts,
                                    "ticker":       _st,
                                    "player":       _so.get("player", ""),
                                    "sport":        _so.get("sport", ""),
                                    "result":       _res,
                                    "side":         _side,
                                    "won":          _won,
                                    "pnl_usd":      _pnl,
                                    "contracts":    _conts,
                                    "entry_cents":  _so.get("price_cents", 0),
                                    "cost_usd":     _cost,
                                    "edge":         _so.get("edge", 0.0),
                                    "model_prob":   _so.get("model_prob", 0.0),
                                    "markov_prob":  _so.get("markov_prob", 0.0),
                                    "espn_win_prob":  _so.get("espn_win_prob", 0.0),
                                    "vegas_live_prob": _so.get("vegas_live_prob", 0.0),
                                    "vegas_open_prob": _so.get("vegas_open_prob", 0.0),
                                    "score_diff":   _so.get("score_diff", 0),
                                    "inning":       _so.get("inning", 0),
                                    "half":         _so.get("half", ""),
                                    "outs":         _so.get("outs", -1),
                                    "on_first":     _so.get("on_first", False),
                                    "on_second":    _so.get("on_second", False),
                                    "on_third":     _so.get("on_third", False),
                                    "pregame_ask":  _so.get("pregame_ask", 0.0),
                                    "home_away":    _so.get("home_away", ""),
                                    "source":       _so.get("source", ""),
                                    "is_live":      _so.get("is_live", True),
                                }) + "\n")
                    except Exception as _se:
                        _log(f"[SETTLE] journal error: {_se}")
            except Exception as e:
                _log(f"[POSITIONS] fetch failed: {e}")

        # Refresh bid price cache for open tickers (used by Orders tab + exit checks)
        if executor._open_tickers:
            fresh: dict[str, float] = {}
            for ticker in list(executor._open_tickers):
                try:
                    m   = _client.get_market(ticker)
                    bid = float(m.get("yes_bid_dollars") or 0)
                    if bid > 0:
                        fresh[ticker] = bid
                except Exception:
                    pass
            with _price_lock:
                _price_cache.update(fresh)

        # Exit check — runs before scan_live so a just-exited ticker gets its SL
        # cooldown recorded before execute_live() can re-enter it this cycle.
        try:
            _check_exits(_client, live_positions)
        except Exception as e:
            _log(f"[EXIT] check error: {e}")

        # Live scan — runs after exits so SL cooldowns are already set
        try:
            _log("Running live scan...")
            live = scan_live(_client)
            for s in live:
                d = _to_dict(s, "live")
                signals.append(d)
                if s.edge > 0:
                    try:
                        with _pregame_cache_lock:
                            _pa = _pregame_cache.get(s.ticker, 0.0)
                        r = executor.execute_live(_client, s, pregame_ask=_pa)
                        d["exec_status"] = r.status
                        d["exec_error"]  = r.error or None
                        _log(f"[LIVE] {r.player} — {r.status} {r.error or ''}")
                    except Exception as e:
                        d["exec_status"] = "error"
                        d["exec_error"]  = str(e)
                        _log(f"[LIVE] execute error: {e}")
                else:
                    d["exec_status"] = None
                    d["exec_error"]  = None
            with _lock:
                _state["signals"]        = signals
                _state["last_live_scan"] = datetime.now(timezone.utc).isoformat()
            _log(f"Live scan done — {len(live)} live signals")

            # Append all signals to today's signal log (buys + skips, full context)
            dry_run = _state["dry_run"]
            sig_f   = SIGNALS_DRY_FILE if dry_run else SIGNALS_LIVE_FILE
            with sig_f.open("a", encoding="utf-8") as fh:
                for d in signals:
                    fh.write(json.dumps(d) + "\n")
        except Exception as e:
            _log(f"Live scan error: {e}")

        # Log price history and portfolio snapshot (live mode only)
        _log_price_history()
        if not _state["dry_run"] and _cycle_cash_usd > 0:
            try:
                _log_portfolio_snapshot(_cycle_cash_usd, live_positions)
            except Exception:
                pass

        # Double-down check — runs if enabled
        try:
            _check_double_downs(_client)
        except Exception as e:
            _log(f"[DD] check error: {e}")

        # Pre-game snapshot (throttled to every 10 min)
        try:
            _log_pregame_snapshots()
        except Exception as e:
            _log(f"[PREGAME] snapshot error: {e}")

        _stop_event.wait(cfg["live_interval_sec"])


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.route("/api/status")
@_require_auth
def api_status():
    with _lock:
        return jsonify({
            "running":        _state["running"],
            "dry_run":        _state["dry_run"],
            "last_live_scan": _state["last_live_scan"],
            "last_arb_scan":  _state["last_arb_scan"],
            "config":         _state["config"],
        })


@app.route("/api/signals")
@_require_auth
def api_signals():
    with _lock:
        return jsonify(_state["signals"])


@app.route("/api/positions")
@_require_auth
def api_positions():
    try:
        c    = _client or KalshiClient(api_key_id=KALSHI_API_KEY)
        resp = c.get_positions()
        raw_markets = resp.get("market_positions", [])
        raw_events  = resp.get("event_positions", [])
        balance_usd = c.get_balance().get("balance", 0) / 100
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Build player/entry-price lookup from live orders (first order per ticker)
    order_meta: dict = {}
    if ORDERS_LIVE_FILE.exists():
        for line in ORDERS_LIVE_FILE.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
                t = o.get("ticker", "")
                if t and t not in order_meta:
                    order_meta[t] = {
                        "player":      o.get("player", ""),
                        "price_cents": o.get("price_cents", 0),
                        "edge":        o.get("edge", 0.0),
                        "opened_at":   o.get("ts", ""),
                        "source":      o.get("source", "live"),
                    }
            except Exception:
                pass

    with _price_lock:
        prices = dict(_price_cache)

    sig_lookup = {s["ticker"]: s for s in _state.get("signals", [])}

    cfg     = _state["config"]
    sl_base = cfg.get("stop_loss_pct", 0.35)
    pt_base = cfg.get("profit_take_pct", 0.50)

    positions = []
    for p in raw_markets:
        ticker    = p.get("ticker", "")
        contracts = int(float(p.get("position_fp", 0)))
        cost_usd  = float(p.get("market_exposure_dollars", 0))
        fees_usd  = float(p.get("fees_paid_dollars", 0))
        realized  = float(p.get("realized_pnl_dollars", 0))
        if contracts <= 0:
            continue
        meta        = order_meta.get(ticker, {})
        entry_cents = meta.get("price_cents", 0)

        cur_sig = sig_lookup.get(ticker)
        if cur_sig:
            model_prob = float(cur_sig.get("model_prob", 0))
            model_source = "current"
        else:
            model_prob = max(0.0, min(0.99, meta.get("edge", 0.0) + entry_cents / 100))
            model_source = "entry"

        bid = prices.get(ticker)
        current_bid_cents = round(bid * 100) if bid else None
        unrealized_pnl = (
            (current_bid_cents - entry_cents) * contracts / 100
            if current_bid_cents and entry_cents else None
        )

        sl_pct, pt_pct = _scale_exits(cost_usd, sl_base, pt_base, cfg)
        positions.append({
            "ticker":            ticker,
            "player":            meta.get("player", ticker),
            "contracts":         contracts,
            "price_cents":       entry_cents,
            "current_bid_cents": current_bid_cents,
            "cost_usd":          cost_usd,
            "fees_usd":          fees_usd,
            "realized_pnl":      realized,
            "unrealized_pnl":    unrealized_pnl,
            "stop_loss_cents":   round(entry_cents * (1 - sl_pct)) if entry_cents else None,
            "profit_take_cents": min(99, round(entry_cents * (1 + pt_pct))) if entry_cents else None,
            "source":            meta.get("source", "live"),
            "opened_at":         meta.get("opened_at", ""),
            "model_prob":        model_prob,
            "model_source":      model_source,
        })

    events = [
        {
            "event_ticker": e.get("event_ticker", ""),
            "cost_usd":     float(e.get("total_cost_dollars", 0)),
            "fees_usd":     float(e.get("fees_paid_dollars", 0)),
            "contracts":    int(float(e.get("total_cost_shares_fp", 0))),
        }
        for e in raw_events
    ]

    return jsonify({"positions": positions, "events": events, "balance_usd": balance_usd})


@app.route("/api/portfolio_value")
@_require_auth
def api_portfolio_value():
    """Return portfolio value snapshots for the equity curve chart."""
    if not PORTFOLIO_SNAP_FILE.exists():
        return jsonify([])
    snaps = []
    for line in PORTFOLIO_SNAP_FILE.read_text(encoding="utf-8").splitlines():
        try:
            s = json.loads(line)
            snaps.append({
                "ts":         s["ts"],
                "cash":       s.get("cash_usd", 0),
                "unrealized": s.get("unrealized", 0),
                "total":      s.get("total_usd", 0),
            })
        except Exception:
            pass
    # Downsample to at most 500 points so the chart stays snappy
    if len(snaps) > 500:
        step = len(snaps) // 500
        snaps = snaps[::step]
    return jsonify(snaps)


@app.route("/api/orders")
@_require_auth
def api_orders():
    mode = request.args.get("mode", "dry")
    f = _orders_file(mode)
    if not f.exists():
        return jsonify([])
    orders = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            orders.append(json.loads(line))
        except Exception:
            pass

    cfg     = _state["config"]
    sl_base = cfg.get("stop_loss_pct", 0.35)
    pt_base = cfg.get("profit_take_pct", 0.50)

    with _price_lock:
        prices = dict(_price_cache)

    sig_lookup = {s["ticker"]: s for s in _state.get("signals", [])}

    for o in orders:
        ticker = o.get("ticker", "")
        bid    = prices.get(ticker)
        o["current_bid_cents"]  = round(bid * 100) if bid else None
        ec = o.get("price_cents", 0)
        if ec > 0:
            sl_pct, pt_pct = _scale_exits(o.get("cost_usd", 0), sl_base, pt_base, cfg)
            o["stop_loss_cents"]   = round(ec * (1 - sl_pct))
            o["profit_take_cents"] = min(99, round(ec * (1 + pt_pct)))
        else:
            o["stop_loss_cents"]   = None
            o["profit_take_cents"] = None

        cur_sig = sig_lookup.get(ticker)
        if cur_sig:
            o["model_prob"]   = float(cur_sig.get("model_prob", 0))
            o["model_source"] = "current"
        else:
            o["model_prob"]   = max(0.0, min(0.99, o.get("edge", 0.0) + ec / 100))
            o["model_source"] = "entry"

    return jsonify(list(reversed(orders[-200:])))


_ALL_LOG_FILES = (
    ORDERS_DRY_FILE,  ORDERS_LIVE_FILE,
    EXITS_DRY_FILE,   EXITS_LIVE_FILE,
    PERF_CACHE_DRY,   PERF_CACHE_LIVE,
    SIGNALS_DRY_FILE, SIGNALS_LIVE_FILE,
    PRICE_HISTORY_FILE, PORTFOLIO_SNAP_FILE,
    PREGAME_SNAP_FILE, SETTLEMENTS_FILE,
)


@app.route("/api/notifications/status")
@_require_auth
def api_notify_status():
    return jsonify({
        "enabled":    notifier.ENABLED,
        "configured": bool(config.PUSHOVER_USER_KEY and config.PUSHOVER_APP_TOKEN),
    })


@app.route("/api/notifications/test", methods=["POST"])
@_require_auth
def api_notify_test():
    ok = notifier.send(
        "Kalshi Trader — Test",
        "Notifications are working. You'll receive alerts for all live trade events.",
    )
    return jsonify({"ok": ok, "enabled": notifier.ENABLED,
                    "configured": bool(config.PUSHOVER_USER_KEY and config.PUSHOVER_APP_TOKEN)})


@app.route("/api/notifications/toggle", methods=["POST"])
@_require_auth
def api_notify_toggle():
    data = request.get_json(silent=True) or {}
    notifier.ENABLED = bool(data.get("enabled", True))
    return jsonify({"ok": True, "enabled": notifier.ENABLED})


@app.route("/api/sell", methods=["POST"])
@_require_auth
def api_sell():
    data       = request.get_json(silent=True) or {}
    ticker     = data.get("ticker", "")
    side       = data.get("side", "yes")
    contracts  = int(data.get("contracts", 0))
    entry_cents = int(data.get("entry_cents", 0))

    if not ticker or contracts <= 0:
        return jsonify({"ok": False, "error": "invalid params"}), 400

    client = _get_client()
    try:
        market    = client.get_market(ticker)
        bid_cents = round(float(market.get("yes_bid_dollars") or 0) * 100)
        if bid_cents <= 0:
            return jsonify({"ok": False, "error": "no bid available — market may be settled"}), 400
        r = executor.execute_exit(client, ticker, side, contracts, entry_cents, bid_cents, "manual")
        return jsonify({
            "ok":         True,
            "status":     r.status,
            "exit_cents": bid_cents,
            "pnl":        round(r.pnl_usd, 2),
            "dry_run":    r.dry_run,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/logs/archive", methods=["POST"])
@_require_auth
def api_logs_archive():
    mode = request.get_json(silent=True, force=True).get("mode", "all") if request.data else "all"
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    moved = []
    targets = _ALL_LOG_FILES if mode == "all" else (
        (_orders_file(mode), _exits_file(mode), _perf_cache_file(mode), _signals_file(mode), PRICE_HISTORY_FILE)
    )
    for f in targets:
        if f.exists():
            dest = f.with_name(f"{f.stem}_{ts}{f.suffix}")
            f.rename(dest)
            moved.append(dest.name)
    executor._load_open_tickers()
    return jsonify({"ok": True, "archived": moved})


@app.route("/api/logs/clear", methods=["POST"])
@_require_auth
def api_logs_clear():
    mode = request.get_json(silent=True, force=True).get("mode", "all") if request.data else "all"
    targets = _ALL_LOG_FILES if mode == "all" else (
        (_orders_file(mode), _exits_file(mode), _perf_cache_file(mode), _signals_file(mode), PRICE_HISTORY_FILE)
    )
    for f in targets:
        if f.exists():
            f.unlink()
    executor._load_open_tickers()
    return jsonify({"ok": True})


@app.route("/api/log")
@_require_auth
def api_log():
    with _lock:
        return jsonify(_state["log"][-150:])


@app.route("/api/analysis")
@_require_auth
def api_analysis():
    mode = request.args.get("mode", "dry")
    f    = _signals_file(mode)
    sigs = []
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                sigs.append(json.loads(line))
            except Exception:
                pass

    total      = len(sigs)
    buy_sigs   = [s for s in sigs if s.get("direction") == "BUY"]
    executed   = [s for s in sigs if s.get("exec_status") in
                  ("dry_run", "submitted", "resting", "executed")]

    # Skip reasons — buy signals that weren't placed
    skip_reasons: dict[str, int] = {}
    for s in buy_sigs:
        if s.get("exec_status") not in ("dry_run", "submitted", "resting", "executed"):
            err = (s.get("exec_error") or s.get("exec_status") or "unknown").strip()
            skip_reasons[err] = skip_reasons.get(err, 0) + 1

    # Sport breakdown
    by_sport: dict[str, dict] = {}
    for s in sigs:
        sport = s.get("sport", "unknown")
        b = by_sport.setdefault(sport, {"total": 0, "buys": 0, "executed": 0, "avg_edge": 0.0, "_edge_sum": 0.0})
        b["total"] += 1
        if s.get("direction") == "BUY":
            b["buys"] += 1
            b["_edge_sum"] += float(s.get("edge", 0))
        if s.get("exec_status") in ("dry_run", "submitted", "resting", "executed"):
            b["executed"] += 1
    for b in by_sport.values():
        b["avg_edge"] = round(b["_edge_sum"] / b["buys"], 4) if b["buys"] else 0
        del b["_edge_sum"]

    # Model confidence distribution
    buckets = {"<50": 0, "50-60": 0, "60-70": 0, "70-80": 0, "80-90": 0, "90+": 0}
    for s in sigs:
        mp = float(s.get("model_prob", 0)) * 100
        if   mp < 50: buckets["<50"]   += 1
        elif mp < 60: buckets["50-60"] += 1
        elif mp < 70: buckets["60-70"] += 1
        elif mp < 80: buckets["70-80"] += 1
        elif mp < 90: buckets["80-90"] += 1
        else:         buckets["90+"]   += 1

    # Edge distribution for buys
    edge_vals = [float(s.get("edge", 0)) for s in buy_sigs]
    avg_edge  = round(sum(edge_vals) / len(edge_vals), 4) if edge_vals else 0
    max_edge  = round(max(edge_vals), 4) if edge_vals else 0

    # Load settlement cache for timing + missed-trades join
    perf_cache = _load_perf_cache(mode)

    # Entry timing: group first exec'd signal per ticker by game period
    executed_first: dict[str, dict] = {}
    for s in sigs:
        if s.get("exec_status") not in ("dry_run", "submitted", "resting", "executed"):
            continue
        t = s.get("ticker", "")
        if t and t not in executed_first:
            executed_first[t] = s

    timing_map: dict[str, dict] = {}
    for s in executed_first.values():
        period = _parse_period(s.get("score_state", ""), s.get("sport", ""))
        result = perf_cache.get(s.get("ticker", ""))
        won    = (result == "yes") if result else None
        b = timing_map.setdefault(period, {
            "period": period, "count": 0, "wins": 0, "settled": 0,
            "avg_edge": 0.0, "_edge_sum": 0.0,
        })
        b["count"] += 1
        b["_edge_sum"] += float(s.get("edge", 0))
        if won is not None:
            b["settled"] += 1
            b["wins"] += int(won)
    for b in timing_map.values():
        b["avg_edge"] = round(b["_edge_sum"] / b["count"], 3) if b["count"] else 0
        b["win_rate"] = round(b["wins"] / b["settled"], 3) if b["settled"] else None
        del b["_edge_sum"]

    def _period_sort_key(p: str) -> tuple:
        half = 0 if p.startswith("Top") or p.startswith("Set") else 1
        m = re.search(r'(\d+)', p)
        return (int(m.group(1)) if m else 99, half)

    timing = sorted(timing_map.values(), key=lambda b: _period_sort_key(b["period"]))

    # Missed trades: positive-edge buy signals skipped for reasons other than "already holding"
    missed_map: dict[str, dict] = {}
    for s in sigs:
        if float(s.get("edge", 0)) <= 0:
            continue
        if s.get("exec_status") in ("dry_run", "submitted", "resting", "executed"):
            continue
        err = (s.get("exec_error") or s.get("exec_status") or "").strip()
        if "already holding" in err:
            continue
        t = s.get("ticker", "")
        if not t:
            continue
        if t not in missed_map:
            missed_map[t] = {
                "ticker":     t,
                "player":     s.get("player", ""),
                "sport":      s.get("sport", ""),
                "ts":         s.get("ts", ""),
                "kalshi_ask": round(float(s.get("kalshi_ask", 0)), 2),
                "model_prob": round(float(s.get("model_prob", 0)), 3),
                "edge":       round(float(s.get("edge", 0)), 3),
                "kelly_usd":  round(float(s.get("kelly_usd", 0)), 2),
                "reason":     err,
                "skip_count": 0,
                "settlement": perf_cache.get(t),
            }
        missed_map[t]["skip_count"] += 1
    missed_trades = sorted(missed_map.values(), key=lambda x: -float(x["kelly_usd"]))[:50]

    return jsonify({
        "summary": {
            "total_signals":   total,
            "buy_signals":     len(buy_sigs),
            "executed":        len(executed),
            "buy_rate":        round(len(buy_sigs) / total, 3) if total else 0,
            "exec_rate":       round(len(executed) / len(buy_sigs), 3) if buy_sigs else 0,
            "avg_edge":        avg_edge,
            "max_edge":        max_edge,
        },
        "by_sport":    by_sport,
        "skip_reasons": dict(sorted(skip_reasons.items(), key=lambda x: -x[1])),
        "conf_buckets": buckets,
        "timing":      timing,
        "missed_trades": missed_trades,
        "signals":     list(reversed(sigs[-500:])),
    })


@app.route("/api/price_history")
@_require_auth
def api_price_history():
    ticker = request.args.get("ticker", "")
    entries: list[dict] = []
    if PRICE_HISTORY_FILE.exists():
        for line in PRICE_HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
                if not ticker or e.get("ticker") == ticker:
                    entries.append(e)
            except Exception:
                pass
    if ticker:
        return jsonify(entries[-500:])
    # No ticker: group by ticker for sparkline summary
    by_ticker: dict[str, dict] = {}
    for e in entries:
        t = e.get("ticker", "")
        if not t:
            continue
        if t not in by_ticker:
            by_ticker[t] = {"ticker": t, "player": e.get("player", ""), "points": [], "first_ts": e.get("ts", "")}
        by_ticker[t]["points"].append(e.get("bid_cents", 0))
    return jsonify(list(by_ticker.values()))


@app.route("/api/start", methods=["POST"])
@_require_auth
def api_start():
    global _scan_thread
    data    = request.get_json(silent=True) or {}
    dry_run = data.get("dry_run", False)

    with _lock:
        if _state["running"]:
            return jsonify({"ok": False, "error": "Already running"})
        _state["running"] = True
        _state["dry_run"] = dry_run
    executor.DRY_RUN = dry_run

    _stop_event.clear()
    _scan_thread = threading.Thread(target=_scanner_loop, daemon=True, name="scanner")
    _scan_thread.start()
    _log(f"Scanner started  dry_run={dry_run}")
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
@_require_auth
def api_stop():
    with _lock:
        if not _state["running"]:
            return jsonify({"ok": False, "error": "Not running"})
        _state["running"] = False
    _stop_event.set()
    _log("Scanner stopped")
    return jsonify({"ok": True})


_CONFIG_BOUNDS = {
    "min_edge":               (0.01, 0.50),
    "max_bet_usd":            (1.0,  500.0),
    "kelly_fraction":         (0.05, 1.0),
    "arb_interval_sec":       (300,  86400),
    "live_interval_sec":      (10,   300),
    "stop_loss_pct":          (0.10, 0.60),
    "profit_take_pct":        (0.20, 0.90),
    "min_ask":                (0.02, 0.25),
    "min_model_prob":         (0.0,  0.95),
    "max_entry_price":        (0.35, 1.0),
    "double_down_min_conf":   (0.55, 0.95),
    "double_down_conf_gain":  (0.05, 0.30),
    "double_down_max_addons": (1,    5),
    "double_down_max_total":  (1.0,  5.0),
}
_CONFIG_BOOLS = {"double_down_enabled", "exits_enabled"}

@app.route("/api/config", methods=["POST"])
@_require_auth
def api_config():
    data = request.get_json(silent=True) or {}
    errors = []
    updates = {}
    for k, v in data.items():
        if k in _CONFIG_BOOLS:
            updates[k] = bool(v)
            continue
        if k not in _CONFIG_BOUNDS:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            errors.append(f"{k}: not a number")
            continue
        lo, hi = _CONFIG_BOUNDS[k]
        if not (lo <= v <= hi):
            errors.append(f"{k}: {v} out of range [{lo}, {hi}]")
            continue
        updates[k] = v
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    with _lock:
        _state["config"].update(updates)
    if "min_edge"    in updates: executor.MIN_EDGE    = updates["min_edge"]
    if "max_bet_usd" in updates: executor.MAX_BET_USD = updates["max_bet_usd"]
    if "double_down_enabled" in updates:
        executor.DOUBLE_DOWN_ENABLED = updates["double_down_enabled"]
    return jsonify({"ok": True, "config": _state["config"]})


# ── Shared helpers ────────────────────────────────────────────────────────────

def _get_client() -> KalshiClient:
    """Return the running scanner client, or create a temporary one."""
    if _client is not None:
        return _client
    return KalshiClient(api_key_id=KALSHI_API_KEY)


def _parse_player(m: dict) -> str:
    import re
    title = m.get("title", "")
    tm    = re.match(r"Will (.+?) win", title)
    sub   = (m.get("yes_sub_title") or m.get("no_sub_title") or "").strip()
    if tm:  return tm.group(1)
    if sub: return sub
    return m.get("ticker", "").split("-")[-1]


def _fetch_kalshi_markets(series: str, now: datetime) -> list[dict]:
    """Fetch open Kalshi markets for a series, tagged with sport and timing."""
    sport = "baseball" if series == "KXMLBGAME" else "tennis"
    try:
        resp = _get_client()._get("/markets", params={
            "limit": 200, "series_ticker": series, "status": "open"
        })
    except Exception as e:
        _log(f"[games] {series} fetch error: {e}")
        return []
    out = []
    for m in resp.get("markets", []):
        odt = m.get("occurrence_datetime", "")
        if not odt or m.get("result") or not m.get("yes_ask_dollars"):
            continue
        dt = datetime.fromisoformat(odt.replace("Z", "+00:00"))
        m["_sport"] = sport
        m["_dt"]    = dt
        m["_live"]  = dt <= now
        out.append(m)
    return out


# ── Live games ─────────────────────────────────────────────────────────────────

_live_now_cache: dict = {"data": [], "ts": 0.0}
_live_now_lock  = threading.Lock()
LIVE_NOW_TTL    = 30  # seconds


def _build_live_games() -> list[dict]:
    from live_scanner import (fetch_espn_tennis, fetch_espn_baseball,
                               find_live_match, find_baseball_game, parse_inning)
    now    = datetime.now(timezone.utc)
    cards: list[dict] = []

    # Tennis: occurrence_datetime = match start, so dt < now is correct.
    # Baseball: occurrence_datetime = settlement deadline (~3h after start),
    #           so we must NOT filter by time — let ESPN confirm liveness instead.
    raw: list[dict] = []
    for series in ("KXATPMATCH", "KXWTAMATCH"):
        for m in _fetch_kalshi_markets(series, now):
            if m["_live"]:
                raw.append(m)
    for m in _fetch_kalshi_markets("KXMLBGAME", now):
        if not m.get("result") and m.get("yes_ask_dollars"):
            raw.append(m)  # include all open baseball markets; ESPN confirms liveness

    # ESPN live scores
    try:
        live_tennis   = fetch_espn_tennis()
        live_baseball = fetch_espn_baseball()
    except Exception as e:
        _log(f"[live_now] ESPN error: {e}")
        return []

    # Snapshot of trading signals for edge overlay
    with _lock:
        sig_by_ticker = {s["ticker"]: s for s in _state["signals"]}

    # Group Kalshi markets by event_ticker
    events: dict[str, list] = {}
    for m in raw:
        key = m.get("event_ticker") or m.get("ticker", "")
        events.setdefault(key, []).append(m)

    for event_key, sides in events.items():
        sport = sides[0]["_sport"]

        parsed_sides = []
        for m in sides:
            player = _parse_player(m)
            sig    = sig_by_ticker.get(m.get("ticker", ""), {})
            parsed_sides.append({
                "ticker":     m.get("ticker", ""),
                "player":     player,
                "ask":        round(float(m["yes_ask_dollars"]), 2),
                "model_prob": sig.get("model_prob"),
                "edge":       sig.get("edge"),
                "direction":  sig.get("direction"),
            })

        # Match ESPN score
        score_state = None
        if sport == "tennis" and parsed_sides:
            espn = None
            for s in parsed_sides:
                espn = find_live_match(s["player"], live_tennis, "p1", "p2")
                if espn:
                    break
            if espn:
                score_state = (f"{espn['sets_p1']}-{espn['sets_p2']} sets  "
                               f"{espn['games_p1']}-{espn['games_p2']} games  "
                               f"({'P1' if espn['p1_serving'] else 'P2'} srv)")
        elif sport == "baseball" and parsed_sides:
            espn = find_baseball_game(parsed_sides[0]["player"], live_baseball)
            if espn:
                inning, is_bot = parse_inning(espn.get("inning_str", ""))
                score_state = (f"{'Bot' if is_bot else 'Top'} {inning}  "
                               f"{espn['score_away']}-{espn['score_home']}")

        # Only include if we have an ESPN score (confirms it's truly in-play)
        if score_state:
            cards.append({
                "event_ticker": event_key,
                "sport":        sport,
                "score_state":  score_state,
                "sides":        parsed_sides,
            })

    return cards


@app.route("/api/live_now")
@_require_auth
def api_live_now():
    with _live_now_lock:
        if time.time() - _live_now_cache["ts"] < LIVE_NOW_TTL:
            return jsonify(_live_now_cache["data"])
    try:
        data = _build_live_games()
    except Exception as e:
        _log(f"[live_now] build error: {e}")
        return jsonify([])
    with _live_now_lock:
        _live_now_cache["data"] = data
        _live_now_cache["ts"]   = time.time()
    return jsonify(data)


# ── Upcoming games ─────────────────────────────────────────────────────────────

_upcoming_cache: dict = {"data": [], "ts": 0.0}
_upcoming_lock  = threading.Lock()
UPCOMING_TTL    = 120   # seconds before refreshing from Kalshi
UPCOMING_WINDOW = 48    # hours ahead to show


def _fetch_upcoming() -> list[dict]:
    """Fetch Kalshi markets starting in the next UPCOMING_WINDOW hours."""
    now = datetime.now(timezone.utc)
    raw: list[dict] = []
    for series in ("KXATPMATCH", "KXWTAMATCH", "KXMLBGAME"):
        for m in _fetch_kalshi_markets(series, now):
            hours_until = (m["_dt"] - now).total_seconds() / 3600
            if 0 < hours_until <= UPCOMING_WINDOW:
                m["_hours_until"] = hours_until
                raw.append(m)

    events: dict[str, list] = {}
    for m in raw:
        key = m.get("event_ticker") or m.get("ticker", "")
        events.setdefault(key, []).append(m)

    result: list[dict] = []
    for event_key, sides in events.items():
        sides.sort(key=lambda m: m["_dt"])
        first      = sides[0]
        dt         = first["_dt"]
        mins_until = int((dt - now).total_seconds() / 60)
        parsed_sides = []
        for m in sides:
            ask = m.get("yes_ask_dollars")
            bid = m.get("yes_bid_dollars")
            parsed_sides.append({
                "ticker": m.get("ticker", ""),
                "player": _parse_player(m),
                "ask":    round(float(ask), 2) if ask else None,
                "bid":    round(float(bid), 2) if bid else None,
            })
        result.append({
            "event_ticker": event_key,
            "sport":        first["_sport"],
            "starts_at":    dt.isoformat(),
            "mins_until":   mins_until,
            "sides":        parsed_sides,
        })

    result.sort(key=lambda e: e["mins_until"])
    return result


@app.route("/api/upcoming")
@_require_auth
def api_upcoming():
    with _upcoming_lock:
        if time.time() - _upcoming_cache["ts"] < UPCOMING_TTL:
            return jsonify(_upcoming_cache["data"])

    try:
        data = _fetch_upcoming()
    except Exception as e:
        _log(f"[upcoming] fetch failed: {e}")
        return jsonify([])

    with _upcoming_lock:
        _upcoming_cache["data"] = data
        _upcoming_cache["ts"]   = time.time()
    return jsonify(data)


# ── Performance tracker ───────────────────────────────────────────────────────

def _load_perf_cache(mode: str) -> dict:
    f = _perf_cache_file(mode)
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_perf_cache(cache: dict, mode: str):
    _perf_cache_file(mode).write_text(json.dumps(cache, indent=2), encoding="utf-8")


@app.route("/api/performance")
@_require_auth
def api_performance():
    mode = request.args.get("mode", "dry")  # 'dry' or 'live'

    # Load orders for this mode
    all_orders: list[dict] = []
    f = _orders_file(mode)
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                all_orders.append(json.loads(line))
            except Exception:
                pass

    # For dry: only confirmed dry_run fills. For live: only real fills.
    if mode == "dry":
        candidates = [
            o for o in all_orders
            if o.get("contracts", 0) > 0
            and o.get("status") == "dry_run"
        ]
    else:
        candidates = [
            o for o in all_orders
            if o.get("contracts", 0) > 0
            and o.get("status") in ("submitted", "resting", "filled", "executed")
        ]

    # Load exits — these take priority over settlement for resolving orders
    exits_by_ticker: dict[str, dict] = {}
    ef = _exits_file(mode)
    if ef.exists():
        for line in ef.read_text(encoding="utf-8").splitlines():
            try:
                x = json.loads(line)
                t = x.get("ticker", "")
                if t and t not in exits_by_ticker and x.get("status") in ("dry_run", "submitted", "resting", "filled"):
                    exits_by_ticker[t] = x
            except Exception:
                pass

    # Load and refresh settlement cache for non-exited orders
    perf_cache = _load_perf_cache(mode)
    unresolved_tickers = {
        o["ticker"] for o in candidates
        if o["ticker"] not in exits_by_ticker
        and o["ticker"] not in perf_cache
    }

    if unresolved_tickers:
        client = _get_client()
        for ticker in unresolved_tickers:
            try:
                m      = client.get_market(ticker)
                result = m.get("result", "")
                if result:
                    perf_cache[ticker] = result
            except Exception:
                pass
        _save_perf_cache(perf_cache, mode)

    # Build resolved / pending lists
    resolved_orders: list[dict] = []
    pending_orders:  list[dict] = []

    for o in candidates:
        ticker = o.get("ticker", "")

        if ticker in exits_by_ticker:
            # Resolved via exit (stop-loss, profit-take, or manual sell)
            x      = exits_by_ticker[ticker]
            pnl    = round(x.get("pnl_usd", 0), 2)
            won    = pnl > 0
            reason = x.get("reason", "exited")
            resolved_orders.append({
                **o,
                "result":      reason,
                "result_type": "exit",
                "exit_cents":  x.get("exit_cents"),
                "won":         won,
                "pnl":         pnl,
            })
        elif perf_cache.get(ticker):
            # Resolved via Kalshi market settlement
            result = perf_cache[ticker]
            side   = o.get("side", "yes")
            won    = (side == "yes" and result == "yes") or \
                     (side == "no"  and result == "no")
            cost   = o.get("cost_usd", 0)
            pnl    = round((o["contracts"] - cost) if won else -cost, 2)
            resolved_orders.append({
                **o,
                "result":      result,
                "result_type": "settlement",
                "won":         won,
                "pnl":         pnl,
            })
        else:
            pending_orders.append(o)

    # Summary stats
    total_cost = sum(o.get("cost_usd", 0) for o in resolved_orders)
    total_pnl  = sum(o["pnl"] for o in resolved_orders)
    wins       = sum(1 for o in resolved_orders if o["won"])
    n          = len(resolved_orders)

    # Edge bucket analysis — did higher edge bets actually win more?
    buckets: dict[str, dict] = {}
    for o in resolved_orders:
        edge = o.get("edge", 0)
        if edge < 0.05:   label = "3-5%"
        elif edge < 0.10: label = "5-10%"
        elif edge < 0.20: label = "10-20%"
        else:             label = "20%+"
        b = buckets.setdefault(label, {"bets": 0, "wins": 0, "pnl": 0.0})
        b["bets"] += 1
        b["wins"] += int(o["won"])
        b["pnl"]  += o["pnl"]

    # Model calibration: stated model_prob at entry vs actual win rate
    model_prob_map: dict[str, float] = {}
    sig_f = _signals_file(mode)
    if sig_f.exists():
        for line in sig_f.read_text(encoding="utf-8").splitlines():
            try:
                s = json.loads(line)
                t = s.get("ticker", "")
                if t and t not in model_prob_map and s.get("exec_status") in (
                        "dry_run", "submitted", "resting", "executed"):
                    model_prob_map[t] = float(s.get("model_prob", 0))
            except Exception:
                pass

    _CAL_LABELS = ["50-55", "55-60", "60-65", "65-70", "70-75",
                   "75-80", "80-85", "85-90", "90-95", "95+"]
    _cal: dict[str, dict] = {lb: {"count": 0, "wins": 0, "_s": 0.0} for lb in _CAL_LABELS}
    for o in resolved_orders:
        mp = model_prob_map.get(o.get("ticker", ""))
        if mp is None:
            continue
        p = mp * 100
        if   p < 50: continue
        elif p < 55: lb = "50-55"
        elif p < 60: lb = "55-60"
        elif p < 65: lb = "60-65"
        elif p < 70: lb = "65-70"
        elif p < 75: lb = "70-75"
        elif p < 80: lb = "75-80"
        elif p < 85: lb = "80-85"
        elif p < 90: lb = "85-90"
        elif p < 95: lb = "90-95"
        else:        lb = "95+"
        _cal[lb]["count"] += 1
        _cal[lb]["wins"]  += int(o["won"])
        _cal[lb]["_s"]    += mp
    calibration = []
    for lb in _CAL_LABELS:
        b = _cal[lb]
        if b["count"]:
            calibration.append({
                "label":           lb + "%",
                "count":           b["count"],
                "avg_model_prob":  round(b["_s"] / b["count"], 3),
                "actual_win_rate": round(b["wins"] / b["count"], 3),
            })

    return jsonify({
        "mode": mode,
        "summary": {
            "total_bets": len(candidates),
            "resolved":   n,
            "pending":            len(pending_orders),
            "wins":               wins,
            "losses":             n - wins,
            "win_rate":           round(wins / n, 3) if n else 0,
            "total_invested":     round(total_cost, 2),
            "total_pnl":          round(total_pnl, 2),
            "roi":                round(total_pnl / total_cost, 3) if total_cost else 0,
        },
        "edge_buckets": buckets,
        "calibration": calibration,
        "resolved":  sorted(resolved_orders, key=lambda x: x["ts"], reverse=True)[:100],
        "pending":   sorted(pending_orders,  key=lambda x: x["ts"], reverse=True)[:50],
    })


@app.route("/")
@_require_auth
def index():
    return render_template_string(HTML)


# ── HTML (single-page app) ────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi Trader</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0d0d0d;color:#e0e0e0;font-family:'Courier New',monospace;font-size:13px;min-height:100vh}

  /* Header */
  .hdr{background:#111;border-bottom:1px solid #2a2a2a;padding:12px 24px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:10}
  .hdr h1{font-size:16px;color:#fff;letter-spacing:.05em}
  .dot{width:10px;height:10px;border-radius:50%;background:#444;flex-shrink:0}
  .dot.on{background:#00ff88;box-shadow:0 0 8px #00ff88}
  .dot.off{background:#ff4444}
  .badge{padding:3px 9px;border-radius:10px;font-size:10px;letter-spacing:.06em;text-transform:uppercase}
  .badge.dry{background:#2a2a2a;color:#888}
  .badge.live{background:#ff4444;color:#fff}
  .hdr-right{margin-left:auto;font-size:11px;color:#444}

  /* Tabs */
  .tabs{display:flex;background:#111;border-bottom:1px solid #2a2a2a}
  .tab{padding:11px 22px;cursor:pointer;color:#555;border-bottom:2px solid transparent;font-size:12px;text-transform:uppercase;letter-spacing:.06em;transition:color .15s}
  .tab.active{color:#00ff88;border-bottom-color:#00ff88}
  .tab:hover:not(.active){color:#aaa}

  /* Content */
  .content{padding:20px 24px;max-width:1600px}
  .panel{display:none}
  .panel.active{display:block}

  /* Stat cards */
  .stats{display:flex;gap:14px;margin-bottom:20px;flex-wrap:wrap}
  .card{background:#141414;border:1px solid #2a2a2a;border-radius:6px;padding:14px 20px;min-width:130px}
  .card .lbl{font-size:10px;color:#555;text-transform:uppercase;letter-spacing:.06em}
  .card .val{font-size:24px;color:#fff;margin-top:6px;font-weight:bold}
  .card .val.green{color:#00ff88}
  .card .val.yellow{color:#ffcc00}

  /* Tables */
  .tbl-wrap{overflow-x:auto}
  table{width:100%;border-collapse:collapse;white-space:nowrap}
  th{text-align:left;padding:8px 12px;color:#444;font-weight:normal;border-bottom:1px solid #222;font-size:10px;text-transform:uppercase;letter-spacing:.06em}
  td{padding:9px 12px;border-bottom:1px solid #1a1a1a;vertical-align:middle}
  tr:hover td{background:#161616}
  .empty{color:#444;text-align:center;padding:40px;font-size:12px}

  /* Colors */
  .green{color:#00ff88}
  .red{color:#ff6b6b}
  .dim{color:#555}
  .yellow{color:#ffcc00}
  .buy{color:#00ff88;font-weight:bold}
  .skip{color:#444}

  /* Buttons */
  .btn{padding:9px 18px;border:none;border-radius:4px;cursor:pointer;font-size:12px;font-family:inherit;letter-spacing:.04em;transition:opacity .15s}
  .btn:hover{opacity:.85}
  .btn:disabled{opacity:.3;cursor:not-allowed}
  .btn-green{background:#00ff88;color:#000;font-weight:bold}
  .btn-orange{background:#ff9900;color:#000;font-weight:bold}
  .btn-red{background:#ff4444;color:#fff}
  .btn-gray{background:#2a2a2a;color:#ccc}

  /* Controls bar */
  .controls{display:flex;gap:10px;align-items:center;margin-bottom:24px;flex-wrap:wrap}

  /* Log terminal */
  .log-box{background:#080808;border:1px solid #222;border-radius:4px;padding:12px;height:460px;overflow-y:auto;font-size:11.5px;color:#00cc66;line-height:1.6}
  .log-box div:nth-child(odd){color:#009944}

  /* Config form */
  .cfg-row{display:flex;align-items:center;gap:16px;margin-bottom:18px}
  .cfg-row label{color:#666;width:170px;font-size:12px}
  .cfg-row input[type=range]{flex:1;max-width:220px;accent-color:#00ff88}
  .cfg-row .cfg-val{color:#fff;width:70px;font-size:13px}
  .cfg-section{color:#444;font-size:10px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:14px;margin-top:8px}

  /* Status row in settings */
  .setting-group{background:#141414;border:1px solid #2a2a2a;border-radius:6px;padding:18px 20px;margin-bottom:18px}
  .setting-group h3{font-size:11px;color:#555;text-transform:uppercase;letter-spacing:.08em;margin-bottom:16px}

  /* Source tag */
  .src{font-size:10px;padding:2px 7px;border-radius:3px;background:#1e1e1e;color:#666}
  .src.live{color:#00ff88;background:#001a0d}
  .src.arb{color:#ffcc00;background:#1a1500}

  /* Status badge */
  .st{font-size:11px}
  .st.dry_run{color:#888}
  .mode-bar{display:flex;gap:6px;margin-bottom:16px}
  .mode-btn{padding:5px 16px;border:1px solid #333;background:#111;color:#888;border-radius:4px;cursor:pointer;font-size:12px;letter-spacing:.04em}
  .mode-btn.active{background:#1a2a1a;border-color:#4caf50;color:#4caf50}
  .st.submitted,.st.resting{color:#00ff88}
  .st.skipped,.st.error{color:#ff6b6b}

  /* Section headers */
  .section-hdr{font-size:11px;color:#00ff88;text-transform:uppercase;letter-spacing:.1em;padding-bottom:10px;border-bottom:1px solid #1a3a1a;margin-bottom:12px;display:flex;align-items:center;gap:8px}
  .section-count{color:#444;font-size:10px}
  .live-dot{width:8px;height:8px;border-radius:50%;background:#ff4444;animation:pulse 1.2s infinite;flex-shrink:0}
  @keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(255,68,68,.6)}50%{box-shadow:0 0 0 5px rgba(255,68,68,0)}}

  /* Live game cards */
  .live-card{background:#0f1a0f;border:1px solid #1a3a1a;border-left:3px solid #00ff88;border-radius:5px;padding:14px 16px;margin-bottom:8px}
  .live-card-header{display:flex;align-items:center;gap:8px;margin-bottom:10px}
  .live-badge{font-size:9px;background:#ff4444;color:#fff;padding:2px 7px;border-radius:3px;letter-spacing:.08em;animation:pulse 1.2s infinite}
  .live-sport{font-size:10px;color:#444;text-transform:uppercase;letter-spacing:.06em}
  .live-score{margin-left:auto;font-size:11px;color:#00ff88;background:#001a0d;padding:3px 10px;border-radius:3px;font-family:'Courier New',monospace}
  .live-teams{display:flex;align-items:stretch;gap:0}
  .live-side{flex:1;padding:8px 12px;background:#0a0a0a;border-radius:4px}
  .live-side.right{text-align:right}
  .live-side-name{font-size:13px;color:#ddd;margin-bottom:4px}
  .live-side-ask{font-size:11px;color:#555}
  .live-side-ask span{color:#aaa}
  .live-side-model{font-size:11px;margin-top:2px}
  .live-side-edge{font-size:11px;font-weight:bold;margin-top:2px}
  .live-vs{display:flex;align-items:center;padding:0 12px;color:#222;font-size:12px;flex-shrink:0}

  /* Upcoming fixtures */
  .fixture-group{margin-bottom:10px}
  .fixture-group-header{font-size:10px;color:#444;text-transform:uppercase;letter-spacing:.08em;padding:6px 0 4px;border-bottom:1px solid #1e1e1e;margin-bottom:4px}
  .fixture{display:flex;align-items:center;background:#111;border:1px solid #1e1e1e;border-radius:5px;padding:12px 16px;margin-bottom:6px;gap:0;transition:border-color .15s}
  .fixture:hover{border-color:#2a2a2a}
  .fixture.soon{border-left:3px solid #ffcc00}
  .fixture.imminent{border-left:3px solid #ff6b6b}
  .fix-sport{width:52px;font-size:10px;color:#555;text-transform:uppercase;letter-spacing:.05em;flex-shrink:0}
  .fix-teams{flex:1;display:flex;align-items:center;gap:10px}
  .fix-side{flex:1}
  .fix-name{font-size:13px;color:#ddd}
  .fix-price{font-size:12px;color:#555;margin-top:2px}
  .fix-price span{color:#aaa}
  .fix-vs{color:#333;font-size:12px;flex-shrink:0;padding:0 4px}
  .fix-time{width:110px;text-align:right;flex-shrink:0}
  .fix-countdown{font-size:13px;font-weight:bold}
  .fix-countdown.near{color:#ffcc00}
  .fix-countdown.hot{color:#ff6b6b}
  .fix-countdown.ok{color:#555}
  .fix-date{font-size:10px;color:#444;margin-top:2px}
  .fix-sport-icon{font-size:15px;margin-right:4px}
  .no-upcoming{color:#444;text-align:center;padding:40px;font-size:12px}

  /* Calibration chart */
  .cal-row{display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:11px}
  .cal-label{width:55px;color:#666;text-align:right;flex-shrink:0}
  .cal-bars{flex:1;position:relative;height:18px;background:#111;border-radius:3px;overflow:hidden}
  .cal-bar-expected{position:absolute;height:100%;background:#1a3a1a;border-radius:3px}
  .cal-bar-actual{position:absolute;height:100%;background:#00ff88;opacity:.8;border-radius:3px;top:0}
  .cal-stat{width:80px;font-size:10px;color:#666;flex-shrink:0}
  /* Sparkline */
  .spark{display:inline-block;vertical-align:middle}

  /* Analysis 3-col grid */
  .analysis-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-top:16px}

  /* ── Mobile / responsive ───────────────────────────────────────────────── */
  @media (max-width:768px){
    /* Scrollable tab bar — no wrapping, swipe horizontally */
    .tabs{overflow-x:auto;-webkit-overflow-scrolling:touch;scroll-behavior:smooth}
    .tab{padding:10px 16px;flex-shrink:0;white-space:nowrap}

    /* Tighter content */
    .content{padding:14px 12px}

    /* Header: drop the last-scan timestamp to save space */
    .hdr-right{display:none}
    .hdr{padding:10px 14px;gap:10px}

    /* Settings sliders: stack label above slider instead of side-by-side */
    .cfg-row{flex-wrap:wrap;gap:6px}
    .cfg-row label{width:100%}
    .cfg-row input[type=range]{max-width:100%}

    /* Analysis 3-col → 1-col */
    .analysis-grid{grid-template-columns:1fr}

    /* Log terminal shorter on phone */
    .log-box{height:300px}

    /* Cards: tighter padding */
    .card{padding:10px 14px;min-width:100px}
    .card .val{font-size:20px}

    /* Fixtures: let time column wrap below team names on small screens */
    .fixture{flex-wrap:wrap}
    .fix-time{width:100%;text-align:left;margin-top:6px;padding-left:56px}
  }

  @media (max-width:480px){
    .hdr h1{font-size:14px}
    .tab{padding:9px 12px;font-size:11px}
    .content{padding:10px 8px}
    .card{padding:8px 12px;min-width:82px}
    .card .val{font-size:17px}
    .btn{padding:8px 14px;font-size:11px}
    .setting-group{padding:14px 14px}
    .fix-sport{width:40px}
    .fix-time{padding-left:44px}
  }
</style>
</head>
<body>

<div class="hdr">
  <div class="dot off" id="dot"></div>
  <h1>KALSHI TRADER</h1>
  <span class="badge dry" id="badge">DRY RUN</span>
  <span class="hdr-right" id="lastScan"></span>
</div>

<div class="tabs">
  <div class="tab active" onclick="showTab('signals')">Signals</div>
  <div class="tab" onclick="showTab('upcoming')">Games</div>
  <div class="tab" onclick="showTab('positions')">Portfolio</div>
  <div class="tab" onclick="showTab('orders')">Orders</div>
  <div class="tab" onclick="showTab('performance')">Performance</div>
  <div class="tab" onclick="showTab('analysis')">Analysis</div>
  <div class="tab" onclick="showTab('log')">Log</div>
  <div class="tab" onclick="showTab('settings')">Settings</div>
</div>

<div class="content">

  <!-- SIGNALS -->
  <div class="panel active" id="panel-signals">
    <div class="stats">
      <div class="card"><div class="lbl">Total Signals</div><div class="val" id="s-total">-</div></div>
      <div class="card"><div class="lbl">BUY Signals</div><div class="val green" id="s-buys">-</div></div>
      <div class="card"><div class="lbl">Best Edge</div><div class="val yellow" id="s-edge">-</div></div>
      <div class="card"><div class="lbl">Live</div><div class="val" id="s-live">-</div></div>
      <div class="card"><div class="lbl">Arb</div><div class="val" id="s-arb">-</div></div>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>Player / Team</th><th>Opponent</th><th>Sport</th><th>Src</th>
          <th>Ask</th><th>Model</th><th>Edge</th><th>Kelly $</th>
          <th>Live State</th><th>Action</th>
        </tr></thead>
        <tbody id="sig-body"></tbody>
      </table>
    </div>
  </div>

  <!-- UPCOMING / GAMES -->
  <div class="panel" id="panel-upcoming">
    <!-- Live now section -->
    <div id="live-now-section">
      <div class="section-hdr">
        <span class="live-dot"></span> LIVE NOW
        <span id="live-count" class="section-count"></span>
      </div>
      <div id="live-now-body"><div class="no-upcoming dim" style="padding:16px 0">Checking for live games...</div></div>
    </div>

    <!-- Upcoming section -->
    <div style="margin-top:24px">
      <div class="section-hdr" style="border-color:#2a2a2a;color:#444">
        UPCOMING &mdash; NEXT 48H
        <span id="u-count" class="section-count"></span>
      </div>
      <div class="stats" style="margin-top:12px">
        <div class="card"><div class="lbl">Starting Soon (&lt;2h)</div><div class="val yellow" id="u-soon">-</div></div>
        <div class="card"><div class="lbl">Tennis</div><div class="val" id="u-tennis">-</div></div>
        <div class="card"><div class="lbl">Baseball</div><div class="val" id="u-baseball">-</div></div>
      </div>
      <div id="upcoming-body"></div>
    </div>
  </div>

  <!-- PORTFOLIO -->
  <div class="panel" id="panel-positions">
    <div class="stats">
      <div class="card"><div class="lbl">Open Positions</div><div class="val" id="p-count">-</div></div>
      <div class="card"><div class="lbl">Total Invested</div><div class="val yellow" id="p-invested">-</div></div>
      <div class="card"><div class="lbl">Total Contracts</div><div class="val" id="p-contracts">-</div></div>
      <div class="card"><div class="lbl">Fees Paid</div><div class="val red" id="p-fees">-</div></div>
      <div class="card"><div class="lbl">Unrealized P&amp;L</div><div class="val" id="p-unrealized">-</div></div>
      <div class="card"><div class="lbl">Cash Available</div><div class="val green" id="p-cash">-</div></div>
    </div>

    <div style="margin:18px 0 8px;font-size:12px;color:#888;text-transform:uppercase;letter-spacing:.08em">Exposure by Event</div>
    <div id="p-event-bars" style="margin-bottom:24px"></div>

    <div style="margin:18px 0 8px;font-size:12px;color:#888;text-transform:uppercase;letter-spacing:.08em">Open Positions</div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>Player</th><th>Contracts</th><th>Entry</th>
          <th title="Latest yes_bid from scanner">Current</th>
          <th title="Unrealized P&L based on current bid">Unreal P&L</th>
          <th>Invested</th><th>Fees</th>
          <th title="Stop-loss / Profit-take trigger prices (cost-scaled)">SL / PT</th>
          <th title="Model win probability">Conf</th>
          <th>Opened</th>
        </tr></thead>
        <tbody id="pos-body"></tbody>
      </table>
    </div>
  </div>

  <!-- ORDERS -->
  <div class="panel" id="panel-orders">
    <div class="mode-bar">
      <button class="mode-btn active" id="ord-btn-dry"  onclick="switchOrders('dry')">Paper Trading</button>
      <button class="mode-btn"        id="ord-btn-live" onclick="switchOrders('live')">Live Trading</button>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>Time</th><th>Player</th><th>Side</th><th>Qty</th>
          <th>Entry</th><th>Cost</th><th>Edge</th>
          <th title="Current yes_bid from last scan">Current</th>
          <th title="Stop-loss ↓ / Profit-take ↑ trigger prices">SL↓ / PT↑</th>
          <th title="Model win probability (current if scanner active, entry estimate otherwise)">Conf</th>
          <th>Status</th><th>Src</th><th title="ESPN link">ESPN</th>
          <th title="Bid price history sparkline">Price Path</th><th></th>
        </tr></thead>
        <tbody id="ord-body"></tbody>
      </table>
    </div>
  </div>

  <!-- PERFORMANCE -->
  <div class="panel" id="panel-performance">

    <!-- Portfolio equity curve (live only) -->
    <div id="portfolio-chart-wrap" style="margin-bottom:24px;display:none">
      <div style="display:flex;align-items:baseline;gap:12px;margin-bottom:8px">
        <span style="font-size:10px;color:#444;text-transform:uppercase;letter-spacing:.08em">Portfolio Value</span>
        <span id="port-current" style="font-size:18px;font-weight:bold;color:#4caf50">—</span>
        <span id="port-change"  style="font-size:12px;color:#888"></span>
      </div>
      <div style="position:relative;height:160px">
        <canvas id="portfolio-chart"></canvas>
      </div>
    </div>

    <div class="mode-bar">
      <button class="mode-btn active" id="pf-btn-dry"  onclick="switchPerf('dry')">Paper Trading</button>
      <button class="mode-btn"        id="pf-btn-live" onclick="switchPerf('live')">Live Trading</button>
    </div>
    <div class="stats" id="perf-stats">
      <div class="card"><div class="lbl" id="pf-lbl-total">Paper Bets</div><div class="val" id="pf-total">-</div></div>
      <div class="card"><div class="lbl">Resolved</div><div class="val" id="pf-resolved">-</div></div>
      <div class="card"><div class="lbl">Win Rate</div><div class="val" id="pf-winrate">-</div></div>
      <div class="card"><div class="lbl" id="pf-lbl-pnl">Hypothetical P&amp;L</div><div class="val" id="pf-pnl">-</div></div>
      <div class="card"><div class="lbl">ROI</div><div class="val" id="pf-roi">-</div></div>
    </div>

    <div style="margin:20px 0 8px;font-size:10px;color:#444;text-transform:uppercase;letter-spacing:.08em">Edge Bucket Analysis</div>
    <div id="perf-buckets" style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:24px"></div>

    <!-- Model Calibration -->
    <div class="setting-group" style="margin-top:16px">
      <h3>Model Calibration
        <span style="font-size:11px;font-weight:normal;color:#666;margin-left:8px">
          green bar = actual win rate &nbsp;|&nbsp; dark bar = model probability (expected)
        </span>
      </h3>
      <div id="perf-cal-body"><div class="dim" style="font-size:12px;padding:8px 0">No resolved trades with signal data yet.</div></div>
    </div>

    <div style="margin-bottom:8px;font-size:10px;color:#444;text-transform:uppercase;letter-spacing:.08em">
      Resolved Bets <span style="color:#555" id="pf-pending-note"></span>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>Time</th><th>Player</th><th>Side</th><th>Qty</th>
          <th>Cost</th><th>Edge</th><th>Result</th><th>P&amp;L</th><th>Src</th>
        </tr></thead>
        <tbody id="perf-body"></tbody>
      </table>
    </div>
  </div>

  <!-- ANALYSIS -->
  <div class="panel" id="panel-analysis">
    <div class="mode-bar">
      <button class="mode-btn active" id="an-btn-dry"  onclick="switchAnalysis('dry')">Paper</button>
      <button class="mode-btn"        id="an-btn-live" onclick="switchAnalysis('live')">Live</button>
    </div>

    <div class="stats" id="an-stats">
      <div class="card"><div class="lbl">Signals Logged</div><div class="val" id="an-total">-</div></div>
      <div class="card"><div class="lbl">Buy Signals</div><div class="val" id="an-buys">-</div></div>
      <div class="card"><div class="lbl">Executed</div><div class="val" id="an-exec">-</div></div>
      <div class="card"><div class="lbl">Buy Rate</div><div class="val" id="an-buyrate">-</div></div>
      <div class="card"><div class="lbl">Exec Rate</div><div class="val" id="an-execrate">-</div></div>
      <div class="card"><div class="lbl">Avg Edge (buys)</div><div class="val green" id="an-avgedge">-</div></div>
    </div>

    <div class="analysis-grid">

      <div class="setting-group" style="margin:0">
        <h3>By Sport</h3>
        <table style="width:100%;font-size:12px;border-collapse:collapse">
          <thead><tr style="color:#888;text-align:left">
            <th style="padding:4px 6px">Sport</th>
            <th style="padding:4px 6px;text-align:right">Total</th>
            <th style="padding:4px 6px;text-align:right">Buys</th>
            <th style="padding:4px 6px;text-align:right">Placed</th>
            <th style="padding:4px 6px;text-align:right">Avg Edge</th>
          </tr></thead>
          <tbody id="an-sport-body"><tr><td colspan="5" class="empty">—</td></tr></tbody>
        </table>
      </div>

      <div class="setting-group" style="margin:0">
        <h3>Skip Reasons</h3>
        <table style="width:100%;font-size:12px;border-collapse:collapse">
          <thead><tr style="color:#888;text-align:left">
            <th style="padding:4px 6px">Reason</th>
            <th style="padding:4px 6px;text-align:right">Count</th>
          </tr></thead>
          <tbody id="an-skip-body"><tr><td colspan="2" class="empty">—</td></tr></tbody>
        </table>
      </div>

      <div class="setting-group" style="margin:0">
        <h3>Confidence Distribution</h3>
        <div id="an-conf-bars" style="font-size:12px"></div>
      </div>

    </div>

    <!-- Entry Timing -->
    <div class="setting-group" style="margin-top:16px">
      <h3>Entry Timing — Win Rate by Game Period</h3>
      <div style="overflow-x:auto">
        <table style="width:100%;font-size:12px;border-collapse:collapse">
          <thead><tr style="color:#888;text-align:left;border-bottom:1px solid #333">
            <th style="padding:5px 8px">Period</th>
            <th style="padding:5px 8px;text-align:right">Entries</th>
            <th style="padding:5px 8px;text-align:right">Avg Edge</th>
            <th style="padding:5px 8px;text-align:right">Settled</th>
            <th style="padding:5px 8px;text-align:right">Win Rate</th>
            <th style="padding:5px 8px">Win Bar</th>
          </tr></thead>
          <tbody id="an-timing-body"><tr><td colspan="6" class="empty">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- Missed Trades -->
    <div class="setting-group" style="margin-top:16px">
      <h3>Missed Trades — Skipped Opportunities with Positive Edge</h3>
      <div style="overflow-x:auto">
        <table style="width:100%;font-size:12px;border-collapse:collapse">
          <thead><tr style="color:#888;text-align:left;border-bottom:1px solid #333">
            <th style="padding:5px 8px">Player</th>
            <th style="padding:5px 8px">Sport</th>
            <th style="padding:5px 8px;text-align:right">Ask</th>
            <th style="padding:5px 8px;text-align:right">Model%</th>
            <th style="padding:5px 8px;text-align:right">Edge</th>
            <th style="padding:5px 8px;text-align:right">Kelly$</th>
            <th style="padding:5px 8px;text-align:right">Skips</th>
            <th style="padding:5px 8px">Reason</th>
            <th style="padding:5px 8px">Settled</th>
          </tr></thead>
          <tbody id="an-missed-body"><tr><td colspan="9" class="empty">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>

    <div class="setting-group" style="margin-top:16px">
      <h3>Signal Log
        <span style="font-size:11px;font-weight:normal;color:#666;margin-left:8px">
          most recent 500 — copy rows to share with Claude for model analysis
        </span>
      </h3>
      <div style="overflow-x:auto">
        <table style="width:100%;font-size:11px;border-collapse:collapse">
          <thead><tr style="color:#888;text-align:left;border-bottom:1px solid #333">
            <th style="padding:5px 6px">Time</th>
            <th style="padding:5px 6px">Sport</th>
            <th style="padding:5px 6px">Player</th>
            <th style="padding:5px 6px">Opponent</th>
            <th style="padding:5px 6px">H/A</th>
            <th style="padding:5px 6px;text-align:right">Model%</th>
            <th style="padding:5px 6px;text-align:right">Edge</th>
            <th style="padding:5px 6px;text-align:right">Ask</th>
            <th style="padding:5px 6px">Score</th>
            <th style="padding:5px 6px">Dir</th>
            <th style="padding:5px 6px">Status</th>
          </tr></thead>
          <tbody id="an-sig-body">
            <tr><td colspan="11" class="empty">No signal data yet — scanner logs signals each cycle.</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- LOG -->
  <div class="panel" id="panel-log">
    <div class="log-box" id="log-box"><div class="dim">Waiting for scanner...</div></div>
  </div>

  <!-- SETTINGS -->
  <div class="panel" id="panel-settings">

    <div class="setting-group">
      <h3>Scanner Control</h3>
      <div class="controls">
        <button class="btn btn-orange" id="btn-start-live" onclick="startScanner(true)">Start LIVE</button>
        <button class="btn btn-green" id="btn-start"     onclick="startScanner(false)">Start (Dry Run)</button>
        <button class="btn btn-red"   id="btn-stop"      onclick="stopScanner()">Stop</button>
      </div>
      <p style="font-size:11px;color:#555">LIVE mode places real orders. Confirm twice before enabling.</p>
    </div>

    <div class="setting-group">
      <h3>Thresholds</h3>
      <div class="cfg-row">
        <label>Min Edge</label>
        <input type="range" id="rng-min-edge" min="1" max="20" step="1"
               oninput="showVal('min-edge', (this.value/100).toFixed(2)*100 + '%')">
        <span class="cfg-val" id="val-min-edge">4%</span>
      </div>
      <div class="cfg-row">
        <label>Min YES Ask</label>
        <input type="range" id="rng-min-ask" min="2" max="25" step="1"
               oninput="showVal('min-ask', this.value + '¢')">
        <span class="cfg-val" id="val-min-ask">5¢</span>
      </div>
      <p style="font-size:10px;color:#555;margin-bottom:8px">
        Skip markets where YES trades below this price — near-zero asks mean the player is nearly eliminated and the market has already priced that in.
      </p>
      <div class="cfg-row">
        <label>Min Model Prob</label>
        <input type="range" id="rng-min-model-prob" min="0" max="95" step="5"
               oninput="showVal('min-model-prob', this.value == 0 ? 'off' : this.value + '%')">
        <span class="cfg-val" id="val-min-model-prob">off</span>
      </div>
      <p style="font-size:10px;color:#555;margin-bottom:8px">
        Skip entries where model confidence is below this threshold, regardless of edge.
        Set to 0 to disable. Calibration data suggests 75–80%+ is the reliable zone.
      </p>
      <div class="cfg-row">
        <label>Max Entry Price</label>
        <input type="range" id="rng-max-entry-price" min="35" max="99" step="1"
               oninput="showVal('max-entry-price', this.value + '¢')">
        <span class="cfg-val" id="val-max-entry-price">65¢</span>
      </div>
      <p style="font-size:10px;color:#555;margin-bottom:8px">
        Skip YES entries priced above this. High-ask markets have severe SL slippage (50%+ vs 35% threshold) due to illiquidity — the bid gaps down far past the trigger price. Data shows 65¢ is the right cutoff.
      </p>
      <div class="cfg-row">
        <label>Max Bet (USD)</label>
        <input type="range" id="rng-max-bet" min="5" max="200" step="5"
               oninput="showVal('max-bet', '$'+this.value)">
        <span class="cfg-val" id="val-max-bet">$25</span>
      </div>
      <div class="cfg-row">
        <label>Kelly Fraction</label>
        <input type="range" id="rng-kelly" min="10" max="100" step="5"
               oninput="showVal('kelly', (this.value/100).toFixed(2))">
        <span class="cfg-val" id="val-kelly">0.50</span>
      </div>
      <div class="cfg-row">
        <label>Live Scan Interval</label>
        <input type="range" id="rng-live-int" min="10" max="120" step="10"
               oninput="showVal('live-int', this.value+'s')">
        <span class="cfg-val" id="val-live-int">30s</span>
      </div>
      <div class="cfg-row">
        <label>Arb Scan Interval</label>
        <input type="range" id="rng-arb-int" min="1" max="24" step="1"
               oninput="showVal('arb-int', this.value+'h')">
        <span class="cfg-val" id="val-arb-int">6h</span>
      </div>
      <p style="font-size:10px;color:#555;margin-bottom:12px">
        Free Odds API tier: 500 req/mo. At 8 req/scan: 1h=5,760/mo, 6h=960/mo, 24h=240/mo.
      </p>
    </div>

    <div class="setting-group">
      <h3>Notifications</h3>
      <p style="font-size:11px;color:#555;margin-bottom:10px">
        Push notifications via Pushover — <strong>live trades only</strong>, never dry-run.
        Requires <code>PUSHOVER_USER_KEY</code> and <code>PUSHOVER_APP_TOKEN</code> in <code>.env</code>.
      </p>
      <div class="controls" style="align-items:center;gap:16px">
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px">
          <input type="checkbox" id="chk-notify" onchange="toggleNotify(this.checked)" style="width:16px;height:16px">
          Enable notifications
        </label>
        <button class="btn btn-gray" onclick="testNotify()">Send Test</button>
      </div>
      <p id="notify-msg" style="font-size:11px;color:#4caf50;margin-top:8px"></p>
    </div>

    <div class="setting-group">
      <h3>Performance Logs</h3>
      <p style="font-size:11px;color:#555;margin-bottom:12px">
        Affects <code>orders.jsonl</code>, <code>exits.jsonl</code>, and <code>perf_cache.json</code>.
        Archive renames them with a timestamp so history is preserved.
        Clear deletes them permanently.
      </p>
      <div class="controls" style="flex-wrap:wrap;gap:8px">
        <button class="btn btn-gray"   onclick="archiveLogs('dry')">Archive Paper</button>
        <button class="btn btn-gray"   onclick="archiveLogs('live')">Archive Live</button>
        <button class="btn btn-gray"   onclick="archiveLogs('all')">Archive All</button>
        <button class="btn btn-red"    onclick="clearLogs('dry')">Clear Paper</button>
        <button class="btn btn-red"    onclick="clearLogs('live')">Clear Live</button>
      </div>
      <p id="log-action-msg" style="font-size:11px;color:#4caf;margin-top:8px"></p>
    </div>

    <div class="setting-group">
      <h3>Exit Rules</h3>
      <div class="controls" style="align-items:center;gap:16px;margin-bottom:14px">
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px">
          <input type="checkbox" id="chk-exits" onchange="toggleExits(this.checked)" style="width:16px;height:16px">
          Enable exits (Stop-Loss + Profit-Take)
        </label>
      </div>
      <p style="font-size:10px;color:#555;margin-bottom:14px">
        When disabled, all positions hold to Kalshi auto-settlement — a clean binary bet on game outcomes.
        Re-enable to restore automatic SL/PT exits.
      </p>
      <div class="cfg-row">
        <label>Stop-Loss</label>
        <input type="range" id="rng-stop-loss" min="10" max="60" step="5"
               oninput="showVal('stop-loss', this.value+'%')">
        <span class="cfg-val" id="val-stop-loss">35%</span>
      </div>
      <p style="font-size:10px;color:#555;margin-bottom:8px">
        Sell if current bid drops X% below your entry price.
      </p>
      <div class="cfg-row">
        <label>Profit-Take</label>
        <input type="range" id="rng-profit-take" min="20" max="90" step="5"
               oninput="showVal('profit-take', this.value+'%')">
        <span class="cfg-val" id="val-profit-take">50%</span>
      </div>
      <p style="font-size:10px;color:#555;margin-bottom:12px">
        Sell if current bid rises X% above your entry price.
      </p>
    </div>

    <div class="setting-group">
      <h3>Double Down</h3>
      <p style="font-size:11px;color:#555;margin-bottom:12px">
        Automatically add to an existing position when model confidence has materially increased since entry.
        <strong>Disabled by default</strong> — enable with caution in live mode.
      </p>
      <div class="controls" style="align-items:center;gap:16px;margin-bottom:14px">
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px">
          <input type="checkbox" id="chk-dd" onchange="toggleDoubleDown(this.checked)" style="width:16px;height:16px">
          Enable Double Down
        </label>
      </div>
      <div class="cfg-row">
        <label>Min Confidence</label>
        <input type="range" id="rng-dd-min-conf" min="55" max="95" step="5"
               oninput="showVal('dd-min-conf', this.value+'%')">
        <span class="cfg-val" id="val-dd-min-conf">75%</span>
      </div>
      <p style="font-size:10px;color:#555;margin-bottom:8px">
        Current model probability must exceed this before adding to a position.
      </p>
      <div class="cfg-row">
        <label>Min Conf Gain</label>
        <input type="range" id="rng-dd-conf-gain" min="5" max="30" step="5"
               oninput="showVal('dd-conf-gain', this.value+'%')">
        <span class="cfg-val" id="val-dd-conf-gain">10%</span>
      </div>
      <p style="font-size:10px;color:#555;margin-bottom:8px">
        Confidence must have risen by at least this much since entry.
      </p>
      <div class="cfg-row">
        <label>Max Add-Ons</label>
        <input type="range" id="rng-dd-max-addons" min="1" max="5" step="1"
               oninput="showVal('dd-max-addons', this.value)">
        <span class="cfg-val" id="val-dd-max-addons">1</span>
      </div>
      <div class="cfg-row">
        <label>Max Total (×Max Bet)</label>
        <input type="range" id="rng-dd-max-total" min="10" max="50" step="5"
               oninput="showVal('dd-max-total', (this.value/10).toFixed(1)+'×')">
        <span class="cfg-val" id="val-dd-max-total">2.0×</span>
      </div>
      <p style="font-size:10px;color:#555;margin-bottom:12px">
        Total position cost cap as a multiple of Max Bet. At 2×: $50 cap with $25 max bet.
      </p>
    </div>

    <div style="padding:4px 0 8px">
      <button class="btn btn-gray" onclick="saveConfig()">Save Config</button>
      <span id="save-config-msg" style="font-size:11px;color:#4caf50;margin-left:12px"></span>
    </div>

  </div>

</div><!-- /content -->

<script>
// ── Sparkline helper ───────────────────────────────────────────────────────
function sparkSvg(points, entryVal) {
  if (!points || points.length < 2) return '<span class="dim" style="font-size:10px">—</span>';
  const mn = Math.min(...points), mx = Math.max(...points, entryVal || 0);
  const range = mx - mn || 1;
  const w = 70, h = 20, pad = 2;
  const xs = points.map((_, i) => pad + (i / (points.length - 1)) * (w - pad*2));
  const ys = points.map(v => h - pad - ((v - mn) / range) * (h - pad*2));
  const line = xs.map((x, i) => `${x.toFixed(1)},${ys[i].toFixed(1)}`).join(' ');
  const lastColor = points[points.length-1] >= (entryVal||0) ? '#00ff88' : '#ff6b6b';
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
    <polyline points="${line}" fill="none" stroke="${lastColor}" stroke-width="1.5" stroke-linejoin="round"/>
    ${entryVal ? `<line x1="${pad}" y1="${(h - pad - ((entryVal - mn) / range) * (h - pad*2)).toFixed(1)}" x2="${w-pad}" y2="${(h - pad - ((entryVal - mn) / range) * (h - pad*2)).toFixed(1)}" stroke="#444" stroke-width="1" stroke-dasharray="2,2"/>` : ''}
  </svg>`;
}

// ── Tab switching ──────────────────────────────────────────────────────────
let currentTab = 'signals';
function showTab(name) {
  document.querySelectorAll('.tab').forEach((t, i) =>
    t.classList.toggle('active', ['signals','upcoming','positions','orders','performance','analysis','log','settings'][i] === name)
  );
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
  currentTab = name;
  if (name === 'upcoming')     { loadLiveNow(); loadUpcoming(); }
  if (name === 'positions')    loadPositions();
  if (name === 'orders')       loadOrders();
  if (name === 'performance')  loadPerformance();
  if (name === 'analysis')     loadAnalysis();
  if (name === 'log')          loadLog();
  if (name === 'settings')     loadNotifyState();
}

// ── Formatting helpers ─────────────────────────────────────────────────────
function pct(v, decimals=1) {
  if (v == null) return '-';
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(decimals) + '%';
}
function pctPlain(v, decimals=1) {
  if (v == null) return '-';
  return (v * 100).toFixed(decimals) + '%';
}
function fmtTime(iso) {
  if (!iso) return '-';
  return new Date(iso).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}
function edgeCls(e) { return e > 0 ? 'green' : (e < 0 ? 'red' : 'dim'); }
function confCls(p)  { return p >= 0.70 ? 'green' : p >= 0.55 ? 'yellow' : 'dim'; }
function confCell(p, src) {
  if (!p) return '<td class="dim">—</td>';
  const title = src === 'entry' ? 'title="Entry estimate"' : 'title="Current model"';
  const dim   = src === 'entry' ? ';opacity:.65' : '';
  return `<td class="${confCls(p)}" style="font-size:11px${dim}" ${title}>${(p*100).toFixed(0)}%</td>`;
}
function fmtResult(o) {
  const won = o.won;
  if (o.result_type === 'exit') {
    const labels = {stop_loss:'STOP-LOSS', profit_take:'PROFIT-TAKE', manual:'MANUAL SELL'};
    const label  = labels[o.result] || o.result.toUpperCase();
    return label + ' ' + (won ? 'PROFIT' : 'LOSS');
  }
  return (o.result || '').toUpperCase() + ' ' + (won ? 'WIN' : 'LOSS');
}

// ── Status ─────────────────────────────────────────────────────────────────
let _config = {};
async function loadStatus() {
  try {
    const s = await fetch('/api/status').then(r => r.json());
    const dot = document.getElementById('dot');
    dot.className = 'dot ' + (s.running ? 'on' : 'off');
    const badge = document.getElementById('badge');
    badge.textContent = s.dry_run ? 'DRY RUN' : 'LIVE';
    badge.className   = 'badge ' + (s.dry_run ? 'dry' : 'live');
    const ls = document.getElementById('lastScan');
    ls.textContent = s.last_live_scan ? 'last scan ' + fmtTime(s.last_live_scan) : '';
    _config = s.config;
    syncSliders(s.config);
  } catch(_) {}
}

function syncSliders(cfg) {
  setValue('min-edge', Math.round(cfg.min_edge * 100),
           Math.round(cfg.min_edge * 100) + '%');
  setValue('max-bet',  cfg.max_bet_usd, '$' + cfg.max_bet_usd);
  setValue('kelly',    Math.round(cfg.kelly_fraction * 100),
           cfg.kelly_fraction.toFixed(2));
  setValue('live-int', cfg.live_interval_sec, cfg.live_interval_sec + 's');
  setValue('stop-loss',    Math.round((cfg.stop_loss_pct   || 0.35) * 100),
           Math.round((cfg.stop_loss_pct   || 0.35) * 100) + '%');
  setValue('profit-take',  Math.round((cfg.profit_take_pct || 0.50) * 100),
           Math.round((cfg.profit_take_pct || 0.50) * 100) + '%');
  setValue('min-ask', Math.round((cfg.min_ask || 0.05) * 100),
           Math.round((cfg.min_ask || 0.05) * 100) + '¢');
  const mmp = Math.round((cfg.min_model_prob || 0) * 100);
  setValue('min-model-prob', mmp, mmp === 0 ? 'off' : mmp + '%');
  setValue('max-entry-price', Math.round((cfg.max_entry_price || 0.65) * 100),
           Math.round((cfg.max_entry_price || 0.65) * 100) + '¢');
  const arbH = Math.max(1, Math.round(cfg.arb_interval_sec / 3600));
  setValue('arb-int', arbH, arbH + 'h');
  // Exits toggle
  const chkExits = document.getElementById('chk-exits');
  if (chkExits) chkExits.checked = cfg.exits_enabled !== false;
  // Double Down
  const chkDd = document.getElementById('chk-dd');
  if (chkDd) chkDd.checked = !!cfg.double_down_enabled;
  const ddConf = Math.round((cfg.double_down_min_conf  || 0.75) * 100);
  const ddGain = Math.round((cfg.double_down_conf_gain || 0.10) * 100);
  const ddMax  = cfg.double_down_max_addons || 1;
  const ddTot  = Math.round((cfg.double_down_max_total || 2.0) * 10);
  setValue('dd-min-conf',   ddConf, ddConf + '%');
  setValue('dd-conf-gain',  ddGain, ddGain + '%');
  setValue('dd-max-addons', ddMax,  String(ddMax));
  setValue('dd-max-total',  ddTot,  (ddTot/10).toFixed(1) + '×');
}

function setValue(id, rngVal, display) {
  const r = document.getElementById('rng-' + id);
  if (r) r.value = rngVal;
  const v = document.getElementById('val-' + id);
  if (v) v.textContent = display;
}

function showVal(id, text) {
  document.getElementById('val-' + id).textContent = text;
}

// ── Signals ────────────────────────────────────────────────────────────────
async function loadSignals() {
  try {
    const sigs = await fetch('/api/signals').then(r => r.json());
    const buys = sigs.filter(s => s.edge > 0);
    const live = sigs.filter(s => s.source === 'live');
    const arb  = sigs.filter(s => s.source === 'arb');
    const best = sigs.length ? Math.max(...sigs.map(s => s.edge)) : null;
    document.getElementById('s-total').textContent = sigs.length || '-';
    document.getElementById('s-buys').textContent  = buys.length || '-';
    document.getElementById('s-edge').textContent  = best != null ? pct(best) : '-';
    document.getElementById('s-live').textContent  = live.length || '-';
    document.getElementById('s-arb').textContent   = arb.length  || '-';

    const tbody = document.getElementById('sig-body');
    if (!sigs.length) {
      tbody.innerHTML = '<tr><td colspan="10" class="empty">No signals yet — scanner is starting...</td></tr>';
      return;
    }
    tbody.innerHTML = sigs.map(s => {
      const homeAwayBadge = s.home_away
        ? `<span style="font-size:9px;padding:1px 4px;border-radius:2px;margin-left:4px;background:${s.home_away==='HOME'?'#1a3a1a':'#3a1a1a'};color:${s.home_away==='HOME'?'#00ff88':'#ff6b6b'}">${s.home_away}</span>`
        : '';
      const opponentCell = s.opponent
        ? `<td style="font-size:12px">vs ${s.opponent}${homeAwayBadge}</td>`
        : '<td class="dim">—</td>';
      return `<tr>
        <td>${s.player || ''}</td>
        ${opponentCell}
        <td class="dim">${s.sport || ''}</td>
        <td><span class="src ${s.source}">${s.source.toUpperCase()}</span></td>
        <td>${pctPlain(s.kalshi_ask)}</td>
        <td class="${confCls(s.model_prob)}">${pctPlain(s.model_prob)}</td>
        <td class="${edgeCls(s.edge)}">${pct(s.edge)}</td>
        <td>$${(s.kelly_usd || 0).toFixed(2)}</td>
        <td class="dim" style="font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis">${s.score_state || ''}</td>
        <td class="${s.direction === 'BUY' ? 'buy' : 'skip'}">${s.direction}</td>
      </tr>`;
    }).join('');
  } catch(_) {}
}

// ── Portfolio ──────────────────────────────────────────────────────────────
function parseEventLabel(et) {
  // KXMLBGAME-26MAY201905TORNYY → "TOR vs NYY — May 20"
  const m = et.match(/-(\d{2})([A-Z]{3})(\d{2})\d{4}([A-Z]+)$/);
  if (!m) return et;
  const [, , mon, day, teams] = m;  // format: YY MON DD time TEAMS
  const tl = teams.length;
  let t1, t2;
  if      (tl >= 6) { t1 = teams.slice(0,3); t2 = teams.slice(3); }
  else if (tl === 5) { t1 = teams.slice(0,3); t2 = teams.slice(3); }
  else               { t1 = teams.slice(0,2); t2 = teams.slice(2); }
  return `${t1} vs ${t2} — ${mon} ${parseInt(day)}`;
}

async function loadPositions() {
  try {
    const data = await fetch('/api/positions').then(r => r.json());
    if (data.error) { console.error(data.error); return; }
    const ps     = data.positions  || [];
    const events = data.events     || [];
    const cash   = data.balance_usd ?? null;

    // ── Summary stats ──────────────────────────────────────────────────────
    const totalInvested   = ps.reduce((s, p) => s + (p.cost_usd  || 0), 0);
    const totalContracts  = ps.reduce((s, p) => s + (p.contracts  || 0), 0);
    const totalFees       = ps.reduce((s, p) => s + (p.fees_usd   || 0), 0);
    const unrealVals      = ps.filter(p => p.unrealized_pnl != null).map(p => p.unrealized_pnl);
    const totalUnreal     = unrealVals.length ? unrealVals.reduce((a,b) => a+b, 0) : null;

    document.getElementById('p-count').textContent     = ps.length || '0';
    document.getElementById('p-invested').textContent  = '$' + totalInvested.toFixed(2);
    document.getElementById('p-contracts').textContent = totalContracts;
    document.getElementById('p-fees').textContent      = '$' + totalFees.toFixed(2);
    const unrealEl = document.getElementById('p-unrealized');
    if (totalUnreal != null) {
      unrealEl.textContent = (totalUnreal >= 0 ? '+' : '') + '$' + totalUnreal.toFixed(2);
      unrealEl.className   = 'val ' + (totalUnreal >= 0 ? 'green' : 'red');
    } else {
      unrealEl.textContent = '—';
      unrealEl.className   = 'val dim';
    }
    const cashEl = document.getElementById('p-cash');
    cashEl.textContent = cash != null ? '$' + cash.toFixed(2) : '—';

    // ── Event exposure bars ────────────────────────────────────────────────
    // Aggregate P&L and SL/PT fractions per event from position data
    const eventPnl    = {};
    const eventThresh = {};
    ps.forEach(p => {
      const evKey = p.ticker.split('-').slice(0, -1).join('-');
      if (p.unrealized_pnl != null)
        eventPnl[evKey] = (eventPnl[evKey] || 0) + p.unrealized_pnl;
      if (p.price_cents && p.stop_loss_cents && p.profit_take_cents) {
        const slF = (p.price_cents - p.stop_loss_cents) / p.price_cents;
        const ptF = (p.profit_take_cents - p.price_cents) / p.price_cents;
        const w   = p.cost_usd || 1;
        if (!eventThresh[evKey]) eventThresh[evKey] = { slW: 0, ptW: 0, wSum: 0 };
        eventThresh[evKey].slW  += slF * w;
        eventThresh[evKey].ptW  += ptF * w;
        eventThresh[evKey].wSum += w;
      }
    });

    const barsEl  = document.getElementById('p-event-bars');
    const maxCost = events.reduce((m, e) => Math.max(m, e.cost_usd), 0) || 1;
    barsEl.innerHTML = events.map(e => {
      const pct  = (e.cost_usd / maxCost * 100).toFixed(1);
      const pctT = (e.cost_usd / (totalInvested || 1) * 100).toFixed(0);
      const lbl  = parseEventLabel(e.event_ticker);
      const pnl  = eventPnl[e.event_ticker] ?? null;
      const pnlStr = pnl != null
        ? `<span class="${pnl >= 0 ? 'green' : 'red'}" style="font-size:11px;margin-left:8px">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}</span>`
        : '';

      // SL / PT gauge
      const th = eventThresh[e.event_ticker];
      const slF = th ? th.slW / th.wSum : 0.28;
      const ptF = th ? th.ptW / th.wSum : 0.40;
      const totalRange = slF + ptF;
      const centerPct  = (slF / totalRange * 100).toFixed(2);  // entry point %
      const pnlFrac    = pnl != null ? pnl / e.cost_usd : null;
      const gaugePct   = pnlFrac != null
        ? Math.max(0, Math.min(100, (slF + pnlFrac) / totalRange * 100))
        : null;
      // filled bar: from center toward current position
      const fillLeft  = gaugePct != null ? Math.min(+centerPct, gaugePct).toFixed(2) : centerPct;
      const fillW     = gaugePct != null ? Math.abs(gaugePct - +centerPct).toFixed(2) : 0;
      const fillColor = pnlFrac != null && pnlFrac >= 0 ? '#00ff88' : '#ff6b6b';

      return `
        <div style="margin-bottom:18px">
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:12px;margin-bottom:4px">
            <span>${lbl}</span>
            <span><span class="yellow">$${e.cost_usd.toFixed(2)}</span>${pnlStr}</span>
          </div>
          <div style="background:#1a1a2e;border-radius:3px;height:7px;overflow:hidden;margin-bottom:5px">
            <div style="width:${pct}%;height:100%;background:linear-gradient(90deg,#4ea8de,#6c63ff);border-radius:3px"></div>
          </div>
          <div style="position:relative;height:10px;border-radius:3px;overflow:hidden;background:#111;margin-bottom:3px">
            <div style="position:absolute;left:0;width:${centerPct}%;height:100%;background:#ff6b6b18"></div>
            <div style="position:absolute;left:${centerPct}%;right:0;height:100%;background:#00ff8818"></div>
            <div style="position:absolute;left:0;width:2px;height:100%;background:#ff6b6b;opacity:.7"></div>
            <div style="position:absolute;right:0;width:2px;height:100%;background:#00ff88;opacity:.7"></div>
            <div style="position:absolute;left:${centerPct}%;width:1px;height:100%;background:#444"></div>
            <div style="position:absolute;left:${fillLeft}%;width:${fillW}%;height:100%;background:${fillColor};opacity:.75;transition:all .4s"></div>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:10px;color:#555;margin-bottom:1px">
            <span style="color:#ff6b6b88">SL −${(slF*100).toFixed(0)}%</span>
            <span>${e.contracts} contracts · ${pctT}% of portfolio</span>
            <span style="color:#00ff8888">PT +${(ptF*100).toFixed(0)}%</span>
          </div>
        </div>`;
    }).join('');

    // ── Positions table ────────────────────────────────────────────────────
    const tbody = document.getElementById('pos-body');
    if (!ps.length) {
      tbody.innerHTML = '<tr><td colspan="10" class="empty">No open positions</td></tr>';
      return;
    }
    tbody.innerHTML = ps.map(p => {
      const curCell = p.current_bid_cents != null
        ? `<td class="${p.current_bid_cents > p.price_cents ? 'green' : p.current_bid_cents < p.price_cents ? 'red' : ''}">${p.current_bid_cents}¢</td>`
        : '<td class="dim">—</td>';

      const pnlSign = p.unrealized_pnl != null ? (p.unrealized_pnl >= 0 ? '+' : '') : '';
      const pnlCell = p.unrealized_pnl != null
        ? `<td class="${p.unrealized_pnl >= 0 ? 'green' : 'red'}">${pnlSign}$${p.unrealized_pnl.toFixed(2)}</td>`
        : '<td class="dim">—</td>';

      const slPt = (p.stop_loss_cents && p.profit_take_cents)
        ? `<td style="font-size:11px"><span style="color:#ff6b6b">SL:${p.stop_loss_cents}¢</span> / <span style="color:#00ff88">PT:${p.profit_take_cents}¢</span></td>`
        : '<td class="dim">—</td>';

      return `<tr>
        <td>${p.player}</td>
        <td>${p.contracts}</td>
        <td>${p.price_cents || '—'}¢</td>
        ${curCell}
        ${pnlCell}
        <td class="yellow">$${(p.cost_usd||0).toFixed(2)}</td>
        <td class="dim" style="font-size:11px">$${(p.fees_usd||0).toFixed(2)}</td>
        ${slPt}
        ${confCell(p.model_prob, p.model_source)}
        <td class="dim" style="font-size:11px">${fmtTime(p.opened_at)}</td>
      </tr>`;
    }).join('');
  } catch(e) { console.error(e); }
}

// ── Orders ─────────────────────────────────────────────────────────────────
let ordersMode = 'dry';
function switchOrders(mode) {
  ordersMode = mode;
  ['dry','live'].forEach(m => {
    document.getElementById('ord-btn-'+m).classList.toggle('active', m === mode);
  });
  loadOrders();
}
function espnUrl(ticker, player) {
  ticker = (ticker || '').toUpperCase();
  player = (player || '').trim();
  let href, label;
  if (ticker.includes('ATP') || ticker.includes('WTA')) {
    href  = 'https://www.espn.com/tennis/results';
    label = 'Tennis';
  } else if (ticker.includes('MLB')) {
    href  = 'https://www.espn.com/mlb/scoreboard';
    label = 'MLB';
  } else {
    return '<td class="dim">—</td>';
  }
  return `<td><a href="${href}" target="_blank" rel="noopener"
    style="font-size:11px;color:#4ea8de;text-decoration:none" title="ESPN ${label}"
    >↗ ${label}</a></td>`;
}

async function loadOrders() {
  try {
    const [os, phRaw] = await Promise.all([
      fetch('/api/orders?mode=' + ordersMode).then(r => r.json()),
      fetch('/api/price_history').then(r => r.json()).catch(() => []),
    ]);
    // Build sparkline lookup: ticker -> {points, player}
    const sparkLookup = {};
    (phRaw || []).forEach(item => {
      if (item.ticker) sparkLookup[item.ticker] = item;
    });

    const tbody = document.getElementById('ord-body');
    if (!os.length) {
      tbody.innerHTML = '<tr><td colspan="15" class="empty">No ' + (ordersMode === 'live' ? 'live' : 'paper') + ' orders logged yet</td></tr>';
      return;
    }
    tbody.innerHTML = os.map(o => {
      const isOpen = ['dry_run','submitted','resting'].includes(o.status);
      const cur    = o.current_bid_cents;
      const sl     = o.stop_loss_cents;
      const pt     = o.profit_take_cents;
      const entry  = o.price_cents || 0;

      // Colour current price relative to entry
      let curCell = '<td class="dim">—</td>';
      if (cur != null) {
        const diff = cur - entry;
        const cls  = diff > 0 ? 'green' : diff < 0 ? 'red' : '';
        curCell = `<td class="${cls}">${cur}¢</td>`;
      }

      // SL / PT targets
      const targCell = (sl != null && pt != null)
        ? `<td style="font-size:11px"><span style="color:#ff6b6b">SL:${sl}¢</span> / <span style="color:#00ff88">PT:${pt}¢</span></td>`
        : '<td class="dim">—</td>';

      // ESPN link
      const espnCell = espnUrl(o.ticker, o.player);

      // Sell button — only for open positions
      const sellBtn = isOpen
        ? `<td><button class="btn btn-red" style="padding:2px 8px;font-size:11px"
             onclick="sellNow('${o.ticker}','${o.side||'yes'}',${o.contracts},${entry})">Sell</button></td>`
        : '<td></td>';

      // Price path sparkline
      const sparkData = sparkLookup[o.ticker];
      const sparkCell = `<td style="padding:5px 8px">${sparkSvg(sparkData ? sparkData.points : null, entry || null)}</td>`;

      return `<tr>
        <td class="dim" style="font-size:11px">${fmtTime(o.ts)}</td>
        <td>${o.player}</td>
        <td>${(o.side||'').toUpperCase()}</td>
        <td>${o.contracts}</td>
        <td>${entry}¢</td>
        <td>$${(o.cost_usd||0).toFixed(2)}</td>
        <td class="${edgeCls(o.edge)}">${pct(o.edge)}</td>
        ${curCell}
        ${targCell}
        ${confCell(o.model_prob, o.model_source)}
        <td><span class="st ${o.status}">${o.status}</span></td>
        <td><span class="src ${o.source}">${o.source}</span></td>
        ${espnCell}
        ${sparkCell}
        ${sellBtn}
      </tr>`;
    }).join('');
  } catch(_) {}
}

async function sellNow(ticker, side, contracts, entry_cents) {
  const label = ordersMode === 'live' ? 'LIVE' : 'DRY RUN';
  if (!confirm(`[${label}] Sell ${contracts} ${side.toUpperCase()} contracts?\nTicker: ${ticker}\nEntry: ${entry_cents}¢\n\nThis will execute at current bid.`)) return;
  try {
    const r = await fetch('/api/sell', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticker, side, contracts, entry_cents})
    });
    const d = await r.json();
    if (d.ok) {
      const pnlSign = d.pnl >= 0 ? '+' : '';
      alert(`${d.dry_run ? '[DRY RUN] ' : ''}Sold @ ${d.exit_cents}¢\nP&L: ${pnlSign}$${d.pnl.toFixed(2)}`);
      loadOrders();
    } else {
      alert('Sell failed: ' + d.error);
    }
  } catch(e) {
    alert('Sell error: ' + e);
  }
}

// ── Log ────────────────────────────────────────────────────────────────────
async function loadLog() {
  try {
    const lines = await fetch('/api/log').then(r => r.json());
    const box = document.getElementById('log-box');
    const atBottom = box.scrollHeight - box.clientHeight <= box.scrollTop + 10;
    box.innerHTML = lines.map(l => `<div>${l}</div>`).join('') || '<div class="dim">No log entries yet.</div>';
    if (atBottom) box.scrollTop = box.scrollHeight;
  } catch(_) {}
}

// ── Scanner controls ───────────────────────────────────────────────────────
async function startScanner(live) {
  if (live) {
    if (!confirm('Enable LIVE trading?\\n\\nReal orders will be placed with real money.')) return;
    if (!confirm('Are you sure? This is your second confirmation.')) return;
  }
  await fetch('/api/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({dry_run: !live})
  });
  loadStatus();
}

async function stopScanner() {
  await fetch('/api/stop', {method: 'POST'});
  loadStatus();
}

// ── Notifications ─────────────────────────────────────────────────────────
async function loadNotifyState() {
  try {
    const d = await fetch('/api/notifications/status').then(r => r.json());
    const box = document.getElementById('chk-notify');
    if (box) box.checked = d.enabled;
    const msg = document.getElementById('notify-msg');
    if (msg && !d.configured) {
      msg.style.color = '#ff6b6b';
      msg.textContent = 'Keys not configured — add PUSHOVER_USER_KEY and PUSHOVER_APP_TOKEN to .env';
    }
  } catch(_) {}
}

async function toggleNotify(enabled) {
  await fetch('/api/notifications/toggle', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({enabled})
  });
}

async function testNotify() {
  const r  = await fetch('/api/notifications/test', {method:'POST'});
  const d  = await r.json();
  const msg = document.getElementById('notify-msg');
  if (!d.configured) {
    msg.style.color = '#ff6b6b';
    msg.textContent = 'Keys not set — add PUSHOVER_USER_KEY and PUSHOVER_APP_TOKEN to .env on the server.';
  } else if (d.ok) {
    msg.style.color = '#4caf50';
    msg.textContent = 'Test notification sent!';
  } else {
    msg.style.color = '#ff6b6b';
    msg.textContent = 'Send failed — check keys in .env.';
  }
}

// ── Log management ────────────────────────────────────────────────────────
async function archiveLogs(mode) {
  const label = mode === 'all' ? 'all' : (mode === 'live' ? 'live' : 'paper');
  if (!confirm(`Archive ${label} log files?\\nFiles will be renamed with a timestamp.`)) return;
  const r = await fetch('/api/logs/archive', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({mode})
  });
  const d = await r.json();
  const msg = document.getElementById('log-action-msg');
  if (d.ok) {
    msg.textContent = d.archived.length ? 'Archived: ' + d.archived.join(', ') : 'Nothing to archive.';
    loadOrders();
  } else {
    msg.textContent = 'Error archiving logs.';
  }
}

async function clearLogs(mode) {
  const label = mode === 'all' ? 'all' : (mode === 'live' ? 'live' : 'paper');
  if (!confirm(`Permanently delete ${label} log files?\\nThis cannot be undone.`)) return;
  if (!confirm(`Are you sure? All ${label} performance history will be lost.`)) return;
  const r = await fetch('/api/logs/clear', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({mode})
  });
  const d = await r.json();
  const msg = document.getElementById('log-action-msg');
  msg.textContent = d.ok ? `${label.charAt(0).toUpperCase()+label.slice(1)} logs cleared.` : 'Error clearing logs.';
  if (d.ok) loadOrders();
}

// ── Config save ────────────────────────────────────────────────────────────
async function toggleExits(enabled) {
  await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({exits_enabled: enabled})
  });
}

async function toggleDoubleDown(enabled) {
  await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({double_down_enabled: enabled})
  });
}

async function saveConfig() {
  const payload = {
    min_edge:          parseFloat(document.getElementById('rng-min-edge').value) / 100,
    max_bet_usd:       parseFloat(document.getElementById('rng-max-bet').value),
    kelly_fraction:    parseFloat(document.getElementById('rng-kelly').value) / 100,
    live_interval_sec: parseFloat(document.getElementById('rng-live-int').value),
    arb_interval_sec:  parseFloat(document.getElementById('rng-arb-int').value) * 3600,
    stop_loss_pct:     parseFloat(document.getElementById('rng-stop-loss').value) / 100,
    profit_take_pct:   parseFloat(document.getElementById('rng-profit-take').value) / 100,
    min_ask:           parseFloat(document.getElementById('rng-min-ask').value) / 100,
    min_model_prob:    parseFloat(document.getElementById('rng-min-model-prob').value) / 100,
    max_entry_price:   parseFloat(document.getElementById('rng-max-entry-price').value) / 100,
    double_down_min_conf:   parseFloat(document.getElementById('rng-dd-min-conf').value) / 100,
    double_down_conf_gain:  parseFloat(document.getElementById('rng-dd-conf-gain').value) / 100,
    double_down_max_addons: parseFloat(document.getElementById('rng-dd-max-addons').value),
    double_down_max_total:  parseFloat(document.getElementById('rng-dd-max-total').value) / 10,
  };
  const r = await fetch('/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  const data = await r.json();
  if (!data.ok) { alert('Error: ' + (data.errors || []).join(', ')); return; }
  const msg = document.getElementById('save-config-msg');
  if (msg) { msg.textContent = 'Saved ✓'; setTimeout(() => { msg.textContent = ''; }, 2500); }
  loadStatus();
}

// ── Live now ───────────────────────────────────────────────────────────────
async function loadLiveNow() {
  try {
    const games = await fetch('/api/live_now').then(r => r.json());
    const ct = document.getElementById('live-count');
    if (ct) ct.textContent = games.length ? `(${games.length})` : '';

    const body = document.getElementById('live-now-body');
    if (!games.length) {
      body.innerHTML = '<div class="no-upcoming dim" style="padding:14px 0;font-size:12px">No live games right now.</div>';
      return;
    }

    body.innerHTML = games.map(g => {
      const icon  = g.sport === 'tennis' ? '&#127934;' : '&#9918;';
      const sideA = g.sides[0] || {};
      const sideB = g.sides[1] || {};

      function sideHtml(s, align) {
        const priceStr  = s.ask != null ? Math.round(s.ask * 100) + 'c' : '-';
        const modelStr  = s.model_prob != null ? Math.round(s.model_prob * 100) + '%' : '';
        const edgeVal   = s.edge;
        const edgeStr   = edgeVal != null ? (edgeVal >= 0 ? '+' : '') + Math.round(edgeVal * 100) + '%' : '';
        const edgeCls   = edgeVal != null ? (edgeVal > 0 ? 'green' : 'red') : 'dim';
        const dirStr    = s.direction === 'BUY' ? ' BUY' : '';
        return `<div class="live-side ${align === 'right' ? 'right' : ''}">
          <div class="live-side-name">${s.player || '—'}</div>
          <div class="live-side-ask">ask <span>${priceStr}</span></div>
          ${modelStr ? `<div class="live-side-model dim">model <span style="color:#aaa">${modelStr}</span></div>` : ''}
          ${edgeStr ? `<div class="live-side-edge ${edgeCls}">${edgeStr}${dirStr}</div>` : ''}
        </div>`;
      }

      return `<div class="live-card">
        <div class="live-card-header">
          <span class="live-badge">LIVE</span>
          <span class="live-sport">${icon} ${g.sport}</span>
          <span class="live-score">${g.score_state}</span>
        </div>
        <div class="live-teams">
          ${sideHtml(sideA, 'left')}
          <div class="live-vs">vs</div>
          ${sideHtml(sideB, 'right')}
        </div>
      </div>`;
    }).join('');
  } catch(e) {
    document.getElementById('live-now-body').innerHTML =
      '<div class="no-upcoming dim" style="padding:14px 0">Failed to load live games.</div>';
  }
}

// ── Upcoming games ─────────────────────────────────────────────────────────
function fmtCountdown(mins) {
  if (mins <= 0)   return 'NOW';
  if (mins < 60)   return mins + 'm';
  const h = Math.floor(mins / 60), m = mins % 60;
  return h + 'h' + (m ? ' ' + m + 'm' : '');
}
function countdownClass(mins) {
  if (mins <= 60)  return 'hot';
  if (mins <= 120) return 'near';
  return 'ok';
}
function fmtStartTime(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString([], {weekday:'short', month:'short', day:'numeric'})
       + ' ' + d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}

async function loadUpcoming() {
  try {
    const events = await fetch('/api/upcoming').then(r => r.json());
    const soon = events.filter(e => e.mins_until <= 120).length;
    const tennis   = events.filter(e => e.sport === 'tennis').length;
    const baseball = events.filter(e => e.sport === 'baseball').length;

    document.getElementById('u-count').textContent   = events.length || '0';
    document.getElementById('u-soon').textContent    = soon || '0';
    document.getElementById('u-tennis').textContent  = tennis || '0';
    document.getElementById('u-baseball').textContent = baseball || '0';

    const container = document.getElementById('upcoming-body');
    if (!events.length) {
      container.innerHTML = '<div class="no-upcoming">No upcoming markets found in the next 48 hours.<br>Check back closer to game time.</div>';
      return;
    }

    // Group by time bucket
    const buckets = {'Starting Soon (< 2h)': [], 'Today': [], 'Tomorrow': []};
    const todayDate = new Date().toDateString();
    events.forEach(e => {
      const d = new Date(e.starts_at);
      if (e.mins_until <= 120)                  buckets['Starting Soon (< 2h)'].push(e);
      else if (d.toDateString() === todayDate)  buckets['Today'].push(e);
      else                                       buckets['Tomorrow'].push(e);
    });

    let html = '';
    for (const [label, group] of Object.entries(buckets)) {
      if (!group.length) continue;
      html += `<div class="fixture-group">
        <div class="fixture-group-header">${label} &mdash; ${group.length} event${group.length > 1 ? 's' : ''}</div>`;

      for (const e of group) {
        const icon      = e.sport === 'tennis' ? '&#127934;' : '&#9918;';
        const cdCls     = countdownClass(e.mins_until);
        const fixCls    = e.mins_until <= 60 ? 'imminent' : e.mins_until <= 120 ? 'soon' : '';
        const sides     = e.sides;
        const sideA     = sides[0] || {};
        const sideB     = sides[1] || {};
        const priceA    = sideA.ask != null ? Math.round(sideA.ask * 100) + 'c' : '-';
        const priceB    = sideB.ask != null ? Math.round(sideB.ask * 100) + 'c' : '-';

        html += `<div class="fixture ${fixCls}">
          <div class="fix-sport"><span class="fix-sport-icon">${icon}</span>${e.sport}</div>
          <div class="fix-teams">
            <div class="fix-side" style="text-align:right">
              <div class="fix-name">${sideA.player || '—'}</div>
              <div class="fix-price">ask <span>${priceA}</span></div>
            </div>
            <div class="fix-vs">vs</div>
            <div class="fix-side">
              <div class="fix-name">${sideB.player || '—'}</div>
              <div class="fix-price">ask <span>${priceB}</span></div>
            </div>
          </div>
          <div class="fix-time">
            <div class="fix-countdown ${cdCls}">${fmtCountdown(e.mins_until)}</div>
            <div class="fix-date">${fmtStartTime(e.starts_at)}</div>
          </div>
        </div>`;
      }
      html += '</div>';
    }
    container.innerHTML = html;
  } catch(err) {
    document.getElementById('upcoming-body').innerHTML =
      '<div class="no-upcoming">Failed to load upcoming games.</div>';
  }
}

// ── Performance ────────────────────────────────────────────────────────────
let perfMode = 'dry';
let _portChart = null;

function switchPerf(mode) {
  perfMode = mode;
  ['dry','live'].forEach(m => {
    document.getElementById('pf-btn-'+m).classList.toggle('active', m === mode);
  });
  const isLive = mode === 'live';
  document.getElementById('pf-lbl-total').textContent = isLive ? 'Live Orders' : 'Paper Bets';
  document.getElementById('pf-lbl-pnl').textContent   = isLive ? 'Realized P&L' : 'Hypothetical P&L';
  const wrap = document.getElementById('portfolio-chart-wrap');
  if (wrap) wrap.style.display = isLive ? '' : 'none';
  loadPerformance();
}

async function loadPortfolioChart() {
  const wrap = document.getElementById('portfolio-chart-wrap');
  if (!wrap) return;
  try {
    const snaps = await fetch('/api/portfolio_value').then(r => r.json());
    if (!snaps || snaps.length === 0) { wrap.style.display = 'none'; return; }
    wrap.style.display = '';

    const rawTs  = snaps.map(s => new Date(s.ts));
    const totals = snaps.map(s => s.total);
    const first  = totals[0];
    const last   = totals[totals.length - 1];
    const delta  = last - first;
    const pct    = first ? ((delta / first) * 100).toFixed(1) : '0.0';
    const deltaStr = (delta >= 0 ? '+' : '') + '$' + delta.toFixed(2);

    document.getElementById('port-current').textContent = '$' + last.toFixed(2);
    document.getElementById('port-current').style.color = delta >= 0 ? '#4caf50' : '#ff6b6b';
    document.getElementById('port-change').textContent  = deltaStr + ' (' + (delta >= 0 ? '+' : '') + pct + '%) since first snapshot';

    // Build sparse x-axis labels (show ~8 evenly-spaced timestamps, rest empty)
    const n = snaps.length;
    const step = Math.max(1, Math.floor(n / 8));
    const labels = rawTs.map((d, i) =>
      i % step === 0 ? d.toLocaleTimeString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : ''
    );
    // Full tooltip labels
    const fullLabels = rawTs.map(d => d.toLocaleString());

    const color = delta >= 0 ? '#4caf50' : '#ff6b6b';
    const bgColor = delta >= 0 ? 'rgba(76,175,80,0.08)' : 'rgba(255,107,107,0.08)';
    const ctx = document.getElementById('portfolio-chart').getContext('2d');
    if (_portChart) { _portChart.destroy(); _portChart = null; }
    _portChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          data: totals,
          borderColor: color,
          backgroundColor: bgColor,
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.3,
          fill: true,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: items => fullLabels[items[0].dataIndex],
              label: item  => '$' + Number(item.raw).toFixed(2),
            }
          },
        },
        scales: {
          x: { ticks: { color: '#555', maxRotation: 0, autoSkip: false }, grid: { color: '#1a1a1a' } },
          y: { ticks: { color: '#555', callback: v => '$' + v.toFixed(0) }, grid: { color: '#1a1a1a' } },
        },
      },
    });
  } catch (e) {
    console.warn('portfolio chart error', e);
    if (wrap) wrap.style.display = 'none';
  }
}

async function loadPerformance() {
  if (perfMode === 'live') loadPortfolioChart();
  try {
    const d = await fetch('/api/performance?mode=' + perfMode).then(r => r.json());
    const s = d.summary;
    const pnlPos = s.total_pnl >= 0;

    document.getElementById('pf-total').textContent    = s.total_bets;
    document.getElementById('pf-resolved').textContent = s.resolved;
    document.getElementById('pf-winrate').textContent  = s.resolved ? Math.round(s.win_rate * 100) + '%' : '-';
    const pnlEl = document.getElementById('pf-pnl');
    pnlEl.textContent  = (pnlPos ? '+' : '') + '$' + s.total_pnl.toFixed(2);
    pnlEl.className    = 'val ' + (pnlPos ? 'green' : 'red');
    const roiEl = document.getElementById('pf-roi');
    roiEl.textContent  = s.resolved ? (pnlPos ? '+' : '') + Math.round(s.roi * 100) + '%' : '-';
    roiEl.className    = 'val ' + (pnlPos ? 'green' : 'red');

    const pn = document.getElementById('pf-pending-note');
    if (pn) pn.textContent = s.pending ? `(${s.pending} pending resolution)` : '';

    // Edge buckets
    const bucketOrder = ['3-5%','5-10%','10-20%','20%+'];
    const bkEl = document.getElementById('perf-buckets');
    bkEl.innerHTML = bucketOrder.map(label => {
      const b = d.edge_buckets[label];
      if (!b) return '';
      const wr = b.bets ? Math.round(b.wins / b.bets * 100) : 0;
      const pnlPos2 = b.pnl >= 0;
      return `<div class="card" style="min-width:120px">
        <div class="lbl">Edge ${label}</div>
        <div style="font-size:18px;color:#fff;margin-top:6px;font-weight:bold">${wr}%</div>
        <div style="font-size:11px;color:#555;margin-top:2px">${b.bets} bets</div>
        <div style="font-size:11px;margin-top:2px;color:${pnlPos2?'#00ff88':'#ff6b6b'}">${pnlPos2?'+':''}$${b.pnl.toFixed(2)}</div>
      </div>`;
    }).join('');

    // Calibration chart
    const cal = d.calibration || [];
    const calDiv = document.getElementById('perf-cal-body');
    if (cal.length === 0) {
      calDiv.innerHTML = '<div class="dim" style="font-size:12px;padding:8px 0">No resolved trades with signal data yet.</div>';
    } else {
      calDiv.innerHTML = cal.map(b => {
        const exp = (b.avg_model_prob * 100).toFixed(0);
        const act = (b.actual_win_rate * 100).toFixed(0);
        const diff = b.actual_win_rate - b.avg_model_prob;
        const diffStr = diff >= 0 ? `<span class="green">+${(diff*100).toFixed(0)}%</span>` : `<span class="red">${(diff*100).toFixed(0)}%</span>`;
        return `<div class="cal-row">
          <div class="cal-label">${b.label}</div>
          <div class="cal-bars">
            <div class="cal-bar-expected" style="width:${exp}%"></div>
            <div class="cal-bar-actual"   style="width:${act}%"></div>
          </div>
          <div class="cal-stat">n=${b.count} &nbsp; actual=${act}%</div>
          <div style="font-size:10px;width:50px">${diffStr}</div>
        </div>`;
      }).join('');
    }

    // Resolved table
    const tbody = document.getElementById('perf-body');
    if (!d.resolved.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty">No resolved bets yet — markets settle after each match.</td></tr>';
      return;
    }
    tbody.innerHTML = d.resolved.map(o => {
      const won = o.won;
      const pnlPos2 = o.pnl >= 0;
      return `<tr>
        <td class="dim" style="font-size:11px">${fmtTime(o.ts)}</td>
        <td>${o.player}</td>
        <td>${(o.side||'').toUpperCase()}</td>
        <td>${o.contracts}</td>
        <td>$${(o.cost_usd||0).toFixed(2)}</td>
        <td class="${o.edge>0?'green':'red'}">${pct(o.edge)}</td>
        <td style="color:${won?'#00ff88':'#ff6b6b'}">${fmtResult(o)}</td>
        <td style="color:${pnlPos2?'#00ff88':'#ff6b6b'};font-weight:bold">${pnlPos2?'+':''}$${o.pnl.toFixed(2)}</td>
        <td><span class="src ${o.source}">${o.source}</span></td>
      </tr>`;
    }).join('');
  } catch(e) {
    document.getElementById('perf-body').innerHTML =
      '<tr><td colspan="9" class="empty">Failed to load performance data.</td></tr>';
  }
}

// ── Analysis ───────────────────────────────────────────────────────────────
let analysisMode = 'dry';
function switchAnalysis(m) {
  analysisMode = m;
  document.getElementById('an-btn-dry').classList.toggle('active',  m === 'dry');
  document.getElementById('an-btn-live').classList.toggle('active', m === 'live');
  loadAnalysis();
}

async function loadAnalysis() {
  try {
    const d = await fetch('/api/analysis?mode=' + analysisMode).then(r => r.json());
    const s = d.summary;

    document.getElementById('an-total').textContent    = s.total_signals || '-';
    document.getElementById('an-buys').textContent     = s.buy_signals   || '-';
    document.getElementById('an-exec').textContent     = s.executed      || '-';
    document.getElementById('an-buyrate').textContent  = s.total_signals ? Math.round(s.buy_rate * 100) + '%' : '-';
    document.getElementById('an-execrate').textContent = s.buy_signals   ? Math.round(s.exec_rate * 100) + '%' : '-';
    document.getElementById('an-avgedge').textContent  = s.avg_edge ? '+' + Math.round(s.avg_edge * 100) + '%' : '-';

    // Sport breakdown
    document.getElementById('an-sport-body').innerHTML = Object.entries(d.by_sport).map(([sport, b]) =>
      `<tr style="border-bottom:1px solid #222">
        <td style="padding:4px 6px;text-transform:capitalize">${sport}</td>
        <td style="padding:4px 6px;text-align:right">${b.total}</td>
        <td style="padding:4px 6px;text-align:right">${b.buys}</td>
        <td style="padding:4px 6px;text-align:right">${b.executed}</td>
        <td style="padding:4px 6px;text-align:right;color:#4caf50">${b.avg_edge ? '+' + Math.round(b.avg_edge*100) + '%' : '-'}</td>
      </tr>`
    ).join('') || '<tr><td colspan="5" class="empty">—</td></tr>';

    // Skip reasons
    const skips = Object.entries(d.skip_reasons);
    document.getElementById('an-skip-body').innerHTML = skips.length
      ? skips.map(([reason, count]) =>
          `<tr style="border-bottom:1px solid #222">
            <td style="padding:4px 6px;color:#aaa;max-width:200px;overflow:hidden;text-overflow:ellipsis" title="${reason}">${reason}</td>
            <td style="padding:4px 6px;text-align:right">${count}</td>
          </tr>`
        ).join('')
      : '<tr><td colspan="2" class="empty">None</td></tr>';

    // Confidence bars
    const maxConf = Math.max(...Object.values(d.conf_buckets), 1);
    document.getElementById('an-conf-bars').innerHTML = Object.entries(d.conf_buckets).map(([lbl, cnt]) => {
      const pct = Math.round(cnt / maxConf * 100);
      const cls = lbl === '90+' ? 'green' : lbl === '80-90' ? 'green' : lbl === '70-80' ? 'yellow' : 'dim';
      return `<div style="display:flex;align-items:center;gap:8px;margin-bottom:5px">
        <span style="width:44px;color:#888">${lbl}%</span>
        <div style="flex:1;background:#222;border-radius:2px;height:10px">
          <div style="width:${pct}%;background:${cls==='green'?'#4caf50':cls==='yellow'?'#ff9800':'#555'};height:100%;border-radius:2px"></div>
        </div>
        <span style="width:28px;text-align:right;color:#aaa">${cnt}</span>
      </div>`;
    }).join('');

    // Signal table
    const sigs = d.signals || [];
    document.getElementById('an-sig-body').innerHTML = sigs.length
      ? sigs.map(s => {
          const t    = s.ts ? new Date(s.ts).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}) : '-';
          const mp   = s.model_prob != null ? Math.round(s.model_prob*100)+'%' : '-';
          const mpCls = s.model_prob >= 0.70 ? 'green' : s.model_prob >= 0.55 ? 'yellow' : 'dim';
          const edgeStr = s.edge != null ? (s.edge>=0?'+':'')+Math.round(s.edge*100)+'%' : '-';
          const eCls  = s.edge > 0 ? 'green' : s.edge < 0 ? 'red' : 'dim';
          const ask   = s.kalshi_ask != null ? Math.round(s.kalshi_ask*100)+'¢' : '-';
          const ha    = s.home_away ? `<span style="font-size:9px;padding:1px 3px;border-radius:2px;background:${s.home_away==='HOME'?'#1a3a1a':'#3a1a1a'};color:${s.home_away==='HOME'?'#00ff88':'#ff6b6b'}">${s.home_away}</span>` : '-';
          const dir   = s.direction === 'BUY' ? '<span class="buy">BUY</span>' : '<span class="skip">SKIP</span>';
          const stat  = s.exec_status || '-';
          const statCls = stat.includes('dry_run')||stat.includes('executed')||stat.includes('submitted') ? 'green' : stat === 'skipped' ? 'yellow' : 'dim';
          return `<tr style="border-bottom:1px solid #1a1a1a">
            <td style="padding:4px 6px;color:#888">${t}</td>
            <td style="padding:4px 6px;text-transform:capitalize">${s.sport||'-'}</td>
            <td style="padding:4px 6px">${s.player||'-'}</td>
            <td style="padding:4px 6px;color:#aaa">${s.opponent||'-'}</td>
            <td style="padding:4px 6px">${ha}</td>
            <td style="padding:4px 6px;text-align:right" class="${mpCls}">${mp}</td>
            <td style="padding:4px 6px;text-align:right" class="${eCls}">${edgeStr}</td>
            <td style="padding:4px 6px;text-align:right;color:#aaa">${ask}</td>
            <td style="padding:4px 6px;color:#666;max-width:160px;overflow:hidden;text-overflow:ellipsis" title="${s.score_state||''}">${s.score_state||'-'}</td>
            <td style="padding:4px 6px">${dir}</td>
            <td style="padding:4px 6px" class="${statCls}">${stat}</td>
          </tr>`;
        }).join('')
      : '<tr><td colspan="11" class="empty">No signal data yet — scanner logs signals each cycle.</td></tr>';

    // Timing table
    const timing = d.timing || [];
    const tbody_t = document.getElementById('an-timing-body');
    if (timing.length === 0) {
      tbody_t.innerHTML = '<tr><td colspan="6" class="empty dim">Not enough data yet</td></tr>';
    } else {
      tbody_t.innerHTML = timing.map(b => {
        const wr = b.win_rate != null ? (b.win_rate * 100).toFixed(0) + '%' : '—';
        const wrColor = b.win_rate == null ? '' : b.win_rate >= 0.55 ? 'color:#00ff88' : b.win_rate >= 0.45 ? 'color:#ffcc00' : 'color:#ff6b6b';
        const bar = b.win_rate != null
          ? `<div style="height:10px;background:#1a3a1a;border-radius:3px;overflow:hidden;width:120px;display:inline-block"><div style="height:100%;width:${(b.win_rate*100).toFixed(0)}%;background:#00ff88;opacity:.8"></div></div>`
          : '';
        return `<tr>
          <td style="padding:5px 8px;font-weight:bold">${b.period}</td>
          <td style="padding:5px 8px;text-align:right">${b.count}</td>
          <td style="padding:5px 8px;text-align:right;color:#ffcc00">${(b.avg_edge*100).toFixed(1)}%</td>
          <td style="padding:5px 8px;text-align:right">${b.settled}</td>
          <td style="padding:5px 8px;text-align:right;${wrColor}">${wr}</td>
          <td style="padding:5px 8px">${bar}</td>
        </tr>`;
      }).join('');
    }

    // Missed trades table
    const missed = d.missed_trades || [];
    const tbody_m = document.getElementById('an-missed-body');
    if (missed.length === 0) {
      tbody_m.innerHTML = '<tr><td colspan="9" class="empty dim">No missed trades with positive edge</td></tr>';
    } else {
      tbody_m.innerHTML = missed.map(t => {
        const sett = t.settlement ? `<span class="${t.settlement === 'yes' ? 'green' : 'red'}">${t.settlement.toUpperCase()}</span>` : '<span class="dim">pending</span>';
        return `<tr>
          <td style="padding:5px 8px">${t.player}</td>
          <td style="padding:5px 8px">${t.sport}</td>
          <td style="padding:5px 8px;text-align:right">${(t.kalshi_ask*100).toFixed(0)}¢</td>
          <td style="padding:5px 8px;text-align:right">${(t.model_prob*100).toFixed(0)}%</td>
          <td style="padding:5px 8px;text-align:right;color:#00ff88">+${(t.edge*100).toFixed(1)}%</td>
          <td style="padding:5px 8px;text-align:right">$${t.kelly_usd.toFixed(2)}</td>
          <td style="padding:5px 8px;text-align:right;color:#555">${t.skip_count}</td>
          <td style="padding:5px 8px;color:#666;font-size:11px">${t.reason}</td>
          <td style="padding:5px 8px">${sett}</td>
        </tr>`;
      }).join('');
    }

  } catch(e) { console.error('analysis error', e); }
}

// ── Polling loop ───────────────────────────────────────────────────────────
async function poll() {
  await loadStatus();
  await loadSignals();
  if (currentTab === 'upcoming')     { await loadLiveNow(); await loadUpcoming(); }
  if (currentTab === 'positions')    await loadPositions();
  if (currentTab === 'orders')       await loadOrders();
  if (currentTab === 'performance')  await loadPerformance();
  if (currentTab === 'analysis')     await loadAnalysis();
  if (currentTab === 'log')          await loadLog();
}

poll();
setInterval(poll, 15000);
</script>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    # Auto-start scanner in live mode at startup
    _state["running"] = True
    _state["dry_run"] = False
    _scan_thread = threading.Thread(target=_scanner_loop, daemon=True, name="scanner")
    _scan_thread.start()
    print("Scanner auto-started in LIVE mode")

    if not config.DASHBOARD_PASS:
        logging.warning("DASHBOARD_PASS is not set — dashboard auth is DISABLED")

    print(f"Dashboard running at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
