from __future__ import annotations

import pytest
from fakes import GOOD_BODY, FakeLLM, pick

from outreach.drafting import Drafter, lint
from outreach.http import Http
from outreach.ingest.discovery import parse_linkedin_result
from outreach.enrich.domain import name_match_score, resolve_domain
from outreach.models import (Company, EmailStatus, Fact, Lead, LeadScore, Person, ResearchDossier,
                             VerificationResult)
from outreach.research import Researcher, Source, evidence_in_source
from outreach.scoring import prequalify, score_lead
from outreach.web import SearchResult, parse_ddg_lite
from conftest import FakeSearch


def lead(title="Managing Partner and CEO", company="Praxis Global Alliance", status=EmailStatus.VERIFIED, conf=90,
         industry="management consulting", location="Mumbai, India"):
    l = Lead(id="ld_test", person=Person(first_name="Madhur", last_name="Singhal", full_name="Madhur Singhal",
                                         title=title, location=location),
             company=Company(name=company, domain="praxisga.com", industry=industry), region="IN")
    l.verification = VerificationResult(email="madhur@praxisga.com", status=status, confidence=conf, method="provider")
    return l


# ── research ────────────────────────────────────────────────────────────────
def test_evidence_matching():
    src = "Praxis Global Alliance today announced the launch of its AI practice in Mumbai, led by its CEO."
    assert evidence_in_source("announced the launch of its AI practice in Mumbai", src)
    assert evidence_in_source("Praxis Global Alliance today announced the launch of its AI practice in mumbai,", src)
    assert not evidence_in_source("announced the acquisition of a Singapore analytics firm", src)
    assert not evidence_in_source("AI", src)  # too short to count as evidence


def test_extraction_can_only_cite_real_sentences():
    l = lead()
    sources = [Source("S1", "https://praxisga.com/", "Praxis", "Praxis is a consulting and transformation firm in India.", "site"),
               Source("S2", "https://news.example/a", "Panel on consumer India",
                      "Panel on consumer India. Madhur Singhal spoke on a panel about Indian consumer markets.", "person")]
    llm = FakeLLM(facts=[
        pick("S1.1", "Praxis is a consulting and transformation firm in India", kind="business"),
        pick("S1.1", "duplicate pick", kind="business"),                                  # duplicate → ignored
        pick("S1.9", "Raised $50M from Sequoia", kind="funding"),                          # no such sentence → dropped
        pick("S2.1", "Madhur Singhal spoke on a panel about Indian consumer markets", about="person", kind="talk"),
        pick("S2.0", "Madhur won a national award for leadership", about="person", kind="article"),  # unfaithful claim
    ])
    r = Researcher(Http(), Http(), FakeSearch(), llm, "internship")
    facts, _ = r.extract(l, sources)
    assert [f.evidence for f in facts] == ["Praxis is a consulting and transformation firm in India.",
                                           "Madhur Singhal spoke on a panel about Indian consumer markets.",
                                           "Panel on consumer India"]
    assert all(f.verified for f in facts)
    assert facts[2].claim == "Panel on consumer India"  # unfaithful paraphrase replaced by the real sentence
    assert r.hook_confidence(facts)[0] == "high"


def test_person_attribution_requires_named_source():
    l = lead()
    sources = [Source("S1", "https://praxisga.com/", "Praxis", "The CEO spoke at a panel on retail growth in Mumbai last week.", "site")]
    llm = FakeLLM(facts=[pick("S1.1", "The CEO spoke at a panel on retail growth in Mumbai", about="person", kind="talk")])
    facts, _ = Researcher(Http(), Http(), FakeSearch(), llm, "internship").extract(l, sources)
    assert facts[0].about == "company"


def test_dossier_fallback_when_no_public_hook(monkeypatch):
    l = lead()
    monkeypatch.setattr("outreach.research.fetch_text", lambda *a, **k: (_ for _ in ()).throw(
        __import__("outreach.http", fromlist=["ProviderError"]).ProviderError("fetch", "network", "down")))
    r = Researcher(Http(), Http(), FakeSearch(), FakeLLM(facts=[]), "internship")
    d = r.build(l)
    assert d.hook_confidence == "low" and "fall back" in d.hook_reason
    assert d.angles  # still an honest role/company-level angle


