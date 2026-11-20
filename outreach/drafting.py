"""Cold-email drafting + QA gate (lint → LLM critic → one rewrite → NEEDS_REVIEW)."""
from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field

from .llm import LLM, LLMError
from .models import EmailDraft, EmailMessage, Lead, QAReport
from .profile import MODE_GOALS, SENDER

log = logging.getLogger(__name__)

SIGN_OFF = f"Best,\n{SENDER.full_name}\nBSc Economics, NMIMS Mumbai '27 · {SENDER.linkedin}"
FOLLOWUP_SIGN_OFF = SENDER.first_name

BANNED_PHRASES = (
    "hope this email finds you", "hope this finds you", "hope you're doing well", "hope you are doing well",
    "i hope you", "i came across", "came across your", "i was impressed", "impressed by", "impressive",
    "your journey", "delve", "synergy", "synergies", "game-changer", "game changer", "i am reaching out",
    "i'm reaching out", "reaching out to", "i wanted to reach out", "passionate", "thrilled", "eager",
    "leverage", "aligns with", "align with", "aligned with", "resonate", "resonated", "esteemed", "humbly",
    "cutting-edge", "cutting edge", "pick your brain", "touch base", "circle back", "circling back",
    "just following up", "bumping this", "value-add", "fast-paced", "in today's", "unlock", "elevate",
    "seamless", "revolutioniz", "testament", "tapestry", "kindly", "dear sir", "dear ma'am", "to whom it may concern",
    "i would love to", "i'd love to", "excited to", "inspiring", "admire", "fascinat", "deeply", "truly",
    "my name is", "quick question", "attached my", "i have attached", "please find attached", "as an ai",
    "rest assured", "look forward to hearing", "at your earliest convenience",
)
SPAM_WORDS = (
    "free", "guarantee", "act now", "urgent", "100%", "risk-free", "click here", "limited time", "winner",
    "no obligation", "opportunity of a lifetime", "$$$", "cash", "earn money", "special promotion", "offer expires",
)
PLACEHOLDER_RX = re.compile(r"\{\{.*?\}\}|\{[a-z_]+\}|\[[A-Z][A-Za-z ]+\]|<[A-Z][A-Za-z ]+>|\bXX+\b|\bTBD\b|\blorem\b", re.I)
URL_RX = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.(com|io|in|co|org|net)\b/?", re.I)


class _Draft(BaseModel):
    subject: str = Field(description="2-5 words, lowercase except proper nouns/acronyms")
    body: str = Field(description="Starts with 'Hi <first name>,' then blank line; 65-110 words; NO sign-off/signature")
    followup_1: str = Field(description="< 60 words, new angle, ends with a simple question; NO greeting line, NO signature")
    followup_2: str = Field(description="< 45 words, gracious close with an easy out; NO greeting line, NO signature")
    fact_ids: list[str] = Field(description="Ids of the verified facts the email relies on")
    proof_point: str = Field(description="Key of the sender proof point used")
    angle: str = Field(description="One line: the outreach angle used")


class _Critique(BaseModel):
    unsupported_claims: list[str] = Field(description="Claims about recipient/company not supported by the facts, or sender claims not in the proof points")
    style_issues: list[str] = Field(description="Generic, salesy, robotic, flattering or awkward phrasing; weak subject; pushy CTA")
    verdict: str = Field(description="'pass' only if there are no unsupported claims and no serious style issues, else 'fail'")


REGION_TONE = {
    "IN": "Indian senior professional: warm, respectful and direct; no flattery, no 'Sir/Ma'am'.",
    "UK": "UK reader: understated and precise; no hype.",
    "US": "US reader: direct and brisk; get to the point in line one.",
    "US_W": "US reader: direct and brisk; get to the point in line one.",
    "SG": "Singapore reader: precise, professional, efficient.",
    "ME": "Gulf reader: courteous and professional, still concise.",
    "EU": "European reader: clear, factual, unembellished.",
    "AU": "Australian reader: relaxed, plain-spoken, no corporate jargon.",
}

