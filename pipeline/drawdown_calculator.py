# pipeline/drawdown_calculator.py
# Recalculates all drawdown metrics using intraday OHLC from trade_price_paths
# NO yfinance calls - reads entirely from the database
#
# Drawdown logic:
#   WIN  trades: max drawdown from entry until HIGH hits +10% target
#   LOSS trades: drawdown = -10% (stopped out at stop price)
#   OPEN trades: max drawdown from entry to end of path
#
# Overwrites all existing drawdown columns in trades table

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import get_connection

BATCH_SIZE = 100
STOP_PCT   = 10.0
TARGET_PCT = 10.0

# ─────────────────────────────────────────────
# CORE ANALYSIS (OHLC-based)
# ─────────────────────────────────────────────

def analyse_trade_ohlc(path_rows, entry_price):
    """
    Simulate 10/10 strategy and calculate drawdown metrics.

    Uses intraday LOW for stop detection and drawdown tracking.
    Uses intraday HIGH for target detection and profitability.

    Drawdown is only tracked until the trade exits.

    Returns dict with:
        outcome:         'win', 'loss', 'open'
        days_to_exit:    days until stop or target hit (None if open)
        max_drawdown:    max % drop from entry before exit
        hit_5pct:        1 if drawdown exceeded -5% before exit
        hit_10pct:       1 if drawdown exceeded -10% before exit
        hit_15pct:       1 if drawdown exceeded -15% before exit
    """
    result = {
        "outcome":      "open",
        "days_to_exit": None,
        "max_drawdown": 0.0,
        "hit_5pct":     0,
        "hit_10pct":    0,
        "hit_15pct":    0,
    }

    if not path_rows or not entry_price or entry_price <= 0:
        return result

    stop_price   = entry_price * (1 - STOP_PCT   / 100)
    target_price = entry_price * (1 + TARGET_PCT / 100)
    max_drawdown = 0.0

    for day, high, low, close in path_rows:
        if low is None or high is None:
            continue

        # Track intraday drawdown
        dd_pct = (low - entry_price) / entry_price * 100
        if dd_pct < max_drawdown:
            max_drawdown = dd_pct

        # Check stop first (conservative)
        if low <= stop_price:
            result["outcome"]      = "loss"
            result["days_to_exit"] = day
            result["max_drawdown"] = -STOP_PCT  # stopped out at exactly -10%
            result["hit_5pct"]     = 1
            result["hit_10pct"]    = 1
            result["hit_15pct"]    = 1 if max_drawdown <= -15.0 else 0
            return result

        # Check target
        if high >= target_price:
            result["outcome"]      = "win"
            result["days_to_exit"] = day
            result["max_drawdown"] = round(max_drawdown, 4)
            result["hit_5pct"]     = 1 if max_drawdown <= -5.0  else 0
            result["hit_10pct"]    = 0  # can't hit -10% stop if we won
            result["hit_15pct"]    = 0
            return result

    # Neither stop nor target hit — open trade
    result["outcome"]      = "open"
    result["max_drawdown"] = round(max_drawdown, 4)
    result["hit_5pct"]     = 1 if max_drawdown <= -5.0  else 0
    result["hit_10pct"]    = 1 if max_drawdown <= -10.0 else 0
    result["hit_15pct"]    = 1 if max_drawdown <= -15.0 else 0
    return result

# ─────────────────────────────────────────────
# MAIN RUNNER
# ─────────────────────────────────────────────

