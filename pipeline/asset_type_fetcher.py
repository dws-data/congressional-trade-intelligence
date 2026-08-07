# pipeline/asset_type_fetcher.py
# Classifies asset_type for every ticker that currently has NULL.
# Uses yfinance quoteType — works without any API key.
#
# asset_type values:
#   'stock'  — ordinary equity (EQUITY)
#   'etf'    — ETF (ETF)
#   'fund'   — mutual fund (MUTUALFUND)
#   'other'  — anything else or unrecognised quoteType
#   'error'  — yfinance call failed or returned no info
#
# IMPORTANT: UPDATE always includes AND asset_type IS NULL.
# This ensures existing classifications are never overwritten,
# even if the API returns a different result on a later run.
#
# Run: python -m pipeline.asset_type_fetcher

import sys
import time
import yfinance as yf
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import get_connection

DELAY      = 0.1    # seconds between yfinance calls — be polite
BATCH_SIZE = 50     # commit every N tickers
INVALID_TICKERS = {"--", "N/A", "NA", ""}

QUOTE_TYPE_MAP = {
    "EQUITY":     "stock",
    "ETF":        "etf",
    "MUTUALFUND": "fund",
}


def classify_ticker(ticker):
    """
    Call yfinance for a single ticker. Returns asset_type string.
    """
    try:
        info = yf.Ticker(ticker).info
        qt = info.get("quoteType", "")
        return QUOTE_TYPE_MAP.get(qt, "other")
    except Exception:
        return "error"


def fetch_asset_types(conn, cursor):
    cursor.execute("""
        SELECT DISTINCT ticker FROM trades
        WHERE ticker IS NOT NULL AND ticker != ''
          AND asset_type IS NULL
        ORDER BY ticker
    """)
    all_tickers = [r[0] for r in cursor.fetchall()]
    print(f"  Tickers to classify: {len(all_tickers):,}")

    counts  = {"stock": 0, "etf": 0, "fund": 0, "other": 0, "error": 0}
    updated = 0

    # Handle clearly invalid tickers immediately
    invalid = [t for t in all_tickers if t in INVALID_TICKERS]
    to_fetch = [t for t in all_tickers if t not in INVALID_TICKERS]

    for t in invalid:
        cursor.execute(
            "UPDATE trades SET asset_type='other' WHERE ticker=? AND asset_type IS NULL", (t,)
        )
        counts["other"] += 1
        updated += 1
    if invalid:
        conn.commit()
        print(f"  Marked {len(invalid):,} invalid tickers as 'other'")

    for i, ticker in enumerate(to_fetch):
        atype = classify_ticker(ticker)
        cursor.execute(
            "UPDATE trades SET asset_type=? WHERE ticker=? AND asset_type IS NULL",
            (atype, ticker)
        )
        counts[atype] += 1
        updated += 1

        if updated % BATCH_SIZE == 0:
            conn.commit()

        if (i + 1) % 50 == 0 or (i + 1) == len(to_fetch):
            print(f"    [{i+1:4d}/{len(to_fetch)}]  "
                  f"stock:{counts['stock']:,}  etf:{counts['etf']:,}  "
                  f"fund:{counts['fund']:,}  other:{counts['other']:,}  "
                  f"error:{counts['error']:,}")

        time.sleep(DELAY)

    conn.commit()
    return counts


def run_asset_type_fetcher(db_path=None):
    print("=" * 60)
    print("  Asset Type Fetcher")
    print("  Source: yfinance quoteType")
    print("=" * 60)

    conn   = get_connection(db_path)
    cursor = conn.cursor()

    # Ensure column exists
    cols = [c[1] for c in cursor.execute("PRAGMA table_info(trades)").fetchall()]
    if "asset_type" not in cols:
        cursor.execute("ALTER TABLE trades ADD COLUMN asset_type TEXT")
        print("  Added asset_type column")
        conn.commit()

    print("\n  Classifying tickers via yfinance...")
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
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None,
                        help="DB path override (default: data/trades.db)")
    args = parser.parse_args()
    run_asset_type_fetcher(db_path=args.db)
