# scrapers/capitol_trades.py
# Scrapes Capitol Trades using Selenium with direct URL pagination

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from bs4 import BeautifulSoup
import sqlite3
import time
import re
from datetime import datetime, timedelta

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

DB_PATH    = "data/trades.db"
BASE_URL   = "https://www.capitoltrades.com/trades?page={}"
PAGE_DELAY = 4
NEXT_DELAY = 2

# ─────────────────────────────────────────────
# SIZE RANGE PARSER
# ─────────────────────────────────────────────
# Capitol Trades uses uppercase with en-dash e.g. "1K–15K"
# Normalise to lowercase with hyphen for matching

SIZE_MAP = {
    "1k-15k":    (1_000,       15_000),
    "15k-50k":   (15_000,      50_000),
    "50k-100k":  (50_000,     100_000),
    "100k-250k": (100_000,    250_000),
    "250k-500k": (250_000,    500_000),
    "500k-1m":   (500_000,  1_000_000),
    "1m-5m":     (1_000_000, 5_000_000),
    "5m+":       (5_000_000, 5_000_000),
}

def parse_size(size_str):
    if not size_str:
        return None, None, None
    # Normalise: lowercase, remove $, replace en-dash/em-dash with hyphen
    n = size_str.lower().strip()
    n = n.replace("$", "").replace(",", "")
    n = n.replace("–", "-").replace("—", "-")  # en-dash, em-dash → hyphen
    n = re.sub(r'\s+', '', n)                   # remove spaces
    for key, (lo, hi) in SIZE_MAP.items():
        if key in n:
            return lo, hi, (lo + hi) // 2
    return None, None, None

# ─────────────────────────────────────────────
# DATE NORMALISER
# ─────────────────────────────────────────────

