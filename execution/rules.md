# Execution Layer

Rules and decisions for live trade execution. This layer sits **after** the signal filter flags a trade — it governs *how* a trade is actually placed.

**Separation principle:** The signal layer (scorer + filter) predicts edge. This layer handles everything that happens at the broker. Keep these concerns separate — do not embed execution rules in scoring logic.

---

## Pipeline Position

```
Capitol Trades scraper → scorer + signal filter → Execution layer → Broker
                                                    ↑
                                               This file
```

A trade reaches execution only if it clears:
1. Compliant buy (stock, disclosed within STOCK_ACT window)
2. Basket/dump exclusion (scorer) — see note below, does not itself block a trade
3. Repeat-before-close filter (scorer)
4. Signal filter: `cluster_count_td >= 2 AND abs_pct_move_before_disclosure >= 15` (see Signal Filter section)
5. Execution-layer filters below

**Note on item 2:** the scorer's basket/dump exclusion (`CLUSTER_THRESHOLD`/`DISCLOSURE_CLUSTER_THRESHOLD` in `pipeline/scorer.py`) only removes a politician's trades from *that politician's own leaderboard score calculation* — it does not remove them from the `trades` table, and it does not remove them from other politicians' `cluster_count_td` counts. A trade from a politician who is otherwise excluded from scoring (e.g. Ro Khanna, basket-dumping 169 tickers/day) can still itself clear the signal filter in item 4 and be tradeable. Don't confuse this with the `cluster_count_td` signal used in item 4 — same word, different mechanism.

---

## Signal Filter

**`cluster_count_td >= 2 AND abs_pct_move_before_disclosure >= 15`**

Replaces the originally planned ML win-probability gate. ML was shelved 2026-05-06 — the model was macro-dominated (SPY 200ma was the top feature) and the congressional signal was too weak in recent years to be useful (2025 AUC = 0.538). This simple, interpretable filter replaced it. Full analysis in `ml/filter_analysis.md`.

Both signals are weak standalone (`abs_pct_move_before_disclosure>=15%` alone: 60.1% WR; `cluster_count_td>=3` alone: 66.1% WR) — they multiply when combined, which is why neither works alone.

| | cluster>=3 (tighter) | cluster>=2 (landed) |
|---|---|---|
| WR with abs_move>=15% | 77.9% (n=181) | 74.7% (n=304) |
| Normal-year (2023–24) frequency | 11–19/yr | 23–32/yr |
| Normal-year WR range | 68–82% | 74–81% |
| Max drawdown | 8.6% | 13.2% |
| Equity curve, incl. 2025 tariff event | $272k — loses to SPY ($339k) | **$441k — beats SPY** |
| Equity curve, ex-tariff | $150k — loses to SPY | $192k — loses to SPY |
| 2022 bear market | Fails (42.9% vs 47.1% baseline) | Fails (46.2% vs 47.1% baseline) |

**Decision: cluster>=2.** More tradeable frequency, and the only variant that beats SPY on absolute return (when the 2025 tariff event is included — neither variant beats SPY ex-tariff). Tradeoff is a wider max drawdown (13.2% vs 8.6%), which is acceptable given both stay well inside SPY's ~34% 2022 drawdown. Neither variant works in a sustained bear market (2022) — this is a real, acknowledged failure mode, not an edge case to ignore.

---

## Entry Order Type

**MOO (Market-on-Open).** Fills at the opening auction price on day 1 after disclosure.

- Backtesting anchors entry to `trade_price_paths` day-1 open — MOO is consistent with this.
- Confirmed switch from close-of-disclosure-day entry: 2026-03-31. Win rate delta was immaterial (−0.56pp).
- Documented in `issues_and_fixes/issue_002_day1_open_results.md`.

---

## LOO Filter (Gap-Up Skip)

**Threshold: +5% above disclosure-date close.**

If the stock gaps up more than 5% from the disclosure-date close to the day-1 open, skip the trade entirely. Rationale: large gap-ups suggest information is already priced in; filtered trades at this threshold have a 52.9% win rate vs 57.0% for kept trades.

**How to apply (manual):**
```
limit = price_at_disclosure_date × 1.05
```
Before market open: check pre-market price. If pre-market is already above `limit`, cancel the MOO and skip. Otherwise, submit MOO — if day-1 open exceeds limit, the order won't fill (or skip manually post-open if not using a true LOO order type).

**Evidence (issue_007_gap_up_analysis.md, n=16,375 decided trades):**

| Gap range | Trades | Win rate |
|---|---|---|
| −2% to +2% (flat) | 14,546 | 57.3% |
| +2% to +5% | 676 | 52.2% |
| +5% to +10% | 96 | 57.3% (neutral — small sample) |
| >+10% | 23 | 34.8% |

