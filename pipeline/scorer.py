# pipeline/scorer.py
# Calculates composite scores for each politician based on:
#   - Clean Win Rate (10/10 stop/target from disclosure date) — 50%
#   - Drawdown Profile (avg max drawdown from disclosure date) — 30%
#   - Large Trade Accuracy (10/10 win rate on trades > $50k)  — 20%
#
# Minimum threshold: 10 completed compliant buy trades
# Score range: 0-100

import sqlite3
import math
from datetime import datetime, timedelta

# ─────────────────────────────────────────────
# SCORING FILTERS — WHY EACH EXISTS
# ─────────────────────────────────────────────
#
# 1. transaction_type = 'buy' + filing_violation = 'compliant'
#    Only score legitimate buy trades filed within STOCK Act limits.
#    Sells and late/amended filings excluded.
#
# 2. price_at_disclosure_date NOT NULL
#    Need an entry price to simulate the 10/10 strategy.
#
# 3. price_fetch_failed = 0
#    Excludes trades where yfinance returned corrupt data (e.g. HURA
#    2024-05-27, Laurel Lee — stock split caused bad price path).
#
# 4. asset_type = 'stock' (stock score) / 'etf' or 'fund' (ETF score)
#    ETFs and funds track broad markets — buying VUG or IVV cannot
#    reflect insider knowledge on individual stocks. Scored separately
#    so ETF win rates don't inflate the stock signal. Hal Rogers (100%
#    win rate, all ETFs) was the trigger for this split.
#
# 5. trade_date NOT NULL
#    Blank trade dates indicate poorly-filed disclosures. Greg Stanton
#    filed 94 blue-chip stocks all with no trade date on a single
#    disclosure — effectively disclosing a whole portfolio at once. With
#    no trade date there is no way to assess timing or intent.
#
# 6. Trade-date cluster filter: exclude (politician, trade_date) pairs where
#    ≥ 10 distinct tickers were traded on the same day. Buying 10+ different
#    stocks on the same day is a portfolio construction/rebalancing event,
#    not targeted informed trading. Ro Khanna (up to 169 tickers/day
#    across multiple dates), Lisa McClain (146), and others drove this.
#
# 7. Disclosure-date cluster filter: exclude (politician, disclosure_date)
#    events where ≥ 20 distinct tickers appear in a single filing. This
#    catches multi-day portfolio construction spread across a filing window
#    that slips under the per-day threshold. E.g. Westerman bought 81 tickers
#    on one day (caught by filter 6) PLUS 18 more across other days — all
#    filed in one event on 2025-04-17 (caught here). Threshold is higher (20)
#    than the trade-date filter because legitimate batch filings can contain
#    10-19 individually timed trades filed together within the 45-day window.
#
# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

DB_PATH           = "data/trades.db"
STOP_PCT          = 10.0
TARGET_PCT        = 10.0
MIN_TRADES        = 5       # minimum compliant buy trades to get a score
LARGE_TRADE       = 50000   # minimum size_midpoint to count as large trade
MIN_LARGE         = 5       # minimum large trades to score large trade accuracy
CLUSTER_THRESHOLD          = 10   # max tickers per trade_date — catches single-day basket buys
DISCLOSURE_CLUSTER_THRESHOLD = 20  # max tickers per disclosure_date — catches multi-day portfolio dumps filed together

# Scoring weights
W_WIN_RATE     = 0.50
W_DRAWDOWN     = 0.30
W_LARGE        = 0.20

# ─────────────────────────────────────────────
# SIMULATE 10/10 OUTCOME FOR A SINGLE TRADE
# ─────────────────────────────────────────────

def simulate_outcome(path_rows, entry_price):
    """
    Given daily OHLC path rows from disclosure date,
    simulate a 10% stop / 10% target strategy.

    Returns: 'win', 'loss', or 'open'
    """
    if not path_rows or not entry_price or entry_price <= 0:
        return "open"

    stop_price   = entry_price * (1 - STOP_PCT   / 100)
    target_price = entry_price * (1 + TARGET_PCT  / 100)

    for day, high, low, close in path_rows:
        if low is None or high is None:
            continue
        if low <= stop_price:
            return "loss"
        if high >= target_price:
            return "win"

    return "open"

# ─────────────────────────────────────────────
# SCORE NORMALISATION HELPERS
# ─────────────────────────────────────────────

