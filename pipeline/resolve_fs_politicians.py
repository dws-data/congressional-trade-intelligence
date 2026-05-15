"""
resolve_fs_politicians.py

Resolves the 18 synthetic FS_ politician IDs created during the FMP Senate merge
for former/retired senators not present in Capitol Trades data.

For each politician:
  1. If the real bioguide ID already exists in politicians: migrate trades to it, delete FS_ row
  2. If not: insert new politicians row with real ID + known metadata, migrate trades, delete FS_ row

After running: re-run scorer to generate scores for newly resolved politicians.
"""

import sqlite3
from datetime import datetime

DB_PATH = "data/trades.db"

# Mapping: FS_ slug -> (real_bioguide_id, full_name, party, state)
RESOLUTIONS = [
    ("FS_david-perdue",     "P000612", "David Perdue",      "R", "GA"),
    ("FS_pat-roberts",      "R000307", "Pat Roberts",       "R", "KS"),
    ("FS_kelly-loeffler",   "L000594", "Kelly Loeffler",    "R", "GA"),
    ("FS_john-hoeven",      "H001061", "John Hoeven",       "R", "ND"),
    ("FS_bill-cassidy",     "C001075", "Bill Cassidy",      "R", "LA"),
    ("FS_james-inhofe",     "I000024", "James Inhofe",      "R", "OK"),
    ("FS_pat-toomey",       "T000461", "Pat Toomey",        "R", "PA"),
    ("FS_john-reed",        "R000122", "Jack Reed",         "D", "RI"),
    ("FS_roy-blunt",        "B000575", "Roy Blunt",         "R", "MO"),
    ("FS_deb-fischer",      "F000463", "Deb Fischer",       "R", "NE"),
    ("FS_tim-kaine",        "K000384", "Tim Kaine",         "D", "VA"),
    ("FS_maria-cantwell",   "C000127", "Maria Cantwell",    "D", "WA"),
    ("FS_john-thune",       "T000250", "John Thune",        "R", "SD"),
    ("FS_dianne-feinstein", "F000062", "Dianne Feinstein",  "D", "CA"),
    ("FS_roger-wicker",     "W000437", "Roger Wicker",      "R", "MS"),
    ("FS_thom-tillis",      "T000476", "Thom Tillis",       "R", "NC"),
    ("FS_tom-udall",        "U000039", "Tom Udall",         "D", "NM"),
    ("FS_jacklyn-rosen",    "R000608", "Jacky Rosen",       "D", "NV"),
]

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print(f"Starting FS_ resolution — {len(RESOLUTIONS)} politicians")
print("=" * 60)

migrated_total = 0
inserted_politicians = 0
merged_politicians = 0

for fs_id, real_id, name, party, state in RESOLUTIONS:
    # Count trades under FS_ ID
    cur.execute("SELECT COUNT(*) FROM trades WHERE politician_id = ?", (fs_id,))
    trade_count = cur.fetchone()[0]

    if trade_count == 0:
        print(f"  SKIP {fs_id} — no trades found")
        continue

    # Check if real ID already exists in politicians
    cur.execute("SELECT politician_id, name FROM politicians WHERE politician_id = ?", (real_id,))
    existing = cur.fetchone()

    if existing:
        print(f"  MERGE  {fs_id} -> {real_id} ({existing['name']}) — real ID already exists, migrating {trade_count} trades")
        merged_politicians += 1
    else:
        print(f"  INSERT {fs_id} -> {real_id} ({name}) — inserting new politician row, migrating {trade_count} trades")
        cur.execute("""
            INSERT INTO politicians (politician_id, name, party, chamber, state)
            VALUES (?, ?, ?, 'Senate', ?)
        """, (real_id, name, party, state))
        inserted_politicians += 1

    # Migrate trades
    cur.execute(
        "UPDATE trades SET politician_id = ? WHERE politician_id = ?",
        (real_id, fs_id)
    )
    migrated = cur.rowcount
    migrated_total += migrated

    # Delete FS_ row from politicians
    cur.execute("DELETE FROM politicians WHERE politician_id = ?", (fs_id,))

    print(f"         {migrated} trades migrated, FS_ row deleted")

conn.commit()

print()
print("=" * 60)
print(f"Done.")
print(f"  Politicians inserted : {inserted_politicians}")
print(f"  Politicians merged   : {merged_politicians}")
print(f"  Trades migrated      : {migrated_total:,}")

# Verify no FS_ rows remain
cur.execute("SELECT COUNT(*) FROM politicians WHERE politician_id LIKE 'FS_%'")
remaining_pol = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM trades WHERE politician_id LIKE 'FS_%'")
remaining_trades = cur.fetchone()[0]
print(f"  FS_ politicians remaining : {remaining_pol}")
print(f"  FS_ trades remaining      : {remaining_trades}")

conn.close()

if remaining_pol == 0 and remaining_trades == 0:
    print("\nAll FS_ IDs resolved. Run scorer next:")
    print("  python pipeline/scorer.py")
else:
    print("\n*** WARNING: some FS_ IDs remain — check output above")
