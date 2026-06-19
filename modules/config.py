"""
config.py — Single source of truth for all system-wide settings
Every module imports from here. Change once, change everywhere.
"""

import os
from dotenv import load_dotenv
load_dotenv()

# ══════════════════════════════════════════════════════════════════════════
# MODE TOGGLE  —  set via GitHub Secret or .env
# ══════════════════════════════════════════════════════════════════════════
# OUTREACH_MODE=internship  →  part-time / remote internship ask
# OUTREACH_MODE=job         →  full-time graduate role ask
OUTREACH_MODE = os.getenv("OUTREACH_MODE", "internship").lower()
assert OUTREACH_MODE in ("internship", "job"), \
    f"OUTREACH_MODE must be 'internship' or 'job', got '{OUTREACH_MODE}'"

# ══════════════════════════════════════════════════════════════════════════
# CADENCE
# Three independent pipeline stages, each with its OWN volume — these are
# NOT the same number wearing different hats, and must not be collapsed
# into one. Sourcing a wide pool, drafting a focused subset of it, and
# sending an even smaller approved subset of THAT are deliberately
# different funnel widths:
#
#   SOURCE  (Module A)  →  WEEKLY_TARGET / SCORE_THRESHOLD
#       How many qualified leads get sourced+scored per week. This is the
#       top of the funnel — wider than what actually gets drafted, so
#       there's always a backlog of qualified-but-not-yet-drafted leads
#       sitting in "Pending" rather than the drafting stage starving.
#
#   DRAFT   (Module C)   →  DRAFTS_PER_RUN / WEEKLY_DRAFT_CAP
#       How many of those sourced leads get an AI-drafted email per run
#       and per week. Deliberately ≤ WEEKLY_TARGET — sourcing stays ahead
#       of drafting on purpose, so a slow week of replies/approvals doesn't
#       leave drafting with nothing queued.
#
#   SEND    (Module D)   →  MAX_PER_RUN / WEEKLY_CAP
#       How many *approved* emails actually go out per dispatch run and
#       per week — the human-in-the-loop bottleneck. This is the real
#       outbound volume ceiling and the one that matters for deliverability/
#       reputation; it is intentionally the smallest of the three.
#
# Sanity ordering this file enforces: WEEKLY_TARGET ≥ WEEKLY_DRAFT_CAP ≥
# WEEKLY_CAP. If you change one, check the others still make sense — e.g.
# raising WEEKLY_DRAFT_CAP above WEEKLY_TARGET just means most weeks you'll
# draft against a thinner backlog than the cap implies.
# ══════════════════════════════════════════════════════════════════════════

# ── SOURCE (Module A) ──
WEEKLY_TARGET   = int(os.getenv("WEEKLY_TARGET",   "30"))  # leads sourced+scored per week
SCORE_THRESHOLD = int(os.getenv("SCORE_THRESHOLD", "55"))  # lower = wider net (was 65)

# ── DRAFT (Module C) ──
DRAFTS_PER_RUN   = int(os.getenv("DRAFTS_PER_RUN",   "8"))   # AI drafts generated per run_drafting_pipeline() call
WEEKLY_DRAFT_CAP = int(os.getenv("WEEKLY_DRAFT_CAP", "30"))  # informational ceiling — enforced via scheduling
                                                               # cadence (e.g. 8/run × ~4 runs/week ≈ 32, the
                                                               # 4th run of the week trims to stay ≤ 30)

# ── SEND (Module D) ──
MAX_PER_RUN     = int(os.getenv("MAX_PER_RUN",     "8"))   # approved emails sent per dispatch run
WEEKLY_CAP      = int(os.getenv("WEEKLY_CAP",      "32"))  # max emails SENT per week (4 runs × 8 = 32)

assert WEEKLY_TARGET >= WEEKLY_DRAFT_CAP, (
    f"WEEKLY_TARGET ({WEEKLY_TARGET}) should stay ≥ WEEKLY_DRAFT_CAP ({WEEKLY_DRAFT_CAP}) "
    "so sourcing keeps a backlog ahead of drafting — otherwise drafting runs dry."
)

