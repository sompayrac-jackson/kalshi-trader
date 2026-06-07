"""
Order executor.

Takes signals from arb_scanner or live_scanner and places limit orders on Kalshi.

DRY_RUN = True by default — prints what would be placed without touching the market.
Set DRY_RUN = False only when you're ready to trade real money.
"""

import json
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from pathlib import Path

from kalshi_client import KalshiClient
from arb_scanner import ArbSignal
from live_scanner import LiveSignal
import notifier

# ── Config ────────────────────────────────────────────────────────────────────

DRY_RUN     = True    # SAFETY: set False only to place real orders
MAX_BET_USD = 25.0    # hard cap per order regardless of Kelly
MIN_BET_USD = 1.0     # ignore signals below this size
MIN_EDGE    = 0.03    # skip signals below this edge even if passed in
MIN_ASK     = 0.05    # skip if market prices YES below this — near-zero asks mean
                      # the market has almost certainly priced out this player already

EXITS_ENABLED    = True   # set False to hold all positions to Kalshi settlement (no SL or PT)
STOP_LOSS_PCT    = 0.35   # sell if bid drops this fraction below entry price
PROFIT_TAKE_PCT  = 0.50   # sell if bid rises this fraction above entry price
MIN_MODEL_PROB   = 0.0    # skip entries where model_prob is below this (0 = disabled)
MAX_ENTRY_PRICE  = 0.65   # skip YES entries priced above this — high-ask markets have
                           # severe SL slippage (50%+ vs 35% threshold) due to illiquidity

# ── Double Down Config ────────────────────────────────────────────────────────
DOUBLE_DOWN_ENABLED    = False  # off by default — explicitly enabled via dashboard
DOUBLE_DOWN_MIN_CONF   = 0.75   # min model_prob to consider adding on
DOUBLE_DOWN_CONF_GAIN  = 0.10   # min confidence gain vs entry before adding
DOUBLE_DOWN_MAX_ADDONS = 1      # max add-ons per ticker
DOUBLE_DOWN_MAX_TOTAL  = 2.0    # max total position as multiple of MAX_BET_USD

ORDERS_DRY_FILE  = Path("orders_dry.jsonl")
ORDERS_LIVE_FILE = Path("orders_live.jsonl")
EXITS_DRY_FILE   = Path("exits_dry.jsonl")
EXITS_LIVE_FILE  = Path("exits_live.jsonl")
SETTLEMENTS_FILE = Path("settlements.jsonl")

# ── Strategy filters (deepdive5.py findings) ──────────────────────────────────
# Toggle any to False to disable that guard individually.
REQUIRE_ESPN_OR_VEGAS = True   # baseball: skip markov-only signals (no ESPN/Vegas data)
SKIP_TWO_OUTS         = True   # baseball: 2-outs entries had 27% historical WR
SKIP_LATE_SLIM_LEAD   = True   # baseball: inning 6+ with +1 lead had 14-18% historical WR

# ── Open-position deduplication ───────────────────────────────────────────────
# Tickers we already hold (bought but not yet exited/settled).
# Prevents re-buying the same live market every scan cycle.

_open_tickers:        set[str]         = set()
_open_events:         set[str]         = set()  # event_ticker -> blocks opposite side of same game
_addon_counts:        dict[str, int]   = {}   # ticker -> add-ons placed so far
_position_cost:       dict[str, float] = {}   # ticker -> total cost USD (initial + add-ons)
_sl_cooldown:         dict[str, float] = {}   # ticker -> timestamp of most recent SL exit
_sl_cooldown_blocks:  dict[str, int]   = {}   # ticker -> number of times blocked by SL cooldown
_position_entry_ts:   dict[str, float] = {}   # ticker -> unix timestamp of first entry

SL_COOLDOWN_SEC = 3600  # block re-entry for 60 min after a stop-loss


def _event_ticker(ticker: str) -> str:
    """Strip the final -TEAM suffix to get the Kalshi event key."""
    return ticker.rsplit("-", 1)[0] if "-" in ticker else ticker


