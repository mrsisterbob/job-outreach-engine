"""
resume_engine.py
================
High-Performance In-Memory Resume Compiler for Kevin Miller.
Translates structured profile data and Gemini ATS bullets into an ATS-compliant PDF via Typst (<30ms).
All factual content (experience, education, skills, approved bullets) is sourced from evidence_bank.json
so nothing here is ever invented independently of the centralized Evidence Bank.
"""

import io
import json
import os
import re
import typst

def escape_typst(text: str) -> str:
    """
    Escapes Typst markup reserved characters to prevent compilation syntax exceptions.
    Order is critical: backslashes must be escaped before structural syntax symbols.
    """
    if not text:
        return ""
    clean = str(text).replace("\\", "\\\\")
    for char in ["#", "$", "[", "]", "*", "_", "<", ">", "@"]:
        clean = clean.replace(char, f"\\{char}")
    return clean

EVIDENCE_BANK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence_bank.json")

# Minimal safe fallback if evidence_bank.json is ever missing/corrupt - keeps PDF compilation alive.
_FALLBACK_EVIDENCE_BANK = {
    "identity": {
        "name": "Kevin Miller", "email": "kjmiller406@gmail.com", "phone": "248-709-6326",
        "location": "Detroit, MI", "website": "montelattice.com", "linkedin": "linkedin.com/in/kevinmiller"
    },
    "experience": [], "education": [], "technical_skills": [],
    "banned_words": [], "pre_approved_bullets": {"a": [], "b": []}, "evergreen_highlights": []
}

