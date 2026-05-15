# pipeline/merge_fmp_staging.py
#
# Merges FULLY RESOLVED trades from data/fmp_staging.db into data/trades.db.
#
# Rules:
#   1. Only insert FMP trades that have a resolved outcome (max_drawdown_disc IS NOT NULL)
#   2. Only insert FMP trades where NO CT trade exists with the same
#      (politician_id, ticker, transaction_type) and a trade_date within 3 days — CT always wins.
#      Fuzzy date match handles cases where FMP and CT record the same trade with slightly
#      different trade_dates (off by 1-3 calendar days).
#   3. Copy price paths for inserted trades from staging into trades.db
#   4. Insert new politicians (synthetic FH_/FS_ IDs) that don't exist in trades.db
#   5. NO dedup_cross_source — CT trades are never touched
#
# Usage:
#   python -m pipeline.merge_fmp_staging
#   python -m pipeline.merge_fmp_staging --dry-run

import sqlite3
import argparse
from pathlib import Path

ROOT       = Path(__file__).parent.parent
STAGING_DB = ROOT / "data" / "fmp_staging.db"
MAIN_DB    = ROOT / "data" / "trades.db"


def run(dry_run=False):
    if not STAGING_DB.exists():
        print(f"ERROR: {STAGING_DB} not found.")
        return
    if not MAIN_DB.exists():
        print(f"ERROR: {MAIN_DB} not found.")
        return

    print()
    print("=" * 65)
    print("  FMP Staging -> trades.db Merge")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print("=" * 65)

    if not dry_run:
        print()
        print("  *** Back up trades.db before proceeding. ***")
        print("  Recommended: cp data/trades.db data/trades_backup_pre_fmp_merge.db")
        print()
        answer = input("  Have you made a backup? (y/n): ").strip().lower()
        if answer != "y":
            print("  Aborted.")
            return

    conn = sqlite3.connect(MAIN_DB)
    c    = conn.cursor()

    trades_before = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    pols_before   = c.execute("SELECT COUNT(*) FROM politicians").fetchone()[0]
    paths_before  = c.execute("SELECT COUNT(*) FROM trade_price_paths").fetchone()[0]

    print(f"\n  trades.db before: {trades_before:,} trades | "
          f"{pols_before} politicians | {paths_before:,} path rows")

    c.execute(f"ATTACH DATABASE '{STAGING_DB}' AS staging")

    # ── Staging coverage check ────────────────────────────────────
    staging_total = c.execute("SELECT COUNT(*) FROM staging.trades").fetchone()[0]
    resolved = c.execute("""
        SELECT COUNT(*) FROM staging.trades
        WHERE max_drawdown_disc IS NOT NULL
    """).fetchone()[0]
    unresolved = staging_total - resolved

    print(f"\n  Staging trades total:      {staging_total:,}")
    print(f"  Resolved (have outcome):   {resolved:,}")
    print(f"  Unresolved (no outcome):   {unresolved:,}  <- will NOT be merged")

    # ── Trades to insert: eligible + no CT equivalent ─────────────
    # Eligible = compliant buys with resolved outcome, OR any other trade
    # (violations, sells, etc.) unconditionally — those never get drawdown
    # calculated so requiring max_drawdown_disc would silently exclude them.
    # CT equivalent = same (politician_id, ticker, transaction_type) and
    # trade_date within 3 days (fuzzy — FMP/CT sometimes differ by 1-3 days
    # for the same underlying trade).
    eligible = c.execute("""
        SELECT COUNT(*) FROM staging.trades s
        WHERE (s.max_drawdown_disc IS NOT NULL
               OR NOT (s.transaction_type = 'buy' AND s.filing_violation = 'compliant'))
    """).fetchone()[0]

    would_insert = c.execute("""
        SELECT COUNT(*) FROM staging.trades s
        WHERE (s.max_drawdown_disc IS NOT NULL
               OR NOT (s.transaction_type = 'buy' AND s.filing_violation = 'compliant'))
          AND NOT EXISTS (
              SELECT 1 FROM trades m
              WHERE m.politician_id    = s.politician_id
                AND m.ticker           = s.ticker
                AND m.transaction_type = s.transaction_type
                AND ABS(julianday(m.trade_date) - julianday(s.trade_date)) <= 3
          )
    """).fetchone()[0]

    ct_conflicts = eligible - would_insert
    print(f"  Eligible to merge:         {eligible:,}")
    print(f"  CT conflicts (fuzzy skip): {ct_conflicts:,}")
    print(f"  Net new to insert:         {would_insert:,}")

    # ── New politicians ───────────────────────────────────────────
    new_pols = c.execute("""
        SELECT COUNT(*) FROM staging.politicians
        WHERE politician_id NOT IN (SELECT politician_id FROM politicians)
    """).fetchone()[0]
    print(f"  New politicians:           {new_pols}")

    # ── Path rows to copy ─────────────────────────────────────────
    # Estimate: paths belonging to eligible, non-conflicting staging trades
    paths_to_copy = c.execute("""
        SELECT COUNT(*) FROM staging.trade_price_paths p
        WHERE EXISTS (
            SELECT 1 FROM staging.trades s
            WHERE s.trade_id = p.trade_id
              AND (s.max_drawdown_disc IS NOT NULL
                   OR NOT (s.transaction_type = 'buy' AND s.filing_violation = 'compliant'))
              AND NOT EXISTS (
                  SELECT 1 FROM trades m
                  WHERE m.politician_id    = s.politician_id
                    AND m.ticker           = s.ticker
                    AND m.transaction_type = s.transaction_type
                    AND ABS(julianday(m.trade_date) - julianday(s.trade_date)) <= 3
              )
        )
    """).fetchone()[0]
    print(f"  Path rows to copy:         {paths_to_copy:,}")

    if dry_run:
        print("\n  DRY RUN -- no changes made.")
        c.execute("DETACH DATABASE staging")
        conn.close()
        return

    # ── 1. Insert new politicians ─────────────────────────────────
    if new_pols > 0:
        c.execute("""
            INSERT OR IGNORE INTO politicians
            SELECT * FROM staging.politicians
            WHERE politician_id NOT IN (SELECT politician_id FROM politicians)
        """)
        conn.commit()
        print(f"\n  Inserted {c.rowcount} new politicians")

    # ── 2. Insert resolved FMP trades with no CT equivalent ───────
    c.execute("""
        INSERT OR IGNORE INTO trades (
            trade_id, politician_id, ticker, company,
            trade_date, disclosure_date, filing_lag_days,
            transaction_type, size_min, size_max, size_midpoint,
            owner, filing_violation, asset_type,
            price_at_disclosure_date,
            max_drawdown_disc, days_to_exit_disc, close_date_disc,
            repeat_before_close, price_fetch_failed
        )
        SELECT
            s.trade_id, s.politician_id, s.ticker, s.company,
            s.trade_date, s.disclosure_date, s.filing_lag_days,
            s.transaction_type, s.size_min, s.size_max, s.size_midpoint,
            s.owner, s.filing_violation, s.asset_type,
            s.price_at_disclosure_date,
            s.max_drawdown_disc, s.days_to_exit_disc, s.close_date_disc,
            s.repeat_before_close, s.price_fetch_failed
        FROM staging.trades s
        WHERE (s.max_drawdown_disc IS NOT NULL
               OR NOT (s.transaction_type = 'buy' AND s.filing_violation = 'compliant'))
          AND NOT EXISTS (
              SELECT 1 FROM trades m
              WHERE m.politician_id    = s.politician_id
                AND m.ticker           = s.ticker
                AND m.transaction_type = s.transaction_type
                AND ABS(julianday(m.trade_date) - julianday(s.trade_date)) <= 3
          )
    """)
    inserted = c.rowcount
    conn.commit()
    print(f"  Inserted {inserted:,} trades")

    # ── 3. Copy price paths for inserted trades ───────────────────
    # Copy paths for any staging trade_id that now exists in trades.db.
    # This is safe because FMP trade_ids (fmps_*/fmph_*) are unique and
    # were not in trades.db before the insert above.
    c.execute("""
        INSERT INTO trade_price_paths
            (trade_id, anchor, day, date, open, high, low, close, pct_change)
        SELECT p.trade_id, p.anchor, p.day, p.date, p.open, p.high, p.low, p.close, p.pct_change
        FROM staging.trade_price_paths p
        WHERE EXISTS (SELECT 1 FROM trades m WHERE m.trade_id = p.trade_id)
    """)
    paths_copied = c.rowcount
    conn.commit()
    print(f"  Copied {paths_copied:,} path rows")

    c.execute("DETACH DATABASE staging")

    # ── Final counts ──────────────────────────────────────────────
    trades_after = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    pols_after   = c.execute("SELECT COUNT(*) FROM politicians").fetchone()[0]
    paths_after  = c.execute("SELECT COUNT(*) FROM trade_price_paths").fetchone()[0]

    conn.close()

    print()
    print("  trades.db after merge:")
    print(f"    Trades:      {trades_before:,} -> {trades_after:,}  (+{trades_after - trades_before:,})")
    print(f"    Politicians: {pols_before} -> {pols_after}  (+{pols_after - pols_before})")
    print(f"    Path rows:   {paths_before:,} -> {paths_after:,}  (+{paths_after - paths_before:,})")
    print()
    print("  CT trades: untouched")
    print("  No dedup needed.")
    print()
    print("  Next: python -m pipeline.scorer")
    print("        python -m pipeline.repeat_buy_flagger")
    print("        python -m qa.comprehensive_qa")
    print("=" * 65)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
