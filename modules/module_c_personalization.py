"""
MODULE C: Personalization Engine — Groq (gpt-oss-120b)
══════════════════════════════════════════════════════
Model: openai/gpt-oss-120b via Groq API (fast, cheap, JSON-mode capable)
API:   console.groq.com → API Keys

Cadence: 8 drafts per run, 30/week cap (see DRAFTS_PER_RUN / WEEKLY_DRAFT_CAP
below). run_drafting_pipeline() enforces the per-run cap directly so a
config drift in Module A's WEEKLY_TARGET can't silently blow past intended
drafting volume — leads beyond the cap roll to the next scheduled run
untouched rather than being dropped.

Regional structure (IN vs International):
  India targets get a direct, no-framework self-introduction structure
  (SYSTEM_INSTRUCTION_IN) — who I am, why I'm writing to you, what I'm
  asking, stated plainly. Framework-driven rhetorical openers read as
  try-hard to Indian senior professionals reading a student's cold email.

  International targets (US/UK/AU/SG/EU/ME/Remote) keep the framework-driven
  approach (SYSTEM_INSTRUCTION_INTL) — one of 7 proven cold-email frameworks
  selected per-lead, since that register matches what these inboxes already
  expect from peers and recruiters.

  Frameworks (international only):
    1. PAS  — Problem → Agitate → Solve  (startup pain points)
    2. BAB  — Before → After → Bridge    (transformation framing)
    3. AIDA — Attention → Interest → Desire → Action  (classic)
    4. SAS  — Star → Arch → Success      (storytelling)
    5. QVC  — Question → Value → CTA     (curiosity opener)
    6. PPPP — Picture → Promise → Prove → Push  (vivid scenario)
    7. FFF  — Feel → Felt → Found        (empathy-led, great for research/impact roles)

  Framework is selected based on:
    - Company type (startup → PAS/BAB; Tier-1 → AIDA/QVC; Research → FFF/SAS)
    - Title tier (A → direct/bold; B/C → rapport-building)
    - Vertical (consulting → QVC; impact → FFF; VC → SAS)

Subject lines: region-specific rules (REGION_SUBJECT_RULES) — IN gets plain/
literal subjects, US gets curiosity-driven, UK/SG/EU stay restrained and
specific. Replaces the old one-size-fits-all "3-10 words, sentence case" rule.

Tone varies by region (REGION_TONE):
  IN → warm-direct; UK → measured confidence; US → direct value-first;
  AU → warm-casual; SG/ME → precise-formal; EU → clear-professional;
  REMOTE → global professional; RESEARCH → intellectually curious.

System prompt for the Groq model (_GROQ_SYSTEM_PROMPT) is written as a hard
output contract rather than a flavor-setting instruction — gpt-oss-120b
drifts more than a frontier model on JSON formatting, word-count discipline,
and banned-phrase leakage if the system message doesn't enforce those
explicitly rather than just describing the persona.

Draft is decoupled from sourcing — can run independently on any Pending rows.
"""

import os, re, json, time, logging, random
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from config import TANMAY, MODE_CONTEXT, OUTREACH_MODE

log = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_URL     = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

# ══════════════════════════════════════════════════════════════════════════
# CADENCE
# Updated targets: 8 drafted per sourcing run, 30/week overall.
# WEEKLY_TARGET in config.py still governs Module A's sourcing volume per
# run — this file doesn't own that number, but run_drafting_pipeline() is
# the natural checkpoint to enforce a hard per-call ceiling on drafting
# volume regardless of how many leads Module A hands it, so a config drift
# upstream can't silently blow past the intended cadence on the Module C
# side (API cost + rate-limit exposure scales with drafts, not just sourced
# leads).
# ══════════════════════════════════════════════════════════════════════════
DRAFTS_PER_RUN   = 8     # hard cap per run_drafting_pipeline() call
WEEKLY_DRAFT_CAP = 30    # informational — enforced by caller's scheduling cadence
                          # (e.g. 8/run × ~4 runs/week ≈ 32, trimmed to 30 by
                          # whichever run would push the week over the cap)


# ══════════════════════════════════════════════════════════════════════════
# REGION DETECTION
# ══════════════════════════════════════════════════════════════════════════

