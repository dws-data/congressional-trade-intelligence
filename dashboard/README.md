Streamlit web dashboard for viewing politician scores, trade signals, and pipeline health.

---

## Purpose

The dashboard is an **exploration and data quality tool**, not a trading signal generator.

Its primary uses are:
- Identifying which politicians are worth watching at all (active, meaningful trade sizes, some sector focus)
- Surfacing data quality anomalies and scoring edge cases
- Understanding the dataset before building the ML layer
- Monitoring pipeline health and data freshness
- Investigating specific politicians and their trade history

---

## Limitations of the Leaderboard

**The score is a blunt instrument.**
It tells you "this politician has traded well historically" — not "this specific trade is worth taking." A high-scoring politician who files 80 tickers on the same day is not actionable. The score conflates signal quality with noise.

**Small samples dominate the top.**
Politicians with fewer than 10 trades (LOW CONF) frequently rank above politicians with 80+ trades. The leaderboard rewards lucky small samples. Statistically meaningful politicians tend to be mid-table.

**No recency weighting.**
A politician who last traded in 2020 scores the same as one who traded last week. The LAST BUY column and LAST TRADED filter partially address this for browsing, but the score itself doesn't decay.

**Backwards-looking in the wrong way.**
The score summarises past performance but gives no per-trade win probability. Without knowing size, sector, market context, filing lag, and cluster count for a specific trade, the score alone is not a reliable execution signal.

**What is genuinely useful from the leaderboard:**
- The Natural Resources committee win rate (93.3%) — a structural edge signal the ML can build on
- Identifying clearly inactive or basket-only traders to exclude from the execution universe
- The FEED tab — actionable recent disclosures with committee relevance and routine buyer flags

---

## What the ML Layer Unlocks

The ML model replaces "watch politician X" with a per-trade win probability:

> "This specific disclosure, on this ticker, with this size, filed by this politician at this lag,
> in this market context, has a 71% win probability."

The politician's historical score becomes one feature among many — not the headline number.
The leaderboard remains useful for exploration and QA. The ML layer is the actual trading signal.

---

*Last updated: 2026-04-08*