def _load_open_tickers():
    """Rebuild tracking state from all order logs minus exit logs at startup."""
    global _open_tickers, _open_events, _addon_counts, _position_cost, _sl_cooldown, _position_entry_ts
    bought:    set[str]         = set()
    addon_c:   dict[str, int]   = {}
    pos_cost:  dict[str, float] = {}
    entry_ts:  dict[str, float] = {}
    for f in (ORDERS_DRY_FILE, ORDERS_LIVE_FILE):
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    o = json.loads(line)
                    if o.get("status") in ("dry_run", "submitted", "resting", "executed") and o.get("contracts", 0) > 0:
                        t = o["ticker"]
                        bought.add(t)
                        if o.get("is_addon"):
                            addon_c[t] = addon_c.get(t, 0) + 1
                        pos_cost[t] = pos_cost.get(t, 0.0) + float(o.get("cost_usd", 0))
                        if t not in entry_ts and o.get("ts"):
                            entry_ts[t] = datetime.fromisoformat(
                                o["ts"].replace("Z", "+00:00")
                            ).timestamp()
                except Exception:
                    pass
    exited: set[str] = set()
    for f in (EXITS_DRY_FILE, EXITS_LIVE_FILE):
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    exited.add(json.loads(line)["ticker"])
                except Exception:
                    pass
    # Also exclude tickers already in settlements.jsonl — prevents re-discovering
    # Kalshi-settled positions as "open" after a scanner restart.
    if SETTLEMENTS_FILE.exists():
        for line in SETTLEMENTS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                exited.add(json.loads(line)["ticker"])
            except Exception:
                pass
    _open_tickers      = bought - exited
    _open_events       = {_event_ticker(t) for t in _open_tickers}
    _addon_counts      = {t: v for t, v in addon_c.items()   if t not in exited}
    _position_cost     = {t: v for t, v in pos_cost.items()  if t not in exited}
    _position_entry_ts = {t: v for t, v in entry_ts.items()  if t not in exited}
    # Reconstruct SL cooldowns from exit logs so restarts don't clear the re-entry block.
    cooldown: dict[str, float] = {}
    for f in (EXITS_DRY_FILE, EXITS_LIVE_FILE):
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    x = json.loads(line)
                    if x.get("reason") == "stop_loss":
                        t  = x.get("ticker", "")
                        ts = x.get("ts", "")
                        if t and ts:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            cooldown[t] = max(cooldown.get(t, 0), dt.timestamp())
                except Exception:
                    pass
    _sl_cooldown = cooldown


_load_open_tickers()


# ── Order result ──────────────────────────────────────────────────────────────

@dataclass
class OrderResult:
    ts:           str
    ticker:       str
    player:       str
    side:         str      # 'yes' or 'no'
    contracts:    int
    price_cents:  int
    cost_usd:     float
    edge:         float
    source:       str      # 'arb' or 'live'
    dry_run:      bool
    order_id:     str  = ""
    status:       str  = ""
    error:        str  = ""
    is_addon:     bool = False
    sport:        str  = ""
    score_state:  str  = ""
    model_prob:   float = 0.0
    score_diff:   int  = 0   # our team/player runs or sets ahead (negative = trailing)
    bid_cents:    int  = 0   # yes_bid at order time (spread = ask - bid)
    spread_cents: int  = 0   # ask_cents - bid_cents at entry
    # Game state (numeric)
    inning:        int   = 0
    half:          str   = ""
    current_set:   int   = 0
    outs:          int   = -1
    # Base state
    on_first:      bool  = False
    on_second:     bool  = False
    on_third:      bool  = False
    scoring_1plus: float = 0.0
    # Three-signal model
    markov_prob:     float = 0.0
    espn_win_prob:   float = 0.0
    vegas_live_prob: float = 0.0
    vegas_open_prob: float = 0.0
    # Entry context
    is_live:      bool  = True    # False = pre-game arb order
    pregame_ask:  float = 0.0     # Kalshi ask before game started (0 = unavailable)
    home_away:    str   = ""      # "HOME" | "AWAY" | ""


