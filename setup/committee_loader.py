# pipeline/committee_loader.py
# Parses committee YAML files and loads into SQLite
# Also flags trades where politician was on a relevant committee
#
# Files needed in data/committees/:
#   committees-current.yaml
#   committees-historical.yaml
#   committee-membership-current.yaml
#
# Creates two DB tables:
#   committees            — committee details + sector mapping
#   committee_memberships — politician <> committee assignments
#
# Then updates trades.committee_relevance for flagged trades
#
# KEY DESIGN DECISIONS:
#   - Subcommittee codes (thomas_id ending in digits e.g. SSJU01) are filtered out
#   - Committee relevance flagging uses ONLY 2019-present sources (clerk + current YAML)
#   - MIT historical data (pre-2019) is loaded but NOT used for trade flagging
#   - This avoids QA-confirmed noise in historical committee name/membership data

import sqlite3
import yaml
import re
from pathlib import Path
from datetime import date
from collections import defaultdict

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────

DB_PATH    = Path(__file__).parent.parent / "data" / "trades.db"
DATA_DIR   = Path(__file__).parent.parent / "data" / "committees"

COMMITTEES_CURRENT    = DATA_DIR / "committees-current.yaml"
COMMITTEES_HISTORICAL = DATA_DIR / "committees-historical.yaml"
MEMBERSHIP_CURRENT    = DATA_DIR / "committee-membership-current.yaml"

# Sources considered reliable for trade flagging (2019-present only)
TRUSTED_SOURCES = {
    "clerk_house_116",
    "clerk_house_117",
    "clerk_house_118",
    "clerk_house_119",
    "current",
}

# ─────────────────────────────────────────────────────────────────
# SUBCOMMITTEE FILTER
# thomas_ids ending in digits are subcommittees — skip them
# e.g. SSJU01, HSSM22, HSED13 are subcommittees
# e.g. SSJU, HSSM, HSED are full committees
# ─────────────────────────────────────────────────────────────────

def is_subcommittee(thomas_id):
    if not thomas_id:
        return True
    return bool(re.search(r'\d', thomas_id))

# ─────────────────────────────────────────────────────────────────
# SECTOR MAPPING
# ─────────────────────────────────────────────────────────────────

from pipeline.committee_config import (
    COMMITTEE_SUBSECTOR_MAP, COMMITTEE_NAMES, get_subsector,
)

# ─────────────────────────────────────────────────────────────────
# DB SETUP
# ─────────────────────────────────────────────────────────────────

