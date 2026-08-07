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
# Schedule with Windows Task Scheduler to run once daily at 9:30pm UK time —
# after US market close (4pm ET), with a buffer for yfinance EOD data to settle.

import sys
import argparse
from datetime import datetime
from pathlib import Path

# Project root = congressional_trading/ (one level up from runner/)
ROOT     = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from db import get_connection

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
# HEALTH TRACKING (scrape sanity checks + weekly digest)
# ─────────────────────────────────────────────
# scrape_history: one row per step_scrape() call, used to detect N
# consecutive zero-new-trade runs (possible scraper breakage) — kept
# separate from run_history so --skip-scrape/--score-only runs don't
# pollute the streak.
# run_history: one row per full run() invocation (success or fail), used
# to build the Sunday weekly digest. The digest exists specifically to
# catch the case a warning-only alert can't: the scheduled task itself
# stops firing entirely (disabled, machine off), so there's no error to
# alert on — silence would otherwise look identical to "all fine".

SCRAPE_ZERO_DAY_THRESHOLD = 5
SCRAPE_HIGH_CEILING       = 500

def ensure_health_tables():
    conn = get_connection()
    c    = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS scrape_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at     TEXT NOT NULL,
            new_trades INTEGER NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS run_history (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at        TEXT NOT NULL,
            mode          TEXT NOT NULL,
            success       INTEGER NOT NULL,
            new_trades    INTEGER,
            duration_sec  REAL,
            error_msg     TEXT
        )
    """)
    conn.commit()
    conn.close()

def _send_health_alert(subject, body):
    from runner.notifier import send_email
    sent = send_email(f"Congressional Trade Tracker — ALERT: {subject}", body)
    log(f"Health alert email {'sent' if sent else 'NOT sent (see warning above)'}")

def record_run(mode, success, new_trades=None, duration_sec=None, error_msg=None):
    # get_connection()'s 30s default timeout matters here specifically: this is
    # called from the crash handler right after a fatal error — if that error
    # was itself a DB lock, SQLite's old 5s default would throw again, uncaught,
    # and kill the alert path before it reaches the failure email. See 2026-08-05
    # diary for the incident this was found from.
    conn = get_connection()
    c    = conn.cursor()
    c.execute("""
        INSERT INTO run_history (run_at, mode, success, new_trades, duration_sec, error_msg)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), mode, int(success), new_trades, duration_sec, error_msg))
    conn.commit()
    conn.close()

def maybe_send_weekly_digest():
    """On Sunday's run, email a 7-day summary regardless of pass/fail — the
    warning-only alerts above catch broken runs, this catches the run not
    happening at all."""
    if datetime.now().weekday() != 6:  # Monday=0 ... Sunday=6
        return

    conn = get_connection()
    c    = conn.cursor()
    c.execute("""
        SELECT run_at, mode, success, new_trades, duration_sec, error_msg
        FROM run_history
        WHERE run_at >= datetime('now', '-7 days')
        ORDER BY run_at
    """)
    rows = c.fetchall()
    conn.close()

    if not rows:
        return

    failures  = [r for r in rows if not r[2]]
    total_new = sum(r[3] or 0 for r in rows)

    lines = [
        f"Weekly digest — {len(rows)} run(s) in the last 7 days, "
        f"{len(failures)} failure(s), {total_new:,} new trades total.",
        "",
    ]
    for run_at, mode, success, new_trades, duration_sec, error_msg in rows:
        status = "OK" if success else "FAILED"
        dur    = f"{duration_sec:.0f}s" if duration_sec else "?"
        nt     = new_trades if new_trades is not None else "?"
        line   = f"  {run_at}  [{status}]  mode={mode}  new_trades={nt}  duration={dur}"
        if error_msg:
            line += f"\n    error: {error_msg}"
        lines.append(line)

    subject = (f"Congressional Trade Tracker — Weekly digest ({len(failures)} failure(s))"
               if failures else
               "Congressional Trade Tracker — Weekly digest (all runs OK)")

    from runner.notifier import send_email
    sent = send_email(subject, "\n".join(lines))
    log(f"Weekly digest email {'sent' if sent else 'NOT sent (see warning above)'}")

# ─────────────────────────────────────────────
# DB HELPERS
# ─────────────────────────────────────────────

