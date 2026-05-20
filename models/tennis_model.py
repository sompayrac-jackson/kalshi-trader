"""
Tennis win probability via Markov chain.

Inputs: p1_serve_win_prob, p2_serve_win_prob, current score state.
Output: probability player 1 wins the match.

Scoring hierarchy: point -> game -> set -> match.
Each level is solved analytically via recursive Markov chain.
"""

from functools import lru_cache


# ── Point / Game ──────────────────────────────────────────────────────────────

def prob_win_game(p: float) -> float:
    """P(server wins game) given p = P(server wins any point)."""
    q = 1 - p

    @lru_cache(maxsize=None)
    def pw(i: int, j: int) -> float:
        # i = server points, j = receiver points
        if i >= 4 and i - j >= 2:
            return 1.0
        if j >= 4 and j - i >= 2:
            return 0.0
        if i >= 3 and j >= 3:
            # at deuce: closed-form
            return (p ** 2) / (p ** 2 + q ** 2)
        return p * pw(i + 1, j) + q * pw(i, j + 1)

    return pw(0, 0)


def prob_win_game_from_score(p: float, pts_server: int, pts_receiver: int) -> float:
    """P(server wins game) from a mid-game score (0-3 scale, 3=40)."""
    q = 1 - p

    @lru_cache(maxsize=None)
    def pw(i: int, j: int) -> float:
        if i >= 4 and i - j >= 2:
            return 1.0
        if j >= 4 and j - i >= 2:
            return 0.0
        if i >= 3 and j >= 3:
            return (p ** 2) / (p ** 2 + q ** 2)
        return p * pw(i + 1, j) + q * pw(i, j + 1)

    return pw(pts_server, pts_receiver)


# ── Tiebreak ──────────────────────────────────────────────────────────────────

def prob_win_tiebreak(p1: float, p2: float) -> float:
    """
    P(player 1 wins tiebreak).
    Serve alternates every 2 points (1 first, then 2-2-2...).
    Approximated with weighted average serve probability.
    """
    p_avg = (p1 + (1 - p2)) / 2  # p1 wins a point on average
    q_avg = 1 - p_avg

    @lru_cache(maxsize=None)
    def pw(i: int, j: int) -> float:
        if i >= 7 and i - j >= 2:
            return 1.0
        if j >= 7 and j - i >= 2:
            return 0.0
        if i >= 6 and j >= 6:
            return (p_avg ** 2) / (p_avg ** 2 + q_avg ** 2)
        return p_avg * pw(i + 1, j) + q_avg * pw(i, j + 1)

    return pw(0, 0)


# ── Set ───────────────────────────────────────────────────────────────────────

def prob_win_set(
    p1: float,
    p2: float,
    p1_serving_first: bool = True,
    final_set_no_tiebreak: bool = False,
) -> float:
    """P(player 1 wins set) from 0-0."""
    return prob_win_set_from_score(p1, p2, 0, 0, p1_serving_first, final_set_no_tiebreak)


def prob_win_set_from_score(
    p1: float,
    p2: float,
    games_p1: int,
    games_p2: int,
    p1_serving: bool = True,
    final_set_no_tiebreak: bool = False,
) -> float:
    """
    P(player 1 wins set) from a mid-set score.
    Uses iterative bottom-up DP to avoid recursion depth issues.
    """
    pg_p1 = prob_win_game(p1)        # P(p1 wins game when serving)
    pg_p2 = 1 - prob_win_game(p2)    # P(p1 wins game when p2 serving)
    p_tb  = prob_win_tiebreak(p1, p2)

    # For advantage sets we go up to MAX each side; use deuce formula at boundary
    MAX = 9 if not final_set_no_tiebreak else 22

    # dp[(i, j, s)] = P(p1 wins set) — s=True means p1 is serving
    dp: dict[tuple[int, int, bool], float] = {}

    # Fill bottom-up: process states in decreasing order of i+j
    for total in range(MAX * 2, -1, -1):
        for i in range(min(total + 1, MAX + 1)):
            j = total - i
            if j < 0 or j > MAX:
                continue
            for s in (True, False):
                # ── Terminal conditions ──────────────────────────────────────
                if i >= 6 and i - j >= 2:
                    dp[(i, j, s)] = 1.0
                    continue
                if j >= 6 and j - i >= 2:
                    dp[(i, j, s)] = 0.0
                    continue
                if i == 6 and j == 6 and not final_set_no_tiebreak:
                    dp[(i, j, s)] = p_tb
                    continue

                # ── Look up next states ──────────────────────────────────────
                p_win = pg_p1 if s else pg_p2
                nxt   = not s
                win_val  = dp.get((i + 1, j, nxt))
                lose_val = dp.get((i, j + 1, nxt))

                if win_val is None or lose_val is None:
                    # At MAX boundary for advantage sets — use deuce approximation
                    p_avg = (pg_p1 + pg_p2) / 2
                    dp[(i, j, s)] = (p_avg ** 2) / (p_avg ** 2 + (1 - p_avg) ** 2)
                    continue

                dp[(i, j, s)] = p_win * win_val + (1 - p_win) * lose_val

    return dp.get((games_p1, games_p2, p1_serving), 0.5)


