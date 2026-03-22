# dashboard/app.py
# Congressional Trade Tracker — Main Dashboard
# Run with: streamlit run dashboard/app.py

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st
import sqlite3
import pandas as pd
from collections import defaultdict
from pipeline.committee_config import COMMITTEE_NAMES

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

DB_PATH    = Path(__file__).parent.parent / "data" / "trades.db"
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

@st.cache_data(ttl=300)
def load_leaderboard():
    conn = sqlite3.connect(DB_PATH)
    pols = pd.read_sql_query("""
        SELECT politician_id, name, party, chamber, state,
               score, win_rate_trade as win_rate, total_trades,
               score_etf, win_rate_etf, etf_trade_count
        FROM politicians
        WHERE score IS NOT NULL OR score_etf IS NOT NULL
        ORDER BY COALESCE(score, 0) DESC
    """, conn)
    buys = pd.read_sql_query("""
        SELECT politician_id, ticker, disclosure_date,
               price_at_disclosure_date, size_midpoint, max_drawdown_disc
        FROM (
            SELECT politician_id, ticker, disclosure_date,
                   price_at_disclosure_date,
                   MAX(size_midpoint) as size_midpoint,
                   AVG(max_drawdown_disc) as max_drawdown_disc
            FROM trades
            WHERE transaction_type = 'buy'
            AND filing_violation = 'compliant'
            AND price_at_disclosure_date IS NOT NULL
            AND disclosure_date IS NOT NULL
            AND disclosure_date != ''
            GROUP BY politician_id, ticker, disclosure_date, price_at_disclosure_date
        )
    """, conn)
    filings = pd.read_sql_query("""
        SELECT politician_id,
               ROUND(AVG(CASE WHEN filing_lag_days <= 45 THEN filing_lag_days END), 1) as avg_filing_lag,
               SUM(CASE WHEN filing_lag_days > 45 THEN 1 ELSE 0 END) as late_filings
        FROM trades
        GROUP BY politician_id
    """, conn)

    # Committee alignment: deduped compliant buys where any underlying trade is committee-relevant
    comm = pd.read_sql_query("""
        SELECT politician_id, COUNT(*) as comm_aligned
        FROM (
            SELECT politician_id, ticker, disclosure_date, price_at_disclosure_date,
                   MAX(CASE WHEN committee_relevance IS NOT NULL THEN 1 ELSE 0 END) as is_aligned
            FROM trades
            WHERE transaction_type = 'buy'
            AND filing_violation = 'compliant'
            AND price_at_disclosure_date IS NOT NULL
            AND disclosure_date IS NOT NULL AND disclosure_date != ''
            GROUP BY politician_id, ticker, disclosure_date, price_at_disclosure_date
        )
        WHERE is_aligned = 1
        GROUP BY politician_id
    """, conn)

    conn.close()

    def agg_buys(g):
        all_dd     = g[g["max_drawdown_disc"].notna() &
                       (g["max_drawdown_disc"] > -10) &
                       (g["max_drawdown_disc"] < 0)]
        large      = g[g["size_midpoint"] >= 50000]
        large_wins = large[large["max_drawdown_disc"] > -10] if len(large) else large
        return pd.Series({
            "compliant_trades": len(g),
            "avg_dd":           round(all_dd["max_drawdown_disc"].mean(), 2) if len(all_dd) else None,
            "large_trade_wr":   round(len(large_wins) / len(large) * 100, 1) if len(large) else None,
        })

    buy_stats = buys.groupby("politician_id").apply(agg_buys).reset_index()
    df = pols.merge(buy_stats, on="politician_id", how="left")
    df = df.merge(filings, on="politician_id", how="left")
    df = df.merge(comm, on="politician_id", how="left")
    df["late_filings"]     = df["late_filings"].fillna(0).astype(int)
    df["compliant_trades"] = df["compliant_trades"].fillna(0).astype(int)
    df["comm_aligned"]     = df["comm_aligned"].fillna(0).astype(int)
    df["comm_align_pct"]   = (
        df["comm_aligned"] / df["compliant_trades"].replace(0, float("nan")) * 100
    ).round(1).fillna(0.0)
    return df


