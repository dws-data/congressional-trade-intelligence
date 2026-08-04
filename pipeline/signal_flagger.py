# pipeline/signal_flagger.py
#
# Flags trades that meet the live execution filter (execution/rules.md
# Signal Filter section, decided 2026-08-03):
#   cluster_count_td >= 2 AND abs_pct_move_before_disclosure >= 15
#
# Stores the result as `signal_flag` (0/1) on trades — recomputed in full
# every run since it's a cheap boolean derived entirely from existing
# columns (cluster_count_td, abs_pct_move_before_disclosure).
#
# Also tracks which flagged trades have already been surfaced via
# `signal_notifications`, so callers (daily_runner.py) only alert on
# genuinely new flags rather than the whole historical backlog.
#
# On the very first run, all currently-flagged trades are seeded into
# signal_notifications as already-notified (no email for the historical
# backlog) — only trades flagged from that point on come back as "new".
#
# Usage:
#   python -m pipeline.signal_flagger

import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "trades.db"

CLUSTER_MIN  = 2
ABS_MOVE_MIN = 15


def ensure_schema(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
    if "signal_flag" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN signal_flag INTEGER")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_notifications (
            trade_id    TEXT PRIMARY KEY,
            notified_at TEXT NOT NULL
        )
    """)
    conn.commit()


def mark_notified(trade_ids, db_path=None):
    if not trade_ids:
        return
    db  = db_path or DB_PATH
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(db, timeout=30)
    conn.executemany(
        "INSERT OR IGNORE INTO signal_notifications (trade_id, notified_at) VALUES (?, ?)",
        [(tid, now) for tid in trade_ids]
    )
    conn.commit()
    conn.close()


def run_signal_flagger(db_path=None):
    """
    Recompute signal_flag for all trades. Returns (total_flagged, newly_flagged)
    where newly_flagged is a list of
    (trade_id, ticker, politician_name, disclosure_date, cluster_count_td,
     abs_pct_move_before_disclosure) tuples not yet recorded in
    signal_notifications. Caller is responsible for calling mark_notified()
    once it has actually surfaced/emailed them.
    """
    db   = db_path or DB_PATH
    conn = sqlite3.connect(db, timeout=30)
    ensure_schema(conn)
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM signal_notifications")
    first_run = cursor.fetchone()[0] == 0

    cursor.execute("""
        UPDATE trades
        SET signal_flag = CASE
            WHEN transaction_type = 'buy'
             AND filing_violation = 'compliant'
             AND asset_type = 'stock'
             AND cluster_count_td >= ?
             AND abs_pct_move_before_disclosure >= ?
            THEN 1 ELSE 0
        END
    """, (CLUSTER_MIN, ABS_MOVE_MIN))
    conn.commit()

    cursor.execute("SELECT COUNT(*) FROM trades WHERE signal_flag = 1")
    total_flagged = cursor.fetchone()[0]

    if first_run:
        cursor.execute("SELECT trade_id FROM trades WHERE signal_flag = 1")
        existing_ids = [r[0] for r in cursor.fetchall()]
        conn.close()
        mark_notified(existing_ids, db_path=db)
        return total_flagged, []

    cursor.execute("""
        SELECT t.trade_id, t.ticker, p.name, t.disclosure_date,
               t.cluster_count_td, t.abs_pct_move_before_disclosure
        FROM trades t
        JOIN politicians p ON t.politician_id = p.politician_id
        LEFT JOIN signal_notifications n ON n.trade_id = t.trade_id
        WHERE t.signal_flag = 1 AND n.trade_id IS NULL
        ORDER BY t.disclosure_date
    """)
    newly_flagged = cursor.fetchall()
    conn.close()
    return total_flagged, newly_flagged


if __name__ == "__main__":
    total, newly = run_signal_flagger()
    print(f"Total flagged trades: {total:,}")
    print(f"Newly flagged (not yet notified): {len(newly)}")
    for row in newly:
        print(f"  {row}")
