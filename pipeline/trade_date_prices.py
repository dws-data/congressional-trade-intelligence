# pipeline/trade_date_prices.py
#
# Fetches price_at_trade_date for trades where it is missing.
# CT trades get this field from the Capitol Trades website.
# FMP trades do not — this script backfills them via yfinance.
#
# Also recomputes pct_move_before_disclosure for any trade that now
# has both price_at_trade_date and price_at_disclosure_date populated:
#
#   pct_move = (price_at_disclosure_date - price_at_trade_date)
#              / price_at_trade_date * 100
#
# Grouped by ticker to minimise yfinance API calls.
# Idempotent — only processes rows where price_at_trade_date IS NULL.
# Does NOT set price_fetch_failed — that flag is owned by build_new_paths
# and controls whether a disc-date path exists. A missing trade-date price
# is not a fatal error for the pipeline; it just leaves pct_move as NULL.
#
# Skip-tracking: tickers that fail to return yfinance history are recorded
# in ticker_fetch_failures (owned exclusively by this module). A ticker is
# only permanently skipped after MAX_CONSECUTIVE_FAILS consecutive failed
# runs — a single bad night (Yahoo backend hiccup) doesn't blacklist a live
# ticker, but a ticker that is genuinely dead (bond CUSIP, crypto pair,
# malformed symbol, real delisting) stops being retried every night after
# a few confirmed misses. Any success resets the counter to 0.
#
# Usage:
#   python -m pipeline.trade_date_prices          # process all missing
#   python -m pipeline.trade_date_prices --dry-run

import sys
import yfinance as yf
import time
import argparse
from datetime import datetime, timedelta, date
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import get_connection

DELAY      = 0.4
COMMIT_N   = 50
TIMEOUT    = 10
MAX_CONSECUTIVE_FAILS = 3   # consecutive failed runs before a ticker is permanently skipped


