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
    _build_reason, _score_lead, _get_company_desc,
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


def run_apollo_import() -> dict:
    """Processes any new CSVs sitting in state/apollo_imports/, AND sweeps
    state/apollo_imports/done/ (everything already processed by a prior
    run). New emails get appended as before. Emails that already exist in
    the pipeline get their Company Description / Reason for Outreach /
    Confidence Score refreshed IN PLACE (no duplicate row) — but only if
    the description currently on file still looks thin (e.g. from before
    the Serper-researched description existed, or a company Serper simply
    didn't have anything for last time). Rows that already have a decent
    description are left alone and never cost a Serper call, so re-running
    this repeatedly is cheap once things are fixed.

    This means simply re-running the action (no re-upload needed) is
    exactly how you backfill better research onto leads already sitting
    in state/pipeline.csv from before.
    """
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)

    new_paths      = sorted(glob.glob(str(IMPORT_DIR / "*.csv")))       # not yet processed
    archived_paths = sorted(glob.glob(str(DONE_DIR / "*.csv")))          # already processed

    if not new_paths and not archived_paths:
        log.info("No CSVs in %s (new or archived) — nothing to do.", IMPORT_DIR)
        return {"leads": [], "updated": 0, "checked": 0}

    from module_b_spreadsheet import read_all_leads, refresh_lead_context

    existing_by_email = {r["Target Email"].strip().lower(): r
                          for r in read_all_leads() if r["Target Email"].strip()}

    week_num = datetime.now(timezone.utc).isocalendar()[1]
    seen_batch: set = set()   # dedup across this run's own files

    new_leads = []
    checked = updated = skipped_bad = skipped_already_good = 0

    MIN_GOOD_DESC_LEN = 40   # below this, treat the stored description as still-thin/pre-fix

    def _process(path: str, is_new_file: bool) -> None:
        nonlocal updated, checked, skipped_bad, skipped_already_good
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cheap_email = (row.get("Email") or "").strip().lower()

                # Cheap skip BEFORE spending a Serper call: already in the
                # pipeline with a decent description already on file.
                if cheap_email and cheap_email in existing_by_email:
                    current_desc = existing_by_email[cheap_email].get("Company Description", "")
                    if len(current_desc.strip()) >= MIN_GOOD_DESC_LEN:
                        skipped_already_good += 1
                        continue

                lead = _row_to_lead(row, week_num)
                if lead is None:
                    skipped_bad += 1
                    continue
                checked += 1

                if lead.email in existing_by_email or lead.email in seen_batch:
                    if refresh_lead_context(lead.email, lead.company_description,
                                            lead.reason_for_outreach, lead.confidence_score):
                        updated += 1
                    continue

                seen_batch.add(lead.email)
                new_leads.append(lead)

        if is_new_file:
            shutil.move(path, DONE_DIR / Path(path).name)

    for path in new_paths:
        _process(path, is_new_file=True)
    for path in archived_paths:
        _process(path, is_new_file=False)

    log.info("Apollo import: %d new leads, %d existing rows refreshed, "
              "%d already-good skipped, %d unusable skipped (%d checked)",
              len(new_leads), updated, skipped_already_good, skipped_bad, checked)

    return {"leads": new_leads, "updated": updated, "checked": checked}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run_apollo_import()
    for l in result["leads"]:
        print(f"{l.confidence_score:3d} | {l.name} | {l.position} @ {l.company} | {l.email}")
    print(f"Refreshed {result['updated']} existing rows")
