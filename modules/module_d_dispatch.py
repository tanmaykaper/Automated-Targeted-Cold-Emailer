"""
MODULE D: Timezone-Aware Dispatch Queue
=========================================
Reads Google Sheets for "Approved" rows, maps location to timezone,
dispatches only during optimal local windows (Tue–Thu, 9:30–11:30 AM / 2–4 PM).
Hard cap: 10 emails/day. Uses Gmail OAuth2.

Dependencies:
    pip install gspread google-auth google-auth-oauthlib \
                google-api-python-client pytz geopy
"""

import os
import json
import base64
import logging
import time
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # Python 3.9+

import pytz
import gspread
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
DAILY_SEND_CAP      = 10
DISPATCH_LOG_FILE   = "output/dispatch_log.json"
GMAIL_SCOPES        = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]
SENDER_NAME         = "Tanmay Kaper"
SENDER_EMAIL        = os.environ.get("SENDER_EMAIL", "tanmay.kaper1401@gmail.com")
CREDENTIALS_FILE    = os.environ.get("GMAIL_OAUTH_CREDENTIALS", "gmail_credentials.json")
TOKEN_FILE          = "output/gmail_token.json"

# Optimal send windows (local time)
MORNING_WINDOW = (9, 30, 11, 30)   # 9:30 AM – 11:30 AM
AFTERNOON_WINDOW = (14, 0, 16, 0)  # 2:00 PM – 4:00 PM
TARGET_WEEKDAYS = {1, 2, 3}        # Mon=0, Tue=1, Wed=2, Thu=3


# ──────────────────────────────────────────────
# LOCATION → TIMEZONE MAPPING
# ──────────────────────────────────────────────
LOCATION_TZ_MAP: dict[str, str] = {
    # India
    "india": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata",
    "delhi": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata",
    "hyderabad": "Asia/Kolkata",
    "chennai": "Asia/Kolkata",
    "pune": "Asia/Kolkata",
    # US
    "new york": "America/New_York",
    "boston": "America/New_York",
    "washington": "America/New_York",
    "chicago": "America/Chicago",
    "dallas": "America/Chicago",
    "houston": "America/Chicago",
    "denver": "America/Denver",
    "san francisco": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles",
    "seattle": "America/Los_Angeles",
    "california": "America/Los_Angeles",
    "united states": "America/New_York",
    # UK
    "london": "Europe/London",
    "manchester": "Europe/London",
    "edinburgh": "Europe/London",
    "united kingdom": "Europe/London",
    "uk": "Europe/London",
    # Europe
    "paris": "Europe/Paris",
    "berlin": "Europe/Berlin",
    "amsterdam": "Europe/Amsterdam",
    "zurich": "Europe/Zurich",
    "frankfurt": "Europe/Berlin",
    # Asia
    "singapore": "Asia/Singapore",
    "hong kong": "Asia/Hong_Kong",
    "tokyo": "Asia/Tokyo",
    "dubai": "Asia/Dubai",
    "abu dhabi": "Asia/Dubai",
    # Australia
    "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne",
    "brisbane": "Australia/Brisbane",
    "australia": "Australia/Sydney",
}

def location_to_timezone(location: str) -> pytz.BaseTzInfo:
    """
    Maps a free-text location string to a pytz timezone.
    Falls back to UTC with a warning if no match found.
    """
    if not location:
        log.warning("Empty location; defaulting to UTC.")
        return pytz.utc

    loc_lower = location.lower()
    for keyword, tz_name in LOCATION_TZ_MAP.items():
        if keyword in loc_lower:
            try:
                return pytz.timezone(tz_name)
            except pytz.exceptions.UnknownTimeZoneError:
                log.warning(f"Unknown timezone: {tz_name}. Defaulting to UTC.")
                return pytz.utc

    log.warning(f"No timezone match for '{location}'. Defaulting to UTC.")
    return pytz.utc


