from __future__ import annotations

from datetime import timedelta

import pytest
import responses

from outreach.http import Http, ProviderError
from outreach.models import EmailCandidate, EmailStatus, utcnow
from outreach.verify import Verifier, policy_ok, sendable, syntax_ok


@pytest.fixture
def verifier(settings, monkeypatch):
    v = Verifier(Http(sleep=lambda s: None), settings)
    monkeypatch.setattr(v.dns, "mx", lambda d: {"dead.example": ("nxdomain", []),
                                                 "nullmx.example": ("null_mx", [])}.get(d, ("ok", ["mx." + d])))
    return v


def cand(email, **kw):
    return EmailCandidate(email=email, source=kw.pop("source", "input:apollo"), **kw)


@pytest.mark.parametrize("email,ok", [("ann.lee@acme.com", True), ("ann..lee@acme.com", False),
                                      ("ann@acme.con", False), ("no-at-sign", False), ("a@b.c", False)])
def test_syntax(email, ok):
    assert syntax_ok(email)[0] is ok


@pytest.mark.parametrize("email,ok", [("info@acme.com", False), ("support.india@acme.com", False),
                                      ("noreply@acme.com", False), ("ann@mailinator.com", False),
                                      ("ira.shah@acme.com", True), ("hima@acme.com", True),
                                      ("lokendra-singh@googlegroups.com", False), ("team@groups.io", False),
                                      ("ann@lists.acme.org", False)])
def test_role_and_disposable(email, ok):
    assert policy_ok(email)[0] is ok


def test_previously_verified_mailing_list_is_never_sendable(settings):
    from outreach.models import VerificationResult
    stale = VerificationResult(email="lokendra-singh@googlegroups.com", status=EmailStatus.VERIFIED, confidence=90)
    assert not sendable(stale, settings)


def test_invalid_domain(verifier):
    assert verifier.verify(cand("a@dead.example")).status == EmailStatus.INVALID
    assert verifier.verify(cand("a@nullmx.example")).status == EmailStatus.INVALID


def test_provider_proof_verifies_when_smtp_blocked(verifier, settings):
    r = verifier.verify(cand("ann@acme.com", provider_status="verified", confidence=85, catch_all_hint=False))
    assert r.status == EmailStatus.VERIFIED and r.method == "provider" and sendable(r, settings)


def test_stale_provider_proof_is_quarantined(verifier, settings):
    r = verifier.verify(cand("ann@acme.com", provider_status="verified", confidence=85,
                             provider_checked_at=utcnow() - timedelta(days=400)))
    assert r.status == EmailStatus.UNVERIFIED_QUARANTINED and not sendable(r, settings)


def test_catch_all_with_provider_proof_is_sendable(verifier, settings):
    r = verifier.verify(cand("vivek.dua@kearney.com", provider_status="verified", confidence=85, catch_all_hint=True))
    assert r.status == EmailStatus.CATCH_ALL and r.confidence >= settings.catch_all_min_confidence
    assert sendable(r, settings)


def test_pattern_guess_never_sendable_without_mailbox_proof(verifier, settings):
    guess = cand("ann.lee@acme.com", source="pattern", provider_status="guessed", confidence=20)
    r = verifier.verify(guess)
    assert r.status == EmailStatus.UNVERIFIED_QUARANTINED and not sendable(r, settings)
    guess.catch_all_hint = True
    r = verifier.verify(guess)
    assert r.status == EmailStatus.CATCH_ALL and not sendable(r, settings)


def test_pattern_cannot_borrow_provider_status(verifier, settings):
    r = verifier.verify(cand("x@acme.com", source="pattern", provider_status="verified"))
    assert not sendable(r, settings)


@responses.activate
def test_api_verifier_invalid_overrides_provider_proof(settings, monkeypatch):
    settings.keys.millionverifier = "k"
    v = Verifier(Http(sleep=lambda s: None), settings)
    monkeypatch.setattr(v.dns, "mx", lambda d: ("ok", ["mx"]))
    responses.get("https://api.millionverifier.com/api/v3/", json={"result": "invalid"})
    r = v.verify(cand("ann@acme.com", provider_status="verified"))
    assert r.status == EmailStatus.INVALID and r.method == "api:millionverifier"


