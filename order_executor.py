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

# ── Config ────────────────────────────────────────────────────────────────────

DRY_RUN     = True    # SAFETY: set False only to place real orders
MAX_BET_USD = 25.0    # hard cap per order regardless of Kelly
MIN_BET_USD = 1.0     # ignore signals below this size
MIN_EDGE    = 0.03    # skip signals below this edge even if passed in
MIN_ASK     = 0.05    # skip if market prices YES below this — near-zero asks mean
                      # the market has almost certainly priced out this player already

STOP_LOSS_PCT   = 0.35   # sell if bid drops this fraction below entry price
PROFIT_TAKE_PCT = 0.50   # sell if bid rises this fraction above entry price

ORDERS_DRY_FILE  = Path("orders_dry.jsonl")
ORDERS_LIVE_FILE = Path("orders_live.jsonl")
EXITS_DRY_FILE   = Path("exits_dry.jsonl")
EXITS_LIVE_FILE  = Path("exits_live.jsonl")

# ── Open-position deduplication ───────────────────────────────────────────────
# Tickers we already hold (bought but not yet exited/settled).
# Prevents re-buying the same live market every scan cycle.

_open_tickers: set[str] = set()


def _load_open_tickers():
    """Rebuild _open_tickers from all order logs minus exit logs at startup."""
    global _open_tickers
    bought: set[str] = set()
    for f in (ORDERS_DRY_FILE, ORDERS_LIVE_FILE):
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    o = json.loads(line)
                    if o.get("status") in ("dry_run", "submitted", "resting") and o.get("contracts", 0) > 0:
                        bought.add(o["ticker"])
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
    _open_tickers = bought - exited


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
    order_id:     str = ""
    status:       str = ""
    error:        str = ""


@dataclass
class ExitResult:
    ts:          str
    ticker:      str
    side:        str
    contracts:   int
    entry_cents: int
    exit_cents:  int
    pnl_usd:     float
    reason:      str   # 'stop_loss' or 'profit_take'
    dry_run:     bool
    order_id:    str = ""
    status:      str = ""
    error:       str = ""


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
        if result.status in ("submitted", "resting"):
            _open_tickers.add(ticker)
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
    )


def execute_live(client: KalshiClient, signal: LiveSignal) -> OrderResult:
    """Execute a live model signal. Buys YES if model says underpriced."""
    if signal.edge <= 0:
        return _skip(
            datetime.now(timezone.utc).isoformat(),
            signal.ticker, signal.player, "yes",
            signal.kalshi_ask, signal.kelly_usd, signal.edge, "live",
            "model says overpriced — no trade",
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
) -> ExitResult:
    """
    Sell an open position at the current bid price.
    reason : 'stop_loss' or 'profit_take'
    """
    ts      = datetime.now(timezone.utc).isoformat()
    pnl_usd = (bid_cents - entry_cents) * contracts / 100

    result = ExitResult(
        ts=ts, ticker=ticker, side=side, contracts=contracts,
        entry_cents=entry_cents, exit_cents=bid_cents,
        pnl_usd=pnl_usd, reason=reason, dry_run=DRY_RUN,
    )

    if DRY_RUN:
        result.status   = "dry_run"
        result.order_id = f"dry-exit-{uuid.uuid4().hex[:8]}"
        _open_tickers.discard(ticker)
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
        _log_exit(result)
    except Exception as e:
        result.status = "error"
        result.error  = str(e)
        _log_exit(result)

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
    tag = "[DRY RUN]" if r.dry_run else "[LIVE]"
    print(
        f"{tag} {r.source.upper():4}  {r.player:<25} "
        f"BUY {r.contracts} YES @ {r.price_cents}¢  "
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
