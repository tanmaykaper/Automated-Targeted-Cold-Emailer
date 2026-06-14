# Outbound Sniper
**Tanmay Kaper — Autonomous Cold Outreach Engine**

Perpetual, self-sufficient cold email system. Sources elite leads globally, drafts deeply personalised emails via Gemini, dispatches automatically via Gmail. Runs entirely on GitHub Actions — zero always-on infrastructure, zero paid APIs beyond pennies/month.

---

## What it does

Every week, automatically:
1. **Sources** 20–25 qualified leads (managers → C-suite) across India-first, then UK/US/SG/UAE/EU/AU/Remote
2. **Drafts** a unique, personalised email for each lead using one of 7 proven cold-email frameworks (Gemini Pro)
3. **Dispatches** up to 20 emails/week during optimal local timezone windows — no approval needed
4. **Logs** every send to `state/dispatch_log.jsonl`; updates `state/pipeline.xlsx` for your review

---

## Cost breakdown

| Component | Tool | Cost |
|---|---|---|
| Lead search | Serper.dev | Free (2,500 searches/month) |
| Profile enrichment | linkedin-api (unofficial) | Free |
| Email inference | Pattern logic + DNS check | Free |
| Email drafting | Gemini 2.0 Flash | Free (1,500 req/day) |
| Email dispatch | Gmail SMTP + App Password | Free |
| Scheduling | GitHub Actions | Free (public repos) |
| State / database | Git-committed CSV | Free |
| **Total** | | **$0.00/month** |

---

## Architecture

```
Every Monday
  04:30 UTC → [SOURCE]    Serper search × 4 geo/vertical combos
                           LinkedIn profile enrichment
                           Email pattern inference + DNS verify
                           Score leads (0-100), keep ≥55
                           Append new leads to state/pipeline.csv
                           Export state/pipeline.xlsx

  05:00 UTC → [DRAFT]     Read all Pending rows without a draft
               (independent — no dependency on source job)
                           Choose framework (PAS/BAB/AIDA/SAS/QVC/PPPP/FFF)
                           Build personalised prompt → Gemini 2.0 Flash
                           Write subject + body back to pipeline.csv
                           Export updated pipeline.xlsx

Mon/Tue/Wed/Thu
  06:30 UTC → [DISPATCH]  Read Pending rows with draft
                           Check target's local timezone window
                           (Tue–Thu 9:30–11:30 AM or 2:00–4:00 PM)
                           Send via Gmail SMTP
                           Mark Status = Sent in CSV
                           Commit state/ back to repo
```

---

## Lead targeting

**Title tiers** (all included — wider net = higher reply rates):
- **Tier A** — Founder, CEO, Partner, Managing Director, CSO, COS, President
- **Tier B** — VP, Director, Head of [X], Principal, Engagement Manager
- **Tier C** — Manager, Senior Analyst, Team Lead, Hiring Manager, Associate, Senior Consultant

**Geographic rotation** (India = ~60% of weekly slots):
- India: Mumbai, Bangalore, Delhi/NCR, Hyderabad, Pune, Chennai, Ahmedabad
- International: London, US (NYC/SF/Boston), Singapore, UAE, Australia, EU, Remote/Global, Research orgs

**Industry verticals** (rotated weekly):
Management Consulting · Tech Startups · Impact/Climate Startups · PE & VC · Corporate Strategy · Economic Research & Policy · Investment Banking · Data & Analytics

**Pool exhaustion?** Never. 12-week rotation × week-number query variation × 8 verticals × 16 geo segments × 3 title tiers = thousands of unique search paths. Same geo+vertical combo recurs after ~3 months with different query phrasing, surfacing new people.

---

## Email personalisation

Each email uses one of 7 frameworks, algorithmically chosen per lead:

| Framework | Best for | Structure |
|---|---|---|
| **PAS** | Startups | Problem → Agitate → Solve |
| **BAB** | Growth roles | Before → After → Bridge |
| **AIDA** | Tier-1 / consulting | Attention → Interest → Desire → Action |
| **SAS** | PE/VC/IB | Star → Arch → Success |
| **QVC** | Any senior role | Question → Value → CTA |
| **PPPP** | Corporate strategy | Picture → Promise → Prove → Push |
| **FFF** | Research / impact | Feel → Felt → Found |

Tone varies by region: India (warm-direct), UK (measured), US (punchy), AU (casual-pro), SG/ME (precise), Remote (async-clear).

Hard rules baked into every prompt:
- ≤ 120 words body
- ≤ 8 word subject (no "Quick question" or "Internship inquiry")
- No generic openers — first line must reference something SPECIFIC about the target
- One CTA only