REGION_TONE = {
    "IN": (
        "Tone: Warm but direct. Open with something specific about their work — not a compliment, "
        "a genuine observation. Never sycophantic. Pivot to value in sentence two. "
        "Close with a single soft but clear ask. Natural, like you'd write to a senior college alumni."
    ),
    "UK": (
        "Tone: Measured confidence. Polished but never stiff. Slight deference on first contact "
        "without being weak. Well-structured. No American hyperbole. Understated credibility."
    ),
    "US": (
        "Tone: Direct. Lead with the punchline — what you bring, not who you are. "
        "One line of context, then the value, then the ask. No pleasantries."
    ),
    "AU": (
        "Tone: Warm-professional. Conversational opener, then crisp. Drop corporate jargon. "
        "Feel like a smart peer reaching out, not a student begging for a chance."
    ),
    "SG": (
        "Tone: Precise, professional, subtly formal. Credentials and concrete outcomes upfront. "
        "Efficient. Respectful of their time from the first word."
    ),
    "ME": (
        "Tone: Formal but warm. Acknowledge their organisation briefly. "
        "Lead with a clear value statement. Professional and polished throughout."
    ),
    "EU": (
        "Tone: Clear and professional. Slightly formal. Concrete, evidence-based. "
        "No fluff. Respect for expertise is implicit, not stated."
    ),
    "REMOTE": (
        "Tone: Global professional. Clean, direct, assumes they value async clarity. "
        "Prove you can communicate concisely — the email IS the audition."
    ),
    "RESEARCH": (
        "Tone: Intellectually curious. Open with a reference to their research domain or a specific "
        "question their work addresses. Position yourself as a thinking peer, not a job-seeker."
    ),
    "DEFAULT": (
        "Tone: Confident, concise, human. Relevance before credentials. One clean ask."
    ),
}

# ──────────────────────────────────────────────────────────────────────────
# SUBJECT LINE RULES PER REGION
# Subject conventions vary as much by region as tone does — a US recruiter's
# inbox runs on punchy/curiosity subjects, an Indian senior's inbox runs on
# plain clarity, a UK/SG inbox skews toward restrained specificity. One
# generic "3-10 words, sentence case" rule (the old behaviour) ignored that
# and meant every region's subject line read identically regardless of who
# was actually receiving it.
# ──────────────────────────────────────────────────────────────────────────
REGION_SUBJECT_RULES = {
    "IN": (
        "Subject: 3-8 words. Plain and literal — state the topic, don't tease it. "
        "Sentence case. Should read like an internal one-line memo from a colleague, not a pitch. "
        "GOOD: 'Quick question on [Company]'s data strategy', 'NMIMS econ student / [Company] internship'. "
        "AVOID anything that sounds like a marketing subject or forced curiosity hook."
    ),
    "UK": (
        "Subject: 4-9 words. Restrained and specific — understated, not clever. Sentence case. "
        "GOOD: 'Economics background, a question on [Company]'s strategy work', 'NMIMS student — quick note on [Company]'. "
        "AVOID exclamation points, AVOID anything that reads as salesy or American-hyperbolic."
    ),
    "US": (
        "Subject: 3-7 words. Punchy, lead with the value or the specific hook — these inboxes are "
        "trained on curiosity-driven subjects from recruiters and peers, so blandness gets buried. "
        "GOOD: '[Company]'s data problem — a thought', 'KPMG analytics, applied to [Company]'. "
        "Sentence case, no exclamation points, no clickbait."
    ),
    "AU": (
        "Subject: 3-8 words. Conversational, low-key, almost like a Slack DM subject. Sentence case. "
        "GOOD: 'A quick one on [Company]', 'Econ student, [Company] question'. Avoid anything stiff or corporate."
    ),
    "SG": (
        "Subject: 4-9 words. Precise and credential-forward — name the concrete hook plainly. Sentence case. "
        "GOOD: 'NMIMS economics — question on [Company]'s [specific thing]', 'KPMG data background, [Company] enquiry'."
    ),
    "ME": (
        "Subject: 4-9 words. Formal but not stiff, organisation-aware. Sentence case. "
        "GOOD: 'Regarding [Company]'s [specific work] — student enquiry', 'NMIMS economics student — quick introduction'."
    ),
    "EU": (
        "Subject: 4-9 words. Clear, professional, no embellishment. Sentence case. "
        "GOOD: '[Company] — a question on [specific topic]', 'Economics student, brief introduction'."
    ),
    "REMOTE": (
        "Subject: 3-8 words. Clean and direct — assume an inbox full of async-first colleagues "
        "who skim hard. Sentence case. GOOD: '[Company] remote role — quick question', 'Econ + data background, [Company]'."
    ),
    "RESEARCH": (
        "Subject: 4-10 words. Intellectually specific — reference the actual research area or question, "
        "not a generic 'opportunity'. Sentence case. GOOD: 'A question on your [specific] research', 'Economics student, [topic] interest'."
    ),
    "DEFAULT": (
        "Subject: 3-9 words. Specific and plain. Sentence case. No exclamation points, no clickbait."
    ),
}

