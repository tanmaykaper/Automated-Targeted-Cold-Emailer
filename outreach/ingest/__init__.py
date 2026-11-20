"""Lead ingestion: every source produces `Lead` objects and funnels through `upsert()`."""
from __future__ import annotations

from ..models import EmailCandidate, Lead, LeadState, identity_keys
from ..store import Store

RETRYABLE_ON_NEW_DATA = {LeadState.DISCOVERED, LeadState.NEEDS_ENRICHMENT, LeadState.ENRICHED}
CONTACTED = {LeadState.SENT, LeadState.REPLIED, LeadState.BOUNCED, LeadState.OPTED_OUT}


def _fill(dst, src) -> bool:
    changed = False
    for name in type(src).model_fields:
        new, old = getattr(src, name), getattr(dst, name)
        if new and not old:
            setattr(dst, name, new)
            changed = True
    return changed


def upsert(store: Store, incoming: Lead) -> tuple[Lead, bool]:
    """Insert a new lead, or merge into the existing person. Returns (lead, created)."""
    keys = identity_keys(incoming.person, incoming.company, incoming.email)
    keys += [f"em:{c.email.lower()}" for c in incoming.candidates]
    if incoming.legacy.get("apollo_id"):
        keys.append(f"ap:{incoming.legacy['apollo_id']}")
    existing = store.find_existing(keys)
    if existing is None:
        incoming.id = store.new_id(keys)
        store.save(incoming)
        return incoming, True

    changed = _fill(existing.person, incoming.person) | _fill(existing.company, incoming.company)
    known = {c.email.lower() for c in existing.candidates}
    new_cands: list[EmailCandidate] = [c for c in incoming.candidates if c.email.lower() not in known]
    if new_cands:
        existing.candidates.extend(new_cands)
        changed = True
        if existing.state in RETRYABLE_ON_NEW_DATA:
            existing.set_state(LeadState.DISCOVERED, f"new email candidate(s) from {incoming.source}")
    # Contact history always wins: if the incoming record says this person was already emailed,
    # the merged lead must be terminal-for-outreach too (v1 emailed 20 people twice this way).
    if incoming.sends:
        have = {(x.step, x.sent_at, x.to) for x in existing.sends}
        existing.sends.extend(x for x in incoming.sends if (x.step, x.sent_at, x.to) not in have)
        changed = True
    if incoming.state in CONTACTED and existing.state not in CONTACTED:
        existing.email = existing.email or incoming.email
        existing.legacy.update(incoming.legacy)
        existing.set_state(incoming.state, incoming.state_reason)
        changed = True
    if changed:
        store.save(existing)
    return existing, False
