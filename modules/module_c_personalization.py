"""
MODULE C: Personalization Engine — Gemini Pro
══════════════════════════════════════════════
Model: gemini-2.0-flash (free tier: 15 req/min, 1500 req/day — more than enough)
API:   Google AI Studio key (free, no billing required)
       → aistudio.google.com → Get API Key

Email philosophy:
  Every email uses one of 7 proven cold-email frameworks, chosen
  algorithmically based on the target's profile. This ensures no two emails
  feel alike, even to people at the same company.

  Frameworks:
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

Tone varies by region:
  IN → respectful-punchy; UK → measured confidence; US → direct value-first;
  AU → warm-casual; SG/ME → precise-formal; REMOTE → global professional.

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
    """Choose the best framework for this specific lead. Returns (name, instruction)."""
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

SYSTEM_INSTRUCTION = """You are writing a cold outreach email on behalf of Tanmay Kaper.
Your goal: make the recipient actually reply.

NON-NEGOTIABLE RULES:
1. Body: strictly ≤ 120 words. Count every word. Never exceed this.
2. Subject line: ≤ 8 words. Must create genuine curiosity or specific relevance.
   BANNED subjects: "Quick question", "Internship inquiry", "Following up", "Opportunity".
3. BANNED openers: "Hope this finds you well", "I came across your profile",
   "I wanted to reach out", "I am reaching out", "My name is Tanmay".
4. First sentence: must reference something SPECIFIC about THEIR company, work, or role.
   Generic observations are rejected. Company name alone is not specific enough.
5. Mention Tanmay's name naturally, once, mid-email — not in the opener.
6. One single, low-friction CTA at the close. No multiple asks.
7. Sign off: "— Tanmay" then next line "tanmay.kaper1401@gmail.com"
8. Output ONLY valid JSON: {"subject_line": "...", "email_body": "..."}
   No markdown. No explanation. No text outside the JSON object."""


def _build_prompt(lead: dict) -> str:
    mode      = lead.get("Outreach Mode", OUTREACH_MODE)
    ctx       = MODE_CONTEXT.get(mode, MODE_CONTEXT["internship"])
    region    = _region(lead)
    tone_inst = REGION_TONE.get(region, REGION_TONE["DEFAULT"])
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

    return f"""{SYSTEM_INSTRUCTION}

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

EMAIL FRAMEWORK TO USE: {fw_name}
{fw_inst}

{tone_inst}

Write the email now. Output only JSON: {{"subject_line": "...", "email_body": "..."}}"""


# ══════════════════════════════════════════════════════════════════════════
# GROQ API CALL (Replaces Gemini)
# ══════════════════════════════════════════════════════════════════════════

import urllib.request, urllib.error

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

def _call_groq(prompt: str, retries: int = 3) -> Optional[dict]:
    if not GROQ_API_KEY:
        log.error("GROQ_API_KEY not set")
        return None

    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = json.dumps({
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": "You are an expert copywriter. Output ONLY valid JSON."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.8,
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
            parsed = json.loads(raw)
            
            assert "subject_line" in parsed and "email_body" in parsed, "Missing keys"

            word_count = len(parsed["email_body"].split())
            if word_count > 130:
                log.warning("Body %d words — requesting tighter rewrite", word_count)
                time.sleep(3)
                tighten = (
                    f"This is {word_count} words. Cut to ≤120 words. Return ONLY JSON: "
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
    log.info("Drafting [%s|%s] %s @ %s",
             fw_name, _region(lead), lead.get("Target Name","?"), lead.get("Company","?"))

    prompt = _build_prompt(lead)
    result = _call_groq(prompt)

    if result:
        wc = len(result["email_body"].split())
        log.info("✓ Draft done — %d words | Subject: %s", wc, result["subject_line"])
    else:
        log.error("Draft failed for %s @ %s", lead.get("Target Name"), lead.get("Company"))

    return result


def run_drafting_pipeline(leads: list) -> list:
    enriched = []
    for i, lead in enumerate(leads, 1):
        log.info("[%d/%d] Drafting for %s", i, len(leads), lead.get("Target Name","?"))
        result = draft_email(lead)
        if result:
            lead["Drafted Email Subject"] = result["subject_line"]
            lead["Drafted Email Body"]    = result["email_body"]
        enriched.append(lead)
        
        # Groq free tier allows 30 requests/min. A 2.5s gap keeps us perfectly safe.
        if i < len(leads):
            import time
            time.sleep(2.5)
    return enriched


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
