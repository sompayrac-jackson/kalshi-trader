"""
Per-player serve win percentages built from Jeff Sackmann's tennis data.
Combines 2026 + 2025 match data for both ATP and WTA.
Results are cached in memory after first fetch.
"""

import io
import csv
import requests
from difflib import get_close_matches

ATP_URLS = [
    "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_2026.csv",
    "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_2025.csv",
]
WTA_URLS = [
    "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_matches_2026.csv",
    "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_matches_2025.csv",
]

ATP_AVG = 0.64
WTA_AVG = 0.58
MIN_SERVE_POINTS = 50   # require at least this many serve points for a reliable estimate

_cache: dict[str, dict[str, float]] = {}


def _parse(csv_text: str) -> dict[str, list[int]]:
    """Return {player_name: [serve_points_won, serve_points_played]}."""
    stats: dict[str, list[int]] = {}
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        for prefix, name_key in (("w_", "winner_name"), ("l_", "loser_name")):
            name = (row.get(name_key) or "").strip()
            if not name:
                continue
            try:
                svpt = int(row.get(f"{prefix}svpt") or 0)
                won  = int(row.get(f"{prefix}1stWon") or 0) + int(row.get(f"{prefix}2ndWon") or 0)
            except (ValueError, TypeError):
                continue
            if svpt == 0:
                continue
            if name not in stats:
                stats[name] = [0, 0]
            stats[name][0] += won
            stats[name][1] += svpt
    return stats


def _load(tour: str) -> dict[str, float]:
    urls = ATP_URLS if tour == "atp" else WTA_URLS
    combined: dict[str, list[int]] = {}
    for url in urls:
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            partial = _parse(resp.text)
            for name, (won, played) in partial.items():
                if name not in combined:
                    combined[name] = [0, 0]
                combined[name][0] += won
                combined[name][1] += played
        except Exception as e:
            print(f"  [serve_stats] could not fetch {url}: {e}")
    return {
        name: won / played
        for name, (won, played) in combined.items()
        if played >= MIN_SERVE_POINTS
    }


def get_stats(tour: str = "atp") -> dict[str, float]:
    """Return {player_name: serve_win_pct}. Cached after first call."""
    if tour not in _cache:
        print(f"  [serve_stats] loading {tour.upper()} serve data...")
        _cache[tour] = _load(tour)
        print(f"  [serve_stats] {len(_cache[tour])} {tour.upper()} players loaded")
    return _cache[tour]


def lookup(name: str, tour: str = "atp") -> float:
    """
    Return serve win % for a player by name, with fuzzy fallback.
    Falls back to tour average if no match found.
    """
    fallback = ATP_AVG if tour == "atp" else WTA_AVG
    stats = get_stats(tour)
    if not name:
        return fallback

    # 1. Exact match
    if name in stats:
        return stats[name]

    # 2. Last-name match (handles "Stan Wawrinka" vs "S. Wawrinka")
    last = name.lower().split()[-1]
    for player, pct in stats.items():
        if player.lower().split()[-1] == last:
            return pct

    # 3. Fuzzy match
    close = get_close_matches(name, stats.keys(), n=1, cutoff=0.72)
    if close:
        return stats[close[0]]

    return fallback


if __name__ == "__main__":
    for tour in ("atp", "wta"):
        stats = get_stats(tour)
        avg = sum(stats.values()) / len(stats)
        top5 = sorted(stats.items(), key=lambda x: x[1], reverse=True)[:5]
        print(f"\n{tour.upper()} — {len(stats)} players, avg serve win: {avg:.1%}")
        for name, pct in top5:
            print(f"  {name}: {pct:.1%}")
    print(f"\nWawrinka: {lookup('Stan Wawrinka', 'atp'):.1%}")
    print(f"Swiatek: {lookup('Iga Swiatek', 'wta'):.1%}")
