from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakeSearch, apollo_csv
from fakes import FakeLLM, pick

from outreach import pipeline as P
from outreach.http import Http, ProviderError
from outreach.ingest.csv_import import ingest_csv_text
from outreach.mailer import followup_due, in_send_window
from outreach.models import (EmailDraft, EmailMessage, Lead, LeadState, Person, SendRecord, utcnow)
from outreach.store import Store

GOOD = ("Madhur,Singhal,Managing Partner and CEO,Praxis Global Alliance,madhur.singhal@praxisga.com,Verified,Not Catch-all,,"
        "http://www.linkedin.com/in/madhursinghal,https://praxisga.com,Mumbai,,India,management consulting,consulting,1")
GUESS = ("Rohan,Mehta,Head of Strategy,Nonexistent Ventures,rohan.mehta@nonexistent-ventures.in,Extrapolated,,,"
         ",,Pune,,India,venture capital,,2")
ROLE = "Info,Desk,Director,Acme Analytics,info@acme-analytics.com,Verified,,,,https://acme-analytics.com,Mumbai,,India,,,3"
HR = ("Priya,Nair,Talent Acquisition Manager,Praxis Global Alliance,priya.nair@praxisga.com,Verified,,,,https://praxisga.com,"
      "Mumbai,,India,management consulting,,4")


@pytest.fixture
def ctx(settings, monkeypatch):
    monkeypatch.setattr("outreach.research.fetch_text",
                        lambda http, url, limit=3500: ("Praxis", "Praxis describes itself as a consulting and transformation firm "
                                                                  "working across strategy, digital and AI for Indian businesses."))
    c = P.Context(settings, http=Http(sleep=lambda s: None), sleep=lambda s: None)
    c.search = FakeSearch()
    c.researcher.search = c.search
    c.llm = FakeLLM(facts=[pick("S1.1", "Praxis describes itself as a consulting and transformation firm", kind="business")])
    c.researcher.llm = c.llm
    c.drafter.llm = c.llm
    monkeypatch.setattr(c.verifier.dns, "mx", lambda d: ("ok", ["mx." + d]))
    return c


def ingest(ctx, *rows):
    ingest_csv_text(ctx.store, apollo_csv(*rows), "t.csv", "internship")


def test_end_to_end_dry_run(ctx):
    ctx.s.force_window = True
    ingest(ctx, GOOD, GUESS, ROLE, HR)
    report = P.run(ctx, ["verify", "research", "score", "draft", "send"])
    states = {l.person.first_name: l.state for l in ctx.store.all()}
    assert states["Madhur"] == LeadState.APPROVED          # dry run never changes state to SENT
    assert states["Rohan"] == LeadState.NEEDS_ENRICHMENT   # unverified guess: quarantined, never queued
    assert states["Info"] == LeadState.NEEDS_ENRICHMENT    # role account → INVALID
    assert states["Priya"] == LeadState.DISQUALIFIED       # HR title
    assert [w["to"] for w in report.would_send] == ["madhur.singhal@praxisga.com"]
    assert not report.systemic_failures()
    madhur = next(l for l in ctx.store.all() if l.person.first_name == "Madhur")
    assert madhur.dossier and madhur.score and madhur.draft.qa.passed and not madhur.sends


def test_quarantined_lead_can_never_reach_send_queue(ctx):
    ctx.s.force_window = True
    ingest(ctx, GUESS)
    lead = next(ctx.store.all())
    lead.draft = EmailDraft(messages=[EmailMessage(step=0, subject="s", body="b")])
    lead.set_state(LeadState.APPROVED, "forced")   # even if something mislabels it
    ctx.store.save(lead)
    report = P.run(ctx, ["send"])
    assert report.would_send == [] and ctx.store.get(lead.id).state == LeadState.NEEDS_ENRICHMENT


def test_window_and_caps(ctx):
    ingest(ctx, GOOD)
    P.run(ctx, ["verify", "research", "score", "draft"])
    ctx.s.force_window = False
    lead = next(l for l in ctx.store.all() if l.state == LeadState.APPROVED)
    sat = datetime(2026, 11, 21, 5, 0, tzinfo=timezone.utc)      # Saturday
    tue_ist_10am = datetime(2026, 11, 17, 4, 45, tzinfo=timezone.utc)
    assert not in_send_window(lead, ctx.s, sat)
    assert in_send_window(lead, ctx.s, tue_ist_10am)
    ctx.s.daily_send_cap = 0
    rep = P.run(ctx, ["send"])
    assert rep.stage("send").outcomes["cap_reached"] == 1 and not rep.would_send


def test_per_lead_errors_are_isolated_and_parked(ctx, monkeypatch):
    ingest(ctx, GOOD, GUESS)
    real = P._verify_lead

    def flaky(c, lead):
        if lead.person.first_name == "Rohan":
            raise ProviderError("hunter", "client_error", "bad request for this lead")
        return real(c, lead)

    monkeypatch.setattr(P, "_verify_lead", flaky)
    for _ in range(3):
        rep = P.run(ctx, ["verify"])
    states = {l.person.first_name: l.state for l in ctx.store.all()}
    assert states["Madhur"] == LeadState.VERIFIED
    assert states["Rohan"] == LeadState.NEEDS_REVIEW and "3x" in next(
        l for l in ctx.store.all() if l.person.first_name == "Rohan").state_reason
    assert rep.stage("verify").errors["client_error"] == 3  # one per tick (report is per Context)


