"""
deepdive5.py — Three-Signal Baseball Model Analysis

Run from /opt/kalshi_trader/ after 48h of data collection:
    python3 deepdive5.py

Reads: orders_live.jsonl, settlements.jsonl, signals_live.jsonl, portfolio_snapshots.jsonl
"""

import json
import math
from collections import defaultdict
from pathlib import Path

# ── File paths ────────────────────────────────────────────────────────────────

ORDERS_FILE     = Path("orders_live.jsonl")
SETTLEMENTS_FILE = Path("settlements.jsonl")
SIGNALS_FILE    = Path("signals_live.jsonl")
SNAPSHOTS_FILE  = Path("portfolio_snapshots.jsonl")
PERF_CACHE_FILE = Path("perf_cache_live.json")

W = 90  # output width


def sep(title=""):
    if title:
        pad = (W - len(title) - 2) // 2
        print(f"\n{'='*pad} {title} {'='*(W-pad-len(title)-2)}")
    else:
        print("─" * W)


def pct(v, d=1):
    return f"{v*100:.{d}f}%" if v is not None else "N/A"


def fmt(v, d=2):
    return f"{v:.{d}f}" if v is not None else "N/A"


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── Section 1: Era Overview ───────────────────────────────────────────────────

def section1(orders, settlements, signals):
    sep("Section 1 — Era Overview")
    all_ts = [o.get("ts", "") for o in orders if o.get("ts")]
    date_range = f"{min(all_ts)[:10]} → {max(all_ts)[:10]}" if all_ts else "N/A"

    baseball_orders = [o for o in orders if o.get("sport") == "baseball" and o.get("contracts", 0) > 0
                       and o.get("status") in ("submitted", "resting", "executed")]
    tennis_orders   = [o for o in orders if o.get("sport") == "tennis"   and o.get("contracts", 0) > 0
                       and o.get("status") in ("submitted", "resting", "executed")]

    pnl_settle = sum(s.get("pnl_usd", 0) for s in settlements)
    wins_settle = sum(1 for s in settlements if s.get("won"))
    n_settle    = len(settlements)

    perf_cache = load_json(PERF_CACHE_FILE)
    pnl_cache  = 0.0
    wins_cache = 0
    n_cache    = 0
    for o in baseball_orders + tennis_orders:
        t = o.get("ticker", "")
        r = perf_cache.get(t)
        if r:
            side = o.get("side", "yes")
            won  = (side == "yes" and r == "yes") or (side == "no" and r == "no")
            cost = float(o.get("cost_usd", 0))
            pnl  = (o.get("contracts", 0) - cost) if won else -cost
            pnl_cache += pnl
            wins_cache += int(won)
            n_cache += 1

    print(f"  Date range      : {date_range}")
    print(f"  Total orders    : {len(orders)}  (baseball={len(baseball_orders)}, tennis={len(tennis_orders)})")
    print(f"  Total signals   : {len(signals)}")
    print(f"  Settlements.jsonl: {n_settle} records, P&L = ${pnl_settle:+.2f}, WR = {pct(wins_settle/n_settle if n_settle else None)}")
    print(f"  Perf cache      : {n_cache} resolved, P&L = ${pnl_cache:+.2f}, WR = {pct(wins_cache/n_cache if n_cache else None)}")


# ── Section 2: Three-Signal Model Comparison ─────────────────────────────────

