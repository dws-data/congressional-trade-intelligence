# dashboard/pages/feed.py
# Congressional Trade Tracker — Trade Feed
# Run with: streamlit run dashboard/app.py

import streamlit as st
import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime, date, timedelta

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).parent.parent.parent / "data" / "trades.db"

st.set_page_config(
    page_title="Trade Feed — Congressional Trade Tracker",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────
# STYLING
# ─────────────────────────────────────────────────────────────────

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');

html, body, [class*="css"] {
    font-family: 'IBM Plex Sans', sans-serif;
    background-color: #0a0a0a;
    color: #e0e0e0;
}
.stApp { background-color: #0a0a0a; }

section[data-testid="stSidebar"] {
    background-color: #0f0f0f;
    border-right: 1px solid #1a1a1a;
}
section[data-testid="stSidebar"] * { color: #e0e0e0 !important; }

.dash-header {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    color: #f5a623;
    letter-spacing: 3px;
    text-transform: uppercase;
    border-bottom: 1px solid #1e1e1e;
    padding-bottom: 8px;
    margin-bottom: 4px;
}
.dash-title {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 28px;
    font-weight: 600;
    color: #ffffff;
    letter-spacing: -0.5px;
    margin: 0; padding: 0;
}
.dash-subtitle {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    color: #555;
    letter-spacing: 2px;
    text-transform: uppercase;
    margin-top: 4px;
}
.metric-card {
    background: #0f0f0f;
    border: 1px solid #1e1e1e;
    border-top: 2px solid #f5a623;
    padding: 16px 20px;
    border-radius: 2px;
}
.metric-label {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    color: #555;
    letter-spacing: 2px;
    text-transform: uppercase;
    margin-bottom: 6px;
}
.metric-value {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 24px;
    font-weight: 600;
    color: #f5a623;
}
.metric-sub {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    color: #444;
    margin-top: 4px;
}
.filter-label {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    color: #f5a623;
    letter-spacing: 2px;
    text-transform: uppercase;
    margin-bottom: 8px;
}
.section-divider {
    border: none;
    border-top: 1px solid #1a1a1a;
    margin: 20px 0;
}
.stSelectbox label, .stSlider label, .stRadio label,
.stCheckbox label, .stMultiSelect label {
    font-family: 'IBM Plex Mono', monospace !important;
    font-size: 10px !important;
    color: #555 !important;
    letter-spacing: 2px !important;
    text-transform: uppercase !important;
}
div[data-baseweb="select"] { background-color: #0f0f0f !important; border-color: #222 !important; }
div[data-baseweb="select"] * {
    background-color: #0f0f0f !important;
    color: #e0e0e0 !important;
    font-family: 'IBM Plex Mono', monospace !important;
    font-size: 12px !important;
}
.stRadio > div { flex-direction: row; gap: 16px; }
hr { border-color: #1a1a1a; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_feed(days_back=7):
    conn   = sqlite3.connect(DB_PATH)
    cutoff = (date.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    df = pd.read_sql_query(f"""
        SELECT
            t.disclosure_date,
            t.filing_lag_days,
            t.ticker,
            t.company,
            t.size_midpoint,
            t.price_at_disclosure_date  AS entry_price,
            t.return_disc_30d           AS move_30d,
            t.filing_violation,
            p.name                      AS politician,
            p.party,
            p.chamber,
            p.state,
            p.score
        FROM trades t
        JOIN politicians p ON t.politician_id = p.politician_id
        WHERE t.disclosure_date >= '{cutoff}'
        AND t.disclosure_date != ''
        AND t.transaction_type = 'buy'
        ORDER BY t.disclosure_date DESC, p.score DESC
    """, conn)
    conn.close()
    return df


@st.cache_data(ttl=300)
def load_feed_stats(days_back=7):
    conn   = sqlite3.connect(DB_PATH)
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

    cursor.execute("SELECT MAX(disclosure_date) FROM trades WHERE disclosure_date != ''")
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

# ─────────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown('<div class="dash-header">FILTERS</div>', unsafe_allow_html=True)
    st.markdown("")

    days_options = {"7 days": 7, "14 days": 14, "30 days": 30, "All time": 3650}
    days_label   = st.selectbox("DISCLOSURE WINDOW", options=list(days_options.keys()), index=0)
    days_back    = days_options[days_label]

    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)

    party_filter   = st.radio("PARTY",   options=["All", "Republican", "Democrat"], index=0, horizontal=True)
    chamber_filter = st.radio("CHAMBER", options=["All", "House", "Senate"],        index=0, horizontal=True)

    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)

    min_score      = st.slider("MINIMUM POLITICIAN SCORE", min_value=0, max_value=100, value=0, step=5)
    compliant_only = st.checkbox("COMPLIANT FILINGS ONLY (<=45 DAYS)", value=False)

    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)

    size_options = {"Any size": 0, "$15K+": 15_000, "$50K+": 50_000, "$100K+": 100_000, "$250K+": 250_000}
    size_label   = st.selectbox("MINIMUM TRADE SIZE", options=list(size_options.keys()), index=0)
    min_size     = size_options[size_label]

# ─────────────────────────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────────────────────────

st.markdown('<div class="dash-header">CONGRESSIONAL TRADE INTELLIGENCE</div>', unsafe_allow_html=True)
st.markdown('<div class="dash-title">TRADE FEED</div>', unsafe_allow_html=True)
st.markdown('<div class="dash-subtitle">Recent Buy Disclosures · Newest First · Buy Trades Only</div>', unsafe_allow_html=True)
st.markdown("<br>", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────
# SUMMARY METRICS
# ─────────────────────────────────────────────────────────────────

total_trades, unique_pols, latest_disc, high_score_trades = load_feed_stats(days_back)
col1, col2, col3, col4 = st.columns(4)

with col1:
    st.markdown(f"""<div class="metric-card">
        <div class="metric-label">Buy Trades</div>
        <div class="metric-value">{total_trades:,}</div>
        <div class="metric-sub">last {days_label}</div>
    </div>""", unsafe_allow_html=True)
with col2:
    st.markdown(f"""<div class="metric-card">
        <div class="metric-label">Politicians Filing</div>
        <div class="metric-value">{unique_pols}</div>
        <div class="metric-sub">last {days_label}</div>
    </div>""", unsafe_allow_html=True)
with col3:
    st.markdown(f"""<div class="metric-card">
        <div class="metric-label">High Score Trades</div>
        <div class="metric-value">{high_score_trades}</div>
        <div class="metric-sub">from politicians scored 70+</div>
    </div>""", unsafe_allow_html=True)
with col4:
    st.markdown(f"""<div class="metric-card">
        <div class="metric-label">Latest Disclosure</div>
        <div class="metric-value" style="font-size:16px; padding-top:4px">{latest_disc or 'N/A'}</div>
        <div class="metric-sub">most recent filing</div>
    </div>""", unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────
# APPLY FILTERS
# ─────────────────────────────────────────────────────────────────

df = load_feed(days_back)

if party_filter != "All":
    df = df[df["party"] == party_filter]
if chamber_filter != "All":
    df = df[df["chamber"] == chamber_filter]
if min_score > 0:
    df = df[df["score"].notna() & (df["score"] >= min_score)]
if compliant_only:
    df = df[df["filing_violation"] == "compliant"]
if min_size > 0:
    df = df[df["size_midpoint"].notna() & (df["size_midpoint"] >= min_size)]

# ─────────────────────────────────────────────────────────────────
# BUILD DISPLAY DATAFRAME
# ─────────────────────────────────────────────────────────────────

st.markdown(f'<div class="filter-label">SHOWING {len(df)} TRADES</div>', unsafe_allow_html=True)
st.markdown("<br>", unsafe_allow_html=True)

if df.empty:
    st.warning("No trades match the current filters.")
else:
    today = date.today()

    def days_ago_str(d):
        try:
            n = (today - datetime.strptime(d, "%Y-%m-%d").date()).days
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

    def fmt_price(v):
        return "—" if pd.isna(v) else f"${v:.2f}"

    def fmt_move(v):
        if pd.isna(v): return "—"
        return f"+{v:.1f}%" if v > 0 else f"{v:.1f}%"

    def fmt_lag(v):
        return "—" if pd.isna(v) else f"{int(v)}d"

    def fmt_party(v):
        if v == "Republican": return "R"
        if v == "Democrat":   return "D"
        return v or "?"

    display_df = pd.DataFrame({
        "Disclosed":   df["disclosure_date"].apply(days_ago_str),
        "Politician":  df["politician"].fillna("—").str[:22],
        "Score":       df["score"],
        "Pty":         df["party"].apply(fmt_party),
        "Chamber":     df["chamber"].fillna("—"),
        "Company":     df["company"].fillna("—").str[:25] + " (" + df["ticker"].fillna("—") + ")",
        "Size":        df["size_midpoint"].apply(fmt_size),
        "Entry":       df["entry_price"].apply(fmt_price),
        "Filed After": df["filing_lag_days"].apply(fmt_lag),
    })

    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        height=min(50 + len(display_df) * 35, 900),
        column_config={
            "Disclosed":   st.column_config.TextColumn("Disclosed",  width="small"),
            "Politician":  st.column_config.TextColumn("Politician", width="medium"),
            "Score":       st.column_config.ProgressColumn(
                               "Score", min_value=0, max_value=100, format="%.0f", width="small"
                           ),
            "Pty":         st.column_config.TextColumn("Pty",        width="small"),
            "Chamber":     st.column_config.TextColumn("Chamber",    width="small"),
            "Company":     st.column_config.TextColumn("Company",    width="medium"),
            "Size":        st.column_config.TextColumn("Size",       width="small"),
            "Entry":       st.column_config.TextColumn("Entry",      width="small"),
            "Filed After": st.column_config.TextColumn("Filed After",width="small"),
        }
    )
# ─────────────────────────────────────────────────────────────────
# FOOTER
# ─────────────────────────────────────────────────────────────────

st.markdown("<br><br>", unsafe_allow_html=True)
st.markdown("""
<div style="font-family: IBM Plex Mono; font-size: 10px; color: #2a2a2a;
            border-top: 1px solid #141414; padding-top: 16px; letter-spacing: 1px;">
CONGRESSIONAL TRADE TRACKER · DATA SOURCE: CAPITOL TRADES · BUY TRADES ONLY · NOT FINANCIAL ADVICE
</div>
""", unsafe_allow_html=True)