def create_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS committees (
            thomas_id    TEXT PRIMARY KEY,
            name         TEXT,
            chamber      TEXT,
            jurisdiction TEXT,
            sectors      TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS committee_memberships (
            bioguide    TEXT,
            thomas_id   TEXT,
            party       TEXT,
            rank        INTEGER,
            title       TEXT,
            start_date  TEXT,
            end_date    TEXT,
            source      TEXT,
            confidence  TEXT,
            PRIMARY KEY (bioguide, thomas_id, start_date)
        )
    """)

def ensure_columns(cursor):
    trade_cols = [c[1] for c in cursor.execute("PRAGMA table_info(trades)").fetchall()]
    if "committee_relevance" not in trade_cols:
        cursor.execute("ALTER TABLE trades ADD COLUMN committee_relevance TEXT")
        print("  Added committee_relevance column to trades")

    mem_cols = [c[1] for c in cursor.execute("PRAGMA table_info(committee_memberships)").fetchall()]
    if "confidence" not in mem_cols:
        cursor.execute("ALTER TABLE committee_memberships ADD COLUMN confidence TEXT")
        print("  Added confidence column to committee_memberships")

    # Set confidence on all existing records
    cursor.execute("""
        UPDATE committee_memberships
        SET confidence = CASE
            WHEN source IN ('clerk_house_116','clerk_house_117',
                            'clerk_house_118','clerk_house_119','current')
            THEN 'high'
            ELSE 'low'
        END
        WHERE confidence IS NULL
    """)

# ─────────────────────────────────────────────────────────────────
# PARSE COMMITTEES
# ─────────────────────────────────────────────────────────────────

def parse_committees(cursor):
    inserted = 0
    for filepath in [COMMITTEES_CURRENT, COMMITTEES_HISTORICAL]:
        if not filepath.exists():
            print(f"  WARNING: {filepath.name} not found — skipping")
            continue
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not data:
            continue
        for committee in data:
            thomas_id    = committee.get("thomas_id", "")
            name         = committee.get("name", "")
            chamber      = committee.get("type", "")
            jurisdiction = committee.get("jurisdiction", "")

            if not thomas_id or is_subcommittee(thomas_id):
                continue

            sectors_str = ""

            cursor.execute("""
                INSERT OR REPLACE INTO committees
                    (thomas_id, name, chamber, jurisdiction, sectors)
                VALUES (?, ?, ?, ?, ?)
            """, (thomas_id, name, chamber, jurisdiction, sectors_str))
            inserted += 1

    print(f"  Committees loaded: {inserted:,}")

# ─────────────────────────────────────────────────────────────────
# PARSE CURRENT MEMBERSHIP
# ─────────────────────────────────────────────────────────────────

def parse_current_membership(cursor):
    if not MEMBERSHIP_CURRENT.exists():
        print(f"  WARNING: {MEMBERSHIP_CURRENT.name} not found — skipping")
        return 0

    with open(MEMBERSHIP_CURRENT, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data:
        return 0

    inserted   = 0
    skipped_sc = 0
    start_date = "2025-01-03"

    for thomas_id, members in data.items():
        if is_subcommittee(thomas_id):
            skipped_sc += 1
            continue
        if not members:
            continue
        for member in members:
            bioguide = member.get("bioguide", "")
            if not bioguide:
                continue
            cursor.execute("""
                INSERT OR IGNORE INTO committee_memberships
                    (bioguide, thomas_id, party, rank, title,
                     start_date, end_date, source, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                bioguide,
                thomas_id,
                member.get("party", ""),
                member.get("rank"),
                member.get("title", ""),
                start_date,
                None,
                "current",
                "high",
            ))
            inserted += 1

    print(f"  Current memberships loaded: {inserted:,}  "
          f"(skipped {skipped_sc} subcommittee codes)")
    return inserted

# ─────────────────────────────────────────────────────────────────
# FLAG COMMITTEE RELEVANCE
# Only uses high-confidence (2019-present) sources
# ─────────────────────────────────────────────────────────────────

