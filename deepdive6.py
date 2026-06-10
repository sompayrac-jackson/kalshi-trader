"""
deepdive6.py — Post-Strategy-Adjustment Analysis

Run from /opt/kalshi_trader/ after accumulating data post-June-7 filter deployment:
    python3 deepdive6.py

Reads: signals_live.jsonl, orders_live.jsonl, settlements.jsonl,
       exits_live.jsonl, portfolio_snapshots.jsonl

Answers:
  1. Era & data health — are settlements clean after dedup fix?
  2. Filter effectiveness — did the 3 new baseball filters block the right trades?
  3. Post-filter entry quality — is the new signal set better calibrated?
  4. Tennis deep dive — first analysis of set/score patterns
  5. Exit quality — SL vs PT performance, slippage
  6. Entry spread vs outcome — are wide spreads hurting us?
  7. Score differential revisited — what situations are still being entered?
  8. Settlements integrity — dedup verification, gap ticker report
"""

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ── File paths ────────────────────────────────────────────────────────────────

SIGNALS_FILE    = Path("signals_live.jsonl")
ORDERS_FILE     = Path("orders_live.jsonl")
SETTLEMENTS_FILE = Path("settlements.jsonl")
EXITS_FILE      = Path("exits_live.jsonl")
SNAPSHOTS_FILE  = Path("portfolio_snapshots.jsonl")

FILTER_DEPLOY_DATE = "2026-06-07"   # date filters went live

W = 92


def sep(title=""):
    if title:
        pad = (W - len(title) - 2) // 2
        print(f"\n{'='*pad} {title} {'='*(W - pad - len(title) - 2)}")
    else:
        print("─" * W)


def pct(v, d=1):
    return f"{v*100:.{d}f}%" if v is not None else " N/A"


def fmt(v, d=2):
    return f"{v:.{d}f}" if v is not None else "N/A"


def wrate(wins, n):
    return wins / n if n else None


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


def is_real_order(o: dict) -> bool:
    return (o.get("contracts", 0) > 0
            and o.get("status") in ("submitted", "resting", "executed")
            and not o.get("dry_run", False))


def post_filter(ts: str) -> bool:
    return ts[:10] >= FILTER_DEPLOY_DATE


# ── Section 1: Era & Data Health ──────────────────────────────────────────────

def section1(signals, orders, settlements, snapshots):
    sep("Section 1 — Era & Data Health")

    real_orders = [o for o in orders if is_real_order(o)]
    bb_orders   = [o for o in real_orders if o.get("sport") == "baseball"]
    tn_orders   = [o for o in real_orders if o.get("sport") == "tennis"]

    all_ts = [o["ts"] for o in real_orders if o.get("ts")]
    sig_ts = [s["ts"] for s in signals if s.get("ts")]

    date_range  = f"{min(all_ts)[:10]} → {max(all_ts)[:10]}" if all_ts else "N/A"
    sig_range   = f"{min(sig_ts)[:10]} → {max(sig_ts)[:10]}" if sig_ts else "N/A"

    post_orders = [o for o in real_orders if post_filter(o.get("ts", ""))]

    pnl_settle = sum(s.get("pnl_usd", 0) for s in settlements)
    wins_settle = sum(1 for s in settlements if s.get("won"))
    n_settle    = len(settlements)

    snap_start = snapshots[0].get("total_usd", 0)  if snapshots else 0
    snap_end   = snapshots[-1].get("total_usd", 0) if snapshots else 0

    print(f"  Orders date range : {date_range}")
    print(f"  Signals date range: {sig_range}")
    print(f"  Real orders       : {len(real_orders)}  (baseball={len(bb_orders)}, tennis={len(tn_orders)})")
    print(f"  Post-{FILTER_DEPLOY_DATE} orders : {len(post_orders)}")
    print(f"  Total signals     : {len(signals)}")
    print(f"  settlements.jsonl : {n_settle} rows | "
          f"P&L = ${pnl_settle:+.2f} | WR = {pct(wrate(wins_settle, n_settle))}")
    print(f"  Equity (snapshots): ${snap_start:.2f} → ${snap_end:.2f}  "
          f"(Δ ${snap_end - snap_start:+.2f})")
    gap = (snap_end - snap_start) - pnl_settle
    print(f"  Equity vs settlements gap: ${gap:+.2f}  "
          f"({'~match' if abs(gap) < 10 else 'check exits/fees'})")


