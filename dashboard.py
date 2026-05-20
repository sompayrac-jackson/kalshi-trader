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

def _orders_file(mode: str) -> Path:
    return ORDERS_LIVE_FILE if mode == "live" else ORDERS_DRY_FILE

def _exits_file(mode: str) -> Path:
    return EXITS_LIVE_FILE if mode == "live" else EXITS_DRY_FILE

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
        "stop_loss_pct":     0.35,
        "profit_take_pct":   0.50,
        "min_ask":           0.05,
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
    executor.DRY_RUN        = _state["dry_run"]
    executor.MIN_EDGE       = cfg["min_edge"]
    executor.MAX_BET_USD    = cfg["max_bet_usd"]
    executor.STOP_LOSS_PCT   = cfg["stop_loss_pct"]
    executor.PROFIT_TAKE_PCT = cfg["profit_take_pct"]
    executor.MIN_ASK         = cfg["min_ask"]
    live_mod.MIN_EDGE       = cfg["min_edge"]
    live_mod.KELLY_FRACTION = cfg["kelly_fraction"]


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


def _check_exits(client: KalshiClient):
    """
    Scan open positions (real in live mode, virtual from orders.jsonl in dry-run)
    and trigger stop-loss or profit-take sells when thresholds are breached.
    """
    cfg             = _state["config"]
    stop_loss_pct   = cfg["stop_loss_pct"]
    profit_take_pct = cfg["profit_take_pct"]
    dry_run         = _state["dry_run"]

    # Real positions from Kalshi
    real_positions: list[dict] = []
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

    # Build entry-price map from appropriate orders file (first logged order per ticker)
    entry_map: dict[str, dict] = {}
    if orders_f.exists():
        for line in orders_f.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
                t = o.get("ticker", "")
                if t and t not in entry_map and o.get("contracts", 0) > 0:
                    entry_map[t] = {
                        "entry_cents": o.get("price_cents", 0),
                        "contracts":   o.get("contracts", 0),
                        "side":        o.get("side", "yes"),
                    }
            except Exception:
                pass

    # Combine: real first, then virtual for tickers not already in real positions
    real_tickers = {p.get("ticker") for p in real_positions}
    to_check = list(real_positions) + [v for v in virtual_positions if v["ticker"] not in real_tickers]

    for pos in to_check:
        ticker   = pos.get("ticker", "")
        net_pos  = pos.get("position", 0)
        virtual  = pos.get("_virtual", False)

        if net_pos <= 0:
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

        try:
            market      = client.get_market(ticker)
            bid_dollars = float(market.get("yes_bid_dollars") or 0)
            bid_cents   = round(bid_dollars * 100)
        except Exception as e:
            _log(f"[EXIT] market fetch failed {ticker}: {e}")
            continue

        if bid_cents <= 0:
            continue

        entry_dollars = entry_cents / 100
        drop = (entry_dollars - bid_dollars) / entry_dollars
        rise = (bid_dollars - entry_dollars) / entry_dollars
        tag  = "[DRY] " if virtual else ""

        if drop >= sl_pct:
            _log(f"[EXIT] {tag}STOP-LOSS {ticker}: entry={entry_cents}¢ bid={bid_cents}¢ drop={drop:.1%} sl={sl_pct:.0%} (cost=${cost_usd:.2f})")
            r = executor.execute_exit(client, ticker, side, net_pos, entry_cents, bid_cents, "stop_loss")
            _log(f"[EXIT] {r.status} pnl=${r.pnl_usd:.2f} {r.error or ''}")
        elif rise >= pt_pct:
            _log(f"[EXIT] {tag}PROFIT-TAKE {ticker}: entry={entry_cents}¢ bid={bid_cents}¢ rise={rise:.1%} pt={pt_pct:.0%} (cost=${cost_usd:.2f})")
            r = executor.execute_exit(client, ticker, side, net_pos, entry_cents, bid_cents, "profit_take")
            _log(f"[EXIT] {r.status} pnl=${r.pnl_usd:.2f} {r.error or ''}")


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

        # Live scan
        try:
            _log("Running live scan...")
            live = scan_live(_client)
            for s in live:
                signals.append(_to_dict(s, "live"))
                if s.edge > 0:
                    try:
                        r = executor.execute_live(_client, s)
                        _log(f"[LIVE] {r.player} — {r.status} {r.error or ''}")
                    except Exception as e:
                        _log(f"[LIVE] execute error: {e}")
            with _lock:
                _state["signals"]        = signals
                _state["last_live_scan"] = datetime.now(timezone.utc).isoformat()
            _log(f"Live scan done — {len(live)} live signals")
        except Exception as e:
            _log(f"Live scan error: {e}")

        # Refresh bid price cache for all open tickers (used by Orders tab)
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

        # Exit check — runs every cycle after live scan
        try:
            _check_exits(_client)
        except Exception as e:
            _log(f"[EXIT] check error: {e}")

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
                        "opened_at":   o.get("ts", ""),
                        "source":      o.get("source", "live"),
                    }
            except Exception:
                pass

    with _price_lock:
        prices = dict(_price_cache)

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

    cfg    = _state["config"]
    sl_base = cfg.get("stop_loss_pct", 0.35)
    pt_base = cfg.get("profit_take_pct", 0.50)

    with _price_lock:
        prices = dict(_price_cache)

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

    return jsonify(list(reversed(orders[-200:])))


