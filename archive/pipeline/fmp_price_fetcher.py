# pipeline/fmp_price_fetcher.py
#
# FMP-based replacement for price_fetcher.py + extend_price_paths.py.
# One API call per ticker fetches the full OHLC history, then:
#   1. Writes spot prices to trades table (same columns as price_fetcher.py)
#   2. Writes daily OHLC rows to trade_price_paths (same as extend_price_paths.py)
#
# Advantages over yfinance:
#   - 300 calls/min vs yfinance's unofficial rate limit
#   - Proper date range params — no over-fetching 5y when we need 2y
#   - OHLC paths written in the same pass, no second script needed
#
# Keep price_fetcher.py intact for ongoing daily pipeline (yfinance).
# Use this script for bulk historical backfills only.
#
# Usage:
#   python pipeline/fmp_price_fetcher.py          # all trades missing prices
#   python pipeline/fmp_price_fetcher.py --test   # first 5 tickers only
#   python pipeline/fmp_price_fetcher.py --paths-only  # only missing OHLC paths
#
# After this runs, kick off drawdown + scoring:
#   python runner/daily_runner.py --score-only     # if prices already done
#   python pipeline/drawdown_calculator.py         # then scorer.py

import requests
import sqlite3
import time
import os
import argparse
from datetime import datetime, timedelta, date
from collections import defaultdict
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

API_KEY        = os.environ["FMP_API_KEY"]
DB_PATH        = "data/trades.db"
REQUEST_DELAY  = 0.22          # ~270 calls/min, comfortably under 300 limit
BATCH_SIZE     = 200           # commit every N tickers
WINDOWS        = [7, 30, 60, 90, 120, 150, 180]

BASE_URL = "https://financialmodelingprep.com/stable/historical-price-eod/full"

# ─────────────────────────────────────────────
# FMP OHLC FETCHER
# ─────────────────────────────────────────────

def fetch_ohlc(ticker, from_date, to_date, session):
    """
    Fetch full daily OHLC from FMP for a ticker between from_date and to_date.
    Returns dict: {date_str: {open, high, low, close}} or {} on failure.
    """
    url = (f"{BASE_URL}?symbol={ticker}"
           f"&from={from_date}&to={to_date}&apikey={API_KEY}")
    try:
        resp = session.get(url, timeout=30)
        if resp.status_code in (400, 404):
            return {}
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return {}
        return {
            row["date"]: {
                "open":  row.get("open"),
                "high":  row.get("high"),
                "low":   row.get("low"),
                "close": row.get("adjClose") or row.get("close"),
            }
            for row in data
            if row.get("date")
        }
    except Exception as e:
        print(f"    FMP error ({ticker}): {e}")
        return {}


# ─────────────────────────────────────────────
# DATE HELPERS
# ─────────────────────────────────────────────

def nearest_price(ohlc, target_date_str, direction="forward", max_offset=5):
    """
    Find the closing price nearest to target_date in ohlc dict.
    direction='forward'  → look ahead up to max_offset days
    direction='backward' → look back up to max_offset days
    Tries both if forward finds nothing.
    """
    if not target_date_str or not ohlc:
        return None
    try:
        target = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    except ValueError:
        return None

    # Forward first
    for offset in range(max_offset + 1):
        d = str(target + timedelta(days=offset))
        if d in ohlc:
            return round(float(ohlc[d]["close"]), 4)

    # Then backward
    for offset in range(1, 4):
        d = str(target - timedelta(days=offset))
        if d in ohlc:
            return round(float(ohlc[d]["close"]), 4)

    return None


def price_n_days_after(ohlc, anchor_date_str, n_days):
    if not anchor_date_str:
        return None
    try:
        target = str(datetime.strptime(anchor_date_str, "%Y-%m-%d").date()
                     + timedelta(days=n_days))
    except ValueError:
        return None
    return nearest_price(ohlc, target)


def calc_return(entry, exit_):
    if entry and exit_ and entry > 0:
        return round((exit_ - entry) / entry * 100, 4)
    return None


# ─────────────────────────────────────────────
# OHLC PATH BUILDER
# ─────────────────────────────────────────────

