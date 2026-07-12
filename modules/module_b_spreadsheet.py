"""
MODULE B: CSV Pipeline Store + Excel Export
════════════════════════════════════════════
This is the module every other file actually imports against:
  - module_a_sourcing.py   -> read_all_leads()            (historical dedup)
  - orchestrator.py        -> append_leads(), get_stats(), export_excel(),
                               read_pending_without_draft(), write_draft()
  - module_d_dispatch.py   -> read_pending_with_draft(), mark_sent()

state/pipeline.csv is THE DATABASE — additive, never overwritten wholesale
except to normalize the header when new columns are introduced (existing
rows are preserved, just backfilled with blank values for new columns).

NOTE (previous version of this file): an earlier revision of this module
was written against Google Sheets + a different function signature set
than orchestrator.py, module_a_sourcing.py, and module_d_dispatch.py
actually call. That mismatch meant `source` crashed inside get_stats()/
export_excel(), and `draft` failed immediately on import (read_pending_
without_draft/write_draft didn't exist) — so nothing ever persisted to
state/, independent of how good the sourced leads were. This rewrite
restores the CSV-based interface the rest of the codebase expects.
"""

import os
import csv
import logging
from pathlib import Path
from dataclasses import asdict, is_dataclass

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.worksheet.datavalidation import DataValidation

log = logging.getLogger(__name__)

STATE_DIR   = Path(__file__).resolve().parent.parent / "state"
CSV_PATH    = STATE_DIR / "pipeline.csv"
EXCEL_PATH  = STATE_DIR / "pipeline.xlsx"

# ──────────────────────────────────────────────
# COLUMN SCHEMA
# ──────────────────────────────────────────────
# Matches the header already in state/pipeline.csv, plus two columns
# (Email Verified / Email Source) that module_a_sourcing.Lead has always
# populated but which the CSV never had a slot for — added here so the
# 5-tier verified-email waterfall's output is actually visible/auditable
# instead of silently discarded on write.
CSV_COLUMNS = [
    "Date Sourced", "Week", "Confidence Score", "Title Tier",
    "Target Name", "Company", "Position",
    "Target Email", "Email Verified", "Email Source",
    "LinkedIn URL", "Location", "Geo Segment", "Vertical",
    "Company Type", "Funding Stage", "Is Remote", "Outreach Mode",
    "Company Description", "Reason for Outreach",
    "Drafted Email Subject", "Drafted Email Body",
    "Status", "Sent At",
]

APPROVAL_STATUSES = ["Pending", "Sent", "Skipped", "Bounced"]

COLUMN_WIDTHS = {
    "Target Name": 20, "Company": 26, "Position": 28,
    "Target Email": 30, "Email Verified": 12, "Email Source": 22,
    "LinkedIn URL": 40, "Location": 20, "Company Description": 40,
    "Reason for Outreach": 42, "Drafted Email Subject": 28,
    "Drafted Email Body": 70, "Status": 12,
}


# ──────────────────────────────────────────────
# LOW-LEVEL CSV I/O
# ──────────────────────────────────────────────
def _read_rows() -> list[dict]:
    """Reads every row, normalized to CSV_COLUMNS (missing legacy columns
    backfilled with ''). Full-file read is fine at this volume (hundreds,
    not millions, of rows) and keeps schema migration trivial/safe."""
    if not CSV_PATH.exists():
        return []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        for r in reader:
            row = {col: (r.get(col) or "") for col in CSV_COLUMNS}
            rows.append(row)
        return rows


def _write_rows(rows: list[dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})


def _lead_to_row(lead) -> dict:
    """Accepts either a module_a_sourcing.Lead dataclass or a plain dict."""
    d = asdict(lead) if is_dataclass(lead) else dict(lead)
    from datetime import datetime, timezone
    return {
        "Date Sourced":          datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "Week":                  d.get("week_sourced", ""),
        "Confidence Score":      d.get("confidence_score", ""),
        "Title Tier":            d.get("title_tier", ""),
        "Target Name":           d.get("name", ""),
        "Company":               d.get("company", ""),
        "Position":              d.get("position", ""),
        "Target Email":          d.get("email", ""),
        "Email Verified":        "TRUE" if d.get("email_verified") else "FALSE",
        "Email Source":          d.get("email_source", ""),
        "LinkedIn URL":          d.get("linkedin_url", ""),
        "Location":              d.get("location", ""),
        "Geo Segment":           d.get("geo_segment", ""),
        "Vertical":              d.get("vertical", ""),
        "Company Type":          d.get("company_type", ""),
        "Funding Stage":         d.get("funding_stage", ""),
        "Is Remote":             "TRUE" if d.get("is_remote_role") else "FALSE",
        "Outreach Mode":         d.get("outreach_mode", ""),
        "Company Description":  d.get("company_description", ""),
        "Reason for Outreach":  d.get("reason_for_outreach", ""),
        "Drafted Email Subject": "",
        "Drafted Email Body":   "",
        "Status":               "Pending",
        "Sent At":               "",
    }