def test_heuristic_extractor_skips_profile_pages(monkeypatch):
    l = lead()
    monkeypatch.setattr("outreach.research.fetch_text", lambda *a, **k: ("Praxis", "Praxis is a leading consulting firm in India offering strategy."))
    search = FakeSearch({"Singhal": [SearchResult("Madhur Singhal - CEO - Praxis | LinkedIn", "https://in.linkedin.com/in/madhur",
                                                 "Madhur Singhal is CEO at Praxis Global Alliance in Mumbai with experience."),
                                    SearchResult("Podcast ep 12", "https://pod.example/12",
                                                 "On this podcast Madhur Singhal discusses consumer demand in tier-2 Indian cities.")]})
    llm = FakeLLM()
    llm.available = False
    d = Researcher(Http(), Http(), search, llm, "internship").build(l)
    urls = [f.source_url for f in d.verified_facts()]
    assert "https://in.linkedin.com/in/madhur" not in urls
    assert any(f.kind == "podcast" and f.about == "person" for f in d.verified_facts())
    assert d.hook_confidence == "high"


# ── scoring ─────────────────────────────────────────────────────────────────
def _with_dossier(l, hook="medium", kinds=("news",)):
    l.dossier = ResearchDossier(hook_confidence=hook, hook_reason=hook, facts=[
        Fact(id=f"F{i}", claim="x", kind=k, source_url="u", evidence="e", verified=True) for i, k in enumerate(kinds, 1)])
    return l


def test_score_tiers_and_breakdown(settings):
    s = score_lead(_with_dossier(lead(), "high", ("funding",)), settings)
    assert s.tier == "A" and s.total >= 80 and len(s.components) == 5 and "Tier A" in s.rationale
    s = score_lead(_with_dossier(lead(title="Consultant", industry="", company="Acme Widgets"), "low", ()), settings)
    assert s.tier == "C"


@pytest.mark.parametrize("total,tier", [(80, "A"), (79, "B"), (60, "B"), (59, "C")])
def test_tier_boundaries(settings, monkeypatch, total, tier):
    from outreach import scoring
    from outreach.models import ScoreComponent
    monkeypatch.setattr(scoring, "_title", lambda l: ScoreComponent(name="t", points=total, max_points=100, reason=""))
    for f in ("_company", "_deliverability", "_hook"):
        monkeypatch.setattr(scoring, f, lambda l: ScoreComponent(name="z", points=0, max_points=1, reason=""))
    monkeypatch.setattr(scoring, "_signal", lambda l, m: ScoreComponent(name="z", points=0, max_points=1, reason=""))
    assert score_lead(lead(), settings).tier == tier


def test_catch_all_scores_lower_than_verified(settings):
    v = score_lead(_with_dossier(lead()), settings).total
    c = score_lead(_with_dossier(lead(status=EmailStatus.CATCH_ALL, conf=75)), settings).total
    assert c < v


@pytest.mark.parametrize("title,ok", [("Talent Acquisition Manager", False), ("Executive Assistant to CEO", False),
                                      ("Director, International Business", True), ("Global People & Development Senior Director", False),
                                      ("Freelance consultant", False), ("Partner", True)])
def test_prequalify_titles(settings, title, ok):
    assert prequalify(lead(title=title), settings)[0] is ok


# ── drafting / QA ───────────────────────────────────────────────────────────
FU = ["One more thought on pricing work. Would that help?", "No worries if not a fit — I won't keep emailing."]


def test_lint_accepts_good_copy():
    assert lint("praxis data work", GOOD_BODY, FU, "Madhur") == []


@pytest.mark.parametrize("subject,body,expect", [
    ("Quick Question About Your Amazing Firm!", GOOD_BODY, "subject"),
    ("praxis data work", GOOD_BODY.replace("Praxis describes", "I hope you're doing well. Praxis describes"), "banned phrase"),
    ("praxis data work", GOOD_BODY.replace("this year", "this year at {{company}}"), "placeholder"),
    ("praxis data work", GOOD_BODY.replace("this year", "this year (see https://x.com/cv)"), "link"),
    ("praxis data work", "Hi Madhur,\n\nShort note.", "words"),
    ("praxis data work", GOOD_BODY.replace("Hi Madhur", "Dear Sir"), "open with"),
    ("praxis data work", GOOD_BODY.replace("this year", "this year, guaranteed free"), "spam"),
])
def test_lint_rejections(subject, body, expect):
    issues = lint(subject, body, FU, "Madhur")
    assert any(expect in i for i in issues), issues


