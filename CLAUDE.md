# CLAUDE.md — Outbound Sniper v2

Targeted cold-outreach pipeline for **Tanmay Kaper** (BSc Economics, NMIMS '27). It finds senior people in
consulting, PE/VC, startups, research and data roles (India-first), verifies their email, researches them,
scores them against the ICP, drafts a grounded plain-text email with 2 follow-ups, and sends via Gmail from
GitHub Actions. The root-cause audit behind this design is in `UPGRADE_AUDIT.md`.

## Commands
```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest                       # full suite, no network, no keys
.venv/bin/python -m outreach doctor              # which keys/providers are wired (never prints values)
.venv/bin/python -m outreach tick                # one reconciler pass (DRY_RUN=true unless set false)
.venv/bin/python -m outreach tick --stages verify,research   # subset of stages
.venv/bin/python -m outreach dryrun --out reports/dryrun.md  # sandbox E2E on tests/fixtures/dryrun_sample.csv
.venv/bin/python -m outreach requeue --state NEEDS_ENRICHMENT   # retry after adding a finder/verifier key
.venv/bin/python -m outreach export --xlsx reports/runs/pipeline.xlsx
```

## Architecture (package `outreach/`)
| Module | Role |
|---|---|
| `settings.py` | Env config with safe defaults; never asserts at import. `DRY_RUN` defaults **true**. |
| `profile.py` | **Sender facts (resume-grounded proof points) + ICP** (titles, verticals, geos, rotation). Edit here when the resume/ask changes. |
| `models.py` | Pydantic v2 contracts: `Lead`, `EmailCandidate`, `VerificationResult`, `ResearchDossier`/`Fact`, `LeadScore`, `EmailDraft`, state enums, identity keys. |
| `store.py` | In-memory SQLite engine over committed `state/leads.jsonl` (sorted, one lead per line) + append-only `state/events.jsonl`. Atomic writes. |
| `http.py` | Single resilient client: timeouts, backoff+jitter, `Retry-After`, per-provider circuit breakers, typed `ProviderError(category)`. |
| `ingest/` | `csv_import` (drop-in CSVs, alias column mapping, hash dedupe), `legacy` (one-time v1 migration), `discovery` (Apollo API search, LinkedIn-via-search with strict parsing), `upsert` (dedupe/merge by LinkedIn slug, email, name+company/domain, Apollo id). |
| `enrich/domain.py` | Company → domain with name-ownership checks, aggregator/gov blocklist, MX requirement. |
| `enrich/finders.py` | `BaseEmailFinder` waterfall: Hunter → Findymail → Prospeo → LeadMagic → AnymailFinder → Apollo match (each auto-enabled by its key). Pattern candidates only when a verifier can confirm them. |
| `verify.py` | Gate: syntax → role/disposable → DNS/MX → SMTP probe (self-disables when port 25 is blocked) → API verifier → provider proof. Statuses `VERIFIED` / `CATCH_ALL`(+confidence) / `UNVERIFIED_QUARANTINED` / `INVALID`. |
| `web.py` | Search (Serper → Tavily → DuckDuckGo Lite, paced; trips on DDG's bot challenge — never bypass it) + HTML-to-text fetch. |
| `research.py` | Dossiers: gather sources, split them into **numbered real sentences**, the LLM only *selects* sentence ids (it cannot invent evidence), paraphrases are checked for faithfulness, person attribution requires a source naming the person; `hook_confidence` is computed, not model-chosen. Deterministic fallback without an LLM. |
| `scoring.py` | 0–100 rubric (title 30, company 25, deliverability 20, signal 15, hook 10) → Tier A ≥80 / B 60–79 / C disqualified. |
| `llm.py` | Two tiers (fast: extraction/synthesis · writer: drafting/critic), each an ordered **fallback chain** of (provider, model, key): Anthropic (`claude-haiku-5-5`/`claude-opus-5-5`, SDK structured outputs) → Groq → Gemini free-tier Flash models. A model that hits quota/overload is skipped for the rest of the run. `GEMINI_API_KEY` may hold several comma-separated keys (free quota is 20 req/day per model **per project**). |
| `drafting.py` | Writer prompt, `lint()` (length, banned clichés, spam words, merge-tag leaks, links), LLM critic for factual alignment, one rewrite, else `NEEDS_REVIEW`. |
| `mailer.py` | Gmail SMTP send, IMAP reply/bounce/opt-out detection, Sent-folder idempotency check, windows, follow-up schedule, suppression list. |
| `pipeline.py` | Stage functions + per-lead isolation (`_each`) + `run()`; leads failing a stage 3× park in `NEEDS_REVIEW`. |
| `report.py` | Job Summary markdown, `pipeline_view.csv`, `needs_enrichment.csv`, `outbox.md`, xlsx. |

**State machine:** `DISCOVERED → ENRICHED → VERIFIED → RESEARCHED → SCORED → (DRAFTED) → APPROVED → SENT → REPLIED/BOUNCED/OPTED_OUT`,
side exits `NEEDS_ENRICHMENT` (quarantine), `NEEDS_REVIEW`, `DISQUALIFIED`.

## Invariants — do not break
1. **Nothing unverified is ever sent.** `verify.sendable()` is checked again in `stage_send`. Pattern guesses can only
   become VERIFIED via SMTP 250 on a non-catch-all server or an API verifier, never via provider labels.
2. **No re-contact.** Identity keys of every v1/v2 contacted lead stay in the store; merges keep contact history.
3. **Zero hallucination.** Drafts may only use `Fact`s with `verified=True` and `SENDER.proof_points` text. The
   lint rejects unknown fact ids; the critic rejects unsupported claims.
4. **DRY_RUN is the default.** Live sends need `DRY_RUN=false` (Actions: repo variable `LIVE_SEND=true`).
5. **Ticks are idempotent.** Any stage can re-run; work is keyed on lead state, never on which cron fired.
6. Per-record failures never raise out of a stage; only "every record failed" or a stage crash exits non-zero.

## Conventions
- Python 3.11+ (CI 3.12). No secrets in logs or commits: `Keys.configured()` reports booleans only.
- State files are committed by `scripts/commit_state.sh` (pull --rebase retry). Don't hand-edit `leads.jsonl`
  while a workflow is running.
- Tests use fakes (`tests/fakes.py`, `FakeSearch`) and `responses` — never real network or keys.
- Repo is public: committed state contains contact names/emails. See README "Privacy".
