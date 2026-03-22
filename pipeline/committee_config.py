# pipeline/committee_config.py
# Single source of truth for committee → sub-sector and committee → name mappings.
# Imported by committee_loader.py and sector_fetcher.py.
#
# Phase 2 upgrade (2026-03-18): switched from raw yfinance industry strings to
# a custom sub-sector taxonomy (~35 categories). This resolves false positives
# that survived Phase 1 (e.g. Oracle vs CrowdStrike both in "Software - Infrastructure").
#
# Ticker → sub-sector assignment:
#   1. Manual override (CYBERSECURITY_TICKERS / INTEL_SURVEILLANCE_TICKERS)
#   2. INDUSTRY_TO_SUBSECTOR lookup (yfinance industry string → sub-sector)
#   3. No match → not flagged

# ─────────────────────────────────────────────────────────────────
# MANUAL TICKER OVERRIDES
# These tickers return "Software - Infrastructure" from yfinance but
# belong to more specific sub-sectors.
# ─────────────────────────────────────────────────────────────────

CYBERSECURITY_TICKERS = {
    "CRWD",   # CrowdStrike
    "PANW",   # Palo Alto Networks
    "FTNT",   # Fortinet
    "ZS",     # Zscaler
    "S",      # SentinelOne
    "OKTA",   # Okta
    "CYBR",   # CyberArk
    "QLYS",   # Qualys
    "TENB",   # Tenable
    "VRNT",   # Verint Systems
    "CHKP",   # Check Point
    "NET",    # Cloudflare
    "RPD",    # Rapid7
    "SAIL",   # SailPoint
    "MIME",   # Mimecast
}

INTEL_SURVEILLANCE_TICKERS = {
    "BAH",    # Booz Allen Hamilton
    "SAIC",   # Science Applications International
    "LDOS",   # Leidos
    "CACI",   # CACI International
    "MANT",   # ManTech International
}

# ─────────────────────────────────────────────────────────────────
# INDUSTRY STRING → SUB-SECTOR
# Maps yfinance industry strings to custom sub-sectors.
# Tickers not covered here (ETFs, foreign ADRs, etc.) won't be flagged.
# ─────────────────────────────────────────────────────────────────

INDUSTRY_TO_SUBSECTOR = {
    # ── Technology ───────────────────────────────────────────────
    # Note: "Software - Infrastructure" cybersecurity names are
    # overridden via CYBERSECURITY_TICKERS above.
    "Software - Infrastructure":                "Cloud & Enterprise Software",
    "Software - Application":                   "Cloud & Enterprise Software",
    "Information Technology Services":          "Cloud & Enterprise Software",
    "Semiconductors":                           "Semiconductors",
    "Semiconductor Equipment & Materials":      "Semiconductors",
    "Electronic Components":                    "Semiconductors",
    "Scientific & Technical Instruments":       "Semiconductors",
    "Communication Equipment":                  "Telecom & Communications",
    "Computer Hardware":                        "Cloud & Enterprise Software",

    # ── Defense ──────────────────────────────────────────────────
    "Aerospace & Defense":                      "Aerospace & Defense",

    # ── Healthcare ───────────────────────────────────────────────
    "Drug Manufacturers - General":             "Big Pharma",
    "Drug Manufacturers - Specialty & Generic": "Big Pharma",
    "Biotechnology":                            "Biotech",
    "Medical Devices":                          "Medical Devices",
    "Medical Care Facilities":                  "Healthcare Services",
    "Healthcare Plans":                         "Health Insurance",
    "Diagnostics & Research":                   "Diagnostics & Lab",
    "Health Information Services":              "Healthcare Services",

    # ── Financial ────────────────────────────────────────────────
    "Banks - Diversified":                      "Banks",
    "Banks - Regional":                         "Banks",
    "Insurance - Life":                         "Insurance",
    "Insurance - Property & Casualty":          "Insurance",
    "Insurance - Diversified":                  "Insurance",
    "Insurance - Specialty":                    "Insurance",
    "Asset Management":                         "Investment & Capital Markets",
    "Capital Markets":                          "Investment & Capital Markets",
    "Financial Data & Stock Exchanges":         "Investment & Capital Markets",
    "Credit Services":                          "Fintech & Payments",
    "Mortgage Finance":                         "Mortgage Finance",

    # ── Energy ───────────────────────────────────────────────────
    "Oil & Gas Integrated":                     "Oil & Gas",
    "Oil & Gas E&P":                            "Oil & Gas",
    "Oil & Gas Refining & Marketing":           "Oil & Gas",
    "Oil & Gas Midstream":                      "Oil & Gas",
    "Oil & Gas Equipment & Services":           "Oil & Gas",
    "Utilities - Regulated Electric":           "Utilities",
    "Utilities - Regulated Gas":                "Utilities",
    "Utilities - Diversified":                  "Utilities",
    "Utilities - Independent Power Producers":  "Utilities",
    "Solar":                                    "Renewable Energy",

    # ── Telecom ──────────────────────────────────────────────────
    "Telecom Services":                         "Telecom & Communications",
    "Broadcasting":                             "Telecom & Communications",
    "Cable & Satellite":                        "Telecom & Communications",
    "Internet Content & Information":           "Telecom & Communications",

    # ── Transport ────────────────────────────────────────────────
    "Airlines":                                 "Airlines",
    "Railroads":                                "Rail",
    "Trucking":                                 "Trucking & Freight",
    "Integrated Freight & Logistics":           "Logistics",
    "Shipping & Ports":                         "Logistics",
    "Engineering & Construction":               "Construction & Engineering",

    # ── Agriculture ──────────────────────────────────────────────
    "Farm Products":                            "Crop Production & Inputs",
    "Agricultural Inputs":                      "Crop Production & Inputs",
    "Farm & Heavy Construction Machinery":      "Agricultural Machinery",
    "Packaged Foods":                           "Food & Beverage",
    "Food Distribution":                        "Food & Beverage",

    # ── Materials ────────────────────────────────────────────────
    "Thermal Coal":                             "Coal",
    "Other Industrial Metals & Mining":         "Mining & Metals",
    "Copper":                                   "Mining & Metals",
    "Gold":                                     "Mining & Metals",
    "Steel":                                    "Mining & Metals",
}