# ── Section 2: Filter Effectiveness ──────────────────────────────────────────

FILTER_REASONS = {
    "no ESPN/Vegas": "baseball: no ESPN/Vegas data — markov-only signal skipped",
    "2 outs":        "baseball: 2 outs — historically 27% WR",
    "late +1 lead":  None,   # partial match — starts with "baseball: inning"
}

def _classify_filter(exec_error: str) -> str | None:
    if not exec_error:
        return None
    if "no ESPN/Vegas" in exec_error:
        return "no ESPN/Vegas"
    if "2 outs" in exec_error:
        return "2 outs"
    if "inning" in exec_error and "+1 lead" in exec_error:
        return "late +1 lead"
    return None


def section2(signals, settlements):
    sep("Section 2 — Filter Effectiveness (post-deployment counterfactual)")

    deploy_ts = FILTER_DEPLOY_DATE + "T00:00:00"

    # Baseball BUY signals since filter deployment
    bb_buy = [
        s for s in signals
        if s.get("sport") == "baseball"
        and s.get("direction") == "BUY"
        and s.get("ts", "") >= deploy_ts
    ]
    if not bb_buy:
        print(f"  No baseball BUY signals found after {FILTER_DEPLOY_DATE}.")
        return

    # Deduplicate to first occurrence per ticker
    first_by_ticker: dict[str, dict] = {}
    for s in sorted(bb_buy, key=lambda x: x.get("ts", "")):
        t = s.get("ticker", "")
        if t and t not in first_by_ticker:
            first_by_ticker[t] = s

    # Build settlement lookup
    settle_by_ticker = {s["ticker"]: s for s in settlements if s.get("ticker")}

    # Count: executed vs filtered
    executed_n  = 0
    blocked: dict[str, list[dict]] = defaultdict(list)   # filter_name -> list of signal dicts

    for ticker, sig in first_by_ticker.items():
        es = sig.get("exec_status", "")
        ee = sig.get("exec_error", "")
        fname = _classify_filter(ee)
        if es == "skipped" and fname:
            blocked[fname].append(sig)
        elif es in ("submitted", "resting", "executed", "dry_run"):
            executed_n += 1
        # Other skips (liquidity, balance, SL cooldown, etc.) are not our new filters

    total_candidates = executed_n + sum(len(v) for v in blocked.values())
    print(f"  Baseball BUY signals since {FILTER_DEPLOY_DATE}: {len(first_by_ticker)} unique tickers")
    print(f"  Executed: {executed_n}  |  Blocked by strategy filters: "
          f"{sum(len(v) for v in blocked.values())}")
    sep()

    col = f"  {'Filter':<30} {'Blocked':>8} {'Settled':>8} {'Wld Win':>8} {'Wld Lose':>9} {'Precision':>10}"
    print(col)
    sep()

    total_blocked = total_would_win = total_would_lose = 0
    for fname in ("no ESPN/Vegas", "2 outs", "late +1 lead"):
        sigs = blocked.get(fname, [])
        n_blocked = len(sigs)
        settled_sigs   = [sg for sg in sigs if sg.get("ticker") in settle_by_ticker]
        would_win  = sum(1 for sg in settled_sigs
                         if settle_by_ticker[sg["ticker"]].get("won", False))
        would_lose = len(settled_sigs) - would_win
        precision  = would_lose / len(settled_sigs) if settled_sigs else None
        total_blocked    += n_blocked
        total_would_win  += would_win
        total_would_lose += would_lose
        print(f"  {fname:<30} {n_blocked:>8} {len(settled_sigs):>8} "
              f"{would_win:>8} {would_lose:>9} {pct(precision):>10}")

    sep()
    all_settled = total_would_win + total_would_lose
    total_prec  = total_would_lose / all_settled if all_settled else None
    print(f"  {'TOTAL':<30} {total_blocked:>8} {all_settled:>8} "
          f"{total_would_win:>8} {total_would_lose:>9} {pct(total_prec):>10}")
    print()
    if total_prec is not None:
        print(f"  Interpretation: filters correctly blocked {pct(total_prec)} of "
              f"blockable settled trades ({total_would_lose} losers prevented).")
    print(f"  Note: 'Settled' < 'Blocked' because many filtered tickers haven't settled yet.")


