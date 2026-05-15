# pipeline/rebuild_bad_paths.py
#
# Identifies and rebuilds price paths where the stored path OHLC data
# is inconsistent with price_at_disclosure_date.
#
# Root cause: fmp_price_fetcher.py built paths from FMP historical data
# at a point in time when FMP had incorrect/unadjusted prices for 2021-2022.
# price_at_disclosure_date is correct (matches yfinance / chart prices).
# The path prices are wrong (significantly higher than actual market prices).
#
# Detection: if path day-1 open > price_at_disclosure_date * (1 + THRESHOLD)
# then the path is inconsistent with the entry price.
#
# Fix: delete bad path rows, rebuild from yfinance (same source as what you
# see on any chart). Entry price is left unchanged.
#
# Run: python -m pipeline.rebuild_bad_paths [--dry-run]

import sqlite3
import yfinance as yf
import time
import argparse
from datetime import datetime, timedelta, date
from collections import defaultdict
from pathlib import Path

DB_PATH   = Path(__file__).parent.parent / "data" / "trades.db"
THRESHOLD = 0.05   # flag paths where day-1 open > entry * 1.05
DELAY     = 0.4    # seconds between yfinance calls
COMMIT_N  = 50


def find_bad_paths(cursor):
    """
    Return list of (trade_id, ticker, disclosure_date, price_at_disclosure_date)
    for trades whose day-1 path open is more than THRESHOLD above entry price.
    """
    cursor.execute("""
        SELECT t.trade_id, t.ticker, t.disclosure_date, t.price_at_disclosure_date,
               p.open AS day1_open,
               ROUND((p.open - t.price_at_disclosure_date)
                     / t.price_at_disclosure_date * 100, 1) AS gap_pct
        FROM trades t
        JOIN trade_price_paths p
          ON p.trade_id = t.trade_id AND p.anchor = 'disc' AND p.day = 1
        WHERE t.price_at_disclosure_date IS NOT NULL
          AND t.price_at_disclosure_date > 0
          AND p.open > t.price_at_disclosure_date * ?
        ORDER BY gap_pct DESC
    """, (1 + THRESHOLD,))
    return cursor.fetchall()


def build_path_from_yf(hist, disc_date_str, entry_price):
    """
    Build list of (anchor, day, date, open, high, low, close, pct_change)
    rows from yfinance history, starting day 1 after disc_date.
    """
    try:
        anchor = datetime.strptime(disc_date_str, "%Y-%m-%d").date()
    except ValueError:
        return []

    today = date.today()
    rows  = []

    trading_dates = sorted(
        d for d in hist.index
        if anchor < d <= today
    )

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


def run(dry_run=False):
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print("=" * 65)
    print("  Rebuild Bad Price Paths")
    print(f"  Threshold: day-1 open > entry price + {THRESHOLD*100:.0f}%")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print("=" * 65)

    # ── Step 1: identify bad paths ─────────────────────────────
    print("\nStep 1: Finding bad paths...")
    bad = find_bad_paths(cursor)
    print(f"  Bad paths found: {len(bad):,}")

    if not bad:
        print("  Nothing to fix.")
        conn.close()
        return

    # Summary by year
    by_year = defaultdict(int)
    for _, _, disc, _, _, _ in bad:
        yr = disc[:4] if disc else "?"
        by_year[yr] += 1
    print("  By disclosure year:")
    for yr in sorted(by_year):
        print(f"    {yr}: {by_year[yr]:,}")

    if dry_run:
        print("\n  (Dry run — no changes made)")
        conn.close()
        return

    # ── Step 2: group by ticker ────────────────────────────────
    by_ticker = defaultdict(list)
    for trade_id, ticker, disc, entry, day1_open, gap_pct in bad:
        by_ticker[ticker].append((trade_id, disc, entry))

    print(f"\nStep 2: Rebuilding paths for {len(by_ticker):,} tickers...")

    deleted_total    = 0
    rebuilt_total    = 0
    failed_tickers   = []
    processed        = 0

    for ticker, trades in sorted(by_ticker.items()):
        # Fetch yfinance history covering all disclosure dates for this ticker
        dates    = [t[1] for t in trades if t[1]]
        min_date = min(dates)
        start    = str(datetime.strptime(min_date, "%Y-%m-%d").date() - timedelta(days=10))
        end      = str(date.today())

        try:
            stock = yf.Ticker(ticker)
            hist  = stock.history(start=start, end=end)
            if hist.empty:
                raise ValueError("empty history")
            hist.index = hist.index.date
        except Exception as e:
            print(f"  [{ticker}]  FAILED: {e} — skipping {len(trades)} trades")
            failed_tickers.append(ticker)
            time.sleep(DELAY)
            continue

        ticker_deleted  = 0
        ticker_rebuilt  = 0

        for trade_id, disc, entry in trades:
            # Delete existing bad path rows
            cursor.execute(
                "DELETE FROM trade_price_paths WHERE trade_id = ? AND anchor = 'disc'",
                (trade_id,)
            )
            ticker_deleted += cursor.rowcount

            # Rebuild from yfinance
            path_rows = build_path_from_yf(hist, disc, entry)
            if path_rows:
                cursor.executemany("""
                    INSERT INTO trade_price_paths
                        (trade_id, anchor, day, date, open, high, low, close, pct_change)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [(trade_id, *r) for r in path_rows])
                ticker_rebuilt += len(path_rows)

        deleted_total += ticker_deleted
        rebuilt_total += ticker_rebuilt
        processed     += 1

        if processed % COMMIT_N == 0:
            conn.commit()

        if processed % 25 == 0 or processed <= 5:
            print(f"  [{processed:4d}/{len(by_ticker)}]  {ticker:<10}  "
                  f"deleted={ticker_deleted}  rebuilt={ticker_rebuilt} rows")

        time.sleep(DELAY)

    conn.commit()

    # ── Step 3: verify ─────────────────────────────────────────
    print("\nStep 3: Verifying — re-checking for remaining bad paths...")
    still_bad = find_bad_paths(cursor)
    conn.close()

    print("\n" + "=" * 65)
    print("  Complete!")
    print(f"  Path rows deleted:   {deleted_total:,}")
    print(f"  Path rows rebuilt:   {rebuilt_total:,}")
    print(f"  Tickers failed:      {len(failed_tickers)}")
    if failed_tickers:
        print(f"    {', '.join(failed_tickers[:20])}")
    print(f"  Still bad (unfixed): {len(still_bad):,}")
    print()
    print("Next steps:")
    print("  python pipeline/drawdown_calculator.py")
    print("  python pipeline/scorer.py")
    print("  python qa/system_qa.py")
    print("=" * 65)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be fixed without making changes")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
