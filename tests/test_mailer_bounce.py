from __future__ import annotations

from datetime import timedelta

from outreach.mailer import Gmail
from outreach.models import Lead, LeadState, Person, SendRecord, utcnow


class _G(Gmail):
    def __init__(self, inbox):  # inbox: list of (from, subject, body)
        self.inbox = inbox

    def _search(self, mailbox, criteria):
        hits = []
        for i, (frm, subj, body) in enumerate(self.inbox):
            if criteria.startswith('(FROM "') and "OR" not in criteria:
                want = criteria.split('"')[1]
                if want in frm:
                    hits.append(str(i).encode())
                continue
            addr = criteria.split('BODY "')[1].split('"')[0]
            sender_ok = "mailer-daemon" in frm or "postmaster" in frm
            if sender_ok and addr in body and "Delay" not in subj:
                hits.append(str(i).encode())
        return hits

    def _fetch_text(self, mailbox, num):
        return self.inbox[int(num)][2]


def _lead():
    return Lead(id="x", state=LeadState.SENT, email="a@tata.com", person=Person(full_name="A B"),
                sends=[SendRecord(step=0, sent_at=utcnow() - timedelta(hours=2))])


def test_exchange_postmaster_ndr_is_bounce():
    g = _G([("postmaster@tata365.onmicrosoft.com", "Undeliverable: hi", "a@tata.com not found")])
    assert g.check_lead(_lead()) == "bounced"


def test_gmail_delay_dsn_is_not_bounce():
    g = _G([("mailer-daemon@googlemail.com", "Delivery Status Notification (Delay)", "a@tata.com delayed")])
    assert g.check_lead(_lead()) == "none"


def test_criteria_covers_postmaster_and_excludes_delay():
    seen = []

    class G(Gmail):
        def __init__(self): pass
        def _search(self, mb, c): seen.append(c); return []

    G().check_lead(_lead())
    assert 'FROM "postmaster"' in seen[-1] and 'NOT SUBJECT "Delay"' in seen[-1]


# ── follow-up rules ─────────────────────────────────────────────────────────
from outreach.mailer import followup_due  # noqa: E402
from outreach.models import EmailDraft, EmailMessage  # noqa: E402
from outreach.settings import load_settings  # noqa: E402


def _sent_lead(days_ago: float):
    msgs = [EmailMessage(step=0, subject="hello", body="b"), EmailMessage(step=1, subject="Re: hello", body="b"),
            EmailMessage(step=2, subject="Re: hello", body="b")]
    return Lead(id="f", state=LeadState.SENT, email="a@acme.com", person=Person(full_name="A B"),
                draft=EmailDraft(messages=msgs),
                sends=[SendRecord(step=0, sent_at=utcnow() - timedelta(days=days_ago), subject="hello")])


def test_first_followup_waits_one_week():
    s = load_settings()
    assert followup_due(_sent_lead(6.5), s, utcnow()) is None
    assert followup_due(_sent_lead(7.1), s, utcnow()) == 1


def test_confirmed_sent_requires_opener_in_sent_folder():
    class G(Gmail):
        def __init__(self, hit): self.hit = hit; self.q = []
        def _search(self, mb, c): self.q.append((mb, c)); return [b"1"] if self.hit else []

    yes, no = G(True), G(False)
    assert yes.confirmed_sent(_sent_lead(8)) is True
    assert no.confirmed_sent(_sent_lead(8)) is False
    assert yes.q[0][0] == "[Gmail]/Sent Mail" and 'SUBJECT "hello"' in yes.q[0][1]
