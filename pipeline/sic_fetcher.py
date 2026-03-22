# pipeline/sic_fetcher.py
# Fetches SIC codes for all tickers using SEC EDGAR (free, no API key needed)
# Stores in trades.sic_code column
#
# Method:
#   1. Download bulk ticker → CIK mapping from SEC (cached to data/sec_tickers.json)
#   2. For each of our tickers, look up CIK
#   3. Fetch SEC submissions JSON per CIK to get SIC code + description
#
# Rate limit: SEC allows ~10 req/sec; we use 0.15s delay (~6/sec to be safe)
#
# Run: python pipeline/sic_fetcher.py

import sqlite3
import json
import time
import urllib.request
import urllib.error
from pathlib import Path

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────

DB_PATH    = Path(__file__).parent.parent / "data" / "trades.db"
CACHE_PATH = Path(__file__).parent.parent / "data" / "sec_tickers.json"
DELAY      = 0.15   # seconds between SEC requests (~6/sec)
BATCH_SIZE = 200    # commit every N updates

SEC_TICKERS_URL     = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
USER_AGENT          = "CongressionalTradeTracker research@example.com"

# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────

def fetch_json(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ensure_columns(cursor):
    cols = [c[1] for c in cursor.execute("PRAGMA table_info(trades)").fetchall()]
    if "sic_code" not in cols:
        cursor.execute("ALTER TABLE trades ADD COLUMN sic_code TEXT")
        print("  Added sic_code column")
    else:
        print("  sic_code column already exists")
    if "sic_description" not in cols:
        cursor.execute("ALTER TABLE trades ADD COLUMN sic_description TEXT")
        print("  Added sic_description column")
    else:
        print("  sic_description column already exists")


# ─────────────────────────────────────────────────────────────────
# STEP 1: LOAD SEC TICKER → CIK MAP (with local cache)
# ─────────────────────────────────────────────────────────────────

def load_ticker_cik_map():
    if CACHE_PATH.exists():
        print(f"  Loading cached SEC ticker map from {CACHE_PATH.name}...")
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    else:
        print(f"  Downloading SEC ticker → CIK map from SEC EDGAR...")
        raw = fetch_json(SEC_TICKERS_URL)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        print(f"  Cached to {CACHE_PATH.name}")

    ticker_to_cik = {}
    for entry in raw.values():
        t   = entry.get("ticker", "").upper()
        cik = str(entry.get("cik_str", "")).zfill(10)
        if t and cik:
            ticker_to_cik[t] = cik

    print(f"  SEC ticker map: {len(ticker_to_cik):,} entries")
    return ticker_to_cik


# ─────────────────────────────────────────────────────────────────
# STEP 2: FETCH SIC PER TICKER
# ─────────────────────────────────────────────────────────────────

def fetch_sic_codes(conn, cursor, ticker_to_cik):
    cursor.execute("""
        SELECT DISTINCT ticker FROM trades
        WHERE ticker != ''
        AND ticker IS NOT NULL
        AND sic_code IS NULL
        ORDER BY ticker
    """)
    tickers = [r[0] for r in cursor.fetchall()]
    print(f"\n  Tickers missing SIC codes: {len(tickers):,}")

    if not tickers:
        print("  Nothing to fetch.")
        return

    matched    = 0
    no_cik     = 0
    fetch_fail = 0

    for i, ticker in enumerate(tickers):
        cik = ticker_to_cik.get(ticker.upper())

        if not cik:
            no_cik += 1
            # Write NULL explicitly so we don't retry on next run
            cursor.execute(
                "UPDATE trades SET sic_code = 'NOT_FOUND' WHERE ticker = ? AND sic_code IS NULL",
                (ticker,)
            )
        else:
            sic = sic_desc = None
            try:
                url  = SEC_SUBMISSIONS_URL.format(cik=cik)
                data = fetch_json(url)
                sic      = str(data.get("sic", "")).strip() or None
                sic_desc = data.get("sicDescription", None)
                matched += 1
            except urllib.error.HTTPError as e:
                fetch_fail += 1
                sic = sic_desc = None
            except Exception:
                fetch_fail += 1
                sic = sic_desc = None

            cursor.execute(
                """UPDATE trades SET sic_code = ?, sic_description = ?
                   WHERE ticker = ? AND sic_code IS NULL""",
                (sic or "NOT_FOUND", sic_desc, ticker)
            )

            time.sleep(DELAY)

        if (i + 1) % BATCH_SIZE == 0:
            conn.commit()
            print(f"  [{i+1:4d}/{len(tickers)}]  matched: {matched:,}  "
                  f"no CIK: {no_cik:,}  failed: {fetch_fail:,}  last: {ticker}")

    conn.commit()
    print(f"\n  Done.  matched: {matched:,}  no CIK: {no_cik:,}  failed: {fetch_fail:,}")


# ─────────────────────────────────────────────────────────────────
# STEP 3: SUMMARY — SIC breakdown for ambiguous industry categories
# ─────────────────────────────────────────────────────────────────

def print_sic_summary(cursor):
    print("\n" + "=" * 60)
    print("  SIC breakdown for ambiguous industry categories")
    print("=" * 60)

    ambiguous = [
        "Software - Infrastructure",
        "Information Technology Services",
        "Software - Application",
    ]

    for industry in ambiguous:
        cursor.execute("""
            SELECT sic_code, sic_description, COUNT(DISTINCT ticker) as n
            FROM trades
            WHERE industry = ?
            AND sic_code IS NOT NULL
            AND sic_code != 'NOT_FOUND'
            GROUP BY sic_code, sic_description
            ORDER BY n DESC
        """, (industry,))
        rows = cursor.fetchall()
        print(f"\n  {industry}:")
        if rows:
            for sic, desc, n in rows:
                print(f"    {sic}  {str(desc or ''):<45}  {n:>3} tickers")
        else:
            print("    (no data)")

    # Overall coverage
    cursor.execute("""
        SELECT
            COUNT(DISTINCT ticker) FILTER (WHERE sic_code IS NOT NULL AND sic_code != 'NOT_FOUND') as with_sic,
            COUNT(DISTINCT ticker) FILTER (WHERE sic_code = 'NOT_FOUND')                          as not_found,
            COUNT(DISTINCT ticker)                                                                  as total
        FROM trades
        WHERE ticker != ''
    """)
    row = cursor.fetchone()
    with_sic, not_found, total = row
    print(f"\n  Overall coverage:")
    print(f"    With SIC:   {with_sic:,} / {total:,} tickers ({with_sic/total*100:.1f}%)")
    print(f"    Not found:  {not_found:,} tickers")


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

def run_sic_fetcher():
    print("=" * 60)
    print("  SIC Code Fetcher — SEC EDGAR")
    print("=" * 60)

    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print("\n  Step 1: Ensuring columns...")
    ensure_columns(cursor)
    conn.commit()

    print("\n  Step 2: Loading SEC ticker → CIK map...")
    ticker_to_cik = load_ticker_cik_map()

    print("\n  Step 3: Fetching SIC codes...")
    fetch_sic_codes(conn, cursor, ticker_to_cik)

    print("\n  Step 4: SIC summary...")
    print_sic_summary(cursor)

    conn.close()
    print("\n" + "=" * 60)
    print("  Complete!")
    print("=" * 60)


if __name__ == "__main__":
    run_sic_fetcher()