def build_price_path(ohlc, anchor_date_str, entry_price, today_str):
    """
    Build list of (anchor, day, date, open, high, low, close, pct_change)
    rows from day 1 after anchor_date up to today.
    Matches trade_price_paths schema used by drawdown_calculator.
    """
    if not anchor_date_str or not ohlc:
        return []

    try:
        anchor = datetime.strptime(anchor_date_str, "%Y-%m-%d").date()
    except ValueError:
        return []

    today = datetime.strptime(today_str, "%Y-%m-%d").date()

    # All trading dates in ohlc that fall after anchor and up to today
    trading_dates = sorted(
        d for d in ohlc
        if anchor < datetime.strptime(d, "%Y-%m-%d").date() <= today
    )

    rows = []
    for day_idx, date_str in enumerate(trading_dates, start=1):
        row = ohlc[date_str]
        close = row["close"]
        pct = round((close - entry_price) / entry_price * 100, 4) \
              if entry_price and entry_price > 0 and close else None
        rows.append((
            "disc",
            day_idx,
            date_str,
            round(float(row["open"]),  4) if row["open"]  else None,
            round(float(row["high"]),  4) if row["high"]  else None,
            round(float(row["low"]),   4) if row["low"]   else None,
            round(float(close),        4) if close        else None,
            pct,
        ))
    return rows


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def run_fmp_price_fetcher(test_mode=False, paths_only=False, retry_failed=False):
    conn    = sqlite3.connect(DB_PATH)
    cursor  = conn.cursor()
    session = requests.Session()
    today   = str(date.today())

    # ── Which trades need work? ────────────────
    if paths_only:
        # Trades priced by yfinance but missing OHLC paths (price_fetch_failed may be 0 or NULL)
        cursor.execute("""
            SELECT trade_id, ticker, trade_date, disclosure_date
            FROM trades
            WHERE transaction_type = 'buy'
            AND filing_violation = 'compliant'
            AND price_at_disclosure_date IS NOT NULL
            AND ticker != '' AND ticker IS NOT NULL
            AND trade_id NOT IN (
                SELECT DISTINCT trade_id FROM trade_price_paths WHERE anchor = 'disc'
            )
        """)
    elif retry_failed:
        # Trades yfinance couldn't price — try FMP instead
        # Reset price_fetch_failed so successful FMP prices aren't blocked
        cursor.execute("""
            SELECT trade_id, ticker, trade_date, disclosure_date
            FROM trades
            WHERE ticker != '' AND ticker IS NOT NULL
            AND price_at_disclosure_date IS NULL
            AND price_fetch_failed = 1
        """)
    else:
        cursor.execute("""
            SELECT trade_id, ticker, trade_date, disclosure_date
            FROM trades
            WHERE ticker != '' AND ticker IS NOT NULL
            AND price_at_disclosure_date IS NULL
            AND (price_fetch_failed IS NULL OR price_fetch_failed = 0)
        """)

    trades = cursor.fetchall()

    if not trades:
        print("No trades need pricing — nothing to do.")
        conn.close()
        return

    # ── Group by ticker ────────────────────────
    # { ticker: [(trade_id, trade_date, disclosure_date), ...] }
    by_ticker = defaultdict(list)
    for trade_id, ticker, trade_date, disc_date in trades:
        by_ticker[ticker].append((trade_id, trade_date, disc_date))

    tickers = list(by_ticker.keys())
    if test_mode:
        tickers = tickers[:5]

    total_tickers  = len(tickers)
    total_trades   = sum(len(by_ticker[t]) for t in tickers)

    print("=" * 60)
    print("  FMP Price Fetcher")
    print("=" * 60)
    print(f"  Unique tickers:  {total_tickers:,}")
    print(f"  Trades to price: {total_trades:,}")
    mode_label = 'PATHS ONLY' if paths_only else ('RETRY FAILED' if retry_failed else 'SPOT + PATHS')
    print(f"  Mode:            {mode_label}")
    if test_mode:
        print(f"  (TEST — first {len(tickers)} tickers)")
    print()

    succeeded = 0
    failed    = 0
    path_rows_added = 0

    for t_idx, ticker in enumerate(tickers, 1):
        ticker_trades = by_ticker[ticker]

        # Date range: earliest trade_date - 5 days (buffer) to today + 180 days
        all_dates = [td for _, td, _ in ticker_trades if td] + \
                    [dd for _, _, dd in ticker_trades if dd]
        if not all_dates:
            failed += len(ticker_trades)
            continue

        from_date = str(
            datetime.strptime(min(all_dates), "%Y-%m-%d").date() - timedelta(days=5)
        )
        to_date   = str(
            datetime.strptime(max(all_dates), "%Y-%m-%d").date() + timedelta(days=185)
        )
        # Cap to_date at today (no future data)
        if to_date > today:
            to_date = today

        ohlc = fetch_ohlc(ticker, from_date, to_date, session)
        time.sleep(REQUEST_DELAY)

        if not ohlc:
            # Mark all trades for this ticker as failed
            for trade_id, _, _ in ticker_trades:
                cursor.execute(
                    "UPDATE trades SET price_fetch_failed = 1 WHERE trade_id = ?",
                    (trade_id,)
                )
            failed += len(ticker_trades)
            print(f"  [{t_idx:4d}/{total_tickers}] {ticker:<10}  NO DATA — {len(ticker_trades)} trades marked failed")
            if t_idx % BATCH_SIZE == 0:
                conn.commit()
            continue

        ticker_ok   = 0
        ticker_fail = 0

        for trade_id, trade_date, disc_date in ticker_trades:
            price_trade = nearest_price(ohlc, trade_date)
            price_disc  = nearest_price(ohlc, disc_date)

            if not paths_only:
                if price_disc is None:
                    cursor.execute(
                        "UPDATE trades SET price_fetch_failed = 1 WHERE trade_id = ?",
                        (trade_id,)
                    )
                    ticker_fail += 1
                    failed += 1
                    continue

                # ── Forward window prices ───────
                trade_fwd = {w: price_n_days_after(ohlc, trade_date, w) for w in WINDOWS}
                disc_fwd  = {w: price_n_days_after(ohlc, disc_date,  w) for w in WINDOWS}

                trade_rets = {w: calc_return(price_trade, trade_fwd[w]) for w in WINDOWS}
                disc_rets  = {w: calc_return(price_disc,  disc_fwd[w])  for w in WINDOWS}

                pct_before = calc_return(price_trade, price_disc)

                cursor.execute("""
                    UPDATE trades SET
                        price_at_trade_date        = ?,
                        price_at_disclosure_date   = ?,
                        pct_move_before_disclosure = ?,

                        price_trade_7d   = ?, price_trade_30d  = ?, price_trade_60d  = ?,
                        price_trade_90d  = ?, price_trade_120d = ?, price_trade_150d = ?,
                        price_trade_180d = ?,

                        price_disc_7d   = ?, price_disc_30d  = ?, price_disc_60d  = ?,
                        price_disc_90d  = ?, price_disc_120d = ?, price_disc_150d = ?,
                        price_disc_180d = ?,

                        return_trade_7d   = ?, return_trade_30d  = ?, return_trade_60d  = ?,
                        return_trade_90d  = ?, return_trade_120d = ?, return_trade_150d = ?,
                        return_trade_180d = ?,

                        return_disc_7d   = ?, return_disc_30d  = ?, return_disc_60d  = ?,
                        return_disc_90d  = ?, return_disc_120d = ?, return_disc_150d = ?,
                        return_disc_180d = ?

                    WHERE trade_id = ?
                """, (
                    price_trade, price_disc, pct_before,
                    trade_fwd[7],   trade_fwd[30],  trade_fwd[60],
                    trade_fwd[90],  trade_fwd[120], trade_fwd[150], trade_fwd[180],
                    disc_fwd[7],    disc_fwd[30],   disc_fwd[60],
                    disc_fwd[90],   disc_fwd[120],  disc_fwd[150],  disc_fwd[180],
                    trade_rets[7],  trade_rets[30],  trade_rets[60],
                    trade_rets[90], trade_rets[120], trade_rets[150], trade_rets[180],
                    disc_rets[7],   disc_rets[30],   disc_rets[60],
                    disc_rets[90],  disc_rets[120],  disc_rets[150],  disc_rets[180],
                    trade_id,
                ))
                # If this was a yfinance-failed trade, clear the flag
                if retry_failed:
                    cursor.execute(
                        "UPDATE trades SET price_fetch_failed = 0 WHERE trade_id = ?",
                        (trade_id,)
                    )
                ticker_ok += 1
                succeeded += 1

            # ── OHLC path ───────────────────────
            if disc_date and price_disc:
                path_rows = build_price_path(ohlc, disc_date, price_disc, today)
                if path_rows:
                    cursor.executemany("""
                        INSERT OR IGNORE INTO trade_price_paths
                            (trade_id, anchor, day, date, open, high, low, close, pct_change)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, [(trade_id, *row) for row in path_rows])
                    path_rows_added += len(path_rows)

        if t_idx % BATCH_SIZE == 0:
            conn.commit()

        status = f"OK:{ticker_ok} fail:{ticker_fail} paths:{path_rows_added:,}"
        if t_idx % 50 == 0 or t_idx <= 3 or ticker_fail > 0:
            print(f"  [{t_idx:4d}/{total_tickers}] {ticker:<12}  {status}")

    conn.commit()
    conn.close()
    session.close()

    print()
    print("=" * 60)
    print("  FMP Price Fetch Complete")
    print("=" * 60)
    print(f"  Tickers processed:  {total_tickers:,}")
    print(f"  Trades priced:      {succeeded:,}")
    print(f"  Failed (no data):   {failed:,}")
    print(f"  Path rows added:    {path_rows_added:,}")
    print()
    print("Next steps:")
    print("  python pipeline/drawdown_calculator.py")
    print("  python pipeline/scorer.py")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FMP Bulk Price Fetcher")
    parser.add_argument("--test",          action="store_true",
                        help="Process first 5 tickers only")
    parser.add_argument("--paths-only",    action="store_true",
                        help="Build OHLC paths for trades already priced (no spot price fetch)")
    parser.add_argument("--retry-failed",  action="store_true",
                        help="Retry tickers yfinance marked as failed (price_fetch_failed=1)")
    args = parser.parse_args()

    run_fmp_price_fetcher(
        test_mode=args.test,
        paths_only=args.paths_only,
        retry_failed=args.retry_failed,
    )