@dataclass
class ExitResult:
    ts:                str
    ticker:            str
    side:              str
    contracts:         int
    entry_cents:       int
    exit_cents:        int
    pnl_usd:           float
    reason:            str   # 'stop_loss' or 'profit_take'
    dry_run:           bool
    order_id:          str   = ""
    status:            str   = ""
    error:             str   = ""
    hold_duration_sec: int   = 0
    slippage_pct:      float = 0.0  # (entry - exit) / entry; negative = favourable (PT)


# ── Core executor ─────────────────────────────────────────────────────────────

def execute(
    client: KalshiClient,
    ticker: str,
    player: str,
    side: str,
    ask_dollars: float,
    kelly_usd: float,
    edge: float,
    source: str,
    sport: str = "",
    score_state: str = "",
    model_prob: float = 0.0,
    score_diff: int = 0,
    inning: int = 0,
    half: str = "",
    current_set: int = 0,
    outs: int = -1,
    on_first: bool = False,
    on_second: bool = False,
    on_third: bool = False,
    scoring_1plus: float = 0.0,
    markov_prob: float = 0.0,
    espn_win_prob: float = 0.0,
    vegas_live_prob: float = 0.0,
    vegas_open_prob: float = 0.0,
    is_live: bool = True,
    pregame_ask: float = 0.0,
    home_away: str = "",
) -> OrderResult:
    """
    Place a limit order. Returns an OrderResult regardless of success/failure.

    side        : 'yes' or 'no'
    ask_dollars : current ask price (0.0–1.0)
    kelly_usd   : recommended bet size from Kelly criterion
    """
    ts = datetime.now(timezone.utc).isoformat()

    # ── Validation ────────────────────────────────────────────────────────────
    if ticker in _open_tickers:
        return _skip(ts, ticker, player, side, ask_dollars, kelly_usd, edge, source,
                     "already holding position in this market")

    ev = _event_ticker(ticker)
    if ev in _open_events:
        return _skip(ts, ticker, player, side, ask_dollars, kelly_usd, edge, source,
                     f"already holding opposite side of this game ({ev})")

    if ask_dollars < MIN_ASK:
        return _skip(ts, ticker, player, side, ask_dollars, kelly_usd, edge, source,
                     f"yes_ask {ask_dollars:.2f} below min {MIN_ASK:.2f} — market has priced out player")

    if edge < MIN_EDGE:
        return _skip(ts, ticker, player, side, ask_dollars, kelly_usd, edge, source,
                     f"edge {edge:.1%} below minimum {MIN_EDGE:.1%}")

    bet_usd = min(kelly_usd, MAX_BET_USD)
    if bet_usd < MIN_BET_USD:
        return _skip(ts, ticker, player, side, ask_dollars, kelly_usd, edge, source,
                     f"bet ${bet_usd:.2f} below minimum ${MIN_BET_USD:.2f}")

    # ── Size ──────────────────────────────────────────────────────────────────
    # Kalshi contracts pay $1. Cost per YES contract = ask_dollars.
    # Cost per NO contract = 1 - ask_dollars (the NO ask price).
    price = ask_dollars if side == "yes" else 1 - ask_dollars
    if price <= 0:
        return _skip(ts, ticker, player, side, ask_dollars, kelly_usd, edge, source,
                     "invalid price")

    contracts = max(1, int(bet_usd / price))
    cost_usd  = contracts * price
    price_cents = round(ask_dollars * 100)   # Kalshi always uses yes_price in cents

    result = OrderResult(
        ts=ts,
        ticker=ticker,
        player=player,
        side=side,
        contracts=contracts,
        price_cents=price_cents,
        cost_usd=cost_usd,
        edge=edge,
        source=source,
        dry_run=DRY_RUN,
        sport=sport,
        score_state=score_state,
        model_prob=model_prob,
        score_diff=score_diff,
        inning=inning,
        half=half,
        current_set=current_set,
        outs=outs,
        on_first=on_first,
        on_second=on_second,
        on_third=on_third,
        scoring_1plus=scoring_1plus,
        markov_prob=markov_prob,
        espn_win_prob=espn_win_prob,
        vegas_live_prob=vegas_live_prob,
        vegas_open_prob=vegas_open_prob,
        is_live=is_live,
        pregame_ask=pregame_ask,
        home_away=home_away,
    )

    # ── Balance check ─────────────────────────────────────────────────────────
    try:
        balance = client.get_balance().get("balance", 0) / 100
        if cost_usd > balance:
            result.status = "skipped"
            result.error  = f"insufficient balance ${balance:.2f} < ${cost_usd:.2f}"
            _log(result)
            return result
    except Exception as e:
        result.status = "error"
        result.error  = f"balance check failed: {e}"
        _log(result)
        return result

    # ── Liquidity check ───────────────────────────────────────────────────────
    try:
        market = client.get_market(ticker)
        size_field = "yes_ask_size_fp" if side == "yes" else "no_ask_size_fp"
        available = float(market.get(size_field) or 0)
        if available < contracts:
            result.status = "skipped"
            result.error  = f"insufficient liquidity: need {contracts}, available {available:.0f}"
            _log(result)
            return result
        bid_d = float(market.get("yes_bid_dollars") or 0)
        result.bid_cents    = round(bid_d * 100)
        result.spread_cents = price_cents - result.bid_cents
    except Exception as e:
        result.status = "error"
        result.error  = f"liquidity check failed: {e}"
        _log(result)
        return result

    # ── Place ─────────────────────────────────────────────────────────────────
    if DRY_RUN:
        result.status   = "dry_run"
        result.order_id = f"dry-{uuid.uuid4().hex[:8]}"
        _open_tickers.add(ticker)
        _open_events.add(ev)
        _position_cost[ticker]     = cost_usd
        _position_entry_ts[ticker] = time.time()
        _print(result)
        _log(result)
        return result

    try:
        resp = client.place_order(
            ticker=ticker,
            side=side,
            count=contracts,
            yes_price=price_cents,
            order_type="limit",
        )
        result.order_id = resp.get("order", {}).get("order_id", "")
        result.status   = resp.get("order", {}).get("status", "submitted")
        if result.status in ("submitted", "resting", "executed"):
            _open_tickers.add(ticker)
            _open_events.add(ev)
            _position_cost[ticker]     = cost_usd
            _position_entry_ts[ticker] = time.time()
            notifier.notify_buy(ticker, player, side, contracts,
                                price_cents, cost_usd, edge)
        _print(result)
        _log(result)
    except Exception as e:
        result.status = "error"
        result.error  = str(e)
        _log(result)

    return result


