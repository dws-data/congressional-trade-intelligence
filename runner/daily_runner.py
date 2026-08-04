# daily_runner.py
# Orchestrates the full daily pipeline:
#   1. Scrape new disclosures from Capitol Trades
#   2. Build price paths for new trades (yfinance, --new-only)
#   3. Classify asset_type for any new tickers (yfinance)
#   4. Recalculate drawdowns for new trades (OHLC path-based)
#   5. Flag within-window repeat buys (repeat_buy_flagger)
#   6. Re-run scorer to update politician scores
#   7. Write run log to logs/daily_run.log
#
# NOTE: Single price-point fetching (price_at_disclosure_date) is no longer a
# separate step. rebuild_all_paths re-fetches entry prices from yfinance on the
# same split-adjusted basis as the path data.
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
    log_section("STEP 1 / 5 — SCRAPE NEW DISCLOSURES")
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
# STEP 2a: BUILD PATHS FOR NEW TRADES
# ─────────────────────────────────────────────

def step_build_new_paths():
    log_section("STEP 2a / 6 — BUILD PATHS FOR NEW TRADES")
    try:
        from pipeline.extend_price_paths import build_new_paths
        inserted = build_new_paths(db_path=DB_PATH)
        log(f"Path rows inserted: {inserted:,}")
        return inserted
    except Exception as e:
        log(f"ERROR in build new paths step: {e}")
        raise


# ─────────────────────────────────────────────
# STEP 2b: EXTEND PATHS FOR OPEN TRADES
# ─────────────────────────────────────────────

def step_extend_open_paths():
    log_section("STEP 2b / 6 — EXTEND PATHS FOR OPEN TRADES")
    try:
        from pipeline.extend_price_paths import extend_open_paths
        appended = extend_open_paths(db_path=DB_PATH)
        log(f"Path rows appended: {appended:,}")
        return appended
    except Exception as e:
        log(f"ERROR in extend paths step: {e}")
        raise

# ─────────────────────────────────────────────
# STEP 2c: TRADE DATE PRICES
# ─────────────────────────────────────────────

def step_trade_date_prices():
    log_section("STEP 2c / 6 — TRADE DATE PRICES")
    try:
        from pipeline.trade_date_prices import run as run_trade_date_prices

        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("""
            SELECT COUNT(*) FROM trades
            WHERE trade_date IS NOT NULL AND trade_date != ''
              AND price_at_trade_date IS NULL
              AND (price_fetch_failed IS NULL OR price_fetch_failed = 0)
        """)
        missing = c.fetchone()[0]
        conn.close()

        if missing == 0:
            log("All trade-date prices populated — skipping")
            return 0, 0

        log(f"Trades missing price_at_trade_date: {missing:,}")
        price_updated, pct_updated = run_trade_date_prices(db_path=DB_PATH)
        log(f"Trade-date prices set: {price_updated:,}  pct_move updated: {pct_updated:,}")
        return price_updated, pct_updated

    except Exception as e:
        log(f"ERROR in trade date prices step: {e}")
        raise


# ─────────────────────────────────────────────
# STEP 3: ASSET TYPE CLASSIFIER
# ─────────────────────────────────────────────

def step_classify_assets():
    log_section("STEP 3 / 6 — CLASSIFY ASSET TYPES")
    try:
        from pipeline.asset_type_fetcher import fetch_asset_types

        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("""
            SELECT COUNT(DISTINCT ticker) FROM trades
            WHERE ticker IS NOT NULL AND ticker != ''
              AND asset_type IS NULL
        """)
        unclassified = c.fetchone()[0]

        if unclassified == 0:
            log("All tickers classified — skipping")
            conn.close()
            return

        log(f"Unclassified tickers: {unclassified:,}")
        counts = fetch_asset_types(conn, c)
        conn.close()
        log(f"Classified: stock={counts['stock']}  etf={counts['etf']}  "
            f"fund={counts['fund']}  other={counts['other']}  error={counts['error']}")

    except Exception as e:
        log(f"ERROR in asset type step: {e}")
        raise


# ─────────────────────────────────────────────
# STEP 4: DRAWDOWN CALCULATOR
# ─────────────────────────────────────────────

def step_drawdown():
    log_section("STEP 4 / 6 — RECALCULATE DRAWDOWNS")
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

        # Always run — calculator overwrites all existing values using MOO entry.
        # Skipping when missing==0 would leave stale disc-close-based values in place.
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
# STEP 4b: SECTOR + COMMITTEE RELEVANCE
# ─────────────────────────────────────────────

def step_market_features():
    log_section("STEP 4c / 6 — MARKET FEATURES")
    try:
        from pipeline.market_features import run as run_market_features

        conn   = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM market_features") if cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='market_features'"
        ).fetchone() else None
        conn.close()

        run_market_features(rebuild=False)

        conn   = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM market_features")
        total = cursor.fetchone()[0]
        conn.close()
        log(f"Market features rows: {total:,}")

    except Exception as e:
        log(f"ERROR in market features step: {e}")
        raise


