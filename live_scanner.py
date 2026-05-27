"""
Live in-play scanner.

For each open Kalshi market that has already started (LIVE):
  - Tennis: fetch current score, compute fair value via Markov chain
  - Baseball: fetch current score/inning, compute fair value via Markov chain
  - Flag when Kalshi price diverges from model fair value by >= threshold
"""

import re
import sys
import requests
from datetime import datetime, timezone, timedelta, date
from dataclasses import dataclass

from kalshi_client import KalshiClient
from models.tennis_model import prob_win_match_from_score
from models.baseball_model import prob_home_wins_simple
from models.serve_stats import lookup as serve_pct
import config

KALSHI_API_KEY = config.KALSHI_API_KEY
MIN_EDGE = 0.04          # minimum model edge to flag (4 cents)
KELLY_FRACTION = 0.5


# ── Signal dataclass ──────────────────────────────────────────────────────────

@dataclass
class LiveSignal:
    ticker:      str
    player:      str
    sport:       str
    kalshi_ask:  float
    model_prob:  float
    edge:        float
    kelly_usd:   float
    score_state: str     # human-readable current score
    opponent:    str = ""
    home_away:   str = ""  # "HOME" | "AWAY" | "" (tennis)
    event_ticker: str = ""


# ── Live score feed (ESPN public API) ────────────────────────────────────────

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

GRAND_SLAMS = {"roland garros", "wimbledon", "us open", "australian open"}

def fetch_espn_tennis() -> list[dict]:
    """
    Fetch in-progress ATP and WTA matches from ESPN.
    Returns rich match state: sets, games in current set, server, tour, best-of.
    """
    matches = []
    for tour in ("atp", "wta"):
        try:
            resp = requests.get(f"{ESPN_BASE}/tennis/{tour}/scoreboard", timeout=8)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [ESPN {tour.upper()}] fetch failed: {e}")
            continue

        for event in data.get("events", []):
            event_name = event.get("name", "").lower()
            is_slam = any(s in event_name for s in GRAND_SLAMS)
            best_of = 5 if (is_slam and tour == "atp") else 3

            for grouping in event.get("groupings", []):
                for comp in grouping.get("competitions", []):
                    if comp.get("status", {}).get("type", {}).get("state") != "in":
                        continue
                    competitors = comp.get("competitors", [])
                    if len(competitors) < 2:
                        continue

                    # order=1 is p1, order=2 is p2
                    by_order = {c.get("order", i+1): c for i, c in enumerate(competitors)}
                    c1 = by_order.get(1, competitors[0])
                    c2 = by_order.get(2, competitors[1])

                    p1_name = c1.get("athlete", {}).get("displayName", "")
                    p2_name = c2.get("athlete", {}).get("displayName", "")
                    ls1 = c1.get("linescores", [])
                    ls2 = c2.get("linescores", [])

                    # All but last linescore entry = completed sets
                    completed = min(len(ls1), len(ls2)) - 1
                    sets_p1 = sum(
                        1 for i in range(completed)
                        if ls1[i].get("value", 0) > ls2[i].get("value", 0)
                    )
                    sets_p2 = sum(
                        1 for i in range(completed)
                        if ls2[i].get("value", 0) > ls1[i].get("value", 0)
                    )

                    # Last entry = games in current set
                    games_p1 = int(ls1[-1].get("value", 0)) if ls1 else 0
                    games_p2 = int(ls2[-1].get("value", 0)) if ls2 else 0

                    # possession=True means that player is currently serving
                    p1_serving = bool(c1.get("possession", False))

                    matches.append({
                        "p1":        p1_name,
                        "p2":        p2_name,
                        "sets_p1":   sets_p1,
                        "sets_p2":   sets_p2,
                        "games_p1":  games_p1,
                        "games_p2":  games_p2,
                        "p1_serving": p1_serving,
                        "tour":      tour,
                        "best_of":   best_of,
                    })
    return matches