# ── Section 3: Post-Filter Entry Quality ─────────────────────────────────────

def section3(orders, settlements):
    sep("Section 3 — Post-Filter Entry Quality")

    real_orders = [o for o in orders if is_real_order(o)]
    settle_map  = {s["ticker"]: s for s in settlements if s.get("ticker")}

    # All settled entries with their period
    def period(ts):
        return "pre-filter" if ts[:10] < FILTER_DEPLOY_DATE else "post-filter"

    buckets: dict[str, dict] = {
        "pre-filter":  {"n": 0, "wins": 0, "pnl": 0.0},
        "post-filter": {"n": 0, "wins": 0, "pnl": 0.0},
    }
    for o in real_orders:
        t   = o.get("ticker", "")
        p   = period(o.get("ts", ""))
        if t not in settle_map:
            continue
        s   = settle_map[t]
        won = s.get("won", False)
        pnl = float(s.get("pnl_usd", 0))
        buckets[p]["n"]    += 1
        buckets[p]["wins"] += int(won)
        buckets[p]["pnl"]  += pnl

    print(f"  {'Period':<15} {'Settled':>8} {'Wins':>6} {'Win Rate':>10} {'Total P&L':>12} {'Avg P&L':>9}")
    sep()
    for p in ("pre-filter", "post-filter"):
        b   = buckets[p]
        n   = b["n"]
        wr  = wrate(b["wins"], n)
        avg = b["pnl"] / n if n else 0
        print(f"  {p:<15} {n:>8} {b['wins']:>6} {pct(wr):>10} ${b['pnl']:>+10.2f} ${avg:>+8.2f}")

    sep()

    # Post-filter: breakdown by signal mix (ESPN+Vegas / ESPN only / Vegas only)
    post_settled = [
        o for o in real_orders
        if post_filter(o.get("ts", "")) and o.get("ticker") in settle_map
        and o.get("sport") == "baseball"
    ]
    if not post_settled:
        print("  No settled post-filter baseball entries yet.")
        return

    mix_buckets: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for o in post_settled:
        espn  = float(o.get("espn_win_prob", 0))
        vegas = float(o.get("vegas_live_prob", 0))
        if espn and vegas:
            mix = "ESPN + Vegas"
        elif espn:
            mix = "ESPN only"
        elif vegas:
            mix = "Vegas only"
        else:
            mix = "Markov only"
        s   = settle_map[o["ticker"]]
        won = s.get("won", False)
        pnl = float(s.get("pnl_usd", 0))
        mix_buckets[mix]["n"]    += 1
        mix_buckets[mix]["wins"] += int(won)
        mix_buckets[mix]["pnl"]  += pnl

    print(f"\n  Post-filter baseball signal mix (settled entries only):")
    print(f"  {'Signal Mix':<15} {'n':>5} {'WR':>8} {'P&L':>10}")
    sep()
    for mix in ("ESPN + Vegas", "ESPN only", "Vegas only", "Markov only"):
        b = mix_buckets.get(mix, {"n": 0, "wins": 0, "pnl": 0.0})
        if b["n"] == 0:
            continue
        print(f"  {mix:<15} {b['n']:>5} {pct(wrate(b['wins'], b['n'])):>8} ${b['pnl']:>+9.2f}")


