# pipeline/cluster_count.py
#
# Computes three columns on qualifying buy trades:
#
#   cluster_count     — OTHER politicians who DISCLOSED the same ticker in the
#                       30 calendar days before this trade's disclosure_date.
#                       Point-in-time correct. Captures disclosure-window clustering.
#
#   cluster_count_td  — OTHER politicians who TRADED the same ticker within 30
#                       calendar days of this trade's trade_date AND whose
#                       disclosure_date < this trade's disclosure_date (so the
#                       trade was already known to the market). Point-in-time
#                       correct. Closer to the academic literature definition of
#                       coordinated trading.
#
#   basket_day        — 1 if this trade was on a basket day (≥10 tickers on same
#                       trade_date OR ≥20 tickers on same disclosure_date for this
#                       politician), else 0. Allows ML to include all trades while
#                       learning the basket-day effect directly.
#
# All three columns are set on compliant buy trades with a price. Non-qualifying
# trades are left NULL.
#
# Usage:
#   python -m pipeline.cluster_count
#   python -m pipeline.cluster_count --db data/trades.db

import sqlite3
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

DB_PATH = Path(__file__).parent.parent / "data" / "trades.db"

WINDOW_DAYS = 30  # calendar days for both cluster windows


def run_cluster_count(db_path=None):
    db   = str(db_path) if db_path else str(DB_PATH)
    conn = sqlite3.connect(db)
    c    = conn.cursor()

    print("=" * 60)
    print("  Cluster Count")
    print(f"  Window: {WINDOW_DAYS} calendar days")
    print("=" * 60)

    # ── Basket day sets ───────────────────────────────────────────────────────
    # (politician_id, trade_date) pairs with ≥10 tickers on that trade_date
    print("\nBuilding basket day sets...")
    c.execute("""
        SELECT politician_id, trade_date
        FROM trades
        WHERE transaction_type = 'buy' AND filing_violation = 'compliant'
        AND trade_date IS NOT NULL AND trade_date != ''
        GROUP BY politician_id, trade_date
        HAVING COUNT(DISTINCT ticker) >= 10
    """)
    basket_trade_date_set = set(c.fetchall())

    # (politician_id, disclosure_date) pairs with ≥20 tickers on that disclosure_date
    c.execute("""
        SELECT politician_id, disclosure_date
        FROM trades
        WHERE transaction_type = 'buy' AND filing_violation = 'compliant'
        AND disclosure_date IS NOT NULL AND disclosure_date != ''
        GROUP BY politician_id, disclosure_date
        HAVING COUNT(DISTINCT ticker) >= 20
    """)
    basket_disc_date_set = set(c.fetchall())

    # ── Fetch all compliant priced buy trades (no basket filter — include all) ─
    print("Fetching all compliant priced buy trades...")
    c.execute("""
        SELECT
            t.trade_id,
            t.politician_id,
            t.ticker,
            t.trade_date,
            t.disclosure_date
        FROM trades t
        WHERE t.transaction_type = 'buy'
        AND   t.filing_violation  = 'compliant'
        AND   t.price_at_disclosure_date IS NOT NULL
        AND   t.disclosure_date IS NOT NULL AND t.disclosure_date != ''
        AND   t.trade_date IS NOT NULL AND t.trade_date != ''
        AND   (t.price_fetch_failed IS NULL OR t.price_fetch_failed = 0)
        ORDER BY t.disclosure_date
    """)
    rows = c.fetchall()
    print(f"Trades fetched: {len(rows):,}")

    # ── Build indexes ─────────────────────────────────────────────────────────
    # For cluster_count (disclosure-date window):
    #   ticker -> sorted list of (disclosure_date, politician_id)
    ticker_disc_idx = defaultdict(list)

    # For cluster_count_td (trade-date window, point-in-time via disclosure_date):
    #   ticker -> sorted list of (trade_date, disclosure_date, politician_id)
    ticker_trade_idx = defaultdict(list)

    for trade_id, pol_id, ticker, trade_date, disc_date in rows:
        ticker_disc_idx[ticker].append((disc_date, pol_id))
        ticker_trade_idx[ticker].append((trade_date, disc_date, pol_id))

    for ticker in ticker_disc_idx:
        ticker_disc_idx[ticker].sort()
    for ticker in ticker_trade_idx:
        ticker_trade_idx[ticker].sort()

    # ── Compute features for each trade ───────────────────────────────────────
    print("Computing cluster counts and basket flags...")
    updates = []

    for trade_id, pol_id, ticker, trade_date, disc_date in rows:
        # basket_day flag
        is_basket = (
            1 if (pol_id, trade_date) in basket_trade_date_set
                 or (pol_id, disc_date) in basket_disc_date_set
            else 0
        )

        # cluster_count: other politicians who DISCLOSED this ticker in the
        # 30 days strictly before this disclosure_date
        disc_window_start = (
            datetime.strptime(disc_date, "%Y-%m-%d") - timedelta(days=WINDOW_DAYS)
        ).strftime("%Y-%m-%d")

        cc_disc = sum(
            1
            for d, pid in ticker_disc_idx[ticker]
            if disc_window_start <= d < disc_date and pid != pol_id
        )

        # cluster_count_td: other politicians who TRADED this ticker within
        # 30 days of this trade_date AND had already disclosed it before this
        # trade's disclosure_date (point-in-time correct)
        td_window_start = (
            datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=WINDOW_DAYS)
        ).strftime("%Y-%m-%d")
        td_window_end = (
            datetime.strptime(trade_date, "%Y-%m-%d") + timedelta(days=WINDOW_DAYS)
        ).strftime("%Y-%m-%d")

        cc_td = sum(
            1
            for td, dd, pid in ticker_trade_idx[ticker]
            if td_window_start <= td <= td_window_end
            and dd < disc_date          # already publicly disclosed
            and pid != pol_id
        )

        updates.append((cc_disc, cc_td, is_basket, trade_id))

    # ── Write to DB ───────────────────────────────────────────────────────────
    print(f"Writing {len(updates):,} rows...")
    c.executemany("""
        UPDATE trades
        SET cluster_count = ?, cluster_count_td = ?, basket_day = ?
        WHERE trade_id = ?
    """, updates)
    conn.commit()

    # ── Summary ───────────────────────────────────────────────────────────────
    c.execute("SELECT COUNT(*) FROM trades WHERE cluster_count IS NOT NULL")
    total = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM trades WHERE basket_day = 1")
    basket_total = c.fetchone()[0]

    # Disclosure-date cluster distribution
    c.execute("""
        SELECT cluster_count, COUNT(*) FROM trades
        WHERE cluster_count IS NOT NULL
        GROUP BY cluster_count ORDER BY cluster_count LIMIT 10
    """)
    dist_disc = c.fetchall()

    # Trade-date cluster distribution
    c.execute("""
        SELECT cluster_count_td, COUNT(*) FROM trades
        WHERE cluster_count_td IS NOT NULL
        GROUP BY cluster_count_td ORDER BY cluster_count_td LIMIT 10
    """)
    dist_td = c.fetchall()

    # Win rate by trade-date cluster bucket (resolved trades only)
    c.execute("""
        SELECT
            CASE
                WHEN cluster_count_td = 0 THEN 'solo (0)'
                WHEN cluster_count_td = 1 THEN '1 other'
                WHEN cluster_count_td = 2 THEN '2 others'
                WHEN cluster_count_td >= 3 THEN '3+ others'
            END as bucket,
            COUNT(*) as n,
            ROUND(100.0*SUM(CASE WHEN drawdown_disc_10pct=0 THEN 1 ELSE 0 END)/COUNT(*),1) as wr
        FROM trades
        WHERE cluster_count_td IS NOT NULL
        AND asset_type = 'stock'
        AND close_date_disc IS NOT NULL
        AND drawdown_disc_10pct IS NOT NULL
        GROUP BY bucket ORDER BY MIN(cluster_count_td)
    """)
    wr_td = c.fetchall()

    conn.close()

    print()
    print("=" * 60)
    print(f"  Complete! Trades updated: {total:,}")
    print(f"  Basket day trades:        {basket_total:,}")
    print()
    print(f"  Disclosure-date cluster distribution:")
    print(f"  {'Count':>6}  {'Trades':>8}")
    for v, n in dist_disc:
        print(f"  {v:>6}  {n:>8,}")
    print()
    print(f"  Trade-date cluster distribution:")
    print(f"  {'Count':>6}  {'Trades':>8}")
    for v, n in dist_td:
        print(f"  {v:>6}  {n:>8,}")
    print()
    print(f"  Win rate by trade-date cluster (resolved stock buys):")
    print(f"  {'Bucket':<12} {'N':>6} {'WR%':>6}")
    print(f"  {'-'*26}")
    for bucket, n, wr in wr_td:
        print(f"  {bucket:<12} {n:>6} {wr:>6}")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None, help="DB path override")
    args = parser.parse_args()
    run_cluster_count(db_path=args.db)