def _region(lead: dict) -> str:
    loc = (lead.get("Location","") + lead.get("Geo Segment","")).lower()
    if any(k in loc for k in ["india","mumbai","delhi","bangalore","bengaluru","hyderabad","pune","chennai","in_"]):
        return "IN"
    if any(k in loc for k in ["london","uk","united kingdom","uk_"]):
        return "UK"
    if any(k in loc for k in ["usa","new york","san francisco","boston","us_"]):
        return "US"
    if any(k in loc for k in ["australia","sydney","melbourne","au"]):
        return "AU"
    if any(k in loc for k in ["singapore","sg"]):
        return "SG"
    if any(k in loc for k in ["dubai","abu dhabi","uae","me"]):
        return "ME"
    if any(k in loc for k in ["amsterdam","berlin","zurich","eu"]):
        return "EU"
    if "remote" in loc:
        return "REMOTE"
    if "research" in loc:
        return "RESEARCH"
    return "DEFAULT"


# ══════════════════════════════════════════════════════════════════════════
# FRAMEWORK SELECTOR
# ══════════════════════════════════════════════════════════════════════════

FRAMEWORKS = {
    "PAS": (
        "Framework: PAS (Problem → Agitate → Solution).\n"
        "Open by naming a real tension or challenge in THEIR industry or role. "
        "Briefly intensify why that tension matters. Then introduce yourself as the solution. "
        "Do NOT use the words 'problem', 'agitate', or 'solution'."
    ),
    "BAB": (
        "Framework: BAB (Before → After → Bridge).\n"
        "Paint a quick 'before' picture of a common friction they'd recognise. "
        "Describe the 'after' — what better looks like. "
        "Bridge with how you specifically close that gap. Keep it concrete."
    ),
    "AIDA": (
        "Framework: AIDA (Attention → Interest → Desire → Action).\n"
        "Hook with one unexpected, specific observation in line one. "
        "Build interest with a relevant credential. Create desire with a concrete outcome you'd drive. "
        "End with one low-friction action."
    ),
    "SAS": (
        "Framework: SAS (Star → Arch → Success).\n"
        "Set up a micro-story: describe a moment of challenge you faced (Star). "
        "Describe the arc — how you approached it (Arch). "
        "Land on the outcome (Success). Connect it directly to their world."
    ),
    "QVC": (
        "Framework: QVC (Question → Value → CTA).\n"
        "Open with a sharp, specific question they'd actually wonder about. "
        "Answer it implicitly with your value proposition. "
        "End with a CTA that feels like a natural continuation of the conversation."
    ),
    "PPPP": (
        "Framework: PPPP (Picture → Promise → Prove → Push).\n"
        "Create a vivid one-sentence scenario they recognise (Picture). "
        "Make a specific promise about what you deliver (Promise). "
        "Back it with one piece of hard evidence (Prove). "
        "End with a direct, confident push for the next step."
    ),
    "FFF": (
        "Framework: FFF (Feel → Felt → Found).\n"
        "Acknowledge something about their world or mission that shows genuine understanding (Feel). "
        "Note that others in this space have felt the same challenge (Felt). "
        "Share what you've found — the insight or approach you bring (Found). "
        "Works best for research/impact/policy roles."
    ),
}

