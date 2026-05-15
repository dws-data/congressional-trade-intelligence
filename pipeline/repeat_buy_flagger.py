# pipeline/repeat_buy_flagger.py
#
# Flags repeat buys of the same ticker by the same politician where the
# previous scored position in that ticker was still open at disclosure time.
#
# Rule: for each (politician, ticker) pair sorted by disclosure_date,
#   a repeat buy is flagged (repeat_before_close=1) if:
#     - a previous unflagged buy exists in the same (politician, ticker), AND
#     - that previous buy's close_date_disc >= current disclosure_date
#       (i.e. the position hadn't closed yet)
#
# When close_date_disc IS NULL (never hit 10% stop or 10% target), the position
# is treated as expiring at disclosure_date + 180 days — the maximum scoring
# window. Using a far-future sentinel (e.g. "9999-12-31") would incorrectly flag
# any future buy of the same ticker as a repeat, even years later.
#
# Only unflagged (scored) trades are used as "previous" trades — an excluded
# trade does not reset the window for subsequent buys.
#
# Sets repeat_before_close = 0 for all trades first, then flags the repeats.
# Run after price_fetcher and drawdown_calculator (requires close_date_disc).

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta

DB_PATH = "data/trades.db"

WINDOW_DAYS = 180  # maximum scoring window; used to cap open positions


def run_repeat_buy_flagger(db_path=None):
    db     = db_path if db_path else DB_PATH
    conn   = sqlite3.connect(db)
    cursor = conn.cursor()

    print("=" * 60)
    print("  Repeat Buy Flagger")
    print("  Flags within-window repeat buys of same (politician, ticker)")
    print("=" * 60)

    # ── Fetch all qualifying buy trades ──────
    # Apply the same cluster filters as scorer.py so that cluster-excluded
    # trades are not treated as "previous" trades in the sequence.
    # All asset types included (stocks and ETFs both need this flag).
    print("\nFetching trades...")
    cursor.execute("""
        SELECT
            trade_id,
            politician_id,
            ticker,
            disclosure_date,
            close_date_disc
        FROM trades
        WHERE transaction_type = 'buy'
        AND   filing_violation  = 'compliant'
        AND   price_at_disclosure_date IS NOT NULL
        AND   disclosure_date IS NOT NULL AND disclosure_date != ''
        AND   trade_date IS NOT NULL AND trade_date != ''
        AND   (price_fetch_failed IS NULL OR price_fetch_failed = 0)
        AND   (politician_id, trade_date) NOT IN (
            SELECT politician_id, trade_date
            FROM   trades
            WHERE  transaction_type = 'buy'
            AND    filing_violation  = 'compliant'
            AND    trade_date IS NOT NULL AND trade_date != ''
            GROUP BY politician_id, trade_date
            HAVING COUNT(DISTINCT ticker) >= 10
        )
        AND   (politician_id, disclosure_date) NOT IN (
            SELECT politician_id, disclosure_date
            FROM   trades
            WHERE  transaction_type = 'buy'
            AND    filing_violation  = 'compliant'
            AND    disclosure_date IS NOT NULL AND disclosure_date != ''
            GROUP BY politician_id, disclosure_date
            HAVING COUNT(DISTINCT ticker) >= 20
        )
        ORDER BY politician_id, ticker, disclosure_date
    """)
    rows = cursor.fetchall()
    print(f"Trades fetched: {len(rows):,}")

    # ── Group by (politician, ticker) ────────
    groups = defaultdict(list)
    for trade_id, pol_id, ticker, disc_date, close_date in rows:
        groups[(pol_id, ticker)].append({
            "trade_id":   trade_id,
            "disc_date":  disc_date,
            "close_date": close_date,
        })

    # ── Reset all flags to 0 ─────────────────
    cursor.execute("UPDATE trades SET repeat_before_close = 0")

    # ── Sequential pass per (politician, ticker) ──
    flagged = 0
    to_flag = []

    for (pol_id, ticker), group in groups.items():
        # Already sorted by disclosure_date from the query
        prev_close = None  # close_date of last scored (unflagged) trade

        for trade in group:
            disc  = trade["disc_date"]
            close = trade["close_date"]

            if prev_close is None:
                # First trade — always scored
                flag = 0
            elif disc <= prev_close:
                # Disclosed before previous position closed — within window
                flag = 1
            else:
                # After previous position closed — independent signal
                flag = 0

            if flag == 1:
                to_flag.append(trade["trade_id"])
                flagged += 1
                # Excluded trade does NOT update prev_close —
                # subsequent buys are still evaluated against the last scored trade
            else:
                # Update prev_close to this trade's close date.
                # NULL close_date means the position never hit 10% stop or target —
                # cap the window at disclosure_date + 180 days rather than using a
                # far-future sentinel, which would incorrectly flag buys years later.
                if close is not None:
                    prev_close = close
                else:
                    prev_close = str(
                        datetime.strptime(disc, "%Y-%m-%d").date() + timedelta(days=WINDOW_DAYS)
                    )

    # ── Write flags ───────────────────────────
    print(f"Flagging {flagged:,} within-window repeat buys...")
    for trade_id in to_flag:
        cursor.execute(
            "UPDATE trades SET repeat_before_close = 1 WHERE trade_id = ?",
            (trade_id,)
        )

    conn.commit()

    # ── Summary ───────────────────────────────
    cursor.execute("""
        SELECT
            COUNT(*) as total,
            SUM(repeat_before_close) as flagged,
            SUM(CASE WHEN repeat_before_close=0 THEN 1 ELSE 0 END) as clean
        FROM trades
        WHERE transaction_type='buy'
        AND filing_violation='compliant'
        AND price_at_disclosure_date IS NOT NULL
        AND trade_date IS NOT NULL AND trade_date != ''
        AND (price_fetch_failed IS NULL OR price_fetch_failed = 0)
    """)
    total, flag_count, clean_count = cursor.fetchone()

    print(f"\n  Total qualifying buy trades: {total:,}")
    print(f"  Flagged (within-window):     {flag_count:,}  ({flag_count/total*100:.1f}%)")
    print(f"  Clean (independent):         {clean_count:,}  ({clean_count/total*100:.1f}%)")

    # ── Top politicians affected ──────────────
    cursor.execute("""
        SELECT p.name, COUNT(*) as flagged
        FROM trades t
        JOIN politicians p ON p.politician_id = t.politician_id
        WHERE t.repeat_before_close = 1
        AND   t.transaction_type = 'buy'
        AND   t.filing_violation  = 'compliant'
        AND   t.asset_type = 'stock'
        GROUP BY t.politician_id
        ORDER BY flagged DESC
        LIMIT 15
    """)
    print(f"\n  Most affected politicians (stock trades flagged):")
    print(f"  {'Name':<30} {'Flagged':>8}")
    print(f"  {'-'*40}")
    for name, count in cursor.fetchall():
        print(f"  {name:<30} {count:>8,}")

    print("=" * 60)
    conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None,
                        help="DB path override (default: data/trades.db)")
    args = parser.parse_args()
    run_repeat_buy_flagger(db_path=args.db)
