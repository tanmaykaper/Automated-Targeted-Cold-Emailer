# Upgrade Audit — Outbound Sniper (2026-11-20)

Forensic audit of `tanmaykaper/Automated-Targeted-Cold-Emailer` at `6b41e3c`, before the v2 rebuild.
Evidence sources: the full source tree, `state/pipeline.csv` (593 rows), `state/dispatch_log.jsonl`
(585 entries), all 389 GitHub Actions runs (public REST API), job/step metadata and check-run
annotations for the failed runs, `resume.pdf`, and the Apollo / Hunter API docs.

> `gh` was not authenticated on this machine, so `gh secret list` and raw run logs (`--log-failed`)
> were unavailable. Run outcomes, step-level conclusions and failure annotations came from the public
> API instead. Configured secrets were inferred from the workflow file and from which providers
> appear in the data (Hunter: yes; Groq: yes; Serper: yes; Prospeo/Snov/AnyMailFinder: no hits).

---

## 1. Outreach goals & ICP (extracted from `modules/config.py`, README, resume)

| Dimension | Definition in repo |
|---|---|
| Sender | Tanmay Kaper — BSc Economics, NMIMS Mumbai (CGPA 3.52/4, grad. May 2027); KPMG Consulting Intern, Technology & Data Analytics (client: Sony Entertainment — Python ETL on BARC data, SAC C-suite dashboards); Yokohama Off-Highway Tyres data intern; founder of Nightwalk Fashion (break-even in 2 months, 30% margin); published economics research (FAME II EV policy, Mumbai street-food market structure, South Africa development) |
| Goal (mode `internship`) | Remote / part-time strategy, consulting, research or data internship |
| Goal (mode `job`, from Jan 2027) | Full-time graduate role in strategy, consulting, data analytics or economic research |
| Ask | A short conversation (15–20 min) / advice — never a direct job request |
| Target titles | Tier A: Founder, CEO, CSO, MD, Partner, Chief of Staff, CDO, COO, President · Tier B: VP, Director, Head of Strategy/Growth/Analytics/Research/Data, Principal, Engagement Manager, Chief Economist · Tier C: Manager, Senior Analyst, Team Lead, Associate, (Senior) Consultant |
| Rejected titles | HR, recruiters, talent acquisition, assistants, customer support, interns |
| Verticals | Management consulting · tech startups · impact/climate startups · PE & VC · corporate strategy · economic research & policy · investment banking · data & analytics |
| Geography | India-first (~60%: Mumbai, Bangalore, Delhi NCR, Hyderabad, Pune, Chennai, Ahmedabad), then UK, US, Singapore, UAE, EU, Australia, remote, think tanks |
| Cadence | ~35 leads sourced / ~30 drafted / ≤32 sent per week |

---

## 2. Why "~50% of runs fail" — root causes

### RC-1 (primary): GitHub silently drops and delays scheduled runs — the pipeline assumes every cron fires
The workflow encodes the pipeline as **6 separate crons** (source Mon 04:30, draft Mon 05:00,
dispatch Mon–Thu 05/10/15/19:00 UTC = 18 scheduled events/week) with each stage running only on
its own cron string.

| ISO weeks | Scheduled runs that actually started |
|---|---|
| W27–W34 | 18 / 18 |
| W35–W39 | 16–17 / 18 |
| **W40–W47** | **10 / 18 (44% never ran)** |

Runs that do start are **5–9 h late** (cron 05:00 UTC → typically starts 12:00–14:00 UTC). When a
delayed run is still queued at the next tick, GitHub coalesces them, so 4 dispatch crons/day became
2 runs/day. All crons sit on minute `:00`, the most congested slot. Because each stage only runs on
its own cron, a dropped draft cron means nothing gets drafted that week, and a dropped dispatch cron
means nothing gets sent. Every run that did start shows "success", which is why this looked like
flakiness rather than a design problem.

### RC-2: Silent failures reported as success
Every stage swallows exceptions per call and exits 0:
- `run_draft` logs "Draft failed — will retry next run", but draft only runs on Monday, so the next
  retry is 7 days later. 9 leads are stuck `Pending` with no draft.
- Serper/Hunter/Groq errors are logged at `debug` and return empty results. A key that expired or a
  quota that ran out looks the same as "found no leads".
- No run summary separates "nothing to do" from "everything failed".

