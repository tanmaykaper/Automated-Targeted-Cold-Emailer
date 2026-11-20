"""Company → email domain resolution with ownership checks.

v1 took the first search hit that wasn't on a short noise list, which is how leads ended up
emailed at x.com, whitehouse.gov, support.linkedhelper.com and rocketreach.co. Here a domain
is only accepted if its label matches the company name (or an acronym of it), or the site's
own title contains the company name, and it is never an aggregator/social/news/gov site.
"""
from __future__ import annotations

import logging
import re
from typing import Callable

from ..models import Lead, norm_company
from ..web import Search

log = logging.getLogger(__name__)

BLOCKED_DOMAINS = {
    "linkedin.com", "x.com", "twitter.com", "facebook.com", "instagram.com", "youtube.com", "tiktok.com",
    "crunchbase.com", "zoominfo.com", "rocketreach.co", "signalhire.com", "apollo.io", "lusha.com",
    "contactout.com", "glassdoor.com", "glassdoor.co.in", "indeed.com", "naukri.com", "ambitionbox.com",
    "wikipedia.org", "bloomberg.com", "reuters.com", "forbes.com", "economictimes.com",
    "economictimes.indiatimes.com", "indiatimes.com", "livemint.com", "moneycontrol.com", "techcrunch.com",
    "yourstory.com", "inc42.com", "medium.com", "substack.com", "reddit.com", "quora.com", "threads.net",
    "github.io", "github.com", "blogspot.com", "wordpress.com", "wix.com", "tracxn.com", "pitchbook.com",
    "owler.com", "dnb.com", "craft.co", "cbinsights.com", "companieshouse.gov.uk", "find-and-update.company-information.service.gov.uk",
    "zaubacorp.com", "tofler.in", "linkedhelper.com", "connectsafely.ai", "salezshark.com", "falconebiz.com",
    "privateequityinternational.com", "recruitingdaily.com", "healthtechmagazine.net", "builtin.com",
    "theorg.com", "wellfound.com", "angel.co", "f6s.com", "clutch.co", "goodfirms.co", "g2.com",
    "amazon.com", "aws.amazon.com", "google.com", "about.google", "microsoft.com", "apple.com",
    "adobeaemcloud.com", "login.gov", "whitehouse.gov", "gov.in", "nic.in", "slideshare.net", "scribd.com",
    "issuu.com", "prnewswire.com", "businesswire.com", "globenewswire.com", "eventbrite.com", "meetup.com",
}
# Some companies really do own these (google.com for Google); allow when the company name matches exactly.
_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "ltd", "plc"}
_GENERIC = {"and", "the", "group", "global", "india", "solutions", "services", "technologies", "technology",
            "consulting", "partners", "capital", "ventures", "advisors", "advisory", "associates",
            "international", "holdings", "labs", "data", "analytics", "digital", "systems", "management"}


def registrable(host: str) -> str:
    host = (host or "").lower().strip().strip(".")
    host = re.sub(r"^https?://", "", host).split("/")[0].split(":")[0]
    host = re.sub(r"^www\d?\.", "", host)
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def is_blocked(domain: str) -> bool:
    d = registrable(domain)
    return d in BLOCKED_DOMAINS or any(d.endswith("." + b) or d == b for b in BLOCKED_DOMAINS) or \
        d.endswith(".gov") or ".gov." in d


def allowed_for(company: str, domain: str) -> bool:
    """Blocked domains are allowed only for the company that owns them (Google → google.com)."""
    return bool(domain) and (not is_blocked(domain) or name_match_score(company, domain) >= 1.0)


def name_tokens(company: str) -> list[str]:
    return [t for t in norm_company(company).split() if len(t) >= 2]


def name_match_score(company: str, domain: str) -> float:
    """0..1 — how plausibly `domain` belongs to `company`, from the name alone."""
    label = registrable(domain).split(".")[0].replace("-", "")
    aliases = [norm_company(a).replace(" ", "") for a in re.findall(r"\(([^)]+)\)", company or "")]
    toks = name_tokens(re.sub(r"\([^)]*\)", " ", company or ""))
    if not label or not toks:
        return 0.0
    if label in aliases or (label in toks and len(label) >= 2 and label not in _GENERIC):
        return 0.9
    joined = "".join(toks)
    core = [t for t in toks if t not in _GENERIC] or toks
    acronym = "".join(t[0] for t in toks)
    if label == joined or label == "".join(core):
        return 1.0
    if len(acronym) >= 2 and label in (acronym, "".join(t[0] for t in core)):
        return 0.85
    if label.startswith(core[0]) and len(core[0]) >= 4:
        return 0.85
    if joined.startswith(label) and len(label) >= 4:
        return 0.8
    if any(len(t) >= 4 and t in label for t in core):
        return 0.7
    return 0.0


def resolve_domain(lead: Lead, search: Search, has_mx: Callable[[str], bool] = lambda d: True) -> tuple[str, float, str]:
    """(domain, confidence, source). Empty domain when nothing trustworthy is found.
    Search-derived domains must name-match the company AND have mail exchangers."""
    c = lead.company
    if c.domain and allowed_for(c.name, c.domain) and c.domain_confidence >= 0.6:
        return registrable(c.domain), c.domain_confidence, c.domain_source or "input"
    if c.website and allowed_for(c.name, registrable(c.website)):
        return registrable(c.website), 0.9, "input:website"
    # Email domains from trustworthy candidates (provider-verified, non-webmail)
    for cand in lead.candidates:
        if cand.provider_status in ("verified", "valid") and "@" in cand.email:
            d = cand.email.split("@", 1)[1]
            if allowed_for(c.name, d) and d not in WEBMAIL and name_match_score(c.name, d) >= 0.7:
                return registrable(d), 0.85, f"email:{cand.source}"
    # URLs mentioned in an existing company description ("Website: https://www.amarapt.com/")
    for host in re.findall(r"https?://(?:www\.)?([a-z0-9.-]+\.[a-z]{2,})", c.description.lower()):
        d = registrable(host)
        if allowed_for(c.name, d) and name_match_score(c.name, d) >= 0.7 and has_mx(d):
            return d, 0.8, "description"
    if not c.name or c.name.endswith(("...", "…")) or norm_company(c.name) in ("", "linkedin", "self employed", "freelance"):
        return "", 0.0, "company name unusable"
    results = search.search(f'"{c.name}" official website', num=8)
    scored: dict[str, float] = {}
    core = [t for t in name_tokens(c.name) if t not in _GENERIC] or name_tokens(c.name)
    for r in results:
        dom = registrable(r.url)
        if not allowed_for(c.name, dom):
            continue
        score = name_match_score(c.name, dom)
        text = (r.title + " " + r.snippet).lower()
        if core and all(t in text for t in core[:2]):
            score = max(score, 0.6) + 0.1
        scored[dom] = max(scored.get(dom, 0.0), min(score, 1.0))
    for dom, score in sorted(scored.items(), key=lambda kv: -kv[1]):
        if score < 0.6:
            break
        if has_mx(dom):
            return dom, round(score, 2), "search"
    best = max(scored.values(), default=0.0)
    return "", best, "no mail-capable domain matched the company name"


WEBMAIL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com",
           "live.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "rediffmail.com",
           "zoho.com", "yandex.com", "gmx.com", "mail.com"}