def normalise_date(raw):
    """
    Convert Capitol Trades date strings to YYYY-MM-DD.
    Handles: '9 Feb2026', '24 Feb 2026', 'Today', '14:01Today' etc.
    """
    if not raw:
        return ""
    raw = raw.strip()
    today = datetime.now().date()

    if "today" in raw.lower():
        return str(today)
    if "yesterday" in raw.lower():
        return str(today - timedelta(days=1))

    # Strip leading time e.g. "14:01Today" → "Today", "16:15  24 Feb 2026" → "24 Feb 2026"
    raw = re.sub(r'^\d{1,2}:\d{2}\s*', '', raw).strip()

    # Re-check after stripping time
    if "today" in raw.lower():
        return str(today)
    if "yesterday" in raw.lower():
        return str(today - timedelta(days=1))

    # Fix missing space between month and year: "Feb2026" → "Feb 2026"
    raw = re.sub(r'([A-Za-z]{3})(\d{4})', r'\1 \2', raw)

    # Fix missing space between day and month: "9Feb" → "9 Feb"
    raw = re.sub(r'(\d{1,2})([A-Za-z])', r'\1 \2', raw)

    raw = raw.strip()

    for fmt in ("%d %b %Y", "%b %d %Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    return ""  # return empty if unparseable rather than garbage

# ─────────────────────────────────────────────
# FILING LAG PARSER
# ─────────────────────────────────────────────

def parse_filing_lag(cell_text):
    """
    Cell 4 contains text like 'days25' or '25 days'.
    Extract the integer.
    """
    match = re.search(r'(\d+)', cell_text)
    return int(match.group(1)) if match else None

# ─────────────────────────────────────────────
# BROWSER SETUP
# ─────────────────────────────────────────────

def make_driver(headless=True):
    options = Options()
    if headless:
        options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--log-level=3")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    )
    return webdriver.Chrome(options=options)

# ─────────────────────────────────────────────
# PAGE PARSER
# Column order confirmed:
# 0: Politician  1: Traded Issuer  2: Published  3: Traded
# 4: Filed After  5: Owner  6: Type  7: Size  8: Price  9: Link
# ─────────────────────────────────────────────

def parse_trades_from_page(html):
    soup   = BeautifulSoup(html, "html.parser")
    trades = []

    table = soup.find("table")
    if not table:
        return trades

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 8:
            continue

        try:
            # ── Cell 0: Politician ───────────────
            pol_cell = cells[0]
            pol_link = pol_cell.find("a", href=re.compile(r'/politicians/'))
            pol_name = pol_link.get_text(strip=True) if pol_link else ""
            pol_href = pol_link.get("href", "") if pol_link else ""
            pol_id   = pol_href.split("/politicians/")[-1].strip("/") if "/politicians/" in pol_href else ""

            if not pol_id or not pol_name:
                continue

            party, chamber, state = "", "", ""
            for span in pol_cell.find_all("span"):
                txt = span.get_text(strip=True)
                if txt in ("Republican", "Democrat", "Independent"):
                    party = txt
                elif txt in ("House", "Senate"):
                    chamber = txt
                elif len(txt) == 2 and txt.isupper() and txt.isalpha():
                    state = txt

            # ── Cell 1: Traded Issuer ────────────
            issuer_cell = cells[1]
            full_text   = issuer_cell.get_text(" ", strip=True)

            # Company name: text before the ticker
            company = ""
            issuer_link = issuer_cell.find("a")
            if issuer_link:
                company = issuer_link.get_text(strip=True)

            # Ticker: pattern "XXX:US" — strip the ":US" suffix
            ticker = ""
            ticker_match = re.search(r'\b([A-Z]{1,5}):US\b', full_text)
            if ticker_match:
                ticker = ticker_match.group(1)

            # ── Cell 2: Published (disclosure date)
            disc_date = normalise_date(cells[2].get_text(strip=True))

            # ── Cell 3: Traded (trade date) ──────
            trade_date = normalise_date(cells[3].get_text(strip=True))

            # ── Cell 4: Filed After (lag days) ───
            filing_lag = parse_filing_lag(cells[4].get_text(strip=True))

            # Always calculate from dates when available — CT's scraped "Filed After"
            # uses exclusive counting (1 day short of calendar days). We store calendar
            # days consistently across all sources. Fall back to scraped value only if
            # trade_date is missing (e.g. politician reported a date range).
            if trade_date and disc_date:
                try:
                    td = datetime.strptime(trade_date, "%Y-%m-%d")
                    dd = datetime.strptime(disc_date,  "%Y-%m-%d")
                    filing_lag = (dd - td).days
                except Exception:
                    pass  # keep scraped value if date parsing fails

            # ── Cell 5: Owner ────────────────────
            owner = cells[5].get_text(strip=True).lower()

            # ── Cell 6: Type (transaction type) ──
            tx_raw  = cells[6].get_text(strip=True).lower()
            if "buy" in tx_raw or "purchase" in tx_raw:
                tx_type = "buy"
            elif "sell" in tx_raw or "sale" in tx_raw:
                tx_type = "sell"
            elif "exchange" in tx_raw:
                tx_type = "exchange"
            elif "option" in tx_raw:
                tx_type = "option"
            elif "receive" in tx_raw:
                continue  # passive receipt (spin-off, distribution) — not a trading decision
            else:
                tx_type = tx_raw

            # ── Cell 7: Size ─────────────────────
            size_str              = cells[7].get_text(strip=True)
            size_min, size_max, size_mid = parse_size(size_str)

            # ── Trade ID from link (cell 9) ──────
            trade_id = ""
            for a in row.find_all("a", href=True):
                if "/trades/" in a["href"]:
                    trade_id = a["href"].split("/trades/")[-1].strip("/")
                    break
            if not trade_id:
                trade_id = f"{pol_id}_{ticker}_{trade_date}".replace(" ", "_")

            # Skip rows with no disclosure date or trade date — unpriceable,
            # unscorable, and NULLs weaken the UNIQUE index.
            if not disc_date or not trade_date:
                continue

            trades.append({
                "trade_id":         trade_id,
                "politician_id":    pol_id,
                "pol_name":         pol_name,
                "party":            party,
                "chamber":          chamber,
                "state":            state,
                "ticker":           ticker,
                "company":          company,
                "trade_date":       trade_date,
                "disclosure_date":  disc_date,
                "filing_lag_days":  filing_lag,
                "transaction_type": tx_type,
                "size_min":         size_min,
                "size_max":         size_max,
                "size_midpoint":    size_mid,
                "owner":            owner,
            })

        except Exception:
            continue

    return trades

# ─────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────

def upsert_politician(cursor, trade):
    cursor.execute("""
        INSERT OR IGNORE INTO politicians (politician_id, name, party, chamber, state)
        VALUES (?, ?, ?, ?, ?)
    """, (trade["politician_id"], trade["pol_name"], trade["party"],
          trade["chamber"], trade["state"]))

def upsert_trade(cursor, trade):
    lag = trade["filing_lag_days"]
    filing_violation = "violation" if (lag is not None and lag > 45) else "compliant"

    cursor.execute("""
        INSERT OR IGNORE INTO trades (
            trade_id, politician_id, ticker, company,
            trade_date, disclosure_date, filing_lag_days,
            transaction_type, size_min, size_max, size_midpoint, owner,
            filing_violation
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade["trade_id"],          trade["politician_id"],    trade["ticker"],
        trade["company"],           trade["trade_date"],       trade["disclosure_date"],
        trade["filing_lag_days"],   trade["transaction_type"], trade["size_min"],
        trade["size_max"],          trade["size_midpoint"],    trade["owner"],
        filing_violation,
    ))

# ─────────────────────────────────────────────
# MAIN SCRAPER
# ─────────────────────────────────────────────

def run_scraper(max_pages=None, start_page=1, headless=True):
    """
    Args:
        max_pages:  page limit for testing (None = full scrape)
        start_page: resume from this page number
        headless:   False = show Chrome window (debug mode)
    """
    # Clear existing data if starting fresh
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    if start_page == 1 and max_pages is None:
        print("Clearing existing data for full re-scrape...")
        cursor.execute("DELETE FROM trades")
        cursor.execute("DELETE FROM politicians")
        conn.commit()

    print("=" * 55)
    print("  Capitol Trades Scraper (Selenium)")
    print("=" * 55)

    driver = make_driver(headless=headless)

    try:
        print(f"\nLoading page 1...")
        driver.get(BASE_URL.format(1))
        time.sleep(PAGE_DELAY)

        # Detect total pages
        soup      = BeautifulSoup(driver.page_source, "html.parser")
        page_text = soup.get_text()
        total_pages = 2903  # known fallback

        match = re.search(r'(\d)\s*/\s*(\d{3,4})', page_text)
        if match:
            total_pages = int(match.group(2))

        end_page = min(start_page + max_pages - 1, total_pages) if max_pages else total_pages

        print(f"Total pages:    {total_pages}")
        print(f"Scraping pages: {start_page} to {end_page}")
        if max_pages:
            print(f"(Test mode: {max_pages} pages)")
        print()

        total_saved = 0

        for page_num in range(start_page, end_page + 1):
            if page_num > 1:
                driver.get(BASE_URL.format(page_num))
                time.sleep(PAGE_DELAY)

            trades = parse_trades_from_page(driver.page_source)

            if not trades:
                print(f"  Page {page_num}: no trades parsed — stopping.")
                with open("data/debug_page.html", "w", encoding="utf-8") as f:
                    f.write(driver.page_source)
                break

            
            new_on_page = 0
            for trade in trades:
                upsert_politician(cursor, trade)
                cursor.execute(
                    "SELECT 1 FROM trades WHERE trade_id = ?",
                    (trade["trade_id"],)
                )
                already_exists = cursor.fetchone() is not None
                upsert_trade(cursor, trade)
                if not already_exists:
                    new_on_page += 1
                    total_saved += 1

            conn.commit()

            if new_on_page == 0 and page_num > 1:
                print(f"  Page {page_num}: no new trades — stopping early.")
                break

            if page_num % 10 == 0 or page_num == start_page or page_num == end_page:
                print(f"  Page {page_num:4d}/{end_page} | New: {new_on_page:2d} | Total new: {total_saved:,}")

            time.sleep(NEXT_DELAY)

    finally:
        driver.quit()
        conn.close()

    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM trades")
    db_trades = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM politicians")
    db_pols   = cursor.fetchone()[0]
    conn.close()

    print("\n" + "=" * 55)
    print(f"  Scrape complete!")
    print(f"  Trades in database:      {db_trades:,}")
    print(f"  Politicians in database: {db_pols:,}")
    print("=" * 55)


if __name__ == "__main__":
    
    run_scraper()