# ── Match ─────────────────────────────────────────────────────────────────────

def prob_win_match(
    p1_serve: float,
    p2_serve: float,
    best_of: int = 3,
    p1_serving_first: bool = True,
    final_set_no_tiebreak: bool = False,
) -> float:
    """P(player 1 wins match) from the start."""
    return prob_win_match_from_score(
        p1_serve, p2_serve, 0, 0,
        best_of=best_of,
        p1_serving=p1_serving_first,
        final_set_no_tiebreak=final_set_no_tiebreak,
    )


def prob_win_match_from_score(
    p1_serve: float,
    p2_serve: float,
    sets_p1: int,
    sets_p2: int,
    games_p1: int = 0,
    games_p2: int = 0,
    p1_serving: bool = True,
    best_of: int = 3,
    final_set_no_tiebreak: bool = False,
) -> float:
    """
    P(player 1 wins match) from an arbitrary match state.

    p1_serve : P(p1 wins a point on their serve)
    p2_serve : P(p2 wins a point on their serve)
    sets_p1/p2 : sets won so far
    games_p1/p2 : games in the current set
    p1_serving  : True if p1 is currently serving
    best_of     : 3 or 5
    """
    sets_to_win = (best_of + 1) // 2

    # P(p1 wins current set from this game score)
    p_win_set_now = prob_win_set_from_score(
        p1_serve, p2_serve, games_p1, games_p2, p1_serving,
        final_set_no_tiebreak=(
            final_set_no_tiebreak and
            sets_p1 + sets_p2 == best_of - 1
        ),
    )
    p_lose_set_now = 1 - p_win_set_now

    # After current set, who serves first next set?
    # Approximate: alternate who served first each set
    next_serving = not p1_serving  # rough approximation

    @lru_cache(maxsize=None)
    def pw(s1: int, s2: int, p1s: bool) -> float:
        if s1 >= sets_to_win:
            return 1.0
        if s2 >= sets_to_win:
            return 0.0
        is_final = s1 + s2 == best_of - 1
        p_ws = prob_win_set(
            p1_serve, p2_serve, p1s,
            final_set_no_tiebreak=(final_set_no_tiebreak and is_final),
        )
        return p_ws * pw(s1 + 1, s2, not p1s) + (1 - p_ws) * pw(s1, s2 + 1, not p1s)

    return (
        p_win_set_now  * pw(sets_p1 + 1, sets_p2, next_serving) +
        p_lose_set_now * pw(sets_p1, sets_p2 + 1, next_serving)
    )


# ── Serve stat helpers ────────────────────────────────────────────────────────

# Average ATP/WTA serve win rates when no player data is available
ATP_AVG_SERVE_WIN = 0.64
WTA_AVG_SERVE_WIN = 0.58


def serve_win_prob_from_elo(elo_p1: float, elo_p2: float, tour: str = "atp") -> tuple[float, float]:
    """
    Estimate serve win probabilities from Elo ratings.
    Returns (p1_serve_win, p2_serve_win).
    """
    base = ATP_AVG_SERVE_WIN if tour == "atp" else WTA_AVG_SERVE_WIN
    elo_diff = elo_p1 - elo_p2
    # Each 100 Elo points ~ 4% shift in overall match win probability.
    # Translate to approximate serve adjustment.
    adjustment = elo_diff * 0.00015
    p1 = min(0.95, max(0.40, base + adjustment))
    p2 = min(0.95, max(0.40, base - adjustment))
    return p1, p2


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Equal players, best of 3
    p = prob_win_match(0.64, 0.64, best_of=3)
    print(f"Equal ATP players: {p:.1%}")  # should be ~50%

    # p1 slightly stronger
    p = prob_win_match(0.67, 0.61, best_of=3)
    print(f"Stronger p1: {p:.1%}")

    # Mid-match: p1 leads 1 set to 0, up 4-2 in set 2
    p = prob_win_match_from_score(0.64, 0.64, sets_p1=1, sets_p2=0, games_p1=4, games_p2=2)
    print(f"P1 leads 1-0 sets, 4-2 games: {p:.1%}")
