"""ICP classification helpers (title tier, vertical, region, company type, timezone)."""
from __future__ import annotations

import re
from functools import lru_cache

from .models import Lead
from .profile import (
    INDIA_MARKERS, REJECT_TITLES, RELEVANT_FUNCTIONS, RESEARCH_MARKERS, TIER1_BRANDS,
    TITLES_TIER_A, TITLES_TIER_B, TITLES_TIER_C, VERTICALS,
)


@lru_cache(maxsize=512)
def _rx(term: str) -> re.Pattern:
    return re.compile(r"(?<![a-z])" + re.escape(term.strip()) + r"(?![a-z])", re.I)


def _has(text: str, terms) -> str:
    for t in terms:
        if _rx(t).search(text):
            return t
    return ""


def title_tier(title: str) -> tuple[str | None, str]:
    """('A'|'B'|'C'|None, reason)."""
    t = " " + (title or "").lower() + " "
    if not title or not title.strip():
        return None, "missing title"
    if bad := _has(t, REJECT_TITLES):
        return None, f"rejected title term '{bad.strip()}'"
    for tier, terms in (("A", TITLES_TIER_A), ("B", TITLES_TIER_B), ("C", TITLES_TIER_C)):
        if hit := _has(t, terms):
            return tier, f"title matches tier {tier} ('{hit}')"
    return None, "title outside target seniority"


def function_relevance(title: str, departments: str = "") -> str:
    return _has(f"{title} {departments}".lower(), RELEVANT_FUNCTIONS)


def detect_vertical(lead: Lead) -> str:
    if lead.vertical and lead.vertical in VERTICALS:
        return lead.vertical
    blob = " ".join([lead.company.industry, " ".join(lead.company.keywords[:40]), lead.company.description,
                     lead.company.name, lead.person.title]).lower()
    best, best_hits = "", 0
    for vid, v in VERTICALS.items():
        hits = sum(1 for k in v["keywords"] if _rx(k).search(blob))
        if hits > best_hits:
            best, best_hits = vid, hits
    return best


def company_type(lead: Lead) -> str:
    blob = f"{lead.company.name} {lead.company.industry} {lead.company.description}".lower()
    if _has(" " + lead.company.name.lower() + " ", TIER1_BRANDS):
        return "tier1"
    if _has(blob, RESEARCH_MARKERS):
        return "research"
    if re.search(r"series [a-e]|seed|pre-seed|venture[- ]backed|raised|y combinator|\byc\b|funding|startup|early[- ]stage",
                 blob + " " + lead.company.funding.lower()):
        return "startup"
    if (lead.vertical or "").startswith("STARTUP_"):
        return "startup"
    return "corporate"


REGION_TZ = {
    "IN": "Asia/Kolkata", "UK": "Europe/London", "US": "America/New_York", "US_W": "America/Los_Angeles",
    "SG": "Asia/Singapore", "ME": "Asia/Dubai", "EU": "Europe/Amsterdam", "AU": "Australia/Sydney",
}
_REGION_MARKERS = (
    ("IN", INDIA_MARKERS),
    ("UK", ("united kingdom", "england", "london", "manchester", "edinburgh", "leeds", "scotland")),
    ("US_W", ("san francisco", "bay area", "california", "los angeles", "seattle", "palo alto")),
    ("US", ("united states", "new york", "boston", "chicago", "washington", "usa", "texas", "atlanta")),
    ("SG", ("singapore",)),
    ("ME", ("dubai", "abu dhabi", "united arab emirates", "uae", "saudi", "riyadh", "qatar", "doha")),
    ("AU", ("australia", "sydney", "melbourne")),
    ("EU", ("germany", "berlin", "france", "paris", "netherlands", "amsterdam", "switzerland", "zurich",
            "spain", "madrid", "italy", "milan", "ireland", "dublin", "sweden", "stockholm")),
)


def detect_region(location: str) -> str:
    loc = " " + (location or "").lower() + " "
    for region, markers in _REGION_MARKERS:
        if _has(loc, markers):
            return region
    if "remote" in loc:
        return "REMOTE"
    return "OTHER"


def lead_timezone(lead: Lead) -> str:
    """Unknown/remote locations default to IST: the ICP is India-first (~60% of leads)."""
    region = lead.region or detect_region(lead.person.location)
    return REGION_TZ.get(region, "Asia/Kolkata")