def flag_committee_relevance(conn, cursor):
    print("\n  Flagging committee relevance with sub-sector taxonomy...")
    print("  (Using high-confidence sources only: clerk 116-119 + current YAML)")

    # Clear existing flags
    cursor.execute("UPDATE trades SET committee_relevance = NULL")
    conn.commit()

    # Load buy trades with industry data (ticker needed for manual overrides)
    cursor.execute("""
        SELECT t.trade_id, t.politician_id, t.disclosure_date, t.ticker, t.industry
        FROM trades t
        WHERE t.transaction_type = 'buy'
        AND t.ticker != ''
        AND t.disclosure_date != ''
        AND t.disclosure_date IS NOT NULL
        AND t.industry IS NOT NULL
    """)
    trades = cursor.fetchall()
    print(f"  Trades with industry data: {len(trades):,}")

    # Load ONLY high-confidence memberships
    cursor.execute("""
        SELECT bioguide, thomas_id, start_date, end_date
        FROM committee_memberships
        WHERE confidence = 'high'
    """)
    memberships = cursor.fetchall()

    # Filter out any subcommittee codes that slipped through
    memberships = [(b, t, s, e) for b, t, s, e in memberships
                   if not is_subcommittee(t)]
    print(f"  High-confidence full-committee memberships: {len(memberships):,}")

    pol_memberships = defaultdict(list)
    for bioguide, thomas_id, start_date, end_date in memberships:
        pol_memberships[bioguide].append((thomas_id, start_date, end_date))

    flagged   = 0
    processed = 0

    for trade_id, politician_id, disc_date, ticker, industry in trades:
        subsector = get_subsector(ticker, industry)
        if not subsector:
            processed += 1
            continue

        memberships_for_pol = pol_memberships.get(politician_id, [])
        if not memberships_for_pol:
            processed += 1
            continue

        relevant = []
        for thomas_id, start_date, end_date in memberships_for_pol:
            if start_date and disc_date < start_date:
                continue
            if end_date and disc_date > end_date:
                continue
            if subsector in COMMITTEE_SUBSECTOR_MAP.get(thomas_id, []):
                name = COMMITTEE_NAMES.get(thomas_id, thomas_id)
                relevant.append(name)

        if relevant:
            flagged += 1
            unique = list(dict.fromkeys(relevant))
            cursor.execute(
                "UPDATE trades SET committee_relevance = ? WHERE trade_id = ?",
                (" | ".join(unique), trade_id)
            )

        processed += 1
        if processed % 100 == 0:
            conn.commit()
        if processed % 5000 == 0:
            print(f"    Processed {processed:,} / {len(trades):,}  "
                  f"Flagged: {flagged:,}")

    conn.commit()
    pct = flagged / len(trades) * 100 if trades else 0
    print(f"  Flagged {flagged:,} trades ({pct:.1f}%)")

    # Sample
    cursor.execute("""
        SELECT p.name, t.ticker, t.sector, t.committee_relevance, t.disclosure_date
        FROM trades t
        JOIN politicians p ON t.politician_id = p.politician_id
        WHERE t.committee_relevance IS NOT NULL
        AND t.transaction_type = 'buy'
        ORDER BY t.disclosure_date DESC
        LIMIT 10
    """)
    print("\n  Sample flagged trades:")
    print(f"  {'Politician':<28} {'Ticker':<8} {'Sector':<25} "
          f"{'Committee':<35} {'Date'}")
    print(f"  {'-'*108}")
    for row in cursor.fetchall():
        print(f"  {str(row[0]):<28} {str(row[1]):<8} {str(row[2]):<25} "
              f"{str(row[3]):<35} {row[4]}")

# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

def run_committee_loader():
    print("=" * 60)
    print("  Committee Loader")
    print("  v2 — subcommittee-filtered, 2019+ flagging only")
    print("=" * 60)

    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print("\n  Setting up tables...")
    create_tables(cursor)
    ensure_columns(cursor)
    conn.commit()

    print("\n  Loading committee details...")
    parse_committees(cursor)
    conn.commit()

    print("\n  Loading current memberships...")
    parse_current_membership(cursor)
    conn.commit()

    flag_committee_relevance(conn, cursor)

    # Summary
    cursor.execute("SELECT COUNT(*) FROM committees")
    n_committees = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM committee_memberships")
    n_total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM committee_memberships WHERE confidence = 'high'")
    n_high = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM committee_memberships WHERE confidence = 'low'")
    n_low = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM trades WHERE committee_relevance IS NOT NULL")
    n_flagged = cursor.fetchone()[0]

    cursor.execute("""
        SELECT committee_relevance, COUNT(*) as cnt
        FROM trades WHERE committee_relevance IS NOT NULL
        GROUP BY committee_relevance
        ORDER BY cnt DESC LIMIT 10
    """)
    top_flags = cursor.fetchall()

    conn.close()

    print("\n" + "=" * 60)
    print(f"  Complete!")
    print(f"  Committees in DB:            {n_committees:,}")
    print(f"  Total memberships:           {n_total:,}")
    print(f"    High confidence (2019+):   {n_high:,}")
    print(f"    Low confidence (pre-2019): {n_low:,}")
    print(f"  Trades flagged:              {n_flagged:,}")
    print(f"\n  Top committee flags:")
    for flag, cnt in top_flags:
        print(f"    {cnt:>5}  {flag}")
    print("=" * 60)


if __name__ == "__main__":
    run_committee_loader()