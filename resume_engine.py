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
import re
import typst

# Company Conservatism & Culture Filter: crypto/Web3 language Gemini might otherwise route into
# a conservative-tone resume (RIAs, banks, custodians) gets scrubbed to institutional-safe phrasing.
_CRYPTO_TERMS_PATTERN = re.compile(r"\b(bitcoin|crypto(?:currency)?|tokeniz\w*|web3|blockchain|trading bots?)\b", re.IGNORECASE)

TONE_SKILL_ADDENDUM = {
    "conservative": ("Compliance & Data Integrity", "Custodial Systems, Data Reconciliation, SEC Compliance."),
    "tech": ("Modern Engineering & Automation", "Asset Tokenization, API Integration, Flask, Process Automation."),
}

def apply_tone_filter(text: str, tone_mode: str) -> str:
    """Scrubs crypto/Web3 keywords to institutional-safe phrasing when tone_mode is 'conservative';
    passes text through unchanged for 'tech' (or any other) tone_mode.
    """
    if str(tone_mode or "").lower() != "conservative" or not text:
        return text
    return _CRYPTO_TERMS_PATTERN.sub("custodial systems", text)

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

# Indices within specific (track, tone_mode) pairs that contain Web3/crypto/tokenization
# references - excluded whenever tone_mode is "conservative", regardless of what Gemini routed.
TRACK_TONE_CONSTRAINTS = {
    ("c", "conservative"): [2],  # e.g., Form D digital asset/tokenization bullet
}
_SAFE_FALLBACK_INDICES = [0, 1, 3]

# Persona framing only (subtitle/keywords/skills prose) - every skill named here must already
# exist in evidence_bank.json's technical_skills; factual content (jobs, dates, bullets) lives in the bank.
TRACKS = {
    "a": {
        "subtitle": "Financial Systems & Operations",
        "keywords": ("Wealth Operations", "Process Automation", "Python", "SQL", "Salesforce", "Reconciliation"),
        "summary": "Operations specialist with experience spanning custodial reconciliations, regulatory compliance, and CRM pipeline automation. Skilled in applying Python, SQL, and system integrations across Salesforce and HubSpot to remove manual bottlenecks, validate institutional data, and streamline complex financial workflows.",
        "skills": [
            ("Operations & Data", "High-Volume Reconciliation, Variance Analysis, Audit Escalation, Power BI (ETL/Modeling), SQL, Advanced Excel."),
            ("Systems & Tools", "Schwab Advisor Center, Fidelity Wealthscape, Salesforce, HubSpot CRM, DocuSign.")
        ]
    },
    "b": {
        "subtitle": "Data & Systems Engineering",
        "keywords": ("Python", "SQL", "REST APIs", "ETL", "Schema Architecture", "Process Automation"),
        "summary": "Operations specialist with hands-on experience building Python and SQL tools that automate reconciliation, validate structured data feeds, and integrate REST APIs across CRM and custodial systems. Comfortable owning a problem from schema design through production deployment in fast-moving financial environments.",
        "skills": [
            ("Engineering & Data", "Python, SQL, REST API Integration, ETL Modeling & Schema Design, Process Automation, Power BI."),
            ("Systems & Tools", "HubSpot CRM, Salesforce, Schwab Advisor Center, Fidelity Wealthscape, Typst.")
        ]
    },
    "c": {
        "subtitle": "Risk & Regulatory Compliance",
        "keywords": ("Regulatory Compliance", "SEC/FinCEN Filings", "Risk Management", "DocuSign", "Salesforce", "Audit Controls"),
        "summary": "Operations specialist with experience across SEC and FinCEN regulatory filings, custodial compliance audits, and DocuSign workflow validation for financial advisory teams. Focused on building controls that catch risk exposure early and keep institutional accounts audit-ready.",
        "skills": [
            ("Compliance & Risk", "SEC/FinCEN Regulatory Filings, RIA Compliance Audits, DocuSign Workflow Validation, Suitability Review, Risk Escalation Controls."),
            ("Systems & Tools", "Salesforce, Schwab Advisor Center, Fidelity Wealthscape, Orion Eclipse, Excel.")
        ]
    },
    "d": {
        "subtitle": "Business Intelligence & Analytics",
        "keywords": ("Power BI", "SQL", "Data Analytics", "Variance Analysis", "Reporting", "Excel"),
        "summary": "Operations specialist with experience building Power BI dashboards, SQL-driven variance analysis, and Excel reporting models that turn raw operational data into decisions leadership can act on. Comfortable translating messy financial data sets into clear, repeatable reporting pipelines.",
        "skills": [
            ("Analytics & Reporting", "Power BI Dashboard Design, SQL Aggregation & Variance Analysis, Advanced Excel Modeling, Executive Reporting."),
            ("Systems & Tools", "Salesforce, HubSpot CRM, Schwab Advisor Center, Fidelity Wealthscape.")
        ]
    },
    "e": {
        "subtitle": "Business Operations & CRM Systems",
        "keywords": ("Business Operations", "Salesforce", "HubSpot CRM", "Process Automation", "Ticket Routing", "Python"),
        "summary": "Operations specialist with experience redesigning ticket routing, CRM workflows, and cross-team escalation processes across Salesforce and HubSpot. Focused on removing friction from day-to-day operations so teams spend less time on manual triage and more time on work that matters.",
        "skills": [
            ("Operations & Process", "Ticket Routing & Workflow Redesign, Process Automation, Cross-Team Coordination, Escalation Handling."),
            ("Systems & Tools", "Salesforce, HubSpot CRM, Python, DocuSign, Schwab Advisor Center.")
        ]
    }
}