# ──────────────────────────────────────────────
# PUBLIC INTERFACE — used by module_a_sourcing, orchestrator, module_d
# ──────────────────────────────────────────────
def read_all_leads() -> list[dict]:
    """Full historical pipeline — used by module_a_sourcing for dedup
    against already-sourced emails."""
    return _read_rows()


def read_pending_without_draft() -> list[dict]:
    """Rows sourced but not yet drafted — feeds Module C."""
    return [r for r in _read_rows()
            if r["Status"] == "Pending" and not r["Drafted Email Body"].strip()]


def read_pending_with_draft() -> list[dict]:
    """Rows drafted and ready to send — feeds Module D."""
    return [r for r in _read_rows()
            if r["Status"] == "Pending" and r["Drafted Email Body"].strip()]


def write_draft(target_email: str, subject: str, body: str) -> bool:
    """Writes a drafted subject/body back to the matching row(s)."""
    rows = _read_rows()
    updated = False
    for row in rows:
        if row["Target Email"].strip().lower() == target_email.strip().lower():
            row["Drafted Email Subject"] = subject
            row["Drafted Email Body"] = body
            updated = True
    if updated:
        _write_rows(rows)
    else:
        log.warning("write_draft: no row found for %s", target_email)
    return updated


def mark_sent(target_email: str, sent_at_iso: str) -> bool:
    """Flips Status -> Sent and records the timestamp. Used by module_d."""
    rows = _read_rows()
    updated = False
    for row in rows:
        if row["Target Email"].strip().lower() == target_email.strip().lower() \
           and row["Status"] != "Sent":
            row["Status"] = "Sent"
            row["Sent At"] = sent_at_iso
            updated = True
    if updated:
        _write_rows(rows)
    return updated


def append_leads(leads: list) -> int:
    """Appends new leads, de-duplicated by Target Email against the full
    existing pipeline. Returns count actually added."""
    if not leads:
        return 0
    rows = _read_rows()
    existing_emails = {r["Target Email"].strip().lower() for r in rows if r["Target Email"]}

    added = 0
    for lead in leads:
        row = _lead_to_row(lead)
        email = row["Target Email"].strip().lower()
        if not email or email in existing_emails:
            continue
        rows.append(row)
        existing_emails.add(email)
        added += 1

    if added:
        _write_rows(rows)
    return added


def get_stats() -> dict:
    rows = _read_rows()
    total    = len(rows)
    drafted  = sum(1 for r in rows if r["Drafted Email Body"].strip())
    sent     = sum(1 for r in rows if r["Status"] == "Sent")
    no_draft = sum(1 for r in rows if r["Status"] == "Pending" and not r["Drafted Email Body"].strip())
    unverified = sum(1 for r in rows if r["Email Verified"].upper() != "TRUE")
    return {
        "total": total, "drafted": drafted, "sent": sent,
        "no_draft": no_draft, "unverified": unverified,
    }


def export_excel() -> str:
    """Regenerates state/pipeline.xlsx from the CSV, styled + with an
    Approval-style Status dropdown for manual edits, and amber highlighting
    on unverified-email rows so bounce-risk sends are obvious at a glance."""
    rows = _read_rows()
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Outbound Pipeline"

    header_fill = PatternFill(start_color="1E3D6A", end_color="1E3D6A", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=10)
    alt_fill    = PatternFill(start_color="EEF2F7", end_color="EEF2F7", fill_type="solid")
    unverified_fill = PatternFill(start_color="FFE066", end_color="FFE066", fill_type="solid")

    for col_idx, col_name in enumerate(CSV_COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[cell.column_letter].width = COLUMN_WIDTHS.get(col_name, 18)
    ws.row_dimensions[1].height = 22
    ws.freeze_panes = "A2"

    status_col_letter = chr(ord("A") + CSV_COLUMNS.index("Status"))
    dv = DataValidation(type="list", formula1=f'"{",".join(APPROVAL_STATUSES)}"',
                         showDropDown=False, sqref=f"{status_col_letter}2:{status_col_letter}5000")
    ws.add_data_validation(dv)

    for i, row in enumerate(rows, start=2):
        verified = row["Email Verified"].upper() == "TRUE"
        fill = None if verified else unverified_fill
        if verified and i % 2 == 0:
            fill = alt_fill
        for col_idx, col_name in enumerate(CSV_COLUMNS, start=1):
            cell = ws.cell(row=i, column=col_idx, value=row.get(col_name, ""))
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if fill:
                cell.fill = fill
        ws.row_dimensions[i].height = 45

    wb.save(EXCEL_PATH)
    log.info("Excel exported to %s (%d rows)", EXCEL_PATH, len(rows))
    return str(EXCEL_PATH)
