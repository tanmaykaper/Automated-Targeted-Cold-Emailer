"""
IMPORT: Apollo.io Manual Contact Exports
═════════════════════════════════════════════════════════════════════════
For when you've manually pulled a contact list in Apollo's UI (no API,
no automation — just their normal export-to-CSV feature) and want it
merged into state/pipeline.csv for drafting/dispatch, same as any other
source.

Usage:
  1. Drop one or more Apollo "Export Contacts" CSVs into state/apollo_imports/
  2. Run:  ACTION=apollo_import python orchestrator.py
     (or trigger "Apollo Import" from the Actions tab)
  3. Every row is run through the same title-tier filter + confidence
     scoring the rest of the pipeline uses, deduplicated by email against
     EVERYTHING already in state/pipeline.csv (across all your Apollo
     files too, in case the same contact appears in more than one export
     — very common when you re-run/refine an Apollo search), then
     appended. Processed files are moved to state/apollo_imports/done/
     so re-running the action doesn't reprocess them.

Note: unlike automated/manual-intake sourcing, rows here are NOT dropped
for scoring below SCORE_THRESHOLD — you already hand-picked this list in
Apollo, so the score is recorded (visible in pipeline.xlsx) for your own
triage rather than used as a silent filter. Rows ARE still dropped if the
title matches a REJECT_TITLES pattern (recruiter/HR/etc.) or there's no
usable email, same as everywhere else in the pipeline.

Company Description is NOT taken from Apollo's own Industry/Keywords
fields (too generic — usually just a one- or two-word category). It's
researched live via the same Serper company-overview lookup the rest of
the pipeline uses, cached per company for this run so multiple contacts
from the same firm only cost one lookup. Requires SERPER_API_KEY to be
set; without it this field is left blank rather than falling back to the
generic Apollo text.
"""

import csv
import glob
import logging
import shutil
import time
from pathlib import Path
from datetime import datetime, timezone

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from module_a_sourcing import (
    Lead, _title_tier, _classify_company, _is_remote,
    _build_reason, _score_lead, _load_seen_emails, _get_company_desc,
)
import module_a_sourcing as _sourcing
from config import OUTREACH_MODE

log = logging.getLogger(__name__)

STATE_DIR   = Path(__file__).resolve().parent.parent / "state"
IMPORT_DIR  = STATE_DIR / "apollo_imports"
DONE_DIR    = IMPORT_DIR / "done"

BAD_EMAIL_MARKERS = ("not_unlocked", "email_not_unlocked", "no_email", "")

# Apollo's own "Industry"/"Keywords" fields are too generic to use as the
# outreach-facing Company Description (e.g. just "management consulting").
# Real descriptions are researched live via the same Serper lookup the rest
# of the pipeline uses, cached per company for this run — several contacts
# from the same firm (very common in an Apollo export) share one lookup
# instead of paying for it per row.
_company_desc_cache: dict = {}


def _researched_company_desc(company: str) -> str:
    key = company.strip().lower()
    if not key:
        return ""
    if key in _company_desc_cache:
        return _company_desc_cache[key]
    if _sourcing._serper_calls >= _sourcing._SERPER_CALL_CEILING:
        log.warning("Serper call budget exhausted for this run — leaving "
                    "Company Description blank for %s and any companies after it", company)
        return ""
    desc = _get_company_desc(company)
    time.sleep(0.6)
    _company_desc_cache[key] = desc
    return desc


def _row_to_lead(row: dict, week_num: int) -> "Lead | None":
    first = (row.get("First Name") or "").strip()
    last  = (row.get("Last Name") or "").strip()
    name  = f"{first} {last}".strip()
    title = (row.get("Title") or "").strip()
    if not name or not title:
        return None

    tier = _title_tier(title)
    if tier is None:
        return None

    email = (row.get("Email") or "").strip().lower()
    if not email or any(marker in email for marker in BAD_EMAIL_MARKERS if marker):
        return None

    company     = (row.get("Company Name") or "").strip()
    linkedin    = (row.get("Person Linkedin Url") or "").strip()
    city        = (row.get("City") or "").strip()
    country     = (row.get("Country") or "").strip()
    location    = ", ".join(p for p in (city, country) if p)
    industry    = (row.get("Industry") or "").strip()
    keywords    = (row.get("Keywords") or "").strip()
    # Only used to help classify company type / remote-ness — never shown
    # to the recipient or stored as the Company Description.
    classification_hint = f"{industry} {keywords}".strip()

    email_status = (row.get("Email Status") or "").strip().lower()
    verified = email_status == "verified"

    company_type, funding_stage = _classify_company(company, classification_hint)
    is_remote = _is_remote(classification_hint, title, location)
    company_desc = _researched_company_desc(company) if company else ""

    lead = Lead(
        name=name, company=company, position=title,
        email=email, email_verified=verified,
        email_source="apollo" if verified else "apollo-unverified",
        linkedin_url=linkedin, location=location,
        geo_segment="MANUAL", vertical="MANUAL",
        company_type=company_type, funding_stage=funding_stage,
        is_remote_role=is_remote, company_description=company_desc,
        title_tier=tier, outreach_mode=OUTREACH_MODE,
        source="Apollo-Manual-Export",
        week_sourced=f"{datetime.now(timezone.utc).year}-W{week_num:02d}",
    )
    lead.reason_for_outreach = _build_reason(lead)
    lead.confidence_score    = _score_lead(lead)
    return lead


def run_apollo_import() -> list:
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)

    csv_paths = sorted(glob.glob(str(IMPORT_DIR / "*.csv")))
    if not csv_paths:
        log.info("No CSVs in %s — nothing to import.", IMPORT_DIR)
        return []

    week_num = datetime.now(timezone.utc).isocalendar()[1]
    seen_hist = _load_seen_emails()          # everything already in pipeline.csv
    seen_batch: set = set()                  # dedup across the Apollo files themselves

    leads = []
    skipped_dupe = skipped_bad = 0

    for path in csv_paths:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                lead = _row_to_lead(row, week_num)
                if lead is None:
                    skipped_bad += 1
                    continue
                if lead.email in seen_hist or lead.email in seen_batch:
                    skipped_dupe += 1
                    continue
                seen_batch.add(lead.email)
                leads.append(lead)

        shutil.move(path, DONE_DIR / Path(path).name)

    log.info("Apollo import: %d files -> %d new leads (%d duplicates skipped, %d unusable skipped)",
              len(csv_paths), len(leads), skipped_dupe, skipped_bad)
    return leads


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    for l in run_apollo_import():
        print(f"{l.confidence_score:3d} | {l.name} | {l.position} @ {l.company} | {l.email}")
