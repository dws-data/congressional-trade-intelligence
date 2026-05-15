# pipeline/politician_features.py
#
# Computes point-in-time politician-level features for each buy trade.
# All features use only trades with trade_date strictly before the current trade_date.
#
# Columns written to trades table:
#   pol_age_days         INTEGER  Days since politician's first ever buy to this trade_date (0 if first)
#   days_since_last_buy  INTEGER  Days since politician's last buy of any ticker (NULL if first)
#   trades_last_90d      INTEGER  Buy count in 90 days prior to this trade_date
#   trades_last_365d     INTEGER  Buy count in 365 days prior to this trade_date
#   pit_ticker_conc_pct  REAL     % of prior buys in single most-bought ticker (NULL if no prior)
#   pit_ticker_hhi       REAL     Herfindahl-Hirschman Index across prior buy tickers (0=diversified, 1=single)
#   pit_repeat_rate      REAL     % of prior buys flagged repeat_before_close=1
#   pit_avg_filing_lag   REAL     Mean filing_lag_days across prior buys
#   pit_late_filing_rate REAL     % of prior buys with filing_lag_days > 30
#   pit_trade_frequency  REAL     Prior buys per year (prior_count / pol_age_years)
#   pit_win_rate         REAL     Win rate on resolved prior buys (drawdown_disc_10pct=0 = WIN)
#   pit_avg_dd           REAL     Mean max_drawdown_disc on resolved prior buys
#   pit_large_trade_wr   REAL     Win rate on resolved prior buys with size_midpoint >= 50000
#
# Uses raw compliant buy history — no scorer filters (cluster, repeat exclusions).
# Consistent with the ML training set definition (all compliant priced stock buys).
#
# Idempotent — overwrites all values on every run.
#
# Usage:
#   python -m pipeline.politician_features
#   python -m pipeline.politician_features --dry-run

import argparse
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH               = Path(__file__).parent.parent / "data" / "trades.db"
COMMIT_N              = 500
LARGE_TRADE_THRESHOLD = 50_000


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


def _hhi(ticker_counts, total):
    if total == 0:
        return None
    return round(sum((c / total) ** 2 for c in ticker_counts.values()), 6)


def ensure_columns(c):
    existing = {r[1] for r in c.execute("PRAGMA table_info(trades)").fetchall()}
    new_cols = [
        ("pol_age_days",         "INTEGER"),
        ("days_since_last_buy",  "INTEGER"),
        ("trades_last_90d",      "INTEGER"),
        ("trades_last_365d",     "INTEGER"),
        ("pit_ticker_conc_pct",  "REAL"),
        ("pit_ticker_hhi",       "REAL"),
        ("pit_repeat_rate",      "REAL"),
        ("pit_avg_filing_lag",   "REAL"),
        ("pit_late_filing_rate", "REAL"),
        ("pit_trade_frequency",  "REAL"),
        ("pit_win_rate",         "REAL"),
        ("pit_avg_dd",           "REAL"),
        ("pit_large_trade_wr",   "REAL"),
    ]
    for name, typ in new_cols:
        if name not in existing:
            c.execute(f"ALTER TABLE trades ADD COLUMN {name} {typ}")


