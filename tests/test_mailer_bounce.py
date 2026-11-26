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
