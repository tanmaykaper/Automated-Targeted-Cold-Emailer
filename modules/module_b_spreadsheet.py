"""
MODULE B: Pipeline State Manager
══════════════════════════════════
Storage: CSV (state/pipeline.csv) — committed to git, additive only.
Excel:   state/pipeline.xlsx — generated fresh each run for human viewing.

Key design decisions (per latest spec):
  • ADDITIVE ONLY — never overwrites existing rows.
    Each run appends NEW leads. Existing rows (and their Sent status) are untouched.
  • Deduplication by Target Email across entire historical file.
  • Status = "Pending" | "Sent" only (no Approved / Rejected / HITL gate).
  • Dispatch reads all Pending rows that have a draft — no approval step.
  • Excel is regenerated each run for easy human review (view-only, not the source of truth).
"""

import os, csv, logging
from datetime import datetime, timezone
from pathlib  import Path

log = logging.getLogger(__name__)

REPO_ROOT    = Path(__file__).parent.parent
STATE_DIR    = REPO_ROOT / "state"
PIPELINE_CSV = STATE_DIR / "pipeline.csv"
PIPELINE_XLS = STATE_DIR / "pipeline.xlsx"

COLUMNS = [
    "Date Sourced",
    "Week",
    "Confidence Score",
    "Title Tier",           # A / B / C
    "Target Name",
    "Company",
    "Position",
    "Target Email",
    "LinkedIn URL",
    "Location",
    "Geo Segment",
    "Vertical",
    "Company Type",         # Tier-1 | Startup | Research | Corporate
    "Funding Stage",
    "Is Remote",
    "Outreach Mode",        # internship | job
    "Company Description",
    "Reason for Outreach",
    "Drafted Email Subject",
    "Drafted Email Body",
    "Status",               # Pending | Sent
    "Sent At",              # UTC timestamp
]


# ══════════════════════════════════════════════════════════════════════════
# CSV — source of truth
# ══════════════════════════════════════════════════════════════════════════

def _ensure_csv() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not PIPELINE_CSV.exists():
        with open(PIPELINE_CSV, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=COLUMNS).writeheader()
        log.info("Created %s", PIPELINE_CSV)


