from __future__ import annotations

from datetime import timedelta

from conftest import apollo_csv

from outreach.ingest import upsert
from outreach.ingest.csv_import import detect_source, ingest_csv_text, ingest_dropins
from outreach.ingest.legacy import migrate_legacy
from outreach.models import Company, EmailCandidate, Lead, LeadState, Person, SendRecord, utcnow
from outreach.store import Store

ROW_A = ("Madhur,Singhal,Managing Partner and CEO,Praxis Global Alliance,madhur@praxisga.com,Verified,Not Catch-all,"
         "2026-07-13T06:11:47+00:00,http://www.linkedin.com/in/madhursinghal,https://praxisga.com,Mumbai,,India,"
         "management consulting,\"consulting, strategy\",abc123")
ROW_B = ("Vivek,Dua,Senior Partner,Kearney,vivek.dua@kearney.com,Verified,Catch-all,,"
         "http://www.linkedin.com/in/vivekdua,https://kearney.com,Gurgaon,,India,management consulting,,def456")


def test_apollo_csv_normalisation(settings):
    store = Store(settings.state_dir)
    res = ingest_csv_text(store, apollo_csv(ROW_A, ROW_B), "export.csv", "internship")
    assert res["source"] == "csv:apollo" and res["created"] == 2
    lead = next(l for l in store.all() if l.person.last_name == "Singhal")
    assert lead.company.domain == "praxisga.com"
    assert lead.region == "IN"
    c = lead.candidates[0]
    assert c.provider_status == "verified" and c.catch_all_hint is False and c.provider_checked_at is not None
    kearney = next(l for l in store.all() if l.person.last_name == "Dua")
    assert kearney.candidates[0].catch_all_hint is True


def test_ingest_is_idempotent_and_dedupes_across_sources(settings):
    store = Store(settings.state_dir)
    ingest_csv_text(store, apollo_csv(ROW_A), "a.csv", "internship")
    ingest_csv_text(store, apollo_csv(ROW_A), "a-again.csv", "internship")
    # Same person from a LinkedIn export: matched on LinkedIn slug, no email
    li = "First Name,Last Name,Company,Position,URL\nMadhur,Singhal,Praxis Global Alliance,CEO,https://www.linkedin.com/in/madhursinghal/\n"
    assert detect_source(li.splitlines()[0].split(",")) == "csv:linkedin"
    res = ingest_csv_text(store, li, "li.csv", "internship")
    assert res["merged"] == 1 and len(store) == 1


def test_unusable_emails_and_missing_columns(settings):
    store = Store(settings.state_dir)
    row = ROW_A.replace("madhur@praxisga.com", "email_not_unlocked@domain.com")
    ingest_csv_text(store, apollo_csv(row), "x.csv", "internship")
    assert list(store.all())[0].candidates == []
    bad = ingest_csv_text(store, "foo,bar\n1,2\n", "bad.csv", "internship")
    assert bad["error"]


def test_dropin_folder_archives_and_skips_identical_files(settings):
    inputs = settings.inputs_dir
    inputs.mkdir(parents=True)
    (inputs / "one.csv").write_text(apollo_csv(ROW_A))
    (inputs / "copy.csv").write_text(apollo_csv(ROW_A))
    store = Store(settings.state_dir)
    res = ingest_dropins(store, inputs)
    assert any(r.get("skipped_duplicate_of") for r in res)
    assert not list(inputs.glob("*.csv"))  # both archived
    assert len(list((inputs / "processed").rglob("*.csv"))) == 2


def test_checkpoint_roundtrip_and_corrupt_line(settings):
    store = Store(settings.state_dir)
    ingest_csv_text(store, apollo_csv(ROW_A, ROW_B), "a.csv", "internship")
    store.checkpoint()
    path = settings.state_dir / "leads.jsonl"
    path.write_text(path.read_text() + "{not json}\n")
    again = Store(settings.state_dir)
    assert len(again) == 2
    assert {l.id for l in again.all()} == {l.id for l in store.all()}


def test_contact_history_wins_on_merge(settings):
    store = Store(settings.state_dir)
    pending = Lead(id="", person=Person(first_name="A", last_name="B", linkedin_url="https://linkedin.com/in/ab"),
                   company=Company(name="X"))
    upsert(store, pending)
    sent = Lead(id="", person=Person(first_name="A", last_name="B", linkedin_url="https://linkedin.com/in/ab"),
                company=Company(name="X"), email="a@x.com", state=LeadState.SENT,
                sends=[SendRecord(step=0, sent_at=utcnow(), to="a@x.com", legacy=True)])
    lead, created = upsert(store, sent)
    assert not created and lead.state == LeadState.SENT and len(lead.sends) == 1


def test_new_candidates_requeue_quarantined_lead(settings):
    store = Store(settings.state_dir)
    lead, _ = upsert(store, Lead(id="", person=Person(first_name="A", last_name="B"), company=Company(name="X Co")))
    lead.set_state(LeadState.NEEDS_ENRICHMENT, "no email")
    store.save(lead)
    upsert(store, Lead(id="", person=Person(first_name="A", last_name="B"), company=Company(name="X Co"),
                       candidates=[EmailCandidate(email="a@x.com", source="input:apollo", provider_status="verified")]))
    assert store.get(lead.id).state == LeadState.DISCOVERED


def test_legacy_migration(tmp_path, settings):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "pipeline_v1.csv").write_text(
        "Target Name,Company,Position,Target Email,Email Verified,Email Source,LinkedIn URL,Location,Status,Sent At,"
        "Drafted Email Subject,Drafted Email Body\n"
        "Ann Lee,Acme,Partner,ann@acme.com,TRUE,hunter (confidence=90),https://linkedin.com/in/annlee,Mumbai,Sent,"
        "2026-11-17T10:00:00+00:00,hi,body\n"
        "Ann Lee,Acme,Partner,ann.lee@x.com,FALSE,pattern-inferred,https://linkedin.com/in/annlee,Mumbai,Sent,"
        "2026-11-18T10:00:00+00:00,hi2,body2\n"
        "Bo Chan,LinkedIn,Director,bo.chan@x.com,FALSE,pattern-inferred,,,Pending,,,\n")
    store = Store(settings.state_dir)
    stats = migrate_legacy(store, legacy, "internship")
    assert stats["pipeline_rows"] == 3
    ann = next(l for l in store.all() if l.person.full_name == "Ann Lee")
    assert ann.state == LeadState.SENT and len(ann.sends) == 2 and ann.legacy["no_followups"]
    bo = next(l for l in store.all() if l.person.full_name == "Bo Chan")
    assert bo.state == LeadState.DISCOVERED and bo.company.name == "" and bo.draft is None
    assert migrate_legacy(store, legacy, "internship") == {"skipped": "already migrated"}
    assert store.sends_between(utcnow() - timedelta(days=3650), utcnow() + timedelta(days=1)) == 2
