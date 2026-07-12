"""
MODULE A2: Manual LinkedIn Intake (Premium / Sales Navigator — ToS-safe)
═════════════════════════════════════════════════════════════════════════
Purpose: get real, current LinkedIn leads into the pipeline WITHOUT any
automated login, scraping, or bot traffic against linkedin.com — so there
is no LinkedIn ToS exposure and no account-ban risk, on any account.

How it's meant to be used (~15-20 min/week):
  1. Browse LinkedIn / Sales Navigator normally, as a human, using your
     Premium filters (title, seniority, geography, company size, etc.)
     to find ~20-25 people matching this week's targets.
  2. For each one, copy name / title / company / profile URL into
     state/manual_leads_inbox.csv (one row per person — see the header
     for the exact columns, or open the file in Excel/Sheets).
  3. Run:  ACTION=manual_intake python orchestrator.py
     This enriches each row through the SAME domain-resolution + 5-tier
     verified email waterfall + scoring pipeline module_a_sourcing.py
     uses for automated sources, then appends qualified leads to
     state/pipeline.csv exactly like a normal source run.
  4. Rows that resolve to a verified/qualified lead are removed from the
     inbox; rows that fail (no title match, no email resolvable) are
     logged and left in the inbox for you to fix or delete.

This intentionally does NOT touch your LinkedIn session in any way —
it only reads a CSV you filled in by hand.
"""

import csv
import logging
from pathlib import Path

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from module_a_sourcing import enrich_and_score_candidate, _load_seen_emails
from config import SCORE_THRESHOLD

log = logging.getLogger(__name__)

STATE_DIR  = Path(__file__).resolve().parent.parent / "state"
INBOX_PATH = STATE_DIR / "manual_leads_inbox.csv"

INBOX_COLUMNS = ["linkedin_url", "name", "title", "company", "location", "geo_segment", "vertical"]


def _ensure_inbox_exists() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not INBOX_PATH.exists():
        with open(INBOX_PATH, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=INBOX_COLUMNS).writeheader()


def _read_inbox() -> list[dict]:
    _ensure_inbox_exists()
    with open(INBOX_PATH, newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def _write_inbox(rows: list[dict]) -> None:
    with open(INBOX_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=INBOX_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in INBOX_COLUMNS})


def run_manual_intake_pipeline() -> list:
    """Reads state/manual_leads_inbox.csv, enriches + scores each row
    through the shared waterfall, returns list[Lead] that qualified.
    Rows that resolved successfully are removed from the inbox; rows
    that failed are kept (with a reason logged) so you can fix/retry."""
    rows = _read_inbox()
    if not rows:
        log.info("Manual intake inbox is empty — nothing to process.")
        return []

    seen_hist = _load_seen_emails()
    seen_run: set = set()

    qualified: list = []
    remaining: list[dict] = []

    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            continue  # blank row, drop silently

        profile = {
            "name":         name,
            "title":        (row.get("title") or "").strip(),
            "company":      (row.get("company") or "").strip(),
            "location":     (row.get("location") or "").strip(),
            "linkedin_url": (row.get("linkedin_url") or "").strip(),
            "summary":      "",
        }
        geo_id      = (row.get("geo_segment") or "MANUAL").strip() or "MANUAL"
        vertical_id = (row.get("vertical") or "MANUAL").strip() or "MANUAL"

        lead = enrich_and_score_candidate(
            profile, geo_id, vertical_id,
            source_tag="Manual-LinkedIn-Premium",
            seen_hist=seen_hist, seen_run=seen_run,
        )

        if lead and lead.confidence_score >= SCORE_THRESHOLD:
            qualified.append(lead)
            log.info("✓ Resolved %s @ %s -> %s", name, profile["company"], lead.email)
        else:
            log.warning("✗ Could not resolve/qualify %s @ %s — left in inbox for retry",
                        name, profile["company"])
            remaining.append(row)

    _write_inbox(remaining)
    log.info("Manual intake done: %d qualified, %d left in inbox", len(qualified), len(remaining))
    return qualified


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    leads = run_manual_intake_pipeline()
    for l in leads:
        print(l.name, "|", l.email, "|", l.email_source)