def _scored_lead():
    l = _with_dossier(lead(), "medium", ("news",))
    l.score = LeadScore(total=75, tier="B", components=[], rationale="")
    return l


def test_drafter_passes_and_appends_signature():
    d = Drafter(FakeLLM(), "internship").draft(_scored_lead())
    assert d.qa.passed and d.qa.attempts == 1
    assert d.step(0).body.endswith("linkedin.com/in/tanmay-k-a344b1226")
    assert d.step(1).subject == "Re: praxis data work" and d.step(2)


def test_drafter_rewrites_once_then_passes():
    llm = FakeLLM(draft_bodies=[GOOD_BODY.replace("Praxis describes", "I came across Praxis, which describes"), GOOD_BODY])
    d = Drafter(llm, "internship").draft(_scored_lead())
    assert d.qa.passed and d.qa.attempts == 2
    assert any("FAILED REVIEW" in p and "came across" in p for p in llm.prompts)


def test_drafter_flags_for_review_after_two_failures():
    llm = FakeLLM(critic_verdicts=["fail", "fail"])
    d = Drafter(llm, "internship").draft(_scored_lead())
    assert not d.qa.passed and d.qa.attempts == 2 and d.qa.critic_issues


# ── discovery / domains / web parsing ───────────────────────────────────────
def test_parse_linkedin_results():
    ok = parse_linkedin_result(SearchResult("Ramesh Rao - Partner - Bain & Company | LinkedIn", "https://www.linkedin.com/in/rameshrao1?x=1",
                                            "Experience: Bain & Company · Location: Mumbai"))
    assert ok == {"name": "Ramesh Rao", "title": "Partner", "company": "Bain & Company", "location": "Mumbai",
                  "linkedin_url": "https://www.linkedin.com/in/rameshrao1"}
    assert parse_linkedin_result(SearchResult("Health Tech - Senior Consultant - Health Tech ... | LinkedIn",
                                              "https://linkedin.com/in/x", "")) is None
    assert parse_linkedin_result(SearchResult("Atharva Shah - Director - LinkedIn", "https://linkedin.com/in/a", "")) is None
    assert parse_linkedin_result(SearchResult("Jo Lee - Talent Acquisition Lead - Acme | LinkedIn", "https://linkedin.com/in/j", "")) is None


def test_domain_resolution_rejects_aggregators_and_requires_mx():
    l = Lead(id="x", person=Person(full_name="A B"), company=Company(name="Amara Partners"))
    search = FakeSearch({"Amara": [SearchResult("Amara Partners | PEI", "https://privateequityinternational.com/amara", ""),
                                   SearchResult("Amara Partners", "https://amaragp.com", "Amara Partners"),
                                   SearchResult("Amara Partners — home", "https://www.amarapt.com/", "Amara Partners")]})
    dom, conf, src = resolve_domain(l, search, has_mx=lambda d: d == "amarapt.com")
    assert dom == "amarapt.com" and src == "search"
    l2 = Lead(id="y", person=Person(full_name="A B"), company=Company(name="LinkedIn"))
    assert resolve_domain(l2, FakeSearch(), lambda d: True)[0] == ""


def test_name_match():
    assert name_match_score("Boston Consulting Group (BCG)", "bcg.com") >= 0.85
    assert name_match_score("Strategy & Operations", "docusign.com") == 0.0


def test_ddg_lite_parser():
    page = ('<a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.praxisga.com%2F&rut=x" class=\'result-link\'>'
            'Praxis &amp;amp; Co</a><td class=\'result-snippet\'>Consulting &amp;amp; transformation</td>')
    r = parse_ddg_lite(page)
    assert r[0].url == "https://www.praxisga.com/" and r[0].title == "Praxis & Co" and "&" in r[0].snippet