def _choose_framework(lead: dict) -> tuple:
    """Choose the best framework for this specific lead. Returns (name, instruction).
    Returns (None, None) for India — IN emails use a direct self-introduction
    structure instead of a rhetorical framework (see SYSTEM_INSTRUCTION_IN)."""
    if _region(lead) == "IN":
        return None, None

    ctype    = lead.get("Company Type","").lower()
    vertical = lead.get("Vertical","").lower()
    tier     = lead.get("Title Tier","B")
    is_remote = str(lead.get("Is Remote","")).lower() == "true"

    # Deterministic but varied: use email hash so same lead always gets same framework
    seed = sum(ord(c) for c in lead.get("Target Email","x"))

    if "research" in vertical or "research" in ctype:
        candidates = ["FFF", "SAS", "QVC"]
    elif "startup" in ctype or "startup" in vertical:
        candidates = ["PAS", "BAB", "PPPP"]
    elif "tier-1" in ctype or "consulting" in vertical:
        candidates = ["AIDA", "QVC", "SAS"]
    elif "pe_vc" in vertical or "ib" in vertical:
        candidates = ["SAS", "AIDA", "QVC"]
    elif "impact" in vertical:
        candidates = ["FFF", "PAS", "BAB"]
    else:
        candidates = list(FRAMEWORKS.keys())

    # Tier C (managers) → slightly warmer frameworks
    if tier == "C":
        warm = ["BAB","FFF","PAS"]
        candidates = [c for c in candidates if c in warm] or candidates

    chosen = candidates[seed % len(candidates)]
    return chosen, FRAMEWORKS[chosen]


# ══════════════════════════════════════════════════════════════════════════
# PROMPT BUILDER
# ══════════════════════════════════════════════════════════════════════════

SYSTEM_INSTRUCTION_BASE = """You are Tanmay, a 20-year-old student pursuing a BSc in Economics at NMIMS Mumbai. You are also the founder of an apparel e-commerce startup, a freelance academic tutor, and have corporate experience from internships at top-tier firms like EY and KPMG. 

Your task is to write a highly personalized, genuine, and professional cold email to an industry professional to explore potential internship or job opportunities. The email must be written entirely in the first person ("I", "my"). 

It must NOT sound like a sales pitch, a marketing email, or clickbait. It must sound like a polite, straightforward, and intellectually curious student reaching out for a genuine professional connection.

NON-NEGOTIABLE RULES (apply regardless of structure used below):
1. Tone: Formal, polite, highly respectful of their time, and completely straightforward. Do not beat around the bush, but do not be demanding.
2. The Body: Seamlessly bring in your background (e.g., your economics training, founder mindset, or analytical experience) only as it relates to their specific industry or company. Do not just list your resume; connect your context to theirs.
3. The Ask (CTA): Be direct but low-pressure. Ask for a brief 10-15 min virtual conversation or advice on entering their specific domain. Do not directly beg for a job in the text; build the professional bridge first.
4. Strict Negative Constraints: Avoid corporate buzzwords and AI clichés. BANNED WORDS/PHRASES: "delve", "thrilled", "passionate", "synergy", "value-add", "hope this finds you well", "esteemed", "utilize", "humbly".
5. Length: Keep the body under 150 words. Use short paragraphs for clean reading.
6. BANNED openers: "Hope this finds you well", "I came across your profile", "I wanted to reach out", "My name is Tanmay".
7. Identity & Tone: You MUST write strictly in the FIRST PERSON ("I", "my", "me"). You are Tanmay. Frame your KPMG data experience and economics research as immediate value, but emphasize that your primary goal is to LEARN, build new skills, and actively contribute to their team. Sound personal, genuine, and deeply curious. ABSOLUTELY NO SALESPERSON OR MARKETING TONE. Write like a human student.
8. First sentence: must reference something SPECIFIC about THEIR company, work, or role.
9. Resume: You MUST naturally mention somewhere in the email that you have attached your resume for their reference.
10. Sign off: "— Tanmay Kaper" then next line "tanmay.kaper1401@gmail.com"
11. Output ONLY valid JSON: {"subject_line": "...", "email_body": "..."}
    No markdown. No text outside the JSON object."""

