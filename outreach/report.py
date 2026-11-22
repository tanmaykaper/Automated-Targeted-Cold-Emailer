"""Run summaries (GitHub Job Summary) and human-readable exports."""
from __future__ import annotations

import csv
import io
import os
from collections import Counter
from pathlib import Path

from .models import EmailStatus, Lead, LeadState
from .pipeline import Context, RunReport
from .store import _atomic_write

VIEW_COLUMNS = ["id", "state", "state_reason", "tier", "score", "name", "title", "company", "domain", "email",
                "email_status", "email_confidence", "hook", "subject", "body", "sends", "last_sent", "source", "updated_at"]


def _row(l: Lead) -> dict:
    v = l.verification
    m = l.draft.step(0) if l.draft else None
    return {
        "id": l.id, "state": l.state.value, "state_reason": l.state_reason[:200],
        "tier": l.score.tier if l.score else "", "score": l.score.total if l.score else "",
        "name": l.person.display_name(), "title": l.person.title, "company": l.company.name,
        "domain": l.company.domain, "email": l.email or (v.email if v else ""),
        "email_status": v.status.value if v else "", "email_confidence": v.confidence if v else "",
        "hook": l.dossier.hook_confidence if l.dossier else "", "subject": m.subject if m else "",
        "body": m.body if m else "", "sends": len(l.sends),
        "last_sent": l.last_send.sent_at.isoformat() if l.last_send else "", "source": l.source,
        "updated_at": l.updated_at.isoformat(),
    }