def fetch_espn_baseball() -> list[dict]:
    """
    Fetch in-progress MLB games from ESPN.
    Returns list with team names, inning, half, and scores.
    """
    games = []
    try:
        resp = requests.get(f"{ESPN_BASE}/baseball/mlb/scoreboard", timeout=8)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [ESPN MLB] fetch failed: {e}")
        return []

    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        status = comp.get("status", {})
        if status.get("type", {}).get("state") != "in":
            continue
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue
        home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
        away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
        period = status.get("period", 1)
        is_bottom = "Bottom" in status.get("type", {}).get("shortDetail", "")
        games.append({
            "home": home.get("team", {}).get("displayName", ""),
            "away": away.get("team", {}).get("displayName", ""),
            "score_home": int(home.get("score", 0)),
            "score_away": int(away.get("score", 0)),
            "inning_str": f"{period}{'B' if is_bottom else 'T'}",
        })
    return games


def parse_inning(inning_str: str) -> tuple[int, bool]:
    """Parse inning string like '5T' or '7B' into (inning, is_bottom)."""
    if not inning_str:
        return 1, False
    s = inning_str.strip().upper()
    is_bottom = s.endswith("B")
    try:
        inning = int(re.sub(r"[^0-9]", "", s)) or 1
    except ValueError:
        inning = 1
    return inning, is_bottom


# ── Name matching ─────────────────────────────────────────────────────────────

from difflib import SequenceMatcher

# Maps Kalshi ticker abbreviations / short names → ESPN displayName substrings
MLB_ALIASES: dict[str, str] = {
    "ATH": "Athletics", "OAK": "Athletics",
    "NYY": "Yankees",   "NY Yankees": "Yankees",
    "NYM": "Mets",      "NY Mets": "Mets",
    "BOS": "Red Sox",
    "TBR": "Rays",      "TB": "Rays",
    "TOR": "Blue Jays",
    "BAL": "Orioles",
    "CHW": "White Sox", "CWS": "White Sox",
    "CHC": "Cubs",
    "MIN": "Twins",
    "DET": "Tigers",
    "CLE": "Guardians",
    "KCR": "Royals",    "KC": "Royals",
    "HOU": "Astros",
    "TEX": "Rangers",
    "LAA": "Angels",
    "SEA": "Mariners",
    "LAD": "Dodgers",
    "SFG": "Giants",    "SF": "Giants",
    "SDP": "Padres",    "SD": "Padres",
    "COL": "Rockies",
    "ARI": "Diamondbacks", "AZ": "Diamondbacks",
    "ATL": "Braves",
    "MIA": "Marlins",
    "NYG": "Mets",   # rare alias
    "PHI": "Phillies",
    "WSN": "Nationals", "WAS": "Nationals",
    "PIT": "Pirates",
    "CIN": "Reds",
    "MIL": "Brewers",
    "STL": "Cardinals",
}


def name_match(a: str, b: str) -> float:
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b:
        return 0.0
    if a.split()[-1] == b.split()[-1]:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _expand_mlb(name: str) -> str:
    """Expand a Kalshi MLB abbreviation to a canonical team word for matching."""
    return MLB_ALIASES.get(name.strip(), MLB_ALIASES.get(name.strip().upper(), name))


def find_live_match(player: str, live_matches: list[dict], key_p1: str, key_p2: str) -> dict | None:
    """Find the live match containing this player (tennis version — last-name fuzzy)."""
    for m in live_matches:
        if name_match(player, m[key_p1]) > 0.75 or name_match(player, m[key_p2]) > 0.75:
            return m
    return None


def find_baseball_game(player: str, live_games: list[dict]) -> dict | None:
    """
    Find the live MLB game for a Kalshi team identifier.
    Handles abbreviations (ATH, SD, CWS) by expanding via MLB_ALIASES first,
    then falls back to substring and fuzzy matching against ESPN full names.
    """
    expanded = _expand_mlb(player)  # e.g. "ATH" -> "Athletics"
    canonical = expanded.lower()

    for g in live_games:
        home_l = g["home"].lower()
        away_l = g["away"].lower()
        # Substring match on expanded alias (e.g. "athletics" in "oakland athletics")
        if canonical in home_l or canonical in away_l:
            return g
        # Fuzzy fallback for full-name inputs
        if name_match(player, g["home"]) > 0.75 or name_match(player, g["away"]) > 0.75:
            return g
    return None


# ── Kelly ─────────────────────────────────────────────────────────────────────

