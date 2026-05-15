# qa/fix_119_committees.py
# Fixes committee assignment errors in the 119th Congress data
# identified by cross-checking against the official Clerk PDF (oal.pdf)
#
# Only fixes House members — Senators are excluded from this fix
# since the PDF is House-only
#
# Run: python qa/fix_119_committees.py

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "trades.db"

# Corrections based on PDF cross-check
# Format: (bioguide, correct_thomas_ids, note)
# correct_thomas_ids = the COMPLETE list of full committees for this member
# in the 119th Congress per the official Clerk PDF

CORRECTIONS = [
    # Hal Rogers — PDF says Armed Services (not Appropriations)
    ("R000395", ["HSAS"],
     "Hal Rogers: PDF shows Armed Services only (switched from Appropriations)"),

    # Dwight Evans — PDF says Energy & Commerce + Homeland Security
    ("E000296", ["HSIF", "HSHM"],
     "Dwight Evans: corrected to Energy & Commerce + Homeland Security"),

    # Jonathan Jackson — missing Armed Services and Intelligence
    ("J000309", ["HSAG", "HSAS", "HSFA", "HLIG"],
     "Jonathan Jackson: added Armed Services and Intelligence"),

    # Buddy Carter — has Budget, should have Energy & Commerce + Homeland Security
    ("C001103", ["HSIF", "HSHM"],
     "Buddy Carter: corrected to Energy & Commerce + Homeland Security"),

    # David Joyce — has Appropriations, should have Energy & Commerce
    ("J000295", ["HSIF"],
     "David Joyce: corrected to Energy & Commerce"),

    # Mike Kelly — has Ways & Means, should have Agriculture + Armed Services + Intelligence
    ("K000376", ["HSAG", "HSAS", "HLIG"],
     "Mike Kelly: corrected to Agriculture + Armed Services + Intelligence"),

    # Mike Kennedy — has Natural Resources/Science/Transportation,
    # should have Homeland Security + Veterans Affairs
    ("K000403", ["HSHM", "HSVR"],
     "Mike Kennedy: corrected to Homeland Security + Veterans Affairs"),

    # Andrew Garbarino — missing Transportation
    ("G000597", ["HSSO", "HSBA", "HSHM", "HSPW"],
     "Andrew Garbarino: added Transportation"),

    # Eleanor Holmes Norton — extra Transportation
    ("N000147", ["HSGO"],
     "Eleanor Holmes Norton: removed extra Transportation"),

    # Don Beyer — extra Science, Space & Tech
    ("B001292", ["JSEC", "HSWM"],
     "Don Beyer: removed extra Science, Space & Tech"),

    # John Rutherford — extra Ethics
    ("R000609", ["HSAP"],
     "John Rutherford: removed extra Ethics"),
]

SOURCE     = "clerk_house_119"
START_DATE = "2025-01-03"
END_DATE   = None
CONFIDENCE = "high"

def run_fixes():
    print("=" * 60)
    print("  119th Congress Committee Assignment Fixer")
    print("  Based on official Clerk PDF (oal.pdf) cross-check")
    print("=" * 60)

    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    total_deleted = 0
    total_inserted = 0

    for bioguide, correct_committees, note in CORRECTIONS:
        print(f"\n  {note}")

        # Get current assignments for this politician in 119th
        cursor.execute("""
            SELECT thomas_id FROM committee_memberships
            WHERE bioguide = ? AND source = ?
        """, (bioguide, SOURCE))
        current = [r[0] for r in cursor.fetchall()]
        print(f"    Before: {sorted(current)}")

        # Delete all existing 119th assignments for this politician
        # from BOTH clerk_house_119 AND current YAML sources
        cursor.execute("""
            DELETE FROM committee_memberships
            WHERE bioguide = ? AND source IN ('clerk_house_119', 'current')
        """, (bioguide,))
        deleted = cursor.rowcount
        total_deleted += deleted

        # Insert correct assignments
        for thomas_id in correct_committees:
            cursor.execute("""
                INSERT OR REPLACE INTO committee_memberships
                    (bioguide, thomas_id, party, rank, title,
                     start_date, end_date, source, confidence)
                VALUES (?, ?, '', NULL, '', ?, ?, ?, ?)
            """, (bioguide, thomas_id, START_DATE, END_DATE,
                  SOURCE, CONFIDENCE))
            total_inserted += 1

        print(f"    After:  {sorted(correct_committees)}")

    conn.commit()

    # Re-run committee relevance flagging for affected politicians
    print(f"\n  Deleted: {total_deleted} old assignments")
    print(f"  Inserted: {total_inserted} corrected assignments")

    # Quick verification
    print("\n  Verification:")
    for bioguide, correct_committees, note in CORRECTIONS:
        cursor.execute("""
            SELECT thomas_id FROM committee_memberships
            WHERE bioguide = ? AND source = ?
            ORDER BY thomas_id
        """, (bioguide, SOURCE))
        stored = [r[0] for r in cursor.fetchall()]
        match  = sorted(stored) == sorted(correct_committees)
        cursor.execute("SELECT name FROM politicians WHERE politician_id = ?", (bioguide,))
        name = cursor.fetchone()
        name = name[0] if name else bioguide
        status = "OK" if match else "MISMATCH"
        print(f"    [{status}] {name}: {sorted(stored)}")

    conn.close()

    print("\n" + "=" * 60)
    print("  Done. Re-run sector_fetcher.py to refresh committee_relevance flags.")
    print("=" * 60)

if __name__ == "__main__":
    run_fixes()