# ── Section 4: Tennis Deep Dive ───────────────────────────────────────────────

def section4(signals, settlements):
    sep("Section 4 — Tennis Deep Dive (first analysis)")

    settle_map = {s["ticker"]: s for s in settlements if s.get("ticker")}

    # Settled tennis entries from orders
    tn_signals = [
        s for s in signals
        if s.get("sport") == "tennis"
        and s.get("direction") == "BUY"
        and s.get("exec_status") in ("submitted", "resting", "executed", "dry_run")
    ]

    # Deduplicate to first execution per ticker
    first_by_ticker: dict[str, dict] = {}
    for s in sorted(tn_signals, key=lambda x: x.get("ts", "")):
        t = s.get("ticker", "")
        if t and t not in first_by_ticker:
            first_by_ticker[t] = s

    settled_tn = {t: sig for t, sig in first_by_ticker.items() if t in settle_map}

    if not settled_tn:
        print("  No settled tennis entries found in signals_live.jsonl.")
        print("  (Check that tennis markets have resolved since scanner started.)")
        return

    print(f"  Settled tennis entries: {len(settled_tn)}")

    # Skip reason breakdown
    tn_skips = [
        s for s in signals
        if s.get("sport") == "tennis" and s.get("direction") == "BUY"
        and s.get("exec_status") == "skipped"
    ]
    skip_counts: dict[str, int] = defaultdict(int)
    for s in tn_skips:
        reason = (s.get("exec_error") or "unknown")[:55]
        skip_counts[reason] += 1

    if skip_counts:
        print(f"\n  Tennis BUY skip reasons:")
        for r, c in sorted(skip_counts.items(), key=lambda x: -x[1])[:10]:
            print(f"    {c:>4}×  {r}")

    # Win rate by current_set
    set_buckets: dict[int, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for t, sig in settled_tn.items():
        s   = settle_map[t]
        cs  = int(sig.get("current_set", 0))
        won = s.get("won", False)
        pnl = float(s.get("pnl_usd", 0))
        set_buckets[cs]["n"]    += 1
        set_buckets[cs]["wins"] += int(won)
        set_buckets[cs]["pnl"]  += pnl

    sep()
    print(f"  Win rate by current set:")
    print(f"  {'Set':>5} {'n':>5} {'WR':>8} {'P&L':>10}")
    sep()
    for cs in sorted(set_buckets):
        b = set_buckets[cs]
        label = str(cs) if cs > 0 else "unknown"
        print(f"  {label:>5} {b['n']:>5} {pct(wrate(b['wins'], b['n'])):>8} ${b['pnl']:>+9.2f}")

    # Win rate by set differential (score_diff = our_sets - opp_sets)
    diff_buckets: dict[int, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for t, sig in settled_tn.items():
        s   = settle_map[t]
        sd  = int(sig.get("score_diff", 0))
        won = s.get("won", False)
        pnl = float(s.get("pnl_usd", 0))
        diff_buckets[sd]["n"]    += 1
        diff_buckets[sd]["wins"] += int(won)
        diff_buckets[sd]["pnl"]  += pnl

    sep()
    print(f"  Win rate by set differential (our sets − opp sets):")
    print(f"  {'Diff':>6} {'n':>5} {'WR':>8} {'P&L':>10}")
    sep()
    for sd in sorted(diff_buckets):
        b = diff_buckets[sd]
        label = f"+{sd}" if sd > 0 else str(sd)
        print(f"  {label:>6} {b['n']:>5} {pct(wrate(b['wins'], b['n'])):>8} ${b['pnl']:>+9.2f}")

    # Model calibration for tennis
    sep()
    print(f"  Tennis model calibration:")
    cal_buckets: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0, "prob_sum": 0.0})
    for t, sig in settled_tn.items():
        s     = settle_map[t]
        mp    = float(sig.get("model_prob", 0))
        won   = s.get("won", False)
        pnl   = float(s.get("pnl_usd", 0))
        if mp < 0.5:
            bkt = "<50%"
        elif mp < 0.55:
            bkt = "50-55%"
        elif mp < 0.60:
            bkt = "55-60%"
        elif mp < 0.65:
            bkt = "60-65%"
        elif mp < 0.70:
            bkt = "65-70%"
        elif mp < 0.75:
            bkt = "70-75%"
        elif mp < 0.80:
            bkt = "75-80%"
        else:
            bkt = "80%+"
        cal_buckets[bkt]["n"]        += 1
        cal_buckets[bkt]["wins"]     += int(won)
        cal_buckets[bkt]["pnl"]      += pnl
        cal_buckets[bkt]["prob_sum"] += mp

    order = ["<50%", "50-55%", "55-60%", "60-65%", "65-70%", "70-75%", "75-80%", "80%+"]
    print(f"  {'Bucket':<10} {'n':>5} {'Avg Prob':>10} {'Actual WR':>10} {'Calibration':>12}")
    sep()
    for bkt in order:
        b = cal_buckets.get(bkt)
        if not b or b["n"] == 0:
            continue
        avg_p = b["prob_sum"] / b["n"]
        wr    = wrate(b["wins"], b["n"])
        calib = (wr - avg_p) if wr is not None else None
        sign  = "+" if calib and calib > 0 else ""
        print(f"  {bkt:<10} {b['n']:>5} {pct(avg_p):>10} {pct(wr):>10} "
              f"{sign+pct(calib) if calib is not None else 'N/A':>12}")