def _ensure_failure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ticker_fetch_failures (
            ticker              TEXT PRIMARY KEY,
            consecutive_fails   INTEGER NOT NULL DEFAULT 0,
            last_attempt        TEXT,
            permanently_skipped INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()


def _load_skipped_tickers(conn):
    rows = conn.execute(
        "SELECT ticker FROM ticker_fetch_failures WHERE permanently_skipped = 1"
    ).fetchall()
    return {r[0] for r in rows}


def _record_failure(conn, ticker, today_str):
    # Guard against same-day re-runs (manual re-trigger, debugging) double-counting
    # a single day's failure as multiple consecutive ones — only the first failure
    # seen for a given calendar date increments the streak.
    conn.execute("""
        INSERT INTO ticker_fetch_failures (ticker, consecutive_fails, last_attempt, permanently_skipped)
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
        INSERT INTO ticker_fetch_failures (ticker, consecutive_fails, last_attempt, permanently_skipped)
        VALUES (?, 0, ?, 0)
        ON CONFLICT(ticker) DO UPDATE SET
            consecutive_fails   = 0,
            last_attempt        = excluded.last_attempt,
            permanently_skipped = 0
    """, (ticker, today_str))


def _fetch_history(ticker, start_date_str, end_date_str):
    """Fetch yfinance Close history for ticker between two date strings. Returns hist or None."""
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


def run(db_path=None, dry_run=False):
    db   = str(db_path) if db_path else "trades.db"
    conn = get_connection(db_path)
    c    = conn.cursor()
    _ensure_failure_table(conn)
    today_str = str(date.today())

    print("=" * 60)
    print("  Trade Date Prices")
    print(f"  DB: {db}")
    if dry_run:
        print("  DRY RUN — no writes")
    print("=" * 60)

    # Load all trades missing price_at_trade_date
    c.execute("""
        SELECT trade_id, ticker, trade_date, price_at_disclosure_date
        FROM trades
        WHERE trade_date IS NOT NULL AND trade_date != ''
          AND ticker IS NOT NULL AND ticker != ''
          AND price_at_trade_date IS NULL
          AND (price_fetch_failed IS NULL OR price_fetch_failed = 0)
        ORDER BY ticker, trade_date
    """)
    rows = c.fetchall()
    print(f"Trades missing price_at_trade_date: {len(rows):,}")

    if not rows:
        print("Nothing to do.")
        conn.close()
        return 0, 0

    # Group by ticker, then drop tickers already confirmed permanently dead
    # (MAX_CONSECUTIVE_FAILS consecutive misses) so we stop wasting API calls
    # and runtime on them every night.
    by_ticker = defaultdict(list)
    for trade_id, ticker, trade_date, disc_price in rows:
        by_ticker[ticker].append((trade_id, trade_date, disc_price))

    skipped_tickers = _load_skipped_tickers(conn)
    already_skipped = [t for t in by_ticker if t in skipped_tickers]
    for t in already_skipped:
        del by_ticker[t]

    print(f"Distinct tickers: {len(by_ticker) + len(already_skipped):,}")
    if already_skipped:
        print(f"Permanently skipped ({MAX_CONSECUTIVE_FAILS}+ consecutive fails, not retried): "
              f"{len(already_skipped):,}")
    print()

    price_updated = 0
    pct_updated   = 0
    no_data       = []
    processed     = 0
    total         = len(by_ticker)

    for ticker, trades in sorted(by_ticker.items()):
        dates    = [t[1] for t in trades]
        min_date = min(dates)
        max_date = max(dates)

        # Buffer before min_date to catch weekends/holidays
        start = str(datetime.strptime(min_date, "%Y-%m-%d").date() - timedelta(days=10))
        end   = str(datetime.strptime(max_date, "%Y-%m-%d").date() + timedelta(days=5))

        hist = _fetch_history(ticker, start, end)
        if hist is None:
            no_data.append(ticker)
            if not dry_run:
                _record_failure(conn, ticker, today_str)
            processed += 1
            time.sleep(DELAY)
            continue

        if not dry_run:
            _record_success(conn, ticker, today_str)

        for trade_id, trade_date, disc_price in trades:
            trade_price = _get_price_on_or_before(hist, trade_date)
            if not trade_price or trade_price <= 0:
                continue

            if not dry_run:
                c.execute(
                    "UPDATE trades SET price_at_trade_date = ? WHERE trade_id = ?",
                    (trade_price, trade_id)
                )
            price_updated += 1

            # Recompute pct_move and derived columns if disc price is available
            if disc_price and disc_price > 0:
                pct = round((disc_price - trade_price) / trade_price * 100, 4)
                abs_pct = round(abs(pct), 4)
                bucket = (
                    "deep_dip"    if pct < -15 else
                    "dip"         if pct < 0   else
                    "gain"        if pct < 15  else
                    "strong_gain"
                )
                if not dry_run:
                    c.execute(
                        """UPDATE trades SET
                               pct_move_before_disclosure = ?,
                               abs_pct_move_before_disclosure = ?,
                               pct_move_bucket = ?
                           WHERE trade_id = ?""",
                        (pct, abs_pct, bucket, trade_id)
                    )
                pct_updated += 1

        processed += 1
        if not dry_run and processed % COMMIT_N == 0:
            conn.commit()
        if processed % 100 == 0 or processed <= 5:
            print(f"  [{processed:4d}/{total}]  {ticker:<10}  trades={len(trades)}")

        time.sleep(DELAY)

    if not dry_run:
        conn.commit()
    conn.close()

    print(f"\nDone: {price_updated:,} trade-date prices set, "
          f"{pct_updated:,} pct_move_before_disclosure / abs / bucket updated")
    print(f"Tickers with no yfinance data: {len(no_data)}")
    if no_data:
        print(f"  {', '.join(no_data[:30])}")
    return price_updated, pct_updated


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be updated without writing")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
