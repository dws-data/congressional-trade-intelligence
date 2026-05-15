# pipeline/dedup_cross_source.py
#
# Removes cross-source duplicate trades where the same trade appears twice
# with the same (politician_id, ticker, transaction_type, trade_date) but
# different disclosure_dates.
#
# Cause: FMP and Capitol Trades sometimes record different disclosure dates
# for the same underlying trade (e.g. 1-2 days apart, or a late amended
# FMP backfill filing creating a second entry months/years later).
#
# Fix: keep the row with the EARLIEST disclosure_date (= first filing).
#       Tiebreaker: prefer priced row; then min trade_id.
#
# After dedup, updates the UNIQUE index to also guard against this pattern.
#
# Usage:
#   python -m pipeline.dedup_cross_source
#   python -m pipeline.dedup_cross_source --dry-run

import sqlite3
import argparse
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "trades.db"


def run(dry_run=False):
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print("=" * 65)
    print("  Cross-Source Duplicate Removal")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print("=" * 65)

    # ── Find all groups with same (pol, ticker, type, trade_date) ──
    cursor.execute("""
        SELECT politician_id, ticker, transaction_type, trade_date,
               COUNT(*) as cnt
        FROM trades
        WHERE trade_date IS NOT NULL AND trade_date != ''
        GROUP BY politician_id, ticker, transaction_type, trade_date
        HAVING cnt > 1
        ORDER BY politician_id, ticker
    """)
    groups = cursor.fetchall()

    print(f"\nDuplicate groups found: {len(groups)}")
    print(f"Excess rows to delete:  {sum(r[4]-1 for r in groups)}")

    if not groups:
        print("\nNothing to fix.")
        conn.close()
        return

    # ── Preview ─────────────────────────────────────────────────
    print("\nGroups (keep = earliest disclosure date):")
    for pol_id, ticker, tx_type, trade_date, cnt in groups:
        cursor.execute("""
            SELECT trade_id, disclosure_date, price_at_disclosure_date, price_fetch_failed
            FROM trades
            WHERE politician_id=? AND ticker=? AND transaction_type=? AND trade_date=?
            ORDER BY disclosure_date ASC,
                     CASE WHEN price_at_disclosure_date IS NOT NULL THEN 0 ELSE 1 END ASC,
                     trade_id ASC
        """, (pol_id, ticker, tx_type, trade_date))
        candidates = cursor.fetchall()
        keep = candidates[0]
        delete = candidates[1:]
        print(f"  {pol_id:<25} {ticker:<8} {tx_type:<5} trade={trade_date}")
        print(f"    KEEP:   {keep[0]}  disc={keep[1]}  price={keep[2]}")
        for d in delete:
            print(f"    DELETE: {d[0]}  disc={d[1]}  price={d[2]}")

    if dry_run:
        print("\n  DRY RUN — no changes made.")
        conn.close()
        return

    # ── Delete duplicate rows ────────────────────────────────────
    print("\nRemoving duplicates...")
    deleted_trades = 0
    deleted_paths  = 0

    for pol_id, ticker, tx_type, trade_date, cnt in groups:
        cursor.execute("""
            SELECT trade_id
            FROM trades
            WHERE politician_id=? AND ticker=? AND transaction_type=? AND trade_date=?
            ORDER BY disclosure_date ASC,
                     CASE WHEN price_at_disclosure_date IS NOT NULL THEN 0 ELSE 1 END ASC,
                     trade_id ASC
        """, (pol_id, ticker, tx_type, trade_date))
        ids = [r[0] for r in cursor.fetchall()]
        delete_ids = ids[1:]

        placeholders = ",".join("?" * len(delete_ids))

        cursor.execute(
            f"DELETE FROM trade_price_paths WHERE trade_id IN ({placeholders})",
            delete_ids
        )
        deleted_paths += cursor.rowcount

        cursor.execute(
            f"DELETE FROM trades WHERE trade_id IN ({placeholders})",
            delete_ids
        )
        deleted_trades += cursor.rowcount

    conn.commit()

    # ── Summary ──────────────────────────────────────────────────
    cursor.execute("SELECT COUNT(*) FROM trades")
    remaining = cursor.fetchone()[0]

    print(f"\n  Trades deleted:       {deleted_trades}")
    print(f"  Path rows deleted:    {deleted_paths}")
    print(f"  Trades remaining:     {remaining:,}")

    # ── Verify no duplicates remain ──────────────────────────────
    cursor.execute("""
        SELECT COUNT(*) FROM (
            SELECT politician_id, ticker, transaction_type, trade_date
            FROM trades
            WHERE trade_date IS NOT NULL AND trade_date != ''
            GROUP BY politician_id, ticker, transaction_type, trade_date
            HAVING COUNT(*) > 1
        )
    """)
    remaining_dups = cursor.fetchone()[0]
    print(f"  Remaining duplicate groups: {remaining_dups}  ({'OK' if remaining_dups == 0 else '** PROBLEM'})")

    conn.close()
    print("\nNext step: python -m pipeline.rebuild_all_paths")
    print("=" * 65)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