def filter_ats_bullets(track: str = "a", bullet_indices: list = None, tone_mode: str = "conservative") -> list:
    """Resolves the actual bullet strings for a track + list of pool indices. Gemini only ever
    routes a track letter and integer indices (Strict Deterministic Template Engine) - it never
    authors bullet text itself, so there is nothing to "validate" beyond bounds-checking.
    Defaults to [0, 1, 2] if bullet_indices is omitted, not a list of ints, or contains any
    out-of-range index. Still screens against evidence_bank.json's banned_words as a
    defense-in-depth guard in case a manual /edit mutation ever introduces one. Reloads both
    banks from disk on every call (hot-reload).
    `tone_mode` applies the Company Conservatism & Culture Filter at bullet-selection time: any
    index flagged in TRACK_TONE_CONSTRAINTS for (track, tone_mode) is dropped and backfilled from
    _SAFE_FALLBACK_INDICES, so a conservative-tone resume never surfaces a Web3/crypto bullet even
    if Gemini's routed indices included one.
    """
    evidence_bank = load_evidence_bank()
    resume_bullets_bank = load_resume_bullets_bank()
    track_key = str(track or "a").lower()
    tone_key = str(tone_mode or "conservative").lower()
    pool_key = TRACK_BULLET_POOL_KEYS.get(track_key, TRACK_BULLET_POOL_KEYS["a"])
    pool = resume_bullets_bank.get(pool_key) or resume_bullets_bank.get(TRACK_BULLET_POOL_KEYS["a"], [])
    banned = [str(w).lower() for w in evidence_bank.get("banned_words", [])]

    is_valid = (
        isinstance(bullet_indices, list) and len(bullet_indices) > 0
        and all(isinstance(i, int) and 0 <= i < len(pool) for i in bullet_indices)
    )
    indices = bullet_indices if is_valid else [0, 1, 2]
    indices = [i for i in indices if 0 <= i < len(pool)]

    forbidden = TRACK_TONE_CONSTRAINTS.get((track_key, tone_key), [])
    if forbidden and any(i in forbidden for i in indices):
        original_len = len(indices)
        indices = [i for i in indices if i not in forbidden]
        for fallback_i in _SAFE_FALLBACK_INDICES:
            if len(indices) >= original_len:
                break
            if fallback_i not in indices and fallback_i not in forbidden and 0 <= fallback_i < len(pool):
                indices.append(fallback_i)
        if not indices:
            indices = [0]

    selected = [pool[i] for i in indices if not any(bw in str(pool[i]).lower() for bw in banned)]
    return selected or pool[:3]

