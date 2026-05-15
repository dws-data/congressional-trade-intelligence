# pipeline/congress_api_fetcher.py
# Fetches historical committee membership from Congress.gov API
# Uses the correct approach: member -> committees (not committee -> members)
#
# API key: store in data/congress_api_key.txt (one line, no spaces)
#
# Run: python pipeline/congress_api_fetcher.py

import sqlite3
import requests
import time
from pathlib import Path

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────

DB_PATH  = Path(__file__).parent.parent / "data" / "trades.db"
KEY_FILE = Path(__file__).parent.parent / "data" / "congress_api_key.txt"
BASE_URL = "https://api.congress.gov/v3"
DELAY    = 0.3   # seconds between API calls

CONGRESSES = [116, 117, 118, 119]

CONGRESS_DATE_RANGES = {
    116: ("2019-01-03", "2021-01-03"),
    117: ("2021-01-03", "2023-01-03"),
    118: ("2023-01-03", "2025-01-03"),
    119: ("2025-01-03", None),
}

# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────

def load_api_key():
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip()
    raise FileNotFoundError(f"API key not found at {KEY_FILE}")

def api_get(url, api_key, params=None):
    if params is None:
        params = {}
    params["api_key"] = api_key
    params["format"]  = "json"
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 429:
            print("    Rate limited — waiting 60s...")
            time.sleep(60)
            return api_get(url, api_key, params)
        else:
            return None
    except Exception as e:
        print(f"    Request failed: {e}")
        return None

# ─────────────────────────────────────────────────────────────────
# FETCH ALL MEMBERS FOR A CONGRESS
# Uses /v3/member/congress/{N}?currentMember=false
# ─────────────────────────────────────────────────────────────────

def fetch_members_for_congress(api_key, congress):
    members  = []
    offset   = 0
    limit    = 250

    while True:
        url  = f"{BASE_URL}/member/congress/{congress}"
        data = api_get(url, api_key, params={
            "currentMember": "false",
            "limit":          limit,
            "offset":         offset,
        })
        time.sleep(DELAY)

        if not data:
            break

        batch = data.get("members", [])
        members.extend(batch)

        total = data.get("pagination", {}).get("count", 0)
        if len(members) >= total or not batch:
            break

        offset += limit

    return members

# ─────────────────────────────────────────────────────────────────
# FETCH COMMITTEE ASSIGNMENTS FOR A SINGLE MEMBER
# Uses /v3/member/{bioguideId} — returns terms with committees
# ─────────────────────────────────────────────────────────────────

def fetch_member_committees(api_key, bioguide_id):
    url  = f"{BASE_URL}/member/{bioguide_id}"
    data = api_get(url, api_key)
    time.sleep(DELAY)

    if not data:
        return []

    member = data.get("member", {})
    terms  = member.get("terms", {})

    # terms can be a dict with "item" key or a list
    if isinstance(terms, dict):
        term_list = terms.get("item", [])
    elif isinstance(terms, list):
        term_list = terms
    else:
        term_list = []

    committees = []
    for term in term_list:
        congress   = term.get("congress")
        chamber    = term.get("chamber", "")
        term_comms = term.get("committees", [])

        if isinstance(term_comms, dict):
            term_comms = term_comms.get("item", [])

        for comm in term_comms:
            thomas_id = comm.get("systemCode", "") or comm.get("committeeCode", "")
            name      = comm.get("name", "")
            committees.append({
                "congress":  congress,
                "chamber":   chamber,
                "thomas_id": thomas_id,
                "name":      name,
            })

    return committees

# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

def run_congress_api_fetcher():
    print("=" * 60)
    print("  Congress.gov API — Committee Membership Fetcher v2")
    print("  Approach: member -> committees (correct endpoint)")
    print("=" * 60)

    api_key = load_api_key()
    print(f"  API key: {api_key[:8]}...")

    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Ensure table exists
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
    conn.commit()

    total_inserted = 0

    for congress in CONGRESSES:
        start_date, end_date = CONGRESS_DATE_RANGES[congress]
        print(f"\n  Congress {congress} ({start_date} → {end_date or 'present'})")

        # Step 1: get all members for this congress
        print(f"    Fetching member list...")
        members = fetch_members_for_congress(api_key, congress)
        print(f"    Found {len(members)} members")

        if not members:
            print(f"    No members returned — skipping")
            continue

        # Step 2: for each member fetch their committee assignments
        congress_inserted = 0
        for i, member in enumerate(members):
            bioguide = member.get("bioguideId", "")
            if not bioguide:
                continue

            committees = fetch_member_committees(api_key, bioguide)

            for comm in committees:
                # Only keep assignments for this congress
                if comm["congress"] != congress:
                    continue
                thomas_id = comm["thomas_id"]
                if not thomas_id:
                    continue

                cursor.execute("""
                    INSERT OR REPLACE INTO committee_memberships
                        (bioguide, thomas_id, party, rank, title,
                         start_date, end_date, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    bioguide,
                    thomas_id,
                    member.get("partyName", ""),
                    None,
                    None,
                    start_date,
                    end_date,
                    f"congress_api_{congress}",
                ))
                congress_inserted += 1
                total_inserted    += 1

            if (i + 1) % 50 == 0:
                conn.commit()
                print(f"    [{i+1}/{len(members)}] {congress_inserted} memberships so far...")

        conn.commit()
        print(f"  Congress {congress}: {congress_inserted} memberships loaded")

    # Summary
    cursor.execute("SELECT COUNT(*) FROM committee_memberships")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(DISTINCT bioguide) FROM committee_memberships")
    unique_pols = cursor.fetchone()[0]

    cursor.execute("""
        SELECT source, COUNT(*) FROM committee_memberships
        GROUP BY source ORDER BY source
    """)
    by_source = cursor.fetchall()

    conn.close()

    print("\n" + "=" * 60)
    print(f"  Complete!")
    print(f"  Total memberships:   {total:,}")
    print(f"  Unique politicians:  {unique_pols:,}")
    print(f"\n  By congress:")
    for source, count in by_source:
        print(f"    {source}: {count:,}")
    print("=" * 60)


if __name__ == "__main__":
    run_congress_api_fetcher()