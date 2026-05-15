# pipeline/market_features.py
#
# Builds market context features for each unique disclosure date in the DB.
# Stores in a `market_features` table keyed on date.
#
# Features computed:
#   vix_close         — VIX level on disclosure date (nearest prior trading day)
#   spy_1m_return     — SPY % return in 30 calendar days before disclosure date
#   xlk_1m_return     — Technology ETF
#   xlv_1m_return     — Healthcare ETF
#   xlf_1m_return     — Financial Services ETF
#   xle_1m_return     — Energy ETF
#   xly_1m_return     — Consumer Cyclical ETF
#   xlp_1m_return     — Consumer Defensive ETF
#   xli_1m_return     — Industrials ETF
#   xlb_1m_return     — Basic Materials ETF
#   xlre_1m_return    — Real Estate ETF (live since 2015-10-07)
#   xlu_1m_return     — Utilities ETF
#   xlc_1m_return     — Communication Services ETF (live since 2018-06-18)
#
# spy_vs_sector (sector ETF return - SPY return) is derived at training time, not stored.
#
# Usage:
#   python -m pipeline.market_features           # fill missing dates only
#   python -m pipeline.market_features --rebuild # wipe and rebuild all

import sqlite3
import argparse
import time
from datetime import date, timedelta, datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

DB_PATH = Path(__file__).parent.parent / "data" / "trades.db"

TICKERS = ["SPY", "^VIX", "XLK", "XLV", "XLF", "XLE", "XLY", "XLP", "XLI", "XLB", "XLRE", "XLU", "XLC"]

SECTOR_TO_ETF = {
    "Technology":             "XLK",
    "Healthcare":             "XLV",
    "Financial Services":     "XLF",
    "Energy":                 "XLE",
    "Consumer Cyclical":      "XLY",
    "Consumer Defensive":     "XLP",
    "Industrials":            "XLI",
    "Basic Materials":        "XLB",
    "Real Estate":            "XLRE",
    "Utilities":              "XLU",
    "Communication Services": "XLC",
}

LOOKBACK_1W   = 7    # calendar days for 1-week return
LOOKBACK_DAYS = 30   # calendar days for 1-month return
LOOKBACK_3M   = 90   # calendar days for 3-month return
MA_WINDOW     = 200  # trading days for 200-day moving average
FETCH_BUFFER  = 350  # extra days to ensure we have enough history for 200MA + 3m lookback


# ─────────────────────────────────────────────────────────────────
# DB SETUP
# ─────────────────────────────────────────────────────────────────

