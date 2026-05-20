"""
Unified runner.

Loops both scanners on a schedule and feeds signals through the order executor.
Tracks open positions and avoids doubling up on the same market.

Usage:
    python3 runner.py              # dry-run, default intervals
    python3 runner.py --live       # real orders (DRY_RUN=False in order_executor.py first!)
    python3 runner.py --once       # single scan then exit
"""

import os
import sys
import time
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from kalshi_client import KalshiClient
from arb_scanner import scan as arb_scan, ArbSignal
from live_scanner import scan_live, LiveSignal
import order_executor as executor
import config

KALSHI_API_KEY = config.KALSHI_API_KEY
ODDS_API_KEY   = config.ODDS_API_KEY

ARB_INTERVAL_SEC  = 300   # re-run arb scanner every 5 minutes
LIVE_INTERVAL_SEC = 30    # re-run live scanner every 30 seconds
POSITIONS_FILE    = Path("positions.jsonl")


# ── Position tracker ──────────────────────────────────────────────────────────

class PositionTracker:
    """
    Tracks open positions so we don't double-buy the same market.
    Persists to positions.jsonl.
    """

    def __init__(self):
        self.positions: dict[str, dict] = {}   # ticker → position dict
        self._load()

    def _load(self):
        if not POSITIONS_FILE.exists():
            return
        for line in POSITIONS_FILE.read_text().splitlines():
            try:
                p = json.loads(line)
                if p.get("status") == "open":
                    self.positions[p["ticker"]] = p
            except Exception:
                pass

    def has(self, ticker: str) -> bool:
        return ticker in self.positions

    def add(self, ticker: str, player: str, contracts: int, price_cents: int, source: str):
        p = {
            "ticker":      ticker,
            "player":      player,
            "contracts":   contracts,
            "price_cents": price_cents,
            "cost_usd":    contracts * price_cents / 100,
            "source":      source,
            "opened_at":   datetime.now(timezone.utc).isoformat(),
            "status":      "open",
        }
        self.positions[ticker] = p
        with POSITIONS_FILE.open("a") as f:
            f.write(json.dumps(p) + "\n")

    def summary(self) -> str:
        if not self.positions:
            return "No open positions."
        lines = [f"{'Ticker':<50} {'Player':<25} {'Cts':>5} {'Price':>6} {'Cost':>8}  Source"]
        lines.append("-" * 100)
        total = 0.0
        for p in self.positions.values():
            cost = p["cost_usd"]
            total += cost
            lines.append(
                f"{p['ticker']:<50} {p['player']:<25} {p['contracts']:>5} "
                f"{p['price_cents']:>5}¢ ${cost:>7.2f}  {p['source']}"
            )
        lines.append(f"\nTotal at risk: ${total:.2f}")
        return "\n".join(lines)


# ── Scanner wrappers ──────────────────────────────────────────────────────────

def run_arb_scanner(client: KalshiClient, tracker: PositionTracker):
    if not ODDS_API_KEY:
        print("  [arb] ODDS_API_KEY not set — skipping arb scan")
        return

    print(f"\n{'-'*60}")
    print(f"  ARB SCAN  {_now()}")
    print(f"{'-'*60}")
    try:
        signals = arb_scan(client, ODDS_API_KEY)
    except Exception as e:
        print(f"  [arb] scan failed: {e}")
        return

    acted = 0
    for sig in signals:
        if sig.is_live:
            continue                    # skip live arb (stale book prices)
        if tracker.has(sig.ticker):
            print(f"  [arb] already in {sig.ticker} — skip")
            continue
        result = executor.execute_arb(client, sig)
        if result.status in ("submitted", "resting", "dry_run") and result.contracts > 0:
            tracker.add(sig.ticker, sig.player, result.contracts, result.price_cents, "arb")
            acted += 1

    if acted == 0:
        print("  [arb] no actionable signals")


def run_live_scanner(client: KalshiClient, tracker: PositionTracker):
    print(f"\n{'-'*60}")
    print(f"  LIVE SCAN  {_now()}")
    print(f"{'-'*60}")
    try:
        signals = scan_live(client)
    except Exception as e:
        print(f"  [live] scan failed: {e}")
        return

    acted = 0
    for sig in signals:
        if sig.edge <= 0:
            continue                    # only buy underpriced markets
        if tracker.has(sig.ticker):
            print(f"  [live] already in {sig.ticker} — skip")
            continue
        result = executor.execute_live(client, sig)
        if result.status in ("submitted", "resting", "dry_run") and result.contracts > 0:
            tracker.add(sig.ticker, sig.player, result.contracts, result.price_cents, "live")
            acted += 1

    if acted == 0:
        print("  [live] no actionable signals")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true",
                        help="Place real orders (set DRY_RUN=False in order_executor.py first)")
    parser.add_argument("--once", action="store_true",
                        help="Run one scan cycle then exit")
    args = parser.parse_args()

    if args.live:
        executor.DRY_RUN = False
        print("*** LIVE MODE - real orders will be placed ***")
    else:
        print("DRY RUN MODE — no real orders")

    client  = KalshiClient(api_key_id=KALSHI_API_KEY)
    tracker = PositionTracker()

    print(f"\nStarting runner  |  arb every {ARB_INTERVAL_SEC}s  |  live every {LIVE_INTERVAL_SEC}s")
    print(f"DRY_RUN={executor.DRY_RUN}  MAX_BET=${executor.MAX_BET_USD}  MIN_EDGE={executor.MIN_EDGE:.0%}\n")

    if tracker.positions:
        print("-- Existing positions --")
        print(tracker.summary())

    last_arb = 0.0

    try:
        while True:
            now = time.time()

            # Arb scan (throttled)
            if now - last_arb >= ARB_INTERVAL_SEC:
                run_arb_scanner(client, tracker)
                last_arb = now

            # Live scan (frequent)
            run_live_scanner(client, tracker)

            if args.once:
                break

            print(f"\n  sleeping {LIVE_INTERVAL_SEC}s …  (Ctrl+C to stop)\n")
            time.sleep(LIVE_INTERVAL_SEC)

    except KeyboardInterrupt:
        print("\n\nStopped.")

    print("\n-- Final positions --")
    print(tracker.summary())


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


if __name__ == "__main__":
    main()
