# pipeline/extend_price_paths.py
#
# Extends existing trade_price_paths rows to today.
# fix_disc_paths.py used INSERT OR IGNORE, so paths fetched in the
# initial run were never updated. This script picks up where each
# path left off and appends new trading days up to today.
#
# Run with: python -m pipeline.extend_price_paths

import sqlite3
import yfinance as yf
from datetime import datetime, timedelta, date

DB_PATH    = "data/trades.db"
BATCH_SIZE = 50

def get_history(ticker):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="2y")
        if hist.empty:
            return None
        hist.index = hist.index.date
        return hist
    except Exception:
        return None

def run():
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    today  = date.today()

    print("=" * 60)
    print("  Extend Price Paths")
    print(f"  Extending all disc paths to {today}")
    print("=" * 60)

    # Get the last stored date per trade (disc anchor only)
    print("Loading last stored dates per trade...")
    cursor.execute("""
        SELECT p.trade_id, t.ticker, t.disclosure_date,
               t.price_at_disclosure_date,
               MAX(p.day)  as last_day,
               MAX(p.date) as last_date
        FROM trade_price_paths p
        JOIN trades t ON p.trade_id = t.trade_id
        WHERE p.anchor = 'disc'
        GROUP BY p.trade_id
        HAVING MAX(p.date) < ?
    """, (str(today),))
    to_extend = cursor.fetchall()
    print(f"  Trades to extend: {len(to_extend):,}")

    # Also check for trades that have NO paths at all
    cursor.execute("""
        SELECT trade_id, ticker, disclosure_date, price_at_disclosure_date
        FROM trades
        WHERE transaction_type = 'buy'
        AND filing_violation = 'compliant'
        AND price_at_disclosure_date IS NOT NULL
        AND disclosure_date IS NOT NULL AND disclosure_date != ''
        AND trade_id NOT IN (
            SELECT DISTINCT trade_id FROM trade_price_paths WHERE anchor = 'disc'
        )
    """)
    missing = cursor.fetchall()
    print(f"  Trades with no paths at all: {len(missing):,}")

    rows_saved = 0
    processed  = 0
    errors     = 0

    # ── Extend existing paths ─────────────────────
    ticker_cache = {}

    for trade_id, ticker, disc_date_str, entry_price, last_day, last_date_str in to_extend:
        if ticker not in ticker_cache:
            ticker_cache[ticker] = get_history(ticker)
        hist = ticker_cache[ticker]

        if hist is None:
            errors += 1
            processed += 1
            continue

        try:
            last_date  = datetime.strptime(last_date_str, "%Y-%m-%d").date()
            start_date = last_date + timedelta(days=1)
            new_days   = [d for d in hist.index if start_date <= d <= today]

            path_rows = []
            for i, actual_date in enumerate(new_days, start=last_day + 1):
                row   = hist.loc[actual_date]
                close = float(row["Close"])
                pct   = round((close - entry_price) / entry_price * 100, 4) if entry_price and entry_price > 0 else None
                path_rows.append((
                    trade_id, "disc", i, str(actual_date),
                    round(float(row["Open"]),  4),
                    round(float(row["High"]),  4),
                    round(float(row["Low"]),   4),
                    round(float(row["Close"]), 4),
                    pct,
                ))

            if path_rows:
                cursor.executemany("""
                    INSERT OR IGNORE INTO trade_price_paths
                        (trade_id, anchor, day, date, open, high, low, close, pct_change)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, path_rows)
                rows_saved += len(path_rows)

        except Exception:
            errors += 1

        processed += 1
        if processed % BATCH_SIZE == 0:
            conn.commit()
            print(f"  [{processed:5d}/{len(to_extend)}] rows added so far: {rows_saved:,}")

    conn.commit()

    # ── Add paths for trades with none at all ─────
    if missing:
        print(f"\nFetching paths for {len(missing):,} trades with no path data...")
        processed_m = 0
        for trade_id, ticker, disc_date_str, entry_price in missing:
            if ticker not in ticker_cache:
                ticker_cache[ticker] = get_history(ticker)
            hist = ticker_cache[ticker]

            if hist is None:
                processed_m += 1
                continue

            try:
                anchor_date = datetime.strptime(disc_date_str, "%Y-%m-%d").date()
                start_date  = anchor_date + timedelta(days=1)
                new_days    = [d for d in hist.index if start_date <= d <= today]

                path_rows = []
                for i, actual_date in enumerate(new_days, start=1):
                    row   = hist.loc[actual_date]
                    close = float(row["Close"])
                    pct   = round((close - entry_price) / entry_price * 100, 4) if entry_price and entry_price > 0 else None
                    path_rows.append((
                        trade_id, "disc", i, str(actual_date),
                        round(float(row["Open"]),  4),
                        round(float(row["High"]),  4),
                        round(float(row["Low"]),   4),
                        round(float(row["Close"]), 4),
                        pct,
                    ))

                if path_rows:
                    cursor.executemany("""
                        INSERT OR IGNORE INTO trade_price_paths
                            (trade_id, anchor, day, date, open, high, low, close, pct_change)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, path_rows)
                    rows_saved += len(path_rows)

            except Exception:
                pass

            processed_m += 1
            if processed_m % BATCH_SIZE == 0:
                conn.commit()
                print(f"  [{processed_m:5d}/{len(missing)}] rows added: {rows_saved:,}")

        conn.commit()

    conn.close()
    print(f"\nDone! Extended {processed:,} trades, added {rows_saved:,} new path rows ({errors} errors)")
    print("Next: re-run drawdown_calculator.py then scorer.py to update scores")

if __name__ == "__main__":
    run()
