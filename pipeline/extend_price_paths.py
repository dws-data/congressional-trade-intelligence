# pipeline/extend_price_paths.py
#
# Two functions for keeping trade_price_paths up to date in the daily runner:
#
#   build_new_paths(db_path)
#       For trades with NO path rows yet. Fetches full yfinance OHLC history
#       from disclosure_date to today. Also re-fetches price_at_disclosure_date
#       so entry price and path data are on the same split-adjusted basis.
#       Grouped by ticker to minimise yfinance calls.
#
#   extend_open_paths(db_path)
#       For open trades (days_to_exit_disc IS NULL) that already have path rows
#       but whose last stored date is before today. Appends new OHLC rows from
#       the day after the last stored date up to today.
#       Grouped by ticker to minimise yfinance calls.
#
# Both are called by the daily runner. rebuild_all_paths.py is the
# recovery/full-wipe tool and is NOT part of the daily run.
#
# Usage:
#   python -m pipeline.extend_price_paths
#   python -m pipeline.extend_price_paths --db data/fmp_staging.db

import sqlite3
import yfinance as yf
import time
import argparse
from datetime import datetime, timedelta, date
from collections import defaultdict
from pathlib import Path

DB_PATH    = Path(__file__).parent.parent / "data" / "trades.db"
DELAY      = 0.4
COMMIT_N   = 50
TIMEOUT    = 10


def _fetch_history(ticker, start_date_str, end_date_str):
    """Fetch yfinance OHLC history for ticker between two date strings. Returns hist or None."""
    try:
        stock = yf.Ticker(ticker)
        hist  = stock.history(start=start_date_str, end=end_date_str, timeout=TIMEOUT)
        if hist.empty:
            return None
        hist.index = hist.index.date
        return hist
    except Exception:
        return None


def _get_price_on_or_before(hist, target_date_str):
    """Return closing price on target_date, or nearest prior trading day."""
    try:
        target = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    available = sorted(d for d in hist.index if d <= target)
    if not available:
        return None
    return round(float(hist.loc[available[-1]]["Close"]), 4)


# ─────────────────────────────────────────────────────────────────
# FUNCTION 1: Build paths for new trades
# ─────────────────────────────────────────────────────────────────

def build_new_paths(db_path=None):
    """
    Fetch full OHLC paths for compliant buy trades that have no path rows yet.

    Price model:
      - price_at_disclosure_date = day-0 close (yfinance close ON disclosure_date)
      - Entry price               = day-1 open  (MOO entry the next trading day)
      - Path rows                 = days 1, 2, 3 ... (trading days after disclosure_date)

    All prices come exclusively from yfinance. If yfinance has no data for a
    trade's disclosure_date, the trade is marked price_fetch_failed=1 and skipped —
    no FMP or other source is used as a fallback.

    Grouped by ticker to minimise yfinance API calls.
    """
    db   = str(db_path if db_path else DB_PATH)
    conn = sqlite3.connect(db)
    c    = conn.cursor()
    today = str(date.today())

    print("=" * 60)
    print("  Build New Paths")
    print(f"  DB: {db}")
    print("=" * 60)

    c.execute("""
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
    rows = c.fetchall()
    print(f"Trades with no path: {len(rows):,}")

    if not rows:
        print("Nothing to do.")
        conn.close()
        return 0

    by_ticker = defaultdict(list)
    for trade_id, ticker, disc, entry in rows:
        by_ticker[ticker].append((trade_id, disc, entry))

    print(f"Distinct tickers:    {len(by_ticker):,}\n")

    inserted   = 0
    price_upd  = 0
    failed     = []
    processed  = 0
    total      = len(by_ticker)

    for ticker, trades in sorted(by_ticker.items()):
        dates    = [t[1] for t in trades if t[1]]
        min_date = min(dates)
        start    = str(datetime.strptime(min_date, "%Y-%m-%d").date() - timedelta(days=10))

        hist = _fetch_history(ticker, start, today)
        if hist is None:
            failed.append(ticker)
            processed += 1
            time.sleep(DELAY)
            continue

        ticker_rows = 0
        for trade_id, disc, old_entry in trades:
            # day-0 close from yfinance — the only allowed price source
            new_entry = _get_price_on_or_before(hist, disc)
            if new_entry and new_entry > 0:
                c.execute("UPDATE trades SET price_at_disclosure_date=?, price_fetch_failed=0 WHERE trade_id=?",
                          (new_entry, trade_id))
                price_upd      += 1
                entry_for_path  = new_entry
            else:
                # No yfinance data — mark failed, skip path building
                c.execute("UPDATE trades SET price_fetch_failed=1 WHERE trade_id=?",
                          (trade_id,))
                continue

            try:
                anchor = datetime.strptime(disc, "%Y-%m-%d").date()
            except ValueError:
                continue

            path_rows = []
            for day_idx, d in enumerate(sorted(d for d in hist.index if anchor < d), start=1):
                row   = hist.loc[d]
                close = float(row["Close"])
                pct   = round((close - entry_for_path) / entry_for_path * 100, 4) \
                        if entry_for_path and entry_for_path > 0 else None
                path_rows.append((
                    trade_id, "disc", day_idx, str(d),
                    round(float(row["Open"]),  4),
                    round(float(row["High"]),  4),
                    round(float(row["Low"]),   4),
                    round(float(close),        4),
                    pct,
                ))

            if path_rows:
                c.executemany("""
                    INSERT OR IGNORE INTO trade_price_paths
                        (trade_id, anchor, day, date, open, high, low, close, pct_change)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, path_rows)
                ticker_rows += len(path_rows)

        inserted  += ticker_rows
        processed += 1
        if processed % COMMIT_N == 0:
            conn.commit()
        if processed % 50 == 0 or processed <= 5:
            print(f"  [{processed:4d}/{total}]  {ticker:<10}  trades={len(trades)}  rows={ticker_rows}")

        time.sleep(DELAY)

    conn.commit()
    conn.close()

    print(f"\nBuild complete: {inserted:,} path rows inserted, "
          f"{price_upd:,} entry prices updated, {len(failed)} tickers failed")
    if failed:
        print(f"  Failed: {', '.join(failed[:20])}")
    return inserted