def is_optimal_send_time(tz: pytz.BaseTzInfo) -> bool:
    """
    Returns True if NOW is within the optimal send window in the target's timezone.
    Optimal: Tuesday–Thursday, 9:30–11:30 AM or 2:00–4:00 PM.
    """
    now_local = datetime.now(tz)
    weekday = now_local.weekday()  # Mon=0 … Sun=6

    if weekday not in TARGET_WEEKDAYS:
        return False

    h, m = now_local.hour, now_local.minute
    current_minutes = h * 60 + m

    morning_start  = MORNING_WINDOW[0] * 60 + MORNING_WINDOW[1]
    morning_end    = MORNING_WINDOW[2] * 60 + MORNING_WINDOW[3]
    afternoon_start = AFTERNOON_WINDOW[0] * 60 + AFTERNOON_WINDOW[1]
    afternoon_end   = AFTERNOON_WINDOW[2] * 60 + AFTERNOON_WINDOW[3]

    in_morning   = morning_start <= current_minutes <= morning_end
    in_afternoon = afternoon_start <= current_minutes <= afternoon_end

    return in_morning or in_afternoon


# ──────────────────────────────────────────────
# DISPATCH LOG (track daily send count)
# ──────────────────────────────────────────────
def load_dispatch_log() -> dict:
    try:
        with open(DISPATCH_LOG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_dispatch_log(log_data: dict):
    os.makedirs(os.path.dirname(DISPATCH_LOG_FILE), exist_ok=True)
    with open(DISPATCH_LOG_FILE, "w") as f:
        json.dump(log_data, f, indent=2)

def get_today_sent_count() -> int:
    log_data = load_dispatch_log()
    today_str = date.today().isoformat()
    return log_data.get(today_str, {}).get("count", 0)

def record_sent(email: str, target_name: str):
    log_data = load_dispatch_log()
    today_str = date.today().isoformat()
    if today_str not in log_data:
        log_data[today_str] = {"count": 0, "sent": []}
    log_data[today_str]["count"] += 1
    log_data[today_str]["sent"].append({"email": email, "name": target_name, "time": datetime.now().isoformat()})
    save_dispatch_log(log_data)


# ──────────────────────────────────────────────
# GMAIL OAUTH2 CLIENT
# ──────────────────────────────────────────────
def get_gmail_service():
    """Authenticates with Gmail via OAuth2. Opens browser on first run."""
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ──────────────────────────────────────────────
# GMAIL SEND
# ──────────────────────────────────────────────
def send_email(
    gmail_service,
    to_email: str,
    subject: str,
    body: str,
    target_name: str,
) -> bool:
    """Sends a plain-text email via Gmail API. Returns True on success."""
    try:
        message = MIMEMultipart("alternative")
        message["to"]      = to_email
        message["from"]    = f"{SENDER_NAME} <{SENDER_EMAIL}>"
        message["subject"] = subject
        message.attach(MIMEText(body, "plain"))

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        result = gmail_service.users().messages().send(
            userId="me",
            body={"raw": raw}
        ).execute()

        log.info(f"Email sent to {target_name} <{to_email}>. Message ID: {result.get('id')}")
        return True

    except Exception as e:
        log.error(f"Failed to send email to {to_email}: {e}")
        return False


# ──────────────────────────────────────────────
# GOOGLE SHEETS READER
# ──────────────────────────────────────────────
def get_approved_rows(spreadsheet_id: str, worksheet_name: str = "Outbound Pipeline") -> list[dict]:
    """
    Returns rows that are cleared for dispatch:
      - Approval Status == 'Approved'  AND  Email Verified == TRUE
        (lookup API confirmed the address — safe to auto-send)
      OR
      - Approval Status == 'Approved'  AND  human has changed status from
        'Manual-Verify' to 'Approved' (human explicitly confirmed the
        pattern-inferred address is real — also safe to send)

    Rows still sitting at 'Manual-Verify' are excluded entirely — those
    need a human to verify the email address in the sheet first, then
    flip the status to Approved.
    """
    from modules.module_b_spreadsheet import get_gspread_client, COLUMNS

    gc = get_gspread_client()
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.worksheet(worksheet_name)

    all_records = ws.get_all_records()

    approved = []
    skipped_unverified = 0

    for r in all_records:
        status   = r.get("Approval Status", "").strip().lower()
        verified = str(r.get("Email Verified", "")).strip().upper()
        sent     = r.get("Sent At", "")

        if sent:
            continue  # already dispatched
        if status != "approved":
            continue  # pending / rejected / manual-verify

        # At this point the human has set status=Approved. Still check
        # email_verified — if FALSE, the human may have approved content
        # but not validated the address. We block and log it conspicuously.
        if verified not in ("TRUE", "YES", "1"):
            log.warning(
                "DISPATCH BLOCKED — %s @ %s: status=Approved but Email Verified=FALSE. "
                "Confirm the address is real in the sheet then re-save.",
                r.get("Target Name","?"), r.get("Company","?"),
            )
            skipped_unverified += 1
            continue

        approved.append(r)

    log.info(
        "Dispatch queue: %d approved+verified rows ready | %d blocked (unverified email)",
        len(approved), skipped_unverified,
    )
    return approved


def mark_as_sent(
    spreadsheet_id: str,
    row_index: int,   # 1-based row in sheet (header=1, first data=2)
    worksheet_name: str = "Outbound Pipeline",
):
    """Updates Approval Status to 'Sent' for a given row."""
    from modules.module_b_spreadsheet import get_gspread_client, COLUMNS

    gc = get_gspread_client()
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.worksheet(worksheet_name)

    approval_col = COLUMNS.index("Approval Status") + 1  # 1-indexed
    ws.update_cell(row_index, approval_col, f"Sent — {datetime.now().strftime('%Y-%m-%d %H:%M')}")


# ──────────────────────────────────────────────
# MAIN DISPATCH RUNNER
# ──────────────────────────────────────────────
def run_dispatch(spreadsheet_id: str):
    """
    Daily dispatch runner. Call this via cron or n8n scheduler.
    Processes approved rows, checks timezone windows, dispatches up to 10/day.
    """
    today_sent = get_today_sent_count()
    if today_sent >= DAILY_SEND_CAP:
        log.info(f"Daily cap reached ({DAILY_SEND_CAP} emails). No more sends today.")
        return

    remaining_cap = DAILY_SEND_CAP - today_sent
    log.info(f"Dispatch run started. Remaining quota today: {remaining_cap}")

    approved_rows = get_approved_rows(spreadsheet_id)
    if not approved_rows:
        log.info("No approved rows found. Exiting.")
        return

    gmail = get_gmail_service()
    sent_this_run = 0

    for i, row in enumerate(approved_rows):
        if sent_this_run >= remaining_cap:
            log.info(f"Daily cap hit during this run after {sent_this_run} emails.")
            break

        target_email  = row.get("Target Email", "").strip()
        target_name   = row.get("Target Name", "Unknown")
        location      = row.get("Location", "")
        subject       = row.get("Email Subject", f"Quick note — Tanmay Kaper")
        body          = row.get("Drafted Email Body", "")
        sheet_row_idx = i + 2  # +2 because row 1 is header, records are 0-indexed

        if not target_email or not body:
            log.warning(f"Skipping row {sheet_row_idx}: missing email or body.")
            continue

        # Timezone window check
        tz = location_to_timezone(location)
        if not is_optimal_send_time(tz):
            now_local = datetime.now(tz)
            log.info(
                f"Skipping {target_name} ({location}): not in optimal window. "
                f"Their local time: {now_local.strftime('%A %H:%M %Z')}"
            )
            continue

        # Send
        success = send_email(gmail, target_email, subject, body, target_name)
        if success:
            record_sent(target_email, target_name)
            mark_as_sent(spreadsheet_id, sheet_row_idx)
            sent_this_run += 1
            time.sleep(2)  # Brief pause between sends

    log.info(f"Dispatch run complete. Sent {sent_this_run} emails this run. "
             f"Total today: {today_sent + sent_this_run}/{DAILY_SEND_CAP}.")


if __name__ == "__main__":
    SPREADSHEET_ID = os.environ["GOOGLE_SHEET_ID"]
    run_dispatch(SPREADSHEET_ID)
