"""Email-finder provider waterfall.

Each `BaseEmailFinder` turns (person, company domain) into `EmailCandidate`s. Providers are
enabled automatically when their env var is present and are tried cheapest/most-reliable first.
Finders only *propose* addresses — nothing is usable until `verify.gate` says so.

Adapters for paid providers follow each vendor's public API docs; their response parsing is
deliberately tolerant and every failure is isolated to the one lead (ProviderError).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from abc import ABC, abstractmethod

from ..http import Http, ProviderError
from ..models import EmailCandidate, Lead
from ..settings import Settings

log = logging.getLogger(__name__)


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def name_parts(lead: Lead) -> tuple[str, str]:
    p = lead.person
    first, last = p.first_name, p.last_name
    if not first:
        parts = p.display_name().split()
        first, last = (parts[0] if parts else ""), (parts[-1] if len(parts) > 1 else "")
    first = re.sub(r"[^a-z]", "", _ascii(first.split()[0] if first else ""))
    last = re.sub(r"[^a-z]", "", _ascii(last.split()[-1] if last else ""))
    return first, last


class BaseEmailFinder(ABC):
    name: str = ""
    env_attr: str = ""        # attribute on Settings.keys
    paid: bool = True

    def __init__(self, http: Http, settings: Settings):
        self.http = http
        self.s = settings

    @property
    def key(self) -> str:
        return getattr(self.s.keys, self.env_attr, "") if self.env_attr else ""

    def enabled(self) -> bool:
        return (not self.env_attr or bool(self.key)) and self.http.available(self.name)

    @abstractmethod
    def find(self, lead: Lead, domain: str) -> list[EmailCandidate]:
        ...


class HunterFinder(BaseEmailFinder):
    name, env_attr = "hunter", "hunter"

    def find(self, lead, domain):
        first, last = name_parts(lead)
        params = {"domain": domain, "api_key": self.key}
        if lead.person.linkedin_url and not (first and last):
            params["linkedin_handle"] = lead.person.linkedin_url
        else:
            params.update(first_name=first, last_name=last)
        data = self.http.request("GET", "https://api.hunter.io/v2/email-finder", provider=self.name,
                                 params=params, ok_statuses=(404,))
        d = (data or {}).get("data") or {}
        email = d.get("email")
        if not email:
            return []
        ver = d.get("verification") or {}
        status = (ver.get("status") or "").lower()  # valid | accept_all | unknown
        return [EmailCandidate(email=email, source="hunter", confidence=int(d.get("score") or 0),
                               provider_status={"valid": "valid", "accept_all": "accept_all"}.get(status, "unknown"),
                               catch_all_hint=bool(d.get("accept_all")) if d.get("accept_all") is not None else None)]


class ProspeoFinder(BaseEmailFinder):
    name, env_attr = "prospeo", "prospeo"

    def find(self, lead, domain):
        first, last = name_parts(lead)
        body = {"first_name": first, "last_name": last, "company": domain}
        if lead.person.linkedin_url:
            body["linkedin_url"] = lead.person.linkedin_url
        data = self.http.request("POST", "https://api.prospeo.io/email-finder", provider=self.name, json=body,
                                 headers={"X-KEY": self.key, "Content-Type": "application/json"},
                                 ok_statuses=(400, 404))
        r = (data or {}).get("response") or {}
        email = r.get("email") or (r.get("email_data") or {}).get("email")
        if not email or data.get("error"):
            return []
        st = r.get("email_status") or (r.get("email_data") or {}).get("status") or ""
        st = (st.get("result") if isinstance(st, dict) else st) or ""
        st = st.upper()
        return [EmailCandidate(email=email, source="prospeo", confidence={"VALID": 90, "ACCEPT_ALL": 65}.get(st, 40),
                               provider_status={"VALID": "valid", "ACCEPT_ALL": "accept_all"}.get(st, "unknown"))]


class FindymailFinder(BaseEmailFinder):
    name, env_attr = "findymail", "findymail"

    def find(self, lead, domain):
        data = self.http.request("POST", "https://app.findymail.com/api/search/name", provider=self.name,
                                 json={"name": lead.person.display_name(), "domain": domain},
                                 headers={"Authorization": f"Bearer {self.key}"}, ok_statuses=(404,))
        contact = (data or {}).get("contact") or {}
        email = contact.get("email")
        # Findymail only returns emails it has verified (pay-per-valid)
        return [EmailCandidate(email=email, source="findymail", confidence=90, provider_status="valid")] if email else []


class LeadMagicFinder(BaseEmailFinder):
    name, env_attr = "leadmagic", "leadmagic"

    def find(self, lead, domain):
        first, last = name_parts(lead)
        data = self.http.request("POST", "https://api.leadmagic.io/email-finder", provider=self.name,
                                 json={"first_name": first, "last_name": last, "domain": domain},
                                 headers={"X-API-Key": self.key}, ok_statuses=(404,))
        email = (data or {}).get("email")
        if not email:
            return []
        st = str((data or {}).get("status", "")).lower()
        valid = st in ("valid", "valid_catch_all", "verified")
        return [EmailCandidate(email=email, source="leadmagic", confidence=88 if st == "valid" else 65 if valid else 40,
                               provider_status="valid" if st == "valid" else "accept_all" if "catch" in st else "unknown")]


class AnymailFinder(BaseEmailFinder):
    name, env_attr = "anymailfinder", "anymailfinder"

    def find(self, lead, domain):
        data = self.http.request("POST", "https://api.anymailfinder.com/v5.0/search/person.json", provider=self.name,
                                 json={"full_name": lead.person.display_name(), "domain": domain},
                                 headers={"Authorization": self.key}, ok_statuses=(404,))
        res = (data or {}).get("results") or data or {}
        email = res.get("email")
        if not email:
            return []
        valid = str(res.get("validation", res.get("email_status", ""))).lower() in ("valid", "verified")
        return [EmailCandidate(email=email, source="anymailfinder", confidence=85 if valid else 50,
                               provider_status="valid" if valid else "unknown")]


class ApolloMatchFinder(BaseEmailFinder):
    """POST /api/v1/people/match — 1 credit per email. Free plan: work-email-registered accounts only."""
    name, env_attr = "apollo", "apollo"

    def __init__(self, http, settings):
        super().__init__(http, settings)
        self.spent = 0

    def enabled(self):
        return super().enabled() and self.spent < self.s.apollo_match_budget

    def find(self, lead, domain):
        first, last = name_parts(lead)
        params = {"first_name": lead.person.first_name or first, "last_name": lead.person.last_name or last,
                  "domain": domain, "organization_name": lead.company.name, "reveal_personal_emails": "false"}
        if lead.person.linkedin_url:
            params["linkedin_url"] = lead.person.linkedin_url
        if apollo_id := lead.legacy.get("apollo_id"):
            params = {"id": apollo_id}
        self.spent += 1
        try:
            data = self.http.request("POST", "https://api.apollo.io/api/v1/people/match", provider=self.name,
                                     params=params, headers={"x-api-key": self.key, "Cache-Control": "no-cache"})
        except ProviderError as e:
            if e.category == "auth":
                log.error("Apollo rejected the API key (HTTP %s). On Apollo's free plan, API access requires an "
                          "account registered with a work email — use the CSV export drop-in (inputs/) instead.", e.status)
            raise
        person = (data or {}).get("person") or {}
        email = person.get("email")
        if not email or "email_not_unlocked" in email:
            return []
        st = (person.get("email_status") or "").lower()
        org = person.get("organization") or {}
        if org.get("primary_domain") and not lead.company.domain:
            lead.company.domain = org["primary_domain"]
        if person.get("linkedin_url") and not lead.person.linkedin_url:
            lead.person.linkedin_url = person["linkedin_url"]
        return [EmailCandidate(email=email, source="apollo_api", confidence=85 if st == "verified" else 35,
                               provider_status="verified" if st == "verified" else "guessed")]


PATTERNS = ("{f}.{l}", "{f}", "{f}{l}", "{fi}{l}", "{f}_{l}", "{fi}.{l}", "{l}.{f}", "{f}{li}", "{l}")


def pattern_candidates(lead: Lead, domain: str, limit: int = 6) -> list[EmailCandidate]:
    """Statistically-ordered guesses. NEVER usable on their own — only a VERIFIED gate result
    (SMTP 250 on a non-catch-all server, or an API verifier saying valid) promotes one."""
    first, last = name_parts(lead)
    if not first or not domain:
        return []
    out, seen = [], set()
    for pat in PATTERNS:
        if "{l" in pat and not last:
            continue
        local = pat.format(f=first, l=last, fi=first[:1], li=last[:1])
        email = f"{local}@{domain}"
        if email not in seen:
            seen.add(email)
            out.append(EmailCandidate(email=email, source="pattern", confidence=20, provider_status="guessed"))
    return out[:limit]


FINDER_ORDER = (HunterFinder, FindymailFinder, ProspeoFinder, LeadMagicFinder, AnymailFinder, ApolloMatchFinder)


def build_finders(http: Http, settings: Settings) -> list[BaseEmailFinder]:
    return [cls(http, settings) for cls in FINDER_ORDER]