def create_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_features (
            date            TEXT PRIMARY KEY,
            vix_close       REAL,
            spy_1w_ret      REAL,
            spy_1m_ret      REAL,
            spy_3m_ret      REAL,
            spy_above_200ma INTEGER,
            xlk_1m_ret      REAL,
            xlv_1m_ret      REAL,
            xlf_1m_ret      REAL,
            xle_1m_ret      REAL,
            xly_1m_ret      REAL,
            xlp_1m_ret      REAL,
            xli_1m_ret      REAL,
            xlb_1m_ret      REAL,
            xlre_1m_ret     REAL,
            xlu_1m_ret      REAL,
            xlc_1m_ret      REAL
        )
    """)
    conn.commit()


# ─────────────────────────────────────────────────────────────────
# FETCH DATES
# ─────────────────────────────────────────────────────────────────

def get_target_dates(conn, rebuild=False):
    """Distinct disclosure dates from scored buy trades that need market features."""
    c = conn.cursor()
    c.execute("""
        SELECT DISTINCT disclosure_date FROM trades
        WHERE transaction_type = 'buy'
        AND filing_violation = 'compliant'
        AND asset_type = 'stock'
        AND disclosure_date IS NOT NULL AND disclosure_date != ''
        ORDER BY disclosure_date
    """)
    all_dates = [r[0] for r in c.fetchall()]

    if rebuild:
        return all_dates

    # Only dates not yet in market_features
    c.execute("SELECT date FROM market_features")
    existing = {r[0] for r in c.fetchall()}
    missing = [d for d in all_dates if d not in existing]
    return missing


# ─────────────────────────────────────────────────────────────────
# FETCH PRICE DATA
# ─────────────────────────────────────────────────────────────────

def fetch_price_data(start_date: str, end_date: str) -> dict[str, pd.Series]:
    """
    Fetch daily close prices for all tickers.
    Returns dict: ticker -> pd.Series indexed by date string.
    We add 1 day to end_date since yfinance end is exclusive.
    """
    print(f"  Fetching price data: {start_date} to {end_date} ...")
    end_dt = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    raw = yf.download(
        TICKERS,
        start=start_date,
        end=end_dt,
        auto_adjust=True,
        progress=False,
        threads=True,
    )

    prices = {}
    for ticker in TICKERS:
        try:
            col = ("Close", ticker)
            if col in raw.columns:
                s = raw[col].dropna()
            else:
                # Single ticker fallback
                s = raw["Close"].dropna()
            # Convert index to date strings
            s.index = s.index.strftime("%Y-%m-%d")
            prices[ticker] = s
        except Exception as e:
            print(f"  WARNING: could not extract {ticker}: {e}")
            prices[ticker] = pd.Series(dtype=float)

    print(f"  Price data fetched. Rows per ticker example (SPY): {len(prices.get('SPY', []))}")
    return prices


# ─────────────────────────────────────────────────────────────────
# COMPUTE FEATURES FOR ONE DATE
# ─────────────────────────────────────────────────────────────────

def nearest_prior_price(series: pd.Series, target_date: str) -> float | None:
    """Return close price on or before target_date."""
    candidates = series[series.index <= target_date]
    if candidates.empty:
        return None
    return float(candidates.iloc[-1])


def period_return(series: pd.Series, target_date: str, lookback_days: int) -> float | None:
    """
    % return from `lookback_days` calendar days before target_date to target_date.
    Uses nearest prior trading day for both endpoints.
    """
    price_now = nearest_prior_price(series, target_date)
    if price_now is None:
        return None

    prior_date = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    price_then = nearest_prior_price(series, prior_date)
    if price_then is None or price_then == 0:
        return None

    return round((price_now - price_then) / price_then * 100, 4)


def one_month_return(series: pd.Series, target_date: str) -> float | None:
    return period_return(series, target_date, LOOKBACK_DAYS)


def above_200ma(series: pd.Series, target_date: str) -> int | None:
    """
    1 if SPY close on target_date is above its 200-day MA, 0 if below, None if insufficient data.
    200-day MA uses the 200 trading days ending on (or before) target_date.
    """
    candidates = series[series.index <= target_date]
    if len(candidates) < MA_WINDOW:
        return None
    ma = float(candidates.iloc[-MA_WINDOW:].mean())
    price = float(candidates.iloc[-1])
    return 1 if price > ma else 0


def compute_row(disc_date: str, prices: dict) -> dict:
    row = {"date": disc_date}
    spy = prices.get("SPY", pd.Series(dtype=float))

    # VIX close
    row["vix_close"]      = nearest_prior_price(prices.get("^VIX", pd.Series(dtype=float)), disc_date)

    # SPY returns: 1w, 1m, 3m + regime flag
    row["spy_1w_ret"]     = period_return(spy, disc_date, LOOKBACK_1W)
    row["spy_1m_ret"]     = period_return(spy, disc_date, LOOKBACK_DAYS)
    row["spy_3m_ret"]     = period_return(spy, disc_date, LOOKBACK_3M)
    row["spy_above_200ma"] = above_200ma(spy, disc_date)

    # Sector ETF 1m returns
    row["xlk_1m_ret"]  = one_month_return(prices.get("XLK",  pd.Series(dtype=float)), disc_date)
    row["xlv_1m_ret"]  = one_month_return(prices.get("XLV",  pd.Series(dtype=float)), disc_date)
    row["xlf_1m_ret"]  = one_month_return(prices.get("XLF",  pd.Series(dtype=float)), disc_date)
    row["xle_1m_ret"]  = one_month_return(prices.get("XLE",  pd.Series(dtype=float)), disc_date)
    row["xly_1m_ret"]  = one_month_return(prices.get("XLY",  pd.Series(dtype=float)), disc_date)
    row["xlp_1m_ret"]  = one_month_return(prices.get("XLP",  pd.Series(dtype=float)), disc_date)
    row["xli_1m_ret"]  = one_month_return(prices.get("XLI",  pd.Series(dtype=float)), disc_date)
    row["xlb_1m_ret"]  = one_month_return(prices.get("XLB",  pd.Series(dtype=float)), disc_date)
    row["xlre_1m_ret"] = one_month_return(prices.get("XLRE", pd.Series(dtype=float)), disc_date)
    row["xlu_1m_ret"]  = one_month_return(prices.get("XLU",  pd.Series(dtype=float)), disc_date)
    row["xlc_1m_ret"]  = one_month_return(prices.get("XLC",  pd.Series(dtype=float)), disc_date)

    return row


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

def run(rebuild=False):
    print("=" * 60)
    print("  Market Features Builder")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)
    create_table(conn)

    if rebuild:
        print("\n  REBUILD mode — clearing existing market_features...")
        conn.execute("DELETE FROM market_features")
        conn.commit()

    target_dates = get_target_dates(conn, rebuild=rebuild)
    conn.close()  # release lock before network calls
    print(f"\n  Disclosure dates to process: {len(target_dates)}")

    if not target_dates:
        print("  Nothing to do — all dates already populated.")
        return

    # Date range for yfinance fetch
    min_date = (datetime.strptime(target_dates[0],  "%Y-%m-%d") - timedelta(days=FETCH_BUFFER)).strftime("%Y-%m-%d")
    max_date = target_dates[-1]
    print(f"  Fetch range: {min_date} to {max_date}")

    prices = fetch_price_data(min_date, max_date)

    print(f"\n  Computing features for {len(target_dates)} dates...")
    rows = []
    for disc_date in target_dates:
        rows.append(compute_row(disc_date, prices))

    # Reopen connection for write — keeps lock window minimal
    conn = sqlite3.connect(DB_PATH)
    print(f"  Inserting {len(rows)} rows into market_features...")
    conn.executemany("""
        INSERT OR REPLACE INTO market_features
            (date, vix_close, spy_1w_ret, spy_1m_ret, spy_3m_ret, spy_above_200ma,
             xlk_1m_ret, xlv_1m_ret, xlf_1m_ret,
             xle_1m_ret, xly_1m_ret, xlp_1m_ret, xli_1m_ret, xlb_1m_ret,
             xlre_1m_ret, xlu_1m_ret, xlc_1m_ret)
        VALUES
            (:date, :vix_close, :spy_1w_ret, :spy_1m_ret, :spy_3m_ret, :spy_above_200ma,
             :xlk_1m_ret, :xlv_1m_ret, :xlf_1m_ret,
             :xle_1m_ret, :xly_1m_ret, :xlp_1m_ret, :xli_1m_ret, :xlb_1m_ret,
             :xlre_1m_ret, :xlu_1m_ret, :xlc_1m_ret)
    """, rows)
    conn.commit()

    # Summary
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM market_features")
    total = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM market_features WHERE vix_close IS NULL")
    null_vix = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM market_features WHERE spy_1m_ret IS NULL")
    null_spy = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM market_features WHERE spy_above_200ma IS NULL")
    null_regime = c.fetchone()[0]

    # Sample
    c.execute("""
        SELECT date, vix_close, spy_1w_ret, spy_1m_ret, spy_3m_ret, spy_above_200ma
        FROM market_features
        ORDER BY date DESC LIMIT 5
    """)
    sample = c.fetchall()

    conn.close()

    print()
    print("=" * 60)
    print(f"  Complete!")
    print(f"  Total rows in market_features: {total:,}")
    print(f"  NULL vix_close:       {null_vix}")
    print(f"  NULL spy_1m_ret:      {null_spy}")
    print(f"  NULL spy_above_200ma: {null_regime}")
    print()
    print(f"  {'Date':<12} {'VIX':>6} {'SPY 1w':>8} {'SPY 1m':>8} {'SPY 3m':>8} {'200MA':>6}")
    print(f"  {'-'*54}")
    for r in sample:
        print(f"  {r[0]:<12} {str(round(r[1],1) if r[1] else '-'):>6} "
              f"{str(round(r[2],2) if r[2] else '-'):>8} "
              f"{str(round(r[3],2) if r[3] else '-'):>8} "
              f"{str(round(r[4],2) if r[4] else '-'):>8} "
              f"{str(r[5]) if r[5] is not None else '-':>6}")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true",
                        help="Wipe and rebuild all market features from scratch")
    args = parser.parse_args()
    run(rebuild=args.rebuild)