def read_all_leads() -> list:
    _ensure_csv()
    with open(PIPELINE_CSV, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _email_exists(email: str, all_rows: list) -> bool:
    e = email.strip().lower()
    return any(r.get("Target Email","").strip().lower() == e for r in all_rows)


def append_leads(leads: list) -> int:
    """
    Append new leads to CSV. Skips any email already present.
    NEVER modifies existing rows. Returns count written.
    """
    _ensure_csv()
    existing = read_all_leads()
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    written  = 0

    with open(PIPELINE_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")

        for lead in leads:
            email = getattr(lead, "email", None) or lead.get("email", "")
            if not email or _email_exists(email, existing):
                continue

            row = {
                "Date Sourced":          today,
                "Week":                  getattr(lead, "week_sourced", ""),
                "Confidence Score":      getattr(lead, "confidence_score", 0),
                "Title Tier":            getattr(lead, "title_tier", ""),
                "Target Name":           getattr(lead, "name", ""),
                "Company":               getattr(lead, "company", ""),
                "Position":              getattr(lead, "position", ""),
                "Target Email":          email,
                "LinkedIn URL":          getattr(lead, "linkedin_url", ""),
                "Location":              getattr(lead, "location", ""),
                "Geo Segment":           getattr(lead, "geo_segment", ""),
                "Vertical":              getattr(lead, "vertical", ""),
                "Company Type":          getattr(lead, "company_type", ""),
                "Funding Stage":         getattr(lead, "funding_stage", ""),
                "Is Remote":             str(getattr(lead, "is_remote_role", False)),
                "Outreach Mode":         getattr(lead, "outreach_mode", ""),
                "Company Description":   getattr(lead, "company_description", ""),
                "Reason for Outreach":   getattr(lead, "reason_for_outreach", ""),
                "Drafted Email Subject": "",
                "Drafted Email Body":    "",
                "Status":                "Pending",
                "Sent At":               "",
            }
            w.writerow(row)
            # Add to in-memory list so duplicates within same batch are also caught
            existing.append(row)
            written += 1
            log.info("+ %s | %s | %s", row["Target Name"], row["Company"], email)

    log.info("Appended %d new leads", written)
    return written


def _rewrite_csv(rows: list) -> None:
    with open(PIPELINE_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def update_field(email: str, field: str, value: str) -> bool:
    rows    = read_all_leads()
    updated = False
    for r in rows:
        if r.get("Target Email","").strip().lower() == email.strip().lower():
            r[field] = value
            updated  = True
            break
    if updated:
        _rewrite_csv(rows)
    return updated


def write_draft(email: str, subject: str, body: str) -> None:
    rows = read_all_leads()
    for r in rows:
        if r.get("Target Email","").strip().lower() == email.strip().lower():
            r["Drafted Email Subject"] = subject
            r["Drafted Email Body"]    = body
            break
    _rewrite_csv(rows)


def mark_sent(email: str, sent_at: str) -> None:
    rows = read_all_leads()
    for r in rows:
        if r.get("Target Email","").strip().lower() == email.strip().lower():
            r["Status"]  = "Sent"
            r["Sent At"] = sent_at
            break
    _rewrite_csv(rows)


def read_pending_with_draft() -> list:
    """
    Return all rows where Status == 'Pending' AND draft body is populated.
    These are the rows Module D will dispatch — no approval gate.
    """
    return [
        r for r in read_all_leads()
        if r.get("Status","").strip() == "Pending"
        and r.get("Drafted Email Body","").strip()
    ]


def read_pending_without_draft() -> list:
    """Return Pending rows that have no email draft yet — Module C's input."""
    return [
        r for r in read_all_leads()
        if r.get("Status","").strip() == "Pending"
        and not r.get("Drafted Email Body","").strip()
    ]


def get_stats() -> dict:
    rows  = read_all_leads()
    total = len(rows)
    return {
        "total":    total,
        "pending":  sum(1 for r in rows if r.get("Status") == "Pending"),
        "sent":     sum(1 for r in rows if r.get("Status") == "Sent"),
        "drafted":  sum(1 for r in rows if r.get("Drafted Email Body","").strip()),
        "no_draft": sum(1 for r in rows if not r.get("Drafted Email Body","").strip()),
    }


# ══════════════════════════════════════════════════════════════════════════
# EXCEL — view-only, regenerated fresh every run
# ══════════════════════════════════════════════════════════════════════════

def export_excel() -> str:
    """
    Generate a formatted Excel file from the full CSV.
    Called at the end of every sourcing / drafting / dispatch run.
    Returns path to the file.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import (PatternFill, Font, Alignment, Border, Side)
        from openpyxl.utils  import get_column_letter
    except ImportError:
        log.warning("openpyxl not installed — skipping Excel export")
        return ""

    rows = read_all_leads()
    wb   = Workbook()
    ws   = wb.active
    ws.title = "Outbound Pipeline"

    # ── Styles ──────────────────────────────────────────────────────────
    HDR_FILL     = PatternFill("solid", fgColor="0F172A")   # slate-900
    HDR_FONT     = Font(color="F8FAFC", bold=True, name="Calibri", size=10)
    PENDING_FILL = PatternFill("solid", fgColor="FEF9C3")   # yellow-100
    SENT_FILL    = PatternFill("solid", fgColor="DCFCE7")   # green-100
    TIER_A_FILL  = PatternFill("solid", fgColor="EDE9FE")   # violet-100
    THIN         = Side(style="thin", color="E2E8F0")
    BORDER       = Border(left=THIN, right=THIN, bottom=THIN, top=THIN)

    COL_W = {
        "Date Sourced": 13, "Week": 10, "Confidence Score": 9, "Title Tier": 7,
        "Target Name": 22, "Company": 26, "Position": 30, "Target Email": 32,
        "LinkedIn URL": 38, "Location": 18, "Geo Segment": 12, "Vertical": 16,
        "Company Type": 12, "Funding Stage": 14, "Is Remote": 9,
        "Outreach Mode": 12, "Company Description": 50,
        "Reason for Outreach": 55, "Drafted Email Subject": 40,
        "Drafted Email Body": 90, "Status": 10, "Sent At": 20,
    }

    # ── Header row ───────────────────────────────────────────────────────
    ws.append(COLUMNS)
    for ci, col_name in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=ci)
        cell.fill      = HDR_FILL
        cell.font      = HDR_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border    = BORDER
        ws.column_dimensions[get_column_letter(ci)].width = COL_W.get(col_name, 18)
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}1"

    # ── Data rows ────────────────────────────────────────────────────────
    status_col = COLUMNS.index("Status") + 1
    tier_col   = COLUMNS.index("Title Tier") + 1

    for row_data in rows:
        row_vals = [row_data.get(c, "") for c in COLUMNS]
        ws.append(row_vals)
        ri = ws.max_row

        status = row_data.get("Status", "Pending")
        tier   = row_data.get("Title Tier", "")

        for ci in range(1, len(COLUMNS) + 1):
            cell = ws.cell(row=ri, column=ci)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            cell.border    = BORDER

        # Colour status column
        status_cell = ws.cell(row=ri, column=status_col)
        status_cell.fill = SENT_FILL if status == "Sent" else PENDING_FILL
        status_cell.font = Font(bold=True, name="Calibri", size=10)

        # Highlight Tier A rows with subtle violet tint on name cell
        if tier == "A":
            ws.cell(row=ri, column=tier_col).fill = TIER_A_FILL

        ws.row_dimensions[ri].height = 70

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    wb.save(PIPELINE_XLS)
    log.info("Excel exported → %s (%d rows)", PIPELINE_XLS, len(rows))
    return str(PIPELINE_XLS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(get_stats())
    export_excel()