# ─────────────────────────────────────────────────────────────────
# FUNCTION 2: Extend paths for open trades
# ─────────────────────────────────────────────────────────────────

def extend_open_paths(db_path=None):
    """
    Append new OHLC rows to open trades (days_to_exit_disc IS NULL) whose
    last stored path date is before today.
    Grouped by ticker to minimise yfinance API calls.
    """
    db    = str(db_path if db_path else DB_PATH)
    conn  = sqlite3.connect(db)
    c     = conn.cursor()
    today = date.today()

    print("=" * 60)
    print("  Extend Open Paths")
    print(f"  DB: {db}")
    print(f"  Extending to {today}")
    print("=" * 60)

    # Open trades with existing paths that end before today
    c.execute("""
        SELECT p.trade_id, t.ticker, t.price_at_disclosure_date,
               MAX(p.day)  as last_day,
               MAX(p.date) as last_date
        FROM trade_price_paths p
        JOIN trades t ON t.trade_id = p.trade_id
        WHERE p.anchor = 'disc'
          AND t.days_to_exit_disc IS NULL
          AND (t.price_fetch_failed IS NULL OR t.price_fetch_failed = 0)
        GROUP BY p.trade_id
        HAVING MAX(p.date) < ?
    """, (str(today),))
    rows = c.fetchall()
    print(f"Open trades to extend: {len(rows):,}")

    if not rows:
        print("Nothing to do.")
        conn.close()
        return 0

    # Group by ticker
    by_ticker = defaultdict(list)
    for trade_id, ticker, entry_price, last_day, last_date in rows:
        if ticker:
            by_ticker[ticker].append((trade_id, entry_price, last_day, last_date))

    print(f"Distinct tickers:      {len(by_ticker):,}\n")

    appended  = 0
    failed    = []
    processed = 0
    total     = len(by_ticker)

    for ticker, trades in sorted(by_ticker.items()):
        # Only need history from the earliest last_date to today
        earliest_last = min(t[3] for t in trades)
        start = str(datetime.strptime(earliest_last, "%Y-%m-%d").date() + timedelta(days=1))

        hist = _fetch_history(ticker, start, str(today))
        if hist is None:
            failed.append(ticker)
            processed += 1
            time.sleep(DELAY)
            continue

        ticker_rows = 0
        for trade_id, entry_price, last_day, last_date_str in trades:
            try:
                last_date  = datetime.strptime(last_date_str, "%Y-%m-%d").date()
                start_date = last_date + timedelta(days=1)
            except ValueError:
                continue

            new_days = sorted(d for d in hist.index if start_date <= d <= today)
            if not new_days:
                continue

            path_rows = []
            for i, d in enumerate(new_days, start=last_day + 1):
                row   = hist.loc[d]
                close = float(row["Close"])
                pct   = round((close - entry_price) / entry_price * 100, 4) \
                        if entry_price and entry_price > 0 else None
                path_rows.append((
                    trade_id, "disc", i, str(d),
                    round(float(row["Open"]),  4),
                    round(float(row["High"]),  4),
                    round(float(row["Low"]),   4),
                    round(float(close),        4),
                    pct,
                ))

            if path_rows:
                c.executemany("""
                    INSERT OR IGNORE INTO trade_price_paths
                        (trade_id, anchor, day, date, open, high, low, close, pct_change)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, path_rows)
                ticker_rows += len(path_rows)

        appended  += ticker_rows
        processed += 1
        if processed % COMMIT_N == 0:
            conn.commit()
        if processed % 50 == 0 or processed <= 5:
            print(f"  [{processed:4d}/{total}]  {ticker:<10}  rows appended={ticker_rows}")

        time.sleep(DELAY)

    conn.commit()
    conn.close()

    print(f"\nExtend complete: {appended:,} path rows appended, {len(failed)} tickers failed")
    if failed:
        print(f"  Failed: {', '.join(failed[:20])}")
    return appended


# ─────────────────────────────────────────────────────────────────
# MAIN — run both
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None, help="DB path override")
    args = parser.parse_args()

    db = args.db
    build_new_paths(db_path=db)
    print()
    extend_open_paths(db_path=db)