# ── Signal adapters ───────────────────────────────────────────────────────────

def execute_arb(client: KalshiClient, signal: ArbSignal) -> OrderResult:
    """Execute an arb scanner signal. Only buys YES (Kalshi is underpriced)."""
    if signal.edge <= 0:
        return _skip(
            datetime.now(timezone.utc).isoformat(),
            signal.ticker, signal.player, "yes",
            signal.kalshi_ask, signal.kelly_usd, signal.edge, "arb",
            "no positive edge",
        )
    return execute(
        client=client,
        ticker=signal.ticker,
        player=signal.player,
        side="yes",
        ask_dollars=signal.kalshi_ask,
        kelly_usd=signal.kelly_usd,
        edge=signal.edge,
        source="arb",
        is_live=False,
    )


def execute_live(client: KalshiClient, signal: LiveSignal, pregame_ask: float = 0.0) -> OrderResult:
    """Execute a live model signal. Buys YES if model says underpriced."""
    if signal.edge <= 0:
        return _skip(
            datetime.now(timezone.utc).isoformat(),
            signal.ticker, signal.player, "yes",
            signal.kalshi_ask, signal.kelly_usd, signal.edge, "live",
            "model says overpriced — no trade",
        )
    if MIN_MODEL_PROB > 0 and signal.model_prob < MIN_MODEL_PROB:
        return _skip(
            datetime.now(timezone.utc).isoformat(),
            signal.ticker, signal.player, "yes",
            signal.kalshi_ask, signal.kelly_usd, signal.edge, "live",
            f"model_prob {signal.model_prob:.2f} below min {MIN_MODEL_PROB:.2f}",
        )
    if MAX_ENTRY_PRICE > 0 and signal.kalshi_ask > MAX_ENTRY_PRICE:
        return _skip(
            datetime.now(timezone.utc).isoformat(),
            signal.ticker, signal.player, "yes",
            signal.kalshi_ask, signal.kelly_usd, signal.edge, "live",
            f"ask {signal.kalshi_ask:.2f} above max_entry_price {MAX_ENTRY_PRICE:.2f}",
        )
    sl_ts = _sl_cooldown.get(signal.ticker, 0)
    if time.time() - sl_ts < SL_COOLDOWN_SEC:
        remaining = int(SL_COOLDOWN_SEC - (time.time() - sl_ts))
        _sl_cooldown_blocks[signal.ticker] = _sl_cooldown_blocks.get(signal.ticker, 0) + 1
        block_n = _sl_cooldown_blocks[signal.ticker]
        return _skip(
            datetime.now(timezone.utc).isoformat(),
            signal.ticker, signal.player, "yes",
            signal.kalshi_ask, signal.kelly_usd, signal.edge, "live",
            f"SL cooldown active — {remaining}s remaining (block #{block_n})",
        )

    # ── Baseball strategy filters (deepdive5.py analysis) ────────────────────
    if signal.sport == "baseball":
        _outs       = getattr(signal, "outs", -1)
        _inning     = getattr(signal, "inning", 0)
        _score_diff = getattr(signal, "score_diff", 0)
        _espn       = getattr(signal, "espn_win_prob", 0.0)
        _vegas      = getattr(signal, "vegas_live_prob", 0.0)
        if REQUIRE_ESPN_OR_VEGAS and _espn == 0.0 and _vegas == 0.0:
            return _skip(
                datetime.now(timezone.utc).isoformat(),
                signal.ticker, signal.player, "yes",
                signal.kalshi_ask, signal.kelly_usd, signal.edge, "live",
                "baseball: no ESPN/Vegas data — markov-only signal skipped",
            )
        if SKIP_TWO_OUTS and _outs == 2:
            return _skip(
                datetime.now(timezone.utc).isoformat(),
                signal.ticker, signal.player, "yes",
                signal.kalshi_ask, signal.kelly_usd, signal.edge, "live",
                "baseball: 2 outs — historically 27% WR",
            )
        if SKIP_LATE_SLIM_LEAD and _inning >= 6 and _score_diff == 1:
            return _skip(
                datetime.now(timezone.utc).isoformat(),
                signal.ticker, signal.player, "yes",
                signal.kalshi_ask, signal.kelly_usd, signal.edge, "live",
                f"baseball: inning {_inning} with +1 lead — historically 14-18% WR",
            )

    return execute(
        client=client,
        ticker=signal.ticker,
        player=signal.player,
        side="yes",
        ask_dollars=signal.kalshi_ask,
        kelly_usd=signal.kelly_usd,
        edge=signal.edge,
        source="live",
        sport=signal.sport,
        score_state=signal.score_state,
        model_prob=signal.model_prob,
        score_diff=getattr(signal, "score_diff", 0),
        inning=getattr(signal, "inning", 0),
        half=getattr(signal, "half", ""),
        current_set=getattr(signal, "current_set", 0),
        outs=getattr(signal, "outs", -1),
        on_first=getattr(signal, "on_first", False),
        on_second=getattr(signal, "on_second", False),
        on_third=getattr(signal, "on_third", False),
        scoring_1plus=getattr(signal, "scoring_1plus", 0.0),
        markov_prob=getattr(signal, "markov_prob", 0.0),
        espn_win_prob=getattr(signal, "espn_win_prob", 0.0),
        vegas_live_prob=getattr(signal, "vegas_live_prob", 0.0),
        vegas_open_prob=getattr(signal, "vegas_open_prob", 0.0),
        is_live=True,
        pregame_ask=pregame_ask,
        home_away=getattr(signal, "home_away", ""),
    )