# ─────────────────────────────────────────────────────────────────
# COMMITTEE → SUB-SECTORS
# Maps thomas_id → list of sub-sectors that committee oversees.
# Empty list = jurisdiction too broad to flag reliably.
# ─────────────────────────────────────────────────────────────────

COMMITTEE_SUBSECTOR_MAP = {
    # ── Agriculture ──────────────────────────────────────────────
    "HSAG": [
        "Crop Production & Inputs", "Food & Beverage", "Agricultural Machinery",
    ],
    "SSAF": [
        "Crop Production & Inputs", "Food & Beverage", "Agricultural Machinery",
    ],

    # ── Armed Services / Defense ─────────────────────────────────
    "HSAS": ["Aerospace & Defense", "Cybersecurity"],
    "SSAS": ["Aerospace & Defense", "Cybersecurity"],

    # ── Banking / Financial Services ─────────────────────────────
    "HSBA": [
        "Banks", "Investment & Capital Markets", "Insurance",
        "Fintech & Payments", "Mortgage Finance",
    ],
    "SSBK": [
        "Banks", "Investment & Capital Markets", "Insurance",
        "Fintech & Payments", "Mortgage Finance",
    ],

    # ── Energy & Commerce (House) — broad jurisdiction ───────────
    "HSIF": [
        "Big Pharma", "Biotech", "Medical Devices",
        "Health Insurance", "Healthcare Services",
        "Oil & Gas", "Utilities",
        "Telecom & Communications",
        "Semiconductors", "Cybersecurity",
    ],

    # ── Energy & Natural Resources (Senate) ──────────────────────
    "SSEG": ["Oil & Gas", "Utilities", "Renewable Energy"],

    # ── Environment & Public Works (Senate) ──────────────────────
    "SSEV": ["Oil & Gas", "Utilities", "Renewable Energy"],

    # ── Science, Space & Technology (House) ──────────────────────
    "HSSY": ["Semiconductors", "Cybersecurity", "Cloud & Enterprise Software"],

    # ── Commerce, Science & Transportation (Senate) ──────────────
    "SSCM": [
        "Telecom & Communications",
        "Airlines", "Rail", "Trucking & Freight", "Logistics",
    ],

    # ── Ways & Means (House) ─────────────────────────────────────
    "HSWM": [
        "Banks", "Investment & Capital Markets", "Insurance",
        "Big Pharma", "Biotech", "Health Insurance",
    ],

    # ── Finance (Senate) ─────────────────────────────────────────
    "SSFI": [
        "Banks", "Investment & Capital Markets", "Insurance",
        "Big Pharma", "Biotech", "Health Insurance",
    ],

    # ── HELP (Senate) ────────────────────────────────────────────
    "SSHR": [
        "Big Pharma", "Biotech", "Medical Devices",
        "Health Insurance", "Healthcare Services", "Diagnostics & Lab",
    ],

    # ── Intelligence ─────────────────────────────────────────────
    "HLIG": ["Cybersecurity", "Aerospace & Defense", "Intelligence & Surveillance"],
    "SLIN": ["Cybersecurity", "Aerospace & Defense", "Intelligence & Surveillance"],

    # ── Foreign Affairs / Relations ──────────────────────────────
    "HSFA": ["Aerospace & Defense"],
    "SSFR": ["Aerospace & Defense"],

    # ── Judiciary ────────────────────────────────────────────────
    "HSJU": ["Banks", "Investment & Capital Markets", "Fintech & Payments"],
    "SSJU": ["Banks", "Investment & Capital Markets", "Fintech & Payments"],

    # ── Transportation & Infrastructure (House) ──────────────────
    "HSPW": [
        "Airlines", "Rail", "Trucking & Freight",
        "Logistics", "Construction & Engineering",
    ],

    # ── Homeland Security (House) ────────────────────────────────
    "HSHM": ["Cybersecurity", "Aerospace & Defense"],

    # ── Natural Resources (House) ────────────────────────────────
    "HSII": ["Oil & Gas", "Mining & Metals", "Coal"],

    # ── Too broad to flag reliably ───────────────────────────────
    "HSSM": [],
    "SSSB": [],
    "HSAP": [],
    "SSAP": [],
    "HSBU": [],
    "SSBU": [],
}

