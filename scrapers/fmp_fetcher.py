# scrapers/fmp_fetcher.py
# Fetches House + Senate trading disclosures from the FMP API
# Paginate both endpoints back to CUTOFF_DATE (~5 years), fuzzy-match
# politician names to existing politicians table, insert new trades.
#
# Usage (from project root):
#   python scrapers/fmp_fetcher.py            # full run (both chambers)
#   python scrapers/fmp_fetcher.py --test     # fetch 2 pages each, print sample
#   python scrapers/fmp_fetcher.py --house    # house only
#   python scrapers/fmp_fetcher.py --senate   # senate only
#
# After this runs, kick off the normal pipeline:
#   python runner/daily_runner.py --skip-scrape

import requests
import sqlite3
import time
import re
import json
import argparse
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
import xml.etree.ElementTree as ET

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

API_KEY      = "gevbyG5hFgMausKgFCpKWhLIeBEecrhg"
DB_PATH      = "data/trades.db"
CUTOFF_DATE  = "2018-01-01"          # don't fetch older than this
REQUEST_DELAY = 0.25                 # seconds between API calls (300/min limit)
BATCH_SIZE   = 100                   # commit every N trades

HOUSE_URL  = "https://financialmodelingprep.com/stable/house-latest"
SENATE_URL = "https://financialmodelingprep.com/stable/senate-latest"
# Note: house-latest only goes back ~2 years (FMP limitation on house disclosures)
# senate-latest goes back to ~2018 across ~124 pages

HOUSE_BY_NAME_URL  = "https://financialmodelingprep.com/stable/house-trades-by-name"
SENATE_BY_NAME_URL = "https://financialmodelingprep.com/stable/senate-trades-by-name"

# MemberData XML files — Clerk snapshots for each congress
# Used to build a comprehensive member list for the congress-xml backfill
CONGRESS_XML_DIR = Path("data/committees")

FUZZY_THRESHOLD = 0.82               # minimum name similarity to auto-match

# ─────────────────────────────────────────────
# SIZE PARSER
# FMP uses strings like "$1,001 - $15,000" or "Over $5,000,000"
# ─────────────────────────────────────────────

def parse_fmp_amount(amount_str):
    """
    Parse FMP dollar range string into (size_min, size_max, size_midpoint).
    Returns (None, None, None) if unparseable.
    """
    if not amount_str:
        return None, None, None

    # Strip $, commas, spaces
    clean = amount_str.replace("$", "").replace(",", "").strip().lower()

    # "over X" → use X as both bounds
    if clean.startswith("over"):
        nums = re.findall(r"[\d]+", clean)
        if nums:
            n = int(nums[0])
            return n, n, n
        return None, None, None

    # "X - Y" or "X to Y"
    parts = re.split(r"[-–to]+", clean)
    nums  = []
    for p in parts:
        digits = re.findall(r"[\d]+", p)
        if digits:
            nums.append(int("".join(digits)))

    if len(nums) >= 2:
        lo, hi = nums[0], nums[1]
        return lo, hi, (lo + hi) // 2
    elif len(nums) == 1:
        n = nums[0]
        return n, n, n

    return None, None, None

# ─────────────────────────────────────────────
# TRANSACTION TYPE NORMALISER
# ─────────────────────────────────────────────

def normalise_tx_type(raw):
    if not raw:
        return raw
    r = raw.lower().strip()
    if "purchase" in r or "buy" in r:
        return "buy"
    if "sale" in r or "sell" in r:
        return "sell"
    if "exchange" in r:
        return "exchange"
    if "option" in r:
        return "option"
    if "receive" in r:
        return "receive"  # flagged for rejection at insert
    return r

# ─────────────────────────────────────────────
# NAME FUZZY MATCHER
# ─────────────────────────────────────────────

