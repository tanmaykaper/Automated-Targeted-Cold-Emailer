"""Sending (Gmail SMTP) + reply/bounce/opt-out detection (Gmail IMAP), same App Password.

Safety rails:
  * DRY_RUN defaults to true; nothing leaves the machine unless DRY_RUN=false.
  * Only APPROVED leads with a sendable verification (VERIFIED, or CATCH_ALL ≥ threshold).
  * Recipient-local send windows (Mon–Thu 09:30–11:30, 14:00–16:00), daily/weekly/per-tick caps,
    randomised human-like gaps between sends.
  * Suppression list (opt-outs, bounces, manual) is checked on every send.
  * Before a live send, Gmail's Sent folder is checked for an earlier message to the recipient,
    so a crashed run can never double-send.
  * Follow-ups are only sent when IMAP confirms there's no reply or bounce.
"""
from __future__ import annotations

import email.utils
import imaplib
import logging
import random
import re
import smtplib
import time
from datetime import datetime, timedelta
from email.message import EmailMessage as MimeMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from .icp import lead_timezone
from .models import Lead, LeadState, SendRecord, utcnow
from .settings import Settings
from .verify import sendable

log = logging.getLogger(__name__)

OPT_OUT_RX = re.compile(r"\b(unsubscribe|remove me|stop emailing|not interested|do not contact|don't contact|take me off)\b", re.I)


class Suppression:
    def __init__(self, path: Path):
        self.path = path
        self.entries: set[str] = set()
        if path.exists():
            self.entries = {ln.strip().lower() for ln in path.read_text(encoding="utf-8").splitlines()
                            if ln.strip() and not ln.startswith("#")}

    def blocked(self, email_addr: str) -> bool:
        e = email_addr.lower()
        return e in self.entries or ("@" + e.split("@", 1)[-1]) in self.entries

    def add(self, email_addr: str, reason: str) -> None:
        e = email_addr.lower()
        if e in self.entries:
            return
        self.entries.add(e)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"{e}\n")
        log.info("suppressed %s (%s)", e, reason)


def in_send_window(lead: Lead, settings: Settings, now: datetime | None = None) -> bool:
    tz = ZoneInfo(lead_timezone(lead))
    local = (now or utcnow()).astimezone(tz)
    if local.weekday() not in settings.send_weekdays:
        return False
    minutes = local.hour * 60 + local.minute
    return any(a <= minutes <= b for a, b in settings.send_windows)


def week_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=7)


def day_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


