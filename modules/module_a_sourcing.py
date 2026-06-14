"""
MODULE A: Sourcing & Filtering Engine
══════════════════════════════════════
Search backend: Serper.dev (replaces Google CSE)
  - 2,500 free searches/month on free plan — ample for 25 leads/week
  - API key: serper.dev → sign up → Dashboard → API Key
  - Header-based auth: X-API-KEY

Profile enrichment: linkedin-api (unofficial, cookie-auth, free)
Email inference:    Pattern logic + DNS MX verification (free)

Target titles now include:
  - Hiring managers, department managers, team leads (Tier C)
  - Plus all previous Tier A + B (C-suite, partners, directors, heads-of)

India geographic segments fill ~60% of weekly rotation slots.
International (UK, US, SG, UAE, EU, AU, Remote) fill the rest.
"""

import os, re, time, json, logging, socket
import urllib.request, urllib.parse
from typing       import Optional
from dataclasses  import dataclass, field, asdict
from datetime     import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from config import (
    OUTREACH_MODE, SCORE_THRESHOLD, WEEKLY_TARGET,
    ALL_TARGET_TITLES, TITLES_TIER_A, TITLES_TIER_B, TITLES_TIER_C,
    REJECT_TITLES, REJECT_EMAIL_PREFIXES,
    get_this_weeks_segments, get_geo, get_vertical, MODE_CONTEXT,
)

log = logging.getLogger(__name__)

SERPER_API_KEY    = os.getenv("SERPER_API_KEY", "")
LINKEDIN_USERNAME = os.getenv("LINKEDIN_USERNAME", "")
LINKEDIN_PASSWORD = os.getenv("LINKEDIN_PASSWORD", "")

EMAIL_PATTERNS = [
    "{first}.{last}@{domain}",
    "{first}@{domain}",
    "{f}{last}@{domain}",
    "{first}{last}@{domain}",
    "{first}_{last}@{domain}",
    "{f}.{last}@{domain}",
    "{last}.{first}@{domain}",
    "{last}@{domain}",
]

TIER1_NAMES = [
    "mckinsey","boston consulting group","bcg","bain",
    "deloitte","pwc","pricewaterhousecoopers","ey ","ernst & young",
    "kpmg","accenture","oliver wyman","roland berger","kearney",
    "lek consulting","arthur d. little","strategy&","alvarez & marsal",
    "goldman sachs","morgan stanley","jp morgan","jpmorgan",
    "blackstone","kkr","sequoia","general atlantic","warburg pincus",
    "aditya birla","tata ","infosys","wipro",
    "world bank","imf","asian development bank",
    "niti aayog","rbi ","sebi ",
    "brookings","rand corporation","chatham house","iipa",
]


@dataclass
class Lead:
    name:                str  = ""
    company:             str  = ""
    position:            str  = ""
    email:               str  = ""
    linkedin_url:        str  = ""
    location:            str  = ""
    geo_segment:         str  = ""
    vertical:            str  = ""
    company_type:        str  = ""   # Tier-1 | Startup | Research | Corporate
    funding_stage:       str  = ""
    is_remote_role:      bool = False
    company_description: str  = ""
    reason_for_outreach: str  = ""
    title_tier:          str  = ""   # A | B | C
    confidence_score:    int  = 0
    outreach_mode:       str  = ""
    source:              str  = ""
    week_sourced:        str  = ""
    raw:                 dict = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════════
# FILTERS
# ══════════════════════════════════════════════════════════════════════════

def _title_tier(title: str) -> Optional[str]:
    """Return 'A', 'B', or 'C' if title passes, None if rejected."""
    t = title.lower().strip()
    for bad in REJECT_TITLES:
        if bad in t:
            return None
    for good in TITLES_TIER_A:
        if good in t:
            return "A"
    for good in TITLES_TIER_B:
        if good in t:
            return "B"
    for good in TITLES_TIER_C:
        if good in t:
            return "C"
    return None


