"""Autonomous lead discovery adapters (plug-and-play; each runs only if its provider is available).

* ApolloDiscovery   POST /api/v1/mixed_people/api_search (0 credits, no emails returned). People with
                    `has_email` are queued; the email itself comes from /people/match in the enrichment
                    waterfall (1 credit each, capped by APOLLO_MATCH_BUDGET).
* SearchDiscovery   `site:linkedin.com/in` queries through Serper / Tavily / DuckDuckGo, with strict
                    result parsing: truncated ("…") or missing companies are dropped, never guessed.
"""
from __future__ import annotations

import logging
import re

from ..http import Http, ProviderError
from ..icp import detect_region, title_tier
from ..models import Company, Lead, Person, utcnow
from ..profile import DISCOVERY_ROTATION, DISCOVERY_TITLES, VERTICALS
from ..settings import Settings
from ..store import Store
from ..web import Search, SearchResult
from . import upsert

log = logging.getLogger(__name__)

_SEP = re.compile(r"\s+[-–—|]\s+")
_EXPERIENCE = re.compile(r"Experience:\s*([^·|]+?)\s*(?:·|\||$)")
_LOCATION = re.compile(r"Location:\s*([^·|]+?)\s*(?:·|\||$)")
_NAME_OK = re.compile(r"^[A-Za-zÀ-ÿ.'’\-]+(?: [A-Za-zÀ-ÿ.'’\-]+){1,3}$")


def _truncated(s: str) -> bool:
    return s.endswith(("...", "…")) or not s.strip()


def parse_linkedin_result(r: SearchResult) -> dict | None:
    if "linkedin.com/in/" not in r.url:
        return None
    raw = re.sub(r"\s*[|·]\s*LinkedIn.*$", "", r.title, flags=re.I).strip()
    parts = [p.strip() for p in _SEP.split(raw) if p.strip()]
    if not parts or not _NAME_OK.match(parts[0]):
        return None
    name = re.sub(r"\s*\(.*?\)", "", parts[0]).strip()
    title, company = "", ""
    if len(parts) >= 2:
        title = parts[1]
        m = re.match(r"^(.+?)\s+(?:at|@)\s+(.+)$", title, re.I)
        if m:
            title, company = m.group(1).strip(), m.group(2).strip()
    if len(parts) >= 3 and not company:
        company = parts[2]
    exp = _EXPERIENCE.search(r.snippet or "")
    if exp and not _truncated(exp.group(1)):
        company = exp.group(1).strip()
    loc = _LOCATION.search(r.snippet or "")
    if _truncated(company) or company.lower() in ("linkedin", "self-employed", "freelance") or _truncated(title):
        return None
    if title_tier(title)[0] is None:
        return None
    return {"name": name, "title": title, "company": company, "location": loc.group(1).strip() if loc else "",
            "linkedin_url": re.sub(r"\?.*$", "", r.url)}


class SearchDiscovery:
    def __init__(self, search: Search, settings: Settings):
        self.search = search
        self.s = settings

    def run(self, store: Store, limit: int) -> dict:
        week = utcnow().isocalendar()[1]
        slot = DISCOVERY_ROTATION[week % len(DISCOVERY_ROTATION)]
        stats = {"queries": 0, "results": 0, "parsed": 0, "created": 0}
        for i, (geo, vertical) in enumerate(slot):
            kws = VERTICALS[vertical]["keywords"]
            for j in range(2):
                if stats["created"] >= limit:
                    return stats
                title = DISCOVERY_TITLES[(week + i * 2 + j) % len(DISCOVERY_TITLES)]
                kw = kws[(week + j) % len(kws)]
                q = f'site:linkedin.com/in "{title}" "{kw}" {geo} -jobs -hiring'
                try:
                    results = self.search.search(q, num=10)
                except ProviderError as e:
                    log.info("discovery search failed (%s)", e.category)
                    continue
                stats["queries"] += 1
                stats["results"] += len(results)
                for r in results:
                    p = parse_linkedin_result(r)
                    if not p:
                        continue
                    stats["parsed"] += 1
                    parts = p["name"].split()
                    lead = Lead(id="", source=f"search:{r.provider}", source_detail=q, outreach_mode=self.s.outreach_mode,
                                vertical=vertical, region=detect_region(p["location"] or geo),
                                person=Person(first_name=parts[0], last_name=parts[-1], full_name=p["name"],
                                              title=p["title"], linkedin_url=p["linkedin_url"],
                                              location=p["location"] or geo),
                                company=Company(name=p["company"]))
                    _, created = upsert(store, lead)
                    stats["created"] += int(created)
        return stats


class ApolloDiscovery:
    SENIORITIES = ["owner", "founder", "c_suite", "partner", "vp", "director", "head", "manager"]

    def __init__(self, http: Http, settings: Settings):
        self.http = http
        self.s = settings

    def enabled(self) -> bool:
        return bool(self.s.keys.apollo) and self.http.available("apollo")

    def run(self, store: Store, limit: int) -> dict:
        week = utcnow().isocalendar()[1]
        slot = DISCOVERY_ROTATION[week % len(DISCOVERY_ROTATION)]
        stats = {"queries": 0, "results": 0, "created": 0}
        for geo, vertical in slot:
            if stats["created"] >= limit:
                break
            body = {"person_titles": list(DISCOVERY_TITLES), "person_seniorities": self.SENIORITIES,
                    "q_keywords": VERTICALS[vertical]["keywords"][week % len(VERTICALS[vertical]["keywords"])],
                    "page": 1 + (week // len(DISCOVERY_ROTATION)) % 5, "per_page": 25}
            if geo not in ("remote", "think tank"):
                body["person_locations"] = [geo]
            try:
                data = self.http.request("POST", "https://api.apollo.io/api/v1/mixed_people/api_search",
                                         provider="apollo", json=body,
                                         headers={"x-api-key": self.s.keys.apollo, "Cache-Control": "no-cache"})
            except ProviderError as e:
                if e.category == "auth":
                    log.error("Apollo People API Search refused (HTTP %s). Free-plan API access requires an Apollo "
                              "account registered with a work email; keep using CSV exports via inputs/.", e.status)
                    break
                continue
            stats["queries"] += 1
            for p in data.get("people") or []:
                stats["results"] += 1
                if not p.get("has_email") or not p.get("id"):
                    continue
                org = p.get("organization") or {}
                lead = Lead(id="", source="apollo_api", source_detail=f"{geo}/{vertical}", outreach_mode=self.s.outreach_mode,
                            vertical=vertical, region=detect_region(geo),
                            person=Person(first_name=p.get("first_name", ""), title=p.get("title", ""), location=geo),
                            company=Company(name=org.get("name", "")), legacy={"apollo_id": p["id"]})
                _, created = upsert(store, lead)
                stats["created"] += int(created)
                if stats["created"] >= limit:
                    break
        return stats