# ── Section 5: Exit Quality Analysis ─────────────────────────────────────────

def section5(exits):
    sep("Section 5 — Exit Quality Analysis")

    if not exits:
        print("  No exits recorded yet (exits_live.jsonl empty or missing).")
        return

    real_exits = [e for e in exits if not e.get("dry_run", False)]
    if not real_exits:
        print("  No live exits found (only dry-run exits).")
        return

    sl = [e for e in real_exits if e.get("reason") == "stop_loss"]
    pt = [e for e in real_exits if e.get("reason") == "profit_take"]

    def stats(group):
        n          = len(group)
        avg_pnl    = sum(e.get("pnl_usd", 0) for e in group) / n if n else 0
        avg_hold   = sum(e.get("hold_duration_sec", 0) for e in group) / n if n else 0
        avg_slip   = sum(e.get("slippage_pct", 0) for e in group) / n if n else 0
        total_pnl  = sum(e.get("pnl_usd", 0) for e in group)
        return n, avg_pnl, avg_hold, avg_slip, total_pnl

    sl_n, sl_avgpnl, sl_hold, sl_slip, sl_total = stats(sl)
    pt_n, pt_avgpnl, pt_hold, pt_slip, pt_total = stats(pt)

    print(f"  {'Reason':<15} {'Count':>7} {'Avg P&L':>9} {'Avg Hold':>12} {'Avg Slip':>10} {'Total P&L':>11}")
    sep()
    print(f"  {'stop_loss':<15} {sl_n:>7} ${sl_avgpnl:>+8.2f} {sl_hold/60:>10.1f}m "
          f"{pct(sl_slip):>10} ${sl_total:>+10.2f}")
    print(f"  {'profit_take':<15} {pt_n:>7} ${pt_avgpnl:>+8.2f} {pt_hold/60:>10.1f}m "
          f"{pct(pt_slip):>10} ${pt_total:>+10.2f}")
    print(f"  {'TOTAL':<15} {sl_n+pt_n:>7} "
          f"${(sl_total+pt_total)/(sl_n+pt_n) if (sl_n+pt_n) else 0:>+8.2f} "
          f"{'':>12} {'':>10} ${sl_total+pt_total:>+10.2f}")

    if sl:
        sep()
        print(f"  Stop-loss slippage detail:")
        print(f"  (slippage = (entry − exit) / entry — how much extra we lost vs hitting exactly at threshold)")
        slips = [(e.get("ticker","")[-30:], e.get("entry_cents",0), e.get("exit_cents",0),
                  e.get("slippage_pct",0), e.get("pnl_usd",0)) for e in sl]
        slips.sort(key=lambda x: -x[3])
        print(f"  {'Ticker':<30} {'Entry':>7} {'Exit':>7} {'Slip%':>8} {'P&L':>8}")
        sep()
        for ticker, entry, exit_c, slip, pnl in slips[:15]:
            print(f"  {ticker:<30} {entry:>6}¢ {exit_c:>6}¢ {pct(slip):>8} ${pnl:>+7.2f}")

    # Systematic SL losers — which tickers appear most often in SL exits?
    sl_ticker_counts: dict[str, int] = defaultdict(int)
    for e in sl:
        sl_ticker_counts[e.get("player", e.get("ticker", ""))] += 1
    top_sl = sorted(sl_ticker_counts.items(), key=lambda x: -x[1])[:5]
    if top_sl:
        sep()
        print(f"  Most frequent stop-loss triggers:")
        for player, cnt in top_sl:
            print(f"    {cnt}×  {player}")


