# pipeline/qa_fmp_staging.py
#
# QA checks for data/fmp_staging.db before merging into trades.db.
# Run after the full staging pipeline has completed.
#
# Sections:
#   1. Row counts and basic coverage
#   2. Date range sanity checks
#   3. Asset type distribution and error rate
#   4. New vs matched politicians
#   5. Cross-check: potential duplicates against trades.db (fuzzy date match)
#   6. Random spot checks (10 trades)
#   7. Pipeline completeness — MUST PASS before merge
#
# Run: python -m pipeline.qa_fmp_staging

import sqlite3
import random
from pathlib import Path

ROOT       = Path(__file__).parent.parent
STAGING_DB = ROOT / "data" / "fmp_staging.db"
MAIN_DB    = ROOT / "data" / "trades.db"


def section(title):
    print()
    print("=" * 65)
    print(f"  {title}")
    print("=" * 65)


def check(label, value, ok_fn, fmt=None):
    status = "OK" if ok_fn(value) else "** WARN"
    display = fmt(value) if fmt else str(value)
    print(f"  {label:<45} {display}  [{status}]")


def run():
    if not STAGING_DB.exists():
        print(f"ERROR: {STAGING_DB} not found. Run create_fmp_staging.py first.")
        return

    stage = sqlite3.connect(STAGING_DB)
    sc    = stage.cursor()

    print("\n" + "=" * 65)
    print("  FMP Staging QA")
    print(f"  DB: {STAGING_DB}")
    print("=" * 65)

    # ── Section 1: Row counts ─────────────────────────────────────
    section("1. Row Counts & Coverage")

    total = sc.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    check("Total trades", total, lambda v: v > 1000, lambda v: f"{v:,}")

    buys = sc.execute("SELECT COUNT(*) FROM trades WHERE transaction_type='buy'").fetchone()[0]
    check("Buy trades", buys, lambda v: v > 500, lambda v: f"{v:,}")

    senate_trades = sc.execute("""
        SELECT COUNT(*) FROM trades t
        JOIN politicians p ON t.politician_id = p.politician_id
        WHERE p.chamber = 'Senate'
    """).fetchone()[0]
    house_trades = sc.execute("""
        SELECT COUNT(*) FROM trades t
        JOIN politicians p ON t.politician_id = p.politician_id
        WHERE p.chamber = 'House'
    """).fetchone()[0]
    print(f"  {'Senate trades':<45} {senate_trades:,}")
    print(f"  {'House trades':<45} {house_trades:,}")

    pols = sc.execute("SELECT COUNT(*) FROM politicians").fetchone()[0]
    pols_with_trades = sc.execute(
        "SELECT COUNT(DISTINCT politician_id) FROM trades"
    ).fetchone()[0]
    check("Politicians with trades", pols_with_trades, lambda v: v > 50,
          lambda v: f"{v:,}")

    # ── Section 2: Date ranges ────────────────────────────────────
    section("2. Date Range Sanity")

    earliest_disc = sc.execute(
        "SELECT MIN(disclosure_date) FROM trades WHERE disclosure_date != ''"
    ).fetchone()[0]
    latest_disc = sc.execute(
        "SELECT MAX(disclosure_date) FROM trades WHERE disclosure_date != ''"
    ).fetchone()[0]
    earliest_trade = sc.execute(
        "SELECT MIN(trade_date) FROM trades WHERE trade_date IS NOT NULL AND trade_date != ''"
    ).fetchone()[0]

    check("Earliest disclosure", earliest_disc,
          lambda v: v is not None and v >= "2021-01-01")
    check("Latest disclosure",   latest_disc,
          lambda v: v is not None and v <= "2026-12-31")
    check("Earliest trade date", earliest_trade,
          lambda v: v is not None and v >= "2018-01-01")

    future = sc.execute(
        "SELECT COUNT(*) FROM trades WHERE disclosure_date > '2026-04-03'"
    ).fetchone()[0]
    check("Trades with future disclosure_date", future, lambda v: v == 0)

    null_disc = sc.execute(
        "SELECT COUNT(*) FROM trades WHERE disclosure_date IS NULL OR disclosure_date = ''"
    ).fetchone()[0]
    check("Trades with NULL/blank disclosure_date", null_disc, lambda v: v == 0)

    # ── Section 3: Asset type distribution ───────────────────────
    section("3. Asset Type Distribution")

    rows = sc.execute("""
        SELECT asset_type, COUNT(*) as cnt
        FROM trades
        GROUP BY asset_type
        ORDER BY cnt DESC
    """).fetchall()

    if not rows or all(r[0] is None for r in rows):
        print("  ** WARNING: asset_type not yet classified. Run asset_type_fetcher first.")
    else:
        for atype, cnt in rows:
            label = str(atype) if atype else "NULL (unclassified)"
            pct = cnt / total * 100 if total else 0
            print(f"  {label:<20} {cnt:>6,}  ({pct:.1f}%)")

        error_count = sc.execute(
            "SELECT COUNT(*) FROM trades WHERE asset_type='error'"
        ).fetchone()[0]
        check("Trades with asset_type='error'", error_count,
              lambda v: v < total * 0.05, lambda v: f"{v:,}")

    # ── Section 4: Politician matching quality ────────────────────
    section("4. Politician Coverage")

    # Synthetic IDs = unmatched (FH_/FS_ prefix)
    synthetic = sc.execute("""
        SELECT COUNT(DISTINCT politician_id) FROM politicians
        WHERE politician_id LIKE 'FH_%' OR politician_id LIKE 'FS_%'
    """).fetchone()[0]
    matched = sc.execute("""
        SELECT COUNT(DISTINCT p.politician_id) FROM politicians p
        JOIN trades t ON p.politician_id = t.politician_id
        WHERE p.politician_id NOT LIKE 'FH_%' AND p.politician_id NOT LIKE 'FS_%'
    """).fetchone()[0]
    unmatched_trades = sc.execute("""
        SELECT COUNT(*) FROM trades t
        JOIN politicians p ON t.politician_id = p.politician_id
        WHERE p.politician_id LIKE 'FH_%' OR p.politician_id LIKE 'FS_%'
    """).fetchone()[0]

    print(f"  {'Matched politicians (existing IDs)':<45} {matched:,}")
    print(f"  {'New (synthetic FH_/FS_ IDs)':<45} {synthetic:,}")
    print(f"  {'Trades with synthetic politicians':<45} {unmatched_trades:,}")

    if synthetic > 0:
        print()
        print("  Synthetic politicians (may need manual review):")
        rows = sc.execute("""
            SELECT p.politician_id, p.name, p.chamber, COUNT(t.trade_id) as cnt
            FROM politicians p
            JOIN trades t ON p.politician_id = t.politician_id
            WHERE p.politician_id LIKE 'FH_%' OR p.politician_id LIKE 'FS_%'
            GROUP BY p.politician_id
            ORDER BY cnt DESC
            LIMIT 20
        """).fetchall()
        for pol_id, name, chamber, cnt in rows:
            print(f"    {pol_id:<30}  {name:<30}  {chamber:<8}  {cnt:3d} trades")

    # ── Section 5: Cross-check vs trades.db ──────────────────────
    section("5. Cross-Check vs trades.db (Potential CT Duplicates)")

    if not MAIN_DB.exists():
        print("  trades.db not found — skipping cross-check.")
    else:
        stage.execute(f"ATTACH DATABASE '{MAIN_DB}' AS main_db")

        # Use same fuzzy date match as merge_fmp_staging.py (±3 days)
        overlap = stage.execute("""
            SELECT COUNT(*) FROM trades s
            JOIN main_db.trades m
              ON s.politician_id    = m.politician_id
             AND s.ticker           = m.ticker
             AND s.transaction_type = m.transaction_type
             AND ABS(julianday(m.trade_date) - julianday(s.trade_date)) <= 3
        """).fetchone()[0]

        check("CT conflicts — fuzzy match (will be skipped at merge)",
              overlap, lambda v: True,  # informational only
              lambda v: f"{v:,}")

        if overlap > 0:
            print()
            print("  Sample overlapping trades (will be skipped at merge):")
            sample = stage.execute("""
                SELECT s.trade_id, s.politician_id, s.ticker, s.trade_date, s.disclosure_date
                FROM trades s
                JOIN main_db.trades m
                  ON s.politician_id    = m.politician_id
                 AND s.ticker           = m.ticker
                 AND s.transaction_type = m.transaction_type
                 AND ABS(julianday(m.trade_date) - julianday(s.trade_date)) <= 3
                LIMIT 5
            """).fetchall()
            for row in sample:
                print(f"    {row}")

        stage.execute("DETACH DATABASE main_db")

    # ── Section 6: Spot checks ────────────────────────────────────
    section("6. Random Spot Checks (10 trades)")

    sample = sc.execute("""
        SELECT t.trade_id, p.name, t.ticker, t.transaction_type,
               t.trade_date, t.disclosure_date, t.filing_lag_days,
               t.size_min, t.size_max, t.asset_type
        FROM trades t
        JOIN politicians p ON t.politician_id = p.politician_id
        WHERE t.transaction_type = 'buy'
          AND t.ticker IS NOT NULL AND t.ticker != ''
          AND t.trade_date IS NOT NULL AND t.trade_date != ''
        ORDER BY RANDOM()
        LIMIT 10
    """).fetchall()

    print()
    print(f"  {'#':<3}  {'Politician':<28}  {'Ticker':<8}  {'Trade Date':<12}  "
          f"{'Disc Date':<12}  {'Lag':>4}  {'Size Range':<20}  {'Asset'}")
    print(f"  {'-'*115}")
    for i, row in enumerate(sample, 1):
        trade_id, name, ticker, tx, trade_date, disc_date, lag, s_min, s_max, atype = row
        size_str = f"${s_min:,}–${s_max:,}" if s_min and s_max else "unknown"
        print(f"  {i:<3}  {name:<28}  {ticker:<8}  {trade_date or '—':<12}  "
              f"{disc_date:<12}  {lag or '—':>4}  {size_str:<20}  {atype or '—'}")

    # ── Section 7: Pipeline completeness ─────────────────────────
    section("7. Pipeline Completeness — MUST PASS before merge")

    compliant_buys = sc.execute("""
        SELECT COUNT(*) FROM trades
        WHERE transaction_type = 'buy' AND filing_violation = 'compliant'
    """).fetchone()[0]
    print(f"  {'Compliant buy trades':<45} {compliant_buys:,}")

    priced = sc.execute("""
        SELECT COUNT(*) FROM trades
        WHERE transaction_type = 'buy'
          AND filing_violation = 'compliant'
          AND price_at_disclosure_date IS NOT NULL
    """).fetchone()[0]
    price_pct = priced / compliant_buys * 100 if compliant_buys else 0
    check("Priced (price_at_disclosure_date set)",
          price_pct, lambda v: v >= 85.0, lambda v: f"{priced:,}  ({v:.1f}%)")

    failed_price = sc.execute("""
        SELECT COUNT(*) FROM trades
        WHERE price_fetch_failed = 1
    """).fetchone()[0]
    fail_pct = failed_price / compliant_buys * 100 if compliant_buys else 0
    check("Price fetch failed",
          fail_pct, lambda v: v < 15.0, lambda v: f"{failed_price:,}  ({v:.1f}%)")

    with_paths = sc.execute("""
        SELECT COUNT(DISTINCT trade_id) FROM trade_price_paths WHERE anchor = 'disc'
    """).fetchone()[0]
    path_pct = with_paths / compliant_buys * 100 if compliant_buys else 0
    check("Trades with price paths",
          path_pct, lambda v: v >= 85.0, lambda v: f"{with_paths:,}  ({v:.1f}%)")

    resolved = sc.execute("""
        SELECT COUNT(*) FROM trades
        WHERE transaction_type = 'buy'
          AND filing_violation = 'compliant'
          AND max_drawdown_disc IS NOT NULL
    """).fetchone()[0]
    resolved_pct = resolved / compliant_buys * 100 if compliant_buys else 0
    check("Resolved outcomes (max_drawdown_disc set)  <- MERGE GATE",
          resolved_pct, lambda v: v >= 85.0, lambda v: f"{resolved:,}  ({v:.1f}%)")

    unresolved = compliant_buys - resolved
    print(f"  {'Unresolved (will NOT be merged)':<45} {unresolved:,}")

    filing_v_set = sc.execute("""
        SELECT COUNT(*) FROM trades WHERE filing_violation IS NULL
    """).fetchone()[0]
    check("Trades with NULL filing_violation",
          filing_v_set, lambda v: v == 0, lambda v: f"{v:,}")

    # Note: repeat_before_close is intentionally NOT checked here.
    # It must be run on trades.db AFTER merge so it sees the full CT+FMP
    # combined dataset. Running it on staging only captures FMP-to-FMP
    # repeats and misses CT→FMP cross-source pairs within the 180-day window.

    null_at = sc.execute(
        "SELECT COUNT(*) FROM trades WHERE asset_type IS NULL"
    ).fetchone()[0]

    # Hard stop flags
    print()
    blockers = []
    if path_pct < 85.0:
        blockers.append("Price paths incomplete — run: python -m pipeline.extend_price_paths --db data/fmp_staging.db")
    if resolved_pct < 85.0:
        blockers.append("Drawdown outcomes incomplete — run: python -m pipeline.drawdown_calculator --db data/fmp_staging.db")
    if null_at > 0:
        blockers.append(f"asset_type NULL on {null_at:,} trades — run: python -m pipeline.asset_type_fetcher --db data/fmp_staging.db")
    if filing_v_set > 0:
        blockers.append(f"filing_violation NULL on {filing_v_set:,} trades — investigate")

    if blockers:
        print("  ** MERGE BLOCKED — fix the following first:")
        for b in blockers:
            print(f"     - {b}")
    else:
        print("  All pipeline checks passed — OK to proceed to merge.")

    # ── Summary ───────────────────────────────────────────────────
    section("Summary")

    print(f"  Total FMP trades in staging:  {total:,}")
    print(f"  Compliant buys:               {compliant_buys:,}")
    print(f"  Resolved (merge-eligible):    {resolved:,}")
    print(f"  Unresolved (will skip):       {unresolved:,}")
    print(f"  CT conflicts (fuzzy):         {overlap if MAIN_DB.exists() else 'N/A'}")
    print(f"  Unclassified asset_type:      {null_at:,}")
    print()
    if blockers:
        print("  ** MERGE BLOCKED — see section 7 above.")
    else:
        print("  Section 7 passed — ready to merge.")
        print("  Next: python -m pipeline.merge_fmp_staging --dry-run")

    stage.close()


if __name__ == "__main__":
    run()
