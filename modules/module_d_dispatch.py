"""
MODULE D: Timezone-Aware Dispatch Queue (Gmail SMTP + App Password)
═════════════════════════════════════════════════════════════════════
Reads Pending+drafted rows from state/pipeline.csv (via module_b), sends
through Gmail SMTP using an App Password (README setup step — no OAuth
client, no Google Sheets), respects per-run and per-week send caps, and
only sends during each recipient's local "reasonable business hours"
window.

NOTE (previous version of this file): an earlier revision expected Gmail
OAuth2 (gmail_credentials.json) and a Google Sheet, and exposed
run_dispatch(spreadsheet_id) instead of the run_dispatch_queue(dry_run,
force_window) orchestrator.py actually imports. That mismatch meant every
`dispatch` action failed at import time. This rewrite matches the SMTP +
App Password design the README/config/workflow already assume.
"""

import os
import json
import smtplib
import logging
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timezone
from pathlib import Path

from zoneinfo import ZoneInfo

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from config import WEEKLY_CAP, MAX_PER_RUN
import module_b_spreadsheet as store

log = logging.getLogger(__name__)

STATE_DIR         = Path(__file__).resolve().parent.parent / "state"
DISPATCH_LOG_PATH = STATE_DIR / "dispatch_log.jsonl"
WEEKLY_COUNT_PATH = STATE_DIR / "weekly_count.json"

SENDER_NAME  = "Tanmay Kaper"
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

RESUME_PATH = Path(__file__).resolve().parent.parent / "resume.pdf"

# Optional safety gate: skip pattern-guessed (unverified) addresses unless
# explicitly told to send them anyway. Off by default so this doesn't
# silently change existing send behaviour — flip on once the free-tier
# email-finder keys (Hunter/Prospeo/AnyMailFinder/Snov/Apollo) are wired
# up and most rows are actually landing as email_verified=TRUE.
REQUIRE_VERIFIED_EMAIL = os.getenv("REQUIRE_VERIFIED_EMAIL", "false").lower() == "true"

# Optimal local send windows (Tue–Thu, per README)
MORNING_WINDOW   = (9, 30, 11, 30)
AFTERNOON_WINDOW = (14, 0, 16, 0)
TARGET_WEEKDAYS  = {1, 2, 3}   # Mon=0 ... Tue=1, Wed=2, Thu=3

LOCATION_TZ_MAP: dict[str, str] = {
    "india": "Asia/Kolkata", "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata", "bengaluru": "Asia/Kolkata", "hyderabad": "Asia/Kolkata",
    "chennai": "Asia/Kolkata", "pune": "Asia/Kolkata", "ahmedabad": "Asia/Kolkata",
    "new york": "America/New_York", "boston": "America/New_York", "washington": "America/New_York",
    "chicago": "America/Chicago", "san francisco": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles", "seattle": "America/Los_Angeles",
    "california": "America/Los_Angeles", "united states": "America/New_York",
    "london": "Europe/London", "manchester": "Europe/London", "united kingdom": "Europe/London", "uk": "Europe/London",
    "paris": "Europe/Paris", "berlin": "Europe/Berlin", "amsterdam": "Europe/Amsterdam", "zurich": "Europe/Zurich",
    "singapore": "Asia/Singapore", "hong kong": "Asia/Hong_Kong", "tokyo": "Asia/Tokyo",
    "dubai": "Asia/Dubai", "abu dhabi": "Asia/Dubai",
    "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne", "australia": "Australia/Sydney",
}


def location_to_timezone(location: str):
    if not location:
        return ZoneInfo("UTC")
    loc_lower = location.lower()
    for keyword, tz_name in LOCATION_TZ_MAP.items():
        if keyword in loc_lower:
            try:
                return ZoneInfo(tz_name)
            except Exception:
                return ZoneInfo("UTC")
    return ZoneInfo("UTC")


def is_optimal_send_time(tz) -> bool:
    now_local = datetime.now(tz)
    if now_local.weekday() not in TARGET_WEEKDAYS:
        return False
    minutes = now_local.hour * 60 + now_local.minute
    m_start = MORNING_WINDOW[0] * 60 + MORNING_WINDOW[1]
    m_end   = MORNING_WINDOW[2] * 60 + MORNING_WINDOW[3]
    a_start = AFTERNOON_WINDOW[0] * 60 + AFTERNOON_WINDOW[1]
    a_end   = AFTERNOON_WINDOW[2] * 60 + AFTERNOON_WINDOW[3]
    return (m_start <= minutes <= m_end) or (a_start <= minutes <= a_end)


# ──────────────────────────────────────────────
# WEEKLY CAP TRACKING (state/weekly_count.json)
# ──────────────────────────────────────────────
def _iso_week() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"


