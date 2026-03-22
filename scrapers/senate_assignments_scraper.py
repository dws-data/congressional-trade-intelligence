# pipeline/senate_assignments_scraper.py
# Live scraper for Senate committee assignments from senate.gov
# Source: https://www.senate.gov/general/committee_assignments/assignments.htm
#
# Replaces senate_119_parser.py as the live source of truth.
# Re-runnable mid-Congress to catch membership changes (deaths, resignations, switches).
#
# Run: python pipeline/senate_assignments_scraper.py

import re
import sqlite3
import unicodedata
import yaml
import requests
from bs4 import BeautifulSoup
from pathlib import Path
from collections import defaultdict

DB_PATH     = Path(__file__).parent.parent / "data" / "trades.db"
LEG_CURRENT = Path(__file__).parent.parent / "data" / "committees" / "legislators-current.yaml"

SOURCE_NAME = "senate_gov_live"
START_DATE  = "2025-01-03"   # 119th Congress start
URL         = "https://www.senate.gov/general/committee_assignments/assignments.htm"

# committee_memberships_SSAP.htm -> extract thomas_id from href
_THOMAS_FROM_URL = re.compile(r'committee_memberships_([A-Z0-9]+)\.htm', re.IGNORECASE)

# Party/state suffix in senator div: "(D-WI)"
_PARTY_STATE = re.compile(r'\(([A-Z]I?)-([A-Z]{2})\)')


# ─────────────────────────────────────────────────────────────────
# NAME NORMALISATION
# ─────────────────────────────────────────────────────────────────

def normalize(text):
    """Lowercase, keep only ASCII alpha/space/hyphen/apostrophe."""
    return ''.join(
        c for c in unicodedata.normalize('NFD', text.lower())
        if c.isascii() and (c.isalpha() or c in " -'")
    )


# ─────────────────────────────────────────────────────────────────
# SENATOR LOOKUP  (legislators-current.yaml)
# ─────────────────────────────────────────────────────────────────

def load_senator_lookup():
    """
    Returns by_last: last_word_norm -> [(bioguide, first_norm, full_last_norm, party, state)]
    """
    by_last = defaultdict(list)
    with open(LEG_CURRENT, encoding='utf-8') as f:
        data = yaml.safe_load(f)
    for leg in data:
        if not any(t.get('type') == 'sen' for t in leg.get('terms', [])):
            continue
        bioguide   = leg['id']['bioguide']
        name       = leg.get('name', {})
        first      = normalize(name.get('first', ''))
        last       = normalize(name.get('last',  ''))
        last_word  = last.split()[-1] if last else last
        sen_terms  = [t for t in leg.get('terms', []) if t.get('type') == 'sen']
        last_term  = sen_terms[-1] if sen_terms else {}
        party      = last_term.get('party', '')
        party_code = ('D' if 'democrat'   in party.lower() else
                      'R' if 'republican' in party.lower() else 'I')
        state      = last_term.get('state', '')
        by_last[last_word].append((bioguide, first, last, party_code, state))
    return by_last


def match_senator(last_raw, first_raw, state_abbrev, by_last):
    """Match last name + first name + 2-letter state to a bioguide entry."""
    norm_last = normalize(last_raw.strip())
    last_word = norm_last.split()[-1] if norm_last else norm_last
    candidates = by_last.get(last_word, [])
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Disambiguate by state
    if state_abbrev:
        for entry in candidates:
            if entry[4] == state_abbrev.upper():
                return entry
    # Disambiguate by first name prefix
    if first_raw:
        norm_first = normalize(first_raw.strip())
        for entry in candidates:
            if norm_first and entry[1].startswith(norm_first[:3]):
                return entry
    # Full last name match
    for entry in candidates:
        if entry[2] == norm_last:
            return entry
    return None


# ─────────────────────────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────────────────────────

