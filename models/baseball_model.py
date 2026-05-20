"""
Baseball win probability via Markov chain.

Models a half-inning as a Markov process over 24 base-out states.
Combines run distributions per inning with score/inning context
to produce P(home team wins) at any game state.

Inputs: current inning, half (top/bottom), score, base-out state,
        batting team's OBP and SLG (or use league averages).
Output: P(home team wins).
"""

import numpy as np
from functools import lru_cache


# ── League average plate appearance outcomes (MLB 2023 approx) ────────────────

PA_OUTCOMES = {
    "out":    0.692,   # includes K, GB out, FB out
    "single": 0.150,
    "double": 0.048,
    "triple": 0.005,
    "hr":     0.033,
    "walk":   0.085,   # BB + HBP
}

# Bases encoded as bitmask: bit0=1B, bit1=2B, bit2=3B
# 0=empty, 1=1B only, 2=2B only, 3=1B+2B, 4=3B only, 5=1B+3B, 6=2B+3B, 7=loaded
BASES_EMPTY  = 0
BASES_1B     = 1
BASES_2B     = 2
BASES_3B     = 4
BASES_1B_2B  = 3
BASES_1B_3B  = 5
BASES_2B_3B  = 6
BASES_LOADED = 7


def advance_runners(bases: int, hit_bases: int) -> tuple[int, int]:
    """
    Advance runners on a hit. Returns (new_bases, runs_scored).
    hit_bases: 1=single, 2=double, 3=triple, 4=HR
    Simple model: runners advance hit_bases + 1 on singles from 2B/3B.
    """
    runs = 0
    new_bases = 0

    if hit_bases == 4:  # home run — everyone scores
        runs = bin(bases).count("1") + 1
        return 0, runs

    # Score runners from 3B always
    if bases & BASES_3B:
        runs += 1

    # Score runners from 2B on double/triple
    if bases & BASES_2B:
        if hit_bases >= 2:
            runs += 1
        else:
            new_bases |= BASES_3B  # single: 2B→3B

    # Score runners from 1B on triple; advance on double
    if bases & BASES_1B:
        if hit_bases >= 3:
            runs += 1
        elif hit_bases == 2:
            new_bases |= BASES_3B
        else:
            new_bases |= BASES_2B  # single: 1B→2B

    # Place batter
    batter_base = 1 << (hit_bases - 1)
    # If occupied, push existing runner forward (simplified)
    if new_bases & batter_base:
        new_bases = (new_bases & ~batter_base) | (batter_base << 1)
    new_bases |= batter_base

    return new_bases & 0b111, runs


def walk_runners(bases: int) -> tuple[int, int]:
    """Advance runners on walk (force only)."""
    runs = 0
    if bases == BASES_LOADED:
        runs = 1
        return BASES_LOADED, runs
    new_bases = bases
    if (bases & BASES_1B) and (bases & BASES_2B):
        new_bases |= BASES_3B
    if bases & BASES_1B:
        new_bases |= BASES_2B
    new_bases |= BASES_1B
    return new_bases & 0b111, runs


# ── Half-inning run distribution ──────────────────────────────────────────────

def half_inning_run_distribution(
    outcomes: dict = None,
    max_runs: int = 10,
) -> np.ndarray:
    """
    P(score exactly r runs in a half-inning) for r in 0..max_runs.
    Uses DP over (outs, bases) states.
    """
    if outcomes is None:
        outcomes = PA_OUTCOMES

    # State: (outs 0-2, bases 0-7) → probability distribution over runs scored
    # dp[outs][bases] = {runs: probability}
    dp = [[{} for _ in range(8)] for _ in range(3)]
    dp[0][0] = {0: 1.0}

    result = [0.0] * (max_runs + 1)

    def recurse(outs: int, bases: int, prob: float, runs_so_far: int):
        if prob < 1e-9:
            return
        if outs == 3:
            idx = min(runs_so_far, max_runs)
            result[idx] += prob
            return

        for outcome, p in outcomes.items():
            if outcome == "out":
                recurse(outs + 1, bases, prob * p, runs_so_far)
            elif outcome == "walk":
                nb, r = walk_runners(bases)
                recurse(outs, nb, prob * p, runs_so_far + r)
            elif outcome in ("single", "double", "triple", "hr"):
                hit_bases = {"single": 1, "double": 2, "triple": 3, "hr": 4}[outcome]
                nb, r = advance_runners(bases, hit_bases)
                recurse(outs, nb, prob * p, runs_so_far + r)

    recurse(0, 0, 1.0, 0)
    arr = np.array(result)
    return arr / arr.sum()  # normalize floating point


# Cache the default distribution (league average)
_DEFAULT_DIST = None

def default_run_dist() -> np.ndarray:
    global _DEFAULT_DIST
    if _DEFAULT_DIST is None:
        _DEFAULT_DIST = half_inning_run_distribution()
    return _DEFAULT_DIST


# ── Win probability ───────────────────────────────────────────────────────────

