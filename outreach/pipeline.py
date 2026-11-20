"""Idempotent reconciler: every tick advances whatever is due, from durable state.

A dropped or late GitHub cron no longer matters: the next tick sees the same state and does
the same work. Each lead is processed in isolation — one bad record or flaky provider can't
crash the batch — and the store is checkpointed after every stage (and after every live send).
"""
from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

from pydantic import ValidationError

from .drafting import Drafter
from .enrich.domain import resolve_domain
from .enrich.finders import build_finders, pattern_candidates
from .http import Http, ProviderError
from .icp import detect_vertical, title_tier
from .ingest.csv_import import ingest_dropins
from .ingest.discovery import ApolloDiscovery, SearchDiscovery
from .ingest.legacy import migrate_legacy
from .llm import LLM, LLMError
from .mailer import Gmail, Suppression, day_bounds, followup_due, human_delay, in_send_window, week_bounds
from .models import EmailStatus, Lead, LeadState, SendRecord, VerificationResult, utcnow
from .research import Researcher
from .scoring import prequalify, score_lead
from .settings import Settings
from .store import Store
from .verify import Verifier, sendable
from .web import Search

log = logging.getLogger(__name__)

NOT_LEADS_FAULT = {"circuit_open", "budget", "unavailable", "rate_limit"}
MAX_STAGE_ATTEMPTS = 3


@dataclass
class StageStats:
    processed: int = 0
    outcomes: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0


@dataclass
class RunReport:
    stages: dict[str, StageStats] = field(default_factory=lambda: defaultdict(StageStats))
    started: float = field(default_factory=time.time)
    crashed: dict[str, str] = field(default_factory=dict)
    would_send: list[dict] = field(default_factory=list)
    sent: list[dict] = field(default_factory=list)

    def stage(self, name: str) -> StageStats:
        return self.stages[name]

    def systemic_failures(self) -> list[str]:
        """Stages where every attempt failed — the only condition that turns the workflow red."""
        out = [f"{k}: {v}" for k, v in self.crashed.items()]
        for name, st in self.stages.items():
            if st.processed >= 3 and sum(st.errors.values()) >= st.processed and not st.outcomes:
                top = st.errors.most_common(1)[0][0]
                out.append(f"{name}: all {st.processed} records failed ({top})")
        return out


