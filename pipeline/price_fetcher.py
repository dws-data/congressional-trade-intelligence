# pipeline/price_fetcher.py
# Fetches historical stock prices for all trades using yfinance
# Populates price_at_trade_date, price_at_disclosure_date,
# and all 7/30/60/90/120/150/180d forward prices + returns

import yfinance as yf
import sqlite3
import time
import pandas as pd
from datetime import datetime, timedelta

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

DB_PATH       = "data/trades.db"
DELAY         = 0.5      # seconds between yfinance calls
BATCH_SIZE    = 50       # commit to DB every N trades
WINDOWS       = [7, 30, 60, 90, 120, 150, 180]  # day windows to track

# ─────────────────────────────────────────────
# PRICE CACHE
# Avoids re-fetching the same ticker multiple times
# ─────────────────────────────────────────────

_price_cache = {}  # { ticker: pd.Series of closing prices }

def get_ticker_history(ticker):
    """
    Fetch full price history for a ticker.
    Uses cache to avoid duplicate API calls.
    Returns a pandas Series of closing prices indexed by date, or None.
    """
    if ticker in _price_cache:
        return _price_cache[ticker]

    try:
        stock = yf.Ticker(ticker)
        hist  = stock.history(period="5y")  # 5 years covers our full dataset

        if hist.empty:
            _price_cache[ticker] = None
            return None

        # Keep only closing prices, index as date strings
        closes = hist["Close"]
        closes.index = pd.to_datetime(closes.index).date
        _price_cache[ticker] = closes
        return closes

    except Exception as e:
        print(f"    yfinance error for {ticker}: {e}")
        _price_cache[ticker] = None
        return None

def get_price_on_date(ticker, target_date_str):
    """
    Get closing price for ticker on or near a target date.
    If market was closed that day, uses the next available trading day.
    Returns float price or None.
    """
    if not ticker or not target_date_str:
        return None

    closes = get_ticker_history(ticker)
    if closes is None:
        return None

    try:
        target = datetime.strptime(target_date_str, "%Y-%m-%d").date()

        # Try exact date first, then look forward up to 5 trading days
        for offset in range(6):
            check_date = target + timedelta(days=offset)
            if check_date in closes.index:
                return round(float(closes[check_date]), 4)

        # If nothing found forward, try looking back up to 3 days
        for offset in range(1, 4):
            check_date = target - timedelta(days=offset)
            if check_date in closes.index:
                return round(float(closes[check_date]), 4)

        return None

    except Exception:
        return None

def get_price_n_days_after(ticker, anchor_date_str, n_days):
    """
    Get closing price n calendar days after anchor_date.
    Returns float or None.
    """
    if not ticker or not anchor_date_str:
        return None
    try:
        anchor = datetime.strptime(anchor_date_str, "%Y-%m-%d").date()
        target = anchor + timedelta(days=n_days)
        return get_price_on_date(ticker, str(target))
    except Exception:
        return None

def calc_return(price_entry, price_exit):
    """Calculate % return between two prices."""
    if price_entry and price_exit and price_entry > 0:
        return round((price_exit - price_entry) / price_entry * 100, 4)
    return None

# ─────────────────────────────────────────────
# MAIN PRICE FETCHER
# ─────────────────────────────────────────────