# ── Exit executor ─────────────────────────────────────────────────────────────

def execute_exit(
    client: KalshiClient,
    ticker: str,
    side: str,
    contracts: int,
    entry_cents: int,
    bid_cents: int,
    reason: str,
    force_live: bool = False,
) -> ExitResult:
    """
    Sell an open position at the current bid price.
    reason     : 'stop_loss' or 'profit_take'
    force_live : always execute as a real sell, even when DRY_RUN is True.
                 Used for real Kalshi positions that need to be exited regardless
                 of the scanner's current dry-run mode.
    """
    ts      = datetime.now(timezone.utc).isoformat()
    pnl_usd = (bid_cents - entry_cents) * contracts / 100
    is_dry  = DRY_RUN and not force_live

    _ets     = _position_entry_ts.get(ticker, 0.0)
    hold_sec = int(time.time() - _ets) if _ets else 0
    slip_pct = round((entry_cents - bid_cents) / entry_cents, 4) if entry_cents > 0 else 0.0

    result = ExitResult(
        ts=ts, ticker=ticker, side=side, contracts=contracts,
        entry_cents=entry_cents, exit_cents=bid_cents,
        pnl_usd=pnl_usd, reason=reason, dry_run=is_dry,
        hold_duration_sec=hold_sec,
        slippage_pct=slip_pct,
    )

    if is_dry:
        result.status   = "dry_run"
        result.order_id = f"dry-exit-{uuid.uuid4().hex[:8]}"
        _open_tickers.discard(ticker)
        _open_events.discard(_event_ticker(ticker))
        _addon_counts.pop(ticker, None)
        _position_cost.pop(ticker, None)
        _position_entry_ts.pop(ticker, None)
        if reason == "stop_loss":
            _sl_cooldown[ticker] = time.time()
        _log_exit(result)
        return result

    try:
        resp = client.sell_order(
            ticker=ticker,
            side=side,
            count=contracts,
            yes_price=bid_cents,
        )
        result.order_id = resp.get("order", {}).get("order_id", "")
        result.status   = resp.get("order", {}).get("status", "submitted")
        _open_tickers.discard(ticker)
        _open_events.discard(_event_ticker(ticker))
        _addon_counts.pop(ticker, None)
        _position_cost.pop(ticker, None)
        _position_entry_ts.pop(ticker, None)
        if reason == "stop_loss":
            _sl_cooldown[ticker] = time.time()
        notifier.notify_sell(ticker, side, contracts,
                             entry_cents, bid_cents, pnl_usd, reason)
        _log_exit(result)
    except Exception as e:
        result.status = "error"
        result.error  = str(e)
        _log_exit(result)

    return result


