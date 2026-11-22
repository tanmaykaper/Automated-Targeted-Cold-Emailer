"""Prospect research dossiers with a zero-hallucination contract.

1. Gather sources: company website (home/about), company news & signals search, person search.
2. Extract facts (fast model). Each fact must cite a source id and quote verbatim evidence.
3. Verify programmatically: the quote must actually appear in the fetched source text, and
   person facts must come from a source that names the person. Unverified facts are dropped.
4. Synthesize (writer model): summary, role scope, 1–2 outreach angles citing fact ids only.
5. hook_confidence is computed from the verified facts, not taken from the model.

With no LLM configured, a deterministic extractor turns relevant search snippets into
sourced facts so scoring still works (drafting will wait for an LLM).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from .http import Http, ProviderError
from .llm import LLM, LLMError
from .models import Fact, Lead, OutreachAngle, ResearchDossier, norm_company
from .profile import MODE_GOALS, SENDER
from .web import Search, SearchResult, fetch_text

log = logging.getLogger(__name__)


# Pages that only restate a profile (name/title/contact) — never a personalisation hook.
NON_HOOK_HOSTS = ("rocketreach.co", "zoominfo.com", "signalhire.com", "contactout.com", "equilar.com",
                  "theofficialboard.com", "lusha.com", "apollo.io", "crunchbase.com", "facebook.com",
                  "instagram.com", "x.com", "twitter.com", "glassdoor", "ambitionbox", "naukri.com", "indeed.com",
                  "linkedin.com/in/", "linkedin.com/company/", "linkedin.com/jobs", "wikipedia.org")


def _non_hook(url: str) -> bool:
    u = url.lower()
    return any(h in u for h in NON_HOOK_HOSTS)


@dataclass
class Source:
    id: str
    url: str
    title: str
    text: str
    origin: str  # site | news | person | signal


class _Pick(BaseModel):
    sentence_id: str = Field(description="Id of the chosen sentence, e.g. S2.3")
    about: Literal["company", "person", "industry"]
    kind: Literal["business", "news", "funding", "hiring", "launch", "expansion", "initiative",
                  "talk", "article", "podcast", "post", "role", "other"]
    claim: str = Field(description="<= 25 words restating ONLY what that sentence says")
    date: str = Field(default="", description="Date if the sentence or source states one, else empty")


class _Picks(BaseModel):
    picks: list[_Pick]


class _XAngle(BaseModel):
    angle: str = Field(description="Why this person would care about the sender's ask right now, in 1-2 sentences")
    fact_ids: list[str]
    proof_point: str = Field(description="Key of the single most relevant sender proof point")


class _Synthesis(BaseModel):
    company_summary: str
    role_scope: str
    angles: list[_XAngle]


EXTRACT_SYSTEM = """You select facts for B2B outreach research from numbered sentences.
Each sentence has an id like S2.3 (source 2, sentence 3). Pick the 3-8 sentences most useful for writing a specific,
relevant first line of a cold email to the prospect: launches, funding, hiring, expansion, new practices or initiatives,
research or reports, talks, podcasts, articles or posts BY the prospect, and concrete descriptions of what the company does.
Skip navigation text, cookie banners, generic slogans and contact details.
For each pick: about = "person" only if the sentence or its source is explicitly by/about the prospect; claim = a short
restatement of ONLY what that sentence says (no outside knowledge, no embellishment).
If nothing is useful, return an empty list."""

SYNTH_SYSTEM = """You are a sharp research analyst preparing a cold-outreach brief for a student job-seeker.
Use only the verified facts given (cite their ids). Never invent achievements, posts, talks, or news.
- company_summary: 1-2 sentences on what the company does, from the facts.
- role_scope: 1 sentence on what someone with this title at this company likely owns. Phrase as inference ("likely…").
- angles: 1-2 concrete reasons THIS person might care about the sender's ask now, each tied to fact ids and to
  the single best-matching sender proof point key. If facts are only generic, give one honest role/company-level angle."""


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = s.translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "}))
    s = re.sub(r"[^a-z0-9%$.,'\- ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def evidence_in_source(evidence: str, source_text: str) -> bool:
    ev, src = _norm(evidence).strip(" .,"), _norm(source_text)
    if len(ev) < 12:
        return False
    if ev in src:
        return True
    words = ev.split()
    if len(words) < 6:
        return False
    shingles = [" ".join(words[i:i + 5]) for i in range(len(words) - 4)]
    hits = sum(1 for sh in shingles if sh in src)
    return hits / len(shingles) >= 0.8


def _mentions_person(lead: Lead, text: str) -> bool:
    t = _norm(text)
    last = _norm(lead.person.last_name)
    first = _norm(lead.person.first_name)
    return bool(last and len(last) >= 2 and last in t and first and first in t)


class Researcher:
    def __init__(self, http: Http, fetch_http: Http, search: Search, llm: LLM, mode: str):
        self.http = http
        self.fetch_http = fetch_http
        self.search = search
        self.llm = llm
        self.mode = mode

    # ── 1. sources ──────────────────────────────────────────────────────────
    def gather(self, lead: Lead) -> tuple[list[Source], list[str]]:
        sources: list[Source] = []
        queries: list[str] = []
        seen: set[str] = set()

        def add(url, title, text, origin):
            if not url or url in seen or not text.strip():
                return
            seen.add(url)
            sources.append(Source(f"S{len(sources) + 1}", url, title[:200], text, origin))

        domain = lead.company.domain
        if domain:
            for path in ("", "/about", "/about-us"):
                try:
                    title, text = fetch_text(self.fetch_http, f"https://{domain}{path}", limit=3500)
                    add(f"https://{domain}{path or '/'}", title, text, "site")
                except ProviderError as e:
                    log.debug("fetch %s%s failed: %s", domain, path, e.category)
                if len([s for s in sources if s.origin == "site"]) >= 2:
                    break

        company = lead.company.name
        name = lead.person.display_name()
        plan: list[tuple[str, str, bool]] = [
            (f'"{company}" launches OR announces OR expands', "signal", False),
            (f'"{name}" "{company}"', "person", False),
            (f'"{name}" interview OR podcast OR article OR report', "person", False),
        ]
        if self.search.s.keys.serper:
            plan.insert(0, (f'"{company}"', "news", True))
        for q, origin, news in plan:
            try:
                results = self.search.search(q, num=6, news=news)
                queries.append(q)
            except ProviderError as e:
                log.info("research search failed (%s): %s", e.category, q)
                continue
            for r in results:
                if _non_hook(r.url):
                    continue
                text = f"{r.title}. {r.snippet}" + (f" ({r.date})" if r.date else "")
                if origin == "person" and not _mentions_person(lead, text):
                    continue
                if origin in ("signal", "news") and not self._mentions_company(company, text):
                    continue
                add(r.url, r.title, text, origin)
        return sources, queries

    @staticmethod
    def _mentions_company(company: str, text: str) -> bool:
        core = [t for t in norm_company(company).split() if len(t) >= 3 and t not in ("and", "the", "group")]
        t = _norm(text)
        return bool(core) and core[0] in t

    # ── 2-3. extraction + verification ──────────────────────────────────────
    @staticmethod
    def sentences(src: Source, limit: int = 18) -> list[tuple[str, str]]:
        body = src.text
        if src.origin != "site" and body.startswith(src.title):
            body = body[len(src.title):].lstrip(". ")
        parts = re.split(r"(?<=[.!?])\s+|\n+", body)
        out, n = [], 0
        if src.origin != "site" and len(src.title) >= 20:
            out.append((f"{src.id}.0", src.title.strip()))  # titles of articles/reports are often the best hook
        for p in parts:
            p = re.sub(r"\s+", " ", p).strip(" -|·")
            if 25 <= len(p) <= 400 and not re.search(r"cookie|subscribe|sign in|log in|©|all rights reserved", p, re.I):
                n += 1
                out.append((f"{src.id}.{n}", p))
            if n >= limit:
                break
        return out

    @staticmethod
    def _faithful(claim: str, evidence: str) -> bool:
        """The paraphrase may not add content words the sentence doesn't contain."""
        stop = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "at", "by", "is", "are", "was",
                "its", "their", "his", "her", "has", "have", "that", "this", "as", "from", "it", "be", "which", "who"}
        cw = [w for w in re.findall(r"[a-z0-9%]+", claim.lower()) if w not in stop and len(w) > 2]
        ev = set(re.findall(r"[a-z0-9%]+", evidence.lower()))
        if not cw:
            return False
        hits = sum(1 for w in cw if w in ev or any(e.startswith(w[:5]) for e in ev if len(w) >= 5))
        return hits / len(cw) >= 0.6

    def extract(self, lead: Lead, sources: list[Source]) -> tuple[list[Fact], str]:
        if not sources:
            return [], ""
        if self.llm.available:
            index: dict[str, tuple[str, Source]] = {}
            blocks = []
            for src in sources:
                sents = self.sentences(src)
                if not sents:
                    continue
                for sid, text in sents:
                    index[sid] = (text, src)
                blocks.append(f"[{src.id}] ({src.origin}) {src.url}\n" + "\n".join(f"{sid}: {t}" for sid, t in sents))
            prompt = (f"Prospect: {lead.person.display_name()} — {lead.person.title} at {lead.company.name}\n\n"
                      "SENTENCES:\n" + "\n\n".join(blocks))
            try:
                out = self.llm.structured("fast", EXTRACT_SYSTEM, prompt, _Picks, max_tokens=2500)
                facts, seen = [], set()
                for pick in out.picks[:10]:
                    sid = pick.sentence_id.strip().strip("[]")
                    if sid not in index or sid in seen:
                        continue
                    seen.add(sid)
                    evidence, src = index[sid]
                    ok = self._faithful(pick.claim, evidence)
                    about = pick.about
                    if about == "person" and not _mentions_person(lead, src.title + " " + src.text):
                        about = "company"  # never attribute to the person without a source naming them
                    facts.append(Fact(id=f"F{len(facts) + 1}", claim=pick.claim if ok else evidence, about=about,
                                      kind=pick.kind, source_url=src.url, evidence=evidence, date=pick.date,
                                      verified=True))
                return facts, self.llm.model("fast")
            except LLMError as e:
                log.info("fact extraction via LLM failed (%s); using deterministic extractor", e.category)
        return self._heuristic_facts(lead, sources), "heuristic"

    def _heuristic_facts(self, lead: Lead, sources: list[Source]) -> list[Fact]:
        facts: list[Fact] = []
        kind_rx = (("funding", r"\braise[sd]?\b|funding|series [a-e]|investment from"),
                   ("hiring", r"\bhiring\b|\bjoins?\b|appoint"),
                   ("launch", r"launch|unveil|introduc"),
                   ("expansion", r"expan|new office|opens?\b"),
                   ("podcast", r"podcast"), ("talk", r"keynote|panel|speaks?|webinar"),
                   ("article", r"\bwrote\b|article|op-ed|column"))
        for s in sources:
            body = s.text[len(s.title) + 2:] if s.origin != "site" and s.text.startswith(s.title) else s.text
            sentences = re.split(r"(?<=[.!?])\s+", body)
            for sent in sentences:
                sent = sent.strip()
                if not (40 <= len(sent) <= 320):
                    continue
                about = "person" if s.origin == "person" and _mentions_person(lead, sent) else "company"
                if about == "company" and not self._mentions_company(lead.company.name, sent) and s.origin != "site":
                    continue
                kind = next((k for k, rx in kind_rx if re.search(rx, sent, re.I)), "")
                if not kind:
                    kind = "business" if s.origin == "site" else ("role" if about == "person" else "news")
                facts.append(Fact(id=f"F{len(facts) + 1}", claim=sent, about=about, kind=kind, source_url=s.url,
                                  evidence=sent, verified=True))
                break  # one fact per source keeps the dossier focused
            if len(facts) >= 8:
                break
        return facts

    # ── 4-5. synthesis + hook confidence ────────────────────────────────────
    @staticmethod
    def hook_confidence(facts: list[Fact]) -> tuple[str, str]:
        v = [f for f in facts if f.verified]
        person_hooks = [f for f in v if f.about == "person" and f.kind in
                        ("talk", "article", "podcast", "post", "initiative", "launch", "news", "expansion", "hiring")]
        if person_hooks:
            return "high", f"person-level hook found ({person_hooks[0].kind}: {person_hooks[0].id})"
        signals = [f for f in v if f.about == "company" and f.kind in
                   ("news", "funding", "hiring", "launch", "expansion", "initiative")]
        if signals:
            return "medium", f"company-level signal ({signals[0].kind}: {signals[0].id}); no person-specific hook"
        return "low", "no specific public hook found — fall back to a role/company-level angle"

    def synthesize(self, lead: Lead, facts: list[Fact]) -> tuple[str, str, list[OutreachAngle]]:
        verified = [f for f in facts if f.verified]
        proof_keys = ", ".join(p.key for p in SENDER.proof_points)
        if self.llm.available:
            fact_txt = "\n".join(f"{f.id} [{f.about}/{f.kind}] {f.claim} (source: {f.source_url})" for f in verified) or "(none)"
            prompt = (f"PROSPECT: {lead.person.display_name()}, {lead.person.title} at {lead.company.name} "
                      f"({lead.person.location or 'location unknown'})\nCompany industry: {lead.company.industry or 'unknown'}\n\n"
                      f"VERIFIED FACTS:\n{fact_txt}\n\nSENDER ASK: {MODE_GOALS[self.mode]['goal']}\n"
                      f"SENDER PROOF POINTS (key: text):\n" +
                      "\n".join(f"- {p.key}: {p.text}" for p in SENDER.proof_points) +
                      f"\n\nValid proof_point keys: {proof_keys}")
            try:
                out = self.llm.structured("fast", SYNTH_SYSTEM, prompt, _Synthesis, max_tokens=2000)
                valid_ids = {f.id for f in verified}
                keys = {p.key for p in SENDER.proof_points}
                angles = [OutreachAngle(angle=a.angle, fact_ids=[i for i in a.fact_ids if i in valid_ids],
                                        proof_point=a.proof_point if a.proof_point in keys else "")
                          for a in out.angles[:2]]
                return out.company_summary, out.role_scope, angles
            except LLMError as e:
                log.info("synthesis via LLM failed (%s); using deterministic summary", e.category)
        site = next((f for f in verified if f.kind == "business"), None)
        summary = site.claim if site else (lead.company.description[:240] or f"{lead.company.name} ({lead.company.industry or 'industry unknown'})")
        role = f"As {lead.person.title}, likely owns or influences team priorities and junior hiring."
        angle = OutreachAngle(angle=f"In a role like {lead.person.title} at {lead.company.name}, analyst capacity that "
                                    f"turns data into decisions quickly is usually welcome.", fact_ids=[f.id for f in verified[:1]])
        return summary, role, [angle]

    def build(self, lead: Lead) -> ResearchDossier:
        sources, queries = self.gather(lead)
        facts, model = self.extract(lead, sources)
        summary, role, angles = self.synthesize(lead, facts)
        hook, why = self.hook_confidence(facts)
        return ResearchDossier(company_summary=summary, role_scope=role, facts=facts, angles=angles,
                               hook_confidence=hook, hook_reason=why, sources=[s.url for s in sources],
                               queries=queries, model=model if not self.llm.available else
                               f"{self.llm.model('fast')}+{self.llm.model('writer')}")