class Gmail:
    def __init__(self, settings: Settings):
        self.s = settings
        self.imap: imaplib.IMAP4_SSL | None = None
        self.imap_error = ""

    # ── IMAP ────────────────────────────────────────────────────────────────
    def _imap(self) -> imaplib.IMAP4_SSL | None:
        if self.imap or self.imap_error:
            return self.imap
        if not self.s.can_send_mail:
            self.imap_error = "no SENDER_EMAIL / GMAIL_APP_PASSWORD"
            return None
        try:
            m = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=20)
            m.login(self.s.keys.sender_email, self.s.keys.gmail_app_password)
            self.imap = m
        except (imaplib.IMAP4.error, OSError) as e:
            self.imap_error = f"IMAP unavailable: {type(e).__name__}"
            log.warning("%s — reply detection and follow-ups disabled this tick", self.imap_error)
        return self.imap

    def _search(self, mailbox: str, criteria: str) -> list[bytes]:
        m = self._imap()
        if not m:
            raise RuntimeError(self.imap_error)
        typ, _ = m.select(f'"{mailbox}"', readonly=True)
        if typ != "OK":
            return []
        typ, data = m.search(None, criteria)
        return data[0].split() if typ == "OK" and data and data[0] else []

    def _fetch_text(self, mailbox: str, num: bytes) -> str:
        m = self._imap()
        m.select(f'"{mailbox}"', readonly=True)
        typ, data = m.fetch(num, "(BODY.PEEK[TEXT]<0.4000>)")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return ""
        return data[0][1].decode("utf-8", errors="replace")

    def check_lead(self, lead: Lead) -> str:
        """'replied' | 'opted_out' | 'bounced' | 'none'. Raises RuntimeError if IMAP is unavailable."""
        last = lead.last_send
        if not last:
            return "none"
        since = (last.sent_at - timedelta(days=1)).strftime("%d-%b-%Y")
        addr = lead.email
        replies = self._search("INBOX", f'(FROM "{addr}" SINCE {since})')
        if replies:
            body = self._fetch_text("INBOX", replies[-1])
            return "opted_out" if OPT_OUT_RX.search(body) else "replied"
        # Gmail DSNs come from mailer-daemon; Microsoft 365 / Exchange NDRs from postmaster@<tenant>.
        # "Delay" DSNs are transient warnings, not bounces.
        bounces = self._search(
            "INBOX",
            f'(OR FROM "mailer-daemon" FROM "postmaster" SINCE {since} BODY "{addr}" NOT SUBJECT "Delay")',
        )
        return "bounced" if bounces else "none"

    def already_sent(self, to: str, subject: str, step: int, since: datetime) -> bool:
        """True if Gmail's Sent folder already holds this step (crash-recovery idempotency check)."""
        subj = subject.replace('"', "").replace("\\", "")[:80]
        try:
            hits = self._search("[Gmail]/Sent Mail",
                                f'(TO "{to}" SUBJECT "{subj}" SINCE {since.strftime("%d-%b-%Y")})')
        except RuntimeError:
            return False
        # step 0: any hit means sent. step n: n-1 earlier "Re:" messages are expected.
        return len(hits) >= max(step, 1)

    # ── SMTP ────────────────────────────────────────────────────────────────
    def send(self, lead: Lead, step: int) -> SendRecord:
        msg_spec = lead.draft.step(step)
        mime = MimeMessage()
        mime["From"] = email.utils.formataddr(("Tanmay Kaper", self.s.keys.sender_email))
        mime["To"] = lead.email
        mime["Subject"] = msg_spec.subject
        mime["Date"] = email.utils.formatdate(localtime=False)
        mid = email.utils.make_msgid(domain=self.s.keys.sender_email.split("@")[-1])
        mime["Message-ID"] = mid
        if step > 0:
            prev = next((s for s in lead.sends if s.message_id), None)
            if prev:
                mime["In-Reply-To"] = prev.message_id
                mime["References"] = prev.message_id
        mime.set_content(msg_spec.body, subtype="plain", charset="utf-8")
        if step == 0 and self.s.attach_resume and self.s.resume_path.exists():
            mime.add_attachment(self.s.resume_path.read_bytes(), maintype="application", subtype="pdf",
                                filename="Tanmay_Kaper_CV.pdf")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(self.s.keys.sender_email, self.s.keys.gmail_app_password)
            server.send_message(mime)
        return SendRecord(step=step, sent_at=utcnow(), message_id=mid, subject=msg_spec.subject, to=lead.email)

    def close(self):
        if self.imap:
            try:
                self.imap.logout()
            except Exception:
                pass


def followup_due(lead: Lead, settings: Settings, now: datetime) -> int | None:
    """Next follow-up step number if one is due, else None."""
    if lead.state != LeadState.SENT or lead.legacy.get("no_followups") or not lead.draft:
        return None
    steps = sorted(s.step for s in lead.sends)
    if not steps:
        return None
    nxt = steps[-1] + 1
    if nxt > settings.max_followups or not lead.draft.step(nxt):
        return None
    gap = settings.followup_gaps_days[min(nxt - 1, len(settings.followup_gaps_days) - 1)]
    return nxt if now - lead.last_send.sent_at >= timedelta(days=gap) else None


def human_delay(settings: Settings, sleep=time.sleep) -> None:
    sleep(random.uniform(settings.min_send_delay_s, settings.max_send_delay_s))


__all__ = ["Gmail", "Suppression", "in_send_window", "followup_due", "week_bounds", "day_bounds",
           "human_delay", "sendable"]
