# pipeline/mit_committee_parser.py
# Parses MIT Stewart-Woon committee assignment data (103rd-115th Congress, 1993-2017)
# Requires:
#   data/committees/house_assignments_103-115-3.xls
#   data/committees/senate_assignments_103-115-3.xls
#   data/committees/legislators-historical.yaml
#
# Loads into committee_memberships table with exact date ranges
# Uses ICPSR -> bioguide crosswalk via legislators-historical.yaml

import sqlite3
import yaml
import pandas as pd
from pathlib import Path
from datetime import datetime

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────

DB_PATH        = Path(__file__).parent.parent / "data" / "trades.db"
DATA_DIR       = Path(__file__).parent.parent / "data" / "committees"
HOUSE_FILE     = DATA_DIR / "house_assignments_103-115-3.xls"
SENATE_FILE    = DATA_DIR / "senate_assignments_103-115-3.xls"
HIST_LEG_FILE  = DATA_DIR / "legislators-historical.yaml"

# ─────────────────────────────────────────────────────────────────
# COMMITTEE CODE -> THOMAS_ID MAPPING
# MIT numeric codes -> thomas_id used in our committees table
# ─────────────────────────────────────────────────────────────────

HOUSE_CODE_MAP = {
    102:  "HSAG",   # Agriculture
    104:  "HSAP",   # Appropriations
    106:  "HSAS",   # Armed Services / National Security
    113:  "HSBA",   # Banking / Financial Services
    115:  "HSBU",   # Budget
    124:  "HSED",   # Education and the Workforce / Labor
    128:  "HSIF",   # Energy and Commerce
    134:  "HSFA",   # Foreign Affairs / International Relations
    138:  "HSGO",   # Oversight and Government Reform
    142:  "HSHA",   # House Administration
    156:  "HSJU",   # Judiciary
    164:  "HSII",   # Natural Resources / Resources
    173:  "HSPW",   # Transportation and Infrastructure
    176:  "HSRU",   # Rules
    182:  "HSSY",   # Science, Space, and Technology
    184:  "HSSM",   # Small Business
    192:  "HSVR",   # Veterans Affairs
    196:  "HSWM",   # Ways and Means
    242:  "HLIG",   # Intelligence (Select)
    251:  "HSHM",   # Homeland Security
    500:  "JSLB",   # Library (Joint)
    503:  "JSTX",   # Taxation (Joint)
    507:  "JSEC",   # Economic (Joint)
}

SENATE_CODE_MAP = {
    305:  "SSAF",   # Agriculture, Nutrition, and Forestry
    306:  "SSAP",   # Appropriations
    308:  "SSAS",   # Armed Services
    314:  "SSBK",   # Banking, Housing, and Urban Affairs
    316:  "SSBU",   # Budget
    321:  "SSCM",   # Commerce, Science, and Transportation
    330:  "SSEG",   # Energy and Natural Resources
    332:  "SSEV",   # Environment and Public Works
    336:  "SSFI",   # Finance
    338:  "SSFR",   # Foreign Relations
    344:  "SSGA",   # Homeland Security and Governmental Affairs
    358:  "SSJU",   # Judiciary
    362:  "SSHR",   # Health, Education, Labor, and Pensions
    380:  "SSSR",   # Rules and Administration
    384:  "SSSB",   # Small Business and Entrepreneurship
    388:  "SSVA",   # Veterans' Affairs
    432:  "SLIN",   # Intelligence (Select)
    434:  "SSEC",   # Ethics (Select)
    503:  "JSTX",   # Taxation (Joint)
    507:  "JSEC",   # Economic (Joint)
}

# ─────────────────────────────────────────────────────────────────
# BUILD ICPSR -> BIOGUIDE CROSSWALK
# Uses legislators-historical.yaml from unitedstates/congress-legislators
# ─────────────────────────────────────────────────────────────────

def build_icpsr_crosswalk():
    """
    Returns dict: {icpsr_id (int): bioguide_id (str)}
    """
    if not HIST_LEG_FILE.exists():
        print(f"  WARNING: {HIST_LEG_FILE.name} not found")
        print(f"  Download from: https://raw.githubusercontent.com/unitedstates/congress-legislators/main/legislators-historical.yaml")
        return {}

    print("  Loading legislators-historical.yaml...")
    with open(HIST_LEG_FILE, "r", encoding="utf-8") as f:
        legislators = yaml.safe_load(f)

    crosswalk = {}
    for leg in legislators:
        bioguide = leg.get("id", {}).get("bioguide", "")
        icpsr    = leg.get("id", {}).get("icpsr")
        if bioguide and icpsr:
            crosswalk[int(icpsr)] = bioguide

    print(f"  Crosswalk built: {len(crosswalk):,} ICPSR -> bioguide mappings")
    return crosswalk

# ─────────────────────────────────────────────────────────────────
# PARSE DATE
# ─────────────────────────────────────────────────────────────────

