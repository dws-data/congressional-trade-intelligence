# Whitepaper v6 — Additions & Updates
# For integration into Congressional_Trade_Tracker_v5.docx

---

## 1. New Database Columns (Schema Update)

The following columns were added to the `trades` table during the scoring QA phase:

| Column | Type | Description |
|---|---|---|
| `close_date_disc` | TEXT (ISO date) | Calendar date when the trade hit either the +10% target or -10% stop loss, measured from disclosure date. NULL if the trade is still open (unresolved within 180 days). |
| `days_to_exit_disc` | INTEGER | Trading days from disclosure date until the position closed (TP or SL hit). NULL if open. Previously misnamed `days_to_profitability_disc` — renamed to reflect that it covers both wins and losses. |
| `days_to_exit_trade` | INTEGER | Same as above but measured from trade date. Previously misnamed `days_to_profitability`. |

**Note:** `close_date_disc` is derived by joining the price path table on the exit day to retrieve the actual calendar date, giving exact position close dates for all resolved trades.

---

## 2. Repeat Buy Empirical Analysis

A dedicated analysis was run (`qa/repeat_buy_analysis.py`) to empirically test whether informational edge degrades when a politician buys the same ticker multiple times. This was motivated by politicians such as Virginia Foxx (ARLP ×13, HTGC ×13) and Lloyd Doggett (KO ×11, JNJ ×9) who exhibit systematic repeat-buying behaviour.

### 2.1 Does Edge Degrade With Repetition?

For each (politician, ticker) pair, trades were sorted chronologically and labelled by sequence position.

| Sequence Position | Win Rate | Trades | Distinct Pairs | Politicians |
|---|---|---|---|---|
| 1st buy | 58.7% | 1,242 | 1,242 | 97 |
| 2nd buy | 57.3% | 393 | 393 | 50 |
| 3rd+ buy | 62.4% | 441 | 178 | 35 |

**Finding:** No meaningful trend. Win rate is flat across sequence positions. The slightly elevated 3rd+ figure is explained by composition — Virginia Foxx's high-win-rate MLP trades dominate that bucket. A sequence-position cap (e.g. "only score first 3 buys per ticker") has no empirical basis.

### 2.2 Does Overlapping With an Open Position Affect Accuracy?

For each repeat buy, the analysis checked whether the new disclosure date fell before `close_date_disc` of the previous trade in that ticker — i.e., whether the politician was effectively buying into an already-open position.

| Status | Win Rate | Trades |
|---|---|---|
| Within open window (correlated) | 59.7% | 215 |
| After close (independent) | 60.1% | 619 |

**Finding:** Essentially identical win rates. Overlapping repeat buys are not less accurate — they are however less independent. The concern is trade count inflation (correlated outcomes being counted as separate evidence of edge), not accuracy degradation.

### 2.3 Does Gap Between Repeat Buys Matter?

| Gap to Previous Buy | Win Rate | n |
|---|---|---|
| < 30 days | 51.2% | 132 |
| 30–90 days | 63.0% | 268 |
| 90–180 days | 61.2% | 228 |
| > 180 days | 60.3% | 206 |

**Finding:** The only meaningful signal is in the sub-30-day bucket, where win rate drops to 51.2% — effectively at the random baseline. Very short-interval repeat buys of the same ticker appear to be noise or averaging behaviour rather than fresh informational signals. Buys separated by 30+ days show consistent edge across all gap sizes.

**Implication:** A rule excluding same-ticker repeat buys within 30 days of a previous buy has empirical support. This is distinct from the concentration question and is a data quality filter (removing near-duplicate correlated trades).

---

## 3. System Architecture — Three Layers

The analysis clarified a distinction between three conceptually separate layers of the system:

### Layer 1 — Signal Layer (current)
Scores politicians based on historical trade accuracy. Measures whether a politician exhibits informational edge. Outputs: politician scores, win rates, trade outcomes.

### Layer 2 — ML Layer (planned)
Uses engineered features to predict win probability for individual trades as they are disclosed in real time. Takes signal layer outputs and additional trade/politician features as inputs.

### Layer 3 — Execution Layer (planned)
Applies portfolio and strategy rules at runtime when deciding whether to act on a signal. This layer is deliberately separate from the signal and ML layers — it asks a different question: *given my current portfolio state, should I act on this signal now?*

Example execution rules:
- Do not open a position in ticker X if already holding an open position in X from a prior signal
- Maximum concurrent positions per politician or per sector
- Position sizing proportional to signal confidence score
- Portfolio-level exposure limits

**Key principle:** The scoring and ML layers should measure edge as cleanly as possible, without being contaminated by execution constraints. Execution rules sit on top and filter or size signals at runtime.

---

## 4. ML Feature Candidates

The following features were identified during the repeat-buy analysis for use in the future ML layer. Not yet implemented.

### Trade-Level Features
*(predict whether a specific disclosed trade will win)*

| Feature | Description | Basis |
|---|---|---|
| `seq_position` | Sequence position of this ticker buy for this politician (1st, 2nd, 3rd+) | Empirically flat — useful as control variable |
| `gap_since_prev_buy` | Calendar days since this politician's last buy of this ticker (NULL if first) | Sub-30d group showed 51% win rate — near chance |
| `prev_trade_open` | Boolean: was the previous position in this ticker still open when this trade was disclosed? | Flags correlated repeat buys |

### Politician-Level Features
*(characterise trading style and edge quality)*

| Feature | Description | Basis |
|---|---|---|
| `ticker_concentration_pct` | % of total scored trades in the politician's single most-traded ticker | Foxx: MLPs dominate; Bresnahan: 34 distinct tickers |
| `ticker_hhi` | Herfindahl-Hirschman Index across all scored tickers (0 = fully diversified, 1 = single ticker) | Continuous concentration measure — better than a hard cap |
| `within_window_repeat_rate` | % of trades that are within-window repeats of an open prior position | High = systematic buyer; Low = targeted, opportunistic |

### Committee-Level Features
*(from earlier committee alignment analysis)*

| Feature | Description |
|---|---|
| `committee_relevance` | Binary flag — trade is in a sector aligned with politician's committee membership (already in DB) |
| Committee identity | Win rates vary strongly by committee: Natural Resources 93.3%, Ways & Means 67.4%, Senate HELP 11.1% — committee membership itself is a feature |

---
*Additions prepared 2026-03-24. For integration into v5 whitepaper.*