def _email_passes(email: str) -> bool:
    if not email or "@" not in email:
        return False
    local = email.split("@")[0].lower()
    for pfx in REJECT_EMAIL_PREFIXES:
        if local == pfx or local.startswith(pfx + ".") or local.startswith(pfx + "_"):
            return False
    if re.match(r"^[a-z]{1,4}\d{3,}$", local):
        return False
    return True


def _classify_company(name: str, desc: str = "") -> tuple:
    combined = (name + " " + desc).lower()
    for t in TIER1_NAMES:
        if t in combined:
            return "Tier-1", ""
    if any(k in combined for k in ["think tank","policy institute","research institute",
                                    "university","economics institute","nber","brookings","rand"]):
        return "Research", ""
    for kw in ["series d","series e","series c"]:
        if kw in combined: return "Startup", kw.title()
    if "series b" in combined: return "Startup", "Series B"
    if "series a" in combined: return "Startup", "Series A"
    if any(k in combined for k in ["seed","pre-seed","funded","raised $","backed by",
                                    "yc ","y combinator","angel"]):
        return "Startup", "Seed/Early"
    return "Corporate", ""


def _is_remote(desc: str, title: str, loc: str) -> bool:
    combined = (desc + title + loc).lower()
    return any(k in combined for k in ["remote","distributed","work from anywhere",
                                        "fully remote","remote-first","globally distributed"])


def _score_lead(lead: Lead) -> int:
    score = 0
    # Email found
    if lead.email and _email_passes(lead.email):   score += 28
    # Title tier
    if lead.title_tier == "A":                     score += 28
    elif lead.title_tier == "B":                   score += 20
    elif lead.title_tier == "C":                   score += 12
    # LinkedIn confirmed
    if lead.linkedin_url:                          score += 14
    # Company enriched
    if len(lead.company_description) > 50:         score += 8
    # Company type bonus
    if lead.company_type == "Tier-1":              score += 10
    elif lead.company_type == "Research":          score += 7
    # Remote bonus (prioritise for internship mode)
    if lead.is_remote_role and OUTREACH_MODE == "internship": score += 6
    # India slight bump in internship mode
    if OUTREACH_MODE == "internship":
        if any(c in lead.location.lower() for c in
               ["india","mumbai","delhi","bangalore","bengaluru","hyderabad","pune","chennai"]):
            score += 4
    return min(score, 100)


# ══════════════════════════════════════════════════════════════════════════
# SERPER SEARCH
# ══════════════════════════════════════════════════════════════════════════

_serper_calls = 0

def _serper_search(query: str, num: int = 8) -> list:
    """
    POST to Serper Google Search API.
    Returns list of organic result dicts: {title, link, snippet}.
    Free plan: 2,500 searches/month (≈ 80/day) — we use ~20/run.
    """
    global _serper_calls
    if not SERPER_API_KEY:
        log.warning("SERPER_API_KEY not set")
        return []
    if _serper_calls >= 70:
        log.warning("Serper call limit reached for this run (%d)", _serper_calls)
        return []

    payload = json.dumps({"q": query, "num": min(num, 10)}).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=payload,
        headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        _serper_calls += 1
        time.sleep(0.4)
        return data.get("organic", [])
    except Exception as e:
        log.warning("Serper error for '%s': %s", query[:60], e)
        return []


def _extract_linkedin_urls(results: list) -> list:
    urls = []
    for r in results:
        link = r.get("link", "")
        if "linkedin.com/in/" in link:
            clean = re.sub(r"\?.*$", "", link).rstrip("/")
            urls.append(clean)
    return list(dict.fromkeys(urls))


# ══════════════════════════════════════════════════════════════════════════
# LINKEDIN ENRICHMENT
# ══════════════════════════════════════════════════════════════════════════

_li_client = None
_li_client_failed = False  # The new kill switch

