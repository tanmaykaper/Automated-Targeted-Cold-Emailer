"""
MODULE D: Timezone-Aware Dispatch Queue — 15-20 emails/week
════════════════════════════════════════════════════════════
Transport:   Gmail SMTP + App Password (free, no OAuth/Cloud billing)
Reads from:  CSV rows where Status == "Pending" AND draft body is populated
No approval gate — bot dispatches automatically after drafting.
Cadence:     4 runs/week (Mon/Tue/Wed/Thu) × 5 emails/run = up to 20/week
Weekly cap:  tracked in state/weekly_count.json, resets each ISO week
"""

import os, json, smtplib, logging, time
from datetime  import datetime, timezone as dt_tz
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from pathlib   import Path

import pytz
from dotenv import load_dotenv
load_dotenv()

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from module_b_spreadsheet import (
    read_pending_with_draft, mark_sent, get_stats, export_excel, REPO_ROOT
)

log = logging.getLogger(__name__)

SENDER_EMAIL       = os.getenv("SENDER_EMAIL",        "tanmay.kaper1401@gmail.com")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD",  "")
MAX_PER_RUN        = int(os.getenv("MAX_PER_RUN",     "5"))
WEEKLY_CAP         = int(os.getenv("WEEKLY_CAP",      "20"))

STATE_DIR         = REPO_ROOT / "state"
WEEKLY_COUNT_FILE = STATE_DIR / "weekly_count.json"
DISPATCH_LOG      = STATE_DIR / "dispatch_log.jsonl"


# ══════════════════════════════════════════════════════════════════════════
# TIMEZONE
# ══════════════════════════════════════════════════════════════════════════

_TZ_MAP = {
    "india":"Asia/Kolkata","mumbai":"Asia/Kolkata","delhi":"Asia/Kolkata",
    "bangalore":"Asia/Kolkata","bengaluru":"Asia/Kolkata","hyderabad":"Asia/Kolkata",
    "pune":"Asia/Kolkata","chennai":"Asia/Kolkata","ahmedabad":"Asia/Kolkata",
    "new york":"America/New_York","boston":"America/New_York","washington":"America/New_York",
    "chicago":"America/Chicago","dallas":"America/Chicago",
    "los angeles":"America/Los_Angeles","san francisco":"America/Los_Angeles","seattle":"America/Los_Angeles",
    "usa":"America/New_York","united states":"America/New_York",
    "london":"Europe/London","uk":"Europe/London","manchester":"Europe/London","edinburgh":"Europe/London",
    "amsterdam":"Europe/Amsterdam","berlin":"Europe/Berlin","zurich":"Europe/Zurich","paris":"Europe/Paris",
    "dubai":"Asia/Dubai","abu dhabi":"Asia/Dubai","uae":"Asia/Dubai",
    "singapore":"Asia/Singapore",
    "sydney":"Australia/Sydney","melbourne":"Australia/Sydney","australia":"Australia/Sydney",
    "tokyo":"Asia/Tokyo","japan":"Asia/Tokyo",
    "hong kong":"Asia/Hong_Kong",
}

def _resolve_tz(location: str, geo_segment: str = "") -> pytz.BaseTzInfo:
    combined = (location + " " + geo_segment).lower()
    for key, tz in _TZ_MAP.items():
        if key in combined:
            return pytz.timezone(tz)
    # Try pytz directly (handles "Asia/Kolkata" style strings in location field)
    try:
        return pytz.timezone(location.strip())
    except Exception:
        pass
    return pytz.timezone("Asia/Kolkata")   # Default: IST (Tanmay's base)


# Optimal send windows: Tue–Thu 9:30–11:30 AM and 2:00–4:00 PM local
OPTIMAL_DAYS    = {1, 2, 3}   # Mon=0 … Thu=3; we run Mon–Thu
OPTIMAL_WINDOWS = [(9,30,11,30),(14,0,16,0)]

def _in_window(tz: pytz.BaseTzInfo) -> bool:
    now = datetime.now(tz)
    if now.weekday() not in OPTIMAL_DAYS:
        return False
    cm = now.hour*60 + now.minute
    return any(sh*60+sm <= cm < eh*60+em for sh,sm,eh,em in OPTIMAL_WINDOWS)


# ══════════════════════════════════════════════════════════════════════════
# WEEKLY CAP
# ══════════════════════════════════════════════════════════════════════════

def _week_key() -> str:
    n = datetime.now(dt_tz.utc)
    return f"{n.year}-W{n.isocalendar()[1]:02d}"

def _week_sent() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not WEEKLY_COUNT_FILE.exists():
        return 0
    try:
        return json.loads(WEEKLY_COUNT_FILE.read_text()).get(_week_key(), 0)
    except Exception:
        return 0

