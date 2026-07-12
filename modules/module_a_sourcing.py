"""
MODULE A: Sourcing & Filtering Engine
══════════════════════════════════════
Search backend: Serper.dev (replaces Google CSE)
  - 2,500 free searches/month on free plan — ample for 25 leads/week
  - API key: serper.dev → sign up → Dashboard → API Key
  - Header-based auth: X-API-KEY
  - Two sources per geo×vertical combo:
      1. /search  — site:linkedin.com/in organic results (scraped + parsed)
      2. /people  — structured profile search, server-side entity resolution
                    (higher hit rate on correctly attributing name/title/company;
                    falls back silently to [] if not on the account's plan)

Profile enrichment: linkedin-api (unofficial, cookie-auth, free)
  - Kill switch flips on ANY confirmed CHALLENGE/checkpoint/rate-limit signal,
    whether it surfaces at client init OR mid-run during get_profile() —
    both paths now short-circuit the rest of the run instead of retrying
    into a wall.

Email inference & verification (free, layered):
  1. Pattern generation, ranked by statistical likelihood (first.last@ first)
  2. Structural validation — RFC-adjacent regex + explicit typo-TLD blocklist
     (.con, .cmo, single-char TLDs, numeric TLDs all rejected before they
     ever reach DNS/SMTP)
  3. MX record lookup (not just bare DNS resolution — a domain can resolve
     and still have no mail service)
  4. SMTP RCPT TO probe (best-effort) — confirms a specific mailbox exists
     where the receiving server allows it; detects catch-all domains and
     adapts instead of false-confirming every candidate
  Net effect: fewer wrong-person sends, fewer hard bounces, better odds the
  email actually reaches — and gets opened by — the intended recipient.

  Requires: dnspython (pip install dnspython) for proper MX resolution.
  Degrades to plain DNS resolution with a loud warning if not installed.

Target titles now include:
  - Hiring managers, department managers, team leads (Tier C)
  - Plus all previous Tier A + B (C-suite, partners, directors, heads-of)

India geographic segments fill ~60% of weekly rotation slots.
International (UK, US, SG, UAE, EU, AU, Remote) fill the rest.
"""

import os, re, time, json, logging, socket
import urllib.request, urllib.parse, urllib.error
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
    REJECT_TITLES, REJECT_EMAIL_PREFIXES, REJECT_TLDS,
    SMTP_VERIFY_ENABLED, SMTP_VERIFY_TIMEOUT, SMTP_HELO_DOMAIN,
    get_this_weeks_segments, get_geo, get_vertical, MODE_CONTEXT,
)

log = logging.getLogger(__name__)

SERPER_API_KEY    = os.getenv("SERPER_API_KEY", "")
LINKEDIN_USERNAME = os.getenv("LINKEDIN_USERNAME", "")
LINKEDIN_PASSWORD = os.getenv("LINKEDIN_PASSWORD", "")

# Email resolution API keys — each is a free-tier service.
# The waterfall tries them in order; first verified hit wins.
# See _resolve_email_waterfall() for the full priority logic.
HUNTER_API_KEY  = os.getenv("HUNTER_API_KEY",  "")   # hunter.io          — 25 searches/mo free
PROSPEO_API_KEY = os.getenv("PROSPEO_API_KEY", "")   # prospeo.com        — 75 lookups/mo free; strong India coverage
SNOV_CLIENT_ID  = os.getenv("SNOV_CLIENT_ID",  "")   # snov.io            — 50 credits/mo free
SNOV_CLIENT_SECRET = os.getenv("SNOV_CLIENT_SECRET", "")
APOLLO_API_KEY  = os.getenv("APOLLO_API_KEY",  "")   # apollo.io          — 600 credits/mo free on basic
ANYMAILFINDER_KEY = os.getenv("ANYMAILFINDER_KEY", "") # anymailfinder.com — 90 lookups/mo free; good for .in domains

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

# ──────────────────────────────────────────────────────────────────────────
# TLD VALIDATION
# Real-world bad data we've actually seen come out of scraped/inferred
# emails: typo'd TLDs (.con, .cmo), single-char TLDs from truncated scrapes,
# and numeric-only TLDs from malformed URLs. A regex alone won't catch a
# typo'd-but-structurally-valid TLD like ".con" — that needs an explicit
# blocklist, since ".con" passes any generic [a-z]{2,} check.
#
# The blocklist itself (REJECT_TLDS) now lives in config.py so a newly
# noticed typo pattern gets added in one place rather than drifting between
# modules.
# ──────────────────────────────────────────────────────────────────────────
_TYPO_TLDS = set(REJECT_TLDS)

# Legitimate TLDs we expect to actually see for this use case (corporate +
# common ccTLDs). Anything outside this set isn't auto-rejected — it just
# skips the "known-good" fast path and goes through stricter regex + MX checks.
_COMMON_GOOD_TLDS = {
    "com", "org", "net", "io", "co", "ai", "in", "uk", "us", "ca", "au",
    "sg", "ae", "de", "fr", "nl", "ch", "eu", "info", "biz", "tech",
    "capital", "ventures", "vc", "consulting", "partners", "global",
}

def _tld_is_valid(domain: str) -> bool:
    """Reject typo'd, truncated, or structurally-bogus TLDs before we
    ever bother forming candidate emails or hitting DNS/SMTP."""
    if not domain or "." not in domain:
        return False
    parts = domain.rsplit(".", 1)
    if len(parts) != 2:
        return False
    tld = parts[1].lower()

    if tld in _TYPO_TLDS:
        return False
    if len(tld) < 2:                       # single-char TLD — always bogus
        return False
    if tld.isdigit():                      # numeric TLD — malformed scrape
        return False
    if not re.match(r"^[a-z]{2,24}$", tld): # letters only, sane length
        return False
    return True