def run_drawdown_calculator(db_path=None):
    """
    Recalculate all drawdown metrics for every buy trade
    using intraday OHLC from trade_price_paths.
    Overwrites existing values.
    """
    conn   = get_connection(db_path)
    cursor = conn.cursor()

    print("=" * 60)
    print("  Drawdown Calculator (OHLC-based)")
    print(f"  Stop: {STOP_PCT}%  |  Target: {TARGET_PCT}%")
    print("  Drawdown tracked only until trade exits")
    print("  Source: trade_price_paths table")
    print("=" * 60)

    # ── Fetch all buy trades ──────────────────
    print("\nFetching trades...")
    cursor.execute("""
        SELECT trade_id,
               price_at_trade_date,
               price_at_disclosure_date
        FROM trades
        WHERE transaction_type = 'buy'
        AND ticker != ''
        AND (price_at_trade_date IS NOT NULL
             OR price_at_disclosure_date IS NOT NULL)
    """)
    trades = cursor.fetchall()
    print(f"Trades to process: {len(trades):,}")

    # ── Fetch day-1 opens (MOO entry for disc anchor) ────
    print("Loading day-1 opens (MOO entry prices)...")
    cursor.execute("""
        SELECT trade_id, open
        FROM trade_price_paths
        WHERE anchor = 'disc' AND day = 1
        AND open IS NOT NULL AND open > 0
    """)
    day1_open_lookup = {r[0]: r[1] for r in cursor.fetchall()}
    print(f"Day-1 opens loaded: {len(day1_open_lookup):,}")

    # ── Fetch all path rows in bulk ───────────
    print("Loading price paths...")
    cursor.execute("""
        SELECT trade_id, anchor, day, date, high, low, close
        FROM trade_price_paths
        ORDER BY trade_id, anchor, day ASC
    """)
    all_paths = cursor.fetchall()
    print(f"Path rows loaded:  {len(all_paths):,}")

    # Group by trade_id and anchor; also build date lookup for close_date
    paths = defaultdict(lambda: defaultdict(list))
    date_lookup = {}  # (trade_id, anchor, day) -> calendar date string
    for trade_id, anchor, day, date, high, low, close in all_paths:
        paths[trade_id][anchor].append((day, high, low, close))
        date_lookup[(trade_id, anchor, day)] = date

    # ── Process each trade ────────────────────
    print("\nCalculating...\n")

    processed  = 0
    no_paths   = 0
    wins_trade = losses_trade = open_trade = 0
    wins_disc  = losses_disc  = open_disc  = 0

    for trade_id, price_trade, price_disc in trades:

        trade_result = None
        disc_result  = None

        if price_trade and paths[trade_id]["trade"]:
            trade_result = analyse_trade_ohlc(
                paths[trade_id]["trade"], price_trade
            )
            if trade_result["outcome"] == "win":   wins_trade   += 1
            elif trade_result["outcome"] == "loss": losses_trade += 1
            else:                                   open_trade   += 1

        # Use day-1 open (MOO entry) for disc anchor; fall back to disc close
        entry_disc = day1_open_lookup.get(trade_id) or price_disc
        if entry_disc and paths[trade_id]["disc"]:
            disc_result = analyse_trade_ohlc(
                paths[trade_id]["disc"], entry_disc
            )
            if disc_result["outcome"] == "win":   wins_disc   += 1
            elif disc_result["outcome"] == "loss": losses_disc += 1
            else:                                  open_disc   += 1

        if not trade_result and not disc_result:
            no_paths += 1
            processed += 1
            continue

        close_date_disc = None
        if disc_result and disc_result["days_to_exit"] is not None:
            close_date_disc = date_lookup.get((trade_id, "disc", disc_result["days_to_exit"]))

        if trade_result and disc_result:
            cursor.execute("""
                UPDATE trades SET
                    max_drawdown_before_profit = ?,
                    days_to_exit_trade         = ?,
                    drawdown_threshold_5pct    = ?,
                    drawdown_threshold_10pct   = ?,
                    drawdown_threshold_15pct   = ?,
                    max_drawdown_disc          = ?,
                    days_to_exit_disc          = ?,
                    drawdown_disc_5pct         = ?,
                    drawdown_disc_10pct        = ?,
                    drawdown_disc_15pct        = ?,
                    close_date_disc            = ?
                WHERE trade_id = ?
            """, (
                trade_result["max_drawdown"],
                trade_result["days_to_exit"],
                trade_result["hit_5pct"],
                trade_result["hit_10pct"],
                trade_result["hit_15pct"],
                disc_result["max_drawdown"],
                disc_result["days_to_exit"],
                disc_result["hit_5pct"],
                disc_result["hit_10pct"],
                disc_result["hit_15pct"],
                close_date_disc,
                trade_id,
            ))
        elif trade_result:
            cursor.execute("""
                UPDATE trades SET
                    max_drawdown_before_profit = ?,
                    days_to_exit_trade         = ?,
                    drawdown_threshold_5pct    = ?,
                    drawdown_threshold_10pct   = ?,
                    drawdown_threshold_15pct   = ?
                WHERE trade_id = ?
            """, (
                trade_result["max_drawdown"],
                trade_result["days_to_exit"],
                trade_result["hit_5pct"],
                trade_result["hit_10pct"],
                trade_result["hit_15pct"],
                trade_id,
            ))
        elif disc_result:
            cursor.execute("""
                UPDATE trades SET
                    max_drawdown_disc          = ?,
                    days_to_exit_disc          = ?,
                    drawdown_disc_5pct         = ?,
                    drawdown_disc_10pct        = ?,
                    drawdown_disc_15pct        = ?,
                    close_date_disc            = ?
                WHERE trade_id = ?
            """, (
                disc_result["max_drawdown"],
                disc_result["days_to_exit"],
                disc_result["hit_5pct"],
                disc_result["hit_10pct"],
                disc_result["hit_15pct"],
                close_date_disc,
                trade_id,
            ))

        processed += 1
        if processed % BATCH_SIZE == 0:
            conn.commit()
        if processed % 1000 == 0:
            print(f"  Processed {processed:,} / {len(trades):,}...")

    conn.commit()

    # ── Summary ───────────────────────────────
    cursor.execute("""
        SELECT
            ROUND(AVG(CASE WHEN max_drawdown_before_profit < 0 
                      AND max_drawdown_before_profit > -10
                      THEN max_drawdown_before_profit END), 2) as avg_dd_wins_trade,
            ROUND(AVG(CASE WHEN max_drawdown_disc < 0
                      AND max_drawdown_disc > -10
                      THEN max_drawdown_disc END), 2)          as avg_dd_wins_disc,
            SUM(drawdown_threshold_5pct)                       as hit5_trade,
            SUM(drawdown_disc_5pct)                            as hit5_disc,
            SUM(drawdown_threshold_10pct)                      as hit10_trade,
            SUM(drawdown_disc_10pct)                           as hit10_disc
        FROM trades
        WHERE transaction_type = 'buy'
        AND max_drawdown_before_profit IS NOT NULL
    """)
    row = cursor.fetchone()
    conn.close()

    print("\n" + "=" * 60)
    print(f"  Complete! Processed {processed:,} trades")
    print(f"  No path data: {no_paths:,}")
    print()
    print(f"  FROM TRADE DATE:")
    print(f"    Wins: {wins_trade:,}  Losses: {losses_trade:,}  Open: {open_trade:,}")
    print(f"    Avg drawdown on winning trades:  {row[0]}%")
    print(f"    Hit 5%  drawdown: {(row[2] or 0):,}")
    print(f"    Hit 10% drawdown: {(row[4] or 0):,}")
    print()
    print(f"  FROM DISC DATE:")
    print(f"    Wins: {wins_disc:,}  Losses: {losses_disc:,}  Open: {open_disc:,}")
    print(f"    Avg drawdown on winning trades:  {row[1]}%")
    print(f"    Hit 5%  drawdown: {(row[3] or 0):,}")
    print(f"    Hit 10% drawdown: {(row[5] or 0):,}")
    print("=" * 60)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None,
                        help="DB path override (default: data/trades.db)")
    args = parser.parse_args()
    run_drawdown_calculator(db_path=args.db)