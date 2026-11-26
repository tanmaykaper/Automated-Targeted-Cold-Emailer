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
            ("DATA_ANALYTICS", "CONSULTING", "STARTUP_TECH", "STARTUP_DATA_AI", "CORP_STRAT", "MEDIA"),
        ),
        ProofPoint(
            "kpmg_sac_dashboards",
            "At KPMG I rebuilt C-suite dashboards in SAP Analytics Cloud for Sony Entertainment, "
            "making them over 80% more responsive, and owned the CEO dashboard data feeds",
            ("DATA_ANALYTICS", "CONSULTING", "CORP_STRAT", "STARTUP_DATA_AI", "STARTUP_FINTECH"),
        ),
        ProofPoint(
            "kpmg_sap_fabric",
            "At KPMG I mapped the integration architecture between SAP environments and Microsoft "
            "Data Fabric for a client's data modernisation",
            ("DATA_ANALYTICS", "CONSULTING", "STARTUP_TECH", "STARTUP_DATA_AI"),
        ),
        ProofPoint(
            "yokohama_pricing",
            "As a data analyst intern at Yokohama Off-Highway Tyres I analysed 10 competitor "
            "portfolios across 200+ SKUs to find pricing gaps for the strategy team",
            ("CORP_STRAT", "CONSULTING", "PE_VC", "IB", "STARTUP_CONSUMER", "STARTUP_FINTECH"),
        ),
        ProofPoint(
            "nightwalk_founder",
            "I founded Nightwalk Fashion, a D2C apparel brand that broke even within two months "
            "and runs at a 30% margin",
            ("STARTUP_TECH", "STARTUP_IMPACT", "STARTUP_CONSUMER", "STARTUP_DATA_AI", "STARTUP_FINTECH", "PE_VC", "CORP_STRAT"),
        ),
        ProofPoint(
            "fame2_research",
            "I wrote a research paper on how effective India's FAME II policy was at driving EV adoption",
            ("RESEARCH_ECON", "STARTUP_IMPACT", "CONSULTING", "STARTUP_FINTECH"),
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
            ("PE_VC", "STARTUP_TECH", "STARTUP_FINTECH"),
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
    "chief of staff", "founder's office", "founders office", "cto", "chief technology officer",
    "chief business officer", "chief product officer", "chief data officer", "chief operating officer", "coo", "president", "owner",
)
TITLES_TIER_B = (
    "vice president", "vp", "director", "head of", "principal", "growth lead", "data lead",
    "analytics lead", "strategy lead", "bizops lead", "business operations lead", "associate partner",
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
    "bizops", "business operations", "founder's office", "product", "revenue", "pricing",
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
    "STARTUP_DATA_AI": {
        "name": "Data / AI / Analytics Startups",
        "keywords": ("data analytics startup", "ai startup", "analytics platform", "data engineering", "generative ai",
                     "machine learning", "business intelligence", "data infrastructure", "martech", "adtech"),
    },
    "STARTUP_FINTECH": {
        "name": "Fintech Startups",
        "keywords": ("fintech", "payments", "lending", "wealthtech", "insurtech", "neobank", "credit", "embedded finance"),
    },
    "STARTUP_CONSUMER": {
        "name": "Consumer / D2C Startups",
        "keywords": ("d2c", "direct to consumer", "consumer brand", "quick commerce", "e-commerce", "consumer internet",
                     "marketplace", "retail tech"),
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

# Discovery rotation (geo search term, vertical id), one slot per ISO week.
# Each slot: 3 startup entries (2 India hubs + 1 abroad) + 2 for consulting/PE/research/data.
# Startups are weighted toward data/AI, fintech and consumer: the verticals where the resume's
# Python ETL, SAC dashboards, pricing analysis and D2C founder proof points land hardest.
DISCOVERY_ROTATION = (
    (("Bangalore", "STARTUP_DATA_AI"), ("Mumbai", "STARTUP_FINTECH"), ("Singapore", "STARTUP_DATA_AI"),
     ("Mumbai", "CONSULTING"), ("Hyderabad", "DATA_ANALYTICS")),
    (("Gurugram", "STARTUP_CONSUMER"), ("Bangalore", "STARTUP_FINTECH"), ("Dubai", "STARTUP_FINTECH"),
     ("Mumbai", "PE_VC"), ("London", "CONSULTING")),
    (("Mumbai", "STARTUP_CONSUMER"), ("Pune", "STARTUP_DATA_AI"), ("London", "STARTUP_FINTECH"),
     ("Delhi", "CORP_STRAT"), ("remote", "RESEARCH_ECON")),
    (("Bangalore", "STARTUP_TECH"), ("Delhi", "STARTUP_IMPACT"), ("San Francisco", "STARTUP_DATA_AI"),
     ("Bangalore", "CONSULTING"), ("Mumbai", "DATA_ANALYTICS")),
    (("Mumbai", "STARTUP_DATA_AI"), ("Gurugram", "STARTUP_FINTECH"), ("Berlin", "STARTUP_TECH"),
     ("Mumbai", "IB"), ("Bangalore", "PE_VC")),
    (("Bangalore", "STARTUP_CONSUMER"), ("Hyderabad", "STARTUP_DATA_AI"), ("New York", "STARTUP_FINTECH"),
     ("Delhi", "RESEARCH_ECON"), ("Singapore", "CONSULTING")),
    (("Chennai", "STARTUP_TECH"), ("Mumbai", "STARTUP_IMPACT"), ("remote", "STARTUP_DATA_AI"),
     ("Dubai", "CORP_STRAT"), ("Pune", "CONSULTING")),
    (("Delhi", "STARTUP_CONSUMER"), ("Bangalore", "STARTUP_IMPACT"), ("Toronto", "STARTUP_TECH"),
     ("Amsterdam", "DATA_ANALYTICS"), ("New York", "PE_VC")),
)
DISCOVERY_TITLES = ("Founder", "Co-Founder", "Partner", "Director", "Head of Strategy", "Founder's Office",
                    "Head of Growth", "Principal", "Head of Data", "Engagement Manager", "Vice President",
                    "Chief of Staff", "Head of Analytics", "CTO")

ROLE_EMAIL_PREFIXES = (
    "info", "contact", "support", "hello", "hi", "team", "noreply", "no-reply", "donotreply",
    "help", "careers", "jobs", "hr", "recruit", "recruitment", "talent", "admin", "general",
    "enquiry", "enquiries", "inquiry", "press", "media", "sales", "marketing", "billing",
    "legal", "compliance", "privacy", "office", "mail", "webmaster", "postmaster", "abuse",
    "accounts", "finance", "partners", "investors", "ir", "feedback", "service",
)