_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9._%+\-]{0,63}@"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)+$"
)

def _email_structurally_valid(email: str) -> bool:
    """RFC-adjacent structural check, stricter than '@' in email.
    Catches double dots, leading/trailing dots in local part, missing
    domain label, and bogus TLDs in one pass."""
    if not email or len(email) > 254:
        return False
    if not _EMAIL_RE.match(email):
        return False
    if ".." in email:
        return False
    local, domain = email.rsplit("@", 1)
    if local.startswith(".") or local.endswith("."):
        return False
    if not _tld_is_valid(domain):
        return False
    return True

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
    email_verified:      bool = False   # True = came from a lookup API; False = pattern-guessed
    email_source:        str  = ""      # "hunter" | "prospeo" | "snov" | "apollo" | "anymailfinder" | "pattern"
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
    if not _email_structurally_valid(email):
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

# Budget: free plan = 2,500/month ≈ 80/day. A single run now spans two
# sources (organic site:linkedin.com/in + /people) across up to ~5 geo×vertical
# combos, so the old ceiling of 70 was cutting runs short before the people-
# search pass got a turn. 95 leaves headroom under the ~80/day average while
# still capping any single run from exhausting the monthly pool.
_SERPER_CALL_CEILING = 95

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
    if _serper_calls >= _SERPER_CALL_CEILING:
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


def _serper_people_search(query: str, num: int = 10) -> list:
    """
    POST to Serper's dedicated People Search endpoint, which returns
    structured profile cards (name, title, company, location, profile link)
    instead of raw organic search snippets — meaningfully higher hit rate
    for resolving the right person than scraping `site:linkedin.com/in`
    result titles, since Serper does the entity resolution server-side.

    Falls back silently to [] if the endpoint isn't available on the
    account's plan (it's a newer addition and not on every tier) — callers
    should treat this as a supplementary source, not a required one.
    """
    global _serper_calls
    if not SERPER_API_KEY:
        return []
    if _serper_calls >= _SERPER_CALL_CEILING:
        log.warning("Serper call limit reached for this run (%d)", _serper_calls)
        return []

    payload = json.dumps({"q": query, "num": min(num, 10)}).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/people",
        data=payload,
        headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        _serper_calls += 1
        time.sleep(0.4)
        # Observed response shape: {"people": [{"name","title","company","location","link",...}]}
        # Degrade gracefully if Serper changes the key name on this endpoint.
        return data.get("people", data.get("organic", []))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log.debug("Serper /people endpoint not available on this plan — skipping")
        else:
            log.warning("Serper people search HTTP %d for '%s'", e.code, query[:60])
        return []
    except Exception as e:
        log.warning("Serper people search error for '%s': %s", query[:60], e)
        return []


def _people_result_to_profile(item: dict) -> dict:
    """Normalizes a Serper /people result into the same shape _enrich_linkedin
    and _parse_snippet produce, so downstream code doesn't care which
    source a lead came from."""
    name     = (item.get("name") or "").strip()
    title    = (item.get("title") or item.get("position") or "").strip()
    company  = (item.get("company") or item.get("organization") or "").strip()
    location = (item.get("location") or "").strip()
    link     = (item.get("link") or item.get("profileUrl") or item.get("url") or "")
    summary  = (item.get("snippet") or item.get("about") or "")

    return {
        "name": name, "title": title, "company": company,
        "location": location, "linkedin_url": link, "summary": summary,
    }


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


_LI_CHALLENGE_MARKERS = (
    "challenge", "captcha", "checkpoint", "security verification",
    "unauthorized", "401", "403", "rate limit", "too many requests",
)