def section2(settlements):
    sep("Section 2 — Three-Signal Model Comparison (baseball only)")
    bb = [s for s in settlements if s.get("sport") == "baseball" and s.get("result") in ("yes", "no")]
    if not bb:
        print("  No settled baseball orders yet.")
        return

    markov_correct = espn_correct = vegas_correct = consensus_correct = 0
    markov_mae = espn_mae = vegas_mae = consensus_mae = 0.0
    n_espn = n_vegas = 0

    print(f"  {'Ticker':<35} {'Ask':>5} {'Markov':>7} {'ESPN':>7} {'Vegas':>7} {'Consens':>8} {'Outcome':>8} {'PnL':>7}")
    sep()
    for s in bb:
        ask    = float(s.get("entry_cents", 0)) / 100
        markov = float(s.get("markov_prob", 0))
        espn   = float(s.get("espn_win_prob", 0))
        vegas  = float(s.get("vegas_live_prob", 0))
        model  = float(s.get("model_prob", 0))
        won    = s.get("won", False)
        pnl    = float(s.get("pnl_usd", 0))
        outcome = 1.0 if won else 0.0

        markov_correct    += int((markov > 0.5) == won)
        consensus_correct += int((model  > 0.5) == won)
        markov_mae    += abs(markov - outcome)
        consensus_mae += abs(model  - outcome)
        if espn:
            espn_correct += int((espn > 0.5) == won)
            espn_mae     += abs(espn - outcome)
            n_espn += 1
        if vegas:
            vegas_correct += int((vegas > 0.5) == won)
            vegas_mae     += abs(vegas - outcome)
            n_vegas += 1

        ticker_short = s.get("ticker", "")[-35:]
        print(f"  {ticker_short:<35} {pct(ask):>5} {pct(markov):>7} "
              f"{pct(espn) if espn else '  N/A':>7} {pct(vegas) if vegas else '  N/A':>7} "
              f"{pct(model):>8} {'WIN' if won else 'LOSS':>8} ${pnl:>6.2f}")

    n = len(bb)
    sep()
    print(f"  Accuracy  — Markov: {markov_correct}/{n} ({pct(markov_correct/n)}), "
          f"ESPN: {espn_correct}/{n_espn} ({pct(espn_correct/n_espn if n_espn else None)}), "
          f"Vegas: {vegas_correct}/{n_vegas} ({pct(vegas_correct/n_vegas if n_vegas else None)}), "
          f"Consensus: {consensus_correct}/{n} ({pct(consensus_correct/n)})")
    print(f"  MAE       — Markov: {fmt(markov_mae/n,3)}, "
          f"ESPN: {fmt(espn_mae/n_espn if n_espn else None,3)}, "
          f"Vegas: {fmt(vegas_mae/n_vegas if n_vegas else None,3)}, "
          f"Consensus: {fmt(consensus_mae/n,3)}")


# ── Section 3: Multi-Signal Agreement ────────────────────────────────────────

def section3(settlements):
    sep("Section 3 — Multi-Signal Agreement Analysis")
    bb = [s for s in settlements if s.get("sport") == "baseball" and s.get("result") in ("yes", "no")]
    if not bb:
        print("  No settled baseball orders yet.")
        return

    groups = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for s in bb:
        ask   = float(s.get("entry_cents", 0)) / 100
        m     = float(s.get("markov_prob", 0))
        e     = float(s.get("espn_win_prob", 0))
        v     = float(s.get("vegas_live_prob", 0))
        won   = s.get("won", False)
        pnl   = float(s.get("pnl_usd", 0))
        agree = sum([m > ask, bool(e) and e > ask, bool(v) and v > ask])
        if agree == 3:
            key = "All 3 agree edge"
        elif agree == 2:
            if not e and not v:
                key = "Only Markov (no ESPN/Vegas)"
            elif m > ask and (e > ask or v > ask):
                key = "2/3 agree (Markov + ESPN or Vegas)"
            else:
                key = "2/3 agree (ESPN+Vegas, Markov no)"
        elif agree == 1 and m > ask:
            key = "Only Markov (ESPN/Vegas disagree)"
        else:
            key = "Only ESPN/Vegas (Markov no edge)"
        groups[key]["n"]    += 1
        groups[key]["wins"] += int(won)
        groups[key]["pnl"]  += pnl

    print(f"  {'Group':<45} {'N':>4} {'Win%':>7} {'Avg PnL':>9}")
    sep()
    for grp, d in sorted(groups.items(), key=lambda x: -x[1]["n"]):
        wr  = d["wins"] / d["n"] if d["n"] else 0
        avg = d["pnl"] / d["n"] if d["n"] else 0
        print(f"  {grp:<45} {d['n']:>4} {pct(wr):>7} ${avg:>8.2f}")


# ── Section 4: Kalshi vs Vegas Price Gap ─────────────────────────────────────