# Common nickname → formal first name mappings
NICKNAME_MAP = {
    "bill": "william", "will": "william",
    "bob": "robert",   "rob": "robert",
    "rick": "richard", "dick": "richard", "rich": "richard",
    "chuck": "charles", "charlie": "charles",
    "mike": "michael", "mick": "michael",
    "jim": "james",    "jimmy": "james",
    "joe": "joseph",   "jo": "joseph",
    "tom": "thomas",   "tommy": "thomas",
    "dave": "david",   "davy": "david",
    "dan": "daniel",   "danny": "daniel",
    "pat": "patricia", "tricia": "patricia",
    "kate": "katherine", "kathy": "katherine", "cathy": "katherine",
    "liz": "elizabeth", "beth": "elizabeth", "betty": "elizabeth",
    "sue": "susan",    "susie": "susan",
    "greg": "gregory",
    "andy": "andrew",  "drew": "andrew",
    "ted": "edward",   "ed": "edward",
    "al": "albert",    "bert": "albert",
    "sam": "samuel",
    "ben": "benjamin",
    "ken": "kenneth",
    "don": "donald",
    "ron": "ronald",
    "len": "leonard",
    "tim": "timothy",
    "nick": "nicholas",
    "mark": "marcus",
    "tony": "anthony",
    "vince": "vincent",
    "frank": "francis",
    "gene": "eugene",
    "jerry": "gerald", "gerry": "gerald",
    "larry": "lawrence",
    "matt": "matthew",
    "steve": "stephen", "steven": "stephen",
    "jeff": "jeffrey",
    "brad": "bradley",
    "greg": "gregory",
}

def _normalise_first(first):
    """Expand nickname to formal first name for matching."""
    f = first.lower().strip()
    return NICKNAME_MAP.get(f, f)

def _last_name(full_name):
    """Extract last name (last token, ignoring suffixes like Jr/Sr/III)."""
    parts = full_name.lower().strip().split()
    suffixes = {"jr", "sr", "ii", "iii", "iv", "jr.", "sr."}
    while parts and parts[-1] in suffixes:
        parts.pop()
    return parts[-1] if parts else ""

def _first_name(full_name):
    """Extract first name (first token)."""
    parts = full_name.lower().strip().split()
    return parts[0] if parts else ""

def name_similarity(a, b):
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

def _name_match_score(fmp_name, db_name):
    """
    Multi-strategy name match score.
    Handles: exact, fuzzy full, last+first with nickname expansion,
    and partial middle-name presence.
    Returns float 0–1.
    """
    fmp_l = fmp_name.lower().strip()
    db_l  = db_name.lower().strip()

    # 1. Exact
    if fmp_l == db_l:
        return 1.0

    # 2. Full fuzzy
    full_score = SequenceMatcher(None, fmp_l, db_l).ratio()

    # 3. Last name match + normalised first name match
    fmp_last  = _last_name(fmp_name)
    db_last   = _last_name(db_name)
    fmp_first = _normalise_first(_first_name(fmp_name))
    db_first  = _normalise_first(_first_name(db_name))

    last_score  = SequenceMatcher(None, fmp_last,  db_last).ratio()
    first_score = SequenceMatcher(None, fmp_first, db_first).ratio()

    # Weight: last name is more distinctive
    component_score = last_score * 0.6 + first_score * 0.4

    return max(full_score, component_score)

def build_politician_lookup(conn):
    """Returns dict: {name_lower: (politician_id, name, party, chamber, state)}"""
    c = conn.cursor()
    c.execute("SELECT politician_id, name, party, chamber, state FROM politicians")
    lookup = {}
    for row in c.fetchall():
        lookup[row[1].lower().strip()] = row
    return lookup

def find_politician(name, chamber, lookup):
    """
    Match FMP name + chamber to existing politicians.
    Returns (politician_id, name, party, chamber, state) or None.
    """
    if not name:
        return None

    name_norm = name.lower().strip()

    # Exact match first
    if name_norm in lookup:
        return lookup[name_norm]

    # Scored match
    best_score = 0
    best_match = None

    for db_name, row in lookup.items():
        score = _name_match_score(name, db_name)
        # Slight boost for same chamber
        if chamber and row[3] and chamber.lower() == row[3].lower():
            score = min(score + 0.04, 1.0)
        if score > best_score:
            best_score = score
            best_match = row

    if best_score >= FUZZY_THRESHOLD:
        return best_match

    return None