def _looks_like_challenge(exc: Exception) -> bool:
    """linkedin-api doesn't raise a typed ChallengeException for every block —
    a lot of it surfaces as a generic Exception/HTTPError with marker text
    in the message. Catch on message content rather than exception type."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _LI_CHALLENGE_MARKERS)


def _enrich_linkedin(url: str) -> dict:
    global _li_client, _li_client_failed

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

        # linkedin-api sometimes returns {} instead of raising when a
        # challenge/checkpoint is served mid-session. An empty payload alone
        # isn't proof of a block (could be a private/deleted profile), so we
        # don't flip the kill switch on this branch — only on a confirmed
        # exception below.
        if not p:
            log.debug("LI returned empty payload for %s — profile may be private/deleted", pid)
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
        if _looks_like_challenge(e):
            # Confirmed block mid-run. Flip the SAME kill switch _get_li_client
            # checks, so every subsequent _enrich_linkedin() call this run
            # short-circuits instantly instead of retrying into a wall.
            log.warning("LI CHALLENGE during get_profile(%s) — killing LI client for rest of run: %s", pid, e)
            _li_client_failed = True
            _li_client = None
        else:
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

# ══════════════════════════════════════════════════════════════════════════
# EMAIL RESOLUTION — 5-TIER VERIFIED WATERFALL
# ══════════════════════════════════════════════════════════════════════════
# The old approach (pattern-guess first.last@domain then SMTP-probe) gave
# ~20-30% accuracy for Indian boutique firms and startups where email
# conventions are non-standard. The screenshot showed 7/7 bounces.
#
# New priority order — first verified hit wins, pattern guessing is a last
# resort that sets email_verified=False and prevents auto-send:
#
#   Tier 1: Hunter.io   /email-finder   (person-level lookup, confidence score)
#   Tier 2: Prospeo     /email-finder   (strong India coverage, 75 free/mo)
#   Tier 3: AnyMailFinder               (good for .in domains, 90 free/mo)
#   Tier 4: Snov.io     /get-emails-by-name (50 free credits/mo)
#   Tier 5: Apollo.io   /people/match   (600 free credits/mo, person-level)
#   Tier 6: Pattern inference           (email_verified=False → manual review queue)
#
# Each tier is skipped silently if its API key is not configured, so the
# pipeline degrades gracefully to whatever keys you have.
# ══════════════════════════════════════════════════════════════════════════

_domain_cache: dict = {}
_snov_token_cache: dict = {}   # {"token": str, "expires": float}

# ── Tier 1: Hunter.io Email Finder ────────────────────────────────────────
def _hunter_find_email(first: str, last: str, domain: str) -> Optional[tuple]:
    """
    Returns (email, confidence_int) or None.
    Hunter's /email-finder takes first name, last name, and domain and returns
    the most likely email with a 0-100 confidence score. Score ≥ 70 is
    Hunter's own threshold for "fairly confident". We accept ≥ 50 and let the
    waterfall escalate to the next tier if lower.
    Free tier: 25 searches/month. API docs: hunter.io/api-documentation/v2
    """
    if not HUNTER_API_KEY:
        return None
    params = urllib.parse.urlencode({
        "domain":      domain,
        "first_name":  first,
        "last_name":   last,
        "api_key":     HUNTER_API_KEY,
    })
    url = f"https://api.hunter.io/v2/email-finder?{params}"
    try:
        req  = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        d = data.get("data", {})
        email      = d.get("email", "")
        confidence = d.get("score", 0)
        if email and confidence >= 50:
            log.info("Hunter found: %s (confidence=%d)", email, confidence)
            return (email, confidence)
        if email:
            log.debug("Hunter found %s but confidence too low (%d) — trying next tier", email, confidence)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            log.warning("Hunter rate limit hit")
        elif e.code != 404:
            log.debug("Hunter error %d for %s %s @ %s", e.code, first, last, domain)
    except Exception as e:
        log.debug("Hunter exception: %s", e)
    return None


def _hunter_domain_search(domain: str, first: str, last: str) -> Optional[tuple]:
    """
    Hunter domain search — finds all emails on a domain, then filters by name.
    Useful as a Hunter Tier 1b fallback when /email-finder returns nothing but
    the domain itself is real (Hunter has it indexed but person-level lookup missed).
    Returns (email, 60) if name match found, else None.
    """
    if not HUNTER_API_KEY:
        return None
    params = urllib.parse.urlencode({
        "domain":  domain,
        "api_key": HUNTER_API_KEY,
        "limit":   20,
        "type":    "personal",
    })
    url = f"https://api.hunter.io/v2/domain-search?{params}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        emails = data.get("data", {}).get("emails", [])
        first_l, last_l = first.lower(), last.lower()
        for entry in emails:
            fn = (entry.get("first_name") or "").lower()
            ln = (entry.get("last_name")  or "").lower()
            em = (entry.get("value")      or "").lower()
            # Match on name or on email local-part containing the name
            if (fn == first_l and ln == last_l) or \
               (first_l in em and last_l in em) or \
               (fn and ln and first_l.startswith(fn) and last_l.startswith(ln)):
                email = entry["value"]
                conf  = entry.get("confidence", 60)
                log.info("Hunter domain-search found: %s (confidence=%d)", email, conf)
                return (email, conf)
    except Exception as e:
        log.debug("Hunter domain-search exception: %s", e)
    return None


# ── Tier 2: Prospeo Email Finder ──────────────────────────────────────────
def _prospeo_find_email(first: str, last: str, domain: str,
                        linkedin_url: str = "") -> Optional[tuple]:
    """
    Prospeo /email-finder — takes full name + domain, or LinkedIn URL.
    Returns (email, confidence) or None.
    Particularly strong India coverage for .in domains and Indian IT/consulting firms.
    Free tier: 75 email lookups/month. prospeo.com/api
    """
    if not PROSPEO_API_KEY:
        return None

    payload: dict = {"domain": domain}
    if linkedin_url:
        payload["linkedin_url"] = linkedin_url
    else:
        payload["full_name"] = f"{first} {last}"

    req = urllib.request.Request(
        "https://api.prospeo.io/email-finder",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type":  "application/json",
            "X-KEY":         PROSPEO_API_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if not data.get("error") and data.get("response"):
            r     = data["response"]
            email = r.get("email", "")
            # Prospeo returns verification status: "VALID", "ACCEPT_ALL", "UNKNOWN", "INVALID"
            vstatus = r.get("email_status", {}).get("result", "UNKNOWN")
            if email and vstatus in ("VALID", "ACCEPT_ALL"):
                conf = 90 if vstatus == "VALID" else 65
                log.info("Prospeo found: %s (status=%s)", email, vstatus)
                return (email, conf)
            if email and vstatus == "UNKNOWN":
                log.debug("Prospeo found %s but unverified — queuing as fallback", email)
                return (email, 40)  # below threshold but kept as a fallback signal
    except urllib.error.HTTPError as e:
        if e.code == 429:
            log.warning("Prospeo rate limit hit")
        else:
            log.debug("Prospeo error %d", e.code)
    except Exception as e:
        log.debug("Prospeo exception: %s", e)
    return None


# ── Tier 3: AnyMailFinder ─────────────────────────────────────────────────
def _anymailfinder_find_email(first: str, last: str, domain: str) -> Optional[tuple]:
    """
    AnyMailFinder /v1/email/find — name + domain lookup.
    Returns (email, confidence) or None.
    Good supplementary coverage for .in domains and Indian companies
    not well indexed by Hunter.
    Free tier: 90 lookups/month. anymailfinder.com/api
    """
    if not ANYMAILFINDER_KEY:
        return None
    payload = {
        "full_name":    f"{first} {last}",
        "domain_or_company": domain,
    }
    req = urllib.request.Request(
        "https://api.anymailfinder.com/v5.0/search/person.json",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type":    "application/json",
            "Authorization":   f"Bearer {ANYMAILFINDER_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        email  = data.get("email", "")
        result = data.get("result_type", "")   # "email_found", "not_found", etc.
        if email and result == "email_found":
            log.info("AnyMailFinder found: %s", email)
            return (email, 80)
    except urllib.error.HTTPError as e:
        log.debug("AnyMailFinder error %d", e.code)
    except Exception as e:
        log.debug("AnyMailFinder exception: %s", e)
    return None


# ── Tier 4: Snov.io ───────────────────────────────────────────────────────
_snov_token_cache: dict = {}

def _snov_get_token() -> str:
    """OAuth2 client_credentials flow for Snov.io. Token cached for 1 hour."""
    import time as _t
    cached = _snov_token_cache.get("token")
    expires = _snov_token_cache.get("expires", 0)
    if cached and _t.time() < expires:
        return cached
    if not SNOV_CLIENT_ID or not SNOV_CLIENT_SECRET:
        return ""
    payload = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     SNOV_CLIENT_ID,
        "client_secret": SNOV_CLIENT_SECRET,
    }).encode()
    try:
        req = urllib.request.Request(
            "https://api.snov.io/v1/oauth/access_token",
            data=payload,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        token = data.get("access_token", "")
        if token:
            _snov_token_cache["token"]   = token
            _snov_token_cache["expires"] = _t.time() + 3500
        return token
    except Exception as e:
        log.debug("Snov.io token error: %s", e)
        return ""


def _snov_find_email(first: str, last: str, domain: str) -> Optional[tuple]:
    """
    Snov.io /get-emails-by-name — returns a list of possible emails with
    confidence. We take the first entry with confidence ≥ 50.
    Free tier: 50 credits/month. docs.snov.io
    """
    token = _snov_get_token()
    if not token:
        return None
    payload = json.dumps({
        "firstName":  first,
        "lastName":   last,
        "domain":     domain,
        "limit":      5,
    }).encode()
    req = urllib.request.Request(
        "https://api.snov.io/v2/email-by-name",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        emails = data.get("emails", [])
        for entry in sorted(emails, key=lambda x: x.get("confidence", 0), reverse=True):
            email = entry.get("email", "")
            conf  = entry.get("confidence", 0)
            if email and conf >= 50 and _email_structurally_valid(email):
                log.info("Snov.io found: %s (confidence=%d)", email, conf)
                return (email, conf)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            log.warning("Snov.io rate limit hit")
        else:
            log.debug("Snov.io error %d", e.code)
    except Exception as e:
        log.debug("Snov.io exception: %s", e)
    return None


# ── Tier 5: Apollo.io person-level match ──────────────────────────────────
def _apollo_find_email(first: str, last: str, domain: str,
                       company: str = "") -> Optional[tuple]:
    """
    Apollo free-tier email lookup via /v1/mixed_people/search.

    /people/match requires a paid plan and returns 401/403 on free accounts
    even with a valid master key — that's the auth error you were seeing.

    /mixed_people/search is available on the free plan (600 export credits/mo).
    We search by name + domain, take the first result that matches the name,
    and extract the email Apollo has on file. Apollo masks emails as
    "email_from_customer" or "email" depending on whether the account has
    export credits — we try both fields.
    """
    if not APOLLO_API_KEY:
        return None

    # Search for this specific person by name + domain
    payload = {
        "person_titles":          [],
        "q_person_name":          f"{first} {last}",
        "organization_domains":   [domain],
        "page":                   1,
        "per_page":               5,
    }
    req = urllib.request.Request(
        "https://api.apollo.io/v1/mixed_people/search",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type":  "application/json",
            "Cache-Control": "no-cache",
            "X-Api-Key":     APOLLO_API_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read())

        people = data.get("people", [])
        first_l, last_l = first.lower(), last.lower()

        for person in people:
            fn = (person.get("first_name") or "").lower()
            ln = (person.get("last_name")  or "").lower()

            # Only accept if names actually match — search results can be fuzzy
            if not (fn and ln and first_l.startswith(fn[:3]) and last_l.startswith(ln[:3])):
                continue

            # Apollo exposes email in different fields depending on plan/credits
            email = (
                person.get("email") or
                person.get("email_from_customer") or
                ""
            )

            # Apollo sometimes returns a sanitized placeholder like
            # "e***@domain.com" — detect and skip those
            if email and "***" not in email and _email_structurally_valid(email):
                estatus = person.get("email_status", "")
                conf = 85 if estatus == "verified" else 62
                log.info("Apollo found: %s (status=%s)", email, estatus)
                return (email, conf)

        log.debug("Apollo search: no email found for %s %s @ %s", first, last, domain)

    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            log.warning(
                "Apollo auth error (HTTP %d) — key may lack search permissions. "
                "Check apollo.io → Settings → Integrations → API Keys and ensure "
                "People API + Organizations API are selected.", e.code
            )
        elif e.code == 429:
            log.warning("Apollo rate limit hit — will retry next run")
        else:
            log.debug("Apollo search error %d", e.code)
    except Exception as e:
        log.debug("Apollo exception: %s", e)
    return None


# ── Tier 6: Pattern Inference (last resort) ────────────────────────────────
def _infer_email_pattern(first: str, last: str, domain: str,
                         smtp_check: bool = None) -> Optional[str]:
    """
    Pattern-guesses email from the top 8 corporate conventions.
    This is the OLD primary method, now demoted to last resort.
    Returns the best candidate but the caller MUST set email_verified=False.
    Only used when all 5 lookup tiers return nothing.
    """
    if smtp_check is None:
        smtp_check = SMTP_VERIFY_ENABLED

    first = re.sub(r"[^a-z]", "", first.lower())
    last  = re.sub(r"[^a-z]", "", last.lower())
    f     = first[0] if first else ""
    if not first or not last or not domain:
        return None
    if not _tld_is_valid(domain):
        return None

    candidates = []
    for pat in EMAIL_PATTERNS:
        try:
            email = pat.format(first=first, last=last, f=f, domain=domain)
        except KeyError:
            continue
        if _email_passes(email) and email not in candidates:
            candidates.append(email)

    if not candidates:
        return None

    if not smtp_check:
        return candidates[0]

    # With pattern guessing, SMTP verification actually matters more here
    # than in the lookup tiers — without a database confirming the format,
    # we really need the SMTP signal. Still falls back if blocked.
    for email in candidates:
        status = _smtp_verify(email)
        if status == "verified":
            log.debug("Pattern+SMTP-verified: %s", email)
            return email
        if status == "catch_all":
            return candidates[0]
        if status == "rejected":
            continue

    log.debug("Pattern fallback (unverified): %s", candidates[0])
    return candidates[0]


# ── Master Waterfall ───────────────────────────────────────────────────────
def _resolve_email_waterfall(
    first: str,
    last:  str,
    domain: str,
    company: str = "",
    linkedin_url: str = "",
) -> tuple:
    """
    Runs through all resolution tiers in priority order.
    Returns (email, verified: bool, source: str).
    Caller should set lead.email_verified = verified and lead.email_source = source.
    If verified=False, the lead should go to a manual-review queue
    rather than auto-dispatch (Module D gates on email_verified).

    Confidence threshold for "verified": ≥ 70 from any lookup API.
    Between 50-69: accepted but flagged "low_confidence" → still goes to sheet
    for human approval but with a warning column.
    Below 50 (pattern only): email_verified=False → dispatch blocked until
    human manually marks it verified in the sheet.
    """
    if not first or not last or not domain:
        return ("", False, "none")

    VERIFIED_THRESHOLD    = 70
    LOW_CONF_THRESHOLD    = 50

    # Tier 1a: Hunter email-finder (person-level, best accuracy)
    result = _hunter_find_email(first, last, domain)
    if result:
        email, conf = result
        verified = conf >= VERIFIED_THRESHOLD
        return (email, verified, f"hunter (confidence={conf})")

    # Tier 1b: Hunter domain-search (broader sweep of the same database)
    result = _hunter_domain_search(domain, first, last)
    if result:
        email, conf = result
        verified = conf >= VERIFIED_THRESHOLD
        return (email, verified, f"hunter-domain (confidence={conf})")

    # Tier 2: Prospeo (strong India + .in coverage)
    result = _prospeo_find_email(first, last, domain, linkedin_url)
    if result:
        email, conf = result
        if conf >= LOW_CONF_THRESHOLD:
            verified = conf >= VERIFIED_THRESHOLD
            return (email, verified, f"prospeo (confidence={conf})")

    # Tier 3: AnyMailFinder (independent database, good .in coverage)
    result = _anymailfinder_find_email(first, last, domain)
    if result:
        email, conf = result
        verified = conf >= VERIFIED_THRESHOLD
        return (email, verified, f"anymailfinder (confidence={conf})")

    # Tier 4: Snov.io
    result = _snov_find_email(first, last, domain)
    if result:
        email, conf = result
        verified = conf >= VERIFIED_THRESHOLD
        return (email, verified, f"snov (confidence={conf})")

    # Tier 5: Apollo person-match
    result = _apollo_find_email(first, last, domain, company)
    if result:
        email, conf = result
        verified = conf >= VERIFIED_THRESHOLD
        return (email, verified, f"apollo (confidence={conf})")

    # Tier 6: Pattern inference (last resort — blocks auto-send)
    domain_ok = _domain_has_mx(domain)
    if domain_ok:
        email = _infer_email_pattern(first, last, domain)
        if email:
            log.warning(
                "Pattern-inferred email for %s %s @ %s — NOT verified, "
                "will require manual review before dispatch: %s",
                first, last, domain, email,
            )
            return (email, False, "pattern-inferred (UNVERIFIED — manual review required)")

    return ("", False, "none")


_domain_cache: dict = {}

def _get_company_domain(company: str) -> Optional[str]:
    """Looks up the canonical email domain for a company name via Serper,
    filtering out social/aggregator noise. Cached per process."""
    if company in _domain_cache:
        return _domain_cache[company]
    results = _serper_search(f'"{company}" official website contact email', num=5)
    noise   = ["linkedin","twitter","facebook","crunchbase","glassdoor","bloomberg",
               "wikipedia","youtube","indeed","naukri","zoominfo","techcrunch","ambitionbox",
               "instagram","medium.com","substack.com","reddit.com","quora.com",
               "x.com","threads.net","github.io","blogspot.com","wordpress.com"]
    for r in results:
        link  = r.get("link", "")
        m = re.search(r"https?://(?:www\.)?([^/]+)", link)
        if not m:
            continue
        domain = m.group(1).lower()
        if any(n in domain for n in noise):
            continue
        if domain.count(".") > 3:
            continue
        if not _tld_is_valid(domain):
            continue
        _domain_cache[company] = domain
        return domain
    _domain_cache[company] = None
    return None


def _domain_has_mx(domain: str) -> bool:
    """Quick DNS check — avoids sending to dead domains.
    Upgraded from a plain getaddrinfo (which just checks the domain
    resolves to *something*, including a bare A record with no mail
    service at all) to an actual MX lookup, with A-record fallback for
    domains that route mail through the bare domain (some small/startup
    setups do this)."""
    if not domain or not _tld_is_valid(domain):
        return False
    try:
        import dns.resolver
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=5)
            return len(answers) > 0
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            pass
        except Exception as e:
            log.debug("MX lookup error for %s: %s — falling back to A record", domain, e)
        # Fallback: some domains accept mail on the bare A record with no MX
        try:
            dns.resolver.resolve(domain, "A", lifetime=5)
            return True
        except Exception:
            return False
    except ImportError:
        # dnspython not installed — degrade to the old behaviour rather
        # than hard-fail the whole pipeline, but log loudly so it gets fixed.
        log.warning("dnspython not installed (pip install dnspython) — MX check degraded to plain DNS resolution")
        try:
            socket.getaddrinfo(domain, None)
            return True
        except socket.gaierror:
            return False


# ──────────────────────────────────────────────────────────────────────────
# SMTP-LEVEL VERIFICATION
# Pattern-matched + MX-confirmed is still a guess. An RCPT TO probe against
# the real mail server is the closest free signal to "does this mailbox
# actually exist" without sending anything. Many corporate mail servers
# (M365, Google Workspace with strict mode) won't give a clean signal — they
# accept-all at SMTP and reject later, or block probing entirely. So this is
# best-effort: a hard 550/551/553 is treated as a real rejection; anything
# else (accept, greylist, timeout, blocked) is treated as "can't disprove it",
# and we keep the lead rather than silently dropping good targets over a
# defensive mail server.
# ──────────────────────────────────────────────────────────────────────────
import smtplib

_smtp_domain_cache: dict = {}   # domain -> "catch_all" | "verified" | "rejected" | "unknown"

def _get_domain_mx_host(domain: str) -> Optional[str]:
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        sorted_mx = sorted(answers, key=lambda r: r.preference)
        return str(sorted_mx[0].exchange).rstrip(".")
    except Exception:
        return None


def _smtp_verify(email: str, helo_domain: str = None) -> str:
    """
    Returns one of: 'verified', 'rejected', 'catch_all', 'unknown'.
    Never raises — every failure mode degrades to 'unknown' so callers
    can decide whether to keep a pattern-matched guess.
    """
    helo_domain = helo_domain or SMTP_HELO_DOMAIN
    domain = email.split("@", 1)[1]

    if domain in _smtp_domain_cache and _smtp_domain_cache[domain] == "catch_all":
        # Once we know a domain accepts everything, there's no point
        # probing it again per-candidate — every address will "pass".
        return "catch_all"

    mx_host = _get_domain_mx_host(domain)
    if not mx_host:
        return "unknown"

    probe_unknown_local = f"definitely-not-a-real-user-{int(time.time())}@{domain}"

    try:
        smtp = smtplib.SMTP(timeout=SMTP_VERIFY_TIMEOUT)
        smtp.connect(mx_host, 25)
        smtp.helo(helo_domain)
        smtp.mail(f"verify@{helo_domain}")

        # First probe a deliberately-fake address to detect catch-all domains.
        code_fake, _ = smtp.rcpt(probe_unknown_local)
        if code_fake == 250:
            _smtp_domain_cache[domain] = "catch_all"
            smtp.quit()
            return "catch_all"   # domain accepts anything — can't disprove or confirm real email

        # Now probe the real candidate.
        code_real, msg_real = smtp.rcpt(email)
        smtp.quit()

        if code_real == 250:
            return "verified"
        if code_real in (550, 551, 553):
            return "rejected"
        return "unknown"

    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
            socket.timeout, socket.gaierror, ConnectionRefusedError, OSError) as e:
        # Most corporate firewalls block outbound SMTP probing on port 25
        # entirely — this is the expected, common case, not an error worth
        # surfacing loudly.
        log.debug("SMTP probe unreachable for %s: %s", domain, e)
        return "unknown"
    except Exception as e:
        log.debug("SMTP probe failed for %s: %s", email, e)
        return "unknown"


def _infer_email(first: str, last: str, domain: str, smtp_check: bool = None) -> Optional[str]:
    """
    Generates candidate emails in order of statistical likelihood
    (first.last@ is the most common corporate convention by a wide margin),
    then — if smtp_check is on — probes them against the real mail server
    and returns the first one that comes back 'verified'.

    If the domain is a confirmed catch-all (accepts everything) or every
    probe comes back 'unknown' (most common — corporate firewalls block
    port 25 probing), we fall back to the single most statistically likely
    pattern rather than dropping the lead. This trades a small amount of
    bounce risk for not silently losing good targets, which matters more
    at 8 emails/run than a marginal bounce-rate improvement would.
    """
    if smtp_check is None:
        smtp_check = SMTP_VERIFY_ENABLED   # config default; explicit arg still overrides

    first = re.sub(r"[^a-z]", "", first.lower())
    last  = re.sub(r"[^a-z]", "", last.lower())
    f     = first[0] if first else ""
    if not first or not last or not domain:
        return None
    if not _tld_is_valid(domain):
        log.debug("Rejected domain with invalid TLD: %s", domain)
        return None

    candidates = []
    for pat in EMAIL_PATTERNS:
        try:
            email = pat.format(first=first, last=last, f=f, domain=domain)
        except KeyError:
            continue
        if _email_passes(email) and email not in candidates:
            candidates.append(email)

    if not candidates:
        return None

    if not smtp_check:
        return candidates[0]

    for email in candidates:
        status = _smtp_verify(email)
        if status == "verified":
            log.debug("SMTP-verified: %s", email)
            return email
        if status == "catch_all":
            # Domain swallows everything — SMTP can't help us pick between
            # patterns. Use the most likely one (first.last) and move on.
            log.debug("Catch-all domain %s — using top pattern guess: %s", domain, candidates[0])
            return candidates[0]
        if status == "rejected":
            # This specific candidate bounced at SMTP level — try the next
            # pattern instead of giving up on the whole domain.
            log.debug("SMTP-rejected candidate %s — trying next pattern", email)
            continue
        # 'unknown' (firewall blocked probe, timeout, etc.) — keep trying
        # remaining candidates in case a later one gets a clean signal,
        # but remember this one as a fallback.

    # No pattern came back definitively 'verified' and none were cleanly
    # 'rejected' either (i.e. everything was 'unknown' — the common case
    # when port 25 is firewalled). Fall back to the top statistical guess
    # rather than dropping a lead purely because we couldn't probe it.
    log.debug("No SMTP-verified candidate for %s @ %s — using top pattern guess", first, domain)
    return candidates[0]


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
    Build Serper queries for a geo × vertical combo.
    week_num shifts phrasing so the same combo surfaces fresh people
    each time it recurs in the 12-week rotation.

    Construction notes:
      - Title and vertical keyword are both quoted as exact phrases —
        unquoted multi-word terms let Google match on partial token overlap,
        which is where a lot of "wrong person" results sneak in (e.g.
        unquoted "Head of Strategy" can match a page that just contains
        "head" and "strategy" anywhere).
      - geo_term is deliberately left unquoted since it's usually a single
        token (a city or "India") or an OR-group, and over-quoting location
        terms tends to suppress valid results where the location appears in
        a different field order ("Mumbai, India" vs "India, Mumbai").
      - -intitle:"jobs" -inurl:"jobs" suppresses job-board postings and
        careers pages from leaking into people-search results — those pages
        rank well for these queries but contain zero individual contacts.
    """
    geo      = get_geo(geo_id)
    vertical = get_vertical(vertical_id)
    if not geo or not vertical:
        return []

    geo_term  = geo["terms"][week_num % len(geo["terms"])]
    vkw       = vertical["keywords"][week_num % len(vertical["keywords"])]
    ta = TITLES_TIER_A[week_num % len(TITLES_TIER_A)]
    tb = TITLES_TIER_B[(week_num + 1) % len(TITLES_TIER_B)]
    tc = TITLES_TIER_C[(week_num + 2) % len(TITLES_TIER_C)]
    et = vertical["extra_titles"][(week_num) % len(vertical["extra_titles"])]

    job_board_suppression = '-inurl:"jobs" -inurl:"careers" -intitle:"hiring"'

    queries = [
        # Tier A — decision makers
        f'site:linkedin.com/in "{ta}" "{vkw}" {geo_term} {job_board_suppression}',
        # Tier B — department heads
        f'site:linkedin.com/in "{tb}" {geo_term} "{vkw}" {job_board_suppression}',
        # Tier C — managers & hiring managers (wider net, higher reply rates)
        f'site:linkedin.com/in "{tc}" "{vkw}" {geo_term} {job_board_suppression}',
        # Vertical-specific extra title
        f'site:linkedin.com/in "{et}" {geo_term} {vkw} {job_board_suppression}',
    ]

    # Remote geo: add remote-specific variant
    if geo_id == "REMOTE":
        queries.append(f'site:linkedin.com/in "remote" "{vkw}" hiring "{ta}" OR "{tb}" {job_board_suppression}')

    # Research geo: think-tank specific
    if vertical_id == "RESEARCH_ECON":
        queries.append(f'site:linkedin.com/in "research" "{geo_term}" economist OR "policy" OR "think tank"')

    return queries[:6]


