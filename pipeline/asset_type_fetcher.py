# pipeline/asset_type_fetcher.py
# Fetches quoteType from yfinance for every distinct ticker
# and writes asset_type to trades table.
#
# asset_type values:
#   'stock'  — EQUITY
#   'etf'    — ETF
#   'fund'   — MUTUALFUND
#   'other'  — anything else (futures, index, crypto, etc.)
#   'error'  — yfinance call failed
#
# Run: python -m pipeline.asset_type_fetcher

import sqlite3
import time
import yfinance as yf
from pathlib import Path

DB_PATH    = Path(__file__).parent.parent / "data" / "trades.db"
DELAY      = 0.3
BATCH_SIZE = 100

QUOTE_TYPE_MAP = {
    "EQUITY":      "stock",
    "ETF":         "etf",
    "MUTUALFUND":  "fund",
}


def ensure_column(cursor):
    cols = [c[1] for c in cursor.execute("PRAGMA table_info(trades)").fetchall()]
    if "asset_type" not in cols:
        cursor.execute("ALTER TABLE trades ADD COLUMN asset_type TEXT")
        print("  Added asset_type column")
    else:
        print("  asset_type column already exists")


def fetch_asset_types(conn, cursor):
    # Only fetch tickers we haven't classified yet
    cursor.execute("""
        SELECT DISTINCT ticker FROM trades
        WHERE ticker != ''
        AND ticker IS NOT NULL
        AND asset_type IS NULL
        ORDER BY ticker
    """)
    tickers = [r[0] for r in cursor.fetchall()]
    print(f"  Tickers to classify: {len(tickers):,}")

    counts   = {"stock": 0, "etf": 0, "fund": 0, "other": 0, "error": 0}
    results  = {}

    for i, ticker in enumerate(tickers):
        try:
            info       = yf.Ticker(ticker).info
            quote_type = info.get("quoteType", "").upper()
            asset_type = QUOTE_TYPE_MAP.get(quote_type, "other" if quote_type else "error")
        except Exception:
            asset_type = "error"

        results[ticker] = asset_type
        counts[asset_type] += 1
        time.sleep(DELAY)

        if (i + 1) % 50 == 0:
            pct = (i + 1) / len(tickers) * 100
            print(f"    [{i+1:4d}/{len(tickers)}  {pct:.0f}%]  "
                  f"stock:{counts['stock']:,}  etf:{counts['etf']:,}  "
                  f"fund:{counts['fund']:,}  other:{counts['other']:,}  "
                  f"error:{counts['error']:,}  |  last: {ticker}")

    # Write to DB
    updated = 0
    for ticker, asset_type in results.items():
        cursor.execute(
            "UPDATE trades SET asset_type = ? WHERE ticker = ?",
            (asset_type, ticker)
        )
        updated += 1
        if updated % BATCH_SIZE == 0:
            conn.commit()

    conn.commit()
    return counts


def run_asset_type_fetcher():
    print("=" * 60)
    print("  Asset Type Fetcher")
    print("  Source: yfinance quoteType")
    print("=" * 60)

    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print("\n  Step 1: Ensuring column exists...")
    ensure_column(cursor)
    conn.commit()

    print("\n  Step 2: Fetching quoteTypes from yfinance...")
    counts = fetch_asset_types(conn, cursor)

    # Summary
    cursor.execute("""
        SELECT asset_type, COUNT(DISTINCT ticker) as tickers, COUNT(*) as trades
        FROM trades
        WHERE ticker != '' AND ticker IS NOT NULL
        GROUP BY asset_type
        ORDER BY trades DESC
    """)
    rows = cursor.fetchall()

    conn.close()

    print("\n" + "=" * 60)
    print("  Complete!\n")
    print(f"  {'Asset Type':<12}  {'Tickers':>8}  {'Trades':>8}")
    print(f"  {'-'*32}")
    for asset_type, tickers, trades in rows:
        print(f"  {str(asset_type):<12}  {tickers:>8,}  {trades:>8,}")
    print("=" * 60)


if __name__ == "__main__":
    run_asset_type_fetcher()