def section4(settlements):
    sep("Section 4 — Kalshi vs Vegas Price Gap")
    bb = [s for s in settlements if s.get("sport") == "baseball" and s.get("result") in ("yes", "no")
          and s.get("vegas_live_prob")]
    if not bb:
        print("  No settled baseball orders with Vegas data yet.")
        return

    buckets = {
        "<-10c": {"n": 0, "wins": 0},
        "-10 to 0c": {"n": 0, "wins": 0},
        "0 to +10c": {"n": 0, "wins": 0},
        ">+10c": {"n": 0, "wins": 0},
    }
    for s in bb:
        ask   = float(s.get("entry_cents", 0)) / 100
        vegas = float(s.get("vegas_live_prob", 0))
        delta = ask - vegas   # positive = Kalshi overpriced vs Vegas
        won   = s.get("won", False)
        if delta < -0.10:
            b = "<-10c"
        elif delta < 0:
            b = "-10 to 0c"
        elif delta <= 0.10:
            b = "0 to +10c"
        else:
            b = ">+10c"
        buckets[b]["n"] += 1
        buckets[b]["wins"] += int(won)

    print(f"  Kalshi_ask − Vegas_live (positive = Kalshi more expensive)")
    print(f"  {'Bucket':<15} {'N':>4} {'Win%':>8}  Interpretation")
    sep()
    interp = {
        "<-10c":     "Vegas much cheaper — Kalshi underpriced (strong edge if we bought)",
        "-10 to 0c": "Kalshi slightly cheaper — small mispricing in our favor",
        "0 to +10c": "Kalshi slightly pricier — marginal overprice vs Vegas",
        ">+10c":     "Kalshi significantly overpriced vs Vegas",
    }
    for bkt in buckets:
        d = buckets[bkt]
        wr = d["wins"] / d["n"] if d["n"] else None
        print(f"  {bkt:<15} {d['n']:>4} {pct(wr):>8}  {interp[bkt]}")


# ── Section 5: Vegas Momentum at Entry ───────────────────────────────────────

def section5(settlements):
    sep("Section 5 — Vegas Momentum at Entry")
    bb = [s for s in settlements if s.get("sport") == "baseball" and s.get("result") in ("yes", "no")
          and s.get("vegas_live_prob") and s.get("vegas_open_prob")]
    if not bb:
        print("  No settled baseball orders with Vegas open+live data yet.")
        return

    with_mom = {"n": 0, "wins": 0, "pnl": 0.0}
    against  = {"n": 0, "wins": 0, "pnl": 0.0}
    for s in bb:
        live = float(s.get("vegas_live_prob", 0))
        open_ = float(s.get("vegas_open_prob", 0))
        drift = live - open_   # positive = market moved toward our side
        won   = s.get("won", False)
        pnl   = float(s.get("pnl_usd", 0))
        bucket = with_mom if drift >= 0 else against
        bucket["n"] += 1
        bucket["wins"] += int(won)
        bucket["pnl"] += pnl

    print(f"  Vegas drift = live_prob − open_prob at entry")
    print(f"  {'Group':<35} {'N':>4} {'Win%':>7} {'Total PnL':>10}")
    sep()
    for label, d in [("With momentum (drift >= 0)", with_mom), ("Against momentum (drift < 0)", against)]:
        wr  = d["wins"] / d["n"] if d["n"] else None
        print(f"  {label:<35} {d['n']:>4} {pct(wr):>7} ${d['pnl']:>9.2f}")


# ── Section 6: Base State Performance ────────────────────────────────────────

def section6(settlements):
    sep("Section 6 — Base State Performance (baseball)")
    bb = [s for s in settlements if s.get("sport") == "baseball" and s.get("result") in ("yes", "no")]
    if not bb:
        print("  No settled baseball orders yet.")
        return

    outs_data = defaultdict(lambda: {"n": 0, "wins": 0})
    base_data = defaultdict(lambda: {"n": 0, "wins": 0})

    def base_label(s):
        f, sc, t = s.get("on_first", False), s.get("on_second", False), s.get("on_third", False)
        if not f and not sc and not t:    return "Empty"
        if sc or t:                        return "RISP (2B/3B)"
        if f and not sc and not t:         return "1B only"
        return "Loaded"

    for s in bb:
        outs = s.get("outs", -1)
        won  = s.get("won", False)
        ok   = str(outs) if outs >= 0 else "N/A"
        outs_data[ok]["n"] += 1
        outs_data[ok]["wins"] += int(won)
        bl = base_label(s)
        base_data[bl]["n"] += 1
        base_data[bl]["wins"] += int(won)

    print("  Win rate by outs:")
    for k in sorted(outs_data):
        d = outs_data[k]
        print(f"    {k} outs: {pct(d['wins']/d['n'] if d['n'] else None):>7}  (n={d['n']})")

    print("\n  Win rate by base state:")
    for k, d in sorted(base_data.items(), key=lambda x: -x[1]["n"]):
        print(f"    {k:<20}: {pct(d['wins']/d['n'] if d['n'] else None):>7}  (n={d['n']})")

    # Heatmap: outs × base_state
    print("\n  Heatmap (outs × base state):")
    states = ["Empty", "1B only", "RISP (2B/3B)", "Loaded"]
    outs_keys = ["0", "1", "2", "N/A"]
    hmap = defaultdict(lambda: {"n": 0, "wins": 0})
    for s in bb:
        outs = str(s.get("outs", -1)) if s.get("outs", -1) >= 0 else "N/A"
        bl   = base_label(s)
        hmap[(outs, bl)]["n"] += 1
        hmap[(outs, bl)]["wins"] += int(s.get("won", False))

    header = f"  {'Outs':<6}" + "".join(f"{st:>16}" for st in states)
    print(header)
    for ok in outs_keys:
        row = f"  {ok:<6}"
        for st in states:
            d = hmap[(ok, st)]
            wr = d["wins"] / d["n"] if d["n"] else None
            cell = f"{pct(wr)} (n={d['n']})" if d["n"] else "    -    "
            row += f"{cell:>16}"
        print(row)


