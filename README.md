# Outbound Sniper v2
**Tanmay Kaper — targeted, verified, researched cold outreach**

Finds senior people in consulting, PE/VC, startups, research and data roles (India-first, then UK/US/SG/UAE/EU/AU),
**verifies** their email, researches them with **source-backed facts**, scores them against the ICP, and drafts a
short plain-text email plus two threaded follow-ups. It runs on GitHub Actions and sends from Gmail
inside each recipient's local business hours. **Dry run is the default.**

> v1 → v2: see [`UPGRADE_AUDIT.md`](UPGRADE_AUDIT.md) for the root causes (silently dropped crons, 64% of sends to
> guessed addresses, hallucinated openers) and [`CLAUDE.md`](CLAUDE.md) for the architecture.

## How it works
```
           ┌─ inputs/*.csv (Apollo / LinkedIn / any sheet)   ┐
 ingest ───┼─ Apollo People API Search (if plan allows)      ├─► dedupe vs everyone ever contacted
           └─ LinkedIn-via-search discovery (Serper/Tavily)  ┘
 verify    domain ownership check → finder waterfall → syntax/role/MX/SMTP/API verifier
           → VERIFIED | CATCH_ALL(conf) | UNVERIFIED_QUARANTINED | INVALID   (only the first two can ever send)
 research  company site + news/signals + person search → facts with verbatim evidence, checked against source
 score     title 30 · company 25 · deliverability 20 · signal 15 · hook 10 → Tier A ≥80 / B ≥60 / C dropped
 draft     65–110 word email + 2 follow-ups → lint + critic → one rewrite → APPROVED or NEEDS_REVIEW
 send      Mon–Thu 09:30–11:30 / 14:00–16:00 recipient-local, caps 8/day · 30/week · 4/tick, human-like gaps,
           Gmail Sent-folder double-send guard, IMAP reply/bounce/opt-out detection before follow-ups
```
One workflow (`.github/workflows/outbound.yml`) runs `python -m outreach tick` hourly on weekdays at :23.
Every tick is idempotent, so a late or dropped GitHub cron is caught up by the next one. Each run writes a
**Job Summary** (counts per stage, verification %, errors by category, would-send list) and commits state.

## Daily use
| You want to… | Do this |
|---|---|
| Add leads from an Apollo/LinkedIn export | Put the CSV in `inputs/` and push. It's ingested within minutes. |
| See the pipeline | `state/pipeline_view.csv` (GitHub renders it as a table) or the `pipeline-<run>` xlsx artifact |
| Review drafts before they go | `state/outbox.md`: dossier, sources, score, email + follow-ups for every approved lead |
| Find leads that need a better email | `state/needs_enrichment.csv` |
| Block someone | Add the email or `@domain` to `state/suppression.txt` |
| Test without sending | Actions → Outbound Sniper → Run workflow → `dry_run: true` |
| Go live | Repo **Settings → Secrets and variables → Actions → Variables** → `LIVE_SEND` = `true` |
| Retry quarantined leads after adding a key | `python -m outreach requeue --state NEEDS_ENRICHMENT` |

## Setup
1. **Secrets** (Settings → Secrets and variables → Actions). Run the workflow with `command: doctor` to see what's
   wired. Values are never printed.
   - Required for drafting: one of `ANTHROPIC_API_KEY` (recommended), `GROQ_API_KEY`, `GEMINI_API_KEY`
   - Required for sending + reply detection: `SENDER_EMAIL`, `GMAIL_APP_PASSWORD` (IMAP must be enabled in Gmail)
   - Recommended: `SERPER_API_KEY` (search + discovery), `HUNTER_API_KEY` (finder + verifier), one API verifier
     (`MILLIONVERIFIER_API_KEY` / `ZEROBOUNCE_API_KEY` / `REOON_API_KEY` / `BOUNCER_API_KEY`)
   - Optional finders: `FINDYMAIL_API_KEY`, `PROSPEO_API_KEY`, `LEADMAGIC_API_KEY`, `ANYMAILFINDER_KEY`, `APOLLO_API_KEY`
2. **Variables** (optional): `LIVE_SEND`, `OUTREACH_MODE` (`internship`|`job`), `DAILY_SEND_CAP`, `WEEKLY_CAP`,
   `MAX_PER_RUN`, `WEEKLY_TARGET`, `CATCH_ALL_MIN_CONFIDENCE`, `LLM_PROVIDER`, `LLM_FAST_MODEL`, `LLM_WRITER_MODEL`.
3. Review `outreach/profile.py`: it holds the sender's resume-grounded proof points, the ask, and the ICP.

### Local
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env            # fill in what you have
.venv/bin/python -m pytest
.venv/bin/python -m outreach doctor
.venv/bin/python -m outreach dryrun --out reports/dryrun.md   # sandboxed E2E, nothing sent, real state untouched
.venv/bin/python -m outreach tick                             # real state, DRY_RUN=true by default
```

## Email verification, honestly
SMTP mailbox probing needs outbound port 25, which is **blocked on GitHub-hosted runners** (and most home ISPs).
So, without an API verifier:
- Provider-verified emails (Apollo "Verified", Hunter "valid", Findymail…) pass after the syntax/role/MX checks.
- Catch-all domains (e.g. McKinsey) pass only with provider proof, at confidence 75 (threshold 70).
- **Pattern guesses are never sent**; they are quarantined to `needs_enrichment.csv`.

Adding one API verifier lets the waterfall confirm `first.last@domain`-style guesses, which is where most
coverage gains come from.

## Copy rules (enforced by `outreach/drafting.py`)
2–5 word lowercase subject · 65–110 word plain-text body · line 1 from a **verified** fact (or an honest
company/role observation when `hook_confidence: low`) · one concrete proof point from the resume ·
interest-based ask, no calendar link · no attachment or links in email 1 · follow-ups under 60 and 45 words, the
last one with an easy out. Clichés ("I came across", "impressed", "aligns with", "eager", "delve" …), spam
words and merge-tag leaks fail QA.

## Privacy
This repo is **public**, and `state/` holds contact names, emails, dossiers and drafts. Consider making the
repo private. That costs nothing: hourly ticks stay well inside the free Actions minutes for private repos.

## Layout
```
outreach/            pipeline package (see CLAUDE.md)
inputs/              CSV drop-in folder (processed/ = archive)
state/leads.jsonl    source of truth, one lead per line (committed)
state/events.jsonl   append-only audit log (sends, migrations, replies)
state/legacy/        v1 files, migrated once and kept for reference
tests/               pytest suite + fixtures/dryrun_sample.csv
reports/             dry-run reports
scripts/commit_state.sh   rebase-and-retry state commit used by the workflow
```
