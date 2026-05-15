# qa/parse_clerk_pdf.py
# Parses the official Clerk of the House committee assignment PDF
# Cross-checks against our stored committee_memberships table
# for the 119th Congress
#
# Save oal.pdf to data/committees/oal.pdf first
# Run: python qa/parse_clerk_pdf.py

import sqlite3
import pdfplumber
import re
from pathlib import Path
from collections import defaultdict

DB_PATH  = Path(__file__).parent.parent / "data" / "trades.db"
PDF_PATH = Path(__file__).parent.parent / "data" / "committees" / "oal.pdf"

# Map PDF committee names -> our thomas_id
# Using the exact names that appear in the PDF
PDF_NAME_TO_THOMAS = {
    "agriculture":                          "HSAG",
    "appropriations":                       "HSAP",
    "armed services":                       "HSAS",
    "budget":                               "HSBU",
    "education and workforce":              "HSED",
    "energy and commerce":                  "HSIF",
    "ethics":                               "HSSO",
    "financial services":                   "HSBA",
    "foreign affairs":                      "HSFA",
    "homeland security":                    "HSHM",
    "house administration":                 "HSHA",
    "judiciary":                            "HSJU",
    "natural resources":                    "HSII",
    "oversight and government reform":      "HSGO",
    "oversight and accountability":         "HSGO",
    "rules":                                "HSRU",
    "science, space, and technology":       "HSSY",
    "small business":                       "HSSM",
    "transportation and infrastructure":    "HSPW",
    "veterans' affairs":                    "HSVR",
    "veterans affairs":                     "HSVR",
    "ways and means":                       "HSWM",
    "permanent select committee on intelligence": "HLIG",
    "select intelligence":                  "HLIG",
    "intelligence":                         "HLIG",
    "joint economic committee":             "JSEC",
    "joint economic":                       "JSEC",
    "select committee on the strategic competition": "HSCN",
}

# Our thomas_id -> readable name for display
THOMAS_TO_NAME = {
    "HSAG": "Agriculture",
    "HSAP": "Appropriations",
    "HSAS": "Armed Services",
    "HSBU": "Budget",
    "HSED": "Education & Workforce",
    "HSIF": "Energy & Commerce",
    "HSSO": "Ethics",
    "HSBA": "Financial Services",
    "HSFA": "Foreign Affairs",
    "HSHM": "Homeland Security",
    "HSHA": "House Administration",
    "HSJU": "Judiciary",
    "HSII": "Natural Resources",
    "HSGO": "Oversight & Gov Reform",
    "HSRU": "Rules",
    "HSSY": "Science, Space & Tech",
    "HSSM": "Small Business",
    "HSPW": "Transportation",
    "HSVR": "Veterans Affairs",
    "HSWM": "Ways & Means",
    "HLIG": "Intelligence",
    "JSEC": "Joint Economic",
    "HSCN": "Select China",
}

def clean_committee_name(raw):
    """Normalise a committee name from the PDF."""
    # Remove leadership indicators like "Chair", "Ranking Member" etc.
    name = re.sub(r',?\s*(chair|ranking member|vice chair|ranking)\.?$', '', raw, flags=re.IGNORECASE)
    name = name.strip().rstrip(',').strip().lower()
    return name

def parse_pdf():
    """
    Parse the Clerk PDF and return dict:
    { last_name_key: { "display_name": str, "committees": [thomas_id, ...] } }
    """
    print(f"  Parsing {PDF_PATH.name}...")

    members = {}
    current_member = None
    current_committees = []

    # Regex to detect a member line:
    # "Adams, AlmaS.,12th NC............................ Agriculture."
    member_pattern = re.compile(
        r'^([A-Z][a-zA-Z\-\']+),\s+(.+?),\s*\d+\w*\s+\w{2}[.\s]+(.+)'
    )

    with pdfplumber.open(PDF_PATH) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue

            for line in text.split('\n'):
                line = line.strip()
                if not line:
                    continue

                # Skip header lines
                if any(skip in line for skip in [
                    'OFFICIAL', 'HOUSE OF REPRESENTATIVES', 'UNITED STATES',
                    'ONE HUNDRED', 'Compiled by', 'clerk.house.gov',
                    'Republicans in roman', 'Resident Commissioner',
                    'March', 'January', 'February', '(', 'Page'
                ]):
                    continue

                # Try to match a new member line
                # Pattern: "Lastname, Firstname...... Committee."
                m = re.match(r'^([A-Z][a-zA-Z\s\-\'\.]+),\s+(.+?)\.\.\.*\s+(.+)', line)
                if m:
                    # Save previous member
                    if current_member and current_committees:
                        members[current_member['key']] = {
                            'display': current_member['display'],
                            'bioguide': current_member.get('bioguide', ''),
                            'committees': current_committees[:]
                        }

                    last_name  = m.group(1).strip()
                    first_rest = m.group(2).strip()
                    comm_text  = m.group(3).strip().rstrip('.')

                    # Build display name
                    # first_rest looks like "AlmaS.,12th NC" or "RickW.,12th GA"
                    # Extract just the first name part
                    first_name = re.sub(r'[A-Z]\.,.*$', '', first_rest).strip()
                    first_name = re.sub(r',.*$', '', first_name).strip()
                    display    = f"{first_name} {last_name}".strip()
                    key        = last_name.lower()

                    current_member     = {'key': key, 'display': display}
                    current_committees = []

                    # Parse first committee on same line
                    comm_name = clean_committee_name(comm_text)
                    thomas_id = PDF_NAME_TO_THOMAS.get(comm_name)
                    if thomas_id:
                        current_committees.append(thomas_id)

                elif current_member is not None:
                    # This is a continuation line with more committees
                    comm_text = line.rstrip('.')
                    comm_name = clean_committee_name(comm_text)
                    thomas_id = PDF_NAME_TO_THOMAS.get(comm_name)
                    if thomas_id:
                        current_committees.append(thomas_id)

    # Save last member
    if current_member and current_committees:
        members[current_member['key']] = {
            'display':   current_member['display'],
            'bioguide':  '',
            'committees': current_committees
        }

    print(f"  Parsed {len(members)} members from PDF")
    return members