def load_evidence_bank() -> dict:
    """Loads the centralized fact bank from disk. Falls back to a minimal safe stub on any failure
    so a missing/corrupt evidence_bank.json never crashes resume compilation.
    """
    try:
        with open(EVIDENCE_BANK_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _FALLBACK_EVIDENCE_BANK

EVIDENCE_BANK = load_evidence_bank()

# Persona framing only (subtitle/keywords/skills prose) - every skill named here must already
# exist in EVIDENCE_BANK["technical_skills"]; factual content (jobs, dates, bullets) lives in the bank.
TRACKS = {
    "a": {
        "subtitle": "Financial Systems & Operations",
        "keywords": ("Wealth Operations", "Process Automation", "Python", "SQL", "Salesforce", "Reconciliation"),
        "skills": [
            ("Operations & Data", "High-Volume Reconciliation, Variance Analysis, Audit Escalation, Power BI (ETL/Modeling), SQL, Advanced Excel."),
            ("Systems & Tools", "HubSpot CRM, Schwab Advisor Center, REST APIs, JSON Schema Design, Fidelity Wealthscape.")
        ]
    },
    "b": {
        "subtitle": "Data & Systems Engineering",
        "keywords": ("Python", "SQL", "REST APIs", "ETL", "Schema Architecture", "Process Automation"),
        "skills": [
            ("Engineering & Data", "Python, SQL, REST API Integration, ETL Modeling & Schema Design, Process Automation, Power BI."),
            ("Systems & Tools", "HubSpot CRM, Schwab Advisor Center, Fidelity Wealthscape, SQLite (WAL mode), Flask, Telegram Bot API Webhooks.")
        ]
    }
}

def filter_ats_bullets(dynamic_bullets: list, track: str = "a") -> list:
    """Grounds Gemini's ats_bullets against the Evidence Bank before they ever reach the PDF:
    drops any bullet containing a banned buzzword, and drops any bullet that isn't itself
    pre-approved AND doesn't reference at least one known technical_skills term (i.e. it would
    otherwise be an unverifiable/hallucinated claim). Backfills with pre-approved bullets if
    Gemini's output was empty or fully rejected, so the resume never renders fewer than 2 bullets.
    """
    track_key = str(track or "a").lower()
    approved_pool = EVIDENCE_BANK.get("pre_approved_bullets", {}).get(track_key) \
        or EVIDENCE_BANK.get("pre_approved_bullets", {}).get("a", [])
    known_skills = [str(s).lower() for s in EVIDENCE_BANK.get("technical_skills", [])]
    banned = [str(w).lower() for w in EVIDENCE_BANK.get("banned_words", [])]
    approved_lower = [str(b).strip().lower() for b in approved_pool]

    validated = []
    for raw in (dynamic_bullets or [])[:2]:
        clean = re.sub(r'^[•\-\*]\s*', '', str(raw).strip())
        if not clean:
            continue
        lower = clean.lower()
        if any(bw in lower for bw in banned):
            continue  # hallucination-prone buzzword - drop outright, never patch/repair it
        is_preapproved = lower in approved_lower
        mentions_known_skill = any(skill in lower for skill in known_skills if skill)
        if is_preapproved or mentions_known_skill:
            validated.append(clean)

    idx = 0
    while len(validated) < 2 and idx < len(approved_pool):
        candidate = approved_pool[idx]
        if candidate not in validated:
            validated.append(candidate)
        idx += 1

    return validated[:2]

def _render_experience_block(evidence: dict) -> str:
    """Renders the Professional Experience section entirely from Evidence Bank data - every
    injected field is escape_typst()'d since none of this is a hardcoded literal anymore.
    """
    lines = ["== Professional Experience"]
    for job in evidence.get("experience", []):
        title = escape_typst(job.get("title", ""))
        company = escape_typst(job.get("company", ""))
        location = escape_typst(job.get("location", ""))
        start = escape_typst(job.get("start", ""))
        end = escape_typst(job.get("end", ""))
        lines.append(f"*{title}* | {company} #h(1fr) {location} ({start} -- {end})")
        for b in job.get("bullets", []):
            lines.append(f"- {escape_typst(b)}")
    return "\n".join(lines)

def _render_education_block(evidence: dict) -> str:
    """Renders the Education & Certifications section entirely from Evidence Bank data."""
    lines = ["== Education & Certifications"]
    for edu in evidence.get("education", []):
        school = escape_typst(edu.get("school", ""))
        degree = escape_typst(edu.get("degree", ""))
        start = escape_typst(edu.get("start", ""))
        end = escape_typst(edu.get("end", ""))
        lines.append(f"*{school}* | {degree} #h(1fr) ({start} -- {end})")
        creds = edu.get("credentials", [])
        if creds:
            cred_str = ", ".join(escape_typst(c) for c in creds)
            lines.append(f"- *Licenses & Credentials:* {cred_str}")
    return "\n".join(lines)

def render_typst_markup(company_name: str, dynamic_bullets: list, track: str = "a") -> str:
    """Builds Typst markup for the selected persona track, sourcing every factual claim
    (experience, education, approved bullets) from the centralized Evidence Bank."""
    track_data = TRACKS.get(str(track or "a").lower(), TRACKS["a"])
    evidence = EVIDENCE_BANK
    identity = evidence.get("identity", {})
    clean_company = escape_typst(company_name or "Target Operations")

    validated_bullets = filter_ats_bullets(dynamic_bullets, track)
    dynamic_bullets_block = "\n".join(f"  - {escape_typst(b)}" for b in validated_bullets)
    evergreen_block = "\n".join(
        f"- *{escape_typst(h.get('label', ''))}:* {escape_typst(h.get('text', ''))}"
        for h in evidence.get("evergreen_highlights", [])
    )

    experience_block = _render_experience_block(evidence)
    education_block = _render_education_block(evidence)
    skills_block = "\n".join(f"- *{cat}:* {desc}" for cat, desc in track_data["skills"])
    keywords_tuple = ", ".join(f'"{kw}"' for kw in track_data["keywords"])

    name = escape_typst(identity.get("name", "Kevin Miller"))
    email = escape_typst(identity.get("email", ""))
    phone = escape_typst(identity.get("phone", ""))
    location = escape_typst(identity.get("location", ""))
    website = escape_typst(identity.get("website", ""))
    linkedin = identity.get("linkedin", "linkedin.com/in/kevinmiller")

    markup = f"""
#set document(
  title: "Kevin Miller - Resume - {clean_company}",
  author: "Kevin Miller",
  date: auto,
  keywords: ({keywords_tuple})
)

#set page(paper: "us-letter", margin: (x: 0.55in, top: 0.45in, bottom: 0.45in))
#set text(font: "Liberation Sans", size: 9.5pt)
#set par(justify: false, leading: 0.52em)

#align(center)[
  #text(size: 15pt, weight: "bold")[{name}] \\
  #text(size: 10pt, weight: "medium", fill: rgb("#1B2A4A"))[{track_data["subtitle"]}] \\
  #v(2pt)
  {email} | {phone} | {location} | {website} | [{escape_typst(linkedin)}](https://{linkedin})
]

#v(4pt)
#line(length: 100%, stroke: 0.6pt + rgb("#CCCCCC"))
#v(2pt)

{experience_block}

== Targeted Systems & Project Highlights ({clean_company})
{dynamic_bullets_block}
{evergreen_block}

{education_block}

== Technical Systems & Core Skills
{skills_block}
"""
    return markup.strip()

def compile_resume_pdf(company_name: str, dynamic_bullets: list, track: str = "a") -> bytes:
    """Compiles the Typst markup string directly into PDF bytes in memory for the selected persona track."""
    markup = render_typst_markup(company_name, dynamic_bullets, track)
    return typst.compile(markup.encode("utf-8"))