def kelly_bet(prob: float, ask: float, bankroll_usd: float, fraction: float = KELLY_FRACTION) -> float:
    b = (1 - ask) / ask
    f = max(0.0, (b * prob - (1 - prob)) / b) * fraction
    return round(f * bankroll_usd, 2)


# ── Tennis live signal ────────────────────────────────────────────────────────

def tennis_signal(km: dict, live_matches: list[dict], tour: str, bankroll_usd: float) -> LiveSignal | None:
    player = km["player"]
    match = find_live_match(player, live_matches, "p1", "p2")
    if not match:
        return None

    p1_name   = match["p1"]
    p2_name   = match["p2"]
    games_p1  = match["games_p1"]
    games_p2  = match["games_p2"]

    # Skip stale or transitional scores (e.g. 7-6 briefly shown between sets)
    if games_p1 > 6 or games_p2 > 6:
        return None

    p1_is_our_player = name_match(player, p1_name) > 0.75
    match_tour = match.get("tour", tour)

    # Real per-player serve win probabilities
    p1_serve = serve_pct(p1_name, match_tour)
    p2_serve = serve_pct(p2_name, match_tour)

    sets_p1    = match["sets_p1"]
    sets_p2    = match["sets_p2"]
    p1_serving = match["p1_serving"]
    best_of   = match.get("best_of", 3)

    model_p1 = prob_win_match_from_score(
        p1_serve, p2_serve,
        sets_p1=sets_p1, sets_p2=sets_p2,
        games_p1=games_p1, games_p2=games_p2,
        p1_serving=p1_serving,
        best_of=best_of,
    )
    model_prob = model_p1 if p1_is_our_player else 1 - model_p1
    edge = model_prob - km["ask"]

    if abs(edge) < MIN_EDGE:
        return None

    score_state = f"{sets_p1}-{sets_p2} sets, {games_p1}-{games_p2} games (srv: {'P1' if p1_serving else 'P2'})"
    opponent = p2_name if p1_is_our_player else p1_name
    return LiveSignal(
        ticker=km["ticker"],
        player=player,
        sport="tennis",
        kalshi_ask=km["ask"],
        model_prob=model_prob,
        edge=edge,
        kelly_usd=kelly_bet(model_prob, km["ask"], bankroll_usd) if edge > 0 else 0,
        score_state=score_state,
        opponent=opponent,
        home_away="",
        event_ticker=km.get("event_ticker", ""),
    )


# ── Baseball live signal ──────────────────────────────────────────────────────

def baseball_signal(km: dict, live_games: list[dict], bankroll_usd: float) -> LiveSignal | None:
    player = km["player"]  # for baseball this is the team name / abbreviation
    game = find_baseball_game(player, live_games)
    if not game:
        return None

    home = game["home"]
    inning, is_bottom = parse_inning(game["inning_str"])
    score_home = game["score_home"]
    score_away = game["score_away"]

    expanded = _expand_mlb(player)
    player_is_home = expanded.lower() in home.lower() or name_match(player, home) > 0.75
    model_home = prob_home_wins_simple(inning, is_bottom, score_home, score_away)
    model_prob = model_home if player_is_home else 1 - model_home
    edge = model_prob - km["ask"]

    if abs(edge) < MIN_EDGE:
        return None

    half = "Bot" if is_bottom else "Top"
    score_state = f"{half} {inning}, {score_away}-{score_home}"
    away = game["away"]
    opponent = away if player_is_home else home
    return LiveSignal(
        ticker=km["ticker"],
        player=player,
        sport="baseball",
        kalshi_ask=km["ask"],
        model_prob=model_prob,
        edge=edge,
        kelly_usd=kelly_bet(model_prob, km["ask"], bankroll_usd) if edge > 0 else 0,
        score_state=score_state,
        opponent=opponent,
        home_away="HOME" if player_is_home else "AWAY",
        event_ticker=km.get("event_ticker", ""),
    )


# ── Main scanner ──────────────────────────────────────────────────────────────

