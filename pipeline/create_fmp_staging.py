# pipeline/create_fmp_staging.py
#
# Creates data/fmp_staging.db with the same schema as trades.db.
# Copies politicians, committees, and committee_memberships from trades.db
# so the fmp_fetcher name-matcher has something to work against.
# Leaves trades, trade_price_paths, data_quality_flags empty.
#
# Run: python -m pipeline.create_fmp_staging

import sqlite3
from pathlib import Path

ROOT       = Path(__file__).parent.parent
MAIN_DB    = ROOT / "data" / "trades.db"
STAGING_DB = ROOT / "data" / "fmp_staging.db"

REFERENCE_TABLES = ("politicians", "committees", "committee_memberships")
EMPTY_TABLES     = ("trades", "trade_price_paths", "data_quality_flags",
                    "alerts", "daily_snapshots")


def run():
    if STAGING_DB.exists():
        print(f"WARNING: {STAGING_DB} already exists.")
        answer = input("Overwrite? (y/n): ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return
        STAGING_DB.unlink()
        print(f"  Deleted existing {STAGING_DB.name}")

    print(f"\nCreating: {STAGING_DB}")

    main_conn  = sqlite3.connect(MAIN_DB)
    stage_conn = sqlite3.connect(STAGING_DB)

    try:
        # ── Replicate schema ──────────────────────────────────────
        schema_rows = main_conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
        ).fetchall()
        index_rows = main_conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        ).fetchall()

        SKIP_TABLES = {"sqlite_sequence"}
        for (name, sql) in schema_rows:
            if name in SKIP_TABLES:
                continue
            stage_conn.execute(sql)
        for (sql,) in index_rows:
            stage_conn.execute(sql)

        stage_conn.commit()
        tables_created = len([n for n, _ in schema_rows if n not in SKIP_TABLES])
        print(f"  Schema: {tables_created} tables, {len(index_rows)} indexes")

        # ── Copy reference tables ─────────────────────────────────
        for table in REFERENCE_TABLES:
            rows = main_conn.execute(f"SELECT * FROM {table}").fetchall()
            if rows:
                placeholders = ",".join("?" * len(rows[0]))
                stage_conn.executemany(
                    f"INSERT OR IGNORE INTO {table} VALUES ({placeholders})", rows
                )
            stage_conn.commit()
            count = stage_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  Copied {count:,} rows  ->  {table}")

        # ── Confirm empty tables ──────────────────────────────────
        for table in EMPTY_TABLES:
            count = stage_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table}: {count} rows  (empty)")

        print(f"\nStaging DB ready: {STAGING_DB}")
        print()
        print("Next steps:")
        print("  python scrapers/fmp_fetcher.py --senate  --db data/fmp_staging.db")
        print("  python scrapers/fmp_fetcher.py --house   --db data/fmp_staging.db")
        print("  python scrapers/fmp_fetcher.py --backfill-senate --db data/fmp_staging.db")
        print("  python -m pipeline.asset_type_fetcher    --db data/fmp_staging.db")
        print("  python -m pipeline.qa_fmp_staging")
        print("  python -m pipeline.merge_fmp_staging")

    finally:
        main_conn.close()
        stage_conn.close()


if __name__ == "__main__":
    run()
