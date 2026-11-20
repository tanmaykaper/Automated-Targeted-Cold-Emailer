"""Command line: `python -m outreach <command>`.

  tick       run every due stage (what the scheduled workflow calls)
  doctor     show which keys/providers are configured (never prints values)
  ingest     ingest specific CSV files now
  requeue    send NEEDS_ENRICHMENT / NEEDS_REVIEW leads back through the pipeline
  export     regenerate state/pipeline_view.csv, needs_enrichment.csv, outbox.md (+ optional .xlsx)
  dryrun     end-to-end sandbox run on a CSV (separate temp state, nothing is sent)
  stats      lead counts by state
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

from .models import LeadState
from .settings import REPO_ROOT, load_settings

log = logging.getLogger("outreach")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stdout)
    for noisy in ("urllib3", "httpx", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_tick(args) -> int:
    from .pipeline import Context, run
    from .report import export_views, summary_markdown, write_step_summary
    overrides = {}
    if args.force_window:
        overrides["force_window"] = True
    if args.dry_run is not None:
        overrides["dry_run"] = args.dry_run
    s = load_settings(**overrides)
    ctx = Context(s)
    report = run(ctx, args.stages.split(",") if args.stages else None)
    export_views(ctx)
    ctx.store.checkpoint()
    md = summary_markdown(ctx, report)
    print(md)
    write_step_summary(md)
    if args.xlsx:
        from .report import export_xlsx
        export_xlsx(ctx, Path(args.xlsx))
    fails = report.systemic_failures()
    if fails:
        log.error("systemic failures: %s", fails)
        return 1
    return 0


def cmd_doctor(args) -> int:
    from .http import Http
    from .llm import LLM
    s = load_settings()
    llm = LLM(Http(), s)
    configured = s.keys.configured()
    groups = {
        "LLM": ["ANTHROPIC_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY"],
        "Search / discovery": ["SERPER_API_KEY", "TAVILY_API_KEY", "APOLLO_API_KEY"],
        "Email finders": ["HUNTER_API_KEY", "FINDYMAIL_API_KEY", "PROSPEO_API_KEY", "LEADMAGIC_API_KEY", "ANYMAILFINDER_KEY"],
        "Email verifiers": ["MILLIONVERIFIER_API_KEY", "ZEROBOUNCE_API_KEY", "REOON_API_KEY", "BOUNCER_API_KEY", "HUNTER_API_KEY"],
        "Sending": ["SENDER_EMAIL", "GMAIL_APP_PASSWORD"],
    }
    lines = [f"## Outreach doctor — mode `{s.outreach_mode}`, DRY_RUN={s.dry_run}", ""]
    for g, names in groups.items():
        lines.append(f"**{g}:** " + " · ".join(f"{'✅' if configured.get(n) else '—'} `{n}`" for n in names))
    lines += ["", f"LLM routing: {llm.describe()}",
              "Web search: " + (" → ".join(p for p, k in (("serper", s.keys.serper), ("tavily", s.keys.tavily)) if k) + " → duckduckgo"
                                if (s.keys.serper or s.keys.tavily) else "duckduckgo (free fallback)"),
              f"Caps: {s.daily_send_cap}/day, {s.weekly_send_cap}/week, {s.per_tick_send_cap}/tick · "
              f"tiers A≥{s.tier_a_min} B≥{s.tier_b_min} · catch-all min confidence {s.catch_all_min_confidence}"]
    if not any(configured[n] for n in groups["Email verifiers"]):
        lines.append("⚠️ No API email verifier configured. SMTP probing is blocked on GitHub runners, so pattern-guessed "
                     "emails will always be quarantined; only provider-verified emails (Apollo/Hunter/Findymail…) can send.")
    if not llm.provider:
        lines.append("⚠️ No LLM key: research falls back to deterministic extraction and drafting is paused.")
    out = "\n".join(lines)
    print(out)
    from .report import write_step_summary
    write_step_summary(out)
    return 0


def cmd_ingest(args) -> int:
    from .ingest.csv_import import ingest_csv_text
    from .store import Store
    s = load_settings()
    store = Store(s.state_dir)
    for p in args.paths:
        res = ingest_csv_text(store, Path(p).read_text(encoding="utf-8-sig", errors="replace"), Path(p).name, s.outreach_mode)
        print(json.dumps(res))
    store.checkpoint()
    return 0


def cmd_requeue(args) -> int:
    from .store import Store
    s = load_settings()
    store = Store(s.state_dir)
    target = {"NEEDS_ENRICHMENT": LeadState.DISCOVERED, "NEEDS_REVIEW": LeadState.DISCOVERED}
    n = 0
    for lead in store.by_state(LeadState(args.state)):
        if args.id and lead.id not in args.id:
            continue
        if lead.state == LeadState.NEEDS_REVIEW and lead.score and lead.dossier:
            lead.set_state(LeadState.SCORED, "requeued for redraft")
            lead.draft = None
        else:
            lead.set_state(target[args.state], "requeued")
            lead.tried_finders.clear()
            lead.legacy.pop("checked_emails", None)
        lead.attempts.clear()
        store.save(lead)
        n += 1
    store.checkpoint()
    print(f"requeued {n} lead(s)")
    return 0


def cmd_export(args) -> int:
    from .pipeline import Context
    from .report import export_views, export_xlsx
    ctx = Context(load_settings())
    export_views(ctx)
    if args.xlsx:
        export_xlsx(ctx, Path(args.xlsx))
    return 0


def cmd_stats(args) -> int:
    from .store import Store
    print(json.dumps(Store(load_settings().state_dir).count_by_state(), indent=2, sort_keys=True))
    return 0


def cmd_dryrun(args) -> int:
    """Sandbox: fresh temp state, ingest CSV(s), run all stages with DRY_RUN + forced window, write a report."""
    from .ingest.csv_import import ingest_csv_text
    from .pipeline import Context, run
    from .report import dossier_markdown, summary_markdown
    from .store import Store
    tmp = Path(tempfile.mkdtemp(prefix="outreach-dryrun-"))
    try:
        s = load_settings(dry_run=True, force_window=True, state_dir=tmp, inputs_dir=tmp / "inputs",
                          research_per_tick=args.limit, drafts_per_tick=args.limit, per_tick_send_cap=args.limit,
                          daily_send_cap=max(args.limit, 1), weekly_send_cap=max(args.limit, 1))
        store = Store(tmp)
        for p in args.csv:
            res = ingest_csv_text(store, Path(p).read_text(encoding="utf-8-sig"), Path(p).name, s.outreach_mode)
            log.info("ingested %s", res)
        store.checkpoint()
        ctx = Context(s, store=store)
        report = run(ctx, ["verify", "research", "score", "draft", "send"])
        md = [f"# Dry run — {Path(args.csv[0]).name}", "", summary_markdown(ctx, report, "Dry run"), "", "## Leads", ""]
        md += [dossier_markdown(l) + "\n\n---\n" for l in sorted(store.all(), key=lambda l: -(l.score.total if l.score else -1))]
        text = "\n".join(md)
        out = Path(args.out) if args.out else None
        if out:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            print(f"report written to {out}")
        else:
            print(text)
        return 1 if report.systemic_failures() else 0
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"sandbox state kept at {tmp}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="outreach", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick")
    t.add_argument("--stages", help="comma list: migrate,sync_replies,ingest,discover,verify,research,score,draft,send")
    t.add_argument("--force-window", action="store_true")
    g = t.add_mutually_exclusive_group()
    g.add_argument("--dry-run", dest="dry_run", action="store_true", default=None)
    g.add_argument("--live", dest="dry_run", action="store_false")
    t.add_argument("--xlsx", help="also write an .xlsx view to this path")
    sub.add_parser("doctor")
    i = sub.add_parser("ingest")
    i.add_argument("paths", nargs="+")
    r = sub.add_parser("requeue")
    r.add_argument("--state", choices=["NEEDS_ENRICHMENT", "NEEDS_REVIEW"], required=True)
    r.add_argument("--id", nargs="*")
    e = sub.add_parser("export")
    e.add_argument("--xlsx")
    sub.add_parser("stats")
    d = sub.add_parser("dryrun")
    d.add_argument("--csv", nargs="+", default=[str(REPO_ROOT / "tests" / "fixtures" / "dryrun_sample.csv")])
    d.add_argument("--limit", type=int, default=10)
    d.add_argument("--out")
    d.add_argument("--keep", action="store_true")
    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return {"tick": cmd_tick, "doctor": cmd_doctor, "ingest": cmd_ingest, "requeue": cmd_requeue,
            "export": cmd_export, "stats": cmd_stats, "dryrun": cmd_dryrun}[args.cmd](args)
