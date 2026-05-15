# pipeline/repeat_buy_features.py
#
# Computes point-in-time repeat / routine buy features for each buy trade.
# All features use only trades with trade_date strictly before the current trade_date.
#
# Columns written:
#   seq_position       INTEGER  1 = first buy of this ticker by this politician, 2 = second, etc.
#   gap_since_prev_buy INTEGER  Days since prior buy of same ticker (NULL if first)
#   prev_trade_open    INTEGER  1 if prior position was still open at this disclosure_date
#   prior_buy_count    INTEGER  Count of prior buys (= seq_position - 1)
#   prior_median_gap   REAL     Median gap (days) between prior consecutive buys (NULL if < 2 prior)
#   is_routine_buyer   INTEGER  1 if prior_buy_count >= 3 AND prior_median_gap <= 92
#
# prev_trade_open uses the same 180-day cap as repeat_buy_flagger.py:
#   if close_date_disc is NULL, effective close = disclosure_date + 180 days.
#
# Idempotent — overwrites all values on every run.
#
# Usage:
#   python -m pipeline.repeat_buy_features
#   python -m pipeline.repeat_buy_features --dry-run

import argparse
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH     = Path(__file__).parent.parent / "data" / "trades.db"
COMMIT_N    = 500
WINDOW_DAYS = 180


def _to_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _days(d1_str, d2_str):
    d1, d2 = _to_date(d1_str), _to_date(d2_str)
    if d1 is None or d2 is None:
        return None
    return (d2 - d1).days


def _effective_close(close_date_str, disc_date_str):
    """Effective close date of a position.
    If close_date_disc is NULL, cap at disclosure_date + 180 days (mirrors repeat_buy_flagger)."""
    if close_date_str:
        return close_date_str
    d = _to_date(disc_date_str)
    if d is None:
        return None
    return str(d + timedelta(days=WINDOW_DAYS))


def ensure_columns(c):
    existing = {r[1] for r in c.execute("PRAGMA table_info(trades)").fetchall()}
    new_cols = [
        ("seq_position",       "INTEGER"),
        ("gap_since_prev_buy", "INTEGER"),
        ("prev_trade_open",    "INTEGER"),
        ("prior_buy_count",    "INTEGER"),
        ("prior_median_gap",   "REAL"),
        ("is_routine_buyer",   "INTEGER"),
    ]
    for name, typ in new_cols:
        if name not in existing:
            c.execute(f"ALTER TABLE trades ADD COLUMN {name} {typ}")


def run(db_path=None, dry_run=False):
    db   = str(db_path if db_path else DB_PATH)
    conn = sqlite3.connect(db)
    c    = conn.cursor()

    print("=" * 60)
    print("  Repeat Buy Features")
    print(f"  DB: {db}")
    if dry_run:
        print("  DRY RUN — no writes")
    print("=" * 60)

    if not dry_run:
        ensure_columns(c)
        conn.commit()

    c.execute("""
        SELECT trade_id, politician_id, ticker,
               trade_date, disclosure_date, close_date_disc
        FROM trades
        WHERE transaction_type = 'buy'
          AND trade_date  IS NOT NULL AND trade_date  != ''
          AND ticker      IS NOT NULL AND ticker      != ''
        ORDER BY politician_id, ticker, trade_date, disclosure_date
    """)
    rows = c.fetchall()
    print(f"\nBuy trades with trade_date: {len(rows):,}")

    groups = defaultdict(list)
    for trade_id, pol_id, ticker, trade_date, disc_date, close_date in rows:
        groups[(pol_id, ticker)].append({
            "trade_id":   trade_id,
            "trade_date": trade_date,
            "disc_date":  disc_date,
            "close_date": close_date,
        })
    print(f"Distinct (politician, ticker) pairs: {len(groups):,}")

    updates = []

    for (pol_id, ticker), trades in groups.items():
        trades.sort(key=lambda t: (t["trade_date"], t["disc_date"] or ""))

        prior = []  # running list of trades with trade_date < current
        i = 0

        while i < len(trades):
            current_td = trades[i]["trade_date"]

            # Collect all trades sharing this trade_date
            same_day = []
            while i < len(trades) and trades[i]["trade_date"] == current_td:
                same_day.append(trades[i])
                i += 1

            # Features based on prior history (strictly before current_td)
            prior_buy_count = len(prior)
            seq_position    = prior_buy_count + 1

            if prior:
                last = prior[-1]
                gap_since_prev_buy = _days(last["trade_date"], current_td)

                if len(prior) >= 2:
                    gaps = [
                        _days(prior[j - 1]["trade_date"], prior[j]["trade_date"])
                        for j in range(1, len(prior))
                    ]
                    gaps = [g for g in gaps if g is not None and g >= 0]
                    prior_median_gap = statistics.median(gaps) if gaps else None
                else:
                    prior_median_gap = None

                last_eff_close = _effective_close(last["close_date"], last["disc_date"])
            else:
                gap_since_prev_buy = None
                prior_median_gap   = None
                last_eff_close     = None

            is_routine = (
                1 if prior_buy_count >= 3
                     and prior_median_gap is not None
                     and prior_median_gap <= 92
                else 0
            )

            for trade in same_day:
                if not prior:
                    prev_trade_open = 0
                elif last_eff_close is None or trade["disc_date"] is None:
                    prev_trade_open = None
                else:
                    prev_trade_open = 1 if last_eff_close >= trade["disc_date"] else 0

                updates.append((
                    seq_position,
                    gap_since_prev_buy,
                    prev_trade_open,
                    prior_buy_count,
                    prior_median_gap,
                    is_routine,
                    trade["trade_id"],
                ))

            prior.extend(same_day)

    print(f"Updates computed: {len(updates):,}")

    if dry_run:
        print("\nSample (first 5):")
        headers = ("seq", "gap", "prev_open", "prior_n", "med_gap", "routine", "trade_id")
        print(f"  {headers}")
        for u in updates[:5]:
            print(f"  {u}")
        conn.close()
        return len(updates)

    for idx, (seq, gap, prev_open, prior_n, med_gap, routine, tid) in enumerate(updates):
        c.execute("""
            UPDATE trades SET
                seq_position       = ?,
                gap_since_prev_buy = ?,
                prev_trade_open    = ?,
                prior_buy_count    = ?,
                prior_median_gap   = ?,
                is_routine_buyer   = ?
            WHERE trade_id = ?
        """, (seq, gap, prev_open, prior_n, med_gap, routine, tid))
        if (idx + 1) % COMMIT_N == 0:
            conn.commit()
    conn.commit()

    print("\nDone.")
    c.execute("SELECT COUNT(*) FROM trades WHERE seq_position IS NOT NULL")
    print(f"  seq_position populated:       {c.fetchone()[0]:,}")
    c.execute("SELECT COUNT(*) FROM trades WHERE gap_since_prev_buy IS NOT NULL")
    print(f"  gap_since_prev_buy populated: {c.fetchone()[0]:,}")
    c.execute("SELECT COUNT(*) FROM trades WHERE is_routine_buyer = 1")
    print(f"  is_routine_buyer = 1:         {c.fetchone()[0]:,}")

    c.execute("""
        SELECT seq_position, COUNT(*) as n
        FROM trades WHERE seq_position IS NOT NULL
        GROUP BY seq_position ORDER BY seq_position
        LIMIT 10
    """)
    print("\n  seq_position distribution:")
    for seq, n in c.fetchall():
        print(f"    {seq}: {n:,}")

    conn.close()
    return len(updates)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
