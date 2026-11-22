"""Sender persona + Ideal Customer Profile (ICP).

Ported from v1 `modules/config.py`, with every sender fact re-grounded against
`resume.pdf`. v1 prompts claimed an EY internship (not on the resume) and an
internship availability window that ended in October 2026; both are removed.
The drafting prompts may ONLY use facts listed in SENDER.proof_points.

Edit this file when the resume or the ask changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProofPoint:
    key: str
    text: str           # wording the model may use (verbatim-safe)
    fits: tuple[str, ...]  # verticals / functions where this is the strongest credential


@dataclass(frozen=True)
class Sender:
    first_name: str = "Tanmay"
    full_name: str = "Tanmay Kaper"
    email: str = "tanmay.kaper1401@gmail.com"
    linkedin: str = "linkedin.com/in/tanmay-k-a344b1226"
    headline: str = "BSc Economics student at NMIMS Mumbai (graduating May 2027)"
    identity: str = ("Tanmay Kaper, a final-year BSc Economics student at SVKM's NMIMS Mumbai (Sarla Anil Modi School "
                     "of Economics), graduating May 2027, currently a Consulting Intern in KPMG's Technology & Data "
                     "Analytics team in Mumbai. Skills: Python, R, SQL, advanced Excel, SAP Analytics Cloud, SAP Datasphere.")
    signature: str = "Tanmay Kaper\nBSc Economics, NMIMS Mumbai '27\nlinkedin.com/in/tanmay-k-a344b1226"
    proof_points: tuple[ProofPoint, ...] = field(default_factory=lambda: (
        ProofPoint(
            "kpmg_sony_etl",
            "At KPMG (Technology & Data Analytics), I built an automated Python ETL pipeline for "
            "Sony Entertainment's raw BARC viewership data, replacing a manual process",
            ("DATA_ANALYTICS", "CONSULTING", "STARTUP_TECH", "CORP_STRAT", "MEDIA"),
        ),
        ProofPoint(
            "kpmg_sac_dashboards",
            "At KPMG I rebuilt C-suite dashboards in SAP Analytics Cloud for Sony Entertainment, "
            "making them over 80% more responsive, and owned the CEO dashboard data feeds",
            ("DATA_ANALYTICS", "CONSULTING", "CORP_STRAT"),
        ),
        ProofPoint(
            "kpmg_sap_fabric",
            "At KPMG I mapped the integration architecture between SAP environments and Microsoft "
            "Data Fabric for a client's data modernisation",
            ("DATA_ANALYTICS", "CONSULTING", "STARTUP_TECH"),
        ),
        ProofPoint(
            "yokohama_pricing",
            "As a data analyst intern at Yokohama Off-Highway Tyres I analysed 10 competitor "
            "portfolios across 200+ SKUs to find pricing gaps for the strategy team",
            ("CORP_STRAT", "CONSULTING", "PE_VC", "IB"),
        ),
        ProofPoint(
            "nightwalk_founder",
            "I founded Nightwalk Fashion, a D2C apparel brand that broke even within two months "
            "and runs at a 30% margin",
            ("STARTUP_TECH", "STARTUP_IMPACT", "PE_VC", "CORP_STRAT"),
        ),
        ProofPoint(
            "fame2_research",
            "I wrote a research paper on how effective India's FAME II policy was at driving EV adoption",
            ("RESEARCH_ECON", "STARTUP_IMPACT", "CONSULTING"),
        ),
        ProofPoint(
            "street_food_research",
            "I studied market structure and pricing in Mumbai's unregulated street-food sector "
            "(monopolistic competition)",
            ("RESEARCH_ECON",),
        ),
        ProofPoint(
            "south_africa_research",
            "I wrote a report on South Africa's post-apartheid macroeconomic development policy",
            ("RESEARCH_ECON",),
        ),
        ProofPoint(
            "thinqbator_vc",
            "I won first place at the Thinqbator business competition evaluating startups as a VC",
            ("PE_VC", "STARTUP_TECH"),
        ),
        ProofPoint(
            "ib_tutor",
            "I've mentored 50+ IB economics and business students through their research essays",
            ("RESEARCH_ECON",),
        ),
    ))
    skills: str = "Python, R, SQL, advanced Excel, SAP Analytics Cloud, SAP Datasphere"


SENDER = Sender()

MODE_GOALS = {
    "internship": {
        "goal": "a remote or part-time internship in strategy, consulting, research or data analytics",
        "availability": "available now for remote/part-time work alongside my final year",
        "soft_asks": (
            "Would you be open to a short chat about how your team takes on interns?",
            "Is it worth a quick conversation, or is there someone on your team better placed to ask?",
            "Would it be useful if I sent over a one-page CV?",
        ),
    },
    "job": {
        "goal": "a full-time graduate analyst role in strategy, consulting, data analytics or economic research from 2027",
        "availability": "graduating May 2027 and open to remote, hybrid or on-site roles",
        "soft_asks": (
            "Would you be open to a short chat about how your team hires graduates?",
            "Is it worth a quick conversation, or is there someone better placed for me to ask?",
            "Would it be useful if I sent over a one-page CV?",
        ),
    },
}

# ── ICP: titles ─────────────────────────────────────────────────────────────
TITLES_TIER_A = (
    "founder", "co-founder", "cofounder", "chief executive", "ceo", "chief strategy officer",
    "managing director", "managing partner", "general partner", "senior partner", "partner",
    "chief of staff", "chief data officer", "chief operating officer", "coo", "president", "owner",
)
TITLES_TIER_B = (
    "vice president", "vp", "director", "head of", "principal", "associate partner",
    "engagement manager", "senior manager", "chief economist", "senior fellow",
    "principal researcher", "department head", "lead economist",
)
TITLES_TIER_C = (
    "manager", "senior analyst", "team lead", "team leader", "lead analyst", "associate",
    "senior associate", "consultant", "senior consultant", "economist", "research fellow",
)
REJECT_TITLES = (
    "executive assistant", "personal assistant", "hr ", "hr,", "human resources", "people partner",
    "talent acquisition", "talent sourcer", "recruiter", "recruitment", "customer service",
    "customer success", "customer support", "office manager", "administrative", "receptionist",
    "intern", "trainee", "student", "marketing coordinator", "sales coordinator", "retired",
    "freelance", "looking for", "open to work", "seeking", "people & development", "people and development",
    "people & culture", "people and culture", "people operations", "talent", "hrbp",
)
# Functions that make a lead more relevant for Tanmay's ask
RELEVANT_FUNCTIONS = (
    "strategy", "analytics", "data", "research", "economic", "consulting", "investment",
    "growth", "operations", "business development", "insights", "policy", "finance",
    "corporate development", "venture", "private equity", "transformation",
)

# ── ICP: verticals ──────────────────────────────────────────────────────────
VERTICALS: dict[str, dict] = {
    "CONSULTING": {
        "name": "Management Consulting",
        "keywords": ("strategy consulting", "management consulting", "consulting", "advisory"),
    },
    "STARTUP_TECH": {
        "name": "Tech Startups",
        "keywords": ("saas", "fintech", "edtech", "ai startup", "b2b software", "data platform", "software", "internet"),
    },
    "STARTUP_IMPACT": {
        "name": "Impact / Climate Startups",
        "keywords": ("climate", "impact", "cleantech", "healthtech", "agritech", "social enterprise", "renewable", "sustainability"),
    },
    "PE_VC": {
        "name": "PE & Venture Capital",
        "keywords": ("private equity", "venture capital", "growth equity", "venture", "investment management"),
    },
    "CORP_STRAT": {
        "name": "Corporate Strategy",
        "keywords": ("corporate strategy", "strategic planning", "business development", "corporate development"),
    },
    "RESEARCH_ECON": {
        "name": "Economic Research & Policy",
        "keywords": ("economic research", "think tank", "policy", "development economics", "economic consulting", "research institute", "public policy"),
    },
    "IB": {
        "name": "Investment Banking",
        "keywords": ("investment bank", "m&a", "boutique bank", "capital markets", "investment banking"),
    },
    "DATA_ANALYTICS": {
        "name": "Data & Analytics",
        "keywords": ("data analytics", "analytics consulting", "data strategy", "decision science", "business intelligence", "analytics"),
    },
}

TIER1_BRANDS = (
    "mckinsey", "boston consulting group", "bcg", "bain", "deloitte", "pwc", "pricewaterhousecoopers",
    "ernst & young", "kpmg", "accenture", "oliver wyman", "roland berger", "kearney", "l.e.k",
    "arthur d. little", "strategy&", "alvarez & marsal", "zs associates", "goldman sachs",
    "morgan stanley", "jp morgan", "jpmorgan", "blackstone", "kkr", "sequoia", "peak xv",
    "general atlantic", "warburg pincus", "aditya birla", "tata", "infosys", "wipro",
    "world bank", "imf", "asian development bank", "niti aayog", "brookings", "rand corporation",
    "chatham house", "j-pal", "idinsight",
)
RESEARCH_MARKERS = ("think tank", "policy institute", "research institute", "university",
                    "economics institute", "foundation", "council", "centre for", "center for")

# ── ICP: geography (India-first) ────────────────────────────────────────────
INDIA_MARKERS = ("india", "mumbai", "delhi", "gurgaon", "gurugram", "noida", "bangalore", "bengaluru",
                 "hyderabad", "pune", "chennai", "ahmedabad", "kolkata")

# Discovery rotation (geo search term, vertical id). India ≈ 60% of slots.
DISCOVERY_ROTATION = (
    (("Mumbai", "CONSULTING"), ("Bangalore", "STARTUP_TECH"), ("Delhi", "CORP_STRAT"), ("Hyderabad", "DATA_ANALYTICS")),
    (("Mumbai", "PE_VC"), ("Bangalore", "CONSULTING"), ("Chennai", "CORP_STRAT"), ("London", "CONSULTING")),
    (("Mumbai", "STARTUP_TECH"), ("Bangalore", "STARTUP_IMPACT"), ("Pune", "DATA_ANALYTICS"), ("remote", "RESEARCH_ECON")),
    (("Delhi", "RESEARCH_ECON"), ("think tank", "RESEARCH_ECON"), ("Singapore", "CONSULTING"), ("London", "STARTUP_TECH")),
    (("Mumbai", "IB"), ("Bangalore", "PE_VC"), ("Dubai", "CORP_STRAT"), ("New York", "PE_VC")),
    (("Ahmedabad", "STARTUP_TECH"), ("Hyderabad", "STARTUP_IMPACT"), ("Pune", "CONSULTING"), ("Amsterdam", "DATA_ANALYTICS")),
    (("Delhi", "CORP_STRAT"), ("Mumbai", "DATA_ANALYTICS"), ("San Francisco", "STARTUP_TECH"), ("Boston", "RESEARCH_ECON")),
    (("Bangalore", "CORP_STRAT"), ("Sydney", "CONSULTING"), ("remote", "STARTUP_TECH"), ("remote", "DATA_ANALYTICS")),
)
DISCOVERY_TITLES = ("Founder", "Partner", "Director", "Head of Strategy", "Principal",
                    "Engagement Manager", "Vice President", "Chief of Staff")

ROLE_EMAIL_PREFIXES = (
    "info", "contact", "support", "hello", "hi", "team", "noreply", "no-reply", "donotreply",
    "help", "careers", "jobs", "hr", "recruit", "recruitment", "talent", "admin", "general",
    "enquiry", "enquiries", "inquiry", "press", "media", "sales", "marketing", "billing",
    "legal", "compliance", "privacy", "office", "mail", "webmaster", "postmaster", "abuse",
    "accounts", "finance", "partners", "investors", "ir", "feedback", "service",
)