def make_fmp_politician_id(name, chamber):
    """Generate a synthetic politician_id for unmatched FMP politicians."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-")
    prefix = "FH" if chamber and "house" in chamber.lower() else "FS"
    return f"{prefix}_{slug}"

# ─────────────────────────────────────────────
# API CALLER
# ─────────────────────────────────────────────

def fetch_page(url, page, session):
    """Fetch one page from the FMP trading endpoint. Returns list of records or []."""
    # FMP stable API: apikey must come after other params in query string
    full_url = f"{url}?page={page}&apikey={API_KEY}"
    try:
        resp = session.get(full_url, timeout=30)
        if resp.status_code == 400:
            return []   # past the last page
        resp.raise_for_status()
        if not resp.text.strip():
            return []
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "results", "trades"):
                if key in data and isinstance(data[key], list):
                    return data[key]
        return []
    except Exception as e:
        print(f"    API error (page {page}): {e}")
        return []

# ─────────────────────────────────────────────
# RECORD PARSER
# ─────────────────────────────────────────────

def _parse_record(rec, chamber):
    """
    Parse a single FMP record (house or senate) into our standard trade dict.
    Both endpoints share the same field names:
      symbol, disclosureDate, transactionDate, firstName, lastName,
      district, owner, assetDescription, assetType, type, amount
    Returns None if essential fields are missing.
    """
    first = (rec.get("firstName") or "").strip()
    last  = (rec.get("lastName")  or "").strip()
    name  = f"{first} {last}".strip()
    if not name:
        return None

    ticker = (rec.get("symbol") or "").strip().upper()
    disc_date  = _norm_date(rec.get("disclosureDate")  or "")
    trade_date = _norm_date(rec.get("transactionDate") or "")

    if not disc_date:
        return None

    tx_type = normalise_tx_type(rec.get("type") or "")

    s_min, s_max, s_mid = parse_fmp_amount(rec.get("amount") or "")

    owner   = (rec.get("owner") or "self").lower().strip()
    company = (rec.get("assetDescription") or "").strip()

    # district field: "PA" (senate) or "OH08" (house) — first 2 chars = state
    district = (rec.get("district") or "").strip()
    state    = district[:2].upper() if district else ""

    filing_lag = None
    if trade_date and disc_date:
        try:
            td = datetime.strptime(trade_date, "%Y-%m-%d")
            dd = datetime.strptime(disc_date,  "%Y-%m-%d")
            filing_lag = (dd - td).days
        except Exception:
            pass

    return {
        "name":             name,
        "chamber":          chamber,
        "party":            "",       # FMP doesn't include party in these endpoints
        "state":            state,
        "ticker":           ticker,
        "company":          company,
        "trade_date":       trade_date,
        "disclosure_date":  disc_date,
        "filing_lag_days":  filing_lag,
        "transaction_type": tx_type,
        "size_min":         s_min,
        "size_max":         s_max,
        "size_midpoint":    s_mid,
        "owner":            owner,
    }

def parse_house_record(rec):
    return _parse_record(rec, "House")

def parse_senate_record(rec):
    return _parse_record(rec, "Senate")

def _norm_date(raw):
    """Convert various date formats to YYYY-MM-DD, or return ''."""
    if not raw:
        return ""
    raw = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""

def _norm_party(raw):
    if not raw:
        return ""
    r = raw.strip().lower()
    if r.startswith("r") or r == "gop":
        return "Republican"
    if r.startswith("d"):
        return "Democrat"
    if r.startswith("i"):
        return "Independent"
    return raw.strip().title()

# ─────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────

def upsert_politician(cursor, pol_id, name, party, chamber, state):
    cursor.execute("""
        INSERT OR IGNORE INTO politicians (politician_id, name, party, chamber, state)
        VALUES (?, ?, ?, ?, ?)
    """, (pol_id, name, party, chamber, state))

def trade_exists_by_attrs(cursor, politician_id, ticker, trade_date, tx_type):
    """Check if a matching trade already exists (catches Capitol Trades duplicates)."""
    cursor.execute("""
        SELECT 1 FROM trades
        WHERE politician_id = ?
        AND ticker = ?
        AND trade_date = ?
        AND transaction_type = ?
        LIMIT 1
    """, (politician_id, ticker, trade_date, tx_type))
    return cursor.fetchone() is not None

def insert_trade(cursor, trade_id, politician_id, trade):
    """Insert a trade. Returns True if inserted, False if rejected or duplicate."""
    if not trade.get("ticker") or not trade["ticker"].strip():
        return False  # reject blank-ticker records — can't be priced or scored
    if trade.get("transaction_type") == "receive":
        return False  # passive receipt (spin-off, distribution) — not a trading decision

    lag = trade["filing_lag_days"]
    violation = "violation" if (lag is not None and lag > 45) else "compliant"

    cursor.execute("""
        INSERT OR IGNORE INTO trades (
            trade_id, politician_id, ticker, company,
            trade_date, disclosure_date, filing_lag_days,
            transaction_type, size_min, size_max, size_midpoint,
            owner, filing_violation
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade_id,
        politician_id,
        trade["ticker"],
        trade["company"],
        trade["trade_date"],
        trade["disclosure_date"],
        lag,
        trade["transaction_type"],
        trade["size_min"],
        trade["size_max"],
        trade["size_midpoint"],
        trade["owner"],
        violation,
    ))
    return cursor.rowcount > 0

