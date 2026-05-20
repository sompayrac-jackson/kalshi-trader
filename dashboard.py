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
import threading
import time
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, Response

import config
from kalshi_client import KalshiClient
from live_scanner import scan_live
import order_executor as executor

try:
    from arb_scanner import scan as arb_scan
    HAS_ARB = True
except ImportError:
    HAS_ARB = False

KALSHI_API_KEY = config.KALSHI_API_KEY
ODDS_API_KEY   = config.ODDS_API_KEY
POSITIONS_FILE = Path("positions.jsonl")
ORDERS_FILE    = Path("orders.jsonl")
PERF_CACHE_FILE = Path("perf_cache.json")

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

# ── Shared state ──────────────────────────────────────────────────────────────

_lock       = threading.Lock()
_stop_event = threading.Event()
_scan_thread: threading.Thread | None = None
_client: KalshiClient | None = None

_state: dict = {
    "running":        False,
    "dry_run":        True,
    "last_live_scan": None,
    "last_arb_scan":  None,
    "signals":        [],
    "log":            [],
    "config": {
        "min_edge":          0.04,
        "max_bet_usd":       25.0,
        "kelly_fraction":    0.5,
        "arb_interval_sec":  300,
        "live_interval_sec": 30,
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


def _scanner_loop():
    global _client
    _client  = KalshiClient(api_key_id=KALSHI_API_KEY)
    last_arb = 0.0

    while not _stop_event.is_set():
        cfg = _state["config"]
        now = time.time()
        signals: list[dict] = []

        # Arb scan (throttled)
        if HAS_ARB and ODDS_API_KEY and (now - last_arb >= cfg["arb_interval_sec"]):
            try:
                _log("Running arb scan...")
                for s in arb_scan(_client, ODDS_API_KEY):
                    signals.append(_to_dict(s, "arb"))
                last_arb = now
                with _lock:
                    _state["last_arb_scan"] = datetime.now(timezone.utc).isoformat()
                _log(f"Arb scan done — {sum(1 for s in signals if s['source']=='arb')} signals")
            except Exception as e:
                _log(f"Arb scan error: {e}")

        # Live scan
        try:
            _log("Running live scan...")
            live = scan_live(_client)
            for s in live:
                signals.append(_to_dict(s, "live"))
            with _lock:
                _state["signals"]        = signals
                _state["last_live_scan"] = datetime.now(timezone.utc).isoformat()
            _log(f"Live scan done — {len(live)} live signals")
        except Exception as e:
            _log(f"Live scan error: {e}")

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
    if not POSITIONS_FILE.exists():
        return jsonify([])
    seen: dict = {}
    for line in POSITIONS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            p = json.loads(line)
            if p.get("status") == "open":
                seen[p["ticker"]] = p
        except Exception:
            pass
    return jsonify(list(seen.values()))


@app.route("/api/orders")
@_require_auth
def api_orders():
    if not ORDERS_FILE.exists():
        return jsonify([])
    orders = []
    for line in ORDERS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            orders.append(json.loads(line))
        except Exception:
            pass
    return jsonify(list(reversed(orders[-200:])))


@app.route("/api/log")
@_require_auth
def api_log():
    with _lock:
        return jsonify(_state["log"][-150:])


@app.route("/api/start", methods=["POST"])
@_require_auth
def api_start():
    global _scan_thread
    data    = request.get_json(silent=True) or {}
    dry_run = data.get("dry_run", True)

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
    "min_edge":          (0.01, 0.50),
    "max_bet_usd":       (1.0,  500.0),
    "kelly_fraction":    (0.05, 1.0),
    "arb_interval_sec":  (300,  86400),
    "live_interval_sec": (10,   300),
}

@app.route("/api/config", methods=["POST"])
@_require_auth
def api_config():
    data = request.get_json(silent=True) or {}
    errors = []
    updates = {}
    for k, v in data.items():
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

    # Kalshi live markets — occurrence_datetime < now, no result
    raw: list[dict] = []
    for series in ("KXATPMATCH", "KXWTAMATCH", "KXMLBGAME"):
        for m in _fetch_kalshi_markets(series, now):
            if m["_live"]:
                raw.append(m)

    if not raw:
        return []

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

def _load_perf_cache() -> dict:
    if not PERF_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(PERF_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_perf_cache(cache: dict):
    PERF_CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


@app.route("/api/performance")
@_require_auth
def api_performance():
    # Load all logged orders
    all_orders: list[dict] = []
    if ORDERS_FILE.exists():
        for line in ORDERS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                all_orders.append(json.loads(line))
            except Exception:
                pass

    # Include: confirmed dry_run orders + liquidity-skipped orders (model said buy)
    virtual = [
        o for o in all_orders
        if o.get("dry_run") and o.get("contracts", 0) > 0
        and o.get("status") in ("dry_run", "skipped")
        and ("liquidity" in o.get("error", "") or o.get("status") == "dry_run")
    ]

    # Load and refresh resolution cache
    perf_cache = _load_perf_cache()
    unresolved_tickers = {
        o["ticker"] for o in virtual
        if o["ticker"] not in perf_cache
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
        _save_perf_cache(perf_cache)

    # Build resolved / pending lists
    resolved_orders: list[dict] = []
    pending_orders:  list[dict] = []

    for o in virtual:
        result = perf_cache.get(o["ticker"])
        if result:
            side = o.get("side", "yes")
            won  = (side == "yes" and result == "yes") or \
                   (side == "no"  and result == "no")
            # cost_usd already accounts for side (see order_executor.py)
            cost = o.get("cost_usd", 0)
            pnl  = (o["contracts"] - cost) if won else -cost
            resolved_orders.append({**o, "result": result, "won": won, "pnl": round(pnl, 2)})
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

    return jsonify({
        "summary": {
            "total_virtual_bets": len(virtual),
            "resolved":           n,
            "pending":            len(pending_orders),
            "wins":               wins,
            "losses":             n - wins,
            "win_rate":           round(wins / n, 3) if n else 0,
            "total_invested":     round(total_cost, 2),
            "total_pnl":          round(total_pnl, 2),
            "roi":                round(total_pnl / total_cost, 3) if total_cost else 0,
        },
        "edge_buckets": buckets,
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
  <div class="tab" onclick="showTab('positions')">Positions</div>
  <div class="tab" onclick="showTab('orders')">Orders</div>
  <div class="tab" onclick="showTab('performance')">Performance</div>
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
          <th>Player / Team</th><th>Sport</th><th>Src</th>
          <th>Ask</th><th>Model</th><th>Edge</th><th>Kelly $</th>
          <th>Score / Book</th><th>Action</th>
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

  <!-- POSITIONS -->
  <div class="panel" id="panel-positions">
    <div class="stats">
      <div class="card"><div class="lbl">Open Positions</div><div class="val" id="p-count">-</div></div>
      <div class="card"><div class="lbl">Total At Risk</div><div class="val yellow" id="p-risk">-</div></div>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>Player / Team</th><th>Ticker</th><th>Contracts</th>
          <th>Price</th><th>Cost</th><th>Source</th><th>Opened</th>
        </tr></thead>
        <tbody id="pos-body"></tbody>
      </table>
    </div>
  </div>

  <!-- ORDERS -->
  <div class="panel" id="panel-orders">
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>Time</th><th>Player</th><th>Side</th><th>Qty</th>
          <th>Price</th><th>Cost</th><th>Edge</th><th>Status</th><th>Src</th>
        </tr></thead>
        <tbody id="ord-body"></tbody>
      </table>
    </div>
  </div>

  <!-- PERFORMANCE -->
  <div class="panel" id="panel-performance">
    <div class="stats" id="perf-stats">
      <div class="card"><div class="lbl">Virtual Bets</div><div class="val" id="pf-total">-</div></div>
      <div class="card"><div class="lbl">Resolved</div><div class="val" id="pf-resolved">-</div></div>
      <div class="card"><div class="lbl">Win Rate</div><div class="val" id="pf-winrate">-</div></div>
      <div class="card"><div class="lbl">Hypothetical P&amp;L</div><div class="val" id="pf-pnl">-</div></div>
      <div class="card"><div class="lbl">ROI</div><div class="val" id="pf-roi">-</div></div>
    </div>

    <div style="margin:20px 0 8px;font-size:10px;color:#444;text-transform:uppercase;letter-spacing:.08em">Edge Bucket Analysis</div>
    <div id="perf-buckets" style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:24px"></div>

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

  <!-- LOG -->
  <div class="panel" id="panel-log">
    <div class="log-box" id="log-box"><div class="dim">Waiting for scanner...</div></div>
  </div>

  <!-- SETTINGS -->
  <div class="panel" id="panel-settings">

    <div class="setting-group">
      <h3>Scanner Control</h3>
      <div class="controls">
        <button class="btn btn-green" id="btn-start"     onclick="startScanner(false)">Start (Dry Run)</button>
        <button class="btn btn-orange" id="btn-start-live" onclick="startScanner(true)">Start LIVE</button>
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
      <button class="btn btn-gray" onclick="saveConfig()" style="margin-top:6px">Save Config</button>
    </div>

  </div>

</div><!-- /content -->

<script>
// ── Tab switching ──────────────────────────────────────────────────────────
let currentTab = 'signals';
function showTab(name) {
  document.querySelectorAll('.tab').forEach((t, i) =>
    t.classList.toggle('active', ['signals','upcoming','positions','orders','performance','log','settings'][i] === name)
  );
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
  currentTab = name;
  if (name === 'upcoming')     { loadLiveNow(); loadUpcoming(); }
  if (name === 'positions')    loadPositions();
  if (name === 'orders')       loadOrders();
  if (name === 'performance')  loadPerformance();
  if (name === 'log')          loadLog();
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
  const arbH = Math.max(1, Math.round(cfg.arb_interval_sec / 3600));
  setValue('arb-int', arbH, arbH + 'h');
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
      tbody.innerHTML = '<tr><td colspan="9" class="empty">No signals yet — start the scanner in Settings</td></tr>';
      return;
    }
    tbody.innerHTML = sigs.map(s => `
      <tr>
        <td>${s.player || ''}</td>
        <td class="dim">${s.sport || ''}</td>
        <td><span class="src ${s.source}">${s.source.toUpperCase()}</span></td>
        <td>${pctPlain(s.kalshi_ask)}</td>
        <td>${pctPlain(s.model_prob)}</td>
        <td class="${edgeCls(s.edge)}">${pct(s.edge)}</td>
        <td>$${(s.kelly_usd || 0).toFixed(2)}</td>
        <td class="dim" style="font-size:11px;max-width:220px;overflow:hidden;text-overflow:ellipsis">${s.score_state || ''}</td>
        <td class="${s.direction === 'BUY' ? 'buy' : 'skip'}">${s.direction}</td>
      </tr>`).join('');
  } catch(_) {}
}

// ── Positions ──────────────────────────────────────────────────────────────
async function loadPositions() {
  try {
    const ps = await fetch('/api/positions').then(r => r.json());
    const total = ps.reduce((s, p) => s + p.cost_usd, 0);
    document.getElementById('p-count').textContent = ps.length || '0';
    document.getElementById('p-risk').textContent  = '$' + total.toFixed(2);

    const tbody = document.getElementById('pos-body');
    if (!ps.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty">No open positions</td></tr>';
      return;
    }
    tbody.innerHTML = ps.map(p => `
      <tr>
        <td>${p.player}</td>
        <td class="dim" style="font-size:11px">${p.ticker}</td>
        <td>${p.contracts}</td>
        <td>${p.price_cents}c</td>
        <td class="yellow">$${p.cost_usd.toFixed(2)}</td>
        <td><span class="src ${p.source}">${p.source}</span></td>
        <td class="dim" style="font-size:11px">${fmtTime(p.opened_at)}</td>
      </tr>`).join('');
  } catch(_) {}
}

// ── Orders ─────────────────────────────────────────────────────────────────
async function loadOrders() {
  try {
    const os = await fetch('/api/orders').then(r => r.json());
    const tbody = document.getElementById('ord-body');
    if (!os.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty">No orders logged yet</td></tr>';
      return;
    }
    tbody.innerHTML = os.map(o => `
      <tr>
        <td class="dim" style="font-size:11px">${fmtTime(o.ts)}</td>
        <td>${o.player}</td>
        <td>${(o.side||'').toUpperCase()}</td>
        <td>${o.contracts}</td>
        <td>${o.price_cents}c</td>
        <td>$${(o.cost_usd||0).toFixed(2)}</td>
        <td class="${edgeCls(o.edge)}">${pct(o.edge)}</td>
        <td><span class="st ${o.status}">${o.status}</span></td>
        <td><span class="src ${o.source}">${o.source}</span></td>
      </tr>`).join('');
  } catch(_) {}
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

// ── Config save ────────────────────────────────────────────────────────────
async function saveConfig() {
  const payload = {
    min_edge:          parseFloat(document.getElementById('rng-min-edge').value) / 100,
    max_bet_usd:       parseFloat(document.getElementById('rng-max-bet').value),
    kelly_fraction:    parseFloat(document.getElementById('rng-kelly').value) / 100,
    live_interval_sec: parseFloat(document.getElementById('rng-live-int').value),
    arb_interval_sec:  parseFloat(document.getElementById('rng-arb-int').value) * 3600,
  };
  const r = await fetch('/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  const data = await r.json();
  if (!data.ok) { alert('Error: ' + (data.errors || []).join(', ')); return; }
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
async function loadPerformance() {
  const container = document.getElementById('perf-stats');
  try {
    const d = await fetch('/api/performance').then(r => r.json());
    const s = d.summary;
    const pnlPos = s.total_pnl >= 0;

    document.getElementById('pf-total').textContent    = s.total_virtual_bets;
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
        <td style="color:${won?'#00ff88':'#ff6b6b'}">${o.result.toUpperCase()} ${won?'WIN':'LOSS'}</td>
        <td style="color:${pnlPos2?'#00ff88':'#ff6b6b'};font-weight:bold">${pnlPos2?'+':''}$${o.pnl.toFixed(2)}</td>
        <td><span class="src ${o.source}">${o.source}</span></td>
      </tr>`;
    }).join('');
  } catch(e) {
    document.getElementById('perf-body').innerHTML =
      '<tr><td colspan="9" class="empty">Failed to load performance data.</td></tr>';
  }
}

// ── Polling loop ───────────────────────────────────────────────────────────
async function poll() {
  await loadStatus();
  await loadSignals();
  if (currentTab === 'upcoming')     { await loadLiveNow(); await loadUpcoming(); }
  if (currentTab === 'positions')    await loadPositions();
  if (currentTab === 'orders')       await loadOrders();
  if (currentTab === 'performance')  await loadPerformance();
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
    print(f"Dashboard running at http://{args.host}:{args.port}")
    print("Open http://localhost:{} in your browser".format(args.port))
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
