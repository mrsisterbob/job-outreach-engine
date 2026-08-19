"""
resume_engine.py
================
High-Performance In-Memory Resume Compiler for Kevin Miller.
Strict Deterministic Template Engine (SDTE): resumes are assembled entirely from local JSON
banks (evidence_bank.json, resume_bullets_bank.json). Gemini never authors bullet prose here -
it only ever selects a track letter (a-e) and a list of pool indices, which this module resolves
and bounds-checks against the actual bullet pool before rendering.
"""

import io
import json
import logging
import os
import typst

def escape_typst(text: str) -> str:
    """
    Escapes Typst markup reserved characters to prevent compilation syntax exceptions.
    Order is critical: backslashes must be escaped before structural syntax symbols.
    """
    if text is None or text == "":
        return ""
    clean = str(text).replace("\\", "\\\\")
    for char in ["#", "$", "[", "]", "*", "_", "<", ">", "@"]:
        clean = clean.replace(char, f"\\{char}")
    return clean

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EVIDENCE_BANK_PATH = os.path.join(BASE_DIR, "evidence_bank.json")
RESUME_BULLETS_BANK_PATH = os.path.join(BASE_DIR, "resume_bullets_bank.json")

# Minimal safe fallback if evidence_bank.json is ever missing/corrupt - keeps PDF compilation alive.
_FALLBACK_EVIDENCE_BANK = {
    "identity": {
        "name": "Kevin Miller", "email": "kjmiller406@gmail.com", "phone": "248-709-6326",
        "location": "Detroit, MI", "website": "montelattice.com", "linkedin": "linkedin.com/in/kevinmiller"
    },
    "experience": [], "education": [], "technical_skills": [], "banned_words": []
}

# Minimal safe fallback if resume_bullets_bank.json is ever missing/corrupt - keeps PDF compilation alive.
_FALLBACK_RESUME_BULLETS_BANK = {
    "track_a_wealth_ops": ["Reconciled high-volume data variances and mapped ownership structures to establish risk escalation logic."],
    "track_b_engineering": ["Designed and scripted ETL pipelines and schema validation logic to automate high-volume data reconciliation."],
    "track_c_risk_compliance": ["Audited compliance documentation to enforce regulatory standards prior to execution."],
    "track_d_business_intelligence": ["Built reporting pipelines to translate raw operational data into executive insights."],
    "track_e_bizops": ["Automated routine data extraction and workflow tasks to reduce manual administrative overhead."]
}