def _load_weekly_counts() -> dict:
    if not WEEKLY_COUNT_PATH.exists():
        return {}
    try:
        return json.loads(WEEKLY_COUNT_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_weekly_counts(counts: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    WEEKLY_COUNT_PATH.write_text(json.dumps(counts, indent=2))


def _week_total(counts: dict) -> int:
    return counts.get(_iso_week(), 0)


def _record_week_send(counts: dict) -> dict:
    wk = _iso_week()
    counts[wk] = counts.get(wk, 0) + 1
    return counts


# ──────────────────────────────────────────────
# DISPATCH LOG (state/dispatch_log.jsonl)
# ──────────────────────────────────────────────
def _append_dispatch_log(entry: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(DISPATCH_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ──────────────────────────────────────────────
# GMAIL SMTP SEND
# ──────────────────────────────────────────────
def _send_via_smtp(to_email: str, subject: str, body: str) -> bool:
    if not SENDER_EMAIL or not GMAIL_APP_PASSWORD:
        log.error("SENDER_EMAIL / GMAIL_APP_PASSWORD not set — cannot send.")
        return False

    if not RESUME_PATH.exists():
        log.error("resume.pdf not found at %s — refusing to send without it. "
                  "Add resume.pdf to the repo root.", RESUME_PATH)
        return False

    try:
        # NOTE: previous version used MIMEMultipart("alternative"), which is
        # for text/html alternatives of the SAME content — it has no concept
        # of attachments, so nothing ever got attached even if code to
        # attach a file were added underneath it. "mixed" is required to
        # combine a body with a separate file attachment.
        msg = MIMEMultipart("mixed")
        msg["From"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with open(RESUME_PATH, "rb") as f:
            part = MIMEBase("application", "pdf")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{RESUME_PATH.name}"')
        msg.attach(part)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
            server.login(SENDER_EMAIL, GMAIL_APP_PASSWORD)
            server.sendmail(SENDER_EMAIL, [to_email], msg.as_string())
        return True
    except Exception as e:
        log.error("SMTP send failed for %s: %s", to_email, e)
        return False


# ──────────────────────────────────────────────
# MAIN DISPATCH RUNNER
# ──────────────────────────────────────────────
def run_dispatch_queue(dry_run: bool = False, force_window: bool = False) -> dict:
    counts = _load_weekly_counts()
    week_total = _week_total(counts)
    week_remaining = max(0, WEEKLY_CAP - week_total)

    if week_remaining <= 0:
        log.info("Weekly cap (%d) already reached — nothing to send this run.", WEEKLY_CAP)
        return {"sent": 0, "deferred": 0, "skipped": 0,
                "week_total": week_total, "week_remaining": 0}

    queue = store.read_pending_with_draft()
    log.info("%d Pending+drafted rows in queue", len(queue))

    sent = deferred = skipped = 0
    run_cap = min(MAX_PER_RUN, week_remaining)

    for row in queue:
        if sent >= run_cap:
            break

        to_email = row["Target Email"].strip()
        name     = row["Target Name"] or "there"
        company  = row["Company"]
        subject  = row["Drafted Email Subject"].strip()
        body     = row["Drafted Email Body"].strip()
        location = row["Location"]
        verified = row["Email Verified"].upper() == "TRUE"

        if not to_email or not subject or not body:
            log.warning("Skipping %s — missing email/subject/body", name)
            skipped += 1
            continue

        if REQUIRE_VERIFIED_EMAIL and not verified:
            log.info("Skipping %s <%s> — email unverified and REQUIRE_VERIFIED_EMAIL=true", name, to_email)
            skipped += 1
            continue

        if not force_window:
            tz = location_to_timezone(location)
            if not is_optimal_send_time(tz):
                deferred += 1
                continue

        if dry_run:
            log.info("[DRY RUN] Would send to %s <%s> — subject: %s", name, to_email, subject)
            sent += 1
            continue

        ok = _send_via_smtp(to_email, subject, body)
        sent_at = datetime.now(timezone.utc).isoformat()

        _append_dispatch_log({
            "week": _iso_week(), "sent_at": sent_at, "success": ok,
            "name": name, "company": company, "email": to_email,
            "subject": subject, "tier": row["Title Tier"],
            "mode": row["Outreach Mode"], "email_verified": verified,
        })

        if ok:
            store.mark_sent(to_email, sent_at)
            counts = _record_week_send(counts)
            sent += 1
            time.sleep(2)
        else:
            skipped += 1

    if not dry_run:
        _save_weekly_counts(counts)

    week_total = _week_total(counts)
    log.info("Dispatch complete: sent=%d deferred=%d skipped=%d | week %d/%d",
              sent, deferred, skipped, week_total, WEEKLY_CAP)

    return {
        "sent": sent, "deferred": deferred, "skipped": skipped,
        "week_total": week_total, "week_remaining": max(0, WEEKLY_CAP - week_total),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(json.dumps(run_dispatch_queue(dry_run=True), indent=2))