- +5% threshold: filters 119 trades (0.7% of total), trivial volume cost.
- +10% is the unambiguous minimum floor (34.8% WR on filtered trades).

**Note:** `day1_gap_pct` requires `day1_open`, which is unknown at disclosure time — it can only be evaluated at the execution layer (this LOO filter), not as an upstream point-in-time feature.

**Status: provisional, not yet re-tested against the live filter population.** This LOO analysis was run on all compliant buys (n=16,549) and the old scorer-filtered set (n=3,830) — it has not been re-run against the current signal-filter population (n=181–304 depending on cluster threshold). That population is small enough that tail conclusions could shift. Revisit once live trade volume under the cluster>=2 filter is large enough to test on directly. The +5% threshold remains a reasonable interim heuristic until then.

**Pre-market prices — investigate:** Pre-market price at ~9:00–9:25am could serve as an early signal of the likely gap before the opening auction. If pre-market is already well above the LOO limit, the trade can be skipped before submitting the MOO — no need to wait for the open. Questions to explore:
- How well does pre-market price predict the actual day-1 open? (correlation, typical slippage)
- Does pre-market move size add predictive signal beyond the disclosure-close gap alone?
- Could pre-market data improve the LOO threshold decision, or enable a dynamic limit?
- Source: yfinance provides pre-market data for many tickers — check coverage and reliability.
This is an enhancement to the execution layer.

---

## Stop-Loss and Take-Profit

Anchored to the **actual MOO fill price**, not the disclosure-date close.

```
entry       = day1_open (MOO fill)
stop_loss   = entry × 0.90   (−10%)
take_profit = entry × 1.10   (+10%)
```

These match the backtesting model exactly. Do NOT pre-calculate from disclosure close — if the stock gaps, stop/target will be misaligned with actual fill.

### Post-Fill Workflow Options

| Option | Description | Status |
|---|---|---|
| Manual | See fill at open, enter stop and target manually | **Current approach** — acceptable at 1–2 trades/week |
| Broker bracket | IBKR: attach stop/target as % offset from fill at order submission | Preferred if volume grows |
| API automation | Submit MOO, listen for fill, calculate and submit OCA stop+target | Only needed at high volume |

**Current decision:** Manual is acceptable at current trade frequency. Revisit if live trade volume grows to the point where manual entry becomes a bottleneck.

Documented in `issues_and_fixes/` — item 5b (Execution Layer Design).

---

## Basket Day Filter

**Skip any signal where `basket_day = 1`, UNLESS the signal filter is met.**

A basket day is when a politician files ≥10 tickers on the same trade_date or ≥20 on the same disclosure_date. Basket trades have identical win rates to non-basket trades (56.9% vs 57.7%) so they're kept in the underlying dataset with a `basket_day` flag rather than dropped — but they are generally not executable:

- Filing 15 stocks on the same day means 15 simultaneous MOO orders at open
- Capital cannot be meaningfully sized across that many positions at once
- The signal per individual ticker is weaker — more likely portfolio rebalancing than targeted conviction

**Exception — signal filter met:** When `cluster_count_td >= 3 AND abs_pct_move_before_disclosure >= 15`, basket_day=1 trades should NOT be excluded. The filter naturally culls a 15-ticker basket filing to the 1–2 names where other politicians also independently bought — those names outperform:

| Condition | n | WR |
|---|---|---|
| basket=0, cluster>=3 + abs_move>=15% | 71 | 74.6% |
| basket=1, cluster>=3 + abs_move>=15% | 110 | 80.0% |

**Note:** this basket-day interaction was only measured at `cluster_count_td>=3`, not at the landed `>=2` threshold. It has not been separately re-verified at `>=2` — treat the exception as provisional at `>=2` until re-tested.

**How to apply:** Check `basket_day` column on the signal. If `basket_day = 1` AND the signal filter is not met, skip. If `basket_day = 1` AND the signal filter IS met, treat as a normal signal.

---

## Position / Portfolio Rules (Not Yet Implemented)

These are decided in principle but not built. They apply at runtime when deciding whether to actually place an order for a new signal.

| Rule | Detail |
|---|---|
| No double-up | Don't open ticker X if already in an open position in X from a prior signal |
| Max concurrent per ticker | TBD — set when trade volume is better understood |
| Max concurrent per politician | TBD |
| Position sizing | Flat sizing (no ranking/weighting model planned since ML was shelved) |
| Portfolio exposure limit | TBD |

---

## What Does NOT Belong Here

- Win/loss prediction and historical win rate measurement — that is the signal/scorer layer (`ml/filter_analysis.md`)
- Deduplication of scraped trades — that is the pipeline layer
- Price path fetching — that is the pipeline layer
