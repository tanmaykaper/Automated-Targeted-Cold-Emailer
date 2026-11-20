"""Drop-in CSV ingestion (Apollo export, LinkedIn / Sales Navigator export, any custom sheet).

Drop files into `inputs/` (commit them, or run locally). Columns are matched by alias
regardless of case/spacing, rows are normalised into `Lead`s, deduplicated against the
whole store, and the file is moved to `inputs/processed/<date>/`. Byte-identical files
are skipped via a content hash.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from ..icp import detect_region
from ..models import Company, EmailCandidate, Lead, LeadState, Person, utcnow
from ..store import Store
from . import upsert

log = logging.getLogger(__name__)

ALIASES: dict[str, tuple[str, ...]] = {
    "first_name": ("firstname", "first", "givenname"),
    "last_name": ("lastname", "last", "surname", "familyname"),
    "full_name": ("name", "fullname", "contactname", "targetname", "person"),
    "title": ("title", "jobtitle", "position", "designation", "role", "currenttitle"),
    "company": ("companyname", "company", "organization", "organisation", "accountname", "account",
                "currentcompany", "employer"),
    "email": ("email", "emailaddress", "workemail", "targetemail", "businessemail", "primaryemail"),
    "email_status": ("emailstatus", "emailverified", "emailverificationstatus", "primaryemailstatus"),
    "catch_all": ("primaryemailcatchallstatus", "catchallstatus", "catchall"),
    "email_confidence": ("emailconfidence", "confidence"),
    "verified_at": ("primaryemaillastverifiedat", "emaillastverifiedat", "lastverifiedat"),
    "linkedin": ("personlinkedinurl", "linkedinurl", "linkedin", "profileurl", "linkedinprofile",
                 "linkedinprofileurl", "url"),
    "website": ("website", "companywebsite", "domain", "companydomain", "websiteurl"),
    "company_linkedin": ("companylinkedinurl",),
    "city": ("city",),
    "state": ("state", "region"),
    "country": ("country",),
    "location": ("location", "geography", "personlocation"),
    "industry": ("industry",),
    "keywords": ("keywords", "companykeywords", "specialties"),
    "employees": ("employees", "numemployees", "companysize", "headcount"),
    "seniority": ("seniority",),
    "departments": ("departments", "department", "function"),
    "funding": ("latestfunding", "totalfunding", "fundingstage", "lastraisedat"),
    "vertical": ("vertical",),
}

UNUSABLE_EMAIL = re.compile(r"not[_ ]?unlocked|no[_ ]?email|unavailable|^n/?a$|^-$", re.I)


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower().replace("#", "num"))


def _build_map(headers: list[str]) -> dict[str, str]:
    normed = {_norm_header(h): h for h in headers}
    out: dict[str, str] = {}
    for field, aliases in ALIASES.items():
        for a in aliases:
            if a in normed:
                out[field] = normed[a]
                break
    return out


def detect_source(headers: list[str]) -> str:
    n = {_norm_header(h) for h in headers}
    if {"apollocontactid", "primaryemailsource"} & n or "personlinkedinurl" in n:
        return "csv:apollo"
    if {"profileurl", "salesnavigatorurl"} & n or ("url" in n and "position" in n):
        return "csv:linkedin"
    if {"targetemail", "targetname"} <= n:
        return "csv:legacy"
    return "csv:custom"


def _domain_from(website: str) -> str:
    w = (website or "").strip().lower()
    w = re.sub(r"^https?://", "", w)
    w = re.sub(r"^www\.", "", w)
    return w.split("/")[0].split("?")[0]


def _email_candidate(email: str, status: str, catch_all: str, confidence: str, verified_at: str,
                     source: str) -> EmailCandidate | None:
    email = (email or "").strip().lower()
    if not email or "@" not in email or UNUSABLE_EMAIL.search(email):
        return None
    st = (status or "").strip().lower()
    if st in ("verified", "true", "valid", "deliverable"):
        pstatus, conf = "verified", 85
    elif st in ("extrapolated", "guessed", "likely to engage", "unverified", "false", "risky"):
        pstatus, conf = "guessed", 30
    elif st in ("invalid", "bounced", "undeliverable"):
        pstatus, conf = "invalid", 0
    else:
        pstatus, conf = "unknown", 40
    try:
        conf = int(float(confidence)) if confidence and confidence.strip() else conf
    except ValueError:
        pass
    ca = (catch_all or "").strip().lower()
    catch_hint = True if ca.startswith("catch") else (False if ca.startswith("not") else None)
    checked = None
    if verified_at:
        try:
            checked = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
        except ValueError:
            checked = None
    return EmailCandidate(email=email, source=f"input:{source.split(':')[-1]}", confidence=conf,
                          provider_status=pstatus, provider_checked_at=checked, catch_all_hint=catch_hint)


def row_to_lead(row: dict, cmap: dict[str, str], source: str, source_detail: str, mode: str) -> Lead | None:
    g = lambda f: (row.get(cmap[f]) or "").strip() if f in cmap else ""  # noqa: E731
    first, last, full = g("first_name"), g("last_name"), g("full_name")
    if not full:
        full = f"{first} {last}".strip()
    if full and not first:
        parts = full.split()
        first, last = parts[0], (parts[-1] if len(parts) > 1 else "")
    company = g("company")
    if not full or not company:
        return None
    location = g("location") or ", ".join(p for p in (g("city"), g("state"), g("country")) if p)
    employees = None
    if emp := re.sub(r"[^0-9]", "", g("employees")):
        employees = int(emp) if len(emp) < 9 else None
    person = Person(first_name=first, last_name=last, full_name=full, title=g("title"),
                    seniority=g("seniority"), departments=g("departments"), linkedin_url=g("linkedin"),
                    location=location, city=g("city"), country=g("country"))
    website = g("website")
    comp = Company(name=company, website=website, domain=_domain_from(website),
                   domain_source="input" if website else "", domain_confidence=0.9 if website else 0.0,
                   linkedin_url=g("company_linkedin"), industry=g("industry"),
                   keywords=[k.strip() for k in g("keywords").split(",") if k.strip()][:60],
                   employees=employees, funding=g("funding"))
    lead = Lead(id="", source=source, source_detail=source_detail, outreach_mode=mode, person=person,
                company=comp, vertical=g("vertical") if g("vertical") not in ("", "MANUAL") else "",
                region=detect_region(location))
    cand = _email_candidate(g("email"), g("email_status"), g("catch_all"), g("email_confidence"),
                            g("verified_at"), source)
    if cand:
        lead.candidates.append(cand)
    return lead


def ingest_csv_text(store: Store, text: str, name: str, mode: str) -> dict:
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    cmap = _build_map(headers)
    source = detect_source(headers)
    stats = {"file": name, "source": source, "rows": 0, "created": 0, "merged": 0, "skipped": 0}
    if "company" not in cmap or not ({"full_name"} & cmap.keys() or {"first_name"} & cmap.keys()):
        log.error("%s: could not find name/company columns (headers: %s)", name, headers[:12])
        stats["error"] = "missing name/company columns"
        return stats
    for row in reader:
        stats["rows"] += 1
        try:
            lead = row_to_lead(row, cmap, source, name, mode)
        except Exception as e:  # malformed row → skip this row only
            log.warning("%s row %d unreadable: %s", name, stats["rows"], e)
            lead = None
        if lead is None:
            stats["skipped"] += 1
            continue
        _, created = upsert(store, lead)
        stats["created" if created else "merged"] += 1
    return stats


def ingest_dropins(store: Store, inputs_dir: Path, extra_dirs: tuple[Path, ...] = (), mode: str = "internship") -> list[dict]:
    """Ingest every *.csv in inputs/ (and legacy state/apollo_imports/), then archive it."""
    results = []
    seen: dict = store.meta.setdefault("ingested_files", {})
    for d in (inputs_dir, *extra_dirs):
        if not d.exists():
            continue
        for path in sorted(d.glob("*.csv")):
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()[:16]
            dest = inputs_dir / "processed" / utcnow().strftime("%Y-%m-%d") / path.name
            if digest in seen:
                log.info("Skipping %s (identical to already-ingested %s)", path.name, seen[digest])
                results.append({"file": path.name, "skipped_duplicate_of": seen[digest]})
            else:
                text = raw.decode("utf-8-sig", errors="replace")
                res = ingest_csv_text(store, text, path.name, mode)
                results.append(res)
                if "error" in res:
                    continue  # leave the file in place so the user sees it wasn't consumed
                seen[digest] = path.name
                store.set_meta("ingested_files", dict(seen))
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), dest)
    return results


def reset_for_sandbox(lead: Lead) -> Lead:
    """Dry-run helper: treat an imported lead as brand new."""
    lead.set_state(LeadState.DISCOVERED, "sandbox")
    lead.sends.clear()
    lead.verification = lead.dossier = lead.score = lead.draft = None
    return lead