WRITER_SYSTEM = f"""You write first-touch cold emails for {SENDER.full_name}, a final-year economics student at NMIMS Mumbai,
to senior professionals. The only goal of email 1 is to earn a reply. Write like a sharp, well-prepared 21-year-old
who respects the reader's time — specific, plain, human. Never like a salesperson, a template, or an AI.

HARD RULES
1. Subject: 2-5 words, lowercase (keep proper nouns/acronyms as written), reads like a note between colleagues.
   No "!", no ":", no emojis, no "quick question", no "opportunity", no clickbait. Make it specific to them.
2. Body starts "Hi <first name>," on its own line, then a blank line.
3. First sentence: one grounded, specific observation taken from ONE verified fact (cite its id in fact_ids).
   State it plainly, as something noticed — no compliments, no adjectives like impressive/inspiring.
   If no person-level fact exists, observe something concrete about the company or the role from the facts;
   never imply you read their posts, heard their talks, or followed their career unless a fact says so.
4. Bridge: 1-2 sentences linking their context to ONE sender proof point, using its concrete detail.
   Use only the proof-point text given. No other claims about the sender. No resume dumps.
5. Ask: one sentence that states the goal plainly, then a low-friction, interest-based question.
   No calendar links, no meeting lengths, no "this week". Don't beg; don't apologise for writing.
6. Body is 65-110 words (count them). Plain text, 2-4 short paragraphs, no bullets, no links, no attachments
   mentioned, no sign-off or signature (the system adds it).
7. Follow-up 1 (< 60 words, sent 4 days later in the same thread): a genuinely different angle — another fact
   or proof point — ending with a simple question. Never "just following up", "bumping", "circling back".
8. Follow-up 2 (< 45 words, final): short and gracious; offer an easy out ("if this isn't a fit, no worries — I won't
   keep emailing") and optionally ask who else might be right to talk to.
9. Never use these words/phrases: {", ".join(sorted(set(BANNED_PHRASES)))}.
10. Never invent facts about the recipient, the company, or the sender. If unsure, leave it out."""

CRITIC_SYSTEM = """You are a strict reviewer of cold emails before they are sent. Check the draft against the
verified facts and the sender proof points ONLY.
- unsupported_claims: every statement about the recipient, their company or their work that the facts do not
  support, and every statement about the sender not present in the proof points. Paraphrase is fine; new specifics are not.
- style_issues: flattery, generic or robotic lines, salesy tone, clichés, a subject that is vague or clickbait,
  a pushy or calendar-style CTA, or anything a busy senior person would find off-putting.
Be concrete. An empty list is fine if there are no problems."""