def run(db_path=None, dry_run=False):
    db   = str(db_path if db_path else DB_PATH)
    conn = sqlite3.connect(db)
    c    = conn.cursor()

    print("=" * 60)
    print("  Politician Point-in-Time Features")
    print(f"  DB: {db}")
    if dry_run:
        print("  DRY RUN — no writes")
    print("=" * 60)

    if not dry_run:
        ensure_columns(c)
        conn.commit()

    c.execute("""
        SELECT trade_id, politician_id, ticker, trade_date, disclosure_date,
               filing_lag_days, repeat_before_close,
               size_midpoint, drawdown_disc_10pct, max_drawdown_disc
        FROM trades
        WHERE transaction_type = 'buy'
          AND trade_date  IS NOT NULL AND trade_date  != ''
          AND ticker      IS NOT NULL AND ticker      != ''
        ORDER BY politician_id, trade_date, disclosure_date
    """)
    rows = c.fetchall()
    print(f"\nBuy trades loaded: {len(rows):,}")

    by_pol = defaultdict(list)
    for row in rows:
        tid, pol_id, ticker, td, disc, lag, repeat, size, dd10, max_dd = row
        by_pol[pol_id].append({
            "trade_id": tid,
            "ticker":   ticker,
            "td":       td,
            "disc":     disc,
            "lag":      lag,
            "repeat":   repeat,
            "size":     size,
            "dd10":     dd10,
            "max_dd":   max_dd,
        })
    print(f"Politicians: {len(by_pol):,}")

    updates = []

    for pol_id, trades in by_pol.items():
        trades.sort(key=lambda t: (t["td"], t["disc"] or ""))
        first_td = trades[0]["td"]

        prior        = []
        ticker_counts = defaultdict(int)  # running ticker frequency count
        i = 0

        while i < len(trades):
            current_td = trades[i]["td"]
            same_day   = []
            while i < len(trades) and trades[i]["td"] == current_td:
                same_day.append(trades[i])
                i += 1

            total_prior = len(prior)

            # ── Age & recency ────────────────────────────────
            age        = _days(first_td, current_td) or 0
            days_since = _days(prior[-1]["td"], current_td) if prior else None

            cur_date   = _to_date(current_td)
            if cur_date:
                cutoff_90  = str(cur_date - timedelta(days=90))
                cutoff_365 = str(cur_date - timedelta(days=365))
                last_90    = sum(1 for t in prior if t["td"] >= cutoff_90)
                last_365   = sum(1 for t in prior if t["td"] >= cutoff_365)
            else:
                last_90 = last_365 = 0

            if total_prior > 0:
                # ── Concentration ────────────────────────────
                max_ticker = max(ticker_counts.values())
                conc_pct   = round(max_ticker / total_prior * 100, 4)
                hhi        = _hhi(ticker_counts, total_prior)

                # ── Repeat rate ──────────────────────────────
                repeat_n    = sum(1 for t in prior if t["repeat"] == 1)
                repeat_rate = round(repeat_n / total_prior * 100, 4)

                # ── Filing lag ───────────────────────────────
                lags     = [t["lag"] for t in prior if t["lag"] is not None]
                avg_lag  = round(sum(lags) / len(lags), 4) if lags else None
                late_pct = round(
                    sum(1 for l in lags if l > 30) / len(lags) * 100, 4
                ) if lags else None

                # ── Trade frequency ──────────────────────────
                freq = round(total_prior / (age / 365.25), 4) if age > 0 else None

                # ── Point-in-time performance ────────────────
                resolved = [t for t in prior if t["dd10"] is not None]
                if resolved:
                    wins     = sum(1 for t in resolved if t["dd10"] == 0)
                    pit_wr   = round(wins / len(resolved) * 100, 4)
                    dds      = [t["max_dd"] for t in resolved if t["max_dd"] is not None]
                    pit_dd   = round(sum(dds) / len(dds), 4) if dds else None
                    large    = [t for t in resolved
                                if t["size"] is not None and t["size"] >= LARGE_TRADE_THRESHOLD]
                    pit_lg   = round(
                        sum(1 for t in large if t["dd10"] == 0) / len(large) * 100, 4
                    ) if large else None
                else:
                    pit_wr = pit_dd = pit_lg = None
            else:
                conc_pct = hhi = repeat_rate = avg_lag = late_pct = freq = None
                pit_wr   = pit_dd = pit_lg = None

            for trade in same_day:
                updates.append((
                    age, days_since, last_90, last_365,
                    conc_pct, hhi, repeat_rate, avg_lag, late_pct, freq,
                    pit_wr, pit_dd, pit_lg,
                    trade["trade_id"],
                ))

            # Update running state after processing same_day
            for trade in same_day:
                prior.append(trade)
                ticker_counts[trade["ticker"]] += 1

    print(f"Updates computed: {len(updates):,}")

    if dry_run:
        cols = ("age", "days_since", "90d", "365d", "conc%", "hhi",
                "rpt%", "lag", "late%", "freq", "wr", "dd", "lg_wr", "trade_id")
        print(f"\nSample (first 3):")
        print(f"  {cols}")
        for u in updates[:3]:
            print(f"  {u}")
        conn.close()
        return len(updates)

    for idx, vals in enumerate(updates):
        c.execute("""
            UPDATE trades SET
                pol_age_days         = ?,
                days_since_last_buy  = ?,
                trades_last_90d      = ?,
                trades_last_365d     = ?,
                pit_ticker_conc_pct  = ?,
                pit_ticker_hhi       = ?,
                pit_repeat_rate      = ?,
                pit_avg_filing_lag   = ?,
                pit_late_filing_rate = ?,
                pit_trade_frequency  = ?,
                pit_win_rate         = ?,
                pit_avg_dd           = ?,
                pit_large_trade_wr   = ?
            WHERE trade_id = ?
        """, vals)
        if (idx + 1) % COMMIT_N == 0:
            conn.commit()
    conn.commit()

    print("\nDone.")
    c.execute("SELECT COUNT(*) FROM trades WHERE pol_age_days IS NOT NULL")
    print(f"  pol_age_days populated:      {c.fetchone()[0]:,}")
    c.execute("SELECT COUNT(*) FROM trades WHERE pit_win_rate IS NOT NULL")
    print(f"  pit_win_rate populated:      {c.fetchone()[0]:,}")
    c.execute("SELECT COUNT(*) FROM trades WHERE days_since_last_buy IS NOT NULL")
    print(f"  days_since_last_buy:         {c.fetchone()[0]:,}")
    c.execute("SELECT COUNT(*) FROM trades WHERE pit_large_trade_wr IS NOT NULL")
    print(f"  pit_large_trade_wr:          {c.fetchone()[0]:,}")

    # Spot-check: compare pit_win_rate for last trade vs politicians table
    c.execute("""
        SELECT p.name, p.win_rate_trade,
               t.pit_win_rate, t.trade_date
        FROM trades t
        JOIN politicians p ON p.politician_id = t.politician_id
        WHERE t.transaction_type = 'buy'
          AND t.pit_win_rate IS NOT NULL
          AND p.win_rate_trade IS NOT NULL
        GROUP BY t.politician_id
        HAVING t.trade_date = MAX(t.trade_date)
        ORDER BY p.total_trades DESC
        LIMIT 8
    """)
    print("\n  Spot-check — pit_win_rate on latest trade vs leaderboard WR:")
    print(f"  {'Name':<30} {'Leaderboard':>12}  {'PIT (latest)':>13}")
    print("  " + "-" * 58)
    for name, lb_wr, pit_wr, td in c.fetchall():
        print(f"  {name:<30} {lb_wr:>11.1f}%  {pit_wr:>12.1f}%")

    conn.close()
    return len(updates)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