# ── Section 6: Entry Spread vs Outcome ───────────────────────────────────────

def section6(orders, settlements):
    sep("Section 6 — Entry Spread vs Outcome")

    real_orders = [o for o in orders if is_real_order(o)]
    settle_map  = {s["ticker"]: s for s in settlements if s.get("ticker")}
    settled     = [o for o in real_orders if o.get("ticker") in settle_map]

    if not settled:
        print("  No settled orders with spread data.")
        return

    # Spread distribution by price range
    price_ranges = [(5, 20), (20, 40), (40, 60), (60, 80), (80, 100)]
    range_data: dict[str, dict] = {}
    for lo, hi in price_ranges:
        range_data[f"{lo}-{hi}¢"] = {"n": 0, "spread_sum": 0, "wins": 0, "pnl": 0.0}

    spread_pnl_pairs = []  # for correlation
    total_edge_lost  = 0.0

    for o in settled:
        spread = int(o.get("spread_cents", 0))
        ask    = int(o.get("price_cents", 0))
        conts  = int(o.get("contracts", 0))
        s      = settle_map[o["ticker"]]
        won    = s.get("won", False)
        pnl    = float(s.get("pnl_usd", 0))

        total_edge_lost += spread * conts / 100

        # Classify by ask price range
        key = None
        for lo, hi in price_ranges:
            if lo <= ask < hi:
                key = f"{lo}-{hi}¢"
                break
        if key:
            range_data[key]["n"]          += 1
            range_data[key]["spread_sum"] += spread
            range_data[key]["wins"]       += int(won)
            range_data[key]["pnl"]        += pnl

        spread_pnl_pairs.append((spread, pnl))

    print(f"  {'Ask Range':<12} {'n':>5} {'Avg Spread':>12} {'Win Rate':>10} {'Total P&L':>11}")
    sep()
    for lo, hi in price_ranges:
        key = f"{lo}-{hi}¢"
        d   = range_data[key]
        n   = d["n"]
        if n == 0:
            continue
        avg_spread = d["spread_sum"] / n
        print(f"  {key:<12} {n:>5} {avg_spread:>10.1f}¢  {pct(wrate(d['wins'], n)):>10} ${d['pnl']:>+10.2f}")

    sep()
    print(f"  Total edge lost to entry spread: ${total_edge_lost:.2f}")

    # Pearson correlation: spread vs P&L
    if len(spread_pnl_pairs) >= 5:
        xs  = [p[0] for p in spread_pnl_pairs]
        ys  = [p[1] for p in spread_pnl_pairs]
        n   = len(xs)
        mx  = sum(xs) / n
        my  = sum(ys) / n
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
        sx  = math.sqrt(sum((x - mx)**2 for x in xs) / n)
        sy  = math.sqrt(sum((y - my)**2 for y in ys) / n)
        r   = cov / (sx * sy) if sx * sy > 0 else 0
        print(f"  Pearson r (spread_cents vs pnl_usd): {r:+.3f}  "
              f"({'negative = wide spread → worse outcome' if r < -0.1 else 'no strong correlation' if abs(r) < 0.2 else 'positive = wide spread → better outcome'})")


