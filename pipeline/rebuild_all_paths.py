# pipeline/rebuild_all_paths.py
#
# Wipes all trade_price_paths and rebuilds entirely from yfinance.
# Also re-fetches price_at_disclosure_date from yfinance history so that
# entry prices and path OHLC are on the same split-adjusted basis.
#
# Rationale: mixed FMP/yfinance sources caused fake day-1 TP/SL hits.
# See issues_and_fixes/issue_001_mixed_price_path_sources.docx
#
# Scope: all compliant buy trades with a price (price_fetch_failed=0).
# Grouped by ticker to minimise yfinance API calls.
#
# Usage:
#   python -m pipeline.rebuild_all_paths             # full rebuild
#   python -m pipeline.rebuild_all_paths --dry-run   # counts only

import sqlite3
import yfinance as yf
import time
import argparse
from datetime import datetime, timedelta, date
from collections import defaultdict
from pathlib import Path

DB_PATH  = Path(__file__).parent.parent / "data" / "trades.db"
DELAY    = 0.4
COMMIT_N = 50
TIMEOUT  = 10


def get_price_on_or_before(hist, target_date_str):
    """
    Return the closing price on target_date, or the nearest prior trading day.
    Returns None if no data available.
    """
    try:
        target = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    except ValueError:
        return None

    available = sorted(d for d in hist.index if d <= target)
    if not available:
        return None

    closest = available[-1]
    return round(float(hist.loc[closest]["Close"]), 4)


def build_path_rows(hist, disc_date_str, entry_price):
    """
    Build (anchor, day, date, open, high, low, close, pct_change) rows
    from yfinance history, starting the day after disc_date.
    """
    try:
        anchor = datetime.strptime(disc_date_str, "%Y-%m-%d").date()
    except ValueError:
        return []

    today = date.today()
    rows  = []

    trading_dates = sorted(d for d in hist.index if anchor < d <= today)

    for day_idx, d in enumerate(trading_dates, start=1):
        row   = hist.loc[d]
        close = float(row["Close"])
        pct   = round((close - entry_price) / entry_price * 100, 4) \
                if entry_price and entry_price > 0 else None
        rows.append((
            "disc",
            day_idx,
            str(d),
            round(float(row["Open"]),  4),
            round(float(row["High"]),  4),
            round(float(row["Low"]),   4),
            round(float(close),        4),
            pct,
        ))
    return rows


