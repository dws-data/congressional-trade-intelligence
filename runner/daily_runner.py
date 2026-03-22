# daily_runner.py
# Orchestrates the full daily pipeline:
#   1. Scrape new disclosures from Capitol Trades
#   2. Fetch prices for new trades (yfinance)
#   3. Recalculate drawdowns for new trades (OHLC path-based)
#   4. Re-run scorer to update politician scores
#   5. Write run log to logs/daily_run.log
#
# Usage (run from project root):
#   python runner/daily_runner.py
#   python runner/daily_runner.py --skip-scrape
#   python runner/daily_runner.py --score-only
#
# Schedule with Windows Task Scheduler to run once daily after market close (e.g. 6pm)

import sys
import sqlite3
import argparse
from datetime import datetime
from pathlib import Path

# Project root = congressional_trading/ (one level up from runner/)
ROOT     = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

DB_PATH  = str(ROOT / "data" / "trades.db")
LOG_DIR  = ROOT / "logs"
LOG_FILE = LOG_DIR / "daily_run.log"

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

LOG_DIR.mkdir(exist_ok=True)

def log(msg, also_print=True):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    if also_print:
        print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def log_section(title):
    bar = "=" * 60
    log(bar)
    log(f"  {title}")
    log(bar)

# ─────────────────────────────────────────────
# DB HELPERS
# ─────────────────────────────────────────────

def get_trade_counts():
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("SELECT COUNT(*) FROM trades")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM trades WHERE transaction_type='buy' AND filing_violation='compliant'")
    compliant = c.fetchone()[0]
    c.execute("SELECT MAX(disclosure_date) FROM trades WHERE disclosure_date != ''")
    latest = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM politicians WHERE score IS NOT NULL")
    scored = c.fetchone()[0]
    conn.close()
    return total, compliant, latest, scored

def get_new_trades_since(last_run_date):
    """Count trades added since last run date."""
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("""
        SELECT COUNT(*) FROM trades
        WHERE disclosure_date >= ?
        AND disclosure_date != ''
    """, (last_run_date,))
    count = c.fetchone()[0]
    conn.close()
    return count

# ─────────────────────────────────────────────
# STEP 1: SCRAPE
# ─────────────────────────────────────────────

def step_scrape():
    log_section("STEP 1 / 4 — SCRAPE NEW DISCLOSURES")
    try:
        from scrapers.capitol_trades import run_scraper
        before_total, _, _, _ = get_trade_counts()
        log(f"Trades before scrape: {before_total:,}")

        # IMPORTANT: always pass max_pages to prevent full re-scrape wipe.
        # Capitol Trades shows ~20 trades/page. 30 pages = ~600 trades = ~1 month of data.
        # The scraper uses INSERT OR IGNORE so duplicates are safely skipped.
        # Never call run_scraper() with no arguments — it wipes the DB first.
        run_scraper(max_pages=30, start_page=1)

        after_total, _, latest, _ = get_trade_counts()
        new_trades = after_total - before_total
        log(f"Trades after scrape:  {after_total:,}  (+{new_trades} new)")
        log(f"Latest disclosure:    {latest}")
        return new_trades

    except Exception as e:
        log(f"ERROR in scrape step: {e}")
        raise

# ─────────────────────────────────────────────
# STEP 2: FETCH PRICES
# ─────────────────────────────────────────────

def step_fetch_prices():
    log_section("STEP 2 / 4 — FETCH PRICES FOR NEW TRADES")
    try:
        from pipeline.price_fetcher import run_price_fetcher

        # Count trades missing prices before
        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("""
            SELECT COUNT(*) FROM trades
            WHERE transaction_type = 'buy'
            AND ticker != ''
            AND price_at_disclosure_date IS NULL
        """)
        missing_before = c.fetchone()[0]
        conn.close()

        log(f"Trades missing disc price: {missing_before:,}")

        if missing_before == 0:
            log("No new prices needed — skipping")
            return 0

        run_price_fetcher()

        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("""
            SELECT COUNT(*) FROM trades
            WHERE transaction_type = 'buy'
            AND ticker != ''
            AND price_at_disclosure_date IS NULL
        """)
        missing_after = c.fetchone()[0]
        conn.close()

        fetched = missing_before - missing_after
        log(f"Prices fetched: {fetched:,}  (still missing: {missing_after:,})")
        return fetched

    except Exception as e:
        log(f"ERROR in price fetch step: {e}")
        raise