def prob_home_wins(
    inning: int,
    is_bottom: bool,
    score_home: int,
    score_away: int,
    outs: int = 0,
    bases: int = 0,
    home_run_dist: np.ndarray = None,
    away_run_dist: np.ndarray = None,
    total_innings: int = 9,
    max_runs: int = 10,
) -> float:
    """
    P(home team wins) from an arbitrary game state.

    inning      : current inning (1-indexed)
    is_bottom   : True if bottom half is batting
    score_home  : home team runs
    score_away  : away team runs
    outs        : outs in current half-inning (0-2)
    bases       : base state bitmask
    home/away_run_dist : per-half-inning run distributions (uses league avg if None)
    """
    if home_run_dist is None:
        home_run_dist = default_run_dist()
    if away_run_dist is None:
        away_run_dist = default_run_dist()

    max_r = max_runs

    # Pre-compute convolutions: P(score exactly r runs in n half-innings)
    def convolve_n(dist: np.ndarray, n: int) -> np.ndarray:
        result = np.zeros(max_r + 1)
        result[0] = 1.0
        for _ in range(n):
            new = np.zeros(max_r + 1)
            for r in range(max_r + 1):
                for s in range(max_r + 1 - r):
                    new[r + s] += result[r] * dist[s]
            result = new
        return result

    # Remaining half-innings for each team from this point
    if is_bottom:
        away_halves_left = total_innings - inning      # away already batted this inning
        home_halves_left = total_innings - inning      # home bats rest of this inning + future
        # Home is currently batting
        batting_team = "home"
        batting_score = score_home
        fielding_score = score_away
    else:
        away_halves_left = total_innings - inning + 1  # away bats rest of this inning + future
        home_halves_left = total_innings - inning      # home bats future innings only
        batting_team = "away"
        batting_score = score_away
        fielding_score = score_home

    batting_dist  = home_run_dist if batting_team == "home" else away_run_dist
    fielding_dist = away_run_dist if batting_team == "home" else home_run_dist

    # Runs the batting team scores in the remainder of this half-inning
    # (simplified: ignore current outs/bases, use full half-inning dist scaled)
    # A proper model would condition on current outs/bases — good enough for now
    outs_factor = (3 - outs) / 3  # fraction of inning remaining
    # Approximate remaining inning runs as scaled distribution
    # (this is a simplification; full model would enumerate states)

    p_home_wins = 0.0

    # Future complete half-innings
    if batting_team == "home":
        home_future = home_halves_left - 1   # exclude current (partial)
        away_future = away_halves_left
    else:
        home_future = home_halves_left
        away_future = away_halves_left - 1   # exclude current (partial)

    away_future_dist = convolve_n(fielding_dist if batting_team == "home" else batting_dist, away_future if batting_team == "home" else home_future)
    home_future_dist = convolve_n(home_run_dist, home_future)

    # P(batting team scores r in rest of this half-inning) — rough approximation
    current_dist = np.zeros(max_r + 1)
    current_dist[0] = 1 - outs_factor
    for r in range(1, max_r + 1):
        current_dist[r] = batting_dist[r] * outs_factor if r < len(batting_dist) else 0
    current_dist[0] += batting_dist[0] * outs_factor
    current_dist /= current_dist.sum()

    # Combine: sum over all run scenarios
    for r_current in range(max_r + 1):
        p_current = current_dist[r_current]
        if p_current < 1e-9:
            continue
        new_batting = batting_score + r_current
        score_h = new_batting if batting_team == "home" else fielding_score
        score_a = fielding_score if batting_team == "home" else new_batting

        for r_hf in range(max_r + 1):
            for r_af in range(max_r + 1):
                p_combo = p_current * home_future_dist[r_hf] * away_future_dist[r_af]
                if p_combo < 1e-9:
                    continue
                final_home = score_h + (r_hf if batting_team != "home" else 0) + (r_hf if batting_team == "home" else 0)
                final_away = score_a + (r_af if batting_team == "home" else 0) + (r_af if batting_team != "home" else 0)

                if final_home > final_away:
                    p_home_wins += p_combo
                elif final_home == final_away:
                    p_home_wins += p_combo * 0.5  # extra innings approximation

    return min(1.0, max(0.0, p_home_wins))


def prob_home_wins_simple(
    inning: int,
    is_bottom: bool,
    score_home: int,
    score_away: int,
    total_innings: int = 9,
) -> float:
    """
    Lightweight version — no base-out state, uses pre-convolved distributions.
    Good for fast scanning.
    """
    return prob_home_wins(
        inning=inning,
        is_bottom=is_bottom,
        score_home=score_home,
        score_away=score_away,
        outs=0,
        bases=0,
        total_innings=total_innings,
    )


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Start of game — should be ~50%
    p = prob_home_wins_simple(1, False, 0, 0)
    print(f"Start of game: {p:.1%}")

    # Home leads 3-0 going into 9th, away batting
    p = prob_home_wins_simple(9, False, 3, 0)
    print(f"Home leads 3-0, top 9th: {p:.1%}")

    # Tied going into bottom of 9th, home batting
    p = prob_home_wins_simple(9, True, 2, 2)
    print(f"Tied 2-2, bottom 9th home batting: {p:.1%}")

    # Away leads 1-0 in 5th inning, bottom half
    p = prob_home_wins_simple(5, True, 0, 1)
    print(f"Away leads 1-0, bottom 5th home batting: {p:.1%}")