def _render_experience_block(evidence: dict, dynamic_bullets: list = None) -> str:
    """Renders the Professional Experience section entirely from Evidence Bank data - every
    injected field is escape_typst()'d since none of this is a hardcoded literal anymore.
    `dynamic_bullets`, if given, entirely replaces the first job's (Signal Advisors) static
    bullets instead of stacking on top of them, so the track-routed bullets lead the section
    without duplicating the static ones.
    """
    lines = []
    for idx, job in enumerate(evidence.get("experience", [])):
        title = escape_typst(job.get("title", ""))
        company = escape_typst(job.get("company", ""))
        location = escape_typst(job.get("location", ""))
        start = escape_typst(job.get("start", ""))
        end = escape_typst(job.get("end", ""))
        if idx > 0:
            lines.append("#v(3pt)")
        lines.append(f"*{title}* | {company} #h(1fr) {location} | {start} -- {end}")
        bullets = dynamic_bullets if (idx == 0 and dynamic_bullets) else job.get("bullets", [])
        for b in bullets:
            lines.append(f"- {escape_typst(b)}")
    return "\n".join(lines)

def _render_education_credentials_block(evidence: dict) -> str:
    """Renders the combined Education & Credentials section - degrees from evidence_bank's
    `education` list followed by a single comma-joined line of `certificates`.
    """
    lines = []
    for idx, edu in enumerate(evidence.get("education", [])):
        school = escape_typst(edu.get("school", ""))
        degree = escape_typst(edu.get("degree", ""))
        location = escape_typst(edu.get("location", ""))
        start = escape_typst(edu.get("start", ""))
        end = escape_typst(edu.get("end", ""))
        if idx > 0:
            lines.append("#v(2pt)")
        lines.append(f"*{degree}*, {school} #h(1fr) {location} | {start} -- {end}")

    certificates_line = ", ".join(escape_typst(c) for c in evidence.get("certificates", []))
    if certificates_line:
        lines.append("#v(3pt)")
        lines.append(f"*Certificates & Licenses:* {certificates_line}")
    return "\n".join(lines)