# ─────────────────────────────────────────────
# CHAMBER FETCHER
# ─────────────────────────────────────────────

def fetch_chamber(url, chamber_label, parse_fn, conn, test_mode=False):
    """
    Paginate through one chamber's FMP trading endpoint.
    Returns (inserted, skipped_dup, skipped_old, unmatched_names).
    """
    cursor  = conn.cursor()
    lookup  = build_politician_lookup(conn)
    session = requests.Session()

    inserted          = 0
    skipped_dup       = 0
    skipped_old       = 0
    skipped_no_ticker = 0
    unmatched         = {}   # name → count (for review)
    new_politicians   = 0

    max_pages = 2 if test_mode else 9999
    page      = 0

    print(f"\n{'='*55}")
    print(f"  {chamber_label} Trading — FMP")
    print(f"{'='*55}")
    if test_mode:
        print("  (TEST MODE — 2 pages only)\n")

    while page < max_pages:
        records = fetch_page(url, page, session)
        time.sleep(REQUEST_DELAY)

        if not records:
            print(f"  Page {page}: empty response — done.")
            break

        # Check if we've gone past the cutoff (records are newest-first)
        last_disc = ""
        for rec in records:
            d = _norm_date(
                rec.get("disclosureDate") or rec.get("disclosure_date") or ""
            )
            if d:
                last_disc = d
                break  # just need the first (newest) on this page for tracking

        # Parse + insert
        page_inserted = 0
        oldest_on_page = "9999-99-99"

        for rec in records:
            trade = parse_fn(rec)
            if trade is None:
                continue

            disc       = trade["disclosure_date"]
            trade_date = trade["trade_date"]

            # Skip trades with no disclosure date or trade date — unpriceable,
            # unscorable, and NULLs weaken the UNIQUE index.
            if not disc or not trade_date:
                continue

            if disc < oldest_on_page:
                oldest_on_page = disc

            # Stop if older than cutoff
            if disc < CUTOFF_DATE:
                skipped_old += 1
                continue

            # Politician matching
            match = find_politician(trade["name"], trade["chamber"], lookup)
            if match:
                pol_id, pol_name, pol_party, pol_chamber, pol_state = match
                # Update party/state from FMP if missing in DB
                if not pol_party and trade["party"]:
                    cursor.execute(
                        "UPDATE politicians SET party=? WHERE politician_id=?",
                        (trade["party"], pol_id)
                    )
            else:
                # New politician — create synthetic ID
                pol_id = make_fmp_politician_id(trade["name"], trade["chamber"])
                pol_name  = trade["name"]
                pol_party = trade["party"]
                pol_state = trade["state"]
                upsert_politician(cursor, pol_id, pol_name, pol_party,
                                  trade["chamber"], pol_state)
                # Add to lookup so subsequent trades from same person match
                lookup[pol_name.lower().strip()] = (
                    pol_id, pol_name, pol_party, trade["chamber"], pol_state
                )
                unmatched[trade["name"]] = unmatched.get(trade["name"], 0) + 1
                new_politicians += 1

            # Skip if duplicate
            if trade_exists_by_attrs(cursor, pol_id, trade["ticker"],
                                     trade["trade_date"], trade["transaction_type"]):
                skipped_dup += 1
                continue

            # Build trade_id
            ticker_slug = trade["ticker"] or "NOTICKER"
            date_slug   = (trade["trade_date"] or trade["disclosure_date"]).replace("-", "")
            prefix      = "fmph" if "house" in trade["chamber"].lower() else "fmps"
            trade_id    = f"{prefix}_{pol_id}_{ticker_slug}_{date_slug}"

            # De-collision: if trade_id already exists (same pol/ticker/date, diff tx type),
            # append tx type initial
            trade_id = f"{trade_id}_{trade['transaction_type'][0]}"

            if insert_trade(cursor, trade_id, pol_id, trade):
                inserted      += 1
                page_inserted += 1
            else:
                skipped_no_ticker += 1

        # Commit every batch
        if (inserted % BATCH_SIZE) < page_inserted or page_inserted > 0:
            conn.commit()

        print(f"  Page {page:4d} | Inserted: {page_inserted:3d} | "
              f"Total: {inserted:,} | Oldest disc: {oldest_on_page}")

        # Stop if all trades on this page are before cutoff
        if oldest_on_page != "9999-99-99" and oldest_on_page < CUTOFF_DATE:
            print(f"  Reached cutoff ({CUTOFF_DATE}) — stopping.")
            break

        page += 1

    conn.commit()
    session.close()

    print(f"\n  {chamber_label} complete:")
    print(f"    Inserted:            {inserted:,}")
    print(f"    Skipped (dup):       {skipped_dup:,}")
    print(f"    Skipped (old):       {skipped_old:,}")
    print(f"    Skipped (no ticker): {skipped_no_ticker:,}")
    print(f"    New politicians:     {new_politicians}")

    if unmatched:
        print(f"\n  Unmatched names ({len(unmatched)}) — review manually:")
        for name, count in sorted(unmatched.items(), key=lambda x: -x[1])[:20]:
            print(f"    {count:3d}x  {name}")

    return inserted, skipped_dup, skipped_old, unmatched

