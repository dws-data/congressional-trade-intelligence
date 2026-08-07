# pipeline/sector_fetcher.py
# Fetches sector per unique ticker from yfinance
# Stores in trades.sector column
# Then re-runs committee_relevance flagging with proper sector matching
#
# Skip-tracking: tickers that never get industry data back from yfinance
# (delistings, bond CUSIPs, malformed symbols) are recorded in
# sector_fetch_failures (owned exclusively by this module, same pattern as
# trade_date_prices.py's ticker_fetch_failures). A ticker is only
# permanently skipped after MAX_CONSECUTIVE_FAILS consecutive failed runs,
# so a single bad night doesn't blacklist a live ticker. Any success resets
# the counter to 0.
#
# Run: python pipeline/sector_fetcher.py

import sqlite3
import yfinance as yf
import time
from datetime import date
from pathlib import Path
from collections import defaultdict

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────

DB_PATH    = Path(__file__).parent.parent / "data" / "trades.db"
DELAY      = 0.3   # seconds between yfinance calls
BATCH_SIZE = 100   # commit every N updates

# Skip-tracking: mirrors pipeline/trade_date_prices.py's ticker_fetch_failures.
# A ticker with no industry data is retried via yf.Ticker().info every night
# forever unless something remembers it failed before — most of these are
# permanently dead (delistings, bond CUSIPs, malformed symbols) and this was
# the single biggest chunk of nightly runtime (2026-08-05 timing breakdown:
# 7m09s / 28% fetching ~930 such tickers). Same 3-consecutive-day threshold
# and same-day guard as trade_date_prices.py, kept in its own table since
# each module owns its own failure history.
MAX_CONSECUTIVE_FAILS = 3

# ─────────────────────────────────────────────────────────────────
# COMMITTEE -> SECTORS MAPPING
# ─────────────────────────────────────────────────────────────────

from pipeline.committee_config import (
    COMMITTEE_SUBSECTOR_MAP, COMMITTEE_NAMES, get_subsector,
)

# ─────────────────────────────────────────────────────────────────
# SKIP-TRACKING (sector_fetch_failures)
# ─────────────────────────────────────────────────────────────────