# ──────────────────────────────────────────────────────────────────────────
# REGION-SPECIFIC STRUCTURE
#
# India targets read American-style framework-driven cold emails (PAS/AIDA/
# storytelling openers etc.) as try-hard and slightly off — the cultural
# norm for a student writing a senior professional in India is closer to a
# clean, direct self-introduction: who I am, why I'm writing to YOU
# specifically, what I'm asking for. No narrative device, no "hook."
# Burying that under a rhetorical framework built for a Western inbox reads
# as evasive rather than polished to an Indian reader, and it's also the
# difference between an email that gets answered vs. politely ignored.
#
# International targets (US/UK/AU/SG/EU/ME/Remote) respond better to a
# framework-driven structure — a sharp hook, a clear value arc, a confident
# ask — because that's the register their own inbox is full of from peers
# and recruiters, so a flat self-intro reads as under-confident by contrast.
# ──────────────────────────────────────────────────────────────────────────

SYSTEM_INSTRUCTION_IN = SYSTEM_INSTRUCTION_BASE + """

STRUCTURE FOR THIS EMAIL (India — direct self-introduction, NO rhetorical framework):
Do NOT use a storytelling hook, a "problem/agitate" opener, or any indirect rhetorical device. Indian senior professionals reading a student's cold email expect clarity over cleverness. Use this direct shape instead:
  Line 1: One sentence referencing something SPECIFIC and real about their company/role/recent work — not a compliment, an observation that proves you actually looked.
  Line 2-3: A clean, confident self-introduction — who you are (NMIMS Economics, KPMG data work, founder background) — stated plainly, not narrated. This is "here's who I am" delivered straight, not dressed up as a story.
  Line 4: Why you're writing to THEM specifically, tied to something concrete about their work.
  Line 5: The ask — a brief 10-15 min call, stated plainly.
  Close: Resume mention, sign-off.
This should read like a sharp, respectful, no-wasted-words note from a capable student — not a pitch, not a story, not a hook. Directness IS the charm here, not a framework.

Subject line: 3 to 8 words. Plain, specific, sentence case. Should look like an internal one-line memo, not a marketing subject. 
   GOOD EXAMPLES: "Quick question on [Company]'s data strategy", "NMIMS econ student / [Company] internship", "KPMG data background — quick intro"."""

SYSTEM_INSTRUCTION_INTL = SYSTEM_INSTRUCTION_BASE + """

STRUCTURE FOR THIS EMAIL (International — framework-driven):
Use the EMAIL FRAMEWORK specified below (PAS / BAB / AIDA / SAS / QVC / PPPP / FFF) to structure the email. The framework should be invisible in the final text — never name it, never make the structure feel mechanical — but it should govern the shape: the hook, the build, the ask.

Subject line: 3 to 10 words. Must be punchy, highly specific, and create genuine curiosity. Use sentence case but be formal and sincere (only capitalize the first letter of the subject and proper nouns like company names) so it looks like a quick internal human memo.
   GOOD EXAMPLES: "Quick question regarding [Company] strategy", "NMIMS econ undergrad / [Company] data", "Thoughts on [Company]'s recent research", "KPMG data applied to [Company]"."""