---

## Setup (one-time, ~15 minutes)

### 1. Fork and clone
```bash
git clone https://github.com/YOUR_USERNAME/outbound-sniper.git
cd outbound-sniper
```

### 2. Get your API keys (all free)

**Serper** — [serper.dev](https://serper.dev) → Sign up → Dashboard → copy API Key

**Gemini** — [aistudio.google.com](https://aistudio.google.com) → Get API Key → copy it

**Gmail App Password:**
1. Enable 2FA on your Google account
2. `myaccount.google.com` → Security → App Passwords
3. App: Mail → Device: Other → name it `OutboundBot`
4. Copy the 16-char password (no spaces)

**LinkedIn secondary account:**
Create a fresh Gmail, then a new LinkedIn account. Takes 5 minutes. Use those credentials — not your main profile.

### 3. Add GitHub Secrets
`Settings → Secrets and variables → Actions → New repository secret`

| Secret | Value |
|---|---|
| `SERPER_API_KEY` | Your Serper key |
| `LINKEDIN_USERNAME` | Secondary LinkedIn email |
| `LINKEDIN_PASSWORD` | Secondary LinkedIn password |
| `GEMINI_API_KEY` | Your Gemini key |
| `SENDER_EMAIL` | `tanmay.kaper1401@gmail.com` |
| `GMAIL_APP_PASSWORD` | 16-char App Password |
| `OUTREACH_MODE` | `internship` (change to `job` for Phase 2) |

### 4. Enable Actions
Repo → **Actions** tab → click **"I understand my workflows, go ahead and enable them"**

First automated run: next Monday at 04:30 UTC.

---

## Switching modes

When you're ready to switch from internship to job search (Jan 2027):
1. Go to `Settings → Secrets → Actions`
2. Edit `OUTREACH_MODE` → change `internship` to `job`
3. That's it — all subsequent emails will be framed around full-time graduate roles

Or trigger a manual run with the override: `Actions → Outbound Sniper → Run workflow → outreach_mode: job`

---

## Manual triggers

`Actions → Outbound Sniper → Run workflow`

| Option | Use case |
|---|---|
| `action: source` | Manually kick off a sourcing run |
| `action: draft` | Draft emails for any un-drafted Pending rows (safe to re-run anytime) |
| `action: dispatch` + `dry_run: true` | Test dispatch without sending — logs what would go |
| `action: dispatch` + `force_window: true` | Send immediately regardless of timezone window |

---

## Monitoring

After each run, GitHub automatically updates:
- `state/pipeline.csv` — full pipeline (source of truth)
- `state/pipeline.xlsx` — formatted Excel for easy viewing
- `state/dispatch_log.jsonl` — every send with timestamp, subject, recipient
- `state/weekly_count.json` — weekly send counter

View the pipeline: go to your repo → `state/pipeline.xlsx` → click **Download**

---

## File structure
```
outbound-sniper/
├── .github/
│   └── workflows/
│       └── outbound.yml          GitHub Actions scheduler (4 dispatch runs/week)
├── modules/
│   ├── config.py                 Mode toggle, cadence, personas, rotation schedule
│   ├── module_a_sourcing.py      Serper + LinkedIn sourcing, scoring, dedup
│   ├── module_b_spreadsheet.py   Additive CSV manager + Excel export
│   ├── module_c_personalization.py  Gemini Pro drafting, 7 frameworks
│   ├── module_d_dispatch.py      Timezone-aware Gmail SMTP dispatch
│   └── orchestrator.py           Single Actions entry point
├── state/
│   ├── pipeline.csv              THE DATABASE — additive, never overwritten
│   ├── pipeline.xlsx             Human-readable view, regenerated each run
│   ├── dispatch_log.jsonl        Every send logged here
│   └── weekly_count.json         Weekly cap tracker
├── .env.example                  Copy to .env for local testing
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Local testing
```bash
pip install -r requirements.txt
cp .env.example .env   # Fill in your real keys

cd modules

# Test sourcing (writes to ../state/pipeline.csv)
ACTION=source python orchestrator.py

# Test drafting (fully independent — works any time)
ACTION=draft python orchestrator.py

# Test dispatch — dry run (logs but does NOT send)
ACTION=dispatch DRY_RUN=true python orchestrator.py

# Test dispatch — force window + dry run
ACTION=dispatch DRY_RUN=true FORCE_WINDOW=true python orchestrator.py

# Actually send
ACTION=dispatch python orchestrator.py
```