def load_json(path: str, fallback: dict) -> dict:
    """Generic JSON bank loader with a safe try/except fallback: logs an error and returns the
    supplied stub dict if the file is missing or fails to parse, so a bad/absent JSON file never
    crashes resume compilation. Called fresh on every request (no module-level cache) so manual
    edits - including from the /edit Telegram command - apply instantly, with no server restart.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"JSON bank load failed for {path}, using fallback stub: {e}")
        return fallback

def load_evidence_bank() -> dict:
    return load_json(EVIDENCE_BANK_PATH, _FALLBACK_EVIDENCE_BANK)

def load_resume_bullets_bank() -> dict:
    return load_json(RESUME_BULLETS_BANK_PATH, _FALLBACK_RESUME_BULLETS_BANK)

# Maps track letters a-e to resume_bullets_bank.json's descriptive pool names.
TRACK_BULLET_POOL_KEYS = {
    "a": "track_a_wealth_ops",
    "b": "track_b_engineering",
    "c": "track_c_risk_compliance",
    "d": "track_d_business_intelligence",
    "e": "track_e_bizops",
}

# Persona framing only (subtitle/keywords/skills prose) - every skill named here must already
# exist in evidence_bank.json's technical_skills; factual content (jobs, dates, bullets) lives in the bank.
TRACKS = {
    "a": {
        "subtitle": "Financial Systems & Operations",
        "keywords": ("Wealth Operations", "Process Automation", "Python", "SQL", "Salesforce", "Reconciliation"),
        "skills": [
            ("Operations & Data", "High-Volume Reconciliation, Variance Analysis, Audit Escalation, Power BI (ETL/Modeling), SQL, Advanced Excel."),
            ("Systems & Tools", "HubSpot CRM, Schwab Advisor Center, Fidelity Wealthscape, DocuSign, Salesforce.")
        ]
    },
    "b": {
        "subtitle": "Data & Systems Engineering",
        "keywords": ("Python", "SQL", "REST APIs", "ETL", "Schema Architecture", "Process Automation"),
        "skills": [
            ("Engineering & Data", "Python, SQL, REST API Integration, ETL Modeling & Schema Design, Process Automation, Power BI."),
            ("Systems & Tools", "HubSpot CRM, Salesforce, Schwab Advisor Center, Fidelity Wealthscape, Typst.")
        ]
    },
    "c": {
        "subtitle": "Risk & Regulatory Compliance",
        "keywords": ("Regulatory Compliance", "SEC/FinCEN Filings", "Risk Management", "DocuSign", "Salesforce", "Audit Controls"),
        "skills": [
            ("Compliance & Risk", "SEC/FinCEN Regulatory Filings, RIA Compliance Audits, DocuSign Workflow Validation, Suitability Review, Risk Escalation Controls."),
            ("Systems & Tools", "Salesforce, Schwab Advisor Center, Fidelity Wealthscape, Orion Eclipse, Excel.")
        ]
    },
    "d": {
        "subtitle": "Business Intelligence & Analytics",
        "keywords": ("Power BI", "SQL", "Data Analytics", "Variance Analysis", "Reporting", "Excel"),
        "skills": [
            ("Analytics & Reporting", "Power BI Dashboard Design, SQL Aggregation & Variance Analysis, Advanced Excel Modeling, Executive Reporting."),
            ("Systems & Tools", "Salesforce, HubSpot CRM, Schwab Advisor Center, Fidelity Wealthscape.")
        ]
    },
    "e": {
        "subtitle": "Business Operations & CRM Systems",
        "keywords": ("Business Operations", "Salesforce", "HubSpot CRM", "Process Automation", "Ticket Routing", "Python"),
        "skills": [
            ("Operations & Process", "Ticket Routing & Workflow Redesign, Process Automation, Cross-Team Coordination, Escalation Handling."),
            ("Systems & Tools", "Salesforce, HubSpot CRM, Python, DocuSign, Schwab Advisor Center.")
        ]
    }
}

def filter_ats_bullets(track: str = "a", bullet_indices: list = None) -> list:
    """Resolves the actual bullet strings for a track + list of pool indices. Gemini only ever
    routes a track letter and integer indices (Strict Deterministic Template Engine) - it never
    authors bullet text itself, so there is nothing to "validate" beyond bounds-checking.
    Defaults to [0, 1, 2] if bullet_indices is omitted, not a list of ints, or contains any
    out-of-range index. Still screens against evidence_bank.json's banned_words as a
    defense-in-depth guard in case a manual /edit mutation ever introduces one. Reloads both
    banks from disk on every call (hot-reload).
    """
    evidence_bank = load_evidence_bank()
    resume_bullets_bank = load_resume_bullets_bank()
    track_key = str(track or "a").lower()
    pool_key = TRACK_BULLET_POOL_KEYS.get(track_key, TRACK_BULLET_POOL_KEYS["a"])
    pool = resume_bullets_bank.get(pool_key) or resume_bullets_bank.get(TRACK_BULLET_POOL_KEYS["a"], [])
    banned = [str(w).lower() for w in evidence_bank.get("banned_words", [])]

    is_valid = (
        isinstance(bullet_indices, list) and len(bullet_indices) > 0
        and all(isinstance(i, int) and 0 <= i < len(pool) for i in bullet_indices)
    )
    indices = bullet_indices if is_valid else [0, 1, 2]
    indices = [i for i in indices if 0 <= i < len(pool)]

    selected = [pool[i] for i in indices if not any(bw in str(pool[i]).lower() for bw in banned)]
    return selected or pool[:3]

def _render_experience_block(evidence: dict) -> str:
    """Renders the Professional Experience section entirely from Evidence Bank data - every
    injected field is escape_typst()'d since none of this is a hardcoded literal anymore.
    """
    lines = ["== Professional Experience"]
    for idx, job in enumerate(evidence.get("experience", [])):
        title = escape_typst(job.get("title", ""))
        company = escape_typst(job.get("company", ""))
        location = escape_typst(job.get("location", ""))
        start = escape_typst(job.get("start", ""))
        end = escape_typst(job.get("end", ""))
        if idx > 0:
            lines.append("#v(5pt)")
        lines.append(f"*{title}* | {company} #h(1fr) {location} | {start} -- {end}")
        for b in job.get("bullets", []):
            lines.append(f"- {escape_typst(b)}")
    return "\n".join(lines)

def _render_education_block(evidence: dict) -> str:
    """Renders the Education & Certifications section entirely from Evidence Bank data."""
    lines = ["== Education & Certifications"]
    for idx, edu in enumerate(evidence.get("education", [])):
        school = escape_typst(edu.get("school", ""))
        degree = escape_typst(edu.get("degree", ""))
        start = escape_typst(edu.get("start", ""))
        end = escape_typst(edu.get("end", ""))
        if idx > 0:
            lines.append("#v(5pt)")
        lines.append(f"*{school}* | {degree} #h(1fr) {start} -- {end}")
        creds = edu.get("credentials", [])
        if creds:
            cred_str = ", ".join(escape_typst(c) for c in creds)
            lines.append(f"- *Licenses & Credentials:* {cred_str}")
    return "\n".join(lines)

def render_typst_markup(company_name: str, track: str = "a", bullet_indices: list = None) -> str:
    """Builds single-column Typst markup for the selected persona track, sourcing every factual
    claim (experience, education, bullets) from the centralized JSON banks (hot-reloaded fresh on
    every call - see load_evidence_bank()/load_resume_bullets_bank()). Section order: Header ->
    Subtitle -> Targeted Systems Highlights -> Experience -> Education -> Skills.
    """
    track_data = TRACKS.get(str(track or "a").lower(), TRACKS["a"])
    evidence = load_evidence_bank()
    identity = evidence.get("identity", {})
    clean_company = escape_typst(company_name or "Target Operations")

    selected_bullets = filter_ats_bullets(track, bullet_indices)
    dynamic_bullets_block = "\n".join(f"- {escape_typst(b)}" for b in selected_bullets)

    experience_block = _render_experience_block(evidence)
    education_block = _render_education_block(evidence)
    skills_block = "\n".join(f"- *{cat}:* {desc}" for cat, desc in track_data["skills"])
    keywords_tuple = ", ".join(f'"{kw}"' for kw in track_data["keywords"])

    name = escape_typst(identity.get("name", "Kevin Miller"))
    email = escape_typst(identity.get("email", ""))
    phone = escape_typst(identity.get("phone", ""))
    location = escape_typst(identity.get("location", ""))
    website_raw = str(identity.get("website", "") or "")
    linkedin_raw = str(identity.get("linkedin", "linkedin.com/in/kevinmiller") or "")
    website_label = escape_typst(website_raw)
    linkedin_label = escape_typst(linkedin_raw)

    # Native Typst #link()[] syntax (never markdown [text](url)) - pipe-joined, skipping blank fields
    # so a missing phone/website never leaves a stray " | | " gap in the header.
    contact_fields = [f for f in (email, phone, location) if f]
    if website_raw:
        contact_fields.append(f'#link("https://{website_raw}")[{website_label}]')
    if linkedin_raw:
        contact_fields.append(f'#link("https://{linkedin_raw}")[{linkedin_label}]')
    contact_line = " | ".join(contact_fields)

    markup = f"""