class Context:
    def __init__(self, settings: Settings, store: Store | None = None, http: Http | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.s = settings
        self.store = store or Store(settings.state_dir)
        self.http = http or Http()
        self.fetch_http = Http(max_retries=1, read_timeout=12, sleep=sleep) if http is None else http
        self.search = Search(self.http, settings)
        self.llm = LLM(self.http, settings)
        self.verifier = Verifier(self.http, settings)
        self.finders = build_finders(self.http, settings)
        self.researcher = Researcher(self.http, self.fetch_http, self.search, self.llm, settings.outreach_mode)
        self.drafter = Drafter(self.llm, settings.outreach_mode)
        self.gmail = Gmail(settings)
        self.suppression = Suppression(settings.state_dir / "suppression.txt")
        self.report = RunReport()
        self.deadline = time.time() + settings.tick_budget_s
        self.followups_allowed = False
        self.sleep = sleep

    def out_of_time(self, reserve: float = 0) -> bool:
        return time.time() + reserve > self.deadline


# ── per-lead isolation ──────────────────────────────────────────────────────
def _each(ctx: Context, stage: str, leads: list[Lead], fn: Callable[[Lead], str], limit: int | None = None) -> None:
    st = ctx.report.stage(stage)
    for lead in leads[: limit if limit is not None else len(leads)]:
        if ctx.out_of_time(reserve=120):
            st.notes.append("stopped early: tick time budget reached")
            break
        st.processed += 1
        try:
            st.outcomes[fn(lead)] += 1
        except (ProviderError, LLMError) as e:
            _fail(ctx, stage, lead, e.category, str(e))
        except ValidationError as e:
            _fail(ctx, stage, lead, "data", str(e))
        except Exception as e:  # a bug must not kill the batch; it is surfaced in the summary
            log.exception("unexpected error in %s for %s", stage, lead.id)
            _fail(ctx, stage, lead, "bug", f"{type(e).__name__}: {e}")
        finally:
            ctx.store.save(lead)


def _fail(ctx: Context, stage: str, lead: Lead, category: str, msg: str) -> None:
    ctx.report.stage(stage).errors[category] += 1
    if category in NOT_LEADS_FAULT:
        return
    attempts = lead.record_error(stage, category, msg)
    if attempts >= MAX_STAGE_ATTEMPTS:
        lead.set_state(LeadState.NEEDS_REVIEW, f"{stage} failed {attempts}x ({category}): {msg[:160]}")


# ── stages ──────────────────────────────────────────────────────────────────
def stage_migrate(ctx: Context) -> None:
    res = migrate_legacy(ctx.store, ctx.s.state_dir / "legacy", ctx.s.outreach_mode)
    if "skipped" not in res:
        ctx.report.stage("migrate").outcomes.update({k: v for k, v in res.items() if isinstance(v, int)})


def stage_sync_replies(ctx: Context) -> None:
    st = ctx.report.stage("sync_replies")
    if not ctx.s.can_send_mail:
        st.notes.append("no Gmail credentials — reply detection off, follow-ups disabled")
        return
    recent = [l for l in ctx.store.by_state(LeadState.SENT)
              if l.last_send and not l.last_send.legacy and utcnow() - l.last_send.sent_at < timedelta(days=30)]
    try:
        for lead in recent:
            st.processed += 1
            result = ctx.gmail.check_lead(lead)
            st.outcomes[result] += 1
            if result == "replied":
                lead.set_state(LeadState.REPLIED, "reply detected via IMAP")
            elif result == "opted_out":
                lead.set_state(LeadState.OPTED_OUT, "opt-out language in reply")
                ctx.suppression.add(lead.email, "opt-out")
            elif result == "bounced":
                lead.set_state(LeadState.BOUNCED, "bounce notification via IMAP")
                ctx.suppression.add(lead.email, "bounce")
            if result != "none":
                ctx.store.event(result, lead.id, email=lead.email)
                ctx.store.save(lead)
        ctx.followups_allowed = True
    except (RuntimeError, OSError) as e:
        st.notes.append(f"IMAP failed ({e}) — follow-ups disabled this tick")
        ctx.followups_allowed = False


def stage_ingest(ctx: Context) -> None:
    res = ingest_dropins(ctx.store, ctx.s.inputs_dir, (ctx.s.state_dir / "apollo_imports",), ctx.s.outreach_mode)
    st = ctx.report.stage("ingest")
    for r in res:
        st.processed += r.get("rows", 0)
        st.outcomes["created"] += r.get("created", 0)
        st.outcomes["merged"] += r.get("merged", 0)
        st.outcomes["skipped_rows"] += r.get("skipped", 0)
        if r.get("error"):
            st.errors["bad_file"] += 1
            st.notes.append(f"{r['file']}: {r['error']}")
        if r.get("skipped_duplicate_of"):
            st.notes.append(f"{r['file']}: identical to {r['skipped_duplicate_of']} (skipped)")


def stage_discover(ctx: Context) -> None:
    st = ctx.report.stage("discover")
    week = utcnow().strftime("%G-W%V")
    done = ctx.store.meta.get("discovered_by_week", {}).get(week, 0)
    remaining = ctx.s.weekly_source_target - done
    backlog = len(ctx.store.by_state(LeadState.DISCOVERED, LeadState.ENRICHED, LeadState.VERIFIED))
    if remaining <= 0:
        st.notes.append(f"weekly target reached ({done}/{ctx.s.weekly_source_target})")
        return
    if backlog >= ctx.s.weekly_source_target:
        st.notes.append(f"backlog of {backlog} unprocessed leads — skipping discovery")
        return
    created = 0
    apollo = ApolloDiscovery(ctx.http, ctx.s)
    if apollo.enabled():
        res = apollo.run(ctx.store, remaining)
        created += res["created"]
        st.outcomes["apollo_api"] += res["created"]
    if created < remaining and (ctx.s.keys.serper or ctx.s.keys.tavily):
        res = SearchDiscovery(ctx.search, ctx.s).run(ctx.store, remaining - created)
        created += res["created"]
        st.outcomes["search"] += res["created"]
        st.processed += res["results"]
    elif created < remaining:
        st.notes.append("no SERPER_API_KEY/TAVILY_API_KEY — web discovery off (DuckDuckGo is used for research only)")
    by_week = dict(ctx.store.meta.get("discovered_by_week", {}))
    by_week[week] = done + created
    ctx.store.set_meta("discovered_by_week", by_week)


def _verify_lead(ctx: Context, lead: Lead) -> str:
    tier, why = title_tier(lead.person.title)
    if tier is None:
        lead.set_state(LeadState.DISQUALIFIED, why)
        return "disqualified_title"
    if any(ctx.suppression.blocked(c.email) for c in lead.candidates):
        lead.set_state(LeadState.DISQUALIFIED, "on suppression list")
        return "suppressed"
    if lead.state == LeadState.DISCOVERED:
        domain, conf, src = resolve_domain(lead, ctx.search, lambda d: ctx.verifier.dns.mx(d)[0] in ("ok", "implicit"))
        if domain:
            lead.company.domain, lead.company.domain_confidence, lead.company.domain_source = domain, conf, src
        lead.vertical = lead.vertical or detect_vertical(lead)
        lead.set_state(LeadState.ENRICHED, f"domain: {domain or 'unresolved'} ({src})")
        ctx.store.save(lead)  # checkpointed sub-step

    domain = lead.company.domain
    best: VerificationResult | None = None
    checked: set[str] = {c.email for c in lead.candidates if c.email in lead.legacy.get("checked_emails", [])}

    def try_candidates(cands) -> VerificationResult | None:
        nonlocal best
        for cand in cands:
            if cand.email in checked or ctx.store.email_known(cand.email, exclude_id=lead.id):
                continue  # already checked, or the mailbox belongs to another (possibly contacted) lead
            checked.add(cand.email)
            res = ctx.verifier.verify(cand)
            if sendable(res, ctx.s):
                return res
            if res.status != EmailStatus.INVALID and (best is None or res.confidence > best.confidence):
                best = res
        return None

    hit = try_candidates(list(lead.candidates))
    if not hit and domain:
        for finder in ctx.finders:
            if hit or finder.name in lead.tried_finders or not finder.enabled():
                continue
            lead.tried_finders.append(finder.name)
            new = [c for c in finder.find(lead, domain) if c.email.lower() not in {x.email.lower() for x in lead.candidates}]
            lead.candidates.extend(new)
            hit = try_candidates(new)
    if not hit and domain and (ctx.verifier.apis.available() or (ctx.s.smtp_probe_enabled and not ctx.verifier.smtp.disabled_reason)):
        pats = [c for c in pattern_candidates(lead, domain, limit=3) if c.email not in {x.email for x in lead.candidates}]
        lead.candidates.extend(pats)
        hit = try_candidates(pats)
    lead.legacy["checked_emails"] = sorted(checked)

    if hit:
        lead.email, lead.verification = hit.email, hit
        lead.set_state(LeadState.VERIFIED, f"{hit.status.value} ({hit.method}, confidence {hit.confidence})")
        return hit.status.value.lower()
    lead.verification = best
    if not lead.candidates:
        lead.set_state(LeadState.NEEDS_ENRICHMENT, "no email found" + ("" if domain else "; company domain unresolved"))
        return "no_email"
    if best is None:
        lead.set_state(LeadState.NEEDS_ENRICHMENT, "all candidate emails INVALID")
        return "invalid"
    lead.set_state(LeadState.NEEDS_ENRICHMENT, f"{best.status.value}: {best.reason}")
    return best.status.value.lower()


def stage_verify(ctx: Context) -> None:
    leads = ctx.store.by_state(LeadState.DISCOVERED, LeadState.ENRICHED)
    _each(ctx, "verify", leads, lambda l: _verify_lead(ctx, l))


def _research_lead(ctx: Context, lead: Lead) -> str:
    ok, why = prequalify(lead, ctx.s)
    if not ok:
        lead.set_state(LeadState.DISQUALIFIED, why)
        return "prequalify_failed"
    lead.dossier = ctx.researcher.build(lead)
    lead.set_state(LeadState.RESEARCHED, f"hook {lead.dossier.hook_confidence}; {len(lead.dossier.verified_facts())} verified facts")
    return f"hook_{lead.dossier.hook_confidence}"


def stage_research(ctx: Context) -> None:
    leads = ctx.store.by_state(LeadState.VERIFIED)
    leads.sort(key=lambda l: -(l.verification.confidence if l.verification else 0))
    _each(ctx, "research", leads, lambda l: _research_lead(ctx, l), limit=ctx.s.research_per_tick)


def _score(ctx: Context, lead: Lead) -> str:
    lead.score = score_lead(lead, ctx.s)
    if lead.score.tier == "C":
        lead.set_state(LeadState.DISQUALIFIED, f"Tier C ({lead.score.total}): {lead.score.rationale}")
        return "tier_c"
    lead.set_state(LeadState.SCORED, lead.score.rationale)
    return f"tier_{lead.score.tier.lower()}"


def stage_score(ctx: Context) -> None:
    _each(ctx, "score", ctx.store.by_state(LeadState.RESEARCHED), lambda l: _score(ctx, l))


def _draft(ctx: Context, lead: Lead) -> str:
    lead.draft = ctx.drafter.draft(lead)
    if lead.draft.qa and lead.draft.qa.passed:
        lead.set_state(LeadState.APPROVED if ctx.s.auto_approve else LeadState.DRAFTED, "QA passed")
        return "approved" if ctx.s.auto_approve else "drafted"
    issues = (lead.draft.qa.lint_issues + lead.draft.qa.critic_issues) if lead.draft.qa else []
    lead.set_state(LeadState.NEEDS_REVIEW, "QA failed after rewrite: " + "; ".join(issues[:4]))
    return "needs_review"


def stage_draft(ctx: Context) -> None:
    st = ctx.report.stage("draft")
    if not ctx.llm.available:
        st.notes.append("no LLM provider configured — leads wait at SCORED")
        return
    leads = ctx.store.by_state(LeadState.SCORED)
    leads.sort(key=lambda l: (l.score.tier if l.score else "Z", -(l.score.total if l.score else 0)))
    _each(ctx, "draft", leads, lambda l: _draft(ctx, l), limit=ctx.s.drafts_per_tick)


def stage_send(ctx: Context) -> None:
    st = ctx.report.stage("send")
    now = utcnow()
    week_sent = ctx.store.sends_between(*week_bounds(now))
    day_sent = ctx.store.sends_between(*day_bounds(now))
    budget = min(ctx.s.per_tick_send_cap, ctx.s.daily_send_cap - day_sent, ctx.s.weekly_send_cap - week_sent)
    queue: list[tuple[Lead, int]] = []
    if ctx.followups_allowed:
        queue += [(l, s) for l in ctx.store.by_state(LeadState.SENT) if (s := followup_due(l, ctx.s, now)) is not None]
    queue += [(l, 0) for l in ctx.store.by_state(LeadState.APPROVED)]
    queue.sort(key=lambda x: (x[1] == 0, x[0].score.tier if x[0].score else "Z", -(x[0].score.total if x[0].score else 0)))
    st.notes.append(f"caps: today {day_sent}/{ctx.s.daily_send_cap}, week {week_sent}/{ctx.s.weekly_send_cap}, "
                    f"this tick ≤{max(budget, 0)}; {len(queue)} queued; dry_run={ctx.s.dry_run}")
    if budget <= 0:
        st.outcomes["cap_reached"] += len(queue)
        return
    if not ctx.s.dry_run and not ctx.s.can_send_mail:
        st.notes.append("live mode but SENDER_EMAIL/GMAIL_APP_PASSWORD missing — nothing sent")
        return
    sent_now = 0
    for lead, step in queue:
        if sent_now >= budget or ctx.out_of_time(reserve=ctx.s.max_send_delay_s + 60):
            break
        st.processed += 1
        if ctx.suppression.blocked(lead.email):
            lead.set_state(LeadState.OPTED_OUT, "suppressed")
            ctx.store.save(lead)
            st.outcomes["suppressed"] += 1
            continue
        if not sendable(lead.verification, ctx.s):
            lead.set_state(LeadState.NEEDS_ENRICHMENT, "verification no longer sendable")
            ctx.store.save(lead)
            st.outcomes["blocked_unverified"] += 1
            continue
        if not ctx.s.force_window and not in_send_window(lead, ctx.s, now):
            st.outcomes["outside_window"] += 1
            continue
        if lead.verification and now - lead.verification.verified_at > timedelta(days=ctx.s.reverify_after_days):
            fresh = ctx.verifier.verify(next((c for c in lead.candidates if c.email == lead.email),
                                             lead.candidates[0]))
            lead.verification = fresh
            if not sendable(fresh, ctx.s):
                lead.set_state(LeadState.NEEDS_ENRICHMENT, f"re-verification: {fresh.status.value}")
                ctx.store.save(lead)
                st.outcomes["failed_reverify"] += 1
                continue
        msg = lead.draft.step(step)
        info = {"lead": lead.id, "to": lead.email, "step": step, "subject": msg.subject,
                "tier": lead.score.tier if lead.score else "", "words": msg.word_count}
        if ctx.s.dry_run:
            ctx.report.would_send.append(info)
            ctx.store.event("dry_run_send", lead.id, step=step, subject=msg.subject)
            st.outcomes["dry_run"] += 1
            sent_now += 1
            continue
        try:
            since = (lead.draft.created_at - timedelta(days=1))
            if ctx.gmail.already_sent(lead.email, msg.subject, step, since):
                st.outcomes["already_in_sent_folder"] += 1
                lead.sends.append(SendRecord(step=step, sent_at=now, subject=msg.subject, to=lead.email))
                lead.set_state(LeadState.SENT, "reconciled from Gmail Sent folder")
                ctx.store.save(lead)
                ctx.store.checkpoint()
                continue
            if sent_now:
                human_delay(ctx.s, ctx.sleep)
            rec = ctx.gmail.send(lead, step)
        except Exception as e:  # SMTP auth/network: stop sending this tick, keep lead approved
            st.errors["smtp"] += 1
            st.notes.append(f"send failed ({type(e).__name__}); stopping sends this tick")
            log.error("send failed for %s: %s", lead.id, e)
            break
        lead.sends.append(rec)
        lead.set_state(LeadState.SENT, f"step {step} sent")
        ctx.store.save(lead)
        ctx.store.event("sent", lead.id, step=step, to=lead.email, subject=msg.subject, message_id=rec.message_id)
        ctx.store.checkpoint()  # irreversible action → persist immediately
        ctx.report.sent.append(info)
        st.outcomes[f"sent_step{step}"] += 1
        sent_now += 1


STAGES: dict[str, Callable[[Context], None]] = {
    "migrate": stage_migrate,
    "sync_replies": stage_sync_replies,
    "ingest": stage_ingest,
    "discover": stage_discover,
    "verify": stage_verify,
    "research": stage_research,
    "score": stage_score,
    "draft": stage_draft,
    "send": stage_send,
}


def run(ctx: Context, stages: list[str] | None = None) -> RunReport:
    for name in stages or list(STAGES):
        if name not in STAGES:
            log.warning("unknown stage %s", name)
            continue
        if ctx.out_of_time(reserve=60):
            ctx.report.stage(name).notes.append("skipped: tick time budget reached")
            continue
        t0 = time.time()
        log.info("── stage %s ──", name)
        try:
            STAGES[name](ctx)
        except Exception as e:  # stage-level crash: record, checkpoint, continue with the next stage
            log.exception("stage %s crashed", name)
            ctx.report.crashed[name] = f"{type(e).__name__}: {e}"
        finally:
            ctx.report.stage(name).seconds += time.time() - t0
            ctx.store.checkpoint()
    ctx.gmail.close()
    return ctx.report
