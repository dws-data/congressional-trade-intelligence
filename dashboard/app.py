# dashboard/app.py
# Congressional Trade Tracker — Main Dashboard
# Run with: streamlit run dashboard/app.py

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st
import pandas as pd
from collections import defaultdict
from datetime import date, timedelta, datetime
from pipeline.committee_config import COMMITTEE_NAMES
from db import get_connection

# ─────────────────────────────────────────────
# COMMITTEE CONFIDENCE TIERS
# Based on win rate analysis (qa/committee_analysis.py)
# ─────────────────────────────────────────────

COMMITTEE_TIERS = {
    # STRONG — meaningful edge observed
    "House Natural Resources":               "STRONG",
    "House Ways & Means":                    "STRONG",
    "House Intelligence (Permanent Select)": "STRONG",
    "Senate Intelligence (Select)":          "STRONG",
    "House Foreign Affairs":                 "STRONG",
    "Senate Foreign Relations":              "STRONG",
    # WEAK — at or below random; flag but limited signal
    "Senate HELP":                           "WEAK",
    "House Homeland Security":               "WEAK",
    "House Judiciary":                       "WEAK",
    "Senate Judiciary":                      "WEAK",
}

def _comm_tier(committee_relevance_str):
    """Return highest tier for a pipe-separated committee_relevance string."""
    if not committee_relevance_str:
        return None
    names = [c.strip() for c in str(committee_relevance_str).split(" | ")]
    if any(COMMITTEE_TIERS.get(n) == "STRONG" for n in names):
        return "STRONG"
    if any(COMMITTEE_TIERS.get(n) == "WEAK" for n in names):
        return "WEAK"
    return "NEUTRAL"

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

STOP_PCT   = 10.0
TARGET_PCT = 10.0