# ─────────────────────────────────────────────────────────────────
# COMMITTEE DISPLAY NAMES
# ─────────────────────────────────────────────────────────────────

COMMITTEE_NAMES = {
    "HSAG": "House Agriculture",
    "SSAF": "Senate Agriculture, Nutrition & Forestry",
    "HSAS": "House Armed Services",
    "SSAS": "Senate Armed Services",
    "HSBA": "House Financial Services",
    "SSBK": "Senate Banking, Housing & Urban Affairs",
    "HSIF": "House Energy & Commerce",
    "SSEG": "Senate Energy & Natural Resources",
    "SSEV": "Senate Environment & Public Works",
    "HSSY": "House Science, Space & Technology",
    "SSCM": "Senate Commerce, Science & Transportation",
    "HSWM": "House Ways & Means",
    "SSFI": "Senate Finance",
    "SSHR": "Senate HELP",
    "HLIG": "House Intelligence (Permanent Select)",
    "SLIN": "Senate Intelligence (Select)",
    "HSFA": "House Foreign Affairs",
    "SSFR": "Senate Foreign Relations",
    "HSJU": "House Judiciary",
    "SSJU": "Senate Judiciary",
    "HSPW": "House Transportation & Infrastructure",
    "HSSM": "House Small Business",
    "SSSB": "Senate Small Business & Entrepreneurship",
    "HSII": "House Natural Resources",
    "HSHM": "House Homeland Security",
    "HSAP": "House Appropriations",
    "SSAP": "Senate Appropriations",
    "HSBU": "House Budget",
    "SSBU": "Senate Budget",
    "HSGO": "House Oversight & Government Reform",
    "SSGA": "Senate Homeland Security & Governmental Affairs",
    "HSED": "House Education & the Workforce",
    "HSVR": "House Veterans Affairs",
    "SSVA": "Senate Veterans Affairs",
    "HSRU": "House Rules",
    "HSHA": "House Administration",
    "HSSO": "House Ethics",
    "SSRA": "Senate Rules & Administration",
    "SSEC": "Senate Ethics (Select)",
    "SPAG": "Senate Special Committee on Aging",
    "SLIA": "Senate Indian Affairs",
    "JSEC": "Joint Economic Committee",
    "JLIB": "Joint Committee on the Library",
    "JPRT": "Joint Committee on Printing",
    "JTAX": "Joint Committee on Taxation",
    "JSTX": "Joint Committee on Taxation",
    "JCSE": "Helsinki Commission",
    "JSIK": "Joint Committee (Other)",
    "JSLC": "Joint Committee on the Library",
    "JSPR": "Joint Committee on Printing",
    "HSZS": "House China Select Committee",
    "HSQJ": "House Jan. 6 Select Subcommittee",
    "SCNC": "Senate Caucus on Narcotics Control",
    "SLET": "Senate Select Ethics",
}

# ─────────────────────────────────────────────────────────────────
# HELPER: resolve ticker → sub-sector
# ─────────────────────────────────────────────────────────────────

def get_subsector(ticker, industry):
    """
    Returns the sub-sector string for a given ticker + industry,
    or None if unmapped.

    Priority:
      1. Manual ticker overrides (cybersecurity / intel & surveillance)
      2. INDUSTRY_TO_SUBSECTOR lookup
    """
    if ticker in CYBERSECURITY_TICKERS:
        return "Cybersecurity"
    if ticker in INTEL_SURVEILLANCE_TICKERS:
        return "Intelligence & Surveillance"
    return INDUSTRY_TO_SUBSECTOR.get(industry)