def run_price_fetcher(limit=None, overwrite=False):
    """
    Fetch prices for all trades in the database.

    Args:
        limit:     process only N trades (for testing)
        overwrite: if True, re-fetch even if prices already exist
    """
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # ── Fetch trades that need pricing ───────
    if overwrite:
        cursor.execute("""
            SELECT trade_id, ticker, trade_date, disclosure_date, transaction_type
            FROM trades
            WHERE ticker != ''
            AND ticker IS NOT NULL
            ORDER BY trade_date DESC
        """)
    else:
        # Only fetch trades missing disclosure price, skip previously failed tickers
        cursor.execute("""
            SELECT trade_id, ticker, trade_date, disclosure_date, transaction_type
            FROM trades
            WHERE ticker != ''
            AND ticker IS NOT NULL
            AND price_at_disclosure_date IS NULL
            AND (price_fetch_failed IS NULL OR price_fetch_failed = 0)
            ORDER BY trade_date DESC
        """)

    all_trades = cursor.fetchall()

    if limit:
        all_trades = all_trades[:limit]

    total      = len(all_trades)
    processed  = 0
    succeeded  = 0
    failed     = 0
    skipped    = 0

    print("=" * 55)
    print("  Price Fetcher")
    print("=" * 55)
    print(f"\nTrades to process: {total:,}")
    if limit:
        print(f"(Test mode: {limit} trades)")
    print()

    for i, (trade_id, ticker, trade_date, disc_date, tx_type) in enumerate(all_trades):

        # ── Fetch anchor prices ───────────────
        price_trade = get_price_on_date(ticker, trade_date)
        price_disc  = get_price_on_date(ticker, disc_date)

        if price_trade is None and price_disc is None:
            failed += 1
            processed += 1
            # Mark ticker as failed so future runs skip it
            cursor.execute("""
                UPDATE trades SET price_fetch_failed = 1
                WHERE ticker = ?
                AND (price_at_trade_date IS NULL OR price_at_disclosure_date IS NULL)
            """, (ticker,))
            if processed % BATCH_SIZE == 0:
                conn.commit()
            if i % 100 == 0 or i < 5:
                print(f"  [{i+1:5d}/{total}] {ticker:<8} {trade_date}  — no price data")
            continue

        # ── Fetch forward prices from trade date ──
        trade_fwd = {}
        for w in WINDOWS:
            trade_fwd[w] = get_price_n_days_after(ticker, trade_date, w)

        # ── Fetch forward prices from disclosure date ──
        disc_fwd = {}
        for w in WINDOWS:
            disc_fwd[w] = get_price_n_days_after(ticker, disc_date, w)

        # ── Calculate returns ─────────────────
        trade_returns = {w: calc_return(price_trade, trade_fwd[w]) for w in WINDOWS}
        disc_returns  = {w: calc_return(price_disc,  disc_fwd[w])  for w in WINDOWS}

        # ── Pct move between trade and disclosure ──
        pct_move_before_disc = calc_return(price_trade, price_disc)

        # ── Update database ───────────────────
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
            price_trade, price_disc, pct_move_before_disc,

            trade_fwd[7],   trade_fwd[30],  trade_fwd[60],
            trade_fwd[90],  trade_fwd[120], trade_fwd[150],
            trade_fwd[180],

            disc_fwd[7],   disc_fwd[30],  disc_fwd[60],
            disc_fwd[90],  disc_fwd[120], disc_fwd[150],
            disc_fwd[180],

            trade_returns[7],   trade_returns[30],  trade_returns[60],
            trade_returns[90],  trade_returns[120], trade_returns[150],
            trade_returns[180],

            disc_returns[7],   disc_returns[30],  disc_returns[60],
            disc_returns[90],  disc_returns[120], disc_returns[150],
            disc_returns[180],

            trade_id,
        ))

        succeeded += 1
        processed += 1

        # Commit every BATCH_SIZE trades
        if processed % BATCH_SIZE == 0:
            conn.commit()

        # Progress report every 100 trades
        if processed % 100 == 0 or processed <= 5:
            pct = processed / total * 100
            print(f"  [{processed:5d}/{total}] {pct:5.1f}%  |  OK: {succeeded:,}  Failed: {failed:,}  |  Last: {ticker}")

        time.sleep(DELAY)

    conn.commit()
    conn.close()

    print("\n" + "=" * 55)
    print(f"  Price fetch complete!")
    print(f"  Succeeded:  {succeeded:,}")
    print(f"  Failed:     {failed:,}  (no price data — private/delisted)")
    print(f"  Skipped:    {skipped:,}  (already had prices)")
    print("=" * 55)


# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    run_price_fetcher()