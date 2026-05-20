import os
import re
import sys
import requests
from datetime import datetime, timezone
from difflib import SequenceMatcher
from dataclasses import dataclass
from kalshi_client import KalshiClient
import config

KALSHI_API_KEY = config.KALSHI_API_KEY
ODDS_API_KEY   = config.ODDS_API_KEY
ODDS_API_BASE  = "https://api.the-odds-api.com/v4"

BOOKMAKERS = ["draftkings", "fanduel", "betmgm", "caesars", "pointsbetus"]
MIN_EDGE       = 0.03   # flag opportunities with edge >= 3 cents
KELLY_FRACTION = 0.5    # half-Kelly to reduce variance


@dataclass
class ArbSignal:
    ticker:     str
    player:     str
    kalshi_ask: float   # cost to buy YES on Kalshi (0.0–1.0)
    book_prob:  float   # best implied probability across bookmakers
    bookmaker:  str
    edge:       float   # book_prob - kalshi_ask
    kelly_usd:  float   # recommended bet size in dollars
    is_live:    bool    # True if match has already started


# ── Kalshi ────────────────────────────────────────────────────────────────────

def fetch_kalshi_tennis(client: KalshiClient) -> list[dict]:
    markets = client.get_tennis_markets(status="open")
    now = datetime.now(timezone.utc)
    result = []
    for m in markets:
        # skip resolved markets; keep pre-match and live (in-play) markets
        if m.get("result"):
            continue

        ask = m.get("yes_ask_dollars")
        bid = m.get("yes_bid_dollars")
        if ask is None:
            continue
        ask = float(ask)
        if ask <= 0 or ask >= 1:
            continue
        match = re.match(r"Will (.+?) win", m.get("title", ""))
        if not match:
            continue
        occurrence = m.get("occurrence_datetime")
        is_live = False
        if occurrence:
            match_time = datetime.fromisoformat(occurrence.replace("Z", "+00:00"))
            is_live = match_time < now

        result.append({
            "ticker":  m["ticker"],
            "title":   m["title"],
            "player":  match.group(1),
            "ask":     ask,
            "bid":     float(bid) if bid else None,
            "is_live": is_live,
        })
    return result


# ── Sportsbooks ───────────────────────────────────────────────────────────────

def fetch_active_tennis_sports(api_key: str) -> list[str]:
    resp = requests.get(
        f"{ODDS_API_BASE}/sports",
        params={"apiKey": api_key},
        timeout=10,
    )
    resp.raise_for_status()
    return [
        s["key"] for s in resp.json()
        if "tennis" in s["key"].lower() and s.get("active")
    ]


def fetch_book_events(sport_key: str, api_key: str) -> list[dict]:
    resp = requests.get(
        f"{ODDS_API_BASE}/sports/{sport_key}/odds",
        params={
            "apiKey":      api_key,
            "regions":     "us",
            "markets":     "h2h",
            "bookmakers":  ",".join(BOOKMAKERS),
            "oddsFormat":  "american",
        },
        timeout=10,
    )
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return resp.json()


# ── Math ──────────────────────────────────────────────────────────────────────

def american_to_prob(odds: float) -> float:
    if odds > 0:
        return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)


def name_similarity(a: str, b: str) -> float:
    a, b = a.lower().strip(), b.lower().strip()
    # last-name exact match is most reliable
    if a.split()[-1] == b.split()[-1]:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def best_book_prob(event: dict, player: str) -> tuple[float, str] | None:
    """Highest implied probability for a player across all bookmakers in an event."""
    best_prob, best_book = 0.0, ""
    for bm in event.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market["key"] != "h2h":
                continue
            for outcome in market.get("outcomes", []):
                if name_similarity(outcome["name"], player) > 0.75:
                    prob = american_to_prob(outcome["price"])
                    if prob > best_prob:
                        best_prob, best_book = prob, bm["title"]
    return (best_prob, best_book) if best_prob > 0 else None


def kelly_bet(prob: float, ask: float, bankroll_usd: float, fraction: float = KELLY_FRACTION) -> float:
    """Fractional Kelly bet size in dollars."""
    b = (1 - ask) / ask     # net profit per $1 wagered if YES wins
    q = 1 - prob
    f = max(0.0, (b * prob - q) / b) * fraction
    return round(f * bankroll_usd, 2)


# ── Scanner ───────────────────────────────────────────────────────────────────

def scan(client: KalshiClient, api_key: str, min_edge: float = MIN_EDGE) -> list[ArbSignal]:
    print("Fetching Kalshi tennis markets...")
    kalshi = fetch_kalshi_tennis(client)
    print(f"  {len(kalshi)} markets with live prices\n")

    print("Fetching active tennis sports from The Odds API...")
    sports = fetch_active_tennis_sports(api_key)
    print(f"  {len(sports)} active: {sports}\n")

    print("Fetching sportsbook odds...")
    all_events: list[dict] = []
    for sport in sports:
        events = fetch_book_events(sport, api_key)
        all_events.extend(events)
        print(f"  {sport}: {len(events)} events")
    print(f"  {len(all_events)} total sportsbook events loaded\n")

    balance_usd = client.get_balance().get("balance", 0) / 100

    signals: list[ArbSignal] = []
    for km in kalshi:
        best = None
        for event in all_events:
            result = best_book_prob(event, km["player"])
            if result and (best is None or result[0] > best[0]):
                best = result
        if best is None:
            continue
        book_prob, bookmaker = best
        edge = book_prob - km["ask"]
        if edge >= min_edge:
            signals.append(ArbSignal(
                ticker=km["ticker"],
                player=km["player"],
                kalshi_ask=km["ask"],
                book_prob=book_prob,
                bookmaker=bookmaker,
                edge=edge,
                kelly_usd=kelly_bet(book_prob, km["ask"], balance_usd),
                is_live=km["is_live"],
            ))

    signals.sort(key=lambda s: s.edge, reverse=True)
    return signals


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if not ODDS_API_KEY:
        print("ERROR: ODDS_API_KEY not set.")
        print("  1. Sign up at https://the-odds-api.com/ (free tier available)")
        print("  2. Then run:  $env:ODDS_API_KEY='your-key-here'  (PowerShell)")
        sys.exit(1)

    client = KalshiClient(api_key_id=KALSHI_API_KEY)
    signals = scan(client, ODDS_API_KEY)

    if not signals:
        print("No arbitrage opportunities found above threshold.")
        return

    w = 100
    print(f"\n{'='*w}")
    print(f"{'ARB SIGNALS':^{w}}")
    print(f"{'='*w}")
    print(f"{'Player':<25} {'Kalshi Ask':>11} {'Book Prob':>10} {'Edge':>7} {'Book':<16} {'Kelly $':>8}  {'Status':<10}  Ticker")
    print("-" * w)
    for s in signals:
        status = "LIVE" if s.is_live else "PRE-MATCH"
        print(
            f"{s.player:<25} {s.kalshi_ask:>10.1%} {s.book_prob:>10.1%} "
            f"{s.edge:>+7.1%} {s.bookmaker:<16} ${s.kelly_usd:>7.2f}  {status:<10}  {s.ticker}"
        )
    print(f"\n{len(signals)} signal(s) found  |  min edge: {MIN_EDGE:.0%}  |  Kelly fraction: {KELLY_FRACTION}")


if __name__ == "__main__":
    main()
