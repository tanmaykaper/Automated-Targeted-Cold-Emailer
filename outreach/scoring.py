"""Transparent 0–100 lead scoring, derived from the repo's ICP (profile.py).

  Title / decision-maker fit   30   tier A 30 · B 24 · C 15, +3 if the function is strategy/data/research/investing
  Company / industry fit       25   target vertical 15 · company type: tier-1 brand 10, research org 8, funded startup 8, other 4
  Email deliverability         20   VERIFIED 20 · CATCH_ALL scaled by confidence · otherwise 0
  Signal strength / recency    15   funding/hiring/expansion/launch 8, other recent news 5, India +4, remote-friendly +3 (internship mode)
  Personalisation hook         10   high 10 · medium 6 · low 2

Tier A ≥ 80 → bespoke 1:1 email, priority queue · Tier B 60–79 → tailored email · Tier C < 60 → disqualified.
"""
from __future__ import annotations

from .icp import company_type, detect_vertical, function_relevance, title_tier
from .models import EmailStatus, Lead, LeadScore, ScoreComponent
from .profile import INDIA_MARKERS, VERTICALS
from .settings import Settings


def _title(lead: Lead) -> ScoreComponent:
    tier, why = title_tier(lead.person.title)
    pts = {"A": 30, "B": 24, "C": 15}.get(tier or "", 0)
    fn = function_relevance(lead.person.title, lead.person.departments)
    if fn and pts:
        pts = min(30, pts + 3)
        why += f"; relevant function ('{fn}')"
    return ScoreComponent(name="title_fit", points=pts, max_points=30, reason=why)


def _company(lead: Lead) -> ScoreComponent:
    vertical = detect_vertical(lead)
    ctype = company_type(lead)
    pts = 15 if vertical else 0
    pts += {"tier1": 10, "research": 8, "startup": 8, "corporate": 4}[ctype]
    vname = VERTICALS.get(vertical, {}).get("name", "no target vertical matched")
    return ScoreComponent(name="company_fit", points=min(pts, 25), max_points=25, reason=f"{vname}; {ctype} company")


def _deliverability(lead: Lead) -> ScoreComponent:
    v = lead.verification
    if v is None:
        return ScoreComponent(name="deliverability", points=0, max_points=20, reason="not verified")
    if v.status == EmailStatus.VERIFIED:
        return ScoreComponent(name="deliverability", points=20, max_points=20, reason=f"verified via {v.method}")
    if v.status == EmailStatus.CATCH_ALL:
        pts = round(16 * v.confidence / 100)
        return ScoreComponent(name="deliverability", points=pts, max_points=20,
                              reason=f"catch-all domain, confidence {v.confidence}")
    return ScoreComponent(name="deliverability", points=0, max_points=20, reason=v.status.value.lower())


def _signal(lead: Lead, mode: str) -> ScoreComponent:
    pts, why = 0, []
    facts = lead.dossier.verified_facts() if lead.dossier else []
    if any(f.kind in ("funding", "hiring", "expansion", "launch") for f in facts):
        pts += 8
        why.append("growth signal (funding/hiring/expansion/launch)")
    elif any(f.kind in ("news", "initiative") for f in facts):
        pts += 5
        why.append("recent news")
    loc = (lead.person.location or "").lower()
    if lead.region == "IN" or any(m in loc for m in INDIA_MARKERS):
        pts += 4
        why.append("India (priority geography)")
    blob = f"{lead.person.location} {lead.company.description} {' '.join(lead.company.keywords[:30])}".lower()
    if mode == "internship" and "remote" in blob:
        pts += 3
        why.append("remote-friendly")
    return ScoreComponent(name="signal", points=min(pts, 15), max_points=15, reason="; ".join(why) or "no signals found")


def _hook(lead: Lead) -> ScoreComponent:
    hc = lead.dossier.hook_confidence if lead.dossier else "low"
    return ScoreComponent(name="hook_quality", points={"high": 10, "medium": 6, "low": 2}[hc], max_points=10,
                          reason=(lead.dossier.hook_reason if lead.dossier else "no dossier"))


def score_lead(lead: Lead, settings: Settings) -> LeadScore:
    comps = [_title(lead), _company(lead), _deliverability(lead), _signal(lead, settings.outreach_mode), _hook(lead)]
    total = sum(c.points for c in comps)
    tier = "A" if total >= settings.tier_a_min else "B" if total >= settings.tier_b_min else "C"
    strongest = max(comps, key=lambda c: c.points / c.max_points)
    weakest = min(comps, key=lambda c: c.points / c.max_points)
    rationale = (f"{total}/100 → Tier {tier}. Strongest: {strongest.name} ({strongest.reason}). "
                 f"Weakest: {weakest.name} ({weakest.reason}).")
    return LeadScore(total=total, tier=tier, components=comps, rationale=rationale)


def prequalify(lead: Lead, settings: Settings) -> tuple[bool, str]:
    """Cheap gate before spending research tokens: title must be in-ICP and the best case must reach Tier B."""
    tier, why = title_tier(lead.person.title)
    if tier is None:
        return False, why
    best_case = _title(lead).points + _company(lead).points + _deliverability(lead).points + 15 + 10
    if best_case < settings.tier_b_min:
        return False, f"cannot reach Tier B even with perfect research (max {best_case})"
    return True, "ok"