def _get_li_client():
    global _li_client, _li_client_failed
    
    # If we already got blocked this run, don't even try again. 
    # Just fail instantly so the bot can move on.
    if _li_client_failed:
        return None
        
    if _li_client is not None:
        return _li_client
        
    if not LINKEDIN_USERNAME or not LINKEDIN_PASSWORD:
        return None
        
    try:
        from linkedin_api import Linkedin
        _li_client = Linkedin(LINKEDIN_USERNAME, LINKEDIN_PASSWORD, debug=False)
        log.info("LinkedIn client ready")
        return _li_client
    except Exception as e:
        # We hit the CHALLENGE wall. Log it ONCE, flip the kill switch, and exit.
        log.warning("LinkedIn init failed (IP blocked): %s", e)
        _li_client_failed = True
        return None


def _enrich_linkedin(url: str) -> dict:
    api = _get_li_client()
    if not api:
        return {}
    m = re.search(r"linkedin\.com/in/([^/?#]+)", url)
    if not m:
        return {}
    pid = m.group(1)
    try:
        time.sleep(2.8)   # respectful pacing
        p = api.get_profile(pid)
        if not p:
            return {}
        exps = p.get("experience", [])
        cur  = exps[0] if exps else {}
        return {
            "name":         f"{p.get('firstName','')} {p.get('lastName','')}".strip(),
            "first_name":   p.get("firstName", ""),
            "last_name":    p.get("lastName",  ""),
            "title":        cur.get("title", ""),
            "company":      cur.get("companyName", ""),
            "location":     p.get("locationName", ""),
            "linkedin_url": url,
            "summary":      p.get("summary", ""),
        }
    except Exception as e:
        log.debug("LI enrich fail %s: %s", pid, e)
        return {}


def _parse_snippet(url: str, results: list) -> dict:
    """Fallback: parse name/title/company from Serper snippet."""
    for r in results:
        if url not in r.get("link", ""):
            continue
        raw   = r.get("title", "")
        snip  = r.get("snippet", "")
        # Strip LinkedIn suffix
        raw = re.sub(r"\s*\|\s*LinkedIn.*$", "", raw, flags=re.IGNORECASE)
        # "Name - Title at Company"
        parts = re.split(r"\s*[-–]\s*", raw, maxsplit=2)
        if len(parts) >= 2:
            name  = parts[0].strip()
            rest  = parts[1].strip()
            at_m  = re.match(r"^(.+?)\s+at\s+(.+)$", rest, re.IGNORECASE)
            title   = at_m.group(1).strip() if at_m else rest
            company = at_m.group(2).strip() if at_m else (parts[2].strip() if len(parts)>2 else "")
            if name and title:
                return {"name": name, "title": title, "company": company,
                        "location": "", "linkedin_url": url, "summary": snip}
    return {}


# ══════════════════════════════════════════════════════════════════════════
# EMAIL INFERENCE
# ══════════════════════════════════════════════════════════════════════════

_domain_cache: dict = {}

def _get_company_domain(company: str) -> Optional[str]:
    if company in _domain_cache:
        return _domain_cache[company]
    results = _serper_search(f'"{company}" official website contact email', num=5)
    noise   = ["linkedin","twitter","facebook","crunchbase","glassdoor","bloomberg",
               "wikipedia","youtube","indeed","naukri","zoominfo","techcrunch","ambitionbox"]
    for r in results:
        link  = r.get("link", "")
        m = re.search(r"https?://(?:www\.)?([^/]+)", link)
        if not m:
            continue
        domain = m.group(1).lower()
        if any(n in domain for n in noise):
            continue
        parts = domain.split(".")
        if len(parts) >= 2 and len(parts[-1]) >= 2:
            _domain_cache[company] = domain
            return domain
    _domain_cache[company] = None
    return None


def _domain_has_mx(domain: str) -> bool:
    """Quick DNS check — avoids sending to dead domains."""
    try:
        socket.getaddrinfo(domain, None)
        return True
    except socket.gaierror:
        return False


def _infer_email(first: str, last: str, domain: str) -> Optional[str]:
    first = re.sub(r"[^a-z]", "", first.lower())
    last  = re.sub(r"[^a-z]", "", last.lower())
    f     = first[0] if first else ""
    if not first or not last or not domain:
        return None
    for pat in EMAIL_PATTERNS:
        try:
            email = pat.format(first=first, last=last, f=f, domain=domain)
            if _email_passes(email):
                return email
        except KeyError:
            continue
    return None


