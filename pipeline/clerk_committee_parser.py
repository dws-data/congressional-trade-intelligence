# pipeline/clerk_committee_parser.py
# Parses Clerk of the House MemberData XML files for 116th-119th Congress
# Extracts committee assignments with bioguide IDs
#
# Files needed in data/committees/:
#   MemberData_116.xml  (Dec 2020 snapshot)
#   MemberData_117.xml  (Dec 2022 snapshot)
#   MemberData_118.xml  (Dec 2024 snapshot)
#   MemberData_119.xml  (current)
#
# Loads into committee_memberships table

import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────

DB_PATH  = Path(__file__).parent.parent / "data" / "trades.db"
DATA_DIR = Path(__file__).parent.parent / "data" / "committees"

CONGRESS_FILES = {
    116: (DATA_DIR / "MemberData_116.xml", "2019-01-03", "2021-01-03"),
    117: (DATA_DIR / "MemberData_117.xml", "2021-01-03", "2023-01-03"),
    118: (DATA_DIR / "MemberData_118.xml", "2023-01-03", "2025-01-03"),
    119: (DATA_DIR / "MemberData_119.xml", "2025-01-03", None),
}

# Clerk comcode (e.g. "AG00") -> thomas_id (e.g. "HSAG")
# Clerk uses 2-letter prefix + "00" for full committees
# House committees start with HS, Senate with SS
# The Clerk file only covers House members/committees

CLERK_TO_THOMAS = {
    # House committees
    "AG00": "HSAG",   # Agriculture
    "AP00": "HSAP",   # Appropriations
    "AS00": "HSAS",   # Armed Services
    "BA00": "HSBA",   # Financial Services
    "BO00": "HSBU",   # Budget  (Budget = BO in Clerk)
    "BU00": "HSBU",   # Budget
    "ED00": "HSED",   # Education and the Workforce
    "EL00": "HSED",   # Education and Labor (alt name)
    "FA00": "HSFA",   # Foreign Affairs
    "GO00": "HSGO",   # Oversight and Government Reform
    "HA00": "HSHA",   # House Administration
    "HM00": "HSHM",   # Homeland Security
    "IF00": "HSIF",   # Energy and Commerce
    "II00": "HSII",   # Natural Resources
    "JU00": "HSJU",   # Judiciary
    "PW00": "HSPW",   # Transportation and Infrastructure
    "RU00": "HSRU",   # Rules
    "SC00": "HSSY",   # Science, Space, and Technology (SC in Clerk)
    "SY00": "HSSY",   # Science alt
    "SM00": "HSSM",   # Small Business
    "SO00": "HSSO",   # Standards of Official Conduct / Ethics
    "VR00": "HSVR",   # Veterans Affairs
    "WM00": "HSWM",   # Ways and Means
    "LG00": "HLIG",   # Intelligence (Permanent Select)
    "IG00": "HLIG",   # Intelligence alt
    "PI00": "HLIG",   # Intelligence alt
    "SL00": "HLIG",   # Intelligence alt
}

# ─────────────────────────────────────────────────────────────────
# PARSE ONE XML FILE
# ─────────────────────────────────────────────────────────────────

def parse_memberdata_xml(filepath, congress, start_date, end_date):
    records = []
    skipped_no_thomas = 0

    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
    except ET.ParseError as e:
        print(f"  ERROR parsing {filepath.name}: {e}")
        return records

    members = root.findall(".//member")

    for member in members:
        info     = member.find("member-info")
        if info is None:
            continue

        bioguide = info.findtext("bioguideID", "").strip()
        party_code = info.findtext("party", "").strip()

        if not bioguide:
            continue

        party = "Republican" if party_code == "R" else \
                "Democrat"   if party_code == "D" else ""

        # Get committee assignments
        assignments = member.find("committee-assignments")
        if assignments is None:
            continue

        for comm in assignments.findall("committee"):
            comcode   = comm.get("comcode", "")
            rank      = comm.get("rank")

            thomas_id = CLERK_TO_THOMAS.get(comcode)
            if not thomas_id:
                skipped_no_thomas += 1
                continue

            records.append({
                "bioguide":   bioguide,
                "thomas_id":  thomas_id,
                "party":      party,
                "rank":       int(rank) if rank and rank.isdigit() else None,
                "title":      "",
                "start_date": start_date,
                "end_date":   end_date,
                "source":     f"clerk_house_{congress}",
            })

    print(f"  Congress {congress}: {len(records):,} assignments | "
          f"{skipped_no_thomas} comcodes not mapped | "
          f"{len(members)} members parsed")
    return records

# ─────────────────────────────────────────────────────────────────
# LOAD INTO DB
# ─────────────────────────────────────────────────────────────────

def load_into_db(records):
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

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
            PRIMARY KEY (bioguide, thomas_id, start_date)
        )
    """)

    inserted = 0
    for r in records:
        cursor.execute("""
            INSERT OR REPLACE INTO committee_memberships
                (bioguide, thomas_id, party, rank, title,
                 start_date, end_date, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            r["bioguide"], r["thomas_id"], r["party"],
            r["rank"],     r["title"],     r["start_date"],
            r["end_date"], r["source"],
        ))
        inserted += 1

    conn.commit()

    cursor.execute("""
        SELECT source, COUNT(*) FROM committee_memberships
        WHERE source LIKE 'clerk_%'
        GROUP BY source ORDER BY source
    """)
    by_source = cursor.fetchall()

    cursor.execute("SELECT COUNT(*) FROM committee_memberships")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(DISTINCT bioguide) FROM committee_memberships")
    unique_pols = cursor.fetchone()[0]

    conn.close()
    return inserted, total, unique_pols, by_source

# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

def run_clerk_parser():
    print("=" * 60)
    print("  Clerk House XML Parser")
    print("  Coverage: 116th-119th Congress (2019-present)")
    print("=" * 60)

    all_records = []

    for congress, (filepath, start_date, end_date) in CONGRESS_FILES.items():
        if not filepath.exists():
            print(f"  WARNING: {filepath.name} not found — skipping")
            continue
        print(f"\n  Parsing {filepath.name}...")
        records = parse_memberdata_xml(filepath, congress, start_date, end_date)
        all_records.extend(records)

    print(f"\n  Total records to load: {len(all_records):,}")
    print("  Loading into database...")

    inserted, total, unique_pols, by_source = load_into_db(all_records)

    print("\n" + "=" * 60)
    print(f"  Complete!")
    print(f"  Records inserted:    {inserted:,}")
    print(f"  Total memberships:   {total:,}")
    print(f"  Unique politicians:  {unique_pols:,}")
    print(f"\n  Clerk records by congress:")
    for source, count in by_source:
        print(f"    {source}: {count:,}")
    print("=" * 60)


if __name__ == "__main__":
    run_clerk_parser()