# ─────────────────────────────────────────────
# STEP 3: DRAWDOWN CALCULATOR
# ─────────────────────────────────────────────

def step_drawdown():
    log_section("STEP 3 / 4 — RECALCULATE DRAWDOWNS")
    try:
        from pipeline.drawdown_calculator import run_drawdown_calculator

        # Count trades missing drawdown before
        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("""
            SELECT COUNT(*) FROM trades
            WHERE transaction_type = 'buy'
            AND price_at_disclosure_date IS NOT NULL
            AND max_drawdown_disc IS NULL
        """)
        missing_before = c.fetchone()[0]
        conn.close()

        log(f"Trades missing drawdown: {missing_before:,}")

        if missing_before == 0:
            log("No new drawdowns needed — skipping")
            return 0

        run_drawdown_calculator()

        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("""
            SELECT COUNT(*) FROM trades
            WHERE transaction_type = 'buy'
            AND price_at_disclosure_date IS NOT NULL
            AND max_drawdown_disc IS NULL
        """)
        missing_after = c.fetchone()[0]
        conn.close()

        calculated = missing_before - missing_after
        log(f"Drawdowns calculated: {calculated:,}  (still missing: {missing_after:,})")
        return calculated

    except Exception as e:
        log(f"ERROR in drawdown step: {e}")
        raise

# ─────────────────────────────────────────────
# STEP 4: SCORER
# ─────────────────────────────────────────────

def step_score():
    log_section("STEP 4 / 4 — UPDATE SCORES")
    try:
        from pipeline.scorer import run_scorer

        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("SELECT COUNT(*) FROM politicians WHERE score IS NOT NULL")
        scored_before = c.fetchone()[0]
        conn.close()

        log(f"Politicians scored before: {scored_before}")

        scored_list = run_scorer()

        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("SELECT COUNT(*) FROM politicians WHERE score IS NOT NULL")
        scored_after = c.fetchone()[0]
        conn.close()

        log(f"Politicians scored after:  {scored_after}")

        if scored_list:
            top = scored_list[0]
            log(f"Top scorer: {top[1]} — {top[4]['score']:.1f} "
                f"(WR: {top[4]['win_rate']:.1f}%  Trades: {top[4]['total_trades']})")

        return scored_after

    except Exception as e:
        log(f"ERROR in scoring step: {e}")
        raise

# ─────────────────────────────────────────────
# MAIN RUNNER
# ─────────────────────────────────────────────

def run(skip_scrape=False, score_only=False):
    start = datetime.now()

    log("")
    log_section(f"DAILY RUNNER — {start.strftime('%Y-%m-%d %H:%M')}")

    total_before, compliant_before, latest_before, scored_before = get_trade_counts()
    log(f"DB state:  {total_before:,} trades  |  {compliant_before:,} compliant  |  "
        f"{scored_before} scored  |  latest: {latest_before}")
    log("")

    new_trades  = 0
    fetched     = 0
    calculated  = 0

    if score_only:
        log("Mode: SCORE ONLY — skipping scrape, prices, drawdown")
        scored = step_score()
    else:
        if not skip_scrape:
            new_trades = step_scrape()
        else:
            log("Skipping scrape (--skip-scrape flag set)")

        fetched    = step_fetch_prices()
        calculated = step_drawdown()
        scored     = step_score()

    # ── Final summary ─────────────────────────
    elapsed = (datetime.now() - start).total_seconds()
    total_after, compliant_after, latest_after, scored_after = get_trade_counts()

    log("")
    log_section("RUN COMPLETE")
    log(f"Duration:        {elapsed:.0f}s")
    log(f"New trades:      {new_trades:,}")
    log(f"Prices fetched:  {fetched:,}")
    log(f"Drawdowns calc:  {calculated:,}")
    log(f"Politicians:     {scored_after} scored")
    log(f"Latest disc:     {latest_after}")
    log(f"Total trades:    {total_after:,}  (was {total_before:,})")
    log("")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Congressional Trade Tracker — Daily Runner")
    parser.add_argument("--skip-scrape", action="store_true",
                        help="Skip scraping step (prices + drawdown + score only)")
    parser.add_argument("--score-only",  action="store_true",
                        help="Run scorer only (fastest, use after manual DB changes)")
    args = parser.parse_args()

    try:
        run(skip_scrape=args.skip_scrape, score_only=args.score_only)
    except Exception as e:
        log(f"FATAL ERROR: {e}")
        import traceback
        log(traceback.format_exc())
        sys.exit(1)