def step_sector_and_committee():
    log_section("STEP 4b / 6 — SECTOR + COMMITTEE RELEVANCE")
    try:
        from pipeline.sector_fetcher import fetch_ticker_data, flag_committee_relevance

        conn   = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT COUNT(DISTINCT ticker) FROM trades
            WHERE ticker != '' AND ticker IS NOT NULL AND industry IS NULL
        """)
        missing = cursor.fetchone()[0]

        if missing == 0:
            log("All tickers have industry data — re-flagging committee relevance only")
        else:
            log(f"Tickers missing industry: {missing:,} — fetching/propagating now")
            fetch_ticker_data(conn, cursor)

        flag_committee_relevance(conn, cursor)

        cursor.execute("SELECT COUNT(*) FROM trades WHERE committee_relevance IS NOT NULL")
        flagged = cursor.fetchone()[0]
        conn.close()

        log(f"Committee-relevant trades: {flagged:,}")

    except Exception as e:
        log(f"ERROR in sector/committee step: {e}")
        raise


# ─────────────────────────────────────────────
# STEP 5: REPEAT BUY FLAGGER
# ─────────────────────────────────────────────

def step_flag_repeats():
    log_section("STEP 5a / 6 — FLAG WITHIN-WINDOW REPEAT BUYS")
    try:
        from pipeline.repeat_buy_flagger import run_repeat_buy_flagger
        run_repeat_buy_flagger()
    except Exception as e:
        log(f"ERROR in repeat buy flagger step: {e}")
        raise


# ─────────────────────────────────────────────
# STEP 5b: CLUSTER COUNT
# ─────────────────────────────────────────────

def step_cluster_count():
    log_section("STEP 5b / 6 — CLUSTER COUNT")
    try:
        from pipeline.cluster_count import run_cluster_count
        run_cluster_count(db_path=DB_PATH)
    except Exception as e:
        log(f"ERROR in cluster count step: {e}")
        raise


# ─────────────────────────────────────────────
# STEP 5c: FLAG WOULD-FOLLOW TRADES
# ─────────────────────────────────────────────

def step_signal_flag():
    log_section("STEP 5c / 6 — FLAG WOULD-FOLLOW TRADES")
    try:
        from pipeline.signal_flagger import run_signal_flagger, mark_notified
        from runner.notifier import send_email

        total_flagged, newly_flagged = run_signal_flagger(db_path=DB_PATH)
        log(f"Total would-follow trades flagged: {total_flagged:,}")

        if not newly_flagged:
            log("No new would-follow trades since last run")
            return 0

        log(f"New would-follow trades: {len(newly_flagged)}")
        lines = []
        for trade_id, ticker, pol_name, disc_date, cluster_ct, abs_move in newly_flagged:
            line = f"  {ticker} — {pol_name} — disclosed {disc_date} (cluster={cluster_ct}, move={abs_move:.1f}%)"
            log(line)
            lines.append(line)

        subject = f"Congressional Trade Tracker — {len(newly_flagged)} new would-follow trade(s)"
        body = (
            f"{len(newly_flagged)} new trade(s) meet the live execution filter "
            f"(cluster_count_td >= 2 AND abs_pct_move_before_disclosure >= 15):\n\n"
            + "\n".join(lines) +
            "\n\nSee execution/rules.md for the filter definition and the dashboard's "
            "WOULD FOLLOW tab for live status."
        )
        sent = send_email(subject, body)
        log(f"Notification email {'sent' if sent else 'NOT sent (see warning above)'}")

        mark_notified([row[0] for row in newly_flagged], db_path=DB_PATH)
        return len(newly_flagged)

    except Exception as e:
        log(f"ERROR in signal flag step: {e}")
        raise


# ─────────────────────────────────────────────
# STEP 5: SCORER
# ─────────────────────────────────────────────

def step_score():
    log_section("STEP 6 / 6 — UPDATE SCORES")
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

    new_trades   = 0
    paths_built  = 0
    paths_ext    = 0
    calculated   = 0

    if score_only:
        log("Mode: SCORE ONLY — skipping scrape, paths, drawdown")
        scored = step_score()
    else:
        if not skip_scrape:
            new_trades = step_scrape()
        else:
            log("Skipping scrape (--skip-scrape flag set)")

        paths_built = step_build_new_paths()
        paths_ext   = step_extend_open_paths()
        step_trade_date_prices()
        step_classify_assets()
        calculated  = step_drawdown()
        step_market_features()
        step_sector_and_committee()
        step_flag_repeats()
        step_cluster_count()
        step_signal_flag()
        scored      = step_score()

    # ── Final summary ─────────────────────────
    elapsed = (datetime.now() - start).total_seconds()
    total_after, compliant_after, latest_after, scored_after = get_trade_counts()

    log("")
    log_section("RUN COMPLETE")
    log(f"Duration:          {elapsed:.0f}s")
    log(f"New trades:        {new_trades:,}")
    log(f"Paths built (new): {paths_built:,}")
    log(f"Paths extended:    {paths_ext:,}")
    log(f"Drawdowns calc:    {calculated:,}")
    log(f"Politicians:       {scored_after} scored")
    log(f"Latest disc:       {latest_after}")
    log(f"Total trades:      {total_after:,}  (was {total_before:,})")
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