def _csv(rows: list[dict], cols: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def export_views(ctx: Context) -> None:
    leads = sorted(ctx.store.all(), key=lambda l: (l.state.value, l.id))
    out = ctx.s.state_dir
    _atomic_write(out / "pipeline_view.csv", _csv([_row(l) for l in leads], VIEW_COLUMNS))
    needs = [l for l in leads if l.state == LeadState.NEEDS_ENRICHMENT]
    rows = [{"id": l.id, "name": l.person.display_name(), "title": l.person.title, "company": l.company.name,
             "domain": l.company.domain, "linkedin_url": l.person.linkedin_url, "reason": l.state_reason,
             "best_status": l.verification.status.value if l.verification else "",
             "candidates": " ".join(f"{c.email}({c.source}:{c.provider_status})" for c in l.candidates),
             "finders_tried": ",".join(l.tried_finders)} for l in needs]
    _atomic_write(out / "needs_enrichment.csv",
                  _csv(rows, ["id", "name", "title", "company", "domain", "linkedin_url", "reason", "best_status",
                              "candidates", "finders_tried"]))
    _atomic_write(out / "outbox.md", outbox_markdown([l for l in leads if l.state in (LeadState.APPROVED, LeadState.NEEDS_REVIEW, LeadState.DRAFTED)]))


def dossier_markdown(l: Lead) -> str:
    d, s, v = l.dossier, l.score, l.verification
    lines = [f"### {l.person.display_name()} — {l.person.title}, {l.company.name}",
             f"`{l.id}` · state **{l.state.value}** · email `{l.email or (v.email if v else '-')}` "
             f"**{v.status.value if v else '-'}** ({v.confidence if v else 0}, via {v.method if v else '-'})"]
    if s:
        lines.append(f"**Score {s.total}/100 → Tier {s.tier}** — " + " · ".join(
            f"{c.name} {c.points}/{c.max_points}" for c in s.components))
        lines.append(f"> {s.rationale}")
    if d:
        lines += ["", f"**Hook confidence:** {d.hook_confidence} — {d.hook_reason}",
                  f"**Company:** {d.company_summary}", f"**Role scope:** {d.role_scope}", "", "**Verified facts**"]
        vf = d.verified_facts()
        lines += [f"- {f.id} [{f.about}/{f.kind}] {f.claim} — [source]({f.source_url})" for f in vf] or ["- (none)"]
        dropped = [f for f in d.facts if not f.verified]
        if dropped:
            lines.append(f"- _{len(dropped)} model-extracted fact(s) dropped: evidence not found verbatim in source_")
        if d.angles:
            lines += ["", "**Angles**"] + [f"- {a.angle} (facts: {', '.join(a.fact_ids) or '-'}; proof: {a.proof_point or '-'})" for a in d.angles]
    if l.draft:
        for m in l.draft.messages:
            label = "Email 1" if m.step == 0 else f"Follow-up {m.step}"
            lines += ["", f"**{label}** ({m.word_count} words) — subject: `{m.subject}`", "```text", m.body, "```"]
        if l.draft.qa:
            q = l.draft.qa
            lines.append(f"QA: {'PASS' if q.passed else 'FAIL'} after {q.attempts} attempt(s)"
                         + (f" — {'; '.join((q.lint_issues + q.critic_issues)[:5])}" if not q.passed else ""))
    return "\n".join(lines)


def outbox_markdown(leads: list[Lead]) -> str:
    head = ["# Outbox", "", "Approved drafts waiting for their send window, plus drafts that need a human look.",
            "Edit `state/suppression.txt` to block an address; set a lead's state in `state/leads.jsonl` to skip it.", ""]
    return "\n".join(head + [dossier_markdown(l) + "\n\n---\n" for l in leads]) + "\n"


def summary_markdown(ctx: Context, report: RunReport, title: str = "Outreach tick") -> str:
    counts = ctx.store.count_by_state()
    leads = list(ctx.store.all())
    ver = Counter(l.verification.status.value for l in leads if l.verification)
    total_v = sum(ver.values()) or 1
    errs: Counter = Counter()
    for st in report.stages.values():
        errs.update(st.errors)
    http_errs = Counter(ctx.http.metrics.error_summary())
    if ctx.llm.http is not ctx.http:
        http_errs.update(ctx.llm.http.metrics.error_summary())
    mode = "🧪 DRY RUN" if ctx.s.dry_run else "📤 LIVE"
    md = [f"## {title} — {mode} · mode `{ctx.s.outreach_mode}` · LLM: {ctx.llm.describe()}", "",
          "| Stage | Processed | Outcomes | Errors | Notes |", "|---|---|---|---|---|"]
    for name, st in report.stages.items():
        oc = ", ".join(f"{k} {v}" for k, v in st.outcomes.most_common()) or "—"
        er = ", ".join(f"{k} {v}" for k, v in st.errors.most_common()) or "—"
        md.append(f"| {name} | {st.processed} | {oc} | {er} | {'; '.join(st.notes)[:300] or '—'} |")
    md += ["", "**Pipeline state:** " + " · ".join(f"{k} {v}" for k, v in sorted(counts.items())),
           "", "**Email verification (all leads):** " + " · ".join(
               f"{s} {ver.get(s.value, 0)} ({100 * ver.get(s.value, 0) // total_v}%)" for s in EmailStatus if ver.get(s.value)),
           ""]
    if errs or http_errs:
        md.append("**Errors by category:** " + " · ".join(f"{k} {v}" for k, v in (errs + Counter(http_errs)).most_common()))
    breakers = {**ctx.http.metrics.breaker_reasons, **ctx.llm.http.metrics.breaker_reasons}
    if breakers:
        md.append("**Circuit breakers opened:** " + "; ".join(f"`{p}` {r}" for p, r in breakers.items()))
    if ctx.verifier.smtp.disabled_reason:
        md.append(f"**SMTP probe:** {ctx.verifier.smtp.disabled_reason}")
    if report.would_send:
        md += ["", f"**Would send ({len(report.would_send)}):**"] + [
            f"- [{w['tier']}] step {w['step']} → `{w['to']}` — “{w['subject']}” ({w['words']} words)" for w in report.would_send]
    if report.sent:
        md += ["", f"**Sent ({len(report.sent)}):**"] + [f"- [{w['tier']}] step {w['step']} → `{w['to']}` — “{w['subject']}”" for w in report.sent]
    if fails := report.systemic_failures():
        md += ["", "### ❌ Systemic failures", *[f"- {f}" for f in fails]]
    if ctx.llm.usage["input_tokens"]:
        md.append(f"\nLLM usage: {ctx.llm.calls} calls ({ctx.llm.used_models()}), "
                  f"{ctx.llm.usage['input_tokens']} in / {ctx.llm.usage['output_tokens']} out tokens")
    return "\n".join(md) + "\n"


def write_step_summary(text: str) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def export_xlsx(ctx: Context, path: Path) -> None:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Pipeline"
    ws.append(VIEW_COLUMNS)
    for c in ws[1]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", start_color="1E3D6A")
    for l in sorted(ctx.store.all(), key=lambda l: (l.state.value, l.id)):
        ws.append([_row(l)[c] for c in VIEW_COLUMNS])
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=False, vertical="top")
    ws.freeze_panes = "A2"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
