"""
ORCHESTRATOR — single entry point for GitHub Actions
═════════════════════════════════════════════════════
ACTION env var controls which pipeline stage runs:

  source   → Module A+B: Serper+LinkedIn sourcing → append to pipeline.csv
  draft    → Module C:   Gemini drafting → write back to pipeline.csv (INDEPENDENT of source)
  dispatch → Module D:   Send Pending+drafted rows via Gmail SMTP → mark Sent

Schedule (GitHub Actions):
  Mon  04:30 UTC → source   (fresh leads for the week)
  Mon  05:00 UTC → draft    (draft emails for all new Pending rows)
  Mon  06:30 UTC → dispatch (send up to 5 — early window for India/UK)
  Tue  06:30 UTC → dispatch
  Wed  06:30 UTC → dispatch
  Thu  06:30 UTC → dispatch
  (4 dispatch runs × 5 max = up to 20/week)

Draft is intentionally decoupled from source:
  - Can be triggered manually at any time to draft for any un-drafted Pending rows
  - Does NOT require source to have run in the same session
  - Safe to re-run: skips rows that already have a draft
"""

import os, sys, json, logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

ACTION    = os.getenv("ACTION",        "source").lower()
DRY_RUN   = os.getenv("DRY_RUN",      "false").lower() == "true"
FORCE_WIN = os.getenv("FORCE_WINDOW", "true").lower() == "true"

GH_SUMMARY = os.getenv("GITHUB_STEP_SUMMARY", "")

def _summary(text: str) -> None:
    if GH_SUMMARY:
        with open(GH_SUMMARY, "a") as f:
            f.write(text + "\n")
    print(text)


# ══════════════════════════════════════════════════════════════════════════
# SOURCE
# ══════════════════════════════════════════════════════════════════════════

def run_source() -> dict:
    log.info("══ ACTION: SOURCE ══")
    from module_a_sourcing    import run_sourcing_pipeline
    from module_b_spreadsheet import append_leads, get_stats, export_excel

    target = int(os.getenv("WEEKLY_TARGET", "25"))
    leads  = run_sourcing_pipeline(weekly_target=target)
    added  = append_leads(leads)
    stats  = get_stats()
    export_excel()

    _summary(f"""## 🎯 Source Run — {os.getenv('OUTREACH_MODE','internship').upper()} mode
| | |
|---|---|
| New leads added | **{added}** |
| Total in pipeline | {stats['total']} |
| Pending (no draft) | {stats['no_draft']} |
| Pending (draft ready) | {stats.get('drafted',0) - stats.get('sent',0)} |
| Sent all-time | {stats['sent']} |
""")
    return {"added": added, "stats": stats}


# ══════════════════════════════════════════════════════════════════════════
# MANUAL INTAKE  — leads sourced by hand from LinkedIn Premium/Sales Nav,
# enriched through the same waterfall as automated sourcing. No LinkedIn
# automation involved — see module_a_manual_intake.py.
# ══════════════════════════════════════════════════════════════════════════

def run_manual_intake() -> dict:
    log.info("══ ACTION: MANUAL_INTAKE ══")
    from module_a_manual_intake import run_manual_intake_pipeline
    from module_b_spreadsheet   import append_leads, get_stats, export_excel

    leads = run_manual_intake_pipeline()
    added = append_leads(leads)
    stats = get_stats()
    export_excel()

    _summary(f"""## 🖐️ Manual Intake Run
| | |
|---|---|
| Rows processed | **{len(leads)}** |
| New leads added | **{added}** |
| Total in pipeline | {stats['total']} |
| Pending (no draft) | {stats['no_draft']} |
""")
    return {"added": added, "stats": stats}


# ══════════════════════════════════════════════════════════════════════════
# APOLLO IMPORT — leads manually exported from Apollo.io's UI (no API, no
# automation), deduplicated against the whole existing pipeline and
# against each other. See import_apollo_export.py.
# ══════════════════════════════════════════════════════════════════════════

def run_apollo_import() -> dict:
    log.info("══ ACTION: APOLLO_IMPORT ══")
    from import_apollo_export      import run_apollo_import as _import
    from module_b_spreadsheet import append_leads, get_stats, export_excel

    leads = _import()
    added = append_leads(leads)
    stats = get_stats()
    export_excel()

    _summary(f"""## 📥 Apollo Import Run
| | |
|---|---|
| New leads added | **{added}** |
| Total in pipeline | {stats['total']} |
| Pending (no draft) | {stats['no_draft']} |
""")
    return {"added": added, "stats": stats}


# ══════════════════════════════════════════════════════════════════════════
# DRAFT  (fully independent — no dependency on source job)
# ══════════════════════════════════════════════════════════════════════════

def run_draft() -> dict:
    log.info("══ ACTION: DRAFT ══")
    from module_b_spreadsheet     import read_pending_without_draft, write_draft, get_stats, export_excel
    from module_c_personalization import draft_email
    import time

    candidates = read_pending_without_draft()
    log.info("%d Pending rows need a draft", len(candidates))

    drafted = failed = 0
    for i, lead in enumerate(candidates, 1):
        log.info("[%d/%d] %s @ %s", i, len(candidates),
                 lead.get("Target Name","?"), lead.get("Company","?"))
        result = draft_email(lead)
        if result and result.get("email_body","").strip():
            write_draft(
                lead["Target Email"],
                result["subject_line"],
                result["email_body"],
            )
            drafted += 1
        else:
            failed += 1
            log.warning("Draft failed — will retry next run")
        if i < len(candidates):
            time.sleep(10)   # Gemini free tier: 15 req/min

    stats = get_stats()
    export_excel()

    _summary(f"""## ✍️ Draft Run Complete
| | |
|---|---|
| Drafted this run | **{drafted}** |
| Failed (will retry) | {failed} |
| Ready to dispatch | {stats.get('drafted',0)} |
| Already sent | {stats['sent']} |
""")
    return {"drafted": drafted, "failed": failed}


# ══════════════════════════════════════════════════════════════════════════
# DISPATCH
# ══════════════════════════════════════════════════════════════════════════

def run_dispatch() -> dict:
    log.info("══ ACTION: DISPATCH ══")
    from module_d_dispatch import run_dispatch_queue

    result = run_dispatch_queue(dry_run=DRY_RUN, force_window=FORCE_WIN)

    icon = "✅" if result.get("sent", 0) > 0 else "⏸️"
    _summary(f"""## {icon} Dispatch Run {'(DRY RUN) ' if DRY_RUN else ''}Complete
| | |
|---|---|
| Sent this run | **{result.get('sent',0)}** |
| Deferred (outside window) | {result.get('deferred',0)} |
| Skipped (bad data) | {result.get('skipped',0)} |
| This week total | {result.get('week_total','?')}/{os.getenv('WEEKLY_CAP','20')} |
| Week cap remaining | {result.get('week_remaining','?')} |
""")
    return result


# ══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    dispatch = {"source": run_source, "manual_intake": run_manual_intake,
                "apollo_import": run_apollo_import,
                "draft": run_draft, "dispatch": run_dispatch}
    if ACTION not in dispatch:
        log.error("Unknown ACTION='%s' — use: source | manual_intake | apollo_import | draft | dispatch", ACTION)
        sys.exit(1)
    result = dispatch[ACTION]()
    print(json.dumps(result, indent=2, default=str))