# ══════════════════════════════════════════════════════════════════════════
# CANDIDATE PERSONA
# ══════════════════════════════════════════════════════════════════════════
TANMAY = {
    "name":         "Tanmay Kaper",
    "email":        "tanmay.kaper1401@gmail.com",
    "linkedin":     "linkedin.com/in/tanmay-k-a344b1226/",
    "degree":       "BSc Economics, SVKM's NMIMS Mumbai (CGPA 3.52/4.0, graduating May 2027)",
    "ib":           "IB Diploma 41/45 (94.3%) — Edubridge International School",
    "current_role": "Intern, KPMG — Data & SAP Department",
    "current_work": (
        "SAP Analytics Cloud (SAC), SAP Datasphere, Python data modelling — "
        "building analytics solutions for live client strategy engagements"
    ),
    "edge": (
        "Rare undergrad who bridges strategy and technical execution: "
        "can think at the advisory level and simultaneously build the SAP/Python "
        "pipelines that deliver it — eliminates the usual hand-off lag between "
        "strategy teams and data teams"
    ),
    "founder": (
        "Founded Nightwalk Fashion (D2C apparel brand): break-even in 2 months, "
        "sustained 30% profit margin, donates 50% of annual profits to education"
    ),
    "research": (
        "Published economic research: FAME II EV policy efficacy (India), "
        "monopolistic competition in unregulated markets (Mumbai street food), "
        "South Africa post-apartheid macroeconomic development"
    ),
    "awards": (
        "Rise Global Finalist top 10% worldwide | "
        "Best Delegate VYMUN 2023 | "
        "Queen's Commonwealth Essay Competition award | "
        "IB Festival of Hope National Exhibitor | "
        "Thinqbator Business Competition 1st place (VC evaluation)"
    ),
    "skills_tech":  "Python, R, SQL, SAP Analytics Cloud, SAP Datasphere, Power BI, Excel (advanced)",
    "skills_soft":  "Strategic planning, cross-functional leadership, client communication, public speaking (MUN/debate)",
    "goal_internship": (
        "Remote or part-time strategy / consulting / research / data internship, "
        "immediately available through October 2026. "
        "Part-time during term, available full-time in summers."
    ),
    "goal_job": (
        "Full-time graduate role in strategy, consulting, data analytics, or economic research "
        "from January 2027. Open globally — remote, hybrid, or on-site."
    ),
}

MODE_CONTEXT = {
    "internship": {
        "ask":          "a 15-minute call to explore a part-time or remote internship fit",
        "availability": "immediately available remotely; part-time during term, full-time in summer (through Oct 2026)",
        "value_prop":   "KPMG-level analytical rigour, zero onboarding overhead, can contribute from day one remotely",
        "goal":         TANMAY["goal_internship"],
        "cta":          "Would a quick 15-minute call this week work?",
    },
    "job": {
        "ask":          "a 20-minute call about graduate analyst / associate roles starting early 2027",
        "availability": "available full-time from January 2027 — remote, hybrid, or on-site globally",
        "value_prop":   "KPMG consulting experience + SAP/Python technical depth + published economics research, all before graduating",
        "goal":         TANMAY["goal_job"],
        "cta":          "Worth a 20-minute call to see if there's a fit?",
    },
}

# ══════════════════════════════════════════════════════════════════════════
# TARGET TITLES  (expanded — includes dept managers + hiring managers)
# ══════════════════════════════════════════════════════════════════════════
# Tier A — ultimate decision makers (highest score bonus)
TITLES_TIER_A = [
    "founder", "co-founder", "cofounder",
    "chief executive officer", "ceo",
    "chief strategy officer", "cso",
    "managing director", "md",
    "managing partner", "general partner", "senior partner", "partner",
    "chief of staff",
    "chief data officer", "chief operating officer",
    "president",
]

# Tier B — department heads and senior managers (solid access)
TITLES_TIER_B = [
    "vice president", "vp ",
    "director", "senior director", "associate director",
    "head of strategy", "head of growth", "head of analytics",
    "head of research", "head of data", "head of business development",
    "head of operations", "head of product", "head of finance",
    "principal", "associate partner", "engagement manager", "senior manager",
    "research director", "chief economist", "senior fellow", "principal researcher",
    "department head", "dept head",
]

# Tier C — managers and hiring managers (wider net, higher reply rate)
TITLES_TIER_C = [
    "manager", "senior analyst", "strategy manager", "analytics manager",
    "data science manager", "research manager", "operations manager",
    "business development manager", "hiring manager",
    "team lead", "team leader", "lead analyst",
    "associate", "senior associate",
    "consultant", "senior consultant",
]

ALL_TARGET_TITLES = TITLES_TIER_A + TITLES_TIER_B + TITLES_TIER_C

REJECT_TITLES = [
    "executive assistant", "personal assistant",
    "hr manager", "hr director", "human resources",
    "talent acquisition", "talent sourcer", "recruiter",
    "customer service", "customer success", "customer support",
    "office manager", "administrative assistant", "receptionist",
    "intern ", "student intern", "graduate trainee",
    "marketing coordinator", "sales coordinator",
]

REJECT_EMAIL_PREFIXES = [
    "info", "contact", "support", "hello", "team",
    "noreply", "no-reply", "help", "careers", "jobs",
    "admin", "general", "enquiry", "press", "media",
    "sales", "marketing", "billing", "legal", "compliance", "privacy",
]

# ══════════════════════════════════════════════════════════════════════════
# EMAIL VALIDATION  (Module A — sourcing/inference layer)
# ══════════════════════════════════════════════════════════════════════════
# Typo'd / structurally-bogus TLDs to reject before a candidate email is
# ever formed. Centralized here (rather than hardcoded in module_a) so a
# newly-noticed typo pattern can be added in one place.
REJECT_TLDS = [
    "con", "cmo", "vom", "comm", "cm", "co.", "ocm", "om", "c0m",
    "nte", "ner", "gmial", "gnail", "gmal", "outlok", "outloo",
]

# SMTP RCPT TO verification — best-effort mailbox existence probe.
# Many corporate mail servers block outbound probing on port 25 entirely
# (most common outcome: 'unknown'), so this is a confidence booster, not a
# guarantee — see module_a_sourcing._smtp_verify() for the full fallback
# logic when a probe is inconclusive.
SMTP_VERIFY_ENABLED = os.getenv("SMTP_VERIFY_ENABLED", "true").lower() == "true"
SMTP_VERIFY_TIMEOUT = int(os.getenv("SMTP_VERIFY_TIMEOUT", "8"))   # seconds per probe
SMTP_HELO_DOMAIN    = os.getenv("SMTP_HELO_DOMAIN", "gmail.com")   # domain used in SMTP HELO/MAIL FROM

# ══════════════════════════════════════════════════════════════════════════
# GEOGRAPHIC SEGMENTS
# India slots appear MORE in rotation (60 % of slots) for Phase 1 priority.
# International slots ensure the pool never dries up.
# ══════════════════════════════════════════════════════════════════════════
GEO_SEGMENTS = [
    # id          display                  search terms                          tz                  region
    ("IN_MUM",   "India — Mumbai",         ["Mumbai", "Maharashtra"],             "Asia/Kolkata",     "IN"),
    ("IN_BLR",   "India — Bangalore",      ["Bangalore", "Bengaluru"],            "Asia/Kolkata",     "IN"),
    ("IN_DEL",   "India — Delhi/NCR",      ["Delhi", "Gurgaon", "Noida"],         "Asia/Kolkata",     "IN"),
    ("IN_HYD",   "India — Hyderabad",      ["Hyderabad", "Telangana"],            "Asia/Kolkata",     "IN"),
    ("IN_PUN",   "India — Pune",           ["Pune"],                              "Asia/Kolkata",     "IN"),
    ("IN_CHE",   "India — Chennai",        ["Chennai", "Tamil Nadu"],             "Asia/Kolkata",     "IN"),
    ("IN_AHM",   "India — Ahmedabad",      ["Ahmedabad", "Gujarat"],              "Asia/Kolkata",     "IN"),
    ("UK_LON",   "UK — London",            ["London", "United Kingdom"],          "Europe/London",    "UK"),
    ("UK_OTH",   "UK — Other",             ["Manchester", "Edinburgh", "Leeds"],  "Europe/London",    "UK"),
    ("US_NYC",   "US — New York",          ["New York", "NYC"],                   "America/New_York", "US"),
    ("US_SF",    "US — San Francisco",     ["San Francisco", "Bay Area"],         "America/Los_Angeles","US"),
    ("US_BOS",   "US — Boston",            ["Boston", "Cambridge MA"],            "America/New_York", "US"),
    ("SG",       "Singapore",              ["Singapore"],                         "Asia/Singapore",   "SG"),
    ("UAE",      "UAE / Dubai",            ["Dubai", "Abu Dhabi"],                "Asia/Dubai",       "ME"),
    ("AU",       "Australia",              ["Sydney", "Melbourne"],               "Australia/Sydney", "AU"),
    ("EU",       "Europe",                 ["Amsterdam", "Berlin", "Zurich"],     "Europe/Amsterdam", "EU"),
    ("REMOTE",   "Remote-first / Global",  ["remote", "distributed", "globally"], "UTC",              "REMOTE"),
    ("RESEARCH", "Research & Think Tanks", ["think tank", "research institute"],  "UTC",              "RESEARCH"),
]