#set document(
  title: "Kevin Miller - Resume - {clean_company}",
  author: "Kevin Miller",
  date: auto,
  keywords: ({keywords_tuple})
)

#set page(paper: "us-letter", margin: (x: 0.55in, top: 0.45in, bottom: 0.45in))
#set text(font: "Liberation Sans", size: 9.5pt)
#set par(justify: false, leading: 0.5em, spacing: 0.65em)
#set list(spacing: 0.38em, indent: 0em)
#show heading: set block(above: 0.85em, below: 0.4em)

#align(center)[
  #text(size: 15pt, weight: "bold")[{name}] \\
  #text(size: 10pt, weight: "medium", fill: rgb("#1B2A4A"))[{track_data["subtitle"]}] \\
  #v(2pt)
  {contact_line}
]

#v(4pt)
#line(length: 100%, stroke: 0.6pt + rgb("#CCCCCC"))
#v(2pt)

== Targeted Systems & Project Highlights ({clean_company})
{dynamic_bullets_block}

{experience_block}

{education_block}

== Technical Systems & Core Skills
{skills_block}
"""
    return markup.strip()

def compile_resume_pdf(company_name: str, track: str = "a", bullet_indices: list = None) -> bytes:
    """Compiles the Typst markup string directly into PDF bytes in memory for the selected persona track."""
    markup = render_typst_markup(company_name, track, bullet_indices)
    return typst.compile(markup.encode("utf-8"))