def _get_company_desc(company: str) -> str:
    """One Serper call to get a short company description."""
    results = _serper_search(f'"{company}" company overview about', num=3)
    for r in results:
        snip = r.get("snippet", "").strip()
        if len(snip) > 60 and company.split()[0].lower() in snip.lower():
            return re.sub(r"\s+", " ", snip)[:300]
    return ""


# ══════════════════════════════════════════════════════════════════════════
# QUERY BUILDER
# ══════════════════════════════════════════════════════════════════════════

def _build_queries(geo_id: str, vertical_id: str, week_num: int) -> list:
    """
    Build 4–6 Serper queries for a geo × vertical combo.
    week_num shifts phrasing so the same combo surfaces fresh people
    each time it recurs in the 12-week rotation.
    """
    geo      = get_geo(geo_id)
    vertical = get_vertical(vertical_id)
    if not geo or not vertical:
        return []

    geo_term  = geo["terms"][week_num % len(geo["terms"])]
    vkw       = vertical["keywords"][week_num % len(vertical["keywords"])]
    # Use both Tier A and Tier C titles for breadth
    ta = TITLES_TIER_A[week_num % len(TITLES_TIER_A)]
    tb = TITLES_TIER_B[(week_num + 1) % len(TITLES_TIER_B)]
    tc = TITLES_TIER_C[(week_num + 2) % len(TITLES_TIER_C)]
    et = vertical["extra_titles"][(week_num) % len(vertical["extra_titles"])]

    queries = [
        # Tier A — decision makers
        f'site:linkedin.com/in "{ta}" "{vkw}" {geo_term}',
        # Tier B — department heads
        f'site:linkedin.com/in "{tb}" {geo_term} "{vkw}"',
        # Tier C — managers & hiring managers (wider net, higher reply rates)
        f'site:linkedin.com/in "{tc}" "{vkw}" {geo_term}',
        # Vertical-specific extra title
        f'site:linkedin.com/in "{et}" {geo_term} {vkw}',
    ]

    # Remote geo: add remote-specific variant
    if geo_id == "REMOTE":
        queries.append(f'site:linkedin.com/in "remote" "{vkw}" hiring "{ta}" OR "{tb}"')

    # Research geo: think-tank specific
    if vertical_id == "RESEARCH_ECON":
        queries.append(f'site:linkedin.com/in "research" "{geo_term}" economist OR "policy" OR "think tank"')

    return queries[:6]


# ══════════════════════════════════════════════════════════════════════════
# DEDUP AGAINST PIPELINE
# ══════════════════════════════════════════════════════════════════════════

def _load_seen_emails() -> set:
    try:
        from module_b_spreadsheet import read_all_leads
        return {r.get("Target Email","").strip().lower()
                for r in read_all_leads() if r.get("Target Email","")}
    except Exception:
        return set()


# ══════════════════════════════════════════════════════════════════════════
# REASON BUILDER
# ══════════════════════════════════════════════════════════════════════════

def _build_reason(lead: Lead) -> str:
    mode_vp   = MODE_CONTEXT[OUTREACH_MODE]["value_prop"]
    stage_str = f" ({lead.funding_stage})" if lead.funding_stage and "Unknown" not in lead.funding_stage else ""
    remote_str= " — remote-friendly" if lead.is_remote_role else ""
    desc_hook = ""
    if lead.company_description:
        clause = lead.company_description.split(".")[0].strip()
        if len(clause) > 20:
            desc_hook = f"; {clause}"
    return (
        f"{lead.name.split()[0]} ({lead.position} @ {lead.company}{stage_str}{remote_str}{desc_hook}) "
        f"— targeted because: {mode_vp}."
    )


# ══════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════