# ─────────────────────────────────────────────
# BY-NAME BACKFILL
# Used after house-latest to recover historical data per-politician
# ─────────────────────────────────────────────

def fetch_page_by_name(url, name, page, session):
    """Fetch one page from a by-name endpoint. Returns list of records or []."""
    import urllib.parse
    encoded_name = urllib.parse.quote(name)
    full_url = f"{url}?name={encoded_name}&page={page}&apikey={API_KEY}"
    try:
        resp = session.get(full_url, timeout=30)
        if resp.status_code in (400, 404):
            return []
        resp.raise_for_status()
        if not resp.text.strip():
            return []
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "results", "trades"):
                if key in data and isinstance(data[key], list):
                    return data[key]
        return []
    except Exception as e:
        print(f"    API error ({name}, page {page}): {e}")
        return []


def backfill_by_name(by_name_url, chamber_label, parse_fn, conn, test_mode=False):
    """
    Loop through all known politicians of a chamber in our DB, fetch each by name.
    This recovers historical data beyond what the -latest endpoint covers.
    Returns (inserted, skipped_dup, skipped_old, unmatched_names).
    """
    cursor  = conn.cursor()
    lookup  = build_politician_lookup(conn)
    session = requests.Session()

    # Get all politicians of this chamber from DB
    chamber_key = "House" if "house" in by_name_url.lower() else "Senate"
    cursor.execute(
        "SELECT politician_id, name FROM politicians WHERE chamber = ?", (chamber_key,)
    )
    politicians = cursor.fetchall()

    if test_mode:
        politicians = politicians[:3]   # just test a handful

    inserted          = 0
    skipped_dup       = 0
    skipped_old       = 0
    skipped_no_ticker = 0
    new_politicians   = 0
    unmatched         = {}

    print(f"\n{'='*55}")
    print(f"  {chamber_label} By-Name Backfill — FMP")
    print(f"  Politicians to query: {len(politicians)}")
    print(f"{'='*55}")
    if test_mode:
        print("  (TEST MODE — 3 politicians only)\n")

    for pol_idx, (pol_id, pol_name) in enumerate(politicians, 1):
        # by-name endpoint returns ALL records in one call (page param ignored)
        records = fetch_page_by_name(by_name_url, pol_name, 0, session)
        time.sleep(REQUEST_DELAY)

        pol_inserted  = 0
        pol_old       = 0
        earliest_seen = "9999-99-99"

        for rec in records:
            trade = parse_fn(rec)
            if trade is None:
                continue

            disc       = trade["disclosure_date"]
            trade_date = trade["trade_date"]
            if not disc or not trade_date:
                continue

            if disc < earliest_seen:
                earliest_seen = disc

            if disc < CUTOFF_DATE:
                skipped_old += 1
                pol_old += 1
                continue

            match = find_politician(trade["name"], trade["chamber"], lookup)
            matched_pol_id = match[0] if match else pol_id

            if trade_exists_by_attrs(cursor, matched_pol_id, trade["ticker"],
                                     trade["trade_date"], trade["transaction_type"]):
                skipped_dup += 1
                continue

            ticker_slug = trade["ticker"] or "NOTICKER"
            date_slug   = (trade["trade_date"] or disc).replace("-", "")
            prefix      = "fmph" if "house" in trade["chamber"].lower() else "fmps"
            trade_id    = f"{prefix}_{matched_pol_id}_{ticker_slug}_{date_slug}_{trade['transaction_type'][0]}"

            if insert_trade(cursor, trade_id, matched_pol_id, trade):
                inserted      += 1
                pol_inserted  += 1
            else:
                skipped_no_ticker += 1

        conn.commit()

        status = f"+{pol_inserted}" if pol_inserted else "  dup"
        print(f"  [{pol_idx:3d}/{len(politicians)}] {pol_name:<35} {status}  (earliest: {earliest_seen})")

    session.close()

    print(f"\n  {chamber_label} by-name backfill complete:")
    print(f"    Inserted:            {inserted:,}")
    print(f"    Skipped (dup):       {skipped_dup:,}")
    print(f"    Skipped (old):       {skipped_old:,}")
    print(f"    Skipped (no ticker): {skipped_no_ticker:,}")

    return inserted, skipped_dup, skipped_old, unmatched