def render_typst_markup(company_name: str, track: str = "a", bullet_indices: list = None, tone_mode: str = "conservative") -> str:
    """Builds single-page Typst markup for the selected persona track, sourcing every factual
    claim (experience, education, certificates) from the centralized JSON banks (hot-reloaded
    fresh on every call), assembled into 4 sections: Summary, Professional Experience,
    Education & Credentials, Skills & Systems. Dynamic 30% customization is entirely
    track-driven (a-e, already routed by Gemini as an SDTE integer/letter, never free text):
    the header summary, the leading achievement bullet, and skill emphasis all key off the
    same `track` value, so no live job-description text is required at render time - the
    resume still compiles correctly even from a bare "a" default with no cached job.
    `tone_mode` ("conservative" | "tech") is the Company Conservatism & Culture Filter: it scrubs
    crypto/Web3 language from the summary for conservative firms (RIAs, banks, custodians) and
    appends tone-appropriate chips to the Skills & Systems line.
    """
    track_data = TRACKS.get(str(track or "a").lower(), TRACKS["a"])
    tone_key = str(tone_mode or "conservative").lower()
    if tone_key not in TONE_SKILL_ADDENDUM:
        tone_key = "conservative"
    evidence = load_evidence_bank()
    identity = evidence.get("identity", {})
    clean_company = escape_typst(company_name or "Target Operations")

    selected_bullets = filter_ats_bullets(track, bullet_indices, tone_key)

    keywords_tuple = ", ".join(f'"{kw}"' for kw in track_data["keywords"])
    summary = escape_typst(apply_tone_filter(track_data["summary"], tone_key))

    name = escape_typst(identity.get("name", "Kevin Miller"))
    email = escape_typst(identity.get("email", ""))
    phone = escape_typst(identity.get("phone", ""))
    location = escape_typst(identity.get("location", ""))
    website_raw = str(identity.get("website", "") or "")
    linkedin_raw = str(identity.get("linkedin", "linkedin.com/in/kevinmiller") or "")
    website_label = escape_typst(website_raw)

    # Native Typst #link()[] syntax (never markdown [text](url)) - pipe-joined, skipping blank fields
    # so a missing phone/website never leaves a stray " | | " gap in the header.
    contact_fields = [f for f in (email, phone, location) if f]
    if website_raw:
        contact_fields.append(f'#link("https://{website_raw}")[{website_label}]')
    if linkedin_raw:
        contact_fields.append(f'#link("https://{linkedin_raw}")[LinkedIn]')
    contact_line = " • ".join(contact_fields)

    experience_block = _render_experience_block(evidence, dynamic_bullets=selected_bullets)
    education_credentials_block = _render_education_credentials_block(evidence)
    tone_skills = list(track_data["skills"]) + [TONE_SKILL_ADDENDUM[tone_key]]
    skills_lines = "\n".join(f"*{escape_typst(label)}:* {escape_typst(desc)}" for label, desc in tone_skills)

    markup = f"""
#set document(
  title: "Kevin Miller - Resume - {clean_company}",
  author: "Kevin Miller",
  date: auto,
  keywords: ({keywords_tuple})
)

// Marcus Thorne spacing and typography - tuned to fill the full 1-page canvas edge-to-edge
#set page(paper: "us-letter", margin: (x: 0.65in, top: 0.5in, bottom: 0.5in))
#set text(font: "Liberation Sans", size: 9.6pt, fill: rgb("#111827"))
#set par(justify: false, leading: 0.5em, spacing: 0.58em)
#set list(spacing: 0.38em, indent: 0em)
#show heading: set block(above: 0.55em, below: 0.3em)

// --- HEADER ---
#align(center)[
  #text(size: 18pt, weight: "bold", fill: rgb("#000000"))[{name}] \\
  #text(size: 10pt, weight: "medium", fill: rgb("#4B5563"))[{escape_typst(track_data["subtitle"])}] \\
  #v(3pt)
  #text(size: 8.8pt, fill: rgb("#6B7280"))[{contact_line}]
]

#v(5pt)
#line(length: 100%, stroke: 0.7pt + rgb("#CCCCCC"))
#v(3pt)

// --- SUMMARY ---
#text(size: 8.3pt, weight: "bold", tracking: 1.1pt, fill: rgb("#374151"))[SUMMARY]
#v(2pt)
{summary}

#v(5pt)
#line(length: 100%, stroke: 0.5pt + rgb("#E5E7EB"))
#v(3pt)

// --- PROFESSIONAL EXPERIENCE ---
#text(size: 8.3pt, weight: "bold", tracking: 1.1pt, fill: rgb("#374151"))[PROFESSIONAL EXPERIENCE]
#v(2pt)
{experience_block}

#v(5pt)
#line(length: 100%, stroke: 0.5pt + rgb("#E5E7EB"))
#v(3pt)

// --- EDUCATION & CREDENTIALS ---
#text(size: 8.3pt, weight: "bold", tracking: 1.1pt, fill: rgb("#374151"))[EDUCATION & CREDENTIALS]
#v(2pt)
{education_credentials_block}

#v(5pt)
#line(length: 100%, stroke: 0.5pt + rgb("#E5E7EB"))
#v(3pt)

// --- SKILLS & SYSTEMS ---
#text(size: 8.3pt, weight: "bold", tracking: 1.1pt, fill: rgb("#374151"))[SKILLS & SYSTEMS]
#v(2pt)
{skills_lines}
"""
    return markup.strip()

def compile_resume_pdf(company_name: str, track: str = "a", bullet_indices: list = None, tone_mode: str = "conservative") -> bytes:
    """Compiles the Typst markup string directly into PDF bytes in memory for the selected persona track."""
    markup = render_typst_markup(company_name, track, bullet_indices, tone_mode)
    return typst.compile(markup.encode("utf-8"))

