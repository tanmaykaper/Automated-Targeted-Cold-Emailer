"""Pydantic v2 data contracts.

Every external payload is parsed into these models at the boundary, so a
malformed API response fails one record (ValidationError → logged per lead)
instead of raising KeyError deep inside a batch.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class LeadState(StrEnum):
    DISCOVERED = "DISCOVERED"
    ENRICHED = "ENRICHED"
    NEEDS_ENRICHMENT = "NEEDS_ENRICHMENT"  # no verifiable email yet (quarantine)
    VERIFIED = "VERIFIED"
    RESEARCHED = "RESEARCHED"
    SCORED = "SCORED"
    DRAFTED = "DRAFTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"          # QA failed twice / repeated stage errors
    APPROVED = "APPROVED"
    SENT = "SENT"
    REPLIED = "REPLIED"
    BOUNCED = "BOUNCED"
    OPTED_OUT = "OPTED_OUT"
    DISQUALIFIED = "DISQUALIFIED"


TERMINAL_STATES = {LeadState.REPLIED, LeadState.BOUNCED, LeadState.OPTED_OUT, LeadState.DISQUALIFIED}


class EmailStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    VERIFIED = "VERIFIED"
    CATCH_ALL = "CATCH_ALL"
    UNVERIFIED_QUARANTINED = "UNVERIFIED_QUARANTINED"
    INVALID = "INVALID"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", use_enum_values=False, validate_assignment=False)


class Person(_Model):
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    title: str = ""
    seniority: str = ""
    departments: str = ""
    linkedin_url: str = ""
    location: str = ""
    city: str = ""
    country: str = ""

    def display_name(self) -> str:
        return self.full_name or f"{self.first_name} {self.last_name}".strip()


class Company(_Model):
    name: str = ""
    domain: str = ""
    domain_confidence: float = 0.0
    domain_source: str = ""
    website: str = ""
    linkedin_url: str = ""
    industry: str = ""
    keywords: list[str] = Field(default_factory=list)
    employees: Optional[int] = None
    funding: str = ""
    description: str = ""


class EmailCandidate(_Model):
    email: str
    source: str                       # input:apollo | hunter | prospeo | pattern | ...
    confidence: int = 0               # 0–100, provider's own confidence
    provider_status: str = ""         # provider's verification label (verified/valid/accept_all/guessed…)
    provider_checked_at: Optional[datetime] = None
    catch_all_hint: Optional[bool] = None


class VerificationResult(_Model):
    email: str
    status: EmailStatus = EmailStatus.UNKNOWN
    confidence: int = 0
    reason: str = ""
    checks: dict[str, str] = Field(default_factory=dict)  # layer → outcome
    method: str = ""                                      # which layer decided
    verified_at: datetime = Field(default_factory=utcnow)

    @property
    def sendable(self) -> bool:
        return self.status in (EmailStatus.VERIFIED, EmailStatus.CATCH_ALL)


class Fact(_Model):
    id: str
    claim: str
    about: Literal["company", "person", "industry"] = "company"
    kind: Literal["business", "news", "funding", "hiring", "launch", "expansion", "initiative",
                  "talk", "article", "podcast", "post", "role", "other"] = "other"
    source_url: str
    evidence: str                      # verbatim span from the source text
    date: str = ""
    verified: bool = False             # evidence found verbatim in fetched source


class OutreachAngle(_Model):
    angle: str
    fact_ids: list[str] = Field(default_factory=list)
    proof_point: str = ""              # SENDER proof point key to pair with


class ResearchDossier(_Model):
    company_summary: str = ""
    role_scope: str = ""
    facts: list[Fact] = Field(default_factory=list)
    angles: list[OutreachAngle] = Field(default_factory=list)
    hook_confidence: Literal["high", "medium", "low"] = "low"
    hook_reason: str = ""
    sources: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    model: str = ""
    generated_at: datetime = Field(default_factory=utcnow)

    def verified_facts(self) -> list[Fact]:
        return [f for f in self.facts if f.verified]

    def fact(self, fid: str) -> Optional[Fact]:
        return next((f for f in self.facts if f.id == fid), None)


class ScoreComponent(_Model):
    name: str
    points: int
    max_points: int
    reason: str


class LeadScore(_Model):
    total: int
    tier: Literal["A", "B", "C"]
    components: list[ScoreComponent]
    rationale: str
    scored_at: datetime = Field(default_factory=utcnow)


class EmailMessage(_Model):
    step: int = 0                      # 0 = initial, 1..n = follow-ups
    subject: str
    body: str

    @property
    def word_count(self) -> int:
        """Words in the message itself, excluding the appended sign-off/signature."""
        text = re.split(r"\n\n(?:Best,\n|Tanmay\s*$)", self.body)[0]
        return len(re.findall(r"[A-Za-z0-9'’\-]+", text))


class QAReport(_Model):
    passed: bool
    lint_issues: list[str] = Field(default_factory=list)
    critic_issues: list[str] = Field(default_factory=list)
    attempts: int = 1


class EmailDraft(_Model):
    messages: list[EmailMessage]
    angle: str = ""
    fact_ids: list[str] = Field(default_factory=list)
    proof_point: str = ""
    model: str = ""
    qa: Optional[QAReport] = None
    created_at: datetime = Field(default_factory=utcnow)

    def step(self, n: int) -> Optional[EmailMessage]:
        return next((m for m in self.messages if m.step == n), None)


class SendRecord(_Model):
    step: int
    sent_at: datetime
    message_id: str = ""
    subject: str = ""
    to: str = ""
    legacy: bool = False


class ErrorRecord(_Model):
    stage: str
    category: str
    message: str
    at: datetime = Field(default_factory=utcnow)


class Lead(_Model):
    id: str
    state: LeadState = LeadState.DISCOVERED
    state_reason: str = ""
    source: str = ""                   # csv:apollo | csv:linkedin | apollo_api | search:serper | legacy_v1
    source_detail: str = ""
    discovered_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    outreach_mode: str = "internship"
    vertical: str = ""
    region: str = ""                   # IN | UK | US | SG | ME | EU | AU | REMOTE | OTHER

    person: Person = Field(default_factory=Person)
    company: Company = Field(default_factory=Company)

    email: str = ""
    candidates: list[EmailCandidate] = Field(default_factory=list)
    verification: Optional[VerificationResult] = None
    tried_finders: list[str] = Field(default_factory=list)

    dossier: Optional[ResearchDossier] = None
    score: Optional[LeadScore] = None
    draft: Optional[EmailDraft] = None
    sends: list[SendRecord] = Field(default_factory=list)

    errors: list[ErrorRecord] = Field(default_factory=list)
    attempts: dict[str, int] = Field(default_factory=dict)
    legacy: dict = Field(default_factory=dict)

    def set_state(self, state: LeadState, reason: str = "") -> None:
        self.state = state
        self.state_reason = reason
        self.updated_at = utcnow()

    def record_error(self, stage: str, category: str, message: str) -> int:
        self.errors = (self.errors + [ErrorRecord(stage=stage, category=category, message=message[:500])])[-10:]
        self.attempts[stage] = self.attempts.get(stage, 0) + 1
        self.updated_at = utcnow()
        return self.attempts[stage]

    @property
    def last_send(self) -> Optional[SendRecord]:
        return max(self.sends, key=lambda s: s.sent_at) if self.sends else None


# ── Canonical identity ──────────────────────────────────────────────────────
_LEGAL_SUFFIX = re.compile(
    r"\b(private limited|pvt\.? ?ltd\.?|limited|ltd\.?|llp|llc|inc\.?|incorporated|corp\.?|corporation|"
    r"gmbh|plc|pte\.? ?ltd\.?|co\.?|company|& co\.?|s\.a\.|ag|bv|nv)\b",
    re.I,
)


def norm_text(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_company(name: str) -> str:
    return norm_text(_LEGAL_SUFFIX.sub(" ", name or ""))


def linkedin_slug(url: str) -> str:
    m = re.search(r"linkedin\.com/(?:in|pub)/([^/?#]+)", url or "", re.I)
    return m.group(1).lower().strip() if m else ""


def identity_keys(person: Person, company: Company, email: str = "") -> list[str]:
    """All keys that identify this person. Any overlap with an existing lead = same person."""
    keys: list[str] = []
    slug = linkedin_slug(person.linkedin_url)
    if slug:
        keys.append(f"li:{slug}")
    if email:
        keys.append(f"em:{email.strip().lower()}")
    parts = person.display_name().split()
    first = norm_text(person.first_name or (parts[0] if parts else ""))
    last = norm_text(person.last_name or (parts[-1] if len(parts) > 1 else ""))
    if first and last:
        for org in dict.fromkeys([company.domain.lower(), norm_company(company.name)]):
            if org:
                keys.append(f"nm:{first}|{last}|{org}")
    return keys


def make_lead_id(keys: list[str]) -> str:
    basis = keys[0] if keys else utcnow().isoformat()
    return "ld_" + hashlib.sha1(basis.encode()).hexdigest()[:12]