def cross_check(pdf_members):
    """
    Compare PDF committee assignments against our DB for 119th Congress.
    Focus on politicians in our politicians table (those we score/track).
    """
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Get our stored 119th Congress memberships
    cursor.execute("""
        SELECT cm.bioguide, cm.thomas_id, p.name
        FROM committee_memberships cm
        JOIN politicians p ON cm.bioguide = p.politician_id
        WHERE cm.source IN ('clerk_house_119', 'current')
        AND cm.confidence = 'high'
        ORDER BY p.name
    """)
    our_data = cursor.fetchall()

    # Build lookup: bioguide -> set of thomas_ids we have
    our_by_bioguide = defaultdict(set)
    bioguide_to_name = {}
    for bioguide, thomas_id, name in our_data:
        our_by_bioguide[bioguide].add(thomas_id)
        bioguide_to_name[bioguide] = name

    # Get all politicians with name -> bioguide mapping
    cursor.execute("SELECT politician_id, name FROM politicians")
    all_pols = cursor.fetchall()

    # Build name lookup: last_name -> list of (bioguide, full_name)
    name_to_bioguide = defaultdict(list)
    for bioguide, name in all_pols:
        if name:
            last = name.split()[-1].lower()
            name_to_bioguide[last].append((bioguide, name))

    conn.close()

    print(f"\n  Cross-checking {len(pdf_members)} PDF members against our DB...")
    print(f"  Our DB has {len(our_by_bioguide)} politicians with 119th Congress data\n")

    matches     = 0
    correct     = 0
    discrepancies = []
    unmatched   = []

    for last_key, pdf_data in sorted(pdf_members.items()):
        pdf_committees = set(pdf_data['committees'])
        pdf_display    = pdf_data['display']

        # Try to match to our politicians by last name
        candidates = name_to_bioguide.get(last_key, [])

        if not candidates:
            unmatched.append(pdf_display)
            continue

        # If multiple candidates pick best match
        bioguide = candidates[0][0]
        our_name = candidates[0][1]

        if bioguide not in our_by_bioguide:
            # We don't have this politician in 119th data
            continue

        matches += 1
        our_committees = our_by_bioguide[bioguide]

        missing_from_ours = pdf_committees - our_committees
        extra_in_ours     = our_committees - pdf_committees

        # Filter out committees we don't track (like JSEC, HSCN)
        missing_filtered = {t for t in missing_from_ours if t in THOMAS_TO_NAME}
        extra_filtered   = {t for t in extra_in_ours     if t in THOMAS_TO_NAME}

        if missing_filtered or extra_filtered:
            discrepancies.append({
                'name':    our_name,
                'bioguide': bioguide,
                'pdf':     {THOMAS_TO_NAME.get(t, t) for t in pdf_committees},
                'ours':    {THOMAS_TO_NAME.get(t, t) for t in our_committees},
                'missing': {THOMAS_TO_NAME.get(t, t) for t in missing_filtered},
                'extra':   {THOMAS_TO_NAME.get(t, t) for t in extra_filtered},
            })
        else:
            correct += 1

    # Print results
    print(f"  {'='*60}")
    print(f"  CROSS-CHECK RESULTS")
    print(f"  {'='*60}")
    print(f"  Politicians matched:  {matches}")
    print(f"  Fully correct:        {correct} ({correct/matches*100:.0f}% if matches else 0%)")
    print(f"  With discrepancies:   {len(discrepancies)}")
    print(f"  Not in our DB:        {len(unmatched)}")

    if discrepancies:
        print(f"\n  DISCREPANCIES (PDF says vs what we have):")
        print(f"  {'-'*60}")
        for d in sorted(discrepancies, key=lambda x: x['name']):
            print(f"\n  {d['name']}  ({d['bioguide']})")
            print(f"    PDF has:   {sorted(d['pdf'])}")
            print(f"    We have:   {sorted(d['ours'])}")
            if d['missing']:
                print(f"    MISSING:   {sorted(d['missing'])}")
            if d['extra']:
                print(f"    EXTRA:     {sorted(d['extra'])}")

    print(f"\n  {'='*60}")
    return discrepancies

if __name__ == "__main__":
    print("=" * 60)
    print("  Clerk PDF Committee Assignment Verifier")
    print("  Source: Official Alphabetical List (oal.pdf)")
    print("  Checking: 119th Congress House members")
    print("=" * 60)

    pdf_members  = parse_pdf()
    discrepancies = cross_check(pdf_members)

    print("\nDone.")