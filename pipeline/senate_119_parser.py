# pipeline/senate_119_parser.py
# Parses the Senate 119th Congress committee assignment PDF
# Source: govinfo.gov senate_assignments_119.pdf
#
# Uses the alphabetical "COMMITTEE ASSIGNMENTS OF SENATORS" section
# at the back of the PDF (pages 32-42) — cleaner and less error-prone
# than the two-column committee-by-committee section.
#
# Run: python pipeline/senate_119_parser.py

import sqlite3
import pdfplumber
import yaml
import re
from pathlib import Path
from collections import defaultdict

DB_PATH     = Path(__file__).parent.parent / "data" / "trades.db"
PDF_PATH    = Path(__file__).parent.parent / "data" / "committees" / "senate_assignments_119.pdf"
LEG_CURRENT = Path(__file__).parent.parent / "data" / "committees" / "legislators-current.yaml"

# Committee name (lowercased, as it appears in the alphabetical section) -> thomas_id
COMMITTEE_NAME_TO_THOMAS = {
    "agriculture, nutrition, and forestry":       "SSAF",
    "appropriations":                             "SSAP",
    "armed services":                             "SSAS",
    "banking, housing, and urban affairs":        "SSBK",
    "budget":                                     "SSBU",
    "commerce, science, and transportation":      "SSCM",
    "energy and natural resources":               "SSEG",
    "environment and public works":               "SSEV",
    "finance":                                    "SSFI",
    "foreign relations":                          "SSFR",
    "health, education, labor, and pensions":     "SSHR",
    "homeland security and governmental affairs": "SSGA",
    "judiciary":                                  "SSJU",
    "rules and administration":                   "SSRA",
    "small business and entrepreneurship":        "SSSB",
    "veterans' affairs":                          "SSVA",
    "veterans\u2019 affairs":                     "SSVA",   # smart quote variant
    "veterans\ufffd affairs":                     "SSVA",   # PDF encoding variant
    "indian affairs":                             "SLIA",
    "select committee on ethics":                 "SSEC",
    "select committee on intelligence":           "SLIN",
    "special committee on aging":                 "SPAG",
    "joint economic committee":                   "JSEC",
    "joint committee on the library":             "JLIB",
    "joint committee on printing":                "JPRT",
    "joint committee on taxation":                "JTAX",
}

# Lines to skip in the alphabetical section
SKIP_PATTERNS = re.compile(
    r'^\d+$'                        # page numbers
    r'|^no$'                        # footer junk
    r'|^COMMITTEE ASSIGNMENTS'      # section header
    r'|^\(Chairman\)$'              # standalone role lines only
    r'|^\(Vice Chairman\)$'
    r'|VerDate|GNIRAEH|YELLEKT|Jkt',
    re.IGNORECASE
)

# Senator line: "Mr./Ms./Mrs./Miss LASTNAME [of State] ..... Committee"
# The "of State" disambiguation appears when two senators share a last name (e.g. both Scotts)
SENATOR_LINE = re.compile(
    r'^(?:Mr\.|Ms\.|Mrs\.|Miss|Dr\.)\s+([A-Z][A-Z\s\'\-\.]+?)'
    r'(?:\s+of\s+([A-Za-z\s]+?))?\s*\.{3,}\s*(.+)$'
)


# ─────────────────────────────────────────────────────────────────
# NAME MATCHING
# ─────────────────────────────────────────────────────────────────

def normalize(text):
    """Lowercase, keep only ASCII alpha/space/hyphen."""
    import unicodedata
    return ''.join(
        c for c in unicodedata.normalize('NFD', text.lower())
        if c.isascii() and (c.isalpha() or c in ' -')
    )


# Full US state name -> 2-letter abbreviation for disambiguation
STATE_ABBREV = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
    'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID',
    'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS',
    'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS',
    'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK',
    'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC',
    'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT',
    'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA', 'west virginia': 'WV',
    'wisconsin': 'WI', 'wyoming': 'WY',
}


def load_senator_lookup():
    """
    Returns:
      by_last: last_word_norm -> [(bioguide, first_norm, full_last_norm, party, state_abbrev)]
    """
    by_last = defaultdict(list)

    with open(LEG_CURRENT, encoding='utf-8') as f:
        data = yaml.safe_load(f)

    for leg in data:
        if not any(t.get('type') == 'sen' for t in leg.get('terms', [])):
            continue
        bioguide = leg['id']['bioguide']
        name     = leg.get('name', {})
        first    = normalize(name.get('first', ''))
        last     = normalize(name.get('last',  ''))
        last_word = last.split()[-1] if last else last

        sen_terms  = [t for t in leg.get('terms', []) if t.get('type') == 'sen']
        last_term  = sen_terms[-1] if sen_terms else {}
        party      = last_term.get('party', '')
        party_code = 'D' if 'democrat' in party.lower() else ('R' if 'republican' in party.lower() else 'I')
        state      = last_term.get('state', '')

        by_last[last_word].append((bioguide, first, last, party_code, state))

    return by_last