# ── Section 7: Score Differential Revisited ──────────────────────────────────

def section7(orders, settlements):
    sep(f"Section 7 — Score Differential Revisited (post-{FILTER_DEPLOY_DATE} baseball only)")

    real_orders = [o for o in orders if is_real_order(o)]
    settle_map  = {s["ticker"]: s for s in settlements if s.get("ticker")}

    post_bb = [
        o for o in real_orders
        if o.get("sport") == "baseball"
        and post_filter(o.get("ts", ""))
        and o.get("ticker") in settle_map
    ]

    if not post_bb:
        print("  No settled post-filter baseball entries yet.")
        return

    # Score differential summary
    sd_buckets: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for o in post_bb:
        sd  = int(o.get("score_diff", 0))
        s   = settle_map[o["ticker"]]
        won = s.get("won", False)
        pnl = float(s.get("pnl_usd", 0))
        if sd <= -3:
            key = "≤-3 (big deficit)"
        elif sd == -2:
            key = "-2"
        elif sd == -1:
            key = "-1 (trailing)"
        elif sd == 0:
            key = " 0 (tied)"
        elif sd == 1:
            key = "+1 (slim lead)"
        elif sd == 2:
            key = "+2"
        else:
            key = "≥+3 (big lead)"
        sd_buckets[key]["n"]    += 1
        sd_buckets[key]["wins"] += int(won)
        sd_buckets[key]["pnl"]  += pnl

    order = ["≤-3 (big deficit)", "-2", "-1 (trailing)", " 0 (tied)",
             "+1 (slim lead)", "+2", "≥+3 (big lead)"]
    print(f"  {'Score Diff':<20} {'n':>5} {'WR':>8} {'P&L':>10}  Note")
    sep()
    for key in order:
        b = sd_buckets.get(key)
        if not b or b["n"] == 0:
            continue
        wr   = wrate(b["wins"], b["n"])
        note = ""
        if key == "+1 (slim lead)" and b["n"] > 0:
            note = "← filter blocks inning 6+ only; early innings still enter"
        elif wr is not None and wr < 0.45:
            note = "← below 50%"
        elif wr is not None and wr > 0.60:
            note = "← strong edge"
        print(f"  {key:<20} {b['n']:>5} {pct(wr):>8} ${b['pnl']:>+9.2f}  {note}")

    # Inning × score_diff heatmap (post-filter data)
    sep()
    print(f"  Inning × Score Diff win-rate heatmap (n in parens, post-filter only):")

    SCORE_COLS = [-3, -2, -1, 0, 1, 2, 3]
    INNING_ROWS = list(range(1, 10)) + [10]  # 10 = "9+"

    heatmap: dict[tuple, dict] = defaultdict(lambda: {"n": 0, "wins": 0})
    for o in post_bb:
        inn = int(o.get("inning", 0))
        sd  = int(o.get("score_diff", 0))
        s   = settle_map[o["ticker"]]
        won = s.get("won", False)
        inn_key = min(inn, 9) if inn > 0 else 0
        sd_key  = max(-3, min(3, sd))
        heatmap[(inn_key, sd_key)]["n"]    += 1
        heatmap[(inn_key, sd_key)]["wins"] += int(won)

    col_labels = ["≤-3", " -2", " -1", "  0", " +1", " +2", "≥+3"]
    header = f"  {'Inn':>4}  " + "  ".join(f"{c:>8}" for c in col_labels)
    print(header)
    sep()
    for inn in INNING_ROWS:
        inn_label = f"{inn}+" if inn == 10 else str(inn)
        row = f"  {inn_label:>4}  "
        for sd in SCORE_COLS:
            sd_k = max(-3, min(3, sd))
            cell = heatmap.get((inn if inn < 10 else 9, sd_k), {"n": 0, "wins": 0})
            if cell["n"] == 0:
                row += f"{'  —':>8}  "
            else:
                wr = cell["wins"] / cell["n"]
                flag = "▲" if wr >= 0.60 else ("▼" if wr <= 0.40 else " ")
                row += f"{pct(wr, 0):>6}({cell['n']}){flag} "
        print(row)
    print(f"  ▲ = ≥60% WR   ▼ = ≤40% WR")