def run(dry_run=False, db_path=None, new_only=False):
    db     = db_path if db_path else DB_PATH
    conn   = sqlite3.connect(db)
    cursor = conn.cursor()

    mode_str = "DRY RUN" if dry_run else ("NEW ONLY" if new_only else "FULL REBUILD")
    print("=" * 65)
    print("  Rebuild All Price Paths + Re-fetch Entry Prices (yfinance)")
    print(f"  DB:   {db}")
    print(f"  Mode: {mode_str}")
    print("=" * 65)

    # ── Current state ──────────────────────────────────────────
    cursor.execute("SELECT COUNT(*) FROM trade_price_paths WHERE anchor='disc'")
    existing_rows = cursor.fetchone()[0]
    print(f"\nExisting path rows:   {existing_rows:,}")

    # ── All compliant buys that need paths ─────────────────────
    # new_only: only trades with no existing path (for daily incremental use)
    # Full rebuild / staging: include trades with NULL price_at_disclosure_date
    #   (the script re-fetches prices from yfinance anyway)
    if new_only:
        cursor.execute("""
            SELECT t.trade_id, t.ticker, t.disclosure_date, t.price_at_disclosure_date
            FROM trades t
            LEFT JOIN trade_price_paths p ON p.trade_id = t.trade_id AND p.anchor = 'disc'
            WHERE t.transaction_type = 'buy'
              AND t.filing_violation = 'compliant'
              AND t.ticker IS NOT NULL AND t.ticker != ''
              AND t.disclosure_date IS NOT NULL AND t.disclosure_date != ''
              AND (t.price_fetch_failed IS NULL OR t.price_fetch_failed = 0)
              AND p.trade_id IS NULL
            ORDER BY t.ticker, t.disclosure_date
        """)
    else:
        cursor.execute("""
            SELECT trade_id, ticker, disclosure_date, price_at_disclosure_date
            FROM trades
            WHERE transaction_type = 'buy'
              AND filing_violation = 'compliant'
              AND ticker IS NOT NULL AND ticker != ''
              AND disclosure_date IS NOT NULL AND disclosure_date != ''
              AND (price_fetch_failed IS NULL OR price_fetch_failed = 0)
            ORDER BY ticker, disclosure_date
        """)
    all_trades = cursor.fetchall()
    print(f"Trades to process:    {len(all_trades):,}")

    by_ticker = defaultdict(list)
    for trade_id, ticker, disc, entry in all_trades:
        if ticker and ticker.strip():
            by_ticker[ticker].append((trade_id, disc, entry))

    print(f"Distinct tickers:     {len(by_ticker):,}")

    if dry_run:
        print("\n  DRY RUN — no changes made.")
        conn.close()
        return

    # ── Step 1: Wipe all existing paths (full rebuild only) ────
    if new_only:
        print("\nStep 1: Skipped (new-only mode — existing paths preserved)")
    else:
        print(f"\nStep 1: Wiping {existing_rows:,} existing path rows...")
        cursor.execute("DELETE FROM trade_price_paths")
        conn.commit()
        print("  Done.")

    # ── Step 2: Rebuild ticker by ticker ───────────────────────
    print(f"\nStep 2: Rebuilding paths + re-fetching entry prices "
          f"for {len(by_ticker):,} tickers...")

    inserted_rows     = 0
    price_updated     = 0
    price_not_found   = 0
    failed_tickers    = []
    processed         = 0
    total_tickers     = len(by_ticker)

    for ticker, trades in sorted(by_ticker.items()):
        dates    = [t[1] for t in trades if t[1]]
        if not dates:
            failed_tickers.append((ticker, "no disclosure dates"))
            processed += 1
            continue

        min_date = min(dates)
        start    = str(datetime.strptime(min_date, "%Y-%m-%d").date() - timedelta(days=10))
        end      = str(date.today())

        try:
            stock = yf.Ticker(ticker)
            hist  = stock.history(start=start, end=end, timeout=TIMEOUT)
            if hist.empty:
                raise ValueError("empty history")
            hist.index = hist.index.date
        except Exception as e:
            print(f"  [{ticker:<8}]  FAILED: {e} — skipping {len(trades)} trades")
            failed_tickers.append((ticker, str(e)))
            processed += 1
            time.sleep(DELAY)
            continue

        ticker_rows = 0

        for trade_id, disc, old_entry in trades:
            # Re-fetch entry price from yfinance history
            new_entry = get_price_on_or_before(hist, disc)

            if new_entry is not None and new_entry > 0:
                cursor.execute(
                    "UPDATE trades SET price_at_disclosure_date=? WHERE trade_id=?",
                    (new_entry, trade_id)
                )
                price_updated += 1
                entry_for_path = new_entry
            else:
                # Keep existing price if yfinance has no data for this date
                entry_for_path = old_entry
                price_not_found += 1

            # Build path rows
            path_rows = build_path_rows(hist, disc, entry_for_path)
            if path_rows:
                cursor.executemany("""
                    INSERT INTO trade_price_paths
                        (trade_id, anchor, day, date, open, high, low, close, pct_change)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [(trade_id, *r) for r in path_rows])
                ticker_rows += len(path_rows)

        inserted_rows += ticker_rows
        processed     += 1

        if processed % COMMIT_N == 0:
            conn.commit()

        if processed % 50 == 0 or processed <= 5:
            pct_done = processed / total_tickers * 100
            print(f"  [{processed:4d}/{total_tickers}  {pct_done:4.0f}%]  "
                  f"{ticker:<10}  trades={len(trades)}  rows={ticker_rows}")

        time.sleep(DELAY)

    conn.commit()

    # ── Step 3: Verify ─────────────────────────────────────────
    print("\nStep 3: Verifying...")

    cursor.execute("SELECT COUNT(*) FROM trade_price_paths WHERE anchor='disc'")
    final_rows = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(DISTINCT trade_id) FROM trade_price_paths WHERE anchor='disc'")
    trades_with_paths = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*) FROM trades t
        JOIN trade_price_paths p ON p.trade_id=t.trade_id AND p.anchor='disc' AND p.day=1
        WHERE t.price_at_disclosure_date IS NOT NULL AND t.price_at_disclosure_date > 0
          AND (p.high >= t.price_at_disclosure_date * 1.10
               OR p.low  <= t.price_at_disclosure_date * 0.90)
    """)
    day1_hits = cursor.fetchone()[0]

    conn.close()

    print("\n" + "=" * 65)
    print("  Complete!")
    print(f"  Path rows inserted:       {inserted_rows:,}")
    print(f"  Final path rows:          {final_rows:,}")
    print(f"  Trades with paths:        {trades_with_paths:,}")
    print(f"  Entry prices updated:     {price_updated:,}")
    print(f"  Entry prices not found:   {price_not_found:,}  (kept existing)")
    print(f"  Tickers failed:           {len(failed_tickers)}")
    if failed_tickers:
        for t, reason in failed_tickers[:20]:
            print(f"    {t:<10}  {reason}")
    print(f"  Day-1 TP/SL hits:         {day1_hits}  (should be ~0)")
    print()
    print("Next steps:")
    print("  python -m pipeline.drawdown_calculator")
    print("  python -m pipeline.scorer")
    print("  python qa/system_qa.py")
    print("=" * 65)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",  action="store_true",
                        help="Count trades to process, no writes")
    parser.add_argument("--new-only", action="store_true",
                        help="Only build paths for trades with no existing path (no wipe)")
    parser.add_argument("--db", default=None,
                        help="DB path override (default: data/trades.db)")
    args = parser.parse_args()
    run(dry_run=args.dry_run, db_path=args.db, new_only=args.new_only)