def scrape_senate_gov():
    """
    Fetch and parse the senate.gov committee assignments page.

    Page structure (discovered by inspection):
      <div>
        <a name="BaldwinWI">&nbsp;</a>
        <a href="...">Baldwin, Tammy</a> (D-WI)
      </div>
      <div>
        <ul>
          <li>
            <strong><a href="/general/.../committee_memberships_SSAP.htm">Committee on Appropriations</a></strong>
            <ul>  ... subcommittee lis ...  </ul>
          </li>
          ...
        </ul>
      </div>

    The thomas_id is embedded in the committee href URL, so no name-mapping dict needed.

    Returns list of dicts: {bioguide, thomas_id, party, title}
    """
    print(f"  Fetching {URL} ...")
    resp = requests.get(URL, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')

    by_last     = load_senator_lookup()
    memberships = []
    seen        = set()   # (bioguide, thomas_id)
    unmatched   = []

    # Collect all senator anchor IDs from the dropdown (handles spaces/hyphens/accents)
    # Dropdown options look like: value="...assignments.htm#BaldwinWI"
    senator_anchors = set()
    for opt in soup.find_all('option'):
        val = opt.get('value', '')
        if 'assignments.htm#' in val:
            anchor_id = val.split('assignments.htm#', 1)[1]
            senator_anchors.add(anchor_id)

    print(f"  Found {len(senator_anchors)} senators in dropdown")

    # Find each senator's <a name="..."> anchor in the page
    all_named_anchors = {a.get('name', ''): a for a in soup.find_all('a', attrs={'name': True})}

    for anchor_id in senator_anchors:
        anchor = all_named_anchors.get(anchor_id)
        if not anchor:
            continue
        senator_div = anchor.parent

        # Extract party/state from div text
        div_text = senator_div.get_text()
        ps_match = _PARTY_STATE.search(div_text)
        if not ps_match:
            continue
        state_abbrev = ps_match.group(2)

        # Extract senator name — the <a href> link text within the div
        name_link = senator_div.find('a', href=True)
        if not name_link:
            continue
        name_text = name_link.get_text(strip=True)   # "Baldwin, Tammy"

        # Parse "Lastname, Firstname" (senate.gov standard)
        if ',' in name_text:
            last_raw, first_raw = name_text.split(',', 1)
        else:
            parts     = name_text.split()
            last_raw  = parts[-1] if parts else name_text
            first_raw = ' '.join(parts[:-1])

        entry = match_senator(last_raw.strip(), first_raw.strip(), state_abbrev, by_last)
        if not entry:
            unmatched.append(name_text)
            continue

        bioguide = entry[0]

        # Navigate to the committee list div (next sibling div)
        committee_div = senator_div.find_next_sibling('div')
        if not committee_div:
            continue
        ul = committee_div.find('ul')
        if not ul:
            continue

        # Each top-level <li> is a full committee
        for li in ul.find_all('li', recursive=False):
            strong = li.find('strong')
            if not strong:
                continue
            a_tag = strong.find('a', href=True)
            if not a_tag:
                continue

            # Extract thomas_id directly from URL
            href = a_tag.get('href', '')
            m = _THOMAS_FROM_URL.search(href)
            if not m:
                continue
            thomas_id = m.group(1).upper()

            # Skip subcommittee codes (contain digits)
            if re.search(r'\d', thomas_id):
                continue

            key = (bioguide, thomas_id)
            if key not in seen:
                seen.add(key)
                memberships.append({
                    'bioguide':  bioguide,
                    'thomas_id': thomas_id,
                    'party':     entry[3],
                    'title':     'Member',
                })

    if unmatched:
        print(f"\n  Unmatched senators ({len(set(unmatched))}):")
        for n in sorted(set(unmatched)):
            print(f"    - {n}")

    return memberships


# ─────────────────────────────────────────────────────────────────
# DB WRITER
# ─────────────────────────────────────────────────────────────────

def load_to_db(memberships):
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM committee_memberships WHERE source = ?", (SOURCE_NAME,)
    )
    deleted = cursor.rowcount
    if deleted:
        print(f"  Removed {deleted} stale {SOURCE_NAME} records")

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
            START_DATE,
            None,
            SOURCE_NAME,
            'high',
        ))

    conn.commit()

    cursor.execute("""
        SELECT thomas_id, COUNT(*) as cnt
        FROM committee_memberships
        WHERE source = ?
        GROUP BY thomas_id
        ORDER BY thomas_id
    """, (SOURCE_NAME,))
    rows = cursor.fetchall()

    print(f"\n  Senate assignments by committee ({SOURCE_NAME}):")
    total = 0
    for thomas_id, cnt in rows:
        print(f"    {thomas_id:<8} {cnt:>3}")
        total += cnt
    print(f"    {'TOTAL':<8} {total:>3}")

    conn.close()
    return len(memberships)


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  Senate Assignments Live Scraper")
    print(f"  Source: {URL}")
    print("=" * 60)

    print("\n  Scraping senate.gov ...")
    memberships = scrape_senate_gov()
    print(f"\n  Parsed {len(memberships)} senator-committee assignments")

    if memberships:
        print("\n  Loading to DB...")
        inserted = load_to_db(memberships)
        print(f"\n  Done — {inserted} records (source='{SOURCE_NAME}', confidence='high')")
        print("\n  NOTE: Re-run pipeline/committee_loader.py to update committee_relevance flags.")
    else:
        print("\n  WARNING: No memberships parsed — check page structure or network.")