def _bump_week(n: int = 1) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {}
    if WEEKLY_COUNT_FILE.exists():
        try:
            data = json.loads(WEEKLY_COUNT_FILE.read_text())
        except Exception:
            pass
    wk        = _week_key()
    data[wk]  = data.get(wk, 0) + n
    WEEKLY_COUNT_FILE.write_text(json.dumps(data, indent=2))
    return data[wk]


# ══════════════════════════════════════════════════════════════════════════
# GMAIL SMTP
# ══════════════════════════════════════════════════════════════════════════

def _send(to: str, subject: str, body: str) -> bool:
    if not GMAIL_APP_PASSWORD:
        log.error("GMAIL_APP_PASSWORD not set")
        return False

    msg              = MIMEMultipart("alternative")
    msg["From"]      = SENDER_EMAIL
    msg["To"]        = to
    msg["Subject"]   = subject
    msg["Reply-To"]  = SENDER_EMAIL
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(SENDER_EMAIL, GMAIL_APP_PASSWORD)
            s.sendmail(SENDER_EMAIL, to, msg.as_string())
        log.info("✉  → %s | %s", to, subject)
        return True
    except smtplib.SMTPAuthenticationError:
        log.error("Gmail auth failed — check SENDER_EMAIL and GMAIL_APP_PASSWORD")
        return False
    except smtplib.SMTPRecipientsRefused:
        log.error("Recipient refused: %s", to)
        return False
    except Exception as e:
        log.error("SMTP error to %s: %s", to, e)
        return False


def _log(lead: dict, ts: str, success: bool) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rec = {
        "week": _week_key(), "sent_at": ts, "success": success,
        "name": lead.get("Target Name"), "company": lead.get("Company"),
        "email": lead.get("Target Email"), "subject": lead.get("Drafted Email Subject",""),
        "tier": lead.get("Title Tier",""), "mode": lead.get("Outreach Mode",""),
    }
    with open(DISPATCH_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


# ══════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════

def run_dispatch_queue(dry_run: bool = False, force_window: bool = False) -> dict:
    """
    Dispatch emails for all Pending leads with a draft.
    No approval gate — runs fully automatically.
    Respects weekly cap and timezone optimal windows.
    """
    week_sent  = _week_sent()
    remaining  = WEEKLY_CAP - week_sent

    log.info("Week %s: %d/%d sent | cap remaining: %d", _week_key(), week_sent, WEEKLY_CAP, remaining)

    if remaining <= 0:
        log.info("Weekly cap reached")
        return {"sent":0,"skipped":0,"deferred":0,"cap_reached":True,
                "week":_week_key(),"week_sent":week_sent}

    candidates = read_pending_with_draft()
    log.info("%d pending-with-draft leads available", len(candidates))

    if not candidates:
        return {"sent":0,"skipped":0,"deferred":0,"no_candidates":True}

    sent_count = skipped = deferred = 0
    run_limit  = min(MAX_PER_RUN, remaining)

    for lead in candidates:
        if sent_count >= run_limit:
            deferred += 1
            continue

        email   = lead.get("Target Email","").strip()
        subject = lead.get("Drafted Email Subject","").strip()
        body    = lead.get("Drafted Email Body","").strip()

        if not email or not body:
            skipped += 1
            continue

        subject = subject or f"Quick note — {lead.get('Company','your team')}"

        # Timezone window check
        tz = _resolve_tz(lead.get("Location",""), lead.get("Geo Segment",""))
        if not force_window and not _in_window(tz):
            now_local = datetime.now(tz).strftime("%a %H:%M %Z")
            log.info("⏰ Defer %s — not in window (%s)", lead.get("Target Name"), now_local)
            deferred += 1
            continue

        ts = datetime.now(dt_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if dry_run:
            log.info("[DRY RUN] → %s | %s", email, subject)
            success = True
        else:
            success = _send(email, subject, body)

        _log(lead, ts, success)

        if success:
            mark_sent(email, ts)
            sent_count += 1
            _bump_week(1)
            time.sleep(4)   # Space sends — avoid Gmail throttle
        else:
            skipped += 1

    # Regenerate Excel after every dispatch run so it reflects latest Sent status
    export_excel()

    summary = {
        "sent": sent_count, "skipped": skipped, "deferred": deferred,
        "week": _week_key(), "week_total": week_sent + sent_count,
        "week_remaining": WEEKLY_CAP - week_sent - sent_count,
        "dry_run": dry_run,
    }
    log.info("Dispatch summary: %s", json.dumps(summary))
    return summary


if __name__ == "__main__":
    import argparse, sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run",      action="store_true")
    p.add_argument("--force-window", action="store_true")
    a = p.parse_args()
    print(json.dumps(run_dispatch_queue(dry_run=a.dry_run, force_window=a.force_window), indent=2))