_ALL_LOG_FILES = (
    ORDERS_DRY_FILE, ORDERS_LIVE_FILE,
    EXITS_DRY_FILE,  EXITS_LIVE_FILE,
    PERF_CACHE_DRY,  PERF_CACHE_LIVE,
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
        (_orders_file(mode), _exits_file(mode), _perf_cache_file(mode))
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
        (_orders_file(mode), _exits_file(mode), _perf_cache_file(mode))
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
    "stop_loss_pct":     (0.10, 0.60),
    "profit_take_pct":   (0.20, 0.90),
    "min_ask":           (0.02, 0.25),
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
            and o.get("status") in ("submitted", "resting", "filled")
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

    return jsonify({
        "mode": mode,
        "summary": {
            "total_bets": len(virtual),
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
          <th>Status</th><th>Src</th><th title="ESPN link">ESPN</th><th></th>
        </tr></thead>
        <tbody id="ord-body"></tbody>
      </table>
    </div>
  </div>

  <!-- PERFORMANCE -->
  <div class="panel" id="panel-performance">
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
        <label>Min YES Ask</label>
        <input type="range" id="rng-min-ask" min="2" max="25" step="1"
               oninput="showVal('min-ask', this.value + '¢')">
        <span class="cfg-val" id="val-min-ask">5¢</span>
      </div>
      <p style="font-size:10px;color:#555;margin-bottom:8px">
        Skip markets where YES trades below this price — near-zero asks mean the player is nearly eliminated and the market has already priced that in.
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
        <td>${pctPlain(s.model_prob)}</td>
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
      tbody.innerHTML = '<tr><td colspan="9" class="empty">No open positions</td></tr>';
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
    const os = await fetch('/api/orders?mode=' + ordersMode).then(r => r.json());
    const tbody = document.getElementById('ord-body');
    if (!os.length) {
      tbody.innerHTML = '<tr><td colspan="13" class="empty">No ' + (ordersMode === 'live' ? 'live' : 'paper') + ' orders logged yet</td></tr>';
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
        <td><span class="st ${o.status}">${o.status}</span></td>
        <td><span class="src ${o.source}">${o.source}</span></td>
        ${espnCell}
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
let perfMode = 'dry';
function switchPerf(mode) {
  perfMode = mode;
  ['dry','live'].forEach(m => {
    document.getElementById('pf-btn-'+m).classList.toggle('active', m === mode);
  });
  const isLive = mode === 'live';
  document.getElementById('pf-lbl-total').textContent = isLive ? 'Live Orders' : 'Paper Bets';
  document.getElementById('pf-lbl-pnl').textContent   = isLive ? 'Realized P&L' : 'Hypothetical P&L';
  loadPerformance();
}
async function loadPerformance() {
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

    # Auto-start scanner in dry-run mode at startup
    _state["running"] = True
    _state["dry_run"] = True
    _scan_thread = threading.Thread(target=_scanner_loop, daemon=True, name="scanner")
    _scan_thread.start()
    print("Scanner auto-started in DRY RUN mode")

    print(f"Dashboard running at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