def test_stage_crash_does_not_stop_later_stages(ctx, monkeypatch):
    ingest(ctx, GOOD)
    monkeypatch.setitem(P.STAGES, "ingest", lambda c: 1 / 0)
    rep = P.run(ctx, ["ingest", "verify"])
    assert "ingest" in rep.crashed and rep.stage("verify").processed == 1
    assert rep.systemic_failures()  # crash is surfaced → workflow goes red


def test_draft_stage_waits_without_llm(ctx):
    ingest(ctx, GOOD)
    ctx.llm.available = False
    rep = P.run(ctx, ["verify", "research", "score", "draft"])
    assert next(ctx.store.all()).state == LeadState.SCORED
    assert "no LLM" in rep.stage("draft").notes[0]


def test_idempotent_ticks(ctx):
    ctx.s.force_window = True
    ingest(ctx, GOOD)
    P.run(ctx, ["verify", "research", "score", "draft", "send"])
    snap = {l.id: (l.state, l.draft.step(0).subject if l.draft else None) for l in ctx.store.all()}
    llm_calls = ctx.llm.calls
    P.run(ctx, ["verify", "research", "score", "draft"])
    assert {l.id: (l.state, l.draft.step(0).subject if l.draft else None) for l in ctx.store.all()} == snap
    assert ctx.llm.calls == llm_calls  # nothing re-researched or re-drafted


def test_live_send_records_and_checkpoints(ctx, monkeypatch):
    ctx.s.force_window = True
    ctx.s.dry_run = False
    ctx.s.keys.sender_email, ctx.s.keys.gmail_app_password = "me@gmail.com", "pw"
    ingest(ctx, GOOD)
    P.run(ctx, ["verify", "research", "score", "draft"])
    sent = []
    monkeypatch.setattr(ctx.gmail, "already_sent", lambda *a: False)
    monkeypatch.setattr(ctx.gmail, "send", lambda lead, step: sent.append(lead.email) or
                        SendRecord(step=step, sent_at=utcnow(), message_id="<m1@gmail.com>", to=lead.email))
    P.run(ctx, ["send"])
    assert sent == ["madhur.singhal@praxisga.com"]
    reloaded = Store(ctx.s.state_dir)        # persisted immediately
    lead = next(reloaded.all())
    assert lead.state == LeadState.SENT and lead.sends[0].message_id == "<m1@gmail.com>"
    assert '"kind": "sent"' in (ctx.s.state_dir / "events.jsonl").read_text()
    P.run(ctx, ["send"])                      # second tick must not resend
    assert sent == ["madhur.singhal@praxisga.com"]


def test_suppression_blocks_send(ctx):
    ctx.s.force_window = True
    ingest(ctx, GOOD)
    P.run(ctx, ["verify", "research", "score", "draft"])
    ctx.suppression.add("madhur.singhal@praxisga.com", "test")
    rep = P.run(ctx, ["send"])
    assert not rep.would_send and next(ctx.store.all()).state == LeadState.OPTED_OUT


def test_followup_schedule(settings):
    now = utcnow()
    lead = Lead(id="x", state=LeadState.SENT, person=Person(full_name="A B"),
                draft=EmailDraft(messages=[EmailMessage(step=i, subject="s", body="b") for i in range(3)]),
                sends=[SendRecord(step=0, sent_at=now - timedelta(days=5))])
    assert followup_due(lead, settings, now) == 1
    lead.sends.append(SendRecord(step=1, sent_at=now - timedelta(days=2)))
    assert followup_due(lead, settings, now) is None
    assert followup_due(lead, settings, now + timedelta(days=5)) == 2
    lead.legacy["no_followups"] = True
    assert followup_due(lead, settings, now + timedelta(days=30)) is None


def test_followups_skipped_without_imap(ctx):
    ctx.s.force_window = True
    lead = Lead(id="ld_f", state=LeadState.SENT, email="a@acme.com", person=Person(full_name="A B"),
                draft=EmailDraft(messages=[EmailMessage(step=i, subject="s", body="b") for i in range(3)]),
                sends=[SendRecord(step=0, sent_at=utcnow() - timedelta(days=9))])
    ctx.store.save(lead)
    rep = P.run(ctx, ["sync_replies", "send"])   # no Gmail creds → IMAP unavailable
    assert not ctx.followups_allowed and not rep.would_send


def test_llm_quota_exhaustion_never_parks_leads(ctx):
    from outreach.llm import LLMError

    ingest(ctx, GOOD)
    P.run(ctx, ["verify", "research", "score"])

    def out_of_quota(*a, **k):
        raise LLMError("quota", "daily quota exhausted")

    ctx.drafter.draft = out_of_quota
    for _ in range(4):
        P.run(ctx, ["draft"])
    lead = next(ctx.store.all())
    assert lead.state == LeadState.SCORED and not lead.attempts.get("draft")