def parse_date(val):
    if pd.isna(val):
        return None
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(val, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None
    if hasattr(val, 'strftime'):
        return val.strftime("%Y-%m-%d")
    return None

# ─────────────────────────────────────────────────────────────────
# PARSE HOUSE FILE
# ─────────────────────────────────────────────────────────────────

def parse_house(crosswalk):
    if not HOUSE_FILE.exists():
        print(f"  WARNING: {HOUSE_FILE.name} not found — skipping house")
        return []

    print(f"  Reading {HOUSE_FILE.name}...")
    df = pd.read_excel(HOUSE_FILE)

    # Normalise column names
    df.columns = [str(c).strip() for c in df.columns]

    records    = []
    skipped_no_bioguide  = 0
    skipped_no_thomas_id = 0

    for _, row in df.iterrows():
        try:
            congress    = int(row.get("Congress", 0))
            comm_code   = row.get("Committee code")
            icpsr       = row.get("ID #")
            name        = str(row.get("Name", "")).strip()
            party_code  = row.get("Party")
            start_date  = parse_date(row.get("Date of Assignment"))
            end_date    = parse_date(row.get("Date of Termination"))
            rank        = row.get("Rank Within Party Status")

            if pd.isna(comm_code) or pd.isna(icpsr):
                continue

            comm_code = int(comm_code)
            icpsr     = int(icpsr)

            # Look up thomas_id
            thomas_id = HOUSE_CODE_MAP.get(comm_code)
            if not thomas_id:
                skipped_no_thomas_id += 1
                continue

            # Look up bioguide
            bioguide = crosswalk.get(icpsr)
            if not bioguide:
                skipped_no_bioguide += 1
                continue

            # Party
            party = "Democrat" if str(party_code) in ("100", "D") else \
                    "Republican" if str(party_code) in ("200", "R") else ""

            records.append({
                "bioguide":   bioguide,
                "thomas_id":  thomas_id,
                "party":      party,
                "rank":       int(rank) if not pd.isna(rank) else None,
                "title":      "",
                "start_date": start_date,
                "end_date":   end_date,
                "source":     f"mit_house_{congress}",
            })

        except Exception:
            continue

    print(f"  House records: {len(records):,} loaded | "
          f"{skipped_no_bioguide} no bioguide | "
          f"{skipped_no_thomas_id} no thomas_id mapping")
    return records

# ─────────────────────────────────────────────────────────────────
# PARSE SENATE FILE
# ─────────────────────────────────────────────────────────────────

def parse_senate(crosswalk):
    if not SENATE_FILE.exists():
        print(f"  WARNING: {SENATE_FILE.name} not found — skipping senate")
        return []

    print(f"  Reading {SENATE_FILE.name}...")
    df = pd.read_excel(SENATE_FILE)
    df.columns = [str(c).strip() for c in df.columns]

    records    = []
    skipped_no_bioguide  = 0
    skipped_no_thomas_id = 0

    for _, row in df.iterrows():
        try:
            congress   = int(row.get("Congress", 0))
            comm_code  = row.get("Committee Code")
            icpsr      = row.get("ID #")
            party_code = row.get("Party Code")
            start_date = parse_date(row.get("Date of Appointment"))
            end_date   = parse_date(row.get("Date of Termination"))
            rank       = row.get("Rank Within Party")

            if pd.isna(comm_code) or pd.isna(icpsr):
                continue

            comm_code = int(comm_code)
            icpsr     = int(icpsr)

            thomas_id = SENATE_CODE_MAP.get(comm_code)
            if not thomas_id:
                skipped_no_thomas_id += 1
                continue

            bioguide = crosswalk.get(icpsr)
            if not bioguide:
                skipped_no_bioguide += 1
                continue

            party = "Democrat" if str(party_code) in ("100", "D") else \
                    "Republican" if str(party_code) in ("200", "R") else ""

            records.append({
                "bioguide":   bioguide,
                "thomas_id":  thomas_id,
                "party":      party,
                "rank":       int(rank) if not pd.isna(rank) else None,
                "title":      "",
                "start_date": start_date,
                "end_date":   end_date,
                "source":     f"mit_senate_{congress}",
            })

        except Exception:
            continue

    print(f"  Senate records: {len(records):,} loaded | "
          f"{skipped_no_bioguide} no bioguide | "
          f"{skipped_no_thomas_id} no thomas_id mapping")
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

    # Summary by source
    cursor.execute("""
        SELECT source, COUNT(*) FROM committee_memberships
        WHERE source LIKE 'mit_%'
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

def run_mit_parser():
    print("=" * 60)
    print("  MIT Committee Assignment Parser")
    print("  Coverage: 103rd-115th Congress (1993-2017)")
    print("=" * 60)

    # Build ICPSR -> bioguide crosswalk
    print("\n  Building ICPSR crosswalk...")
    crosswalk = build_icpsr_crosswalk()

    if not crosswalk:
        print("  ERROR: Cannot proceed without crosswalk")
        return

    # Parse both files
    print("\n  Parsing house assignments...")
    house_records = parse_house(crosswalk)

    print("\n  Parsing senate assignments...")
    senate_records = parse_senate(crosswalk)

    all_records = house_records + senate_records
    print(f"\n  Total records to load: {len(all_records):,}")

    # Load into DB
    print("\n  Loading into database...")
    inserted, total, unique_pols, by_source = load_into_db(all_records)

    print("\n" + "=" * 60)
    print(f"  Complete!")
    print(f"  Records inserted:     {inserted:,}")
    print(f"  Total memberships:    {total:,}")
    print(f"  Unique politicians:   {unique_pols:,}")
    print(f"\n  MIT records by congress:")
    for source, count in by_source:
        print(f"    {source}: {count:,}")
    print("=" * 60)


if __name__ == "__main__":
    run_mit_parser()