def match_senator(last_name_raw, state_raw, by_last):
    """
    Match PDF last name (ALL CAPS) + optional state name to a bioguide entry.
    state_raw is the full state name from the PDF (e.g. 'Florida'), or None.
    """
    norm      = normalize(last_name_raw.strip())
    last_word = norm.split()[-1] if norm else norm
    state_abbrev = STATE_ABBREV.get(state_raw.lower()) if state_raw else None

    candidates = by_last.get(last_word, [])
    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    # Use state to disambiguate if provided
    if state_abbrev:
        for entry in candidates:
            if entry[4] == state_abbrev:
                return entry

    # Try matching full last name
    for entry in candidates:
        if entry[2] == norm:
            return entry

    # Try last two words of PDF name against full last name
    parts = norm.split()
    if len(parts) >= 2:
        two_word = ' '.join(parts[-2:])
        for entry in candidates:
            if entry[2] == two_word:
                return entry

    return None


# ─────────────────────────────────────────────────────────────────
# PDF PARSER — alphabetical section
# ─────────────────────────────────────────────────────────────────

def parse_committee_name(raw):
    """Clean and look up a committee name from the alphabetical section."""
    # Strip trailing role markers like "(Chairman)", "(Vice Chairman)"
    cleaned = re.sub(r'\s*\([^)]*\)\s*$', '', raw).strip()
    # Normalise smart quotes and PDF encoding noise
    cleaned = cleaned.replace('\u2019', "'").replace('\ufffd', '')
    return COMMITTEE_NAME_TO_THOMAS.get(cleaned.lower())


def parse_senate_pdf():
    """
    Parse the alphabetical 'COMMITTEE ASSIGNMENTS OF SENATORS' section.
    Returns list of dicts: {bioguide, thomas_id, party, title}
    """
    by_last = load_senator_lookup()

    memberships = []
    seen        = set()   # (bioguide, thomas_id)

    current_senator = None   # (bioguide, party_code)
    in_alpha_section = False
    unmatched = []

    with pdfplumber.open(PDF_PATH) as pdf:
        for page_num, page in enumerate(pdf.pages):
            text = page.extract_text()
            if not text:
                continue

            for line in text.split('\n'):
                line = line.strip()
                if not line:
                    continue

                # Detect start of alphabetical section
                if 'COMMITTEE ASSIGNMENTS OF SENATORS' in line:
                    in_alpha_section = True
                    continue

                if not in_alpha_section:
                    continue

                if SKIP_PATTERNS.search(line):
                    continue

                # Try to match a senator line: "Mr. LASTNAME .... Committee"
                m = SENATOR_LINE.match(line)
                if m:
                    last_name_raw = m.group(1).strip()
                    state_raw     = m.group(2)           # None if not present
                    committee_raw = m.group(3).strip()

                    entry = match_senator(last_name_raw, state_raw, by_last)
                    if entry:
                        current_senator = entry   # (bioguide, first, full_last, party)
                    else:
                        unmatched.append(last_name_raw)
                        current_senator = None

                    # Record first committee on same line
                    if current_senator:
                        thomas_id = parse_committee_name(committee_raw)
                        if thomas_id:
                            key = (current_senator[0], thomas_id)
                            if key not in seen:
                                seen.add(key)
                                memberships.append({
                                    'bioguide':  current_senator[0],
                                    'thomas_id': thomas_id,
                                    'party':     current_senator[3],
                                    'title':     'Member',
                                })
                    continue

                # Continuation line — committee for current senator
                if current_senator:
                    thomas_id = parse_committee_name(line)
                    if thomas_id:
                        key = (current_senator[0], thomas_id)
                        if key not in seen:
                            seen.add(key)
                            memberships.append({
                                'bioguide':  current_senator[0],
                                'thomas_id': thomas_id,
                                'party':     current_senator[3],
                                'title':     'Member',
                            })

    if unmatched:
        print(f"\n  Unmatched names ({len(set(unmatched))}):")
        for n in sorted(set(unmatched)):
            print(f"    - {n}")

    return memberships


# ─────────────────────────────────────────────────────────────────
# DB WRITER
# ─────────────────────────────────────────────────────────────────

def load_to_db(memberships):
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("DELETE FROM committee_memberships WHERE source = 'senate_119'")
    deleted = cursor.rowcount
    if deleted:
        print(f"  Removed {deleted} stale senate_119 records")

    inserted = 0
    for m in memberships:
        cursor.execute("""
            INSERT OR REPLACE INTO committee_memberships
                (bioguide, thomas_id, party, rank, title,
                 start_date, end_date, source, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            m['bioguide'],
            m['thomas_id'],
            m['party'],
            None,
            m['title'],
            '2025-01-03',
            None,
            'senate_119',
            'high',
        ))
        inserted += 1

    conn.commit()

    cursor.execute("""
        SELECT thomas_id, COUNT(*) as cnt
        FROM committee_memberships
        WHERE source = 'senate_119'
        GROUP BY thomas_id
        ORDER BY thomas_id
    """)
    rows = cursor.fetchall()

    print("\n  Senate 119th memberships by committee:")
    total = 0
    for thomas_id, cnt in rows:
        print(f"    {thomas_id:<8} {cnt:>3}")
        total += cnt
    print(f"    {'TOTAL':<8} {total:>3}")

    conn.close()
    return inserted


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  Senate 119th Congress Committee Parser (v2)")
    print("  Source: senate_assignments_119.pdf — alphabetical section")
    print("=" * 60)

    print("\n  Parsing PDF...")
    memberships = parse_senate_pdf()
    print(f"\n  Parsed {len(memberships)} senator-committee assignments")

    print("\n  Loading to DB...")
    inserted = load_to_db(memberships)
    print(f"\n  Done — {inserted} records (source='senate_119', confidence='high')")
    print("\n  NOTE: Re-run pipeline/committee_loader.py to update committee_relevance flags.")