@st.cache_data(ttl=300)
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
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    c.execute("SELECT COUNT(*) FROM politicians WHERE score IS NOT NULL")
    scored = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM politicians")
    total_pols = c.fetchone()[0]

    c.execute("SELECT COUNT(DISTINCT ticker) FROM trades WHERE transaction_type='buy' AND filing_violation='compliant'")
    tickers = c.fetchone()[0]

    c.execute("SELECT MAX(disclosure_date) FROM trades WHERE disclosure_date != ''")
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
    conn = sqlite3.connect(DB_PATH)

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
               MAX(asset_type) as asset_type
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

    # Path rows for simulation
    if len(trades) > 0:
        trade_ids = trades["trade_id"].tolist()
        placeholders = ",".join("?" * len(trade_ids))
        paths_df = pd.read_sql_query(f"""
            SELECT trade_id, day, date, high, low, close
            FROM trade_price_paths
            WHERE anchor = 'disc'
            AND trade_id IN ({placeholders})
            ORDER BY trade_id, day ASC
        """, conn, params=trade_ids)
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
            "ticker":              t["ticker"],
            "disc_date":           t["disclosure_date"],
            "entry":               entry,
            "stop":                round(stop, 2),
            "target":              round(target, 2),
            "size":                t["size"],
            "dd":                  t["dd"],
            "outcome":             outcome,
            "exit_day":            exit_day,
            "exit_date":           exit_date,
            "committee_relevance": t.get("committee_relevance"),
        })

    return pd.DataFrame(results)