def _build_people_search_queries(geo_id: str, vertical_id: str, week_num: int) -> list:
    """
    Companion query set for the Serper /people endpoint, which does its own
    entity resolution server-side rather than relying on us parsing organic
    snippet text — so these queries skip the site:linkedin.com/in restriction
    (the people endpoint isn't a generic web search, it's already scoped to
    profiles) and instead lean on title + geo + vertical only.
    """
    geo      = get_geo(geo_id)
    vertical = get_vertical(vertical_id)
    if not geo or not vertical:
        return []

    geo_term = geo["terms"][week_num % len(geo["terms"])]
    vkw      = vertical["keywords"][(week_num + 1) % len(vertical["keywords"])]
    ta       = TITLES_TIER_A[(week_num + 2) % len(TITLES_TIER_A)]
    tb       = TITLES_TIER_B[week_num % len(TITLES_TIER_B)]

    return [
        f'{ta} {vkw} {geo_term}',
        f'{tb} {vkw} {geo_term}',
    ]


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

    def _process_candidate(profile: dict, geo_id: str, vertical_id: str, source_tag: str) -> Optional[Lead]:
        """Shared enrichment/scoring path for a raw profile dict, regardless
        of whether it came from a scraped LinkedIn URL or a Serper /people
        hit. Keeping this in one place means a fix to email inference,
        title filtering, or scoring logic applies identically to both
        sourcing paths instead of silently diverging over time."""
        if not profile or not profile.get("name"):
            return None

        name  = profile["name"].strip()
        title = (profile.get("title") or "").strip()
        if not name or not title:
            return None

        tier = _title_tier(title)
        if tier is None:
            return None   # rejected title

        company  = (profile.get("company") or "").strip()
        location = (profile.get("location") or "").strip()
        summary  = (profile.get("summary") or "")
        url      = (profile.get("linkedin_url") or "")

        key = f"{name.lower()}|{company.lower()}"
        if key in seen_run:
            return None
        seen_run.add(key)

        company_type, funding_stage = _classify_company(company, summary)
        is_remote = _is_remote(summary, title, location)

        # Company description (1 Serper call, quota-conscious)
        company_desc = ""
        if company and _serper_calls < 60:
            company_desc = _get_company_desc(company)
            time.sleep(0.6)

        # Domain resolution
        domain = None
        if company and _serper_calls < 65:
            domain = _get_company_domain(company)
            time.sleep(0.6)

        # ── Email resolution waterfall ─────────────────────────────────────
        # Old behaviour: pattern-guess → ~20-30% accuracy for Indian firms.
        # New behaviour: 5 verified lookup APIs first, pattern as last resort.
        email = ""
        email_verified = False
        email_source   = "none"

        if domain:
            parts = name.split()
            if len(parts) >= 2:
                first_n, last_n = parts[0], parts[-1]
                email, email_verified, email_source = _resolve_email_waterfall(
                    first=first_n, last=last_n,
                    domain=domain, company=company,
                    linkedin_url=url,
                )
                if email in seen_hist or email in seen_run:
                    log.debug("Email already seen — skipping duplicate: %s", email)
                    email = ""

        if not email:
            log.debug("No email resolved for %s @ %s", name, company)
            return None

        seen_run.add(email)

        lead = Lead(
            name=name, company=company, position=title,
            email=email, email_verified=email_verified, email_source=email_source,
            linkedin_url=url, location=location,
            geo_segment=geo_id, vertical=vertical_id,
            company_type=company_type, funding_stage=funding_stage,
            is_remote_role=is_remote, company_description=company_desc,
            title_tier=tier, outreach_mode=OUTREACH_MODE,
            source=source_tag,
            week_sourced=f"{datetime.now(timezone.utc).year}-W{week_num:02d}",
        )
        lead.reason_for_outreach = _build_reason(lead)
        lead.confidence_score    = _score_lead(lead)

        verified_tag = "✓VERIFIED" if email_verified else "⚠ UNVERIFIED"
        log.info("✓ [Tier%s|%d|%s] %s | %s | %s | %s [%s]",
                 tier, lead.confidence_score, verified_tag,
                 name, title, company, email, email_source)
        return lead

    for geo_id, vertical_id in combos:
        if len(all_leads) >= target * 3:
            break

        geo      = get_geo(geo_id)
        vertical = get_vertical(vertical_id)
        if not geo or not vertical:
            log.warning("Unknown geo '%s' or vertical '%s'", geo_id, vertical_id)
            continue

        log.info("▶ [%s × %s]", geo.get("name","?"), vertical.get("name","?"))

        # ── SOURCE 1: site:linkedin.com/in organic search ──
        queries = _build_queries(geo_id, vertical_id, week_num)
        for query in queries:
            if len(all_leads) >= target * 3:
                break

            results       = _serper_search(query, num=8)
            linkedin_urls = _extract_linkedin_urls(results)

            for url in linkedin_urls:
                if len(all_leads) >= target * 3:
                    break
                profile = _enrich_linkedin(url) or _parse_snippet(url, results)
                lead = _process_candidate(
                    profile, geo_id, vertical_id,
                    source_tag=f"Serper+LinkedIn [{geo_id}×{vertical_id}]",
                )
                if lead:
                    all_leads.append(lead)

        # ── SOURCE 2: Serper /people structured search ──
        # Runs as a supplementary pass on the same geo×vertical combo.
        # Serper's people endpoint does entity resolution server-side, so
        # it tends to surface cleanly-attributed name/title/company triples
        # that the snippet-parsing fallback in SOURCE 1 sometimes mangles
        # (e.g. when LinkedIn's title format doesn't match the
        # "Name - Title at Company" pattern _parse_snippet expects).
        if len(all_leads) < target * 3:
            people_queries = _build_people_search_queries(geo_id, vertical_id, week_num)
            for pq in people_queries:
                if len(all_leads) >= target * 3:
                    break
                people_results = _serper_people_search(pq, num=8)
                for item in people_results:
                    if len(all_leads) >= target * 3:
                        break
                    profile = _people_result_to_profile(item)
                    lead = _process_candidate(
                        profile, geo_id, vertical_id,
                        source_tag=f"Serper-People [{geo_id}×{vertical_id}]",
                    )
                    if lead:
                        all_leads.append(lead)

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