def _ensure_failure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sector_fetch_failures (
            ticker              TEXT PRIMARY KEY,
            consecutive_fails   INTEGER NOT NULL DEFAULT 0,
            last_attempt        TEXT,
            permanently_skipped INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()


def _load_skipped_tickers(conn):
    rows = conn.execute(
        "SELECT ticker FROM sector_fetch_failures WHERE permanently_skipped = 1"
    ).fetchall()
    return {r[0] for r in rows}


def _record_failure(conn, ticker, today_str):
    # Same-day guard: a manual re-trigger on the same calendar date shouldn't
    # inflate the streak.
    conn.execute("""
        INSERT INTO sector_fetch_failures (ticker, consecutive_fails, last_attempt, permanently_skipped)
        VALUES (?, 1, ?, 0)
        ON CONFLICT(ticker) DO UPDATE SET
            consecutive_fails   = CASE WHEN last_attempt = excluded.last_attempt
                                       THEN consecutive_fails
                                       ELSE consecutive_fails + 1 END,
            permanently_skipped = CASE WHEN last_attempt = excluded.last_attempt
                                       THEN permanently_skipped
                                       WHEN consecutive_fails + 1 >= ?
                                       THEN 1 ELSE permanently_skipped END,
            last_attempt        = excluded.last_attempt
    """, (ticker, today_str, MAX_CONSECUTIVE_FAILS))


def _record_success(conn, ticker, today_str):
    conn.execute("""
        INSERT INTO sector_fetch_failures (ticker, consecutive_fails, last_attempt, permanently_skipped)
        VALUES (?, 0, ?, 0)
        ON CONFLICT(ticker) DO UPDATE SET
            consecutive_fails   = 0,
            last_attempt        = excluded.last_attempt,
            permanently_skipped = 0
    """, (ticker, today_str))

# ─────────────────────────────────────────────────────────────────
# STEP 1: ENSURE COLUMNS EXIST
# ─────────────────────────────────────────────────────────────────

def ensure_columns(cursor):
    cols = [c[1] for c in cursor.execute("PRAGMA table_info(trades)").fetchall()]
    if "sector" not in cols:
        cursor.execute("ALTER TABLE trades ADD COLUMN sector TEXT")
        print("  Added sector column")
    else:
        print("  sector column already exists")
    if "industry" not in cols:
        cursor.execute("ALTER TABLE trades ADD COLUMN industry TEXT")
        print("  Added industry column")
    else:
        print("  industry column already exists")

# ─────────────────────────────────────────────────────────────────
# STEP 2: FETCH SECTOR + INDUSTRY PER UNIQUE TICKER
# ─────────────────────────────────────────────────────────────────

def fetch_ticker_data(conn, cursor):
    _ensure_failure_table(conn)
    today_str = str(date.today())

    # Fetch tickers missing industry data (includes those with sector but no industry)
    cursor.execute("""
        SELECT DISTINCT ticker FROM trades
        WHERE ticker != ''
        AND ticker IS NOT NULL
        AND industry IS NULL
        AND (price_fetch_failed IS NOT 1 OR price_fetch_failed IS NULL)
        ORDER BY ticker
    """)
    tickers = [r[0] for r in cursor.fetchall()]

    # Drop tickers already confirmed permanently dead (MAX_CONSECUTIVE_FAILS
    # consecutive misses) so we stop burning yf.Ticker().info calls on them
    # every night — see sector_fetch_failures.
    skipped_tickers = _load_skipped_tickers(conn)
    already_skipped = [t for t in tickers if t in skipped_tickers]
    if already_skipped:
        tickers = [t for t in tickers if t not in skipped_tickers]

    print(f"  Tickers to fetch: {len(tickers) + len(already_skipped):,}")
    if already_skipped:
        print(f"  Permanently skipped ({MAX_CONSECUTIVE_FAILS}+ consecutive fails, not retried): "
              f"{len(already_skipped):,}")

    ticker_data = {}
    failed      = 0
    succeeded   = 0
    propagated  = 0

    for i, ticker in enumerate(tickers):
        # Check if industry already known from other rows (new scrape added rows for existing ticker)
        cursor.execute(
            "SELECT sector, industry FROM trades WHERE ticker = ? AND industry IS NOT NULL LIMIT 1",
            (ticker,)
        )
        existing = cursor.fetchone()
        if existing:
            ticker_data[ticker] = (existing[0], existing[1])
            propagated += 1
            continue

        try:
            info     = yf.Ticker(ticker).info
            sector   = info.get("sector",   None)
            industry = info.get("industry", None)
            ticker_data[ticker] = (sector, industry)
            if industry:
                succeeded += 1
                _record_success(conn, ticker, today_str)
            else:
                failed += 1
                _record_failure(conn, ticker, today_str)
        except Exception:
            ticker_data[ticker] = (None, None)
            failed += 1
            _record_failure(conn, ticker, today_str)

        time.sleep(DELAY)

        if (i + 1) % 100 == 0:
            conn.commit()
            print(f"    [{i+1:4d}/{len(tickers)}] OK: {succeeded:,}  Propagated: {propagated:,}  No data: {failed:,}  Last: {ticker}")

    # Write to DB — update sector (if missing) and industry for each ticker
    updated = 0
    for ticker, (sector, industry) in ticker_data.items():
        cursor.execute(
            "UPDATE trades SET sector = COALESCE(sector, ?), industry = ? WHERE ticker = ?",
            (sector, industry, ticker)
        )
        if (updated + 1) % BATCH_SIZE == 0:
            conn.commit()
        updated += 1

    conn.commit()
    print(f"\n  Industry fetched (yfinance): {succeeded:,} | Propagated from existing: {propagated:,} | No data: {failed:,}")
    return ticker_data

# ─────────────────────────────────────────────────────────────────
# STEP 3: RE-FLAG COMMITTEE RELEVANCE WITH SECTOR MATCHING
# ─────────────────────────────────────────────────────────────────

def flag_committee_relevance(conn, cursor):
    print("\n  Re-flagging committee relevance with sub-sector taxonomy...")

    # Clear existing flags
    cursor.execute("UPDATE trades SET committee_relevance = NULL")
    conn.commit()

    # Load all buy trades with industry data (ticker needed for manual overrides)
    cursor.execute("""
        SELECT t.trade_id, t.politician_id, t.disclosure_date, t.ticker, t.industry
        FROM trades t
        WHERE t.transaction_type = 'buy'
        AND t.ticker != ''
        AND t.disclosure_date != ''
        AND t.disclosure_date IS NOT NULL
        AND t.industry IS NOT NULL
    """)
    trades = cursor.fetchall()
    print(f"  Trades with industry data: {len(trades):,}")

    # Load only high-confidence committee memberships
    cursor.execute("""
        SELECT bioguide, thomas_id, start_date, end_date
        FROM committee_memberships
        WHERE confidence = 'high'
    """)
    memberships = cursor.fetchall()

    pol_memberships = defaultdict(list)
    for bioguide, thomas_id, start_date, end_date in memberships:
        pol_memberships[bioguide].append((thomas_id, start_date, end_date))

    flagged   = 0
    processed = 0

    for trade_id, politician_id, disc_date, ticker, industry in trades:
        subsector = get_subsector(ticker, industry)
        if not subsector:
            processed += 1
            continue

        memberships_for_pol = pol_memberships.get(politician_id, [])
        if not memberships_for_pol:
            processed += 1
            continue

        relevant = []
        for thomas_id, start_date, end_date in memberships_for_pol:
            if start_date and disc_date < start_date:
                continue
            if end_date and disc_date > end_date:
                continue
            if subsector in COMMITTEE_SUBSECTOR_MAP.get(thomas_id, []):
                name = COMMITTEE_NAMES.get(thomas_id, thomas_id)
                relevant.append(name)

        if relevant:
            flagged += 1
            unique = list(dict.fromkeys(relevant))
            cursor.execute(
                "UPDATE trades SET committee_relevance = ? WHERE trade_id = ?",
                (" | ".join(unique), trade_id)
            )

        processed += 1
        if processed % BATCH_SIZE == 0:
            conn.commit()
        if processed % 5000 == 0:
            print(f"    Processed {processed:,} / {len(trades):,}  Flagged: {flagged:,}")

    conn.commit()
    total = len(trades)
    print(f"  Flagged {flagged:,} trades with committee relevance ({flagged/total*100:.1f}% of {total:,} with industry data)")

    # Show sample
    cursor.execute("""
        SELECT p.name, t.ticker, t.sector, t.committee_relevance, t.disclosure_date
        FROM trades t
        JOIN politicians p ON t.politician_id = p.politician_id
        WHERE t.committee_relevance IS NOT NULL
        AND t.transaction_type = 'buy'
        ORDER BY t.disclosure_date DESC
        LIMIT 10
    """)
    print("\n  Sample flagged trades:")
    print(f"  {'Politician':<28} {'Ticker':<8} {'Sector':<25} {'Committee':<35} {'Date'}")
    print(f"  {'-'*120}")
    for row in cursor.fetchall():
        print(f"  {row[0]:<28} {row[1]:<8} {str(row[2]):<25} {str(row[3]):<35} {row[4]}")

# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

def run_sector_fetcher():
    print("=" * 60)
    print("  Sector Fetcher & Committee Relevance Fixer")
    print("=" * 60)

    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print("\n  Step 1: Ensuring columns exist...")
    ensure_columns(cursor)
    conn.commit()

    print("\n  Step 2: Fetching sector + industry from yfinance...")
    fetch_ticker_data(conn, cursor)

    print("\n  Step 3: Re-flagging committee relevance...")
    flag_committee_relevance(conn, cursor)

    # Final summary
    cursor.execute("SELECT COUNT(*) FROM trades WHERE industry IS NOT NULL")
    with_sector = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM trades WHERE committee_relevance IS NOT NULL")
    flagged = cursor.fetchone()[0]

    cursor.execute("""
        SELECT committee_relevance, COUNT(*) as cnt
        FROM trades
        WHERE committee_relevance IS NOT NULL
        GROUP BY committee_relevance
        ORDER BY cnt DESC
        LIMIT 10
    """)
    top_flags = cursor.fetchall()

    conn.close()

    print("\n" + "=" * 60)
    print(f"  Complete!")
    print(f"  Trades with sector data:      {with_sector:,}")
    print(f"  Trades flagged as relevant:   {flagged:,}")
    print(f"\n  Top committee flags:")
    for flag, cnt in top_flags:
        print(f"    {cnt:>5}  {flag}")
    print("=" * 60)


if __name__ == "__main__":
    run_sector_fetcher()