# ── Section 7: Inning × Score Differential Heatmap ───────────────────────────

def section7(settlements):
    sep("Section 7 — Inning × Score Differential Heatmap")
    bb = [s for s in settlements if s.get("sport") == "baseball" and s.get("result") in ("yes", "no")]
    if not bb:
        print("  No settled baseball orders yet.")
        return

    diff_labels = ["<=-3", "-2", "-1", "0", "+1", "+2", ">=+3"]
    def diff_bucket(d):
        if d <= -3: return "<=-3"
        if d == -2: return "-2"
        if d == -1: return "-1"
        if d == 0:  return "0"
        if d == 1:  return "+1"
        if d == 2:  return "+2"
        return ">=+3"

    hmap = defaultdict(lambda: {"n": 0, "wins": 0})
    innings_seen = set()
    for s in bb:
        inn  = min(s.get("inning", 0) or 0, 9)
        diff = s.get("score_diff", 0) or 0
        db   = diff_bucket(diff)
        hmap[(inn, db)]["n"]    += 1
        hmap[(inn, db)]["wins"] += int(s.get("won", False))
        innings_seen.add(inn)

    inn_list = sorted(innings_seen) or list(range(1, 10))
    header = f"  {'Inn':<5}" + "".join(f"{dl:>12}" for dl in diff_labels)
    print(header)
    for inn in inn_list:
        row = f"  {inn:<5}"
        for dl in diff_labels:
            d = hmap[(inn, dl)]
            if d["n"]:
                wr = d["wins"] / d["n"]
                flag = " *" if wr > 0.6 else (" !" if wr < 0.4 else "  ")
                row += f"{pct(wr)}({d['n']}){flag}".rjust(12)
            else:
                row += "           -".rjust(12)
        print(row)
    print("  * = win rate >60%   ! = win rate <40%")


# ── Section 8: Model Calibration ─────────────────────────────────────────────

def section8(settlements):
    sep("Section 8 — Model Calibration")
    bb = [s for s in settlements if s.get("result") in ("yes", "no")]
    if not bb:
        print("  No settled orders yet.")
        return

    labels = ["45-50", "50-55", "55-60", "60-65", "65-70", "70-75", "75-80", "80+"]
    cal = defaultdict(lambda: {"n": 0, "wins": 0, "sum_p": 0.0})

    def bucket(p):
        p *= 100
        if p < 45:  return None
        if p < 50:  return "45-50"
        if p < 55:  return "50-55"
        if p < 60:  return "55-60"
        if p < 65:  return "60-65"
        if p < 70:  return "65-70"
        if p < 75:  return "70-75"
        if p < 80:  return "75-80"
        return "80+"

    for s in bb:
        p  = float(s.get("model_prob", 0))
        b  = bucket(p)
        if b is None:
            continue
        cal[b]["n"]     += 1
        cal[b]["wins"]  += int(s.get("won", False))
        cal[b]["sum_p"] += p

    print(f"  {'Bucket':<10} {'N':>4} {'Avg Model':>10} {'Actual WR':>10} {'Delta':>8}")
    sep()
    for lb in labels:
        d = cal[lb]
        if not d["n"]:
            continue
        avg_p = d["sum_p"] / d["n"]
        wr    = d["wins"]  / d["n"]
        delta = wr - avg_p
        bar_e = "=" * round(avg_p * 20)
        bar_a = "#" * round(wr    * 20)
        print(f"  {lb+'%':<10} {d['n']:>4} {pct(avg_p):>10} {pct(wr):>10} {delta:>+8.1%}")
        print(f"    expected [{bar_e:<20}]")
        print(f"    actual   [{bar_a:<20}]")