### RC-3: Hard crashes on config (the real `failure` runs)
- W26 source failures (e.g. job `85330342805`) exited 1 in **0 seconds**, i.e. at import time.
  `config.py` asserts `WEEKLY_TARGET >= WEEKLY_DRAFT_CAP` and that `OUTREACH_MODE` is valid at
  module import, so a secret like `WEEKLY_TARGET=25` (the README's own example) kills every stage.
- Dispatch jobs `86119212757` and `92682005944` were cancelled: *"The job was not acquired by Runner
  … after multiple attempts"* (runner capacity). Nothing retried them.

### RC-4: Fragile state & concurrency
- `state/pipeline.csv` is rewritten in full on every draft and on every single sent email
  (`mark_sent` rewrites the whole file once per send). A crash midway through a send loop leaves the
  CSV and `weekly_count.json` out of sync, because counts are only saved at the end.
- `git push` has no `pull --rebase`/retry. A push collision or a human commit landing mid-run
  rejects the push, and that run's state is lost.
- `concurrency: cancel-in-progress: false` still **cancels the pending run** when a third run
  arrives (GitHub allows one pending run per group). Monday 04:30/05:00/05:00 triggers 3 runs at once.
- The binary `pipeline.xlsx` (230 KB) is committed on every run, which bloats the repo.

### RC-5: The scheduled window check is always bypassed
`FORCE_WINDOW: ${{ github.event.inputs.force_window || 'true' }}`. Scheduled runs have no inputs,
so **every scheduled dispatch force-sends** whatever the recipient's local time. Combined with
RC-1's delays, India leads were mailed at 18:30–04:00 IST.

### RC-6: Deprecated runtime
Annotations: `actions/checkout@v4` and `setup-python@v5` target Node 20, which is deprecated and
force-run on Node 24.

---

## 3. Why emails are unusable — lead & data quality

| Metric (state/pipeline.csv) | Value |
|---|---|
| Rows | 593 |
| Sent | **584** |
| Sent to an **unverified, pattern-guessed** address | **371 (64%)** |
| Email source = pattern inference | 320 |
| Company parsed as "LinkedIn" | 60 |
| Company name truncated (`...`) | 169 |
| Missing company description | 148 |

**Addresses actually emailed include** `@support.linkedhelper.com` (38), `@x.com` (22),
`@rocketreach.co` (13), `@whitehouse.gov` (12), `@about.google` (8), `@login.gov` (5),
`@privateequityinternational.com`, `@careers.docusign.com`, `@healthtechmagazine.net`. These are
guaranteed bounces or wrong people, and they have been eroding the reputation of the personal Gmail
sender.

Root causes:
1. **Domain resolution is a Google "I'm feeling lucky".** `_get_company_domain()` takes the first
   organic result for `"<company> official website contact email"` that isn't on a short noise list.
   It never checks that the domain belongs to the company. When the company was mis-parsed as
   "LinkedIn", "Strategy & Operations" or "Health Tech ...", it picked random sites.
2. **Snippet parsing is brittle.** `"Name - Title - Company | LinkedIn"` titles get split on every
   dash, LinkedIn truncates long companies with `...`, and the fallback takes "LinkedIn" itself as
   the company.
3. **Pattern guessing is emitted as a usable email.** The waterfall's last tier returns `first.last@`
   with `email_verified=False`, but `REQUIRE_VERIFIED_EMAIL` defaults to `false`, so it is sent anyway.
4. **SMTP verification can never succeed where it runs.** Outbound port 25 is blocked on GitHub-hosted
   (Azure) runners. It was also confirmed blocked on this machine. Every RCPT probe returns `unknown`,
   so the code always falls through to the guessed pattern.
5. **Apollo API is effectively dead code.** `_apollo_find_email()` returns `None` unconditionally.
   Per current Apollo docs, People Search moved to `POST /api/v1/mixed_people/api_search` (0 credits;
   returns no emails, obfuscated last names, no LinkedIn URL), and enrichment is
   `POST /api/v1/people/match` (1 credit/email, `x-api-key` header). **On the free plan, both are only
   available to accounts registered with a work email.** A Gmail-registered Apollo account gets
   403s, which matches what the stub comment describes. The manual CSV export is therefore the right
   Apollo path for this user. It was already the cleanest data source (100 verified rows).
6. **Pipeline dedup is by email only.** The same person with a different guessed address is a "new"
   lead, and there is no canonical person ID.
7. **Apollo import re-processes `done/`** on every run (by design, for "refresh"). Duplicate
   exports `(1)`/`(0)` and `(3)`/`(4)` are byte-identical.

---

## 4. Why replies are low — copy quality

Measured across the 584 drafted emails:

| Signal | Count |
|---|---|
| "align" / "aligns" | 242 |
| "eager" | 99 |
| "impressed" | 82 |
| "I am reaching out" | 58 |
| "I have attached my resume" | 543 |
| Avg body length | 119 words (max 166) |
| Most common subject | "Quick question on McKinsey data" (23×), "Quick question on LinkedIn data" (19×) |

1. **Hallucinated hooks.** The prompt orders "line 1 must reference something SPECIFIC" but supplies
   no researched facts, so the model invents them. Examples sent: *"Strategy & Operations recently
   supported Stripe in enhancing its data analytics workflow…"* and *"PSC recently delivered a
   sustainability assessment for a major infrastructure project."* Neither is true. A false
   "observation" in line 1 is worse than no observation.
2. **Fabricated sender facts.** `SYSTEM_INSTRUCTION_BASE` says *"internships at top-tier firms like
   EY and KPMG"*. EY does not appear on the resume.
3. **Stale availability.** `goal_internship` says "available … through October 2026". Today is
   2026-11-20.
4. **Resume-dump structure.** Every email lists degree, KPMG, the D2C brand and research in the same
   order, then a calendar ask ("Would a quick 15-minute call this week work?").
5. **Spam-filter risk.** A PDF attachment on a first-touch cold email from a personal Gmail is a
   strong spam signal. Subjects repeat verbatim across dozens of sends.
6. **No QA gate.** The only check is word count. There's no banned-phrase lint, no placeholder-leak
   check (`[Company]` examples are in the prompt) and no fact check against sources.
7. **No follow-ups, no reply/bounce detection, no opt-out handling.**

---

## 5. Upgrade plan (implemented in v2 — see `CLAUDE.md` for the architecture)

| # | Fix | Addresses |
|---|---|---|
| 1 | **Reconciler "tick" model.** One idempotent `python -m outreach tick` runs every stage that is due. A single cron at an off-peak minute fires hourly on weekdays, so a dropped or late run just means the next tick catches up. Stage cadence (weekly sourcing, daily caps) is enforced from DB state, not from which cron fired. | RC-1, RC-2 |
| 2 | **SQLite engine + git-friendly JSONL snapshots** (`state/leads.jsonl`, `state/events.jsonl`). Atomic writes (tmp + `os.replace`), per-lead commits, explicit state machine `DISCOVERED → ENRICHED → VERIFIED → RESEARCHED → SCORED → DRAFTED → APPROVED → SENT` (+ `NEEDS_ENRICHMENT`, `NEEDS_REVIEW`, `DISQUALIFIED`, `BOUNCED`, `REPLIED`, `OPTED_OUT`), canonical lead IDs. Commit step uses `pull --rebase` with retries. | RC-4 |
| 3 | **Resilient HTTP client.** Timeouts, exponential backoff + jitter, `Retry-After`, per-host circuit breakers, error categories. Per-record error isolation, so one bad lead never kills a batch. | RC-2 |
| 4 | **Pydantic v2 contracts** for leads, enrichment, verification, dossiers, scores and drafts. Config is validated with clear errors, never asserted at import. | RC-3 |
| 5 | **Job Summary + run report** with counts by stage, verification %, errors by category, and the "nothing to do" vs "failed" distinction. Non-zero exit only on systemic failure (e.g. every LLM call failed). | RC-2 |
| 6 | **Scheduled sends respect windows.** Force-window only on an explicit manual input. **`DRY_RUN` defaults to true** until the repo variable `LIVE_SEND=true` is set. Daily + weekly caps, jittered human-like delays. | RC-5 |
| 7 | **Ingestion:** first-class CSV drop-in (`inputs/`, Apollo/LinkedIn/custom column aliasing), Apollo API adapter on current endpoints with explicit plan diagnostics, pluggable search discovery (Serper / Tavily / DuckDuckGo) with strict title parsing. Legacy migration imports all 593 rows so no one is ever emailed twice. | §3 |
| 8 | **Domain resolution with ownership checks** (company tokens must match the domain or the site title; a blocklist of aggregators/social/gov/news domains). | §3.1–3.2 |
| 9 | **Email-finder waterfall** (`BaseEmailFinder`): input email → Hunter → Prospeo → Findymail → LeadMagic → AnymailFinder → Apollo match, each auto-enabled by its env var. Pattern candidates are generated **only** for verifier confirmation and never emitted as usable. | §3.3 |
| 10 | **Mandatory verification gate:** syntax → role/disposable → DNS/MX → SMTP probe (auto-disabled when port 25 is blocked) → API verifier (Hunter / ZeroBounce / MillionVerifier / Reoon / Bouncer). Statuses: `VERIFIED`, `CATCH_ALL` (with confidence), `UNVERIFIED_QUARANTINED`, `INVALID`. Quarantined and invalid leads are hard-blocked from the send queue and exported to `state/needs_enrichment.csv`. | §3.3–3.4 |
| 11 | **Research dossiers** from company site + news + person search. Every fact carries a source URL and verbatim evidence that is checked programmatically against the fetched text. `hook_confidence` is high/medium/low, with an explicit company/role-level fallback. | §4.1 |
| 12 | **Transparent 0–100 scoring** derived from the ICP above, with an itemized breakdown + rationale. Tier A ≥80, B 60–79, C <60 is disqualified before any drafting tokens are spent. | — |
| 13 | **Tiered LLM routing** (Anthropic → Groq → Gemini, whichever key exists): fast model for extraction, flagship for dossier synthesis and drafting. | — |
| 14 | **Rewritten copy system:** 2–5 word lowercase subjects, 65–110 word plain-text body, line 1 drawn only from verified dossier facts, interest-based CTA, no attachment by default, 2 threaded follow-ups (<60 words). Grounded sender facts only (EY claim and stale dates removed). | §4 |
| 15 | **QA gate:** lint (length, banned clichés, spam words, merge-tag leaks, links, exclamation marks) + LLM critic checks factual alignment with the dossier. One automatic rewrite, then `NEEDS_REVIEW`. | §4.6 |
| 16 | **IMAP reply/bounce detection** with the same Gmail app password. Follow-ups only go out when no reply or bounce has been seen. Opt-out phrases go on a suppression list. | §4.7 |
| 17 | **pytest suite + CI workflow**, Node-24-native action versions, pip caching, Python 3.12. | RC-6 |
