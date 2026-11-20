"""One-time migration of v1 state (state/legacy/pipeline_v1.csv, dispatch_log_v1.jsonl, apollo exports).

Guarantees nobody v1 already emailed is ever contacted again: every v1 row becomes a lead
whose identity keys (email, LinkedIn slug, name+company) are registered in the store.
v1-sent leads are terminal for v2 (no automatic follow-ups — 64% of v1 sends went to
pattern-guessed addresses, so "following up" would compound the damage).
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..icp import detect_region
from ..models import (Company, EmailCandidate, EmailDraft, EmailMessage, Lead, LeadState, Person,
                      SendRecord, utcnow)
from ..store import Store
from . import upsert
from .csv_import import ingest_csv_text

log = logging.getLogger(__name__)

STATUS_MAP = {"Sent": LeadState.SENT, "Bounced": LeadState.BOUNCED, "Skipped": LeadState.DISQUALIFIED}


def _ts(raw: str) -> datetime:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return utcnow()


def _legacy_row_to_lead(r: dict, mode: str) -> Lead | None:
    email = (r.get("Target Email") or "").strip().lower()
    name = (r.get("Target Name") or "").strip()
    if not name and not email:
        return None
    parts = name.split()
    src = (r.get("Email Source") or "").lower()
    verified = (r.get("Email Verified") or "").upper() == "TRUE"
    trusted_domain = verified and ("apollo" in src or "hunter" in src)
    company_name = (r.get("Company") or "").strip()
    if company_name.lower() in ("linkedin", "linkedin ..."):
        company_name = ""
    domain = email.split("@", 1)[1] if (email and trusted_domain) else ""
    lead = Lead(
        id="", source="legacy_v1", source_detail=r.get("Geo Segment", ""), outreach_mode=r.get("Outreach Mode") or mode,
        vertical=(r.get("Vertical") or "").replace("MANUAL", ""),
        region=detect_region(r.get("Location", "")),
        person=Person(first_name=parts[0] if parts else "", last_name=parts[-1] if len(parts) > 1 else "",
                      full_name=name, title=r.get("Position", ""), linkedin_url=r.get("LinkedIn URL", ""),
                      location=r.get("Location", "")),
        company=Company(name=company_name, domain=domain, domain_source="legacy" if domain else "",
                        domain_confidence=0.8 if domain else 0.0,
                        description=(r.get("Company Description") or "")[:500]),
        legacy={k: r.get(k, "") for k in ("Date Sourced", "Week", "Confidence Score", "Title Tier",
                                          "Email Source", "Reason for Outreach", "Status")},
    )
    if email:
        lead.candidates.append(EmailCandidate(
            email=email, source=f"legacy:{src.split(' ')[0] or 'unknown'}",
            confidence=85 if verified else 20, provider_status="verified" if verified else "guessed"))
    status = r.get("Status", "Pending")
    if r.get("Drafted Email Body", "").strip():
        lead.draft = EmailDraft(messages=[EmailMessage(step=0, subject=r.get("Drafted Email Subject", ""),
                                                       body=r["Drafted Email Body"])], model="legacy-v1")
    if status in STATUS_MAP:
        lead.email = email
        lead.set_state(STATUS_MAP[status], f"v1 status {status}")
        if status == "Sent":
            lead.sends.append(SendRecord(step=0, sent_at=_ts(r.get("Sent At", "")), to=email,
                                         subject=r.get("Drafted Email Subject", ""), legacy=True))
            lead.legacy["no_followups"] = True
    else:
        lead.draft = None  # v1 drafts were ungrounded; pending leads get a fresh v2 pass
        lead.set_state(LeadState.DISCOVERED, "v1 pending lead re-entering the v2 pipeline")
    return lead


def migrate_legacy(store: Store, legacy_dir: Path, mode: str) -> dict:
    if store.meta.get("legacy_migrated_at"):
        return {"skipped": "already migrated"}
    stats = {"pipeline_rows": 0, "created": 0, "merged": 0, "dispatch_log_orphans": 0, "apollo_files": 0}
    pipe = legacy_dir / "pipeline_v1.csv"
    if pipe.exists():
        with open(pipe, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                stats["pipeline_rows"] += 1
                lead = _legacy_row_to_lead(r, mode)
                if lead is None:
                    continue
                _, created = upsert(store, lead)
                stats["created" if created else "merged"] += 1
    log_path = legacy_dir / "dispatch_log_v1.jsonl"
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            email = (ev.get("email") or "").lower()
            if not ev.get("success") or not email or store.email_known(email):
                continue
            stats["dispatch_log_orphans"] += 1
            lead = _legacy_row_to_lead({"Target Email": email, "Target Name": ev.get("name", ""),
                                        "Company": ev.get("company", ""), "Status": "Sent",
                                        "Sent At": ev.get("sent_at", ""),
                                        "Drafted Email Subject": ev.get("subject", "")}, mode)
            if lead:
                upsert(store, lead)
    for path in sorted((legacy_dir / "apollo_imports").glob("*.csv")):
        stats["apollo_files"] += 1
        res = ingest_csv_text(store, path.read_text(encoding="utf-8-sig", errors="replace"), path.name, mode)
        stats["merged"] += res.get("merged", 0)
        stats["created"] += res.get("created", 0)
    store.set_meta("legacy_migrated_at", utcnow().isoformat())
    store.event("legacy_migration", **stats)
    log.info("Legacy migration: %s", stats)
    return stats