# ── Section 9: Fee Drag Analysis ─────────────────────────────────────────────

def section9(orders):
    sep("Section 9 — Fee Drag Analysis")
    real = [o for o in orders if o.get("contracts", 0) > 0
            and o.get("status") in ("submitted", "resting", "executed")]
    total_cost = sum(float(o.get("cost_usd", 0)) for o in real)
    total_pnl  = 0.0
    try:
        from kalshi_client import KalshiClient
        import config
        client = KalshiClient(api_key_id=config.KALSHI_API_KEY)
        positions = client.get_positions().get("market_positions", [])
        total_fees = sum(float(p.get("fees_paid_dollars", 0)) for p in positions)
        total_realized = sum(float(p.get("realized_pnl_dollars", 0)) for p in positions)
        print(f"  Total fees paid  : ${total_fees:.2f}")
        print(f"  Total invested   : ${total_cost:.2f}")
        print(f"  Fee as % of invested: {pct(total_fees/total_cost if total_cost else None)}")
        print(f"  Realized P&L (API): ${total_realized:.2f}")
    except Exception as e:
        print(f"  Could not fetch live positions: {e}")
        print(f"  Total invested (from orders): ${total_cost:.2f}")


# ── Section 10: Equity Curve Recap ───────────────────────────────────────────

def section10():
    sep("Section 10 — Equity Curve Recap")
    snaps = load_jsonl(SNAPSHOTS_FILE)
    if not snaps:
        print("  No portfolio snapshots found.")
        return

    totals = [float(s.get("total_usd", 0)) for s in snaps]
    ts_list = [s.get("ts", "")[:16] for s in snaps]
    start = totals[0]
    end   = totals[-1]
    hi    = max(totals)
    lo    = min(totals)

    # Biggest single-day change (approximate using 30-min scan cycle ≈ 48 pts/day)
    day_changes = []
    pts_per_day = 48
    for i in range(pts_per_day, len(totals)):
        day_changes.append(totals[i] - totals[i - pts_per_day])
    best_day  = max(day_changes) if day_changes else 0
    worst_day = min(day_changes) if day_changes else 0

    print(f"  Snapshots        : {len(snaps)}")
    print(f"  Time range       : {ts_list[0]} → {ts_list[-1]}")
    print(f"  Start            : ${start:.2f}")
    print(f"  End              : ${end:.2f}  ({end-start:+.2f})")
    print(f"  High             : ${hi:.2f}")
    print(f"  Low              : ${lo:.2f}")
    print(f"  Best ~day        : ${best_day:+.2f}")
    print(f"  Worst ~day       : ${worst_day:+.2f}")

    # Mini equity curve (40-col ASCII)
    width = 60
    mn, mx = min(totals), max(totals)
    rng = mx - mn or 1
    step = max(1, len(totals) // width)
    sampled = totals[::step][:width]
    bar = "".join("▂▃▄▅▆▇█"[min(6, int((v - mn) / rng * 7))] for v in sampled)
    print(f"\n  Equity: [{bar}]")
    print(f"          ${mn:.0f}" + " " * (len(bar) - len(f"${mn:.0f}") - len(f"${mx:.0f}")) + f"${mx:.0f}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * W)
    print(f"{'deepdive5.py — Three-Signal Baseball Model Analysis':^{W}}")
    print("=" * W)

    orders      = load_jsonl(ORDERS_FILE)
    settlements = load_jsonl(SETTLEMENTS_FILE)
    signals     = load_jsonl(SIGNALS_FILE)

    section1(orders, settlements, signals)
    section2(settlements)
    section3(settlements)
    section4(settlements)
    section5(settlements)
    section6(settlements)
    section7(settlements)
    section8(settlements)
    section9(orders)
    section10()

    sep()
    print("  Analysis complete.")


if __name__ == "__main__":
    main()