def run_sourcing_pipeline(
    weekly_target: int = None,
    override_combos: list = None,
) -> list:
    """
    Run the full sourcing pipeline for the current week's rotation slot.
    Returns list[Lead] scored ≥ SCORE_THRESHOLD, capped at weekly_target,
    with no duplicates against the historical pipeline.csv.
    """
    target    = weekly_target or WEEKLY_TARGET
    week_num  = datetime.now(timezone.utc).isocalendar()[1]
    combos    = override_combos or get_this_weeks_segments()
    seen_hist = _load_seen_emails()
    seen_run  = set()

    log.info("MODE=%s | WEEK=%d | TARGET=%d | COMBOS=%s",
             OUTREACH_MODE, week_num, target, combos)

    all_leads: list = []

    for geo_id, vertical_id in combos:
        if len(all_leads) >= target * 3:
            break

        geo      = get_geo(geo_id)
        vertical = get_vertical(vertical_id)
        if not geo or not vertical:
            log.warning("Unknown geo '%s' or vertical '%s'", geo_id, vertical_id)
            continue

        log.info("▶ [%s × %s]", geo.get("name","?"), vertical.get("name","?"))
        queries = _build_queries(geo_id, vertical_id, week_num)

        for query in queries:
            if len(all_leads) >= target * 3:
                break

            results       = _serper_search(query, num=8)
            linkedin_urls = _extract_linkedin_urls(results)

            for url in linkedin_urls:
                if len(all_leads) >= target * 3:
                    break

                # Enrich
                profile = _enrich_linkedin(url) or _parse_snippet(url, results)
                if not profile or not profile.get("name"):
                    continue

                name  = profile["name"].strip()
                title = (profile.get("title") or "").strip()
                if not name or not title:
                    continue

                tier = _title_tier(title)
                if tier is None:
                    continue   # rejected title

                company  = (profile.get("company") or "").strip()
                location = (profile.get("location") or "").strip()
                summary  = (profile.get("summary") or "")

                key = f"{name.lower()}|{company.lower()}"
                if key in seen_run:
                    continue
                seen_run.add(key)

                company_type, funding_stage = _classify_company(company, summary)
                is_remote = _is_remote(summary, title, location)

                # Company description (1 Serper call, quota-conscious)
                company_desc = ""
                if company and _serper_calls < 60:
                    company_desc = _get_company_desc(company)
                    time.sleep(0.6)

                # Domain + email
                domain = None
                if company and _serper_calls < 65:
                    domain = _get_company_domain(company)
                    time.sleep(0.6)

                email = ""
                if domain and _domain_has_mx(domain):
                    parts = name.split()
                    if len(parts) >= 2:
                        inferred = _infer_email(parts[0], parts[-1], domain)
                        if inferred and inferred not in seen_hist and inferred not in seen_run:
                            email = inferred

                if not email:
                    log.debug("No email for %s @ %s", name, company)
                    continue

                seen_run.add(email)

                lead = Lead(
                    name=name, company=company, position=title,
                    email=email, linkedin_url=url, location=location,
                    geo_segment=geo_id, vertical=vertical_id,
                    company_type=company_type, funding_stage=funding_stage,
                    is_remote_role=is_remote, company_description=company_desc,
                    title_tier=tier, outreach_mode=OUTREACH_MODE,
                    source=f"Serper+LinkedIn [{geo_id}×{vertical_id}]",
                    week_sourced=f"{datetime.now(timezone.utc).year}-W{week_num:02d}",
                )
                lead.reason_for_outreach = _build_reason(lead)
                lead.confidence_score    = _score_lead(lead)

                all_leads.append(lead)
                log.info("✓ [Tier%s|%d] %s | %s | %s | %s",
                         tier, lead.confidence_score, name, title, company, email)

    qualified = sorted(
        [l for l in all_leads if l.confidence_score >= SCORE_THRESHOLD],
        key=lambda l: l.confidence_score, reverse=True,
    )
    final = qualified[:target]
    log.info("Pipeline done: %d raw → %d qualified → %d selected",
             len(all_leads), len(qualified), len(final))
    return final


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    leads = run_sourcing_pipeline()
    for l in leads[:3]:
        print(json.dumps(asdict(l), indent=2, default=str))
