# Kalshi Sports — Pure Math Strategy Brief

## Goal
Build a systematic, math-driven trading approach for Kalshi sports binary contracts that requires **no sports knowledge or market familiarity** — only price data and probability math.

---

## Core Strategy Stack

### 1. Cross-Platform Arbitrage
**Role:** Primary edge source

- Compare Kalshi contract prices against implied probabilities on other sportsbooks (DraftKings, FanDuel, etc.)
- If the same outcome is priced at a meaningful discount on Kalshi relative to the implied probability elsewhere, that gap is the edge
- No directional opinion needed — pure price discrepancy
- **Requires:** Accounts on multiple platforms, fast price comparison, execution speed

**Signal example:** Kalshi has Team A YES at 44¢. DraftKings moneyline implies Team A wins at 52% probability. Buy YES on Kalshi.

---

### 2. Cross-Market Correlation (Same Game)
**Role:** Secondary edge source

- Kalshi often lists multiple contract types on the same game (moneyline, spread, totals)
- These markets are mathematically linked — if one moves, the others should follow
- When one contract reprices and a correlated contract on the same game hasn't caught up, that lag is an exploitable inefficiency
- **Requires:** Monitoring multiple contracts per game simultaneously, understanding the mathematical relationship between contract types

**Signal example:** Moneyline for Team A shifts from 50¢ to 62¢ (new info). The first-half spread contract on the same game hasn't moved yet. The lag is the trade.

---

### 3. Kelly Criterion — Bet Sizing
**Role:** Bankroll management / position sizing

- Once an edge is identified (via arb or correlation), Kelly Criterion determines the mathematically optimal fraction of bankroll to wager
- Prevents overbetting (ruin) and underbetting (leaving edge on the table)
- Formula: `f* = (bp - q) / b`
  - `b` = net odds on the bet (e.g., buying at 44¢ pays out 56¢ profit on a $1 contract → b = 56/44)
  - `p` = estimated probability of winning
  - `q` = 1 - p

- Use **fractional Kelly** (e.g., half-Kelly) to reduce variance in practice

---

### 4. Time Decay Awareness — Entry/Exit Filter
**Role:** Trade timing filter

- Sports contracts have a hard resolution time, which affects fair value
- The same price (e.g., 45¢) carries different meaning at different points:
  - 3 days before game: wide uncertainty, price reflects pre-game odds
  - 10 minutes into game, tied score: price should be near 50¢ by entropy logic
  - 4th quarter, large deficit: price should be collapsing toward 0 or 100
- Use time remaining + current game state as a filter for whether a price is mispriced relative to fair value
- **Avoid entering positions** when time decay is working against you (e.g., buying YES at 45¢ late in a game where the team is losing)

---

## Suggested Build Order

1. **Price ingestion** — Pull live Kalshi contract prices via API
2. **Arb scanner** — Compare against sportsbook implied probabilities in real time
3. **Correlation mapper** — Link related contracts on the same game, flag when one moves without the other
4. **Kelly sizer** — Given edge and current bankroll, output recommended position size
5. **Time decay filter** — Tag each opportunity with time-to-resolution and apply fair value sanity check before executing

---

## Constraints / Risks to Model

- Kalshi charges fees — factor into arb calculations before flagging a trade
- Thin order books on smaller games can cause slippage — check available liquidity before sizing
- Breaking news (injuries, weather) will cause fast price moves that invalidate pre-news arb signals — consider a news event cooldown filter
- Kelly assumes accurate probability estimates — garbage in, garbage out on sizing