def _build_prompt(lead: dict) -> str:
    mode      = lead.get("Outreach Mode", OUTREACH_MODE)
    ctx       = MODE_CONTEXT.get(mode, MODE_CONTEXT["internship"])
    region    = _region(lead)
    tone_inst = REGION_TONE.get(region, REGION_TONE["DEFAULT"])
    subj_inst = REGION_SUBJECT_RULES.get(region, REGION_SUBJECT_RULES["DEFAULT"])
    fw_name, fw_inst = _choose_framework(lead)

    ctype     = lead.get("Company Type","")
    stage     = lead.get("Funding Stage","")
    remote    = str(lead.get("Is Remote","")).lower() == "true"
    company_ctx = ""
    if ctype == "Startup" and stage and "Unknown" not in stage:
        company_ctx = f"{stage}-funded startup"
    elif ctype == "Tier-1":
        company_ctx = "leading firm in its field"
    elif ctype == "Research":
        company_ctx = "research/policy organisation"
    else:
        company_ctx = ctype or "organisation"

    remote_note = (
        "\nNOTE: This is a remote-friendly role/organisation. "
        "Mention Tanmay's immediate remote availability naturally — don't force it."
    ) if remote else ""

    # India: direct self-intro structure, no framework section in the prompt
    # at all — including an unused "EMAIL FRAMEWORK TO USE" block tends to
    # leak structural artifacts into the output even when told to ignore it.
    if region == "IN":
        system_instruction = SYSTEM_INSTRUCTION_IN
        framework_block = ""
    else:
        system_instruction = SYSTEM_INSTRUCTION_INTL
        framework_block = f"""

EMAIL FRAMEWORK TO USE: {fw_name}
{fw_inst}"""

    return f"""{system_instruction}

---
ABOUT THE RECIPIENT:
Name:         {lead.get("Target Name","")}
Title:        {lead.get("Position","")}
Company:      {lead.get("Company","")} ({company_ctx})
Location:     {lead.get("Location","")}
Company bio:  {lead.get("Company Description","(not available)")}
Why targeted: {lead.get("Reason for Outreach","")}
{remote_note}

ABOUT THE SENDER (use selectively — do NOT paste all of this in):
Name:         {TANMAY["name"]}
Degree:       {TANMAY["degree"]}
IB Score:     {TANMAY["ib"]}
Current:      {TANMAY["current_role"]} — {TANMAY["current_work"]}
Unique edge:  {TANMAY["edge"]}
Founded:      {TANMAY["founder"]}
Research:     {TANMAY["research"]}
Awards:       {TANMAY["awards"]}
Goal:         {ctx["goal"]}
Availability: {ctx["availability"]}
Ask:          {ctx["ask"]}
CTA wording:  {ctx["cta"]}
Email:        {TANMAY["email"]}
{framework_block}

{tone_inst}

SUBJECT LINE RULE FOR THIS REGION (overrides any generic subject guidance above):
{subj_inst}

Write the email now. Output only JSON: {{"subject_line": "...", "email_body": "..."}}"""


# ══════════════════════════════════════════════════════════════════════════
# GROQ API CALL (Replaces Gemini)
# ══════════════════════════════════════════════════════════════════════════

import urllib.request, urllib.error

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

_GROQ_SYSTEM_PROMPT = """You are a precision email-drafting engine, not a creative writer. You generate ONE cold email per call based strictly on the structural, tonal, and regional rules given in the user message — you do not add your own structure, flourishes, or framework preferences on top of what's specified.

HARD OUTPUT CONTRACT (violating any of these makes the output unusable, not just imperfect):
1. Output ONLY a single valid JSON object: {"subject_line": "...", "email_body": "..."}
   - No markdown code fences. No ```json. No preamble like "Here's the email:". No trailing commentary.
   - The response must be parseable by json.loads() with zero post-processing.
2. email_body must be under 150 words. Count before you finalize — if you're unsure, cut a sentence rather than risk going over.
3. Never include the banned words/phrases listed in the user message — check your draft against that list before outputting, not after.
4. Follow the REGION's structure exactly as specified in the user message — if it says "no framework, direct self-introduction," do not apply a rhetorical framework anyway. If it says "use framework X," do not substitute a different one.
5. First person only ("I", "my", "me") — Tanmay is the narrator, not a third party describing him.
6. Do not invent facts about Tanmay or the recipient beyond what's given in the user message. No fabricated shared connections, no invented company details, no assumed mutual acquaintances.

You will be given the recipient profile, sender profile, regional tone rules, and (if applicable) a structural framework — in the user message. Follow those exactly. Do not override them with your own judgment of what makes a "better" email."""