def scan_live(client: KalshiClient) -> list[LiveSignal]:
    now = datetime.now(timezone.utc)
    balance_usd = client.get_balance().get("balance", 0) / 100

    # Fetch all live Kalshi tennis and baseball markets
    tennis_markets = [
        m for m in client.get_tennis_markets(status="open")
        if not m.get("result")
        and m.get("yes_ask_dollars")
        and m.get("occurrence_datetime")
        and datetime.fromisoformat(m["occurrence_datetime"].replace("Z", "+00:00")) < now
    ]

    # Baseball: occurrence_datetime is the settlement deadline (~3h after first pitch),
    # NOT the start time — filtering dt < now would exclude in-progress games.
    # ESPN match confirms the game is actually live.
    # Date-filter tickers to today + yesterday so future games (MAY29+) are never
    # matched against live ESPN game data from a different day/matchup.
    _MONTHS = {'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,
               'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}
    def _ticker_game_date(ticker: str):
        m = re.search(r'(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})', ticker)
        if not m:
            return None
        return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))

    _today     = now.date()
    _yesterday = _today - timedelta(days=1)
    baseball_markets = [
        m for m in client._get("/markets", params={"limit": 100, "series_ticker": "KXMLBGAME", "status": "open"}).get("markets", [])
        if not m.get("result")
        and m.get("yes_ask_dollars")
        and _ticker_game_date(m.get("ticker", "")) in (_today, _yesterday)
    ]

    print(f"  {len(tennis_markets)} live Kalshi tennis markets")
    print(f"  {len(baseball_markets)} live Kalshi baseball markets")

    # Fetch live scores
    print("Fetching live scores from ESPN...")
    live_tennis = fetch_espn_tennis()
    live_baseball = fetch_espn_baseball()
    print(f"  {len(live_tennis)} live tennis matches, {len(live_baseball)} live baseball games")

    # Build Kalshi market dicts
    def to_km(m: dict) -> dict:
        ask = float(m["yes_ask_dollars"])
        title = m.get("title", "")
        ticker = m.get("ticker", "")
        # Tennis: "Will <Player> win ..."
        tennis_match = re.match(r"Will (.+?) win", title)
        # Baseball: ticker ends in -<TEAM> abbreviation; title is "TeamA vs TeamB Winner?"
        # Use the sub_title field if available, else parse ticker suffix
        sub = m.get("yes_sub_title") or m.get("no_sub_title") or ""
        if tennis_match:
            player = tennis_match.group(1)
        elif sub:
            player = sub
        else:
            # fallback: last segment of ticker
            player = ticker.split("-")[-1]
        return {
            "ticker":       ticker,
            "player":       player,
            "ask":          ask,
            "event_ticker": m.get("event_ticker", ""),
        }

    signals: list[LiveSignal] = []

    for m in tennis_markets:
        km = to_km(m)
        if not km["player"]:
            continue
        tour = "wta" if "WTA" in m["ticker"] else "atp"
        sig = tennis_signal(km, live_tennis, tour, balance_usd)
        if sig:
            signals.append(sig)

    for m in baseball_markets:
        km = to_km(m)
        if not km["player"]:
            continue
        expanded = _expand_mlb(km["player"])
        print(f"  [baseball] {km['ticker']} player='{km['player']}' -> '{expanded}'")
        sig = baseball_signal(km, live_baseball, balance_usd)
        if sig:
            signals.append(sig)

    signals.sort(key=lambda s: abs(s.edge), reverse=True)
    return signals


def main():
    client = KalshiClient(api_key_id=KALSHI_API_KEY)

    print("Scanning live markets...")
    signals = scan_live(client)

    if not signals:
        print("\nNo live signals above threshold.")
        return

    w = 110
    print(f"\n{'='*w}")
    print(f"{'LIVE MODEL SIGNALS':^{w}}")
    print(f"{'='*w}")
    print(f"{'Player':<25} {'Sport':<10} {'Kalshi Ask':>11} {'Model Prob':>11} {'Edge':>7} {'Kelly $':>8}  Score State")
    print("-" * w)
    for s in signals:
        direction = "BUY" if s.edge > 0 else "SKIP(overpriced)"
        print(
            f"{s.player:<25} {s.sport:<10} {s.kalshi_ask:>10.1%} {s.model_prob:>10.1%} "
            f"{s.edge:>+7.1%} ${s.kelly_usd:>7.2f}  {s.score_state}  [{direction}]"
        )
    print(f"\n{len(signals)} live signal(s)  |  min edge: {MIN_EDGE:.0%}  |  Kelly: {KELLY_FRACTION} fraction")


if __name__ == "__main__":
    main()