# ── Add-on executor ──────────────────────────────────────────────────────────

def execute_addon(
    client: KalshiClient,
    ticker: str,
    player: str,
    side: str,
    ask_dollars: float,
    kelly_usd: float,
    edge: float,
) -> OrderResult:
    """
    Add to an existing position (double-down).
    Bypasses the _open_tickers dedup check; all other guards still apply.
    Call only after the caller has verified double-down conditions are met.
    """
    ts = datetime.now(timezone.utc).isoformat()

    sl_ts = _sl_cooldown.get(ticker, 0)
    if time.time() - sl_ts < SL_COOLDOWN_SEC:
        remaining = int(SL_COOLDOWN_SEC - (time.time() - sl_ts))
        return _skip(ts, ticker, player, side, ask_dollars, kelly_usd, edge, "live",
                     f"SL cooldown active — addon blocked ({remaining}s remaining)")

    if ask_dollars < MIN_ASK:
        return _skip(ts, ticker, player, side, ask_dollars, kelly_usd, edge, "live",
                     f"yes_ask {ask_dollars:.2f} below min {MIN_ASK:.2f}")

    bet_usd = min(kelly_usd, MAX_BET_USD)
    if bet_usd < MIN_BET_USD:
        return _skip(ts, ticker, player, side, ask_dollars, kelly_usd, edge, "live",
                     f"bet ${bet_usd:.2f} below minimum ${MIN_BET_USD:.2f}")

    price = ask_dollars if side == "yes" else 1 - ask_dollars
    if price <= 0:
        return _skip(ts, ticker, player, side, ask_dollars, kelly_usd, edge, "live",
                     "invalid price")

    contracts   = max(1, int(bet_usd / price))
    cost_usd    = contracts * price
    price_cents = round(ask_dollars * 100)

    result = OrderResult(
        ts=ts, ticker=ticker, player=player, side=side,
        contracts=contracts, price_cents=price_cents, cost_usd=cost_usd,
        edge=edge, source="live", dry_run=DRY_RUN, is_addon=True,
    )

    # Balance check
    try:
        balance = client.get_balance().get("balance", 0) / 100
        if cost_usd > balance:
            result.status = "skipped"
            result.error  = f"insufficient balance ${balance:.2f} < ${cost_usd:.2f}"
            _log(result)
            return result
    except Exception as e:
        result.status = "error"
        result.error  = f"balance check failed: {e}"
        _log(result)
        return result

    # Liquidity check
    try:
        market = client.get_market(ticker)
        size_field = "yes_ask_size_fp" if side == "yes" else "no_ask_size_fp"
        available  = float(market.get(size_field) or 0)
        if available < contracts:
            result.status = "skipped"
            result.error  = f"insufficient liquidity: need {contracts}, available {available:.0f}"
            _log(result)
            return result
    except Exception as e:
        result.status = "error"
        result.error  = f"liquidity check failed: {e}"
        _log(result)
        return result

    if DRY_RUN:
        result.status   = "dry_run"
        result.order_id = f"dry-addon-{uuid.uuid4().hex[:8]}"
        _addon_counts[ticker]  = _addon_counts.get(ticker, 0) + 1
        _position_cost[ticker] = _position_cost.get(ticker, 0.0) + cost_usd
        _print(result)
        _log(result)
        return result

    try:
        resp = client.place_order(
            ticker=ticker,
            side=side,
            count=contracts,
            yes_price=price_cents,
            order_type="limit",
        )
        result.order_id = resp.get("order", {}).get("order_id", "")
        result.status   = resp.get("order", {}).get("status", "submitted")
        if result.status in ("submitted", "resting", "executed"):
            _addon_counts[ticker]  = _addon_counts.get(ticker, 0) + 1
            _position_cost[ticker] = _position_cost.get(ticker, 0.0) + cost_usd
            notifier.notify_buy(ticker, player, side, contracts,
                                price_cents, cost_usd, edge)
        _print(result)
        _log(result)
    except Exception as e:
        result.status = "error"
        result.error  = str(e)
        _log(result)

    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _skip(ts, ticker, player, side, ask, kelly, edge, source, reason) -> OrderResult:
    r = OrderResult(
        ts=ts, ticker=ticker, player=player, side=side,
        contracts=0, price_cents=round(ask * 100), cost_usd=0,
        edge=edge, source=source, dry_run=DRY_RUN,
        status="skipped", error=reason,
    )
    return r


def _print(r: OrderResult):
    tag    = "[DRY RUN]" if r.dry_run else "[LIVE]"
    action = "ADDON" if r.is_addon else "BUY "
    print(
        f"{tag} {r.source.upper():4}  {r.player:<25} "
        f"{action} {r.contracts} YES @ {r.price_cents}¢  "
        f"cost=${r.cost_usd:.2f}  edge={r.edge:+.1%}  "
        f"id={r.order_id}  status={r.status}"
    )


def _log(r: OrderResult):
    f = ORDERS_DRY_FILE if r.dry_run else ORDERS_LIVE_FILE
    with f.open("a") as fh:
        fh.write(json.dumps(asdict(r)) + "\n")


def _log_exit(r: ExitResult):
    f = EXITS_DRY_FILE if r.dry_run else EXITS_LIVE_FILE
    with f.open("a") as fh:
        fh.write(json.dumps(asdict(r)) + "\n")