@responses.activate
def test_api_verifier_promotes_pattern(settings, monkeypatch):
    settings.keys.millionverifier = "k"
    v = Verifier(Http(sleep=lambda s: None), settings)
    monkeypatch.setattr(v.dns, "mx", lambda d: ("ok", ["mx"]))
    responses.get("https://api.millionverifier.com/api/v3/", json={"result": "ok"})
    r = v.verify(cand("ann.lee@acme.com", source="pattern", provider_status="guessed"))
    assert r.status == EmailStatus.VERIFIED


# ── HTTP client ─────────────────────────────────────────────────────────────
@responses.activate
def test_retries_5xx_then_succeeds():
    responses.get("https://api.test/x", status=503)
    responses.get("https://api.test/x", json={"ok": 1})
    assert Http(sleep=lambda s: None).request("GET", "https://api.test/x", provider="t") == {"ok": 1}


@responses.activate
def test_retry_after_is_respected():
    slept = []
    responses.get("https://api.test/x", status=429, headers={"Retry-After": "7"})
    responses.get("https://api.test/x", json={"ok": 1})
    Http(sleep=slept.append).request("GET", "https://api.test/x", provider="t")
    assert slept == [7.0]


@responses.activate
def test_auth_error_trips_breaker_without_retry():
    responses.get("https://api.test/x", status=401)
    h = Http(sleep=lambda s: None)
    with pytest.raises(ProviderError) as e:
        h.request("GET", "https://api.test/x", provider="t")
    assert e.value.category == "auth" and len(responses.calls) == 1
    with pytest.raises(ProviderError) as e2:
        h.request("GET", "https://api.test/x", provider="t")
    assert e2.value.category == "circuit_open" and len(responses.calls) == 1


@responses.activate
def test_breaker_opens_after_repeated_failures():
    responses.get("https://api.test/x", status=500)
    h = Http(max_retries=0, sleep=lambda s: None)
    for _ in range(4):
        with pytest.raises(ProviderError):
            h.request("GET", "https://api.test/x", provider="t")
    assert not h.available("t")


@responses.activate
def test_client_error_is_not_retried():
    responses.get("https://api.test/x", status=422)
    with pytest.raises(ProviderError) as e:
        Http(sleep=lambda s: None).request("GET", "https://api.test/x", provider="t")
    assert e.value.category == "client_error" and len(responses.calls) == 1


@responses.activate
def test_hunter_429_means_quota():
    responses.get("https://api.hunter.io/v2/email-finder", status=429)
    h = Http(sleep=lambda s: None)
    with pytest.raises(ProviderError) as e:
        h.request("GET", "https://api.hunter.io/v2/email-finder", provider="hunter")
    assert e.value.category == "quota" and not h.available("hunter")


@responses.activate
def test_google_retry_delay_in_body_is_respected():
    slept = []
    responses.post("https://api.test/g", status=429,
                   json={"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"}]}})
    responses.post("https://api.test/g", json={"ok": 1})
    Http(sleep=slept.append).request("POST", "https://api.test/g", provider="llm:gemini")
    assert slept == [12.0]


@responses.activate
def test_site_403_is_blocked_not_auth():
    responses.get("https://site.test/", status=403)
    h = Http(sleep=lambda s: None)
    with pytest.raises(ProviderError) as e:
        h.request("GET", "https://site.test/", provider="fetch:site.test", expect_json=False)
    assert e.value.category == "blocked" and not h.available("fetch:site.test")


@responses.activate
def test_llm_falls_back_through_model_chain(settings):
    from pydantic import BaseModel

    from outreach.llm import LLM

    class Out(BaseModel):
        ok: bool

    settings.keys.gemini = "g"
    base = "https://generativelanguage.googleapis.com/v1beta/models/"
    quota = {"error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
                                   {"retryDelay": "70000s"}]}}
    responses.post(base + "gemini-3.6-flash:generateContent", status=429, json=quota)
    responses.post(base + "gemini-3.5-flash:generateContent", status=503, json={"error": {"message": "overloaded"}})
    responses.post(base + "gemini-3-flash-preview:generateContent",
                   json={"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}, "finishReason": "STOP"}]})
    llm = LLM(Http(max_retries=1, sleep=lambda s: None), settings)
    assert llm.structured("writer", "sys", "p", Out).ok is True
    assert "gemini:gemini-3.6-flash#0" in llm.exhausted
    assert llm.model("writer") != "gemini-3.6-flash"
    calls_36 = sum(1 for c in responses.calls if "3.6-flash" in c.request.url)
    assert calls_36 == 1  # daily quota is not retried