# ─────────────────────────────────────────────
# CONGRESS XML — COMPREHENSIVE MEMBER LIST
# Parses MemberData_NNN.xml to find house members not yet in our DB
# ─────────────────────────────────────────────

def parse_congress_xml(xml_path):
    """
    Parse a MemberData XML file (Clerk snapshots).
    Returns list of dicts: {bioguide, firstname, lastname, official_name, party, state}
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()
    members = root.findall('.//member')
    results = []
    for m in members:
        statedistrict = (m.findtext('statedistrict') or "").strip()
        state = statedistrict[:2].upper() if len(statedistrict) >= 2 else ""

        info = m.find('member-info')
        if info is None:
            continue

        bioguide     = (info.findtext('bioguideID')    or "").strip()
        firstname    = (info.findtext('firstname')     or "").strip()
        lastname     = (info.findtext('lastname')      or "").strip()
        official     = (info.findtext('official-name') or "").strip()
        party_raw    = (info.findtext('party')         or "").strip()

        if not bioguide or not lastname:
            continue

        # Normalise party
        party = {"R": "Republican", "D": "Democrat", "I": "Independent"}.get(party_raw, party_raw)

        # Build full name: prefer official-name, fallback to firstname + lastname
        full_name = official if official else f"{firstname} {lastname}".strip()

        results.append({
            "bioguide":    bioguide,
            "firstname":   firstname,
            "lastname":    lastname,
            "full_name":   full_name,
            "party":       party,
            "state":       state,
        })
    return results


def get_missing_house_members(conn, congresses=("116", "117", "118", "119")):
    """
    Returns list of {bioguide, full_name, party, state} for house members in the
    Congress XML files who are NOT yet in our politicians table.
    """
    # All bioguide IDs currently in DB
    existing_ids = set(
        row[0] for row in conn.execute("SELECT politician_id FROM politicians").fetchall()
    )

    seen = set()
    missing = []

    for congress in congresses:
        xml_path = CONGRESS_XML_DIR / f"MemberData_{congress}.xml"
        if not xml_path.exists():
            print(f"  XML not found: {xml_path}")
            continue

        members = parse_congress_xml(xml_path)
        for m in members:
            bg = m["bioguide"]
            if bg in existing_ids or bg in seen:
                continue
            seen.add(bg)
            missing.append(m)

    return missing


def backfill_congress_members(conn, test_mode=False):
    """
    For every house member in our Congress XML files not yet in our DB:
    1. Query FMP house-trades-by-name for their trades
    2. If trades found, insert the politician + their trades
    Skips politicians who have no FMP data (no point adding empty records).
    """
    session = requests.Session()
    lookup  = build_politician_lookup(conn)
    cursor  = conn.cursor()

    missing = get_missing_house_members(conn)

    if test_mode:
        missing = missing[:5]

    print(f"\n{'='*55}")
    print(f"  Congress XML Backfill — House members not in DB")
    print(f"  Missing members to check: {len(missing)}")
    print(f"{'='*55}")
    if test_mode:
        print("  (TEST MODE — 5 members only)\n")

    added_politicians = 0
    inserted_trades   = 0
    skipped_no_ticker = 0
    no_fmp_data       = 0

    for idx, m in enumerate(missing, 1):
        pol_id = m["bioguide"]

        # FMP uses simple "First Last" (no middle initials).
        # Try simple name first; fall back to official-name if FMP returns nothing.
        simple_name   = f"{m['firstname']} {m['lastname']}".strip()
        official_name = m["full_name"]

        query_name = simple_name
        store_name = simple_name  # stored in DB; consistent with FMP style

        # by-name endpoint returns ALL records in one call — try simple name first,
        # fall back to official name (with middle initial) if empty
        records = fetch_page_by_name(HOUSE_BY_NAME_URL, simple_name, 0, session)
        time.sleep(REQUEST_DELAY)
        if not records and simple_name != official_name:
            records = fetch_page_by_name(HOUSE_BY_NAME_URL, official_name, 0, session)
            time.sleep(REQUEST_DELAY)
            if records:
                store_name = official_name

        pol_trades   = 0
        earliest     = "9999-99-99"
        pol_inserted = False

        for rec in records:
            trade = parse_house_record(rec)
            if trade is None:
                continue

            disc       = trade["disclosure_date"]
            trade_date = trade["trade_date"]
            if not disc or not trade_date:
                continue

            if disc < earliest:
                earliest = disc
            if disc < CUTOFF_DATE:
                continue

            # Lazily insert politician on first in-window trade found
            if not pol_inserted:
                upsert_politician(cursor, pol_id, store_name, m["party"], "House", m["state"])
                lookup[store_name.lower().strip()] = (pol_id, store_name, m["party"], "House", m["state"])
                pol_inserted = True
                added_politicians += 1

            if trade_exists_by_attrs(cursor, pol_id, trade["ticker"],
                                     trade["trade_date"], trade["transaction_type"]):
                continue

            ticker_slug = trade["ticker"] or "NOTICKER"
            date_slug   = (trade["trade_date"] or disc).replace("-", "")
            trade_id    = f"fmph_{pol_id}_{ticker_slug}_{date_slug}_{trade['transaction_type'][0]}"

            if insert_trade(cursor, trade_id, pol_id, trade):
                inserted_trades   += 1
                pol_trades        += 1
            else:
                skipped_no_ticker += 1

        conn.commit()

        if pol_trades == 0:
            no_fmp_data += 1
            status = "  no FMP data"
        else:
            status = f"+{pol_trades} trades  (earliest: {earliest})"

        print(f"  [{idx:3d}/{len(missing)}] {store_name:<40} {status}")

    session.close()
    conn.commit()

    print(f"\n  Congress XML backfill complete:")
    print(f"    New politicians added:    {added_politicians}")
    print(f"    Trades inserted:          {inserted_trades:,}")
    print(f"    Skipped (no ticker):      {skipped_no_ticker:,}")
    print(f"    No FMP data found:        {no_fmp_data}")

    return added_politicians, inserted_trades


# ─────────────────────────────────────────────
# TEST / INSPECT
# ─────────────────────────────────────────────

def test_connection():
    """Fetch 1 page from each endpoint, print raw field names and a sample record."""
    session = requests.Session()
    for label, url in [("HOUSE", HOUSE_URL), ("SENATE", SENATE_URL)]:
        print(f"\n{'='*55}")
        print(f"  {label} — raw sample (page 0, first record)")
        print(f"{'='*55}")
        records = fetch_page(url, 0, session)
        if records:
            print(f"  Total on page: {len(records)}")
            print(f"  Fields: {list(records[0].keys())}")
            print(f"  Sample record:")
            print(json.dumps(records[0], indent=4))
        else:
            print("  No records returned — check API key or endpoint.")
        time.sleep(REQUEST_DELAY)
    session.close()

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def run_fmp_fetcher(house=True, senate=True, test_mode=False,
                    backfill_house=False, backfill_senate=False,
                    backfill_congress=False, db_path=None):
    db = db_path if db_path else DB_PATH
    conn = sqlite3.connect(db)

    try:
        total_before = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        print(f"\nTrades in DB before: {total_before:,}")
        print(f"Cutoff date:         {CUTOFF_DATE}")
        if test_mode:
            print("Mode:                TEST (2 pages per chamber / 3 politicians)")

        total_inserted = 0

        # ── Latest-endpoint pass ──────────────────
        if house:
            ins, _, _, _ = fetch_chamber(
                HOUSE_URL, "HOUSE", parse_house_record, conn, test_mode
            )
            total_inserted += ins

        if senate:
            ins, _, _, _ = fetch_chamber(
                SENATE_URL, "SENATE", parse_senate_record, conn, test_mode
            )
            total_inserted += ins

        # ── By-name backfill pass ─────────────────
        if backfill_house:
            ins, _, _, _ = backfill_by_name(
                HOUSE_BY_NAME_URL, "HOUSE", parse_house_record, conn, test_mode
            )
            total_inserted += ins

        if backfill_senate:
            ins, _, _, _ = backfill_by_name(
                SENATE_BY_NAME_URL, "SENATE", parse_senate_record, conn, test_mode
            )
            total_inserted += ins

        # ── Former members from Congress XML snapshots ─
        if backfill_congress:
            _, ins = backfill_congress_members(conn, test_mode)
            total_inserted += ins

        total_after = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

        print(f"\n{'='*55}")
        print(f"  FMP fetch complete!")
        print(f"  Total inserted:  {total_inserted:,}")
        print(f"  Trades before:   {total_before:,}")
        print(f"  Trades after:    {total_after:,}")
        print(f"{'='*55}")
        print("\nNext step: python runner/daily_runner.py --skip-scrape")

    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="FMP Congressional Trading Fetcher"
    )
    parser.add_argument("--test",           action="store_true",
                        help="Fetch 2 pages each / 3 politicians (test mode)")
    parser.add_argument("--inspect",        action="store_true",
                        help="Print raw API response (1 page each)")
    parser.add_argument("--house",          action="store_true", help="House latest only")
    parser.add_argument("--senate",         action="store_true", help="Senate latest only")
    parser.add_argument("--backfill-house", action="store_true",
                        help="Backfill house historical data via house-trades-by-name")
    parser.add_argument("--backfill-senate",  action="store_true",
                        help="Backfill senate historical data via senate-trades-by-name")
    parser.add_argument("--backfill-congress", action="store_true",
                        help="Query FMP for house members in XML snapshots not yet in DB (former members)")
    parser.add_argument("--db", default=None,
                        help="DB path override (default: data/trades.db). Use data/fmp_staging.db for staging.")
    args = parser.parse_args()

    if args.inspect:
        test_connection()
    else:
        explicit = (args.house or args.senate or args.backfill_house
                    or args.backfill_senate or args.backfill_congress)
        do_house            = args.house            or not explicit
        do_senate           = args.senate           or not explicit
        do_backfill_house   = args.backfill_house
        do_backfill_senate  = args.backfill_senate
        do_backfill_congress = args.backfill_congress

        run_fmp_fetcher(
            house=do_house,
            senate=do_senate,
            test_mode=args.test,
            backfill_house=do_backfill_house,
            backfill_senate=do_backfill_senate,
            backfill_congress=do_backfill_congress,
            db_path=args.db,
        )