st.set_page_config(
    page_title="Congressional Trade Tracker",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────

if "selected_pol_id" not in st.session_state:
    st.session_state.selected_pol_id   = None
if "selected_pol_name" not in st.session_state:
    st.session_state.selected_pol_name = None

# ─────────────────────────────────────────────
# STYLING
# ─────────────────────────────────────────────

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');

html, body, [class*="css"] {
    font-family: 'IBM Plex Sans', monospace;
    background-color: #0a0a0a;
    color: #e0e0e0;
}
.stApp { background-color: #0a0a0a; }
.block-container { max-width: 100% !important; padding-left: 2rem !important; padding-right: 2rem !important; }

section[data-testid="stSidebar"] {
    background-color: #0f0f0f;
    border-right: 1px solid #1a1a1a;
}
section[data-testid="stSidebar"] * { color: #e0e0e0 !important; }

.dash-header {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px; color: #f5a623;
    letter-spacing: 3px; text-transform: uppercase;
    border-bottom: 1px solid #1e1e1e;
    padding-bottom: 8px; margin-bottom: 4px;
}
.dash-title {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 28px; font-weight: 600;
    color: #ffffff; letter-spacing: -0.5px;
    margin: 0; padding: 0;
}
.dash-subtitle {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px; color: #555;
    letter-spacing: 2px; text-transform: uppercase; margin-top: 4px;
}
.metric-card {
    background: #0f0f0f; border: 1px solid #1e1e1e;
    border-top: 2px solid #f5a623;
    padding: 16px 20px; border-radius: 2px;
}
.metric-label {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px; color: #555;
    letter-spacing: 2px; text-transform: uppercase; margin-bottom: 6px;
}
.metric-value {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 24px; font-weight: 600; color: #f5a623;
}
.metric-sub {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px; color: #444; margin-top: 4px;
}
.section-divider {
    border: none; border-top: 1px solid #1a1a1a; margin: 20px 0;
}
.filter-label {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px; color: #f5a623;
    letter-spacing: 2px; text-transform: uppercase; margin-bottom: 8px;
}
.stSelectbox label, .stSlider label, .stRadio label,
.stCheckbox label, .stMultiSelect label {
    font-family: 'IBM Plex Mono', monospace !important;
    font-size: 10px !important; color: #555 !important;
    letter-spacing: 2px !important; text-transform: uppercase !important;
}
.stSlider > div > div > div { background-color: #f5a623 !important; }
div[data-baseweb="select"] {
    background-color: #0f0f0f !important; border-color: #222 !important;
}
div[data-baseweb="select"] * {
    background-color: #0f0f0f !important; color: #e0e0e0 !important;
    font-family: 'IBM Plex Mono', monospace !important; font-size: 12px !important;
}
.stRadio > div { flex-direction: row; gap: 16px; }
hr { border-color: #1a1a1a; }

[data-testid="stDataFrame"] * {
    font-size: 11px !important;
    font-family: 'IBM Plex Mono', monospace !important;
}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────

@st.cache_data(ttl=3600, persist="disk")
def load_leaderboard():
    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT politician_id, name, party, chamber, state,
               score,
               win_rate_trade          as win_rate,
               total_trades,
               score_etf, win_rate_etf, etf_trade_count,
               avg_dd,
               large_trade_wr,
               COALESCE(compliant_trades_all, 0) as compliant_trades,
               avg_filing_lag,
               COALESCE(late_filings, 0)         as late_filings,
               COALESCE(comm_aligned, 0)          as comm_aligned,
               (SELECT MAX(t.disclosure_date) FROM trades t
                WHERE t.politician_id = politicians.politician_id
                AND t.transaction_type = 'buy') as last_trade_date
        FROM politicians
        WHERE score IS NOT NULL OR score_etf IS NOT NULL
        ORDER BY COALESCE(score, 0) DESC
    """, conn)
    conn.close()
    df["comm_align_pct"] = (
        df["comm_aligned"] / df["compliant_trades"].replace(0, float("nan")) * 100
    ).round(1).fillna(0.0)
    return df


@st.cache_data(ttl=3600, persist="disk")
def load_summary_stats():
    """
    Reads scorer output directly from the politicians table.

    IMPORTANT — why we do it this way:
      scorer.py calculates win_rate as wins / (wins + losses + opens)
      Open trades are genuinely undecided and correctly dilute the rate.
      max_drawdown_disc cannot distinguish wins from opens — both show > -10.
      Re-deriving win rates from trades table will always give wrong numbers.
      Single source of truth: politicians.win_rate_trade + politicians.total_trades.

    Verified ground truth: Overall 56.6%, R 57.9%, D 56.0%
    """
    conn = get_connection()
    c    = conn.cursor()

    c.execute("SELECT COUNT(*) FROM politicians WHERE score IS NOT NULL")
    scored = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM politicians")
    total_pols = c.fetchone()[0]

    c.execute("SELECT COUNT(DISTINCT ticker) FROM trades WHERE transaction_type='buy' AND filing_violation='compliant'")
    tickers = c.fetchone()[0]

    c.execute("SELECT MAX(disclosure_date) FROM trades WHERE disclosure_date != '' AND transaction_type = 'buy'")
    latest = c.fetchone()[0]

    # Total deduped compliant buy positions (for display only — not used for WR)
    c.execute("""
        SELECT COUNT(*) FROM (
            SELECT 1 FROM trades
            WHERE transaction_type='buy' AND filing_violation='compliant'
            AND price_at_disclosure_date IS NOT NULL
            AND disclosure_date IS NOT NULL AND disclosure_date != ''
            GROUP BY politician_id, ticker, disclosure_date, price_at_disclosure_date
        )
    """)
    total_buys = c.fetchone()[0]

    # Avg DD — still read from trades table but only for context, not WR
    c.execute("""
        SELECT AVG(dd) FROM (
            SELECT AVG(max_drawdown_disc) as dd
            FROM trades
            WHERE transaction_type='buy' AND filing_violation='compliant'
            AND price_at_disclosure_date IS NOT NULL
            AND disclosure_date IS NOT NULL AND disclosure_date != ''
            AND max_drawdown_disc IS NOT NULL
            AND max_drawdown_disc > -10 AND max_drawdown_disc < 0
            GROUP BY politician_id, ticker, disclosure_date, price_at_disclosure_date
        )
    """)
    avg_dd = c.fetchone()[0]

    # Win rates — read DIRECTLY from scorer output in politicians table
    # win_rate_trade is wins/(wins+losses+opens)*100 as computed by scorer.py
    rows = c.execute("""
        SELECT party,
               SUM(ROUND(win_rate_trade / 100.0 * total_trades)) as wins,
               SUM(total_trades) as total
        FROM politicians
        WHERE score IS NOT NULL AND win_rate_trade IS NOT NULL
        GROUP BY party
    """).fetchall()

    total_wins  = sum(r[1] or 0 for r in rows)
    total_all   = sum(r[2] or 0 for r in rows)
    overall_wr  = round(total_wins / total_all * 100, 1) if total_all > 0 else None

    party_wr = {}
    for party, pw, pt in rows:
        if party in ("Republican", "Democrat") and pt > 0:
            party_wr[party] = round(pw / pt * 100, 1)

    conn.close()
    return {
        "scored":     scored,    "total_pols": total_pols,
        "tickers":    tickers,   "latest":     latest,
        "overall_wr": overall_wr,"avg_dd":     avg_dd,
        "total_buys": total_buys,"party_wr":   party_wr,
        "wins":       int(total_wins), "losses": int(total_all - total_wins),
    }

@st.cache_data(ttl=300)
def load_politician_profile(pol_id):
    conn = get_connection()

    # Basic info
    pol = pd.read_sql_query("""
        SELECT politician_id, name, party, chamber, state, score, win_rate_trade,
               score_etf, win_rate_etf, etf_trade_count, total_trades
        FROM politicians WHERE politician_id = ?
    """, conn, params=(pol_id,)).iloc[0]

    # Deduplicated compliant buys — same filters as scorer
    trades = pd.read_sql_query("""
        SELECT MIN(trade_id) as trade_id, ticker, disclosure_date,
               price_at_disclosure_date as entry,
               AVG(max_drawdown_disc) as dd,
               MAX(size_midpoint) as size,
               MAX(committee_relevance) as committee_relevance,
               MAX(asset_type) as asset_type,
               MIN(COALESCE(repeat_before_close, 0)) as repeat_before_close
        FROM trades
        WHERE politician_id = ?
        AND transaction_type = 'buy'
        AND filing_violation = 'compliant'
        AND price_at_disclosure_date IS NOT NULL
        AND disclosure_date IS NOT NULL
        AND disclosure_date != ''
        AND trade_date IS NOT NULL
        AND trade_date != ''
        AND (price_fetch_failed IS NULL OR price_fetch_failed = 0)
        AND (politician_id, trade_date) NOT IN (
            SELECT politician_id, trade_date FROM trades
            WHERE transaction_type = 'buy' AND filing_violation = 'compliant'
            AND trade_date IS NOT NULL AND trade_date != ''
            GROUP BY politician_id, trade_date
            HAVING COUNT(DISTINCT ticker) >= 10
        )
        AND (politician_id, disclosure_date) NOT IN (
            SELECT politician_id, disclosure_date FROM trades
            WHERE transaction_type = 'buy' AND filing_violation = 'compliant'
            AND disclosure_date IS NOT NULL AND disclosure_date != ''
            GROUP BY politician_id, disclosure_date
            HAVING COUNT(DISTINCT ticker) >= 20
        )
        GROUP BY ticker, disclosure_date, price_at_disclosure_date
        ORDER BY disclosure_date DESC
    """, conn, params=(pol_id,))

    # Committee memberships for this politician — names resolved via COMMITTEE_NAMES
    committees = pd.read_sql_query("""
        SELECT DISTINCT thomas_id
        FROM committee_memberships
        WHERE bioguide = ?
        AND confidence = 'high'
        AND end_date IS NULL
        ORDER BY thomas_id
    """, conn, params=(pol_id,))

    # Path rows for simulation + day-1 opens for MOO entry prices
    if len(trades) > 0:
        trade_ids    = trades["trade_id"].tolist()
        placeholders = ",".join("?" * len(trade_ids))
        paths_df = pd.read_sql_query(f"""
            SELECT trade_id, day, date, high, low, close
            FROM trade_price_paths
            WHERE anchor = 'disc'
            AND trade_id IN ({placeholders})
            ORDER BY trade_id, day ASC
        """, conn, params=trade_ids)
        day1_opens = pd.read_sql_query(f"""
            SELECT trade_id, open as day1_open
            FROM trade_price_paths
            WHERE anchor = 'disc' AND day = 1
            AND open IS NOT NULL AND open > 0
            AND trade_id IN ({placeholders})
        """, conn, params=tuple(trade_ids))
        trades = trades.merge(day1_opens, on="trade_id", how="left")
        trades["entry"] = trades["day1_open"].where(
            trades["day1_open"].notna() & (trades["day1_open"] > 0),
            other=trades["entry"]
        )
        trades = trades.drop(columns=["day1_open"])
    else:
        paths_df = pd.DataFrame()

    conn.close()
    return pol, trades, paths_df, committees


def simulate_trades(trades_df, paths_df):
    """Simulate 10/10 outcomes for a politician's trades."""
    paths_by_trade = defaultdict(list)
    dates_by_trade = defaultdict(dict)
    for _, row in paths_df.iterrows():
        tid = row["trade_id"]
        paths_by_trade[tid].append((row["day"], row["high"], row["low"], row["close"]))
        dates_by_trade[tid][row["day"]] = row["date"]

    results = []
    for _, t in trades_df.iterrows():
        tid    = t["trade_id"]
        entry  = t["entry"]
        path   = paths_by_trade.get(tid, [])
        stop   = entry * (1 - STOP_PCT   / 100)
        target = entry * (1 + TARGET_PCT / 100)

        outcome   = "open"
        exit_day  = None
        exit_date = None

        for day, high, low, close in path:
            if low is None or high is None:
                continue
            if low <= stop:
                outcome   = "LOSS"
                exit_day  = day
                exit_date = dates_by_trade[tid].get(day)
                break
            if high >= target:
                outcome   = "WIN"
                exit_day  = day
                exit_date = dates_by_trade[tid].get(day)
                break

        results.append({
            "ticker":               t["ticker"],
            "disc_date":            t["disclosure_date"],
            "entry":                entry,
            "stop":                 round(stop, 2),
            "target":               round(target, 2),
            "size":                 t["size"],
            "dd":                   t["dd"],
            "outcome":              outcome,
            "exit_day":             exit_day,
            "exit_date":            exit_date,
            "committee_relevance":  t.get("committee_relevance"),
            "asset_type":           t.get("asset_type"),
            "repeat_before_close":  t.get("repeat_before_close"),
        })

    return pd.DataFrame(results)


# ─────────────────────────────────────────────
# COMBO STATS (custom stop/target re-simulation)
# ─────────────────────────────────────────────

@st.cache_data(ttl=3600, persist="disk")
def load_combo_stats(stop_pct, target_pct, min_size=0):
    """
    Re-simulate all compliant buy trades with custom stop/target.
    Returns df with politician_id, win_rate, avg_dd, compliant_trades.

    Fast path for 10/10: uses pre-computed max_drawdown_disc / days_to_exit_disc
    from the trades table — no path data needed, instant.
    Custom combos: full re-simulation from trade_price_paths.
    """
    conn = get_connection()

    if stop_pct == 10 and target_pct == 10:
        # ── Fast path: read pre-computed outcomes from trades table ──
        # WIN  = max_drawdown_disc > -10 AND days_to_exit_disc IS NOT NULL
        # LOSS = max_drawdown_disc <= -10
        # OPEN = max_drawdown_disc > -10 AND days_to_exit_disc IS NULL
        size_having = f"HAVING MAX(size_midpoint) >= {min_size}" if min_size > 0 else ""
        df = pd.read_sql_query(f"""
            WITH deduped AS (
                SELECT politician_id, MIN(trade_id) as trade_id
                FROM trades
                WHERE transaction_type = 'buy'
                AND filing_violation = 'compliant'
                AND price_at_disclosure_date IS NOT NULL
                AND disclosure_date IS NOT NULL AND disclosure_date != ''
                AND max_drawdown_disc IS NOT NULL
                GROUP BY politician_id, ticker, disclosure_date, price_at_disclosure_date
                {size_having}
            )
            SELECT
                t.politician_id,
                COUNT(*) as combo_trades,
                SUM(CASE WHEN t.max_drawdown_disc > -10
                          AND t.days_to_exit_disc IS NOT NULL THEN 1 ELSE 0 END) as combo_wins,
                SUM(CASE WHEN t.max_drawdown_disc <= -10 THEN 1 ELSE 0 END) as combo_losses,
                ROUND(
                    100.0 * SUM(CASE WHEN t.max_drawdown_disc > -10
                                      AND t.days_to_exit_disc IS NOT NULL THEN 1 ELSE 0 END) /
                    NULLIF(SUM(CASE WHEN t.max_drawdown_disc <= -10
                                        OR t.days_to_exit_disc IS NOT NULL THEN 1 ELSE 0 END), 0)
                , 1) as combo_wr,
                ROUND(AVG(CASE WHEN t.max_drawdown_disc > -10
                               AND t.days_to_exit_disc IS NOT NULL
                               AND t.max_drawdown_disc < 0
                               THEN t.max_drawdown_disc END), 2) as combo_dd
            FROM trades t
            INNER JOIN deduped d ON t.trade_id = d.trade_id
            GROUP BY t.politician_id
        """, conn)
        conn.close()
        return df

    conn   = get_connection()

    # Fetch deduplicated compliant buys with path data
    size_clause = f"AND MAX(size_midpoint) >= {min_size}" if min_size > 0 else ""
    trades = pd.read_sql_query(f"""
        SELECT MIN(trade_id) as trade_id, politician_id,
               price_at_disclosure_date as entry,
               MAX(size_midpoint) as size
        FROM trades
        WHERE transaction_type = 'buy'
        AND filing_violation = 'compliant'
        AND price_at_disclosure_date IS NOT NULL
        AND disclosure_date IS NOT NULL AND disclosure_date != ''
        GROUP BY politician_id, ticker, disclosure_date, price_at_disclosure_date
        HAVING entry > 0 {size_clause}
    """, conn)

    if trades.empty:
        conn.close()
        return pd.DataFrame()

    trade_ids    = trades["trade_id"].tolist()
    placeholders = ",".join("?" * len(trade_ids))

    # Override entry with day-1 open (MOO) — fetch all, no IN clause (avoids SQLite variable limit)
    day1_opens = pd.read_sql_query("""
        SELECT trade_id, open as day1_open
        FROM trade_price_paths
        WHERE anchor = 'disc' AND day = 1
        AND open IS NOT NULL AND open > 0
    """, conn)
    trades = trades.merge(day1_opens, on="trade_id", how="left")
    trades["entry"] = trades["day1_open"].where(
        trades["day1_open"].notna() & (trades["day1_open"] > 0),
        other=trades["entry"]
    )
    trades = trades.drop(columns=["day1_open"])

    paths = pd.read_sql_query(f"""
        SELECT trade_id, day, high, low
        FROM trade_price_paths
        WHERE anchor = 'disc'
        AND trade_id IN ({placeholders})
        ORDER BY trade_id, day ASC
    """, conn, params=trade_ids)
    conn.close()

    # Group paths — use zip over series (100x faster than iterrows on 8M rows)
    from collections import defaultdict
    paths_by_trade = defaultdict(list)
    for tid, day, high, low in zip(paths["trade_id"], paths["day"], paths["high"], paths["low"]):
        paths_by_trade[tid].append((day, high, low))

    stop_mult   = 1 - stop_pct   / 100
    target_mult = 1 + target_pct / 100

    results = []
    for _, t in trades.iterrows():
        tid   = t["trade_id"]
        entry = t["entry"]
        stop  = entry * stop_mult
        tgt   = entry * target_mult
        path  = paths_by_trade.get(tid, [])

        outcome = "open"
        dd      = 0.0
        min_low = entry

        for day, high, low in path:
            if low  is None or high is None: continue
            if low  < min_low: min_low = low
            if low  <= stop:  outcome = "LOSS"; break
            if high >= tgt:   outcome = "WIN";  break

        dd_pct = (min_low - entry) / entry * 100 if entry > 0 else None

        results.append({
            "politician_id": t["politician_id"],
            "outcome":       outcome,
            "dd":            dd_pct,
        })

    res_df = pd.DataFrame(results)

    def agg(g):
        wins   = (g["outcome"] == "WIN").sum()
        losses = (g["outcome"] == "LOSS").sum()
        decided = wins + losses
        winning_dd = g[(g["outcome"] == "WIN") & (g["dd"] < 0)]["dd"]
        return pd.Series({
            "combo_trades":  len(g),
            "combo_wins":    wins,
            "combo_losses":  losses,
            "combo_wr":      round(wins / decided * 100, 1) if decided > 0 else None,
            "combo_dd":      round(winning_dd.mean(), 2) if len(winning_dd) > 0 else None,
        })

    return res_df.groupby("politician_id").apply(agg).reset_index()


# ─────────────────────────────────────────────
# FEED DATA LOADING
# ─────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_feed(days_back=7):
    conn   = get_connection()
    cutoff = (date.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    df = pd.read_sql_query(f"""
        SELECT
            t.disclosure_date,
            t.trade_date,
            t.filing_lag_days,
            t.ticker,
            t.company,
            t.size_midpoint,
            t.price_at_disclosure_date  AS entry_price,
            t.filing_violation,
            t.committee_relevance,
            p.name                      AS politician,
            p.politician_id,
            p.party,
            p.chamber,
            p.score
        FROM trades t
        JOIN politicians p ON t.politician_id = p.politician_id
        WHERE t.disclosure_date >= '{cutoff}'
        AND t.disclosure_date != ''
        AND t.transaction_type = 'buy'
        AND t.ticker IS NOT NULL AND t.ticker != ''
        ORDER BY t.disclosure_date DESC, p.score DESC
    """, conn)

    if not df.empty:
        # ── Routine buyer detection ──────────────────────────────────────────
        # For each (politician, ticker) in the feed, look at all historical buys.
        # Tag as ROUTINE if: 3+ buys AND median gap between consecutive buys ≤ 92 days.
        # 92 days covers monthly (~30d), bi-monthly (~60d), and quarterly (~90d) patterns.
        pairs   = df[["politician_id", "ticker"]].drop_duplicates()
        history = pd.read_sql_query("""
            SELECT politician_id, ticker, trade_date
            FROM trades
            WHERE transaction_type = 'buy'
            AND trade_date IS NOT NULL AND trade_date != ''
            AND ticker IS NOT NULL AND ticker != ''
        """, conn)
        history = history.merge(pairs, on=["politician_id", "ticker"])

        routine = {}
        for (pol_id, ticker), grp in history.groupby(["politician_id", "ticker"]):
            dates = sorted(pd.to_datetime(grp["trade_date"]).tolist())
            if len(dates) < 3:
                continue
            gaps = [(dates[i+1] - dates[i]).days for i in range(len(dates) - 1)]
            median_gap = sorted(gaps)[len(gaps) // 2]
            if median_gap <= 92:
                routine[(pol_id, ticker)] = (len(dates), int(sum(gaps) / len(gaps)))

        def _r(pol_id, ticker, idx):
            entry = routine.get((pol_id, ticker))
            return entry[idx] if entry else None

        df["routine_count"]   = [_r(r.politician_id, r.ticker, 0) for _, r in df.iterrows()]
        df["routine_avg_gap"] = [_r(r.politician_id, r.ticker, 1) for _, r in df.iterrows()]

    conn.close()
    return df


@st.cache_data(ttl=300)
def load_feed_stats(days_back=7):
    conn   = get_connection()
    cutoff = (date.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT COUNT(*) FROM trades
        WHERE disclosure_date >= '{cutoff}' AND disclosure_date != ''
        AND transaction_type = 'buy'
    """)
    total_trades = cursor.fetchone()[0]
    cursor.execute(f"""
        SELECT COUNT(DISTINCT politician_id) FROM trades
        WHERE disclosure_date >= '{cutoff}' AND disclosure_date != ''
        AND transaction_type = 'buy'
    """)
    unique_pols = cursor.fetchone()[0]
    cursor.execute("SELECT MAX(disclosure_date) FROM trades WHERE disclosure_date != '' AND transaction_type = 'buy'")
    latest = cursor.fetchone()[0]
    cursor.execute(f"""
        SELECT COUNT(*) FROM trades t
        JOIN politicians p ON t.politician_id = p.politician_id
        WHERE t.disclosure_date >= '{cutoff}' AND t.disclosure_date != ''
        AND t.transaction_type = 'buy' AND p.score >= 70
    """)
    high_score_trades = cursor.fetchone()[0]
    conn.close()
    return total_trades, unique_pols, latest, high_score_trades


@st.cache_data(ttl=300)
def load_would_follow():
    """
    Trades flagged by pipeline/signal_flagger.py — meet the live execution filter
    (execution/rules.md: cluster_count_td >= 2 AND abs_pct_move_before_disclosure >= 15).

    Outcome derivation (consistent with load_committee_stats / fast-path 10/10 sim):
      loss: max_drawdown_disc <= -10
      win:  max_drawdown_disc > -10  AND days_to_exit_disc IS NOT NULL
      open: max_drawdown_disc IS NULL OR days_to_exit_disc IS NULL (still running)
    """
    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT
            t.trade_id,
            t.disclosure_date,
            t.trade_date,
            t.ticker,
            t.company,
            t.size_midpoint,
            t.price_at_disclosure_date AS entry_price,
            t.cluster_count_td,
            t.abs_pct_move_before_disclosure,
            t.max_drawdown_disc,
            t.days_to_exit_disc,
            p.name    AS politician,
            p.party,
            p.chamber,
            p.score,
            CASE
                WHEN t.max_drawdown_disc IS NULL THEN 'open'
                WHEN t.max_drawdown_disc <= -10 THEN 'loss'
                WHEN t.days_to_exit_disc IS NOT NULL THEN 'win'
                ELSE 'open'
            END AS outcome
        FROM trades t
        JOIN politicians p ON t.politician_id = p.politician_id
        WHERE t.signal_flag = 1
        ORDER BY t.disclosure_date DESC
    """, conn)
    conn.close()
    return df


@st.cache_data(ttl=3600, persist="disk")
def load_pipeline_funnel():
    """
    Returns the filter-funnel counts for the PIPELINE tab.
    Each step is cumulative (survivors at that point in the chain).

    Basket/repeat/dedup steps use CTEs + LEFT JOIN instead of correlated NOT EXISTS
    subqueries — dramatically faster on SQLite (single pass vs O(n²)).
    """
    conn = get_connection()
    cur  = conn.cursor()

    def q(sql): return cur.execute(sql).fetchone()[0]

    steps = []
    steps.append(("All trades in DB",          q("SELECT COUNT(*) FROM trades")))
    steps.append(("Buy trades",                 q("SELECT COUNT(*) FROM trades WHERE transaction_type='buy'")))
    steps.append(("Compliant buys",             q("SELECT COUNT(*) FROM trades WHERE transaction_type='buy' AND filing_violation='compliant'")))
    steps.append(("Priced at disclosure",       q("SELECT COUNT(*) FROM trades WHERE transaction_type='buy' AND filing_violation='compliant' AND price_at_disclosure_date IS NOT NULL")))
    steps.append(("Price fetch not failed",     q("SELECT COUNT(*) FROM trades WHERE transaction_type='buy' AND filing_violation='compliant' AND price_at_disclosure_date IS NOT NULL AND (price_fetch_failed IS NULL OR price_fetch_failed=0)")))
    steps.append(("asset_type = stock",         q("SELECT COUNT(*) FROM trades WHERE transaction_type='buy' AND filing_violation='compliant' AND price_at_disclosure_date IS NOT NULL AND (price_fetch_failed IS NULL OR price_fetch_failed=0) AND asset_type='stock'")))
    steps.append(("Trade date not blank",       q("SELECT COUNT(*) FROM trades WHERE transaction_type='buy' AND filing_violation='compliant' AND price_at_disclosure_date IS NOT NULL AND (price_fetch_failed IS NULL OR price_fetch_failed=0) AND asset_type='stock' AND trade_date IS NOT NULL AND trade_date!=''"  )))

    # ── Single CTE pass: basket filters + repeat filter (replaces 3 correlated NOT EXISTS queries) ──
    cte_counts = cur.execute("""
        WITH base AS (
            SELECT trade_id, politician_id, ticker, trade_date, disclosure_date,
                   price_at_disclosure_date, repeat_before_close
            FROM trades
            WHERE transaction_type='buy' AND filing_violation='compliant'
              AND price_at_disclosure_date IS NOT NULL AND (price_fetch_failed IS NULL OR price_fetch_failed=0)
              AND asset_type='stock' AND trade_date IS NOT NULL AND trade_date!=''
        ),
        basket_td AS (
            SELECT politician_id, trade_date
            FROM trades WHERE transaction_type='buy' AND filing_violation='compliant'
              AND asset_type='stock' AND trade_date IS NOT NULL AND trade_date!=''
            GROUP BY politician_id, trade_date HAVING COUNT(DISTINCT ticker) >= 10
        ),
        basket_disc AS (
            SELECT politician_id, disclosure_date
            FROM trades WHERE transaction_type='buy' AND filing_violation='compliant'
              AND asset_type='stock'
            GROUP BY politician_id, disclosure_date HAVING COUNT(DISTINCT ticker) >= 20
        ),
        joined AS (
            SELECT b.repeat_before_close,
                   td.politician_id  AS in_td,
                   bd.politician_id  AS in_disc
            FROM base b
            LEFT JOIN basket_td  td ON td.politician_id=b.politician_id AND td.trade_date=b.trade_date
            LEFT JOIN basket_disc bd ON bd.politician_id=b.politician_id AND bd.disclosure_date=b.disclosure_date
        )
        SELECT
            SUM(CASE WHEN in_td IS NULL THEN 1 ELSE 0 END)                                                           AS no_td,
            SUM(CASE WHEN in_td IS NULL AND in_disc IS NULL THEN 1 ELSE 0 END)                                        AS no_baskets,
            SUM(CASE WHEN in_td IS NULL AND in_disc IS NULL AND COALESCE(repeat_before_close,0)=0 THEN 1 ELSE 0 END)  AS no_repeat
        FROM joined
    """).fetchone()

    steps.append(("Not trade-date basket (>=10)", cte_counts[0]))
    steps.append(("Not disc-date basket (>=20)",  cte_counts[1]))
    steps.append(("Not repeat within window",      cte_counts[2]))

    # Scored positions (dedup by politician/ticker/disc/price) — CTE-based
    scored = q("""
        WITH base AS (
            SELECT politician_id, ticker, disclosure_date, price_at_disclosure_date, trade_date, repeat_before_close
            FROM trades
            WHERE transaction_type='buy' AND filing_violation='compliant'
              AND price_at_disclosure_date IS NOT NULL AND (price_fetch_failed IS NULL OR price_fetch_failed=0)
              AND asset_type='stock' AND trade_date IS NOT NULL AND trade_date!=''
              AND COALESCE(repeat_before_close,0)=0
        ),
        basket_td AS (
            SELECT politician_id, trade_date
            FROM trades WHERE transaction_type='buy' AND filing_violation='compliant'
              AND asset_type='stock' AND trade_date IS NOT NULL AND trade_date!=''
            GROUP BY politician_id, trade_date HAVING COUNT(DISTINCT ticker) >= 10
        ),
        basket_disc AS (
            SELECT politician_id, disclosure_date
            FROM trades WHERE transaction_type='buy' AND filing_violation='compliant'
              AND asset_type='stock'
            GROUP BY politician_id, disclosure_date HAVING COUNT(DISTINCT ticker) >= 20
        )
        SELECT COUNT(*) FROM (
            SELECT 1 FROM base b
            LEFT JOIN basket_td  td ON td.politician_id=b.politician_id AND td.trade_date=b.trade_date
            LEFT JOIN basket_disc bd ON bd.politician_id=b.politician_id AND bd.disclosure_date=b.disclosure_date
            WHERE td.politician_id IS NULL AND bd.politician_id IS NULL
            GROUP BY b.politician_id, b.ticker, b.disclosure_date, b.price_at_disclosure_date
        )
    """)
    steps.append(("Scored positions (deduped)", scored))

    # Asset type breakdown
    cur.execute("""
        SELECT COALESCE(asset_type,'NULL'), COUNT(*) as n
        FROM trades
        WHERE transaction_type='buy' AND filing_violation='compliant'
          AND price_at_disclosure_date IS NOT NULL AND (price_fetch_failed IS NULL OR price_fetch_failed=0)
        GROUP BY asset_type ORDER BY n DESC
    """)
    asset_breakdown = cur.fetchall()

    # Win rate by year — CTE-based (replaces final correlated NOT EXISTS query)
    cur.execute("""
        WITH base AS (
            SELECT politician_id, trade_date, disclosure_date, repeat_before_close,
                   max_drawdown_disc, days_to_exit_disc
            FROM trades
            WHERE transaction_type='buy' AND filing_violation='compliant'
              AND price_at_disclosure_date IS NOT NULL AND (price_fetch_failed IS NULL OR price_fetch_failed=0)
              AND asset_type='stock' AND trade_date IS NOT NULL AND trade_date!=''
              AND COALESCE(repeat_before_close,0)=0
              AND max_drawdown_disc IS NOT NULL
        ),
        basket_td AS (
            SELECT politician_id, trade_date
            FROM trades WHERE transaction_type='buy' AND filing_violation='compliant'
              AND asset_type='stock' AND trade_date IS NOT NULL AND trade_date!=''
            GROUP BY politician_id, trade_date HAVING COUNT(DISTINCT ticker) >= 10
        ),
        basket_disc AS (
            SELECT politician_id, disclosure_date
            FROM trades WHERE transaction_type='buy' AND filing_violation='compliant'
              AND asset_type='stock'
            GROUP BY politician_id, disclosure_date HAVING COUNT(DISTINCT ticker) >= 20
        ),
        filtered AS (
            SELECT b.disclosure_date, b.max_drawdown_disc, b.days_to_exit_disc
            FROM base b
            LEFT JOIN basket_td  td ON td.politician_id=b.politician_id AND td.trade_date=b.trade_date
            LEFT JOIN basket_disc bd ON bd.politician_id=b.politician_id AND bd.disclosure_date=b.disclosure_date
            WHERE td.politician_id IS NULL AND bd.politician_id IS NULL
        )
        SELECT strftime('%Y', disclosure_date) as yr,
               COUNT(*) as total,
               SUM(CASE WHEN max_drawdown_disc > -10 AND days_to_exit_disc IS NOT NULL THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN max_drawdown_disc <= -10 THEN 1 ELSE 0 END) as losses,
               SUM(CASE WHEN max_drawdown_disc > -10 AND days_to_exit_disc IS NULL THEN 1 ELSE 0 END) as opens
        FROM filtered
        GROUP BY yr ORDER BY yr
    """)
    win_by_year = cur.fetchall()

    conn.close()
    return steps, asset_breakdown, win_by_year


@st.cache_data(persist="disk")
def load_freshness_stats():
    """Latest filing date and latest price path date — used in sidebar status block."""
    conn   = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(disclosure_date) FROM trades WHERE disclosure_date != ''")
    latest_filing = cursor.fetchone()[0]
    cursor.execute("SELECT MAX(date) FROM trade_price_paths WHERE anchor = 'disc'")
    latest_price  = cursor.fetchone()[0]
    conn.close()
    return latest_filing, latest_price


@st.cache_data(ttl=3600, persist="disk")
def load_committee_stats():
    """
    Per-committee win rate stats derived from stored drawdown columns — no path loading needed.

    Outcome derivation (consistent with drawdown_calculator.py):
      LOSS: max_drawdown_disc <= -10  (stopped out — drawdown set to exactly -10%)
      WIN:  max_drawdown_disc > -10   AND days_to_exit_disc IS NOT NULL  (target hit)
      OPEN: max_drawdown_disc > -10   AND days_to_exit_disc IS NULL      (still running)
    """
    conn = get_connection()
    trades_df = pd.read_sql_query("""
        SELECT committee_relevance, max_drawdown_disc, days_to_exit_disc
        FROM trades
        WHERE committee_relevance IS NOT NULL
        AND transaction_type  = 'buy'
        AND filing_violation  = 'compliant'
        AND price_at_disclosure_date IS NOT NULL
        AND disclosure_date IS NOT NULL AND disclosure_date != ''
        AND max_drawdown_disc IS NOT NULL
    """, conn)
    conn.close()

    if trades_df.empty:
        return pd.DataFrame()

    # Classify outcome (vectorized — no iterrows)
    trades_df["outcome"] = "open"
    trades_df.loc[trades_df["max_drawdown_disc"] <= -10, "outcome"] = "loss"
    trades_df.loc[
        (trades_df["max_drawdown_disc"] > -10) & trades_df["days_to_exit_disc"].notna(),
        "outcome"
    ] = "win"

    # Explode pipe-separated committees into one row per committee
    trades_df["committee"] = trades_df["committee_relevance"].str.split(r"\s*\|\s*")
    trades_df = trades_df.explode("committee")
    trades_df["committee"] = trades_df["committee"].str.strip()

    # Aggregate wins/losses/opens
    agg = trades_df.groupby("committee", sort=False).agg(
        wins=  ("outcome", lambda x: (x == "win").sum()),
        losses=("outcome", lambda x: (x == "loss").sum()),
        opens= ("outcome", lambda x: (x == "open").sum()),
    ).reset_index()
    agg["n"]    = agg["wins"] + agg["losses"] + agg["opens"]
    agg["Win%"] = (agg["wins"] / agg["n"] * 100).round(1)

    # Avg DD: rows where -10 < dd < 0 (same logic as original)
    dd_mask = (trades_df["max_drawdown_disc"] > -10) & (trades_df["max_drawdown_disc"] < 0)
    dd_agg  = (
        trades_df[dd_mask]
        .groupby("committee")["max_drawdown_disc"]
        .mean().round(2).reset_index()
        .rename(columns={"max_drawdown_disc": "Avg DD"})
    )

    agg = agg.merge(dd_agg, on="committee", how="left")
    agg["Tier"] = agg["committee"].map(lambda c: COMMITTEE_TIERS.get(c, "NEUTRAL"))

    return (
        agg.rename(columns={"committee": "Committee"})
        .sort_values("n", ascending=False)
        .reset_index(drop=True)
    )


# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────

df_all = load_leaderboard()

with st.sidebar:
    if st.session_state.selected_pol_id:
        st.markdown('<div class="dash-header">NAVIGATION</div>', unsafe_allow_html=True)
        if st.button("← BACK TO LEADERBOARD", use_container_width=True):
            st.session_state.selected_pol_id   = None
            st.session_state.selected_pol_name = None
            st.rerun()
        st.markdown('<hr class="section-divider">', unsafe_allow_html=True)

    if not st.session_state.selected_pol_id:
        st.markdown('<div class="filter-label">VIEW PROFILE</div>', unsafe_allow_html=True)
        all_names    = df_all.sort_values("score", ascending=False)["name"].tolist()
        search_query = st.text_input("", placeholder="Search politician…",
                                     label_visibility="collapsed", key="sidebar_pol_search")
        if search_query:
            filtered_names = [n for n in all_names if search_query.lower() in n.lower()]
            if len(filtered_names) == 1:
                row = df_all[df_all["name"] == filtered_names[0]].iloc[0]
                st.session_state.selected_pol_id   = row["politician_id"]
                st.session_state.selected_pol_name = filtered_names[0]
                st.rerun()
            elif len(filtered_names) == 0:
                st.markdown('<div style="font-family:IBM Plex Mono;font-size:10px;'
                            'color:#555">No matches</div>', unsafe_allow_html=True)
            else:
                sidebar_names = ["— select —"] + filtered_names
                sidebar_sel   = st.selectbox("", options=sidebar_names,
                                             label_visibility="collapsed",
                                             key=f"sidebar_pol_select_{search_query}")
                if sidebar_sel != "— select —":
                    row = df_all[df_all["name"] == sidebar_sel].iloc[0]
                    st.session_state.selected_pol_id   = row["politician_id"]
                    st.session_state.selected_pol_name = sidebar_sel
                    st.rerun()
        else:
            sidebar_names = ["— select politician —"] + all_names
            sidebar_sel   = st.selectbox("", options=sidebar_names,
                                         label_visibility="collapsed",
                                         key="sidebar_pol_select_default")
            if sidebar_sel != "— select politician —":
                row = df_all[df_all["name"] == sidebar_sel].iloc[0]
                st.session_state.selected_pol_id   = row["politician_id"]
                st.session_state.selected_pol_name = sidebar_sel
                st.rerun()
        st.markdown('<hr class="section-divider">', unsafe_allow_html=True)

    st.markdown('<div class="dash-header">FILTERS</div>', unsafe_allow_html=True)
    st.markdown("")

    party_filter = st.radio("PARTY", options=["All", "Republican", "Democrat"],
                            index=0, horizontal=True)
    chamber_filter = st.radio("CHAMBER", options=["All", "House", "Senate"],
                              index=0, horizontal=True)

    states = ["All"] + sorted(df_all["state"].dropna().unique().tolist())
    state_filter = st.selectbox("STATE", options=states, index=0)

    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)

    min_trades = st.slider("MINIMUM TRADES", min_value=1, max_value=50, value=5, step=1)

    recency_options = {"Any": 9999, "Last 6 months": 180, "Last 12 months": 365, "Last 24 months": 730}
    recency_label   = st.selectbox("LAST TRADED", options=list(recency_options.keys()), index=0)
    recency_days    = recency_options[recency_label]

    comm_aligned_only = st.checkbox("COMMITTEE ALIGNED ONLY",
                                    help="Only show politicians with at least one trade "
                                         "in a sector they oversee")

    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)

    disc_lag_options = {"Any": 999, "≤ 7 days": 7, "≤ 14 days": 14,
                        "≤ 30 days": 30, "≤ 45 days": 45}
    disc_lag_label = st.selectbox("AVG DISCLOSURE LAG",
                                  options=list(disc_lag_options.keys()), index=0)
    disc_lag_max = disc_lag_options[disc_lag_label]

    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)
    st.markdown('<div class="filter-label">STOP / TARGET COMBO</div>', unsafe_allow_html=True)
    stop_pct   = st.slider("STOP LOSS %",   min_value=2,  max_value=20, value=10, step=1)
    target_pct = st.slider("TAKE PROFIT %", min_value=2,  max_value=30, value=10, step=1)
    if stop_pct != 10 or target_pct != 10:
        st.markdown(f'''<div style="font-family:IBM Plex Mono;font-size:10px;
                         color:#f5a623;margin-top:4px">
            ⚠ Custom combo — live re-simulation</div>''', unsafe_allow_html=True)

    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)
    st.markdown('<div class="filter-label">MIN TRADE SIZE</div>', unsafe_allow_html=True)
    size_options = {"Any": 0, "$15k+": 15000, "$50k+": 50000,
                    "$100k+": 100000, "$250k+": 250000}
    size_label   = st.selectbox("", options=list(size_options.keys()),
                                 index=0, label_visibility="collapsed",
                                 key="size_filter")
    min_size = size_options[size_label]

    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)
    st.markdown('<div class="filter-label">SCORE COMPONENTS</div>', unsafe_allow_html=True)
    st.markdown("""
    <div style="font-family: IBM Plex Mono; font-size: 10px; color: #444; line-height: 1.8;">
    Win Rate (10/10)........50%<br>
    Avg Drawdown (DD).......30%<br>
    Large Trade Accuracy....20%
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)
    st.markdown('<div class="filter-label">SCORING FILTERS</div>', unsafe_allow_html=True)
    st.markdown("""
    <div style="font-family: IBM Plex Mono; font-size: 10px; color: #444; line-height: 2.0;">
    <span style="color:#555">✓</span> Compliant buys only<br>
    <span style="color:#555">✓</span> Valid disclosure date<br>
    <span style="color:#555">✓</span> Entry price required<br>
    <span style="color:#555">✓</span> Failed price fetch excluded<br>
    <span style="color:#555">✓</span> Trade date required<br>
    <span style="color:#555">✓</span> ETFs scored separately<br>
    <span style="color:#555">✓</span> &ge;10 tickers/day excluded<br>
    <span style="color:#555">✓</span> &ge;20 tickers/filing excluded<br>
    <span style="color:#555">✓</span> Within-window repeats excluded<br>
    <br>
    <span style="color:#333;font-size:9px">See METHODOLOGY tab for details.</span>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)
    _latest_filing, _latest_price = load_freshness_stats()
    st.markdown(f"""
    <div style="font-family:IBM Plex Mono;font-size:9px;color:#444;line-height:2.2;">
    <span style="color:#555;letter-spacing:1px;font-size:8px">DATA STATUS</span><br>
    Latest filing&nbsp;&nbsp;&nbsp;{_latest_filing or "—"}<br>
    Prices to&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{_latest_price  or "—"}
    </div>
    """, unsafe_allow_html=True)

# ─────────────────────────────────────────────
# PROFILE PAGE
# ─────────────────────────────────────────────

def render_profile(pol_id):
    pol, trades_df, paths_df, committees_df = load_politician_profile(pol_id)

    party      = pol["party"] or ""
    pty_color  = "#ff5f5f" if party == "Republican" else "#4a9eff"
    pty_short  = "R" if party == "Republican" else "D"
    score      = pol["score"] or 0
    sc_color   = "#4caf50" if score >= 70 else "#f5a623" if score >= 40 else "#ff5252"

    # Rank + prev/next from leaderboard
    ranked     = df_all.sort_values("score", ascending=False).reset_index(drop=True)
    ranked_ids = ranked["politician_id"].tolist()
    try:
        cur_idx   = ranked_ids.index(pol_id)
        rank      = cur_idx + 1
        prev_id   = ranked_ids[cur_idx - 1] if cur_idx > 0 else None
        next_id   = ranked_ids[cur_idx + 1] if cur_idx < len(ranked_ids) - 1 else None
        prev_name = ranked.iloc[cur_idx - 1]["name"] if prev_id else None
        next_name = ranked.iloc[cur_idx + 1]["name"] if next_id else None
    except ValueError:
        rank = prev_id = next_id = prev_name = next_name = None

    # Header
    st.markdown('<div class="dash-header">CONGRESSIONAL TRADE INTELLIGENCE · POLITICIAN PROFILE</div>',
                unsafe_allow_html=True)

    hdr_col, nav_col = st.columns([3, 1])
    with hdr_col:
        rank_str = f"RANK #{rank} of {len(ranked_ids)}" if rank else ""
        st.markdown(f'<div class="dash-title">{pol["name"]}</div>', unsafe_allow_html=True)
        st.markdown(f"""
        <div class="dash-subtitle">
            <span style="color:{pty_color}">{pty_short}</span> &nbsp;·&nbsp;
            {pol["chamber"]} &nbsp;·&nbsp; {pol["state"]}
            &nbsp;&nbsp;<span style="color:#f5a623">{rank_str}</span>
        </div>
        """, unsafe_allow_html=True)
    with nav_col:
        nav_c1, nav_c2 = st.columns(2)
        with nav_c1:
            if prev_id:
                if st.button(f"↑ #{rank-1}", help=prev_name, use_container_width=True):
                    st.session_state.selected_pol_id   = prev_id
                    st.session_state.selected_pol_name = prev_name
                    st.rerun()
        with nav_c2:
            if next_id:
                if st.button(f"↓ #{rank+1}", help=next_name, use_container_width=True):
                    st.session_state.selected_pol_id   = next_id
                    st.session_state.selected_pol_name = next_name
                    st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)

    # Simulate all trades
    if len(trades_df) > 0 and len(paths_df) > 0:
        results = simulate_trades(trades_df, paths_df)
    else:
        results = pd.DataFrame()

    # scored_results: stocks only, no within-window repeats — matches scorer.py filters exactly
    # results (full set) is kept for the trade table which shows SKIP/ETF labels
    scored_results = results[
        (results["asset_type"] == "stock") &
        (results["repeat_before_close"] == 0)
    ] if len(results) else pd.DataFrame()

    wins   = len(scored_results[scored_results["outcome"] == "WIN"])   if len(scored_results) else 0
    losses = len(scored_results[scored_results["outcome"] == "LOSS"])  if len(scored_results) else 0
    opens  = len(scored_results[scored_results["outcome"] == "open"])  if len(scored_results) else 0
    total  = wins + losses + opens
    # denominator includes opens — matches scorer.py (open trades dilute the rate)
    win_rate = wins / total * 100 if total > 0 else 0

    winning_dd = scored_results[
        scored_results["dd"].notna() &
        (scored_results["dd"] > -10) &
        (scored_results["dd"] < 0)
    ]["dd"] if len(scored_results) else pd.Series()
    avg_dd = winning_dd.mean() if len(winning_dd) else None

    large = scored_results[scored_results["size"] >= 50000] if len(scored_results) else pd.DataFrame()
    large_wins = large[large["outcome"] == "WIN"] if len(large) else pd.DataFrame()
    large_wr = len(large_wins) / len(large) * 100 if len(large) else None

    avg_exit = scored_results[scored_results["exit_day"].notna()]["exit_day"].mean() if len(scored_results) else None

    # ETF score data
    etf_score = pol.get("score_etf")
    etf_wr    = pol.get("win_rate_etf")
    etf_n     = pol.get("etf_trade_count")
    has_etf   = pd.notna(etf_score) and etf_score is not None

    # Metric cards
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    with col1:
        st.markdown(f"""<div class="metric-card">
            <div class="metric-label">Stock Score</div>
            <div class="metric-value" style="color:{sc_color}">{score:.1f}</div>
            <div class="metric-sub">composite</div>
        </div>""", unsafe_allow_html=True)
    with col2:
        wr_color = "#4caf50" if win_rate >= 65 else "#ff9800" if win_rate >= 55 else "#ff5252"
        st.markdown(f"""<div class="metric-card">
            <div class="metric-label">Win Rate</div>
            <div class="metric-value" style="color:{wr_color}">{win_rate:.1f}%</div>
            <div class="metric-sub">{wins}W · {losses}L · {opens} open</div>
        </div>""", unsafe_allow_html=True)
    with col3:
        dd_str = f"{avg_dd:.2f}%" if avg_dd is not None else "N/A"
        st.markdown(f"""<div class="metric-card">
            <div class="metric-label">Avg DD (wins)</div>
            <div class="metric-value" style="color:#ff9800">{dd_str}</div>
            <div class="metric-sub">before target hit</div>
        </div>""", unsafe_allow_html=True)
    with col4:
        lg_str = f"{large_wr:.1f}%" if large_wr is not None else "N/A"
        st.markdown(f"""<div class="metric-card">
            <div class="metric-label">Large Win %</div>
            <div class="metric-value">{lg_str}</div>
            <div class="metric-sub">trades &gt; $50k</div>
        </div>""", unsafe_allow_html=True)
    with col5:
        exit_str = f"{avg_exit:.0f}d" if avg_exit is not None else "N/A"
        st.markdown(f"""<div class="metric-card">
            <div class="metric-label">Avg Exit</div>
            <div class="metric-value">{exit_str}</div>
            <div class="metric-sub">days to stop/target</div>
        </div>""", unsafe_allow_html=True)
    with col6:
        if has_etf:
            etf_sc_color = "#4a9eff" if etf_score >= 70 else "#4a9eff" if etf_score >= 40 else "#4a9eff"
            etf_sub      = f"{etf_wr:.1f}% win · {int(etf_n)} trades" if pd.notna(etf_wr) else "—"
            st.markdown(f"""<div class="metric-card" style="border-top-color:#4a9eff">
                <div class="metric-label" style="color:#4a9eff">ETF Score</div>
                <div class="metric-value" style="color:#4a9eff">{etf_score:.1f}</div>
                <div class="metric-sub">{etf_sub}</div>
            </div>""", unsafe_allow_html=True)
        else:
            st.markdown("""<div class="metric-card" style="border-top-color:#222">
                <div class="metric-label" style="color:#333">ETF Score</div>
                <div class="metric-value" style="color:#333;font-size:16px">N/A</div>
                <div class="metric-sub">&lt; 5 ETF trades</div>
            </div>""", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # Committee memberships
    if len(committees_df) > 0:
        st.markdown('<div class="filter-label">COMMITTEE MEMBERSHIPS</div>', unsafe_allow_html=True)
        tags_html = " &nbsp;".join(
            f'<span style="background:#0f1a0f;color:#4caf50;border:1px solid #1a3a1a;'
            f'font-family:IBM Plex Mono,monospace;font-size:10px;padding:3px 8px;'
            f'border-radius:2px;letter-spacing:1px">'
            f'{COMMITTEE_NAMES.get(row["thomas_id"], row["thomas_id"])}</span>'
            for _, row in committees_df.iterrows()
        )
        st.html(f'<div style="padding:6px 0 12px 0;line-height:2">{tags_html}</div>')

    # Charts row
    if len(scored_results) > 0:
        import json

        col_left, col_right = st.columns(2)

        # ── Donut chart — Win/Loss/Open ───────
        with col_left:
            st.markdown('<div class="filter-label">OUTCOME BREAKDOWN</div>',
                        unsafe_allow_html=True)
            donut_data = json.dumps([
                {"label": "WIN",  "value": wins,   "color": "#4caf50"},
                {"label": "LOSS", "value": losses, "color": "#ff5252"},
                {"label": "OPEN", "value": opens,  "color": "#444444"},
            ])
            st.html(f"""
            <div style="display:flex;align-items:center;gap:40px;padding:20px 0">
                <canvas id="donut" width="180" height="180"></canvas>
                <div id="legend" style="font-family:IBM Plex Mono,monospace;font-size:12px"></div>
            </div>
            <script>
            (function() {{
                const data  = {donut_data};
                const total = data.reduce((s,d) => s + d.value, 0);
                const canvas = document.getElementById('donut');
                const ctx    = canvas.getContext('2d');
                const cx = 90, cy = 90, r = 70, inner = 42;
                let angle = -Math.PI / 2;
                data.forEach(d => {{
                    const slice = (d.value / total) * 2 * Math.PI;
                    ctx.beginPath();
                    ctx.moveTo(cx, cy);
                    ctx.arc(cx, cy, r, angle, angle + slice);
                    ctx.closePath();
                    ctx.fillStyle = d.color;
                    ctx.fill();
                    angle += slice;
                }});
                ctx.beginPath();
                ctx.arc(cx, cy, inner, 0, 2 * Math.PI);
                ctx.fillStyle = '#0a0a0a';
                ctx.fill();
                ctx.fillStyle = '#ffffff';
                ctx.font = 'bold 18px IBM Plex Mono';
                ctx.textAlign = 'center';
                ctx.fillText(total > 0 ? Math.round(data[0].value/total*100)+'%' : '', cx, cy - 6);
                ctx.font = '10px IBM Plex Mono';
                ctx.fillStyle = '#888';
                ctx.fillText('WIN RATE', cx, cy + 12);
                const leg = document.getElementById('legend');
                leg.innerHTML = data.map(d =>
                    `<div style="margin-bottom:10px">
                        <span style="display:inline-block;width:10px;height:10px;
                            background:${{d.color}};border-radius:50%;margin-right:8px"></span>
                        <span style="color:#888">${{d.label}}</span>
                        <span style="color:#fff;margin-left:12px;font-weight:600">${{d.value}}</span>
                     </div>`
                ).join('');
            }})();
            </script>
            """)

        # ── Drawdown histogram ────────────────
        with col_right:
            st.markdown('<div class="filter-label">DRAWDOWN DISTRIBUTION (WINNING TRADES)</div>',
                        unsafe_allow_html=True)
            if len(winning_dd) > 0:
                bins   = [-10, -8, -6, -4, -2, 0]
                labels = ["-10–-8%", "-8–-6%", "-6–-4%", "-4–-2%", "-2–0%"]
                counts = [int(((winning_dd >= bins[i]) & (winning_dd < bins[i+1])).sum())
                          for i in range(len(bins)-1)]
                max_count = max(counts) if max(counts) > 0 else 1

                bars_html = ""
                for label, count in zip(labels, counts):
                    bar_w = int(count / max_count * 100)
                    bars_html += f"""
                    <div style="display:flex;align-items:center;margin-bottom:8px;
                                font-family:IBM Plex Mono,monospace;font-size:11px">
                        <span style="color:#888;width:80px;text-align:right;
                                     margin-right:12px">{label}</span>
                        <div style="background:#1a1a1a;height:18px;flex:1;border-radius:1px">
                            <div style="background:#ff9800;height:18px;width:{bar_w}%;
                                        border-radius:1px"></div>
                        </div>
                        <span style="color:#fff;margin-left:10px;width:30px">{count}</span>
                    </div>"""
                st.html(f'<div style="padding:10px 0">{bars_html}</div>')
            else:
                st.markdown('<div style="color:#444;font-family:IBM Plex Mono;'
                            'font-size:12px;padding:20px 0">No drawdown data</div>',
                            unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Top tickers ───────────────────────────
    if len(scored_results) > 0:
        st.markdown('<div class="filter-label">TOP TICKERS</div>', unsafe_allow_html=True)

        ticker_stats = scored_results.groupby("ticker").apply(lambda g: pd.Series({
            "trades":   len(g),
            "wins":     (g["outcome"] == "WIN").sum(),
            "losses":   (g["outcome"] == "LOSS").sum(),
            "win_rate": (g["outcome"] == "WIN").sum() / len(g[g["outcome"] != "open"]) * 100
                        if len(g[g["outcome"] != "open"]) > 0 else 0,
        })).reset_index().sort_values("trades", ascending=False).head(10)

        ticker_rows = ""
        for _, t in ticker_stats.iterrows():
            wr = t["win_rate"]
            wr_color = "#4caf50" if wr >= 65 else "#ff9800" if wr >= 55 else "#ff5252"
            ticker_rows += f"""<tr style="border-bottom:1px solid #141414">
                <td style="padding:6px 10px;color:#fff;font-family:IBM Plex Mono,monospace;
                           font-size:12px;font-weight:600">{t['ticker']}</td>
                <td style="padding:6px 10px;color:#888;font-family:IBM Plex Mono,monospace;
                           font-size:12px">{int(t['trades'])}</td>
                <td style="padding:6px 10px;color:#4caf50;font-family:IBM Plex Mono,monospace;
                           font-size:12px">{int(t['wins'])}</td>
                <td style="padding:6px 10px;color:#ff5252;font-family:IBM Plex Mono,monospace;
                           font-size:12px">{int(t['losses'])}</td>
                <td style="padding:6px 10px;color:{wr_color};font-family:IBM Plex Mono,monospace;
                           font-size:12px;font-weight:600">{wr:.0f}%</td>
            </tr>"""

        st.html(f"""
        <table style="border-collapse:collapse;width:400px">
            <thead>
                <tr style="border-bottom:2px solid #f5a623">
                    <th style="padding:6px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                               font-size:10px;letter-spacing:2px;text-align:left">TICKER</th>
                    <th style="padding:6px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                               font-size:10px;letter-spacing:2px;text-align:left">TRADES</th>
                    <th style="padding:6px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                               font-size:10px;letter-spacing:2px;text-align:left">WINS</th>
                    <th style="padding:6px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                               font-size:10px;letter-spacing:2px;text-align:left">LOSSES</th>
                    <th style="padding:6px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                               font-size:10px;letter-spacing:2px;text-align:left">WIN%</th>
                </tr>
            </thead>
            <tbody>{ticker_rows}</tbody>
        </table>
        """)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Recent trades table ───────────────────
    if len(results) > 0:
        show_all = st.checkbox("SHOW ALL TRADES", value=False)
        recent   = results if show_all else results.head(25)
        st.markdown(f'<div class="filter-label">{"ALL" if show_all else "RECENT"} TRADES '
                    f'({len(recent)} of {len(results)})</div>', unsafe_allow_html=True)
        trade_rows = ""
        for _, t in recent.iterrows():
            is_skip   = bool(t.get("repeat_before_close"))
            outcome   = t["outcome"]
            if is_skip:
                oc_color  = "#555"
                outcome   = "SKIP"
            else:
                oc_color  = "#4caf50" if outcome == "WIN" else "#ff5252" if outcome == "LOSS" else "#888"
            row_style = "border-bottom:1px solid #141414;opacity:0.45" if is_skip else "border-bottom:1px solid #141414"
            dd_str    = f"{t['dd']:.1f}%" if pd.notna(t["dd"]) else "—"
            size_str  = f"${t['size']:,.0f}" if pd.notna(t["size"]) else "—"
            exit_str  = t["exit_date"] or "open"
            comm_rel  = t.get("committee_relevance")
            if comm_rel:
                first_comm = comm_rel.split(" | ")[0].strip()
                tier = COMMITTEE_TIERS.get(first_comm, "NEUTRAL")
                if tier == "STRONG":
                    _bg, _fg, _bd = "#0f1a0f", "#4caf50", "#1a3a1a"
                elif tier == "WEAK":
                    _bg, _fg, _bd = "#141414", "#555",    "#2a2a2a"
                else:
                    _bg, _fg, _bd = "#0a1218", "#3a7abf", "#0d2a40"
                comm_cell = (f'<span style="background:{_bg};color:{_fg};'
                             f'border:1px solid {_bd};font-size:9px;padding:2px 6px;'
                             f'border-radius:2px;letter-spacing:1px">{first_comm}</span>')
            else:
                comm_cell = '<span style="color:#333">—</span>'
            asset_type = t.get("asset_type") or ""
            if asset_type in ("etf", "fund"):
                asset_cell = (f'<span style="background:#001a2e;color:#4a9eff;'
                              f'border:1px solid #003366;font-size:9px;padding:2px 6px;'
                              f'border-radius:2px;letter-spacing:1px">{asset_type.upper()}</span>')
            else:
                asset_cell = ""
            trade_rows += f"""<tr style="{row_style}">
                <td style="padding:6px 10px;color:#fff;font-family:IBM Plex Mono,monospace;
                           font-size:12px;font-weight:600">{t['ticker']}</td>
                <td style="padding:6px 10px;color:#888;font-family:IBM Plex Mono,monospace;
                           font-size:12px">{t['disc_date']}</td>
                <td style="padding:6px 10px;color:#888;font-family:IBM Plex Mono,monospace;
                           font-size:12px">{t['entry']:.2f}</td>
                <td style="padding:6px 10px;color:#ff5252;font-family:IBM Plex Mono,monospace;
                           font-size:12px">{t['stop']:.2f}</td>
                <td style="padding:6px 10px;color:#4caf50;font-family:IBM Plex Mono,monospace;
                           font-size:12px">{t['target']:.2f}</td>
                <td style="padding:6px 10px;color:#888;font-family:IBM Plex Mono,monospace;
                           font-size:12px">{size_str}</td>
                <td style="padding:6px 10px;color:#ff9800;font-family:IBM Plex Mono,monospace;
                           font-size:12px">{dd_str}</td>
                <td style="padding:6px 10px;color:{oc_color};font-family:IBM Plex Mono,monospace;
                           font-size:12px;font-weight:600">{outcome.upper()}</td>
                <td style="padding:6px 10px;color:#888;font-family:IBM Plex Mono,monospace;
                           font-size:12px">{exit_str}</td>
                <td style="padding:6px 10px;font-family:IBM Plex Mono,monospace;
                           font-size:12px">{comm_cell}</td>
                <td style="padding:6px 10px;font-family:IBM Plex Mono,monospace;
                           font-size:12px">{asset_cell}</td>
            </tr>"""

        st.html(f"""
        <div style="overflow-x:auto;border:1px solid #1a1a1a;border-radius:2px">
        <table style="width:100%;border-collapse:collapse;background:#0a0a0a">
            <thead>
                <tr style="border-bottom:2px solid #f5a623">
                    <th style="padding:8px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                               font-size:10px;letter-spacing:2px;text-align:left;
                               background:#0f0f0f">TICKER</th>
                    <th style="padding:8px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                               font-size:10px;letter-spacing:2px;text-align:left;
                               background:#0f0f0f">DISC DATE</th>
                    <th style="padding:8px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                               font-size:10px;letter-spacing:2px;text-align:left;
                               background:#0f0f0f">ENTRY</th>
                    <th style="padding:8px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                               font-size:10px;letter-spacing:2px;text-align:left;
                               background:#0f0f0f">STOP</th>
                    <th style="padding:8px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                               font-size:10px;letter-spacing:2px;text-align:left;
                               background:#0f0f0f">TARGET</th>
                    <th style="padding:8px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                               font-size:10px;letter-spacing:2px;text-align:left;
                               background:#0f0f0f">SIZE</th>
                    <th style="padding:8px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                               font-size:10px;letter-spacing:2px;text-align:left;
                               background:#0f0f0f">DD</th>
                    <th style="padding:8px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                               font-size:10px;letter-spacing:2px;text-align:left;
                               background:#0f0f0f">OUTCOME</th>
                    <th style="padding:8px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                               font-size:10px;letter-spacing:2px;text-align:left;
                               background:#0f0f0f">EXIT DATE</th>
                    <th style="padding:8px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                               font-size:10px;letter-spacing:2px;text-align:left;
                               background:#0f0f0f">COMMITTEE</th>
                    <th style="padding:8px 10px;color:#4a9eff;font-family:IBM Plex Mono,monospace;
                               font-size:10px;letter-spacing:2px;text-align:left;
                               background:#0f0f0f">TYPE</th>
                </tr>
            </thead>
            <tbody>{trade_rows}</tbody>
        </table>
        </div>
        """)
    else:
        st.markdown('<div style="color:#444;font-family:IBM Plex Mono;font-size:12px">'
                    'No trade data available</div>', unsafe_allow_html=True)


# ─────────────────────────────────────────────
# LEADERBOARD PAGE
# ─────────────────────────────────────────────

def render_leaderboard(stop_pct=10, target_pct=10, min_size=0):
    st.markdown('<div class="dash-header">CONGRESSIONAL TRADE INTELLIGENCE</div>',
                unsafe_allow_html=True)
    st.markdown('<div class="dash-title">POLITICIAN LEADERBOARD</div>', unsafe_allow_html=True)
    st.markdown('<div class="dash-subtitle">10% Stop / 10% Target · Market-on-Open Entry (Day-1) · '
                'Compliant Trades Only</div>', unsafe_allow_html=True)
    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(["  LEADERBOARD  ", "  FEED  ", "  METHODOLOGY  ", "  COMMITTEES  ", "  PIPELINE  ", "  WOULD FOLLOW  "])

    with tab2:
        st.markdown("<br>", unsafe_allow_html=True)

        # ── In-tab filters ──────────────────────
        fc1, fc2, fc3, fc4, fc5 = st.columns([1.2, 1, 1, 1, 1])
        with fc1:
            days_options = {"Last 7 days": 7, "Last 14 days": 14, "Last 30 days": 30, "All time": 3650}
            days_label   = st.selectbox("WINDOW", options=list(days_options.keys()), index=0, key="feed_days")
            days_back    = days_options[days_label]
        with fc2:
            feed_party = st.radio("PARTY", options=["All", "R", "D"], index=0, horizontal=True, key="feed_party")
        with fc3:
            feed_chamber = st.radio("CHAMBER", options=["All", "House", "Senate"], index=0, horizontal=True, key="feed_chamber")
        with fc4:
            feed_min_score = st.slider("MIN SCORE", min_value=0, max_value=100, value=0, step=10, key="feed_score")
        with fc5:
            size_opts  = {"Any size": 0, "$15K+": 15_000, "$50K+": 50_000, "$100K+": 100_000, "$250K+": 250_000}
            size_label = st.selectbox("MIN SIZE", options=list(size_opts.keys()), index=0, key="feed_size")
            feed_min_size = size_opts[size_label]

        st.markdown("<br>", unsafe_allow_html=True)

        # ── Metrics ─────────────────────────────
        total_trades, unique_pols, latest_disc, high_score_trades = load_feed_stats(days_back)
        mc1, mc2, mc3, mc4 = st.columns(4)
        with mc1:
            st.markdown(f'''<div class="metric-card">
                <div class="metric-label">Buy Trades</div>
                <div class="metric-value">{total_trades:,}</div>
                <div class="metric-sub">{days_label.lower()}</div>
            </div>''', unsafe_allow_html=True)
        with mc2:
            st.markdown(f'''<div class="metric-card">
                <div class="metric-label">Politicians Filing</div>
                <div class="metric-value">{unique_pols}</div>
                <div class="metric-sub">{days_label.lower()}</div>
            </div>''', unsafe_allow_html=True)
        with mc3:
            st.markdown(f'''<div class="metric-card">
                <div class="metric-label">High Score Trades</div>
                <div class="metric-value">{high_score_trades}</div>
                <div class="metric-sub">from politicians scored 70+</div>
            </div>''', unsafe_allow_html=True)
        with mc4:
            st.markdown(f'''<div class="metric-card">
                <div class="metric-label">Latest Disclosure</div>
                <div class="metric-value" style="font-size:16px;padding-top:4px">{latest_disc or "N/A"}</div>
                <div class="metric-sub">most recent buy filing</div>
            </div>''', unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # ── Load + filter ───────────────────────
        feed_df = load_feed(days_back)
        if feed_party   != "All": feed_df = feed_df[feed_df["party"] == ("Republican" if feed_party == "R" else "Democrat")]
        if feed_chamber != "All": feed_df = feed_df[feed_df["chamber"] == feed_chamber]
        if feed_min_score > 0:    feed_df = feed_df[feed_df["score"].notna() & (feed_df["score"] >= feed_min_score)]
        if feed_min_size  > 0:    feed_df = feed_df[feed_df["size_midpoint"].notna() & (feed_df["size_midpoint"] >= feed_min_size)]

        st.markdown(f'<div class="filter-label">SHOWING {len(feed_df)} TRADES</div>', unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)

        if feed_df.empty:
            st.markdown('<div style="color:#444;font-family:IBM Plex Mono;font-size:12px">'
                        'No trades match the current filters.</div>', unsafe_allow_html=True)
        else:
            feed_today = date.today()

            def days_ago_str(d):
                try:
                    n = (feed_today - datetime.strptime(d, "%Y-%m-%d").date()).days
                    if n == 0: return "Today"
                    if n == 1: return "Yesterday"
                    return f"{n}d ago"
                except Exception:
                    return d

            def fmt_size(v):
                if pd.isna(v): return "—"
                if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
                if v >= 1_000:     return f"${v/1_000:.0f}K"
                return f"${v:.0f}"

            feed_rows_html = ""
            for _, row in feed_df.iterrows():
                pty        = row.get("party", "") or ""
                pty_short  = "R" if pty == "Republican" else "D" if pty == "Democrat" else "?"
                pty_color  = "#ff5f5f" if pty == "Republican" else "#4a9eff"
                score      = row.get("score")
                score_val  = score if pd.notna(score) else 0
                sc_str     = f"{score_val:.0f}" if pd.notna(score) else "—"
                sc_color   = "#4caf50" if score_val >= 70 else "#f5a623" if score_val >= 40 else "#888"
                bar_w      = min(int(score_val), 100)
                ticker     = row.get("ticker") or "—"
                company    = (row.get("company") or "—")[:28]
                r_count    = row.get("routine_count")
                r_gap      = row.get("routine_avg_gap")
                if r_count and pd.notna(r_count):
                    routine_badge = (f'<div style="color:#555;font-size:9px;margin-top:2px;'
                                     f'letter-spacing:0.5px">ROUTINE · {int(r_count)}× · ~{int(r_gap)}d</div>')
                else:
                    routine_badge = ""
                size_str   = fmt_size(row.get("size_midpoint"))
                entry      = row.get("entry_price")
                entry_str  = f"${entry:.2f}" if pd.notna(entry) else "—"
                lag        = row.get("filing_lag_days")
                lag_str    = f"{int(lag)}d" if pd.notna(lag) else "—"
                lag_color  = "#ff5252" if pd.notna(lag) and lag > 30 else "#888"
                disc_date  = row.get("disclosure_date", "") or ""
                disc_str   = days_ago_str(disc_date)
                trade_date = row.get("trade_date", "") or "—"
                name       = (row.get("politician") or "—")[:22]

                comm_rel   = row.get("committee_relevance")
                if comm_rel and pd.notna(comm_rel):
                    first_comm = str(comm_rel).split(" | ")[0].strip()
                    tier = COMMITTEE_TIERS.get(first_comm, "NEUTRAL")
                    if tier == "STRONG":
                        c_bg, c_fg, c_bd = "#0f1a0f", "#4caf50", "#1a3a1a"
                    elif tier == "WEAK":
                        c_bg, c_fg, c_bd = "#141414", "#555",    "#2a2a2a"
                    else:
                        c_bg, c_fg, c_bd = "#0a1218", "#3a7abf", "#0d2a40"
                    comm_cell = (f'<span style="background:{c_bg};color:{c_fg};'
                                 f'border:1px solid {c_bd};font-size:9px;padding:1px 5px;'
                                 f'border-radius:2px;letter-spacing:1px;white-space:nowrap">'
                                 f'{first_comm}</span>')
                else:
                    comm_cell = '<span style="color:#2a2a2a">—</span>'

                feed_rows_html += f"""<tr style="border-bottom:1px solid #111">
                    <td style="padding:5px 10px;font-family:IBM Plex Mono,monospace;white-space:nowrap">
                        <div style="color:#555;font-size:11px">{disc_str}</div>
                        <div style="color:#333;font-size:10px;margin-top:1px">{disc_date}</div>
                    </td>
                    <td style="padding:5px 10px;color:#666;font-family:IBM Plex Mono,monospace;
                               font-size:11px;white-space:nowrap">{trade_date}</td>
                    <td style="padding:5px 10px;color:#e0e0e0;font-family:IBM Plex Mono,monospace;
                               font-size:11px;white-space:nowrap">{name}</td>
                    <td style="padding:5px 10px;color:{pty_color};font-family:IBM Plex Mono,monospace;
                               font-size:11px">{pty_short}</td>
                    <td style="padding:5px 10px;font-family:IBM Plex Mono,monospace;font-size:11px">
                        <span style="color:{sc_color};font-weight:600">{sc_str}</span>
                        <div style="background:#1a1a1a;height:2px;width:50px;margin-top:2px;
                                    border-radius:1px">
                            <div style="background:{sc_color};height:2px;width:{bar_w/2:.0f}px;
                                        border-radius:1px"></div>
                        </div>
                    </td>
                    <td style="padding:5px 10px;font-family:IBM Plex Mono,monospace">
                        <div style="color:#fff;font-size:12px;font-weight:600">{ticker}</div>
                        {routine_badge}
                    </td>
                    <td style="padding:5px 10px;color:#666;font-family:IBM Plex Mono,monospace;
                               font-size:11px">{company}</td>
                    <td style="padding:5px 10px;color:#ccc;font-family:IBM Plex Mono,monospace;
                               font-size:11px;white-space:nowrap">{size_str}</td>
                    <td style="padding:5px 10px;color:#888;font-family:IBM Plex Mono,monospace;
                               font-size:11px;white-space:nowrap">{entry_str}</td>
                    <td style="padding:5px 10px;color:{lag_color};font-family:IBM Plex Mono,monospace;
                               font-size:11px">{lag_str}</td>
                    <td style="padding:5px 10px">{comm_cell}</td>
                </tr>"""

            st.html(f"""
            <div style="overflow-x:auto;border:1px solid #1a1a1a;border-radius:2px">
            <table style="width:100%;border-collapse:collapse;background:#0a0a0a">
                <thead>
                    <tr style="border-bottom:2px solid #f5a623">
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f;white-space:nowrap">DISCLOSED</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f;white-space:nowrap">TRADE DATE</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">POLITICIAN</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">PTY</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">SCORE</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">TICKER</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">COMPANY</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">SIZE</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">ENTRY</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">LAG</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">COMMITTEE</th>
                    </tr>
                </thead>
                <tbody>{feed_rows_html}</tbody>
            </table>
            </div>
            """)

    with tab3:
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="dash-header">SCORING MODEL</div>', unsafe_allow_html=True)
        st.markdown("""
        <div style="font-family:IBM Plex Mono,monospace;font-size:12px;color:#ccc;line-height:2.2;margin-bottom:24px">
            <span style="color:#f5a623;font-weight:600">Strategy</span>&nbsp;&nbsp;&nbsp;
            10% stop / 10% target · entry at market-on-open (day after disclosure)<br>
            <span style="color:#f5a623;font-weight:600">Score formula</span>&nbsp;&nbsp;&nbsp;
            Win Rate 50% &nbsp;·&nbsp; Avg Drawdown 30% &nbsp;·&nbsp; Large Trade Accuracy 20%<br>
            <span style="color:#f5a623;font-weight:600">Min trades</span>&nbsp;&nbsp;&nbsp;
            5 trades to appear on leaderboard &nbsp;·&nbsp;
            <span style="color:#f5a623">&lt;10 trades = LOW CONFIDENCE *</span><br>
            <span style="color:#f5a623;font-weight:600">Asset split</span>&nbsp;&nbsp;&nbsp;
            Stocks and ETFs/funds scored separately — ETF score shown on politician profile page
        </div>
        """, unsafe_allow_html=True)

        st.markdown('<div class="dash-header">TRADE FILTERS</div>', unsafe_allow_html=True)
        st.markdown("""
        <div style="overflow-x:auto;border:1px solid #1a1a1a;border-radius:2px;margin-bottom:24px">
        <table style="width:100%;border-collapse:collapse;font-family:IBM Plex Mono,monospace;font-size:12px;background:#0a0a0a">
            <thead>
                <tr style="border-bottom:2px solid #f5a623">
                    <th style="padding:8px 16px;color:#f5a623;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f;width:260px">FILTER</th>
                    <th style="padding:8px 16px;color:#f5a623;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">WHY</th>
                </tr>
            </thead>
            <tbody>
                <tr style="border-bottom:1px solid #141414">
                    <td style="padding:10px 16px;color:#fff;font-weight:600">Compliant buys only</td>
                    <td style="padding:10px 16px;color:#888">Amended or late filings (STOCK Act violations) are excluded — the disclosure date is unreliable as a trade entry signal when the filing itself is non-compliant</td>
                </tr>
                <tr style="border-bottom:1px solid #141414">
                    <td style="padding:10px 16px;color:#fff;font-weight:600">Valid disclosure date</td>
                    <td style="padding:10px 16px;color:#888">Disclosure date must exist and be non-blank — it is the entry point for the simulation</td>
                </tr>
                <tr style="border-bottom:1px solid #141414">
                    <td style="padding:10px 16px;color:#fff;font-weight:600">Entry price required</td>
                    <td style="padding:10px 16px;color:#888">Entry is the opening price of the first trading day after disclosure (market-on-open). Yahoo Finance must return path data for the ticker — delistings, bad tickers, or failed fetches are excluded</td>
                </tr>
                <tr style="border-bottom:1px solid #141414">
                    <td style="padding:10px 16px;color:#fff;font-weight:600">Failed price fetch excluded</td>
                    <td style="padding:10px 16px;color:#888">Tickers where Yahoo Finance returned no price data at all (delisted, bad ticker, data gap) are permanently flagged and skipped</td>
                </tr>
                <tr style="border-bottom:1px solid #141414">
                    <td style="padding:10px 16px;color:#fff;font-weight:600">Trade date required</td>
                    <td style="padding:10px 16px;color:#888">Trades with blank trade dates indicate poorly-filed disclosures and are excluded</td>
                </tr>
                <tr style="border-bottom:1px solid #141414">
                    <td style="padding:10px 16px;color:#fff;font-weight:600">ETFs scored separately</td>
                    <td style="padding:10px 16px;color:#888">Broad market funds (ETFs, index funds) can't reflect individual stock edge — they are scored independently and not included in the stock score</td>
                </tr>
                <tr style="border-bottom:1px solid #141414">
                    <td style="padding:10px 16px;color:#fff;font-weight:600">&ge;10 tickers same day excluded</td>
                    <td style="padding:10px 16px;color:#888">Single-day basket buys across 10 or more stocks are portfolio construction events, not informed picks</td>
                </tr>
                <tr style="border-bottom:1px solid #141414">
                    <td style="padding:10px 16px;color:#fff;font-weight:600">&ge;20 tickers same filing excluded</td>
                    <td style="padding:10px 16px;color:#888">Multi-day trades filed together in one disclosure event (e.g. 100+ tickers at once) are portfolio dumps, not individual signals</td>
                </tr>
                <tr>
                    <td style="padding:10px 16px;color:#fff;font-weight:600">Within-window repeat buys excluded</td>
                    <td style="padding:10px 16px;color:#888">If a position in a ticker is already open, re-buying the same ticker adds no new signal — only the first buy per open window is scored. Shown in trade tables as <span style="background:#1a1a00;color:#888;border:1px solid #333;font-size:9px;padding:1px 5px;border-radius:2px;letter-spacing:1px">SKIP</span></td>
                </tr>
            </tbody>
        </table>
        </div>
        """, unsafe_allow_html=True)

        st.markdown('<div class="dash-header">DATA NOTES</div>', unsafe_allow_html=True)
        st.markdown("""
        <div style="font-family:IBM Plex Mono,monospace;font-size:12px;color:#888;line-height:2.2">
            <span style="color:#ccc">Committee data</span>&nbsp;&nbsp;&nbsp;
            116th–119th Congress for House · 119th Congress only for Senate
            (senate scraper captured current memberships only — senate committee flags are thinner)<br>
            <span style="color:#ccc">Price data</span>&nbsp;&nbsp;&nbsp;
            OHLC paths fetched from Yahoo Finance · tracked from disclosure date until stop or target is hit · open positions extended to today<br>
            <span style="color:#ccc">Trade data</span>&nbsp;&nbsp;&nbsp;
            Sourced from Capitol Trades · covers House and Senate filings
        </div>
        """, unsafe_allow_html=True)

    with tab4:
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="dash-header">COMMITTEE SIGNAL ANALYSIS</div>', unsafe_allow_html=True)
        st.markdown("""
        <div style="font-family:IBM Plex Mono,monospace;font-size:12px;color:#888;
                    line-height:2.0;margin-bottom:20px">
            Win rates for trades where the disclosing politician sat on a committee with
            oversight of the traded sector at the time of disclosure.
            Tiers based on observed edge across the data set —
            <span style="color:#4caf50;font-weight:600">STRONG</span> = meaningful edge ·
            <span style="color:#3a7abf">NEUTRAL</span> = no clear signal ·
            <span style="color:#555">WEAK</span> = at or below random.
        </div>
        """, unsafe_allow_html=True)

        comm_stats = load_committee_stats()

        if comm_stats.empty:
            st.markdown('<div style="color:#444;font-family:IBM Plex Mono,monospace;'
                        'font-size:12px">No committee data available.</div>',
                        unsafe_allow_html=True)
        else:
            comm_rows_html = ""
            for _, row in comm_stats.iterrows():
                tier = row["Tier"]
                if tier == "STRONG":
                    t_bg, t_fg, t_bd = "#0f1a0f", "#4caf50", "#1a3a1a"
                elif tier == "WEAK":
                    t_bg, t_fg, t_bd = "#141414", "#555",    "#2a2a2a"
                else:
                    t_bg, t_fg, t_bd = "#0a1218", "#3a7abf", "#0d2a40"

                wr      = row["Win%"]
                wr_col  = "#4caf50" if wr >= 65 else "#ff9800" if wr >= 55 else "#ff5252"
                dd_str  = f"{row['Avg DD']:.2f}%" if row["Avg DD"] is not None else "—"
                tier_badge = (f'<span style="background:{t_bg};color:{t_fg};'
                              f'border:1px solid {t_bd};font-size:9px;padding:2px 6px;'
                              f'border-radius:2px;letter-spacing:1px">{tier}</span>')

                comm_rows_html += f"""<tr style="border-bottom:1px solid #141414">
                    <td style="padding:8px 14px;color:#e0e0e0;font-family:IBM Plex Mono,
                               monospace;font-size:12px">{row['Committee']}</td>
                    <td style="padding:8px 14px;color:#888;font-family:IBM Plex Mono,
                               monospace;font-size:12px;text-align:right">{int(row['n'])}</td>
                    <td style="padding:8px 14px;color:{wr_col};font-family:IBM Plex Mono,
                               monospace;font-size:12px;font-weight:600;text-align:right">{wr:.1f}%</td>
                    <td style="padding:8px 14px;color:#ff9800;font-family:IBM Plex Mono,
                               monospace;font-size:12px;text-align:right">{dd_str}</td>
                    <td style="padding:8px 14px">{tier_badge}</td>
                </tr>"""

            st.html(f"""
            <div style="overflow-x:auto;border:1px solid #1a1a1a;border-radius:2px;
                        margin-bottom:24px">
            <table style="width:100%;border-collapse:collapse;background:#0a0a0a">
                <thead>
                    <tr style="border-bottom:2px solid #f5a623">
                        <th style="padding:8px 14px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                                   font-size:10px;letter-spacing:2px;text-align:left;
                                   background:#0f0f0f">COMMITTEE</th>
                        <th style="padding:8px 14px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                                   font-size:10px;letter-spacing:2px;text-align:right;
                                   background:#0f0f0f">N</th>
                        <th style="padding:8px 14px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                                   font-size:10px;letter-spacing:2px;text-align:right;
                                   background:#0f0f0f">WIN%</th>
                        <th style="padding:8px 14px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                                   font-size:10px;letter-spacing:2px;text-align:right;
                                   background:#0f0f0f">AVG DD</th>
                        <th style="padding:8px 14px;color:#f5a623;font-family:IBM Plex Mono,monospace;
                                   font-size:10px;letter-spacing:2px;text-align:left;
                                   background:#0f0f0f">TIER</th>
                    </tr>
                </thead>
                <tbody>{comm_rows_html}</tbody>
            </table>
            </div>
            """)

        st.markdown('<div class="dash-header">DATA NOTES</div>', unsafe_allow_html=True)
        st.markdown("""
        <div style="font-family:IBM Plex Mono,monospace;font-size:12px;color:#888;line-height:2.2">
            <span style="color:#ccc">House coverage</span>&nbsp;&nbsp;&nbsp;
            116th–119th Congress (2019–present) via Clerk of the House XML snapshots<br>
            <span style="color:#ccc">Senate coverage</span>&nbsp;&nbsp;&nbsp;
            119th Congress only (Jan 2025–present) — senate scraper captured current memberships only,
            so senate committee sample sizes are smaller and pre-2025 senate trades are not flagged<br>
            <span style="color:#ccc">Date granularity</span>&nbsp;&nbsp;&nbsp;
            Membership dates are congress-level (e.g. 2023-01-03 to 2025-01-03 for 118th) —
            mid-congress roster changes are not captured in the Clerk XML snapshots.
            Estimated ~10–20 vacancies per congress may cause a small number of
            post-resignation trades to be incorrectly flagged<br>
            <span style="color:#ccc">Flagging logic</span>&nbsp;&nbsp;&nbsp;
            A trade is flagged when the politician sat on a committee with oversight
            of the traded sector at the time of disclosure
        </div>
        """, unsafe_allow_html=True)

    with tab5:
        import plotly.graph_objects as go

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="dash-header">SCORING PIPELINE — FILTER FUNNEL</div>',
                    unsafe_allow_html=True)
        st.markdown("""
        <div style="font-family:IBM Plex Mono,monospace;font-size:12px;color:#888;
                    line-height:2.0;margin-bottom:20px">
            How raw trade records flow through each filter before reaching the scorer.
            Numbers are live counts from the database.
        </div>
        """, unsafe_allow_html=True)

        funnel_steps, asset_breakdown, win_by_year = load_pipeline_funnel()

        labels = [s[0] for s in funnel_steps]
        values = [s[1] for s in funnel_steps]

        # Drop colour: grey for early infra steps, amber for scoring-specific steps
        bar_colours = [
            "#555", "#555", "#555", "#555", "#555",   # DB → price fetch
            "#f5a623", "#f5a623", "#f5a623", "#f5a623", "#f5a623",  # stock filters
            "#4caf50",  # scored positions
        ]

        dropped = [values[i-1] - values[i] if i > 0 else 0 for i in range(len(values))]

        fig = go.Figure()

        # Horizontal bar (total count)
        fig.add_trace(go.Bar(
            y=labels,
            x=values,
            orientation="h",
            marker_color=bar_colours[:len(values)],
            text=[f"{v:,}" for v in values],
            textposition="outside",
            textfont=dict(family="IBM Plex Mono", size=11, color="#ccc"),
            hovertemplate="<b>%{y}</b><br>Survivors: %{x:,}<extra></extra>",
        ))

        fig.update_layout(
            paper_bgcolor="#0a0a0a",
            plot_bgcolor="#0a0a0a",
            font=dict(family="IBM Plex Mono", color="#888"),
            height=420,
            margin=dict(l=10, r=120, t=10, b=10),
            xaxis=dict(
                showgrid=True, gridcolor="#1a1a1a",
                tickfont=dict(size=10),
                title=None,
            ),
            yaxis=dict(
                autorange="reversed",
                tickfont=dict(size=11, color="#ccc"),
                title=None,
            ),
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)

        # Drop table
        st.markdown('<div class="dash-header">STEP-BY-STEP DROPS</div>', unsafe_allow_html=True)
        rows_html = ""
        for i, (label, val) in enumerate(funnel_steps):
            drop     = dropped[i]
            drop_pct = f"-{drop / funnel_steps[i-1][1] * 100:.1f}%" if i > 0 and funnel_steps[i-1][1] > 0 else "—"
            drop_str = f"-{drop:,}" if drop > 0 else "—"
            col      = "#ff5252" if drop > 1000 else "#ff9800" if drop > 100 else "#555"
            rows_html += f"""
            <tr style="border-bottom:1px solid #111">
                <td style="padding:6px 14px;color:#ccc;font-family:IBM Plex Mono,monospace;font-size:11px">{label}</td>
                <td style="padding:6px 14px;color:#ccc;font-family:IBM Plex Mono,monospace;font-size:11px;text-align:right">{val:,}</td>
                <td style="padding:6px 14px;color:{col};font-family:IBM Plex Mono,monospace;font-size:11px;text-align:right">{drop_str}</td>
                <td style="padding:6px 14px;color:{col};font-family:IBM Plex Mono,monospace;font-size:11px;text-align:right">{drop_pct}</td>
            </tr>"""

        st.html(f"""
        <div style="overflow-x:auto;border:1px solid #1a1a1a;border-radius:2px;margin-bottom:24px">
        <table style="width:100%;border-collapse:collapse;background:#0a0a0a">
            <thead>
                <tr style="border-bottom:2px solid #f5a623">
                    <th style="padding:8px 14px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">FILTER STEP</th>
                    <th style="padding:8px 14px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:right;background:#0f0f0f">SURVIVORS</th>
                    <th style="padding:8px 14px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:right;background:#0f0f0f">DROPPED</th>
                    <th style="padding:8px 14px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:right;background:#0f0f0f">DROP %</th>
                </tr>
            </thead>
            <tbody>{rows_html}</tbody>
        </table></div>""")

        # Asset type breakdown
        c1, c2 = st.columns(2)

        with c1:
            st.markdown('<div class="dash-header">ASSET TYPE BREAKDOWN</div>', unsafe_allow_html=True)
            st.markdown("""
            <div style="font-family:IBM Plex Mono,monospace;font-size:11px;color:#555;
                        margin-bottom:10px">Priced compliant buys only</div>
            """, unsafe_allow_html=True)
            at_rows = ""
            for atype, cnt in asset_breakdown:
                colour = "#4caf50" if atype == "stock" else "#3a7abf" if atype in ("etf","fund") else "#ff5252" if atype == "NULL" else "#555"
                at_rows += f"""<tr style="border-bottom:1px solid #111">
                    <td style="padding:5px 14px;color:{colour};font-family:IBM Plex Mono,monospace;font-size:11px">{atype}</td>
                    <td style="padding:5px 14px;color:#ccc;font-family:IBM Plex Mono,monospace;font-size:11px;text-align:right">{cnt:,}</td>
                </tr>"""
            st.html(f"""<div style="border:1px solid #1a1a1a;border-radius:2px">
            <table style="width:100%;border-collapse:collapse;background:#0a0a0a">
                <thead><tr style="border-bottom:2px solid #333">
                    <th style="padding:7px 14px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">ASSET TYPE</th>
                    <th style="padding:7px 14px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:right;background:#0f0f0f">TRADES</th>
                </tr></thead>
                <tbody>{at_rows}</tbody>
            </table></div>""")

        with c2:
            st.markdown('<div class="dash-header">WIN RATE BY DISCLOSURE YEAR</div>', unsafe_allow_html=True)
            st.markdown("""
            <div style="font-family:IBM Plex Mono,monospace;font-size:11px;color:#555;
                        margin-bottom:10px">Scored stock positions · decided trades only</div>
            """, unsafe_allow_html=True)
            yr_rows = ""
            for yr, total, wins, losses, opens in win_by_year:
                decided   = wins + losses
                wr        = wins / decided * 100 if decided > 0 else 0
                wr_col    = "#4caf50" if wr >= 65 else "#ff9800" if wr >= 55 else "#ff5252"
                yr_rows  += f"""<tr style="border-bottom:1px solid #111">
                    <td style="padding:5px 14px;color:#ccc;font-family:IBM Plex Mono,monospace;font-size:11px">{yr}</td>
                    <td style="padding:5px 14px;color:#888;font-family:IBM Plex Mono,monospace;font-size:11px;text-align:right">{total:,}</td>
                    <td style="padding:5px 14px;color:#4caf50;font-family:IBM Plex Mono,monospace;font-size:11px;text-align:right">{wins}</td>
                    <td style="padding:5px 14px;color:#ff5252;font-family:IBM Plex Mono,monospace;font-size:11px;text-align:right">{losses}</td>
                    <td style="padding:5px 14px;color:#555;font-family:IBM Plex Mono,monospace;font-size:11px;text-align:right">{opens}</td>
                    <td style="padding:5px 14px;color:{wr_col};font-family:IBM Plex Mono,monospace;font-size:11px;text-align:right;font-weight:600">{wr:.1f}%</td>
                </tr>"""
            st.html(f"""<div style="border:1px solid #1a1a1a;border-radius:2px">
            <table style="width:100%;border-collapse:collapse;background:#0a0a0a">
                <thead><tr style="border-bottom:2px solid #333">
                    <th style="padding:7px 14px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">YEAR</th>
                    <th style="padding:7px 14px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:right;background:#0f0f0f">POS</th>
                    <th style="padding:7px 14px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:right;background:#0f0f0f">W</th>
                    <th style="padding:7px 14px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:right;background:#0f0f0f">L</th>
                    <th style="padding:7px 14px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:right;background:#0f0f0f">OPEN</th>
                    <th style="padding:7px 14px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:right;background:#0f0f0f">WIN%</th>
                </tr></thead>
                <tbody>{yr_rows}</tbody>
            </table></div>""")

    with tab6:
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="dash-header">WOULD FOLLOW</div>', unsafe_allow_html=True)
        st.markdown("""
        <div style="font-family:IBM Plex Mono,monospace;font-size:12px;color:#888;
                    line-height:1.8;margin-bottom:16px">
            Trades that meet the live execution filter (execution/rules.md):
            cluster_count_td &ge; 2 AND abs_pct_move_before_disclosure &ge; 15%.
            These are the trades the strategy would actually have taken.
        </div>
        """, unsafe_allow_html=True)

        wf_df = load_would_follow()

        wf_wins   = len(wf_df[wf_df["outcome"] == "win"])   if len(wf_df) else 0
        wf_losses = len(wf_df[wf_df["outcome"] == "loss"])  if len(wf_df) else 0
        wf_opens  = len(wf_df[wf_df["outcome"] == "open"])  if len(wf_df) else 0
        wf_decided = wf_wins + wf_losses
        wf_wr      = wf_wins / wf_decided * 100 if wf_decided > 0 else 0

        wc1, wc2, wc3, wc4 = st.columns(4)
        with wc1:
            st.markdown(f'''<div class="metric-card">
                <div class="metric-label">Flagged Trades</div>
                <div class="metric-value">{len(wf_df)}</div>
                <div class="metric-sub">all-time, backdated</div>
            </div>''', unsafe_allow_html=True)
        with wc2:
            st.markdown(f'''<div class="metric-card">
                <div class="metric-label">Win Rate</div>
                <div class="metric-value">{wf_wr:.0f}%</div>
                <div class="metric-sub">{wf_wins}W / {wf_losses}L, decided only</div>
            </div>''', unsafe_allow_html=True)
        with wc3:
            st.markdown(f'''<div class="metric-card">
                <div class="metric-label">Open</div>
                <div class="metric-value">{wf_opens}</div>
                <div class="metric-sub">still running</div>
            </div>''', unsafe_allow_html=True)
        with wc4:
            latest_flag = wf_df["disclosure_date"].max() if len(wf_df) else None
            st.markdown(f'''<div class="metric-card">
                <div class="metric-label">Latest Flag</div>
                <div class="metric-value" style="font-size:16px;padding-top:4px">{latest_flag or "N/A"}</div>
                <div class="metric-sub">most recent disclosure</div>
            </div>''', unsafe_allow_html=True)

        # Tariff-window caveat (Liberation Day, April 2, 2025 — see README.md).
        # Recomputed live off wf_df's trade_date rather than hardcoded, so it stays
        # accurate as new trades get flagged.
        if wf_decided > 0:
            tariff_mask   = wf_df["trade_date"].notna() & (wf_df["trade_date"] >= "2025-03-01") & (wf_df["trade_date"] < "2025-05-01")
            tariff_df     = wf_df[tariff_mask]
            other_df      = wf_df[~tariff_mask]
            tariff_decided = len(tariff_df[tariff_df["outcome"] != "open"])
            other_decided  = len(other_df[other_df["outcome"] != "open"])
            tariff_wr = (tariff_df["outcome"] == "win").sum() / tariff_decided * 100 if tariff_decided > 0 else 0
            other_wr  = (other_df["outcome"]  == "win").sum() / other_decided  * 100 if other_decided  > 0 else 0
            if tariff_decided > 0:
                st.markdown(f"""
                <div style="font-family:IBM Plex Mono,monospace;font-size:11px;color:#555;
                            line-height:1.6;margin-bottom:16px;border-left:2px solid #333;
                            padding-left:10px">
                    {len(tariff_df)} of {len(wf_df)} flagged trades ({len(tariff_df)/len(wf_df)*100:.0f}%)
                    fall in the Mar–Apr 2025 tariff-crash window (Liberation Day, April 2, 2025) —
                    {tariff_wr:.0f}% WR there vs {other_wr:.0f}% WR on the rest. The blended win rate
                    above is inflated by this single macro event, not a steady-state number.
                </div>
                """, unsafe_allow_html=True)

        if wf_df.empty:
            st.markdown('<div style="color:#444;font-family:IBM Plex Mono;font-size:12px">'
                        'No trades currently meet the would-follow filter.</div>', unsafe_allow_html=True)
        else:
            def wf_fmt_size(v):
                if pd.isna(v): return "—"
                if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
                if v >= 1_000:     return f"${v/1_000:.0f}K"
                return f"${v:.0f}"

            wf_rows_html = ""
            for _, row in wf_df.iterrows():
                pty        = row.get("party", "") or ""
                pty_short  = "R" if pty == "Republican" else "D" if pty == "Democrat" else "?"
                pty_color  = "#ff5f5f" if pty == "Republican" else "#4a9eff"
                ticker     = row.get("ticker") or "—"
                company    = (row.get("company") or "—")[:24]
                name       = (row.get("politician") or "—")[:16]
                size_str   = wf_fmt_size(row.get("size_midpoint"))
                entry      = row.get("entry_price")
                entry_str  = f"${entry:.2f}" if pd.notna(entry) else "—"
                disc_date  = row.get("disclosure_date", "") or ""
                trade_date = row.get("trade_date", "") or "—"
                cluster    = row.get("cluster_count_td")
                cluster_str = f"{int(cluster)}" if pd.notna(cluster) else "—"
                move       = row.get("abs_pct_move_before_disclosure")
                move_str   = f"{move:.1f}%" if pd.notna(move) else "—"
                dd         = row.get("max_drawdown_disc")
                dd_str     = f"{dd:.1f}%" if pd.notna(dd) else "—"
                outcome    = row.get("outcome", "open")
                oc_color   = "#4caf50" if outcome == "win" else "#ff5252" if outcome == "loss" else "#888"

                wf_rows_html += f"""<tr style="border-bottom:1px solid #111">
                    <td style="padding:5px 10px;color:#666;font-family:IBM Plex Mono,monospace;
                               font-size:11px;white-space:nowrap">{disc_date}</td>
                    <td style="padding:5px 10px;color:#666;font-family:IBM Plex Mono,monospace;
                               font-size:11px;white-space:nowrap">{trade_date}</td>
                    <td style="padding:5px 10px;color:#e0e0e0;font-family:IBM Plex Mono,monospace;
                               font-size:11px;max-width:90px;overflow:hidden;
                               text-overflow:ellipsis;white-space:nowrap">{name}</td>
                    <td style="padding:5px 10px;color:{pty_color};font-family:IBM Plex Mono,monospace;
                               font-size:11px">{pty_short}</td>
                    <td style="padding:5px 10px;font-family:IBM Plex Mono,monospace;
                               font-size:11px;max-width:120px">
                        <div style="color:#fff;font-weight:600">{company}</div>
                        <div style="color:#666;font-size:10px;margin-top:1px">({ticker})</div>
                    </td>
                    <td style="padding:5px 10px;color:#ccc;font-family:IBM Plex Mono,monospace;
                               font-size:11px;white-space:nowrap">{size_str}</td>
                    <td style="padding:5px 10px;color:#888;font-family:IBM Plex Mono,monospace;
                               font-size:11px;white-space:nowrap">{entry_str}</td>
                    <td style="padding:5px 10px;color:#ccc;font-family:IBM Plex Mono,monospace;
                               font-size:11px;text-align:center">{cluster_str}</td>
                    <td style="padding:5px 10px;color:#ccc;font-family:IBM Plex Mono,monospace;
                               font-size:11px;text-align:right">{move_str}</td>
                    <td style="padding:5px 10px;color:#888;font-family:IBM Plex Mono,monospace;
                               font-size:11px;text-align:right">{dd_str}</td>
                    <td style="padding:5px 10px;font-family:IBM Plex Mono,monospace;
                               font-size:12px;font-weight:600;color:{oc_color}">{outcome.upper()}</td>
                </tr>"""

            st.html(f"""
            <div style="overflow-x:auto;border:1px solid #1a1a1a;border-radius:2px">
            <table style="width:100%;border-collapse:collapse;background:#0a0a0a">
                <thead>
                    <tr style="border-bottom:2px solid #f5a623">
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f;white-space:nowrap">DISCLOSED</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f;white-space:nowrap">TRADE DATE</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">POLITICIAN</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">PTY</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">COMPANY</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">SIZE</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">ENTRY</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:center;background:#0f0f0f">CLUSTER</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:right;background:#0f0f0f">MOVE</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:right;background:#0f0f0f">MAX DD</th>
                        <th style="padding:7px 10px;color:#f5a623;font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:2px;text-align:left;background:#0f0f0f">OUTCOME</th>
                    </tr>
                </thead>
                <tbody>{wf_rows_html}</tbody>
            </table>
            </div>
            """)

    with tab1:
        st.markdown("<br>", unsafe_allow_html=True)

        s = load_summary_stats()

        # Apply filters + combo simulation first so stats panel reflects them
        df = df_all.copy()
        if party_filter   != "All": df = df[df["party"]   == party_filter]
        if chamber_filter != "All": df = df[df["chamber"] == chamber_filter]
        if state_filter   != "All": df = df[df["state"]   == state_filter]
        if min_trades > 1:          df = df[df["total_trades"] >= min_trades]
        if recency_days < 9999:
            cutoff = (date.today() - timedelta(days=recency_days)).strftime("%Y-%m-%d")
            df = df[df["last_trade_date"].notna() & (df["last_trade_date"] >= cutoff)]
        if comm_aligned_only:       df = df[df["comm_aligned"] > 0]
        if disc_lag_max < 999:
            df = df[df["avg_filing_lag"].notna() & (df["avg_filing_lag"] <= disc_lag_max)]
    
        using_combo = (stop_pct != 10 or target_pct != 10 or min_size > 0)
        if using_combo:
            with st.spinner(f"Simulating {stop_pct}% stop / {target_pct}% target"
                            + (f" · ${min_size:,}+ trades" if min_size > 0 else "") + "..."):
                combo = load_combo_stats(stop_pct, target_pct, min_size)
            if not combo.empty:
                df = df.merge(combo, on="politician_id", how="left")
                df["win_rate"]         = df["combo_wr"].combine_first(df["win_rate"])
                df["avg_dd"]           = df["combo_dd"].combine_first(df["avg_dd"])
                df["compliant_trades"] = df["combo_trades"].combine_first(df["compliant_trades"])
                df = df.sort_values("combo_wr", ascending=False).reset_index(drop=True)
        else:
            df = df.sort_values("score", ascending=False).reset_index(drop=True)
    
        # ── Row 1: Data overview (static) ────────
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.markdown(f'''<div class="metric-card">
                <div class="metric-label">Politicians Shown</div>
                <div class="metric-value">{len(df)}</div>
                <div class="metric-sub">of {s["scored"]} scored · {s["total_pols"]} tracked</div>
            </div>''', unsafe_allow_html=True)
        with col2:
            total_shown_trades = int(df["total_trades"].sum()) if not df.empty else 0
            st.markdown(f'''<div class="metric-card">
                <div class="metric-label">Buy Positions</div>
                <div class="metric-value">{total_shown_trades:,}</div>
                <div class="metric-sub">across shown politicians</div>
            </div>''', unsafe_allow_html=True)
        with col3:
            st.markdown(f'''<div class="metric-card">
                <div class="metric-label">Unique Tickers</div>
                <div class="metric-value">{s["tickers"]:,}</div>
                <div class="metric-sub">all compliant buys</div>
            </div>''', unsafe_allow_html=True)
        with col4:
            st.markdown(f'''<div class="metric-card">
                <div class="metric-label">Latest Disclosure</div>
                <div class="metric-value" style="font-size:16px;padding-top:4px">{s["latest"] or "N/A"}</div>
                <div class="metric-sub">most recent filing</div>
            </div>''', unsafe_allow_html=True)
    
        st.markdown("<br>", unsafe_allow_html=True)
    
        # ── Row 2: Performance (trade-weighted from filtered df) ─
        if not df.empty and using_combo and "combo_wins" in df.columns:
            # Use raw win/loss counts from combo simulation
            total_w   = int(df["combo_wins"].fillna(0).sum())
            total_l   = int(df["combo_losses"].fillna(0).sum())
            decided   = total_w + total_l
            avg_wr    = round(total_w / decided * 100, 1) if decided > 0 else None
            avg_dd_v  = round(df["combo_dd"].dropna().mean(), 2) if df["combo_dd"].notna().any() else None
            r_sub     = df[df["party"] == "Republican"]
            d_sub     = df[df["party"] == "Democrat"]
            r_w = int(r_sub["combo_wins"].fillna(0).sum()); r_l = int(r_sub["combo_losses"].fillna(0).sum())
            d_w = int(d_sub["combo_wins"].fillna(0).sum()); d_l = int(d_sub["combo_losses"].fillna(0).sum())
            r_wr = round(r_w / (r_w + r_l) * 100, 1) if (r_w + r_l) > 0 else None
            d_wr = round(d_w / (d_w + d_l) * 100, 1) if (d_w + d_l) > 0 else None
        elif not df.empty:
            # Default 10/10 — use verified ground truth from load_summary_stats
            # Matches scorer.py exactly: Overall 56.6%, R 57.9%, D 56.0%
            avg_wr   = s["overall_wr"]
            avg_dd_v = float(s["avg_dd"]) if s["avg_dd"] else None
            total_w  = s["wins"] or 0
            total_l  = s["losses"] or 0
            decided  = total_w + total_l
            r_wr     = s["party_wr"].get("Republican")
            d_wr     = s["party_wr"].get("Democrat")
            if party_filter == "Republican": d_wr = None
            if party_filter == "Democrat":   r_wr = None
    
        wr_color = "#4caf50" if (avg_wr or 0) >= 60 else "#ff9800"
        dd_str   = f"{avg_dd_v:.2f}%" if avg_dd_v else "N/A"
        r_str    = f"{r_wr:.1f}%" if r_wr else "N/A"
        d_str    = f"{d_wr:.1f}%" if d_wr else "N/A"
        target_label = f"+{target_pct}% target" if using_combo else "+10% target"
    
        col5, col6, col7, col8 = st.columns(4)
        with col5:
            wl_sub = f"{total_w:,}W · {total_l:,}L" if total_w is not None else "—"
            st.markdown(f'''<div class="metric-card">
                <div class="metric-label">Overall Win Rate</div>
                <div class="metric-value" style="color:{wr_color}">{avg_wr or "N/A"}%</div>
                <div class="metric-sub">{wl_sub} · trade weighted</div>
            </div>''', unsafe_allow_html=True)
        with col6:
            st.markdown(f'''<div class="metric-card">
                <div class="metric-label">Avg DD (Winning Trades)</div>
                <div class="metric-value" style="color:#ff9800">{dd_str}</div>
                <div class="metric-sub">before {target_label}</div>
            </div>''', unsafe_allow_html=True)
        with col7:
            st.markdown(f'''<div class="metric-card">
                <div class="metric-label">Republican Win Rate</div>
                <div class="metric-value" style="color:#ff5f5f">{r_str}</div>
                <div class="metric-sub">shown politicians only</div>
            </div>''', unsafe_allow_html=True)
        with col8:
            st.markdown(f'''<div class="metric-card">
                <div class="metric-label">Democrat Win Rate</div>
                <div class="metric-value" style="color:#4a9eff">{d_str}</div>
                <div class="metric-sub">shown politicians only</div>
            </div>''', unsafe_allow_html=True)
    
        st.markdown("<br>", unsafe_allow_html=True)
    
        combo_label = ""
        if stop_pct != 10 or target_pct != 10:
            combo_label = f" · {stop_pct}% STOP / {target_pct}% TARGET"
        if min_size > 0:
            combo_label += f" · ${min_size:,}+ ONLY"
    
        st.markdown(f'<div class="filter-label">SHOWING {len(df)} POLITICIANS{combo_label} — '
                    f'SELECT A NAME IN THE SIDEBAR TO VIEW PROFILE</div>', unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)
    
        if df.empty:
            st.warning("No politicians match the current filters.")
            return
    
        # Politician selector — clickable buttons
        rows_html = ""
        for i, row in df.iterrows():
            rank     = i + 1
            name     = row["name"]
            pol_id   = row["politician_id"]
            party    = row["party"] or ""
            chamber  = row["chamber"] or ""
            state    = row["state"] or ""
            score    = row["score"] if pd.notna(row["score"]) else 0
            win_rate = row["win_rate"] if pd.notna(row["win_rate"]) else 0
            trades   = int(row["total_trades"] or 0)
            late     = int(row["late_filings"] or 0)
            avg_dd   = row["avg_dd"]
            avg_lag  = row["avg_filing_lag"]
            large_wr = row["large_trade_wr"]
    
            pty_color     = "#ff5f5f" if party == "Republican" else "#4a9eff"
            pty_short     = "R" if party == "Republican" else "D"
            conf_badge    = (' <span style="background:#1a1200;color:#f5a623;border:1px solid #3d2a00;'
                             'font-size:9px;padding:1px 5px;border-radius:2px;margin-left:6px;">'
                             'LOW CONF</span>') if trades < 10 else ""
            late_color    = "#ff5252" if late > 0 else "#444"
            dd_str        = f"{avg_dd:.1f}%"   if pd.notna(avg_dd)   else "—"
            lag_str       = f"{avg_lag:.0f}d"  if pd.notna(avg_lag)  else "—"
            large_str     = f"{large_wr:.0f}%" if pd.notna(large_wr) else "—"
            wr_color      = "#4caf50" if win_rate >= 65 else "#ff9800" if win_rate >= 55 else "#ff5252"
            sc_color      = "#4caf50" if score   >= 70 else "#f5a623" if score   >= 40 else "#ff5252"
            last_trade    = row.get("last_trade_date") or ""
            if last_trade:
                days_since = (date.today() - datetime.strptime(last_trade, "%Y-%m-%d").date()).days
                if days_since <= 180:   lt_color = "#4caf50"
                elif days_since <= 365: lt_color = "#f5a623"
                else:                   lt_color = "#ff5252"
                lt_str = last_trade
            else:
                lt_color, lt_str = "#444", "—"
            comm_pct      = row.get("comm_align_pct", 0.0) or 0.0
            comm_str      = f"{comm_pct:.0f}%" if comm_pct > 0 else "—"
            comm_color    = "#4caf50" if comm_pct >= 30 else "#f5a623" if comm_pct > 0 else "#333"
            etf_score     = row.get("score_etf")
            etf_wr        = row.get("win_rate_etf")
            etf_n         = row.get("etf_trade_count")
            if pd.notna(etf_score) and etf_score is not None:
                etf_sc_color = "#4caf50" if etf_score >= 70 else "#f5a623" if etf_score >= 40 else "#ff5252"
                etf_str      = f"{etf_score:.1f}"
                etf_wr_str   = f"{etf_wr:.1f}%" if pd.notna(etf_wr) else "—"
                etf_n_str    = f"({int(etf_n)})" if pd.notna(etf_n) else ""
            else:
                etf_sc_color = "#333"
                etf_str      = "—"
                etf_wr_str   = "—"
                etf_n_str    = ""
            # When using combo, rank by win_rate; show win_rate bar instead of score bar
            if using_combo:
                bar_w    = min(int(win_rate or 0), 100)
                bar_val  = f"{win_rate:.1f}%" if pd.notna(win_rate) else "—"
                bar_lbl  = "WIN%"
                bar_col  = wr_color
            else:
                bar_w    = min(int(score), 100)
                bar_val  = f"{score:.1f}"
                bar_lbl  = "SCORE"
                bar_col  = sc_color
    
            td = "padding:5px 7px;font-family:IBM Plex Mono,monospace;font-size:11px;white-space:nowrap"
            rows_html += f"""<tr style="border-bottom:1px solid #141414"
                                 data-polid="{pol_id}" data-polname="{name}"
                                 class="pol-row">
                <td style="{td};color:#444;width:30px">{rank}</td>
                <td style="padding:5px 7px;width:200px">
                    <span style="color:#f5a623;font-weight:500;cursor:pointer;
                                 font-family:IBM Plex Mono,monospace;font-size:11px"
                          class="pol-name-link">{name}</span>{conf_badge}
                </td>
                <td style="{td};color:{pty_color};width:30px">{pty_short}</td>
                <td style="{td};color:#888;width:55px">{chamber}</td>
                <td style="padding:5px 7px;width:100px">
                    <span style="color:{bar_col};font-weight:600;font-size:12px;
                                 font-family:IBM Plex Mono,monospace">{bar_val}</span>
                    <div style="background:#1a1a1a;height:2px;width:100%;
                                margin-top:3px;border-radius:1px">
                        <div style="background:{bar_col};height:2px;
                                    width:{bar_w}%;border-radius:1px"></div>
                    </div>
                </td>
                <td style="{td};color:{wr_color};width:55px">{win_rate:.1f}%</td>
                <td style="{td};color:#ff9800;width:55px">{dd_str}</td>
                <td style="{td};color:#888;width:60px">{large_str}</td>
                <td style="{td};color:#888;width:45px">{trades}</td>
                <td style="{td};color:{lt_color};width:90px">{lt_str}</td>
                <td style="{td};color:{late_color};width:40px">{late}</td>
                <td style="{td};color:#888;width:45px">{lag_str}</td>
                <td style="{td};color:{comm_color};width:55px">{comm_str}</td>
                <td style="padding:5px 7px;width:90px">
                    <span style="color:{etf_sc_color};font-weight:600;font-size:12px;
                                 font-family:IBM Plex Mono,monospace">{etf_str}</span>
                    <span style="color:#555;font-size:10px;font-family:IBM Plex Mono,monospace;
                                 margin-left:3px">{etf_wr_str} {etf_n_str}</span>
                </td>
            </tr>"""
    
        th = "padding:6px 7px;font-size:10px;letter-spacing:1px;text-align:left;background:#0f0f0f;white-space:nowrap"
        st.html(f"""
        <div style="overflow-x:auto;border:1px solid #1a1a1a;border-radius:2px">
        <table style="width:100%;border-collapse:collapse;font-family:'IBM Plex Mono',monospace;
                      font-size:11px;background:#0a0a0a">
            <thead>
                <tr style="border-bottom:2px solid #f5a623">
                    <th style="{th};color:#f5a623">#</th>
                    <th style="{th};color:#f5a623">POLITICIAN</th>
                    <th style="{th};color:#f5a623">PTY</th>
                    <th style="{th};color:#f5a623">CHAMBER</th>
                    <th style="{th};color:#f5a623">{"WIN%" if using_combo else "SCORE"}</th>
                    <th style="{th};color:#f5a623">WIN%</th>
                    <th style="{th};color:#f5a623">AVG DD</th>
                    <th style="{th};color:#f5a623">LG WIN%</th>
                    <th style="{th};color:#f5a623">BUYS</th>
                    <th style="{th};color:#f5a623">LAST BUY</th>
                    <th style="{th};color:#f5a623">LATE</th>
                    <th style="{th};color:#f5a623">LAG</th>
                    <th style="{th};color:#f5a623">COMM%</th>
                    <th style="{th};color:#4a9eff">ETF</th>
                </tr>
            </thead>
            <tbody>{rows_html}</tbody>
        </table>
        </div>
        """)
    
        # CSV export
        csv = df[["name","party","chamber","state","score","win_rate",
                  "avg_dd","large_trade_wr","compliant_trades",
                  "late_filings","avg_filing_lag","comm_align_pct"]].copy()
        csv.columns = ["Politician","Party","Chamber","State","Score","Win%",
                       "Avg DD","Large Win%","Comp Buys","Late Files","Avg Lag","Comm Align%"]
        st.download_button(
            label="⬇ EXPORT CSV",
            data=csv.to_csv(index=False),
            file_name="leaderboard.csv",
            mime="text/csv",
        )


# ─────────────────────────────────────────────
# ROUTER
# ─────────────────────────────────────────────

if st.session_state.selected_pol_id:
    render_profile(st.session_state.selected_pol_id)
else:
    render_leaderboard(stop_pct=stop_pct, target_pct=target_pct, min_size=min_size)

# Footer
st.markdown("<br><br>", unsafe_allow_html=True)
st.markdown("""
<div style="font-family: IBM Plex Mono; font-size: 10px; color: #2a2a2a;
            border-top: 1px solid #141414; padding-top: 16px; letter-spacing: 1px;">
CONGRESSIONAL TRADE TRACKER · DATA SOURCE: CAPITOL TRADES ·
SCORES BASED ON 10/10 STOP/TARGET FROM DISCLOSURE DATE ·
NOT FINANCIAL ADVICE
</div>
""", unsafe_allow_html=True)