# ─────────────────────────────────────────────
# COMBO STATS (custom stop/target re-simulation)
# ─────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_combo_stats(stop_pct, target_pct, min_size=0):
    """
    Re-simulate all compliant buy trades with custom stop/target.
    Returns df with politician_id, win_rate, avg_dd, compliant_trades.
    Uses path data — takes a few seconds on first run, then cached.
    """
    conn   = sqlite3.connect(DB_PATH)

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
    paths = pd.read_sql_query(f"""
        SELECT trade_id, day, high, low
        FROM trade_price_paths
        WHERE anchor = 'disc'
        AND trade_id IN ({placeholders})
        ORDER BY trade_id, day ASC
    """, conn, params=trade_ids)
    conn.close()

    # Group paths
    from collections import defaultdict
    paths_by_trade = defaultdict(list)
    for _, r in paths.iterrows():
        paths_by_trade[r["trade_id"]].append((r["day"], r["high"], r["low"]))

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
    <span style="color:#555">✓</span> Compliant buy trades only<br>
    <span style="color:#555">✓</span> Valid entry price required<br>
    <span style="color:#555">✓</span> Corrupt price data excluded<br>
    <span style="color:#555">✓</span> ETFs scored separately<br>
    <span style="color:#555">✓</span> Trade date required<br>
    <span style="color:#555">✓</span> &ge;10 tickers/day excluded<br>
    <br>
    <span style="color:#333;font-size:9px">
    ETFs excluded from stock score —<br>
    broad market buys can't reflect<br>
    individual stock edge.<br><br>
    Blank trade dates &amp; cluster buys<br>
    (&ge;10 stocks same day) excluded —<br>
    portfolio construction events,<br>
    not informed picks.
    </span>
    </div>
    """, unsafe_allow_html=True)

    if not st.session_state.selected_pol_id:
        st.markdown('<hr class="section-divider">', unsafe_allow_html=True)
        st.markdown('<div class="filter-label">VIEW PROFILE</div>', unsafe_allow_html=True)
        sidebar_names = ["— select politician —"] + df_all.sort_values("score", ascending=False)["name"].tolist()
        sidebar_sel   = st.selectbox("", options=sidebar_names, label_visibility="collapsed",
                                     key="sidebar_pol_select")
        if sidebar_sel != "— select politician —":
            row = df_all[df_all["name"] == sidebar_sel].iloc[0]
            st.session_state.selected_pol_id   = row["politician_id"]
            st.session_state.selected_pol_name = sidebar_sel
            st.rerun()

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

    wins   = len(results[results["outcome"] == "WIN"])   if len(results) else 0
    losses = len(results[results["outcome"] == "LOSS"])  if len(results) else 0
    opens  = len(results[results["outcome"] == "open"])  if len(results) else 0
    total  = len(results)
    decided = wins + losses
    win_rate = wins / decided * 100 if decided > 0 else 0

    winning_dd = results[
        results["dd"].notna() &
        (results["dd"] > -10) &
        (results["dd"] < 0)
    ]["dd"] if len(results) else pd.Series()
    avg_dd = winning_dd.mean() if len(winning_dd) else None

    large = results[results["size"] >= 50000] if len(results) else pd.DataFrame()
    large_wins = large[large["outcome"] == "WIN"] if len(large) else pd.DataFrame()
    large_wr = len(large_wins) / len(large) * 100 if len(large) else None

    avg_exit = results[results["exit_day"].notna()]["exit_day"].mean() if len(results) else None

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
    if len(results) > 0:
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
    if len(results) > 0:
        st.markdown('<div class="filter-label">TOP TICKERS</div>', unsafe_allow_html=True)

        ticker_stats = results.groupby("ticker").apply(lambda g: pd.Series({
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
            outcome   = t["outcome"]
            oc_color  = "#4caf50" if outcome == "WIN" else "#ff5252" if outcome == "LOSS" else "#888"
            dd_str    = f"{t['dd']:.1f}%" if pd.notna(t["dd"]) else "—"
            size_str  = f"${t['size']:,.0f}" if pd.notna(t["size"]) else "—"
            exit_str  = t["exit_date"] or "open"
            comm_rel  = t.get("committee_relevance")
            if comm_rel:
                # Show first committee name only (can be "Finance | Armed Services")
                first_comm = comm_rel.split(" | ")[0]
                comm_cell = (f'<span style="background:#0f1a0f;color:#4caf50;'
                             f'border:1px solid #1a3a1a;font-size:9px;padding:2px 6px;'
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
            trade_rows += f"""<tr style="border-bottom:1px solid #141414">
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
    st.markdown('<div class="dash-subtitle">10% Stop / 10% Target · Disclosure Date Entry · '
                'Compliant Trades Only</div>', unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)

    s = load_summary_stats()

    # Apply filters + combo simulation first so stats panel reflects them
    df = df_all.copy()
    if party_filter   != "All": df = df[df["party"]   == party_filter]
    if chamber_filter != "All": df = df[df["chamber"] == chamber_filter]
    if state_filter   != "All": df = df[df["state"]   == state_filter]
    if min_trades > 1:          df = df[df["total_trades"] >= min_trades]
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

        rows_html += f"""<tr style="border-bottom:1px solid #141414"
                             data-polid="{pol_id}" data-polname="{name}"
                             class="pol-row">
            <td style="padding:7px 10px;color:#444;font-size:11px;
                       font-family:IBM Plex Mono,monospace;width:40px">{rank}</td>
            <td style="padding:7px 10px;width:220px">
                <span style="color:#f5a623;font-weight:500;cursor:pointer;
                             font-family:IBM Plex Mono,monospace;font-size:12px"
                      class="pol-name-link">{name}</span>{conf_badge}
            </td>
            <td style="padding:7px 10px;color:{pty_color};font-family:IBM Plex Mono,monospace;
                       font-size:12px;width:40px">{pty_short}</td>
            <td style="padding:7px 10px;color:#888;font-family:IBM Plex Mono,monospace;
                       font-size:12px;width:70px">{chamber}</td>
            <td style="padding:7px 10px;color:#888;font-family:IBM Plex Mono,monospace;
                       font-size:12px;width:50px">{state}</td>
            <td style="padding:7px 10px;width:120px">
                <span style="color:{bar_col};font-weight:600;font-size:13px;
                             font-family:IBM Plex Mono,monospace">{bar_val}</span>
                <div style="background:#1a1a1a;height:3px;width:100%;
                            margin-top:3px;border-radius:1px">
                    <div style="background:{bar_col};height:3px;
                                width:{bar_w}%;border-radius:1px"></div>
                </div>
            </td>
            <td style="padding:7px 10px;color:{wr_color};font-family:IBM Plex Mono,monospace;
                       font-size:12px;width:70px">{win_rate:.1f}%</td>
            <td style="padding:7px 10px;color:#ff9800;font-family:IBM Plex Mono,monospace;
                       font-size:12px;width:70px">{dd_str}</td>
            <td style="padding:7px 10px;color:#888;font-family:IBM Plex Mono,monospace;
                       font-size:12px;width:80px">{large_str}</td>
            <td style="padding:7px 10px;color:#888;font-family:IBM Plex Mono,monospace;
                       font-size:12px;width:60px">{trades}</td>
            <td style="padding:7px 10px;color:{late_color};font-family:IBM Plex Mono,monospace;
                       font-size:12px;width:70px">{late}</td>
            <td style="padding:7px 10px;color:#888;font-family:IBM Plex Mono,monospace;
                       font-size:12px;width:60px">{lag_str}</td>
            <td style="padding:7px 10px;color:{comm_color};font-family:IBM Plex Mono,monospace;
                       font-size:12px;width:80px">{comm_str}</td>
            <td style="padding:7px 10px;width:110px">
                <span style="color:{etf_sc_color};font-weight:600;font-size:13px;
                             font-family:IBM Plex Mono,monospace">{etf_str}</span>
                <span style="color:#555;font-size:10px;font-family:IBM Plex Mono,monospace;
                             margin-left:4px">{etf_wr_str} {etf_n_str}</span>
            </td>
        </tr>"""

    st.html(f"""
    <div style="overflow-x:auto;border:1px solid #1a1a1a;border-radius:2px">
    <table style="width:100%;border-collapse:collapse;font-family:'IBM Plex Mono',monospace;
                  font-size:12px;background:#0a0a0a">
        <thead>
            <tr style="border-bottom:2px solid #f5a623">
                <th style="padding:8px 10px;color:#f5a623;font-size:10px;letter-spacing:2px;
                           text-align:left;background:#0f0f0f">#</th>
                <th style="padding:8px 10px;color:#f5a623;font-size:10px;letter-spacing:2px;
                           text-align:left;background:#0f0f0f">POLITICIAN</th>
                <th style="padding:8px 10px;color:#f5a623;font-size:10px;letter-spacing:2px;
                           text-align:left;background:#0f0f0f">PTY</th>
                <th style="padding:8px 10px;color:#f5a623;font-size:10px;letter-spacing:2px;
                           text-align:left;background:#0f0f0f">CHAMBER</th>
                <th style="padding:8px 10px;color:#f5a623;font-size:10px;letter-spacing:2px;
                           text-align:left;background:#0f0f0f">ST</th>
                <th style="padding:8px 10px;color:#f5a623;font-size:10px;letter-spacing:2px;
                           text-align:left;background:#0f0f0f">{"WIN%" if using_combo else "SCORE"}</th>
                <th style="padding:8px 10px;color:#f5a623;font-size:10px;letter-spacing:2px;
                           text-align:left;background:#0f0f0f">WIN%</th>
                <th style="padding:8px 10px;color:#f5a623;font-size:10px;letter-spacing:2px;
                           text-align:left;background:#0f0f0f">AVG DD</th>
                <th style="padding:8px 10px;color:#f5a623;font-size:10px;letter-spacing:2px;
                           text-align:left;background:#0f0f0f">LG WIN%</th>
                <th style="padding:8px 10px;color:#f5a623;font-size:10px;letter-spacing:2px;
                           text-align:left;background:#0f0f0f">COMP BUYS</th>
                <th style="padding:8px 10px;color:#f5a623;font-size:10px;letter-spacing:2px;
                           text-align:left;background:#0f0f0f">LATE FILES</th>
                <th style="padding:8px 10px;color:#f5a623;font-size:10px;letter-spacing:2px;
                           text-align:left;background:#0f0f0f">AVG LAG</th>
                <th style="padding:8px 10px;color:#f5a623;font-size:10px;letter-spacing:2px;
                           text-align:left;background:#0f0f0f">COMM ALIGN%</th>
                <th style="padding:8px 10px;color:#4a9eff;font-size:10px;letter-spacing:2px;
                           text-align:left;background:#0f0f0f">ETF SCORE</th>
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