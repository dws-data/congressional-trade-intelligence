# pipeline/dedup_trades.py
#
# One-time deduplication of the trades table.
#
# Two problems fixed:
#   1. Blank-ticker rows (NOTICKER FMP junk) — deleted entirely
#   2. True duplicates — same (politician_id, ticker, transaction_type,
#      disclosure_date, trade_date) ingested multiple times from multiple
#      FMP XML snapshots or scraper re-runs. Keep the best copy, delete the rest.
#
# "Best copy" priority:
#   1. Has price_at_disclosure_date (priced > unpriced)
#   2. Lower repeat_before_close (0 > 1)
#   3. MIN(trade_id) as tiebreaker
#
# After cleanup: adds UNIQUE index to prevent future duplicates.
# Also cleans up orphaned trade_price_paths rows for deleted trade_ids.
#
# Usage:
#   python pipeline/dedup_trades.py
#   python pipeline/dedup_trades.py --dry-run   # preview counts only

import sqlite3
import argparse

DB_PATH = "data/trades.db"


def run_dedup(dry_run=False):
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print("=" * 60)
    print("  Trade Deduplication")
    print("=" * 60)

    # ── Step 1: Count blank-ticker rows ──────────────────────
    cursor.execute("SELECT COUNT(*) FROM trades WHERE ticker = '' OR ticker IS NULL")
    blank_count = cursor.fetchone()[0]
    print(f"\nBlank-ticker rows:  {blank_count:,}")

    # ── Step 2: Find true duplicate groups ───────────────────
    # Groups where same (pol, ticker, type, disc_date, trade_date) appears > once
    cursor.execute("""
        SELECT politician_id, ticker, transaction_type, disclosure_date,
               COALESCE(trade_date, '') as td,
               COUNT(*) as cnt
        FROM trades
        WHERE ticker != '' AND ticker IS NOT NULL
        GROUP BY politician_id, ticker, transaction_type, disclosure_date,
                 COALESCE(trade_date, '')
        HAVING cnt > 1
    """)
    dup_groups = cursor.fetchall()
    total_excess = sum(r[5] - 1 for r in dup_groups)
    print(f"Duplicate groups:   {len(dup_groups):,}")
    print(f"Excess rows:        {total_excess:,}")
    print(f"Total to delete:    {blank_count + total_excess:,}")

    if dry_run:
        print("\n  DRY RUN — no changes made.")
        conn.close()
        return

    print("\nProceeding with cleanup...")

    # ── Step 3: Delete blank-ticker rows + their paths ───────
    cursor.execute("""
        DELETE FROM trade_price_paths
        WHERE trade_id IN (
            SELECT trade_id FROM trades WHERE ticker = '' OR ticker IS NULL
        )
    """)
    paths_deleted = cursor.rowcount
    cursor.execute("DELETE FROM trades WHERE ticker = '' OR ticker IS NULL")
    blank_deleted = cursor.rowcount
    print(f"  Blank-ticker trades deleted:  {blank_deleted:,}  (paths: {paths_deleted:,})")

    # ── Step 4: Deduplicate — keep best copy per group ───────
    # "Best" = priced first, then lowest repeat flag, then min trade_id
    dup_deleted = 0
    path_rows_deleted = 0

    for pol_id, ticker, tx_type, disc_date, trade_date, cnt in dup_groups:
        # Fetch all trade_ids for this group, ordered by preference
        cursor.execute("""
            SELECT trade_id
            FROM trades
            WHERE politician_id = ?
            AND ticker = ?
            AND transaction_type = ?
            AND disclosure_date = ?
            AND COALESCE(trade_date, '') = ?
            ORDER BY
                CASE WHEN price_at_disclosure_date IS NOT NULL THEN 0 ELSE 1 END ASC,
                COALESCE(repeat_before_close, 0) ASC,
                trade_id ASC
        """, (pol_id, ticker, tx_type, disc_date, trade_date))
        ids = [r[0] for r in cursor.fetchall()]

        if len(ids) < 2:
            continue  # group already cleaned up (e.g. blank-ticker pass removed some rows)

        keep_id   = ids[0]
        delete_ids = ids[1:]

        placeholders = ",".join("?" * len(delete_ids))

        # Delete paths for removed trade_ids
        cursor.execute(
            f"DELETE FROM trade_price_paths WHERE trade_id IN ({placeholders})",
            delete_ids
        )
        path_rows_deleted += cursor.rowcount

        # Delete the duplicate trade rows
        cursor.execute(
            f"DELETE FROM trades WHERE trade_id IN ({placeholders})",
            delete_ids
        )
        dup_deleted += cursor.rowcount

    conn.commit()
    print(f"  Duplicate trades deleted:     {dup_deleted:,}  (paths: {path_rows_deleted:,})")

    # ── Step 5: Add UNIQUE index to prevent future duplicates ─
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_trade
        ON trades(
            politician_id,
            ticker,
            transaction_type,
            disclosure_date,
            COALESCE(trade_date, '')
        )
    """)
    conn.commit()
    print("  UNIQUE index created: idx_unique_trade")

    # ── Summary ───────────────────────────────────────────────
    cursor.execute("SELECT COUNT(*) FROM trades")
    remaining = cursor.fetchone()[0]
    print(f"\n  Trades remaining: {remaining:,}")
    print("\nNext steps:")
    print("  python pipeline/repeat_buy_flagger.py")
    print("  python pipeline/drawdown_calculator.py")
    print("  python pipeline/scorer.py")
    print("=" * 60)

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deduplicate trades table")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview counts without making changes")
    args = parser.parse_args()
    run_dedup(dry_run=args.dry_run)