# ── Section 8: Settlements Integrity Check ───────────────────────────────────

def section8(orders, settlements):
    sep("Section 8 — Settlements Integrity Check")

    # Dedup check
    ticker_counts: dict[str, int] = defaultdict(int)
    for s in settlements:
        ticker_counts[s.get("ticker", "")] += 1

    dupes = {t: c for t, c in ticker_counts.items() if c > 1}
    total_rows   = len(settlements)
    unique_count = len(ticker_counts)

    print(f"  settlements.jsonl rows : {total_rows}")
    print(f"  Unique tickers         : {unique_count}")
    if dupes:
        print(f"  DUPLICATES FOUND       : {len(dupes)} tickers with >1 row  "
              f"(dedup fix may not have taken effect yet on existing data)")
        for t, c in sorted(dupes.items(), key=lambda x: -x[1])[:5]:
            print(f"    {c}×  {t}")
    else:
        print(f"  No duplicates — dedup fix working correctly.")

    sep()

    # Cross-reference with orders
    real_orders = [o for o in orders if is_real_order(o)]
    order_tickers  = {o["ticker"] for o in real_orders if o.get("ticker")}
    settled_tickers = {s["ticker"] for s in settlements if s.get("ticker")}

    in_orders_not_settled = order_tickers - settled_tickers
    in_settled_not_orders = settled_tickers - order_tickers

    print(f"  Real orders (unique tickers)     : {len(order_tickers)}")
    print(f"  Settlements (unique tickers)     : {len(settled_tickers)}")
    print(f"  In orders, not yet settled       : {len(in_orders_not_settled)}  (open or pending)")
    print(f"  In settlements, no matching order: {len(in_settled_not_orders)}  (pre-log or addon settlements)")

    if in_orders_not_settled:
        still_open = sorted(in_orders_not_settled)
        print(f"\n  Unsettled tickers (first 10):")
        for t in still_open[:10]:
            print(f"    {t}")

    sep()

    # P&L reconciliation
    pnl_settle = sum(s.get("pnl_usd", 0) for s in settlements)
    wins  = sum(1 for s in settlements if s.get("won"))
    loses = sum(1 for s in settlements if not s.get("won") and s.get("result") in ("yes", "no"))
    print(f"  Settlements P&L: ${pnl_settle:+.2f}  (won={wins}, lost={loses})")
    print(f"  Avg per settled trade: ${pnl_settle/(wins+loses) if (wins+loses) else 0:+.2f}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * W)
    print(f"{'deepdive6.py — Post-Strategy-Adjustment Analysis':^{W}}")
    print(f"{'Filter deploy date: ' + FILTER_DEPLOY_DATE:^{W}}")
    print("=" * W)

    signals     = load_jsonl(SIGNALS_FILE)
    orders      = load_jsonl(ORDERS_FILE)
    settlements = load_jsonl(SETTLEMENTS_FILE)
    exits       = load_jsonl(EXITS_FILE)
    snapshots   = load_jsonl(SNAPSHOTS_FILE)

    print(f"\n  Loaded: {len(signals)} signals, {len(orders)} order rows, "
          f"{len(settlements)} settlement rows, {len(exits)} exits, "
          f"{len(snapshots)} snapshots")

    section1(signals, orders, settlements, snapshots)
    section2(signals, settlements)
    section3(orders, settlements)
    section4(signals, settlements)
    section5(exits)
    section6(orders, settlements)
    section7(orders, settlements)
    section8(orders, settlements)

    print("\n" + "=" * W)
    print(f"{'deepdive6 complete':^{W}}")
    print("=" * W)


if __name__ == "__main__":
    main()