def get_trade_counts():
    conn = get_connection()
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
    conn = get_connection()
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

        conn = get_connection()
        c    = conn.cursor()
        c.execute("INSERT INTO scrape_history (run_at, new_trades) VALUES (?, ?)",
                   (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), new_trades))
        conn.commit()
        c.execute("SELECT new_trades FROM scrape_history ORDER BY id DESC LIMIT ?",
                   (SCRAPE_ZERO_DAY_THRESHOLD,))
        recent = [row[0] for row in c.fetchall()]
        conn.close()

        if new_trades > SCRAPE_HIGH_CEILING:
            log(f"WARNING: scrape returned {new_trades} new trades — above the "
                f"{SCRAPE_HIGH_CEILING} sanity ceiling")
            _send_health_alert(
                f"scrape returned unusually high count ({new_trades})",
                f"step_scrape() saw +{new_trades} new trades this run, above the "
                f"{SCRAPE_HIGH_CEILING} sanity ceiling. Could indicate a duplicate-insert "
                f"or DB wipe bug. Check logs/daily_run.log."
            )
        elif len(recent) == SCRAPE_ZERO_DAY_THRESHOLD and all(n == 0 for n in recent):
            log(f"WARNING: {SCRAPE_ZERO_DAY_THRESHOLD} consecutive scrapes with 0 new trades")
            _send_health_alert(
                f"{SCRAPE_ZERO_DAY_THRESHOLD} consecutive scrapes with 0 new trades",
                f"The last {SCRAPE_ZERO_DAY_THRESHOLD} daily runs have all returned 0 new "
                f"trades from Capitol Trades. Zero on a single day is normal (weekends/slow "
                f"news), but not {SCRAPE_ZERO_DAY_THRESHOLD} in a row — check for site layout "
                f"changes, CAPTCHAs, or driver issues. See logs/daily_run.log."
            )

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

        conn = get_connection()
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

        conn = get_connection()
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
        conn = get_connection()
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

        conn = get_connection()
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

        conn   = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM market_features") if cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='market_features'"
        ).fetchone() else None
        conn.close()

        run_market_features(rebuild=False)

        conn   = get_connection()
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

        conn   = get_connection()
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

        conn = get_connection()
        c    = conn.cursor()
        c.execute("SELECT COUNT(*) FROM politicians WHERE score IS NOT NULL")
        scored_before = c.fetchone()[0]
        conn.close()

        log(f"Politicians scored before: {scored_before}")

        scored_list = run_scorer()

        conn = get_connection()
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
    ensure_health_tables()
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

    mode = "score-only" if score_only else ("skip-scrape" if skip_scrape else "full")
    record_run(mode, success=True, new_trades=new_trades, duration_sec=elapsed)
    maybe_send_weekly_digest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Congressional Trade Tracker — Daily Runner")
    parser.add_argument("--skip-scrape", action="store_true",
                        help="Skip scraping step (prices + drawdown + score only)")
    parser.add_argument("--score-only",  action="store_true",
                        help="Run scorer only (fastest, use after manual DB changes)")
    args = parser.parse_args()

    ensure_health_tables()
    run_start = datetime.now()
    mode = "score-only" if args.score_only else ("skip-scrape" if args.skip_scrape else "full")

    try:
        run(skip_scrape=args.skip_scrape, score_only=args.score_only)
    except Exception as e:
        log(f"FATAL ERROR: {e}")
        import traceback
        tb = traceback.format_exc()
        log(tb)

        duration = (datetime.now() - run_start).total_seconds()

        # Email first: it's the actual point of this handler and doesn't touch
        # trades.db, so it can't be defeated by whatever just crashed the run
        # (including a DB lock). record_run() is wrapped separately below —
        # on 2026-08-04 21:45 an unguarded record_run() call here threw its own
        # "database is locked" (same lock that caused the fatal error in the
        # first place), which went uncaught and killed the process before the
        # email ever sent. Neither failure mode should be able to silence the
        # other.
        try:
            from runner.notifier import send_email
            sent = send_email(
                "Congressional Trade Tracker — ALERT: daily run FAILED",
                f"The daily runner crashed with a fatal error:\n\n{e}\n\n{tb}\n\n"
                f"See logs/daily_run.log for full context."
            )
            log(f"Failure alert email {'sent' if sent else 'NOT sent (see warning above)'}")
        except Exception as email_err:
            log(f"WARNING: failed to send failure alert email: {email_err}")

        try:
            record_run(mode, success=False, duration_sec=duration, error_msg=str(e))
        except Exception as record_err:
            log(f"WARNING: failed to record failed run to run_history: {record_err}")

        sys.exit(1)