def normalise_win_rate(win_rate):
    """
    Convert raw win rate (0-1) to 0-100 score.
    Anchored so that:
      - 70%+ win rate = 100
      - 50% win rate  = 0  (no edge)
      - Below 50%     = 0
    Linear interpolation between 50% and 70%.
    """
    if win_rate <= 0.50:
        return 0.0
    if win_rate >= 0.70:
        return 100.0
    return (win_rate - 0.50) / (0.70 - 0.50) * 100

def normalise_drawdown(avg_drawdown):
    """
    Convert avg max drawdown (negative %) to 0-100 score.
    Anchored so that:
      - 0% drawdown   = 100 (never goes negative)
      - -15% drawdown = 0   (very painful to hold)
    Linear interpolation.
    """
    if avg_drawdown is None:
        return 50.0  # neutral if no data
    if avg_drawdown >= 0:
        return 100.0
    if avg_drawdown <= -15.0:
        return 0.0
    return (avg_drawdown + 15.0) / 15.0 * 100

# ─────────────────────────────────────────────
# SCORE A SINGLE 
# ─────────────────────────────────────────────

def score_politician(politician_id, trades, paths_by_trade):
    """
    Calculate composite score for one politician.

    Args:
        politician_id: politician ID
        trades:        list of (trade_id, price_disc, size_midpoint, max_dd_disc)
        paths_by_trade: dict of trade_id -> list of (day, high, low, close)

    Returns dict with all score components, or None if below threshold.
    """
    # Filter to trades that have path data
    scoreable = [(tid, price, size, dd) for tid, price, size, dd in trades
                 if tid in paths_by_trade and price and price > 0]

    if len(scoreable) < MIN_TRADES:
        return None

    # ── Factor 1: Clean Win Rate (10/10) ─────
    wins = losses = opens = 0
    for trade_id, entry_price, size, dd in scoreable:
        outcome = simulate_outcome(paths_by_trade[trade_id], entry_price)
        if outcome == "win":
            wins += 1
        elif outcome == "loss":
            losses += 1
        else:
            opens += 1

    total_decided = wins + losses
    if total_decided == 0:
        return None

    win_rate       = wins / (wins + losses + opens)
    win_rate_score = normalise_win_rate(win_rate)

    # ── Factor 2: Drawdown Profile ───────────
    # Only average drawdown on winning trades (dd > -10 means didn't hit stop)
    # Exclude dd = 0 (went straight up, no drawdown experienced)
    drawdowns = [dd for _, _, _, dd in scoreable 
                if dd is not None and dd > -10 and dd < 0]
    avg_dd    = sum(drawdowns) / len(drawdowns) if drawdowns else None
    dd_score  = normalise_drawdown(avg_dd)

    # ── Factor 3: Large Trade Accuracy ───────
    large_trades = [(tid, price, size, dd) for tid, price, size, dd in scoreable
                    if size and size >= LARGE_TRADE]

    if len(large_trades) >= MIN_LARGE:
        l_wins = l_losses = l_opens = 0
        for trade_id, entry_price, size, dd in large_trades:
            outcome = simulate_outcome(paths_by_trade[trade_id], entry_price)
            if outcome == "win":
                l_wins += 1
            elif outcome == "loss":
                l_losses += 1
            else:
                l_opens += 1
        l_total   = l_wins + l_losses + l_opens
        l_win_rate = l_wins / l_total if l_total > 0 else 0
        large_score = normalise_win_rate(l_win_rate)
        large_weight = W_LARGE
        win_weight   = W_WIN_RATE
    else:
        # Not enough large trades — redistribute weight to win rate
        large_score  = None
        large_weight = 0
        win_weight   = W_WIN_RATE + W_LARGE

    # ── Composite Score ───────────────────────
    if large_score is not None:
        composite = (win_rate_score * win_weight +
                     dd_score       * W_DRAWDOWN  +
                     large_score    * large_weight)
    else:
        composite = (win_rate_score * win_weight +
                     dd_score       * W_DRAWDOWN)

    return {
        "score":             round(composite, 2),
        "win_rate":          round(win_rate * 100, 2),
        "win_rate_score":    round(win_rate_score, 2),
        "avg_drawdown":      round(avg_dd, 4) if avg_dd is not None else None,
        "drawdown_score":    round(dd_score, 2),
        "large_win_rate":    round(l_win_rate * 100, 2) if large_score is not None else None,
        "large_score":       round(large_score, 2) if large_score is not None else None,
        "total_trades":      len(scoreable),
        "wins":              wins,
        "losses":            losses,
        "opens":             opens,
        "large_trade_count": len(large_trades),
        "low_confidence": len(scoreable) < 10
    }

# ─────────────────────────────────────────────
# FETCH TRADES HELPER
# ─────────────────────────────────────────────

def fetch_trades_by_pol(cursor, asset_filter_sql):
    """
    Fetch compliant buy trades filtered by asset_type.
    asset_filter_sql: a SQL fragment for the WHERE clause, e.g.
        "AND asset_type = 'stock'"
        "AND asset_type IN ('etf', 'fund')"
    Returns dict of pol_id -> [(trade_id, price, size, dd), ...]
    """
    cursor.execute(f"""
        SELECT MIN(trade_id) as trade_id,
               politician_id,
               price_at_disclosure_date,
               MAX(size_midpoint) as size_midpoint,
               AVG(max_drawdown_disc) as max_drawdown_disc
        FROM trades
        WHERE transaction_type = 'buy'
        AND filing_violation = 'compliant'
        AND price_at_disclosure_date IS NOT NULL
        AND disclosure_date IS NOT NULL
        AND disclosure_date != ''
        AND trade_date IS NOT NULL
        AND trade_date != ''
        AND (price_fetch_failed IS NULL OR price_fetch_failed = 0)
        {asset_filter_sql}
        AND (politician_id, trade_date) NOT IN (
            SELECT politician_id, trade_date
            FROM trades
            WHERE transaction_type = 'buy'
            AND filing_violation = 'compliant'
            AND trade_date IS NOT NULL AND trade_date != ''
            {asset_filter_sql}
            GROUP BY politician_id, trade_date
            HAVING COUNT(DISTINCT ticker) >= {CLUSTER_THRESHOLD}
        )
        AND (politician_id, disclosure_date) NOT IN (
            SELECT politician_id, disclosure_date
            FROM trades
            WHERE transaction_type = 'buy'
            AND filing_violation = 'compliant'
            AND disclosure_date IS NOT NULL AND disclosure_date != ''
            {asset_filter_sql}
            GROUP BY politician_id, disclosure_date
            HAVING COUNT(DISTINCT ticker) >= {DISCLOSURE_CLUSTER_THRESHOLD}
        )
        GROUP BY politician_id, ticker, disclosure_date, price_at_disclosure_date
    """)
    trades_by_pol = {}
    for trade_id, pol_id, price, size, dd in cursor.fetchall():
        if pol_id not in trades_by_pol:
            trades_by_pol[pol_id] = []
        trades_by_pol[pol_id].append((trade_id, price, size, dd))
    return trades_by_pol


# ─────────────────────────────────────────────
# MAIN RUNNER
# ─────────────────────────────────────────────

def run_scorer():
    from collections import defaultdict

    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print("=" * 60)
    print("  Politician Scorer")
    print(f"  Strategy: {STOP_PCT}% stop / {TARGET_PCT}% target")
    print(f"  Weights: Win {W_WIN_RATE*100:.0f}% | DD {W_DRAWDOWN*100:.0f}% | Large {W_LARGE*100:.0f}%")
    print(f"  Minimum trades: {MIN_TRADES}")
    print(f"  Filters: trade_date required | trade_date cluster >= {CLUSTER_THRESHOLD} excluded | disclosure cluster >= {DISCLOSURE_CLUSTER_THRESHOLD} excluded")
    print("=" * 60)

    # ── Fetch all politicians ─────────────────
    cursor.execute("SELECT politician_id, name, party, chamber FROM politicians ORDER BY name")
    politicians = cursor.fetchall()
    print(f"\nPoliticians to score: {len(politicians):,}")

    # ── Fetch trades — two passes ─────────────
    print("Fetching stock trades...")
    stock_trades_by_pol = fetch_trades_by_pol(cursor, "AND asset_type = 'stock'")
    stock_count = sum(len(v) for v in stock_trades_by_pol.values())
    print(f"  Stock trades: {stock_count:,}")

    print("Fetching ETF/fund trades...")
    etf_trades_by_pol = fetch_trades_by_pol(cursor, "AND asset_type IN ('etf', 'fund')")
    etf_count = sum(len(v) for v in etf_trades_by_pol.values())
    print(f"  ETF/fund trades: {etf_count:,}")

    # ── Fetch all disc path rows in bulk ──────
    print("Fetching price paths...")
    cursor.execute("""
        SELECT trade_id, day, high, low, close
        FROM trade_price_paths
        WHERE anchor = 'disc'
        ORDER BY trade_id, day ASC
    """)
    paths_by_trade = defaultdict(list)
    for trade_id, day, high, low, close in cursor.fetchall():
        paths_by_trade[trade_id].append((day, high, low, close))
    print(f"Path rows loaded: {len(paths_by_trade):,}")

    # ── Score each politician ─────────────────
    print("\nScoring politicians...\n")

    scored        = []
    below_min     = 0
    no_trades     = 0

    for pol_id, name, party, chamber in politicians:
        stock_trades = stock_trades_by_pol.get(pol_id, [])
        etf_trades   = etf_trades_by_pol.get(pol_id, [])

        if not stock_trades and not etf_trades:
            no_trades += 1
            continue

        stock_result = score_politician(pol_id, stock_trades, paths_by_trade)
        etf_result   = score_politician(pol_id, etf_trades,   paths_by_trade)

        if stock_result is None and etf_result is None:
            below_min += 1
            continue

        scored.append((pol_id, name, party, chamber, stock_result, etf_result))

    # ── Sort by stock score (primary), ETF score (secondary) ──
    def sort_key(x):
        s = x[4]["score"] if x[4] else 0
        e = x[5]["score"] if x[5] else 0
        return (s, e)

    scored.sort(key=sort_key, reverse=True)

    # ── Write scores to database ──────────────
    print("Writing scores to database...")

    # Zero out all existing scores first — politicians who no longer qualify
    # (e.g. filtered out by trade_date or cluster rules) must not retain stale scores.
    cursor.execute("""
        UPDATE politicians SET
            score = NULL, win_rate_trade = NULL, total_trades = NULL,
            score_etf = NULL, win_rate_etf = NULL, etf_trade_count = NULL
    """)

    for pol_id, name, party, chamber, sr, er in scored:
        cursor.execute("""
            UPDATE politicians SET
                score           = ?,
                win_rate_trade  = ?,
                total_trades    = ?,
                score_etf       = ?,
                win_rate_etf    = ?,
                etf_trade_count = ?,
                last_updated    = ?
            WHERE politician_id = ?
        """, (
            sr["score"]       if sr else None,
            sr["win_rate"]    if sr else None,
            sr["total_trades"] if sr else None,
            er["score"]       if er else None,
            er["win_rate"]    if er else None,
            er["total_trades"] if er else None,
            datetime.now().strftime("%Y-%m-%d"),
            pol_id,
        ))

    conn.commit()

    # ── Print leaderboard ─────────────────────
    print(f"\n{'Rank':<5} {'Name':<30} {'Party':<12} {'Chamber':<8} "
          f"{'StockScore':>10} {'StockWin%':>9} {'Trades':>7}  "
          f"{'ETFScore':>8} {'ETFWin%':>8} {'ETFTrades':>9}")
    print("-" * 115)

    for rank, (pol_id, name, party, chamber, sr, er) in enumerate(scored[:30], start=1):
        s_score  = f"{sr['score']:.1f}"    if sr else "  -"
        s_win    = f"{sr['win_rate']:.1f}%" if sr else "    -"
        s_trades = f"{sr['total_trades']:,}" if sr else "-"
        s_conf   = "*" if sr and sr['total_trades'] < 10 else " "

        e_score  = f"{er['score']:.1f}"    if er else "  -"
        e_win    = f"{er['win_rate']:.1f}%" if er else "    -"
        e_trades = f"{er['total_trades']:,}" if er else "-"
        e_conf   = "*" if er and er['total_trades'] < 10 else " "

        print(f"  {rank:<4} {name:<30} {party:<12} {chamber:<8} "
              f"{s_score:>9}{s_conf} {s_win:>9} {s_trades:>7}  "
              f"{e_score:>7}{e_conf} {e_win:>8} {e_trades:>9}")

    print("  * Low confidence — fewer than 10 trades")
    print("-" * 115)

    stock_scored = sum(1 for _, _, _, _, sr, _ in scored if sr)
    etf_scored   = sum(1 for _, _, _, _, _, er in scored if er)
    print(f"\n  Politicians with stock score: {stock_scored:,}")
    print(f"  Politicians with ETF score:   {etf_scored:,}")
    print(f"  Below threshold:              {below_min:,} (< {MIN_TRADES} trades in both)")
    print(f"  No trades:                    {no_trades:,}")
    if scored and scored[0][4]:
        print(f"\n  Top stock scorer: {scored[0][1]} — {scored[0][4]['score']:.1f}")
    print("=" * 60)

    conn.close()
    return scored


if __name__ == "__main__":
    run_scorer()