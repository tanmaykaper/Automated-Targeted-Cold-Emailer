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
            ("DATA_ANALYTICS", "CONSULTING", "CORP_STRAT", "STARTUP_DATA_AI", "STARTUP_FINTECH", "STARTUP_BRANDS",
             "STARTUP_PRODUCT"),
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
            ("CORP_STRAT", "CONSULTING", "PE_VC", "IB", "STARTUP_CONSUMER", "STARTUP_FINTECH", "STARTUP_BRANDS",
             "STARTUP_FOOD", "STARTUP_PRODUCT"),
        ),
        ProofPoint(
            "nightwalk_founder",
            "I founded Nightwalk Fashion, a D2C apparel brand that broke even within two months "
            "and runs at a 30% margin",
            ("STARTUP_TECH", "STARTUP_IMPACT", "STARTUP_CONSUMER", "STARTUP_DATA_AI", "STARTUP_FINTECH", "STARTUP_BRANDS",
             "STARTUP_FOOD", "STARTUP_PRODUCT", "PE_VC", "CORP_STRAT"),
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
            ("RESEARCH_ECON", "STARTUP_FOOD"),
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
    "brand manager", "category manager", "product manager", "growth manager", "strategy manager",
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
    "brand", "brands", "marketing", "category", "segment", "business head", "p&l",
)

# ── ICP: job-title glossary ─────────────────────────────────────────────────
# The same job is titled differently across companies (and especially across startups).
# Each canonical role lists its alternates and the tier it scores as. icp.title_tier() takes the
# best tier from either the plain lists above or this glossary; discovery also rotates through it.
TITLE_GLOSSARY: dict[str, tuple[str, tuple[str, ...]]] = {
    "Founder / CEO": ("A", (
        "founder", "co-founder", "cofounder", "founding partner", "ceo", "chief executive",
        "managing director", "proprietor")),
    "Founder's Office / Chief of Staff": ("A", (
        "founder's office", "founders office", "founders' office", "office of the founder", "office of the ceo",
        "ceo's office", "ceo office", "chief of staff", "special projects lead", "strategic initiatives lead")),
    "Head of Strategy": ("B", (
        "head of strategy", "strategy head", "head - strategy", "head, strategy", "director of strategy",
        "strategy director", "vp strategy", "vp - strategy", "strategy lead", "head of corporate strategy",
        "head of strategic planning", "gm strategy", "general manager strategy")),
    "Head of Brand / Marketing": ("B", (
        "head of brand", "head of brands", "brand head", "head - brand", "brand director", "brand lead",
        "head of marketing", "marketing head", "marketing lead", "cmo", "chief marketing officer",
        "head of brand marketing", "brand custodian")),
    "Category / Segment / Business Head": ("B", (
        "category head", "category lead", "head of category", "segment head", "segment lead", "business head",
        "business unit head", "bu head", "vertical head", "vertical lead", "p&l head", "p&l owner",
        "general manager", "country manager", "city head", "regional head", "cluster head", "channel head")),
    "Head of Growth": ("B", (
        "head of growth", "growth head", "growth lead", "vp growth", "director of growth", "growth director",
        "head of revenue", "revenue lead", "head of d2c", "d2c head", "head of ecommerce", "ecommerce head")),
    "Head of Data / Analytics": ("B", (
        "head of data", "data head", "head of analytics", "analytics head", "analytics lead", "data lead",
        "director of analytics", "head of insights", "insights lead", "head of business intelligence",
        "bi lead", "head of data science")),
    "Head of Operations / BizOps": ("B", (
        "head of operations", "operations head", "ops head", "bizops lead", "business operations lead",
        "head of bizops", "head of business operations", "director of operations", "supply chain head")),
    "Head of Product": ("B", (
        "head of product", "product head", "product lead", "director of product", "vp product",
        "chief product officer", "cpo", "group product manager", "head of npd", "npd head",
        "new product development head")),
    "Brand / Category / Product Manager": ("C", (
        "brand manager", "senior brand manager", "category manager", "senior category manager",
        "product manager", "senior product manager", "growth manager", "strategy manager", "business manager")),
}

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
    "STARTUP_BRANDS": {
        "name": "Consumer Brand Startups (D2C, beauty, wellness, fashion)",
        "keywords": ("d2c brand", "consumer brand", "beauty", "personal care", "skincare", "wellness", "health and wellness",
                     "fashion brand", "apparel", "lifestyle brand", "house of brands", "brand building"),
    },
    "STARTUP_FOOD": {
        "name": "Food & Beverage Startups",
        "keywords": ("food and beverage", "f&b", "food brand", "beverage", "snacks", "packaged food", "healthy snacking",
                     "coffee", "dairy", "cloud kitchen", "restaurant", "foodtech", "nutrition"),
    },
    "STARTUP_PRODUCT": {
        "name": "Consumer Product / CPG Startups",
        "keywords": ("cpg", "fmcg", "consumer goods", "consumer products", "home care", "home goods", "pet care",
                     "baby care", "consumer electronics", "consumer hardware", "product company"),
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
# Each slot: 4 startup entries (two non-tech: brands / food / consumer products, two tech; India hubs
# plus one abroad) + 2 for consulting / PE / research / data. Scoring still ranks tier-1 firms above
# startups; this only decides who gets *found*.
DISCOVERY_ROTATION = (
    (("Mumbai", "STARTUP_BRANDS"), ("Bangalore", "STARTUP_FOOD"), ("Bangalore", "STARTUP_DATA_AI"),
     ("Singapore", "STARTUP_BRANDS"), ("Mumbai", "CONSULTING"), ("Hyderabad", "DATA_ANALYTICS")),
    (("Gurugram", "STARTUP_PRODUCT"), ("Mumbai", "STARTUP_FOOD"), ("Bangalore", "STARTUP_FINTECH"),
     ("Dubai", "STARTUP_FOOD"), ("Mumbai", "PE_VC"), ("London", "CONSULTING")),
    (("Delhi", "STARTUP_BRANDS"), ("Pune", "STARTUP_PRODUCT"), ("Mumbai", "STARTUP_CONSUMER"),
     ("London", "STARTUP_BRANDS"), ("Delhi", "CORP_STRAT"), ("remote", "RESEARCH_ECON")),
    (("Bangalore", "STARTUP_BRANDS"), ("Gurugram", "STARTUP_FOOD"), ("Pune", "STARTUP_DATA_AI"),
     ("New York", "STARTUP_PRODUCT"), ("Bangalore", "CONSULTING"), ("Mumbai", "DATA_ANALYTICS")),
    (("Mumbai", "STARTUP_PRODUCT"), ("Delhi", "STARTUP_FOOD"), ("Gurugram", "STARTUP_FINTECH"),
     ("Singapore", "STARTUP_FOOD"), ("Mumbai", "IB"), ("Bangalore", "PE_VC")),
    (("Gurugram", "STARTUP_BRANDS"), ("Bangalore", "STARTUP_PRODUCT"), ("Hyderabad", "STARTUP_DATA_AI"),
     ("San Francisco", "STARTUP_BRANDS"), ("Delhi", "RESEARCH_ECON"), ("Singapore", "CONSULTING")),
    (("Mumbai", "STARTUP_FOOD"), ("Chennai", "STARTUP_PRODUCT"), ("Mumbai", "STARTUP_IMPACT"),
     ("Dubai", "STARTUP_BRANDS"), ("Dubai", "CORP_STRAT"), ("Pune", "CONSULTING")),
    (("Bangalore", "STARTUP_CONSUMER"), ("Delhi", "STARTUP_PRODUCT"), ("Ahmedabad", "STARTUP_FOOD"),
     ("Berlin", "STARTUP_DATA_AI"), ("Amsterdam", "DATA_ANALYTICS"), ("New York", "PE_VC")),
)
DISCOVERY_TITLES = ("Founder", "Co-Founder", "Partner", "Director", "Head of Strategy", "Founder's Office",
                    "Head of Growth", "Brand Head", "Principal", "Head of Data", "Category Head",
                    "Engagement Manager", "Head of Brand", "Vice President", "Business Head", "Chief of Staff",
                    "Segment Lead", "Head of Marketing", "Head of Analytics", "Strategy Lead", "CTO")

ROLE_EMAIL_PREFIXES = (
    "info", "contact", "support", "hello", "hi", "team", "noreply", "no-reply", "donotreply",
    "help", "careers", "jobs", "hr", "recruit", "recruitment", "talent", "admin", "general",
    "enquiry", "enquiries", "inquiry", "press", "media", "sales", "marketing", "billing",
    "legal", "compliance", "privacy", "office", "mail", "webmaster", "postmaster", "abuse",
    "accounts", "finance", "partners", "investors", "ir", "feedback", "service",
)