def lint(subject: str, body: str, followups: list[str], first_name: str) -> list[str]:
    issues = []
    sw = subject.split()
    if not 2 <= len(sw) <= 5:
        issues.append(f"subject has {len(sw)} words (want 2-5)")
    if re.search(r"[!:]|\?\?", subject):
        issues.append("subject contains salesy punctuation")
    if sum(1 for w in sw if w[:1].isupper()) > max(2, len(sw) // 2 + 1) and not subject.isupper():
        issues.append("subject is Title Case — should read lowercase/natural")
    if subject.isupper() and len(subject) > 4:
        issues.append("subject is ALL CAPS")
    wc = len(re.findall(r"[A-Za-z0-9'’\-]+", body))
    if not 65 <= wc <= 110:
        issues.append(f"body is {wc} words (want 65-110)")
    if not re.match(rf"^\s*(hi|hello|hey)\s+{re.escape(first_name)}\b", body, re.I):
        issues.append(f"body must open with 'Hi {first_name},'")
    for i, fu in enumerate(followups, 1):
        n = len(re.findall(r"[A-Za-z0-9'’\-]+", fu))
        limit = 60 if i == 1 else 45
        if n == 0 or n >= limit:
            issues.append(f"follow-up {i} is {n} words (want 1-{limit - 1})")
    everything = "\n".join([subject, body, *followups])
    low = everything.lower()
    for p in BANNED_PHRASES:
        if p in low:
            issues.append(f"banned phrase: '{p}'")
    spam_scan = re.sub(r"feel free|free time|free to", "", low)
    for w in SPAM_WORDS:
        if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", spam_scan):
            issues.append(f"spam-trigger word: '{w}'")
    if m := PLACEHOLDER_RX.search(everything):
        issues.append(f"placeholder/merge-tag leak: '{m.group(0)}'")
    if URL_RX.search(body) or any(URL_RX.search(f) for f in followups):
        issues.append("contains a link (keep email 1 and follow-ups link-free)")
    if everything.count("!") > 1:
        issues.append("too many exclamation marks")
    if re.search(r"(best|regards|cheers|thanks),?\s*\n\s*tanmay", low):
        issues.append("contains a sign-off (the system adds it)")
    return issues


class Drafter:
    def __init__(self, llm: LLM, mode: str):
        self.llm = llm
        self.mode = mode

    def _context(self, lead: Lead) -> str:
        d = lead.dossier
        facts = d.verified_facts() if d else []
        fact_txt = "\n".join(f"{f.id} [{f.about}/{f.kind}{'/' + f.date if f.date else ''}] {f.claim}" for f in facts) or "(none)"
        angles = "\n".join(f"- {a.angle} (facts {', '.join(a.fact_ids) or '-'}; proof {a.proof_point or '-'})"
                           for a in (d.angles if d else [])) or "(none)"
        goal = MODE_GOALS[self.mode]
        tier = lead.score.tier if lead.score else "B"
        depth = ("TIER A — bespoke 1:1: make it unmistakably written for this one person; use the person-level fact "
                 "if there is one and reference their exact remit." if tier == "A" else
                 "TIER B — tailored to their role and company; one sharp company/role observation is enough.")
        return f"""RECIPIENT
Name: {lead.person.display_name()} (first name: {lead.person.first_name or lead.person.display_name().split()[0]})
Title: {lead.person.title}
Company: {lead.company.name}
Location: {lead.person.location or 'unknown'}
Tone: {REGION_TONE.get(lead.region, 'International reader: clear, direct, warm.')}
{depth}

RESEARCH (hook confidence: {d.hook_confidence if d else 'low'} — {d.hook_reason if d else ''})
Company summary: {d.company_summary if d else ''}
Role scope (inference): {d.role_scope if d else ''}
Verified facts:
{fact_txt}
Suggested angles:
{angles}

SENDER
Goal: {goal['goal']} ({goal['availability']})
Proof points (use exactly one in email 1; key: text):
""" + "\n".join(f"- {p.key}: {p.text}" for p in SENDER.proof_points) + f"""
Example interest-based asks (adapt, don't copy verbatim): {' | '.join(goal['soft_asks'])}"""

    def _write(self, lead: Lead, feedback: list[str] | None) -> _Draft:
        prompt = self._context(lead)
        if feedback:
            prompt += "\n\nYOUR PREVIOUS DRAFT FAILED REVIEW. Fix every issue:\n- " + "\n- ".join(feedback[:12])
        return self.llm.structured("writer", WRITER_SYSTEM, prompt, _Draft, max_tokens=3000, effort="high")

    def _critic(self, lead: Lead, dr: _Draft) -> list[str]:
        d = lead.dossier
        facts = "\n".join(f"{f.id}: {f.claim}" for f in (d.verified_facts() if d else [])) or "(none)"
        proof = "\n".join(f"- {p.text}" for p in SENDER.proof_points)
        prompt = (f"VERIFIED FACTS about {lead.person.display_name()} / {lead.company.name} "
                  f"(title: {lead.person.title}):\n{facts}\n\nSENDER PROOF POINTS:\n{proof}\n\n"
                  f"DRAFT\nSubject: {dr.subject}\n\n{dr.body}\n\nFollow-up 1:\n{dr.followup_1}\n\nFollow-up 2:\n{dr.followup_2}")
        c = self.llm.structured("writer", CRITIC_SYSTEM, prompt, _Critique, max_tokens=1500, effort="medium")
        issues = [f"unsupported claim: {x}" for x in c.unsupported_claims]
        if c.verdict.strip().lower() != "pass":
            issues += [f"style: {x}" for x in c.style_issues]
        return issues

    def _check(self, lead: Lead, dr: _Draft) -> tuple[list[str], list[str]]:
        first = lead.person.first_name or lead.person.display_name().split()[0]
        lint_issues = lint(dr.subject, dr.body, [dr.followup_1, dr.followup_2], first)
        valid = {f.id for f in (lead.dossier.verified_facts() if lead.dossier else [])}
        bad_ids = [i for i in dr.fact_ids if i not in valid]
        if bad_ids:
            lint_issues.append(f"cites unknown/unverified fact ids {bad_ids}")
        critic = [] if lint_issues else self._critic(lead, dr)
        return lint_issues, critic

    def draft(self, lead: Lead) -> EmailDraft:
        """Raises LLMError on provider failure. Returns a draft whose qa.passed decides APPROVED vs NEEDS_REVIEW."""
        dr = self._write(lead, None)
        lint_issues, critic = self._check(lead, dr)
        attempts = 1
        if lint_issues or critic:
            log.info("draft for %s failed QA (%d lint, %d critic) — rewriting once", lead.id, len(lint_issues), len(critic))
            dr = self._write(lead, lint_issues + critic)
            lint_issues, critic = self._check(lead, dr)
            attempts = 2
        subject = dr.subject.strip().rstrip(".")
        msgs = [EmailMessage(step=0, subject=subject, body=dr.body.strip() + "\n\n" + SIGN_OFF),
                EmailMessage(step=1, subject=f"Re: {subject}", body=f"Hi {lead.person.first_name or ''},\n\n".replace(" ,", ",")
                             + dr.followup_1.strip() + f"\n\n{FOLLOWUP_SIGN_OFF}"),
                EmailMessage(step=2, subject=f"Re: {subject}", body=f"Hi {lead.person.first_name or ''},\n\n".replace(" ,", ",")
                             + dr.followup_2.strip() + f"\n\n{FOLLOWUP_SIGN_OFF}")]
        return EmailDraft(messages=msgs, angle=dr.angle, fact_ids=dr.fact_ids, proof_point=dr.proof_point,
                          model=self.llm.model("writer"),
                          qa=QAReport(passed=not (lint_issues or critic), lint_issues=lint_issues,
                                      critic_issues=critic, attempts=attempts))


__all__ = ["Drafter", "lint", "LLMError", "SIGN_OFF"]
