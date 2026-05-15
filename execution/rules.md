# Execution Layer

Rules and decisions for live trade execution. This layer sits **after** the ML model outputs a WIN probability and the scoring system flags a trade — it governs *how* a trade is actually placed.

**Separation principle:** The signal layer (scorer) and ML layer predict edge. This layer handles everything that happens at the broker. Keep these concerns separate — do not embed execution rules in scoring logic.

---

## Pipeline Position

```
Capitol Trades scraper → scorer / ML model → Execution layer → Broker
                                                    ↑
                                               This file
```

A trade reaches execution only if it clears:
1. Compliant buy (stock, disclosed within STOCK_ACT window)
2. Cluster filter (scorer)
3. Repeat-before-close filter (scorer)
4. ML win probability threshold (TBD — set when model is trained)
5. Execution-layer filters below

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
- Analysis on scorer-filtered set is consistent but has thin tails — revisit after FMP Senate backfill adds more data.

**Note:** `day1_gap_pct` is NOT a valid ML feature — it requires `day1_open` which is unknown at disclosure time when the model runs. LOO filter is the correct home for this signal.

**Revisit after ML:** This entire LOO analysis was run on all compliant buys (n=16,549) and the scorer-filtered set (n=3,830). Once the ML model is trained and we have a clearer picture of which trades we'd actually take, redo this analysis on that population. Tail sample sizes in the scored set are already thin — the ML-filtered set will be smaller still, so conclusions may shift. The +5% threshold is a reasonable interim heuristic; treat it as provisional.

**Pre-market prices — investigate:** Pre-market price at ~9:00–9:25am could serve as an early signal of the likely gap before the opening auction. If pre-market is already well above the LOO limit, the trade can be skipped before submitting the MOO — no need to wait for the open. Questions to explore:
- How well does pre-market price predict the actual day-1 open? (correlation, typical slippage)
- Does pre-market move size add predictive signal beyond the disclosure-close gap alone?
- Could pre-market data improve the LOO threshold decision, or enable a dynamic limit?
- Source: yfinance provides pre-market data for many tickers — check coverage and reliability.
This is an enhancement to the execution layer, not the ML model.

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

**Current decision:** Manual is acceptable at current trade frequency. Revisit if ML narrows to consistent volume.

Documented in `issues_and_fixes/` — item 5b (Execution Layer Design).

---

## Basket Day Filter

**Skip any signal where `basket_day = 1`, UNLESS the cluster filter is met.**

A basket day is when a politician files ≥10 tickers on the same trade_date or ≥20 on the same disclosure_date. Basket trades have identical win rates to non-basket trades (56.9% vs 57.7%) so the ML model is trained on them — but they are generally not executable:

- Filing 15 stocks on the same day means 15 simultaneous MOO orders at open
- Capital cannot be meaningfully sized across that many positions at once
- The signal per individual ticker is weaker — more likely portfolio rebalancing than targeted conviction

**Exception — cluster filter met:** When `cluster_count_td >= 3 AND abs_pct_move_before_disclosure >= 15`, basket_day=1 trades should NOT be excluded. The cluster filter naturally culls a 15-ticker basket filing to the 1–2 names where other politicians also independently bought — those names outperform:

| Condition | n | WR |
|---|---|---|
| basket=0, cluster>=3 + abs_move>=15% | 71 | 74.6% |
| basket=1, cluster>=3 + abs_move>=15% | 110 | 80.0% |

**How to apply:** Check `basket_day` column on the signal. If `basket_day = 1` AND the cluster filter is not met, skip. If `basket_day = 1` AND cluster filter IS met, treat as a normal signal.

---

## Position / Portfolio Rules (Not Yet Implemented)

These are decided in principle but not built. They apply at runtime when deciding whether to actually place an order for a new signal.

| Rule | Detail |
|---|---|
| No double-up | Don't open ticker X if already in an open position in X from a prior signal |
| Max concurrent per ticker | TBD — set when trade volume is better understood |
| Max concurrent per politician | TBD |
| Position sizing | Flat sizing initially; confidence-weighted sizing once ML is trained |
| Portfolio exposure limit | TBD |

---

## What Does NOT Belong Here

- Win/loss prediction — that is the ML layer
- Historical win rate measurement — that is the signal/scorer layer
- Deduplication of scraped trades — that is the pipeline layer
- Price path fetching — that is the pipeline layer