def _call_groq(prompt: str, retries: int = 3) -> Optional[dict]:
    if not GROQ_API_KEY:
        log.error("GROQ_API_KEY not set")
        return None

    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = json.dumps({
        "model": "openai/gpt-oss-120b",
        "messages": [
            {"role": "system", "content": _GROQ_SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"}
    }).encode()

    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())

            raw = data["choices"][0]["message"]["content"].strip()
            # gpt-oss-120b occasionally wraps JSON in ```json fences even with
            # response_format={"type":"json_object"} set — strip defensively
            # rather than letting json.loads() throw on a clean response.
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw)
            parsed = json.loads(raw)
            
            assert "subject_line" in parsed and "email_body" in parsed, "Missing keys"

            word_count = len(parsed["email_body"].split())
            if word_count > 160:
                log.warning("Body %d words — requesting tighter rewrite", word_count)
                time.sleep(3)
                tighten = (
                    f"This is {word_count} words. Cut to ≤150 words. Return ONLY JSON: "
                    f"{{\"subject_line\": \"{parsed['subject_line']}\", \"email_body\": \"...\"}}\n\n"
                    f"Original:\n{parsed['email_body']}"
                )
                return _call_groq(tighten, retries=1)

            return parsed

        except urllib.error.HTTPError as e:
            log.error("Groq HTTP %d: %s", e.code, e.read().decode()[:200])
            time.sleep(5)
        except Exception as e:
            log.error("Groq error attempt %d: %s", attempt, e)
            time.sleep(5)

    return None


# ══════════════════════════════════════════════════════════════════════════
# PUBLIC INTERFACE
# ══════════════════════════════════════════════════════════════════════════

def draft_email(lead: dict) -> Optional[dict]:
    if lead.get("Drafted Email Body","").strip():
        return {
            "subject_line": lead.get("Drafted Email Subject",""),
            "email_body":   lead.get("Drafted Email Body",""),
        }

    fw_name, _ = _choose_framework(lead)
    fw_label = fw_name or "direct-IN"
    log.info("Drafting [%s|%s] %s @ %s",
             fw_label, _region(lead), lead.get("Target Name","?"), lead.get("Company","?"))

    prompt = _build_prompt(lead)
    result = _call_groq(prompt)

    if result:
        wc = len(result["email_body"].split())
        log.info("✓ Draft done — %d words | Subject: %s", wc, result["subject_line"])
    else:
        log.error("Draft failed for %s @ %s", lead.get("Target Name"), lead.get("Company"))

    return result


def run_drafting_pipeline(leads: list) -> list:
    """
    Drafts emails for up to DRAFTS_PER_RUN (8) leads per call, regardless
    of how many leads are passed in. Leads beyond the cap are returned
    untouched (still "Pending", no draft) so the caller can pick them up
    on the next scheduled run rather than losing them.
    """
    if len(leads) > DRAFTS_PER_RUN:
        log.info("Capping run at %d drafts (got %d leads) — remainder rolls to next run",
                  DRAFTS_PER_RUN, len(leads))

    to_draft, deferred = leads[:DRAFTS_PER_RUN], leads[DRAFTS_PER_RUN:]

    enriched = []
    for i, lead in enumerate(to_draft, 1):
        log.info("[%d/%d] Drafting for %s", i, len(to_draft), lead.get("Target Name","?"))
        result = draft_email(lead)
        if result:
            lead["Drafted Email Subject"] = result["subject_line"]
            lead["Drafted Email Body"]    = result["email_body"]
        enriched.append(lead)

        # Groq free tier allows 30 requests/min. A 2.5s gap keeps us perfectly safe.
        if i < len(to_draft):
            time.sleep(2.5)

    return enriched + deferred


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    test = {
        "Target Name":        "Rahul Mathur",
        "Company":            "Sequoia Capital India",
        "Position":           "Principal",
        "Location":           "Mumbai, India",
        "Geo Segment":        "IN_MUM",
        "Vertical":           "PE_VC",
        "Company Type":       "Tier-1",
        "Funding Stage":      "",
        "Is Remote":          "False",
        "Outreach Mode":      "internship",
        "Company Description":"Sequoia Capital India backs exceptional founders building legendary companies across India and Southeast Asia.",
        "Reason for Outreach":"Principal at Sequoia India — decision-maker for portfolio strategy and team expansion; Tanmay's KPMG analytics + economics research directly applicable.",
        "Title Tier":         "B",
        "Drafted Email Body": "",
    }
    r = draft_email(test)
    if r:
        print(f"\nSubject: {r['subject_line']}")
        print(f"\nBody ({len(r['email_body'].split())} words):\n{r['email_body']}")