# ══════════════════════════════════════════════════════════════════════════
# INDUSTRY VERTICALS
# ══════════════════════════════════════════════════════════════════════════
INDUSTRY_VERTICALS = [
    # id              display                    search keywords                    extra titles
    ("CONSULTING",    "Management Consulting",
     ["strategy consulting", "management consulting", "business strategy"],
     ["manager", "consultant", "senior consultant", "engagement manager", "associate"]),

    ("STARTUP_TECH",  "Tech Startups",
     ["SaaS", "fintech", "edtech", "AI startup", "B2B software", "data platform"],
     ["manager", "team lead", "senior analyst", "business analyst"]),

    ("STARTUP_IMPACT","Impact / Climate Startups",
     ["climate tech", "impact startup", "cleantech", "healthtech", "agritech", "social enterprise"],
     ["manager", "team lead", "programme manager"]),

    ("PE_VC",         "PE & Venture Capital",
     ["private equity", "venture capital", "growth equity"],
     ["associate", "senior associate", "analyst", "investment analyst"]),

    ("CORP_STRAT",    "Corporate Strategy",
     ["corporate strategy", "strategic planning", "business development"],
     ["strategy manager", "business development manager", "senior analyst"]),

    ("RESEARCH_ECON", "Economic Research & Policy",
     ["economic research", "think tank", "policy institute", "development economics", "economic consulting"],
     ["research manager", "analyst", "senior analyst", "associate researcher"]),

    ("IB",            "Investment Banking",
     ["investment bank", "M&A advisory", "boutique bank", "capital markets"],
     ["associate", "analyst", "vice president"]),

    ("DATA_ANALYTICS","Data & Analytics",
     ["data analytics", "analytics consulting", "data strategy", "decision science"],
     ["analytics manager", "data science manager", "senior analyst", "team lead"]),
]

# ══════════════════════════════════════════════════════════════════════════
# WEEKLY ROTATION — India-heavy (60 % of slots)
# 12-slot cycle × week-based query variation = inexhaustible pool
# ══════════════════════════════════════════════════════════════════════════
ROTATION_SCHEDULE = [
    # Slot 0 — India sweep (consulting + tech)
    [("IN_MUM","CONSULTING"),("IN_BLR","STARTUP_TECH"),("IN_DEL","CORP_STRAT"),("IN_HYD","DATA_ANALYTICS")],
    # Slot 1 — India + one intl
    [("IN_MUM","PE_VC"),("IN_BLR","CONSULTING"),("IN_CHE","CORP_STRAT"),("UK_LON","CONSULTING")],
    # Slot 2 — India startup + remote
    [("IN_MUM","STARTUP_TECH"),("IN_BLR","STARTUP_IMPACT"),("IN_PUN","DATA_ANALYTICS"),("REMOTE","RESEARCH_ECON")],
    # Slot 3 — India research + international push
    [("IN_DEL","RESEARCH_ECON"),("RESEARCH","RESEARCH_ECON"),("SG","CONSULTING"),("UK_LON","STARTUP_TECH")],
    # Slot 4 — India IB + UAE + US
    [("IN_MUM","IB"),("IN_BLR","PE_VC"),("UAE","CORP_STRAT"),("US_NYC","PE_VC")],
    # Slot 5 — India second-tier cities + EU
    [("IN_AHM","STARTUP_TECH"),("IN_HYD","STARTUP_IMPACT"),("IN_PUN","CONSULTING"),("EU","DATA_ANALYTICS")],
    # Slot 6 — India corp strat + US
    [("IN_DEL","CORP_STRAT"),("IN_MUM","DATA_ANALYTICS"),("US_SF","STARTUP_TECH"),("US_BOS","RESEARCH_ECON")],
    # Slot 7 — India + Australia + remote
    [("IN_BLR","CORP_STRAT"),("AU","CONSULTING"),("REMOTE","STARTUP_TECH"),("REMOTE","DATA_ANALYTICS")],
    # Slot 8 — India + UK sweep
    [("IN_MUM","CONSULTING"),("IN_DEL","IB"),("UK_LON","PE_VC"),("UK_OTH","STARTUP_IMPACT")],
    # Slot 9 — India impact + intl research
    [("IN_BLR","STARTUP_IMPACT"),("IN_CHE","DATA_ANALYTICS"),("UK_LON","RESEARCH_ECON"),("US_BOS","CONSULTING")],
    # Slot 10 — India PE + SG + EU
    [("IN_MUM","PE_VC"),("IN_HYD","CONSULTING"),("SG","STARTUP_TECH"),("EU","CONSULTING")],
    # Slot 11 — Full international rotation
    [("US_NYC","CONSULTING"),("US_SF","DATA_ANALYTICS"),("SG","PE_VC"),("UAE","STARTUP_TECH")],
]

def get_this_weeks_segments() -> list:
    from datetime import datetime, timezone
    week_num = datetime.now(timezone.utc).isocalendar()[1]
    return ROTATION_SCHEDULE[week_num % len(ROTATION_SCHEDULE)]

def get_geo(geo_id: str) -> dict:
    return {g[0]: dict(zip(["id","name","terms","tz","region"], g))
            for g in GEO_SEGMENTS}.get(geo_id, {})

def get_vertical(v_id: str) -> dict:
    return {v[0]: dict(zip(["id","name","keywords","extra_titles"], v))
            for v in INDUSTRY_VERTICALS}.get(v_id, {})
