"""
resume_engine.py
================
High-Performance In-Memory Resume Compiler for Kevin Miller.
Strict Deterministic Template Engine (SDTE): resumes are assembled entirely from local JSON
banks (evidence_bank.json, resume_bullets_bank.json). Gemini never authors bullet prose here -
it only ever selects a track letter (a-e) and a list of pool indices, which this module resolves
and bounds-checks against the actual bullet pool before rendering.
"""

import difflib
import io
import json
import logging
import os
import re
import typst

# Company Conservatism & Culture Filter: crypto/Web3 language Gemini might otherwise route into
# a conservative-tone resume (RIAs, banks, custodians) gets scrubbed to institutional-safe phrasing.
_CRYPTO_TERMS_PATTERN = re.compile(r"\b(bitcoin|crypto(?:currency)?|tokeniz\w*|web3|blockchain|trading bots?)\b", re.IGNORECASE)

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

# Indices within specific (track, tone_mode) pairs to exclude regardless of what Gemini routed.
# apply_tone_filter() already scrubs crypto/Web3 wording from bullet prose, so an index belongs
# here only when the underlying work itself is off-message for the tone - not merely its phrasing.
TRACK_TONE_CONSTRAINTS = {}

# Persona framing only (subtitle/keywords/skills prose) - every skill named here must already
# exist in evidence_bank.json's technical_skills; factual content (jobs, dates, bullets) lives in the bank.
TRACKS = {
    "a": {
        "subtitle": "Financial Systems & Operations",
        "keywords": ("Wealth Operations", "Process Automation", "Python", "SQL", "Salesforce", "Reconciliation"),
        "summary": "I reconcile custodial accounts across 500+ client files and automate onboarding paperwork with Python and Salesforce.",
        "skills": [
            ("Core Operations", "Custodial Cashiering & Reconciliations, Ticketing Queue Management, RIA Audits, Automation."),
            ("Systems & Tools", "Salesforce, Schwab Advisor Center, Fidelity Wealthscape, DocuSign, Python, SQL, Excel.")
        ]
    },
    "b": {
        "subtitle": "Data & Systems Engineering",
        "keywords": ("Python", "SQL", "REST APIs", "ETL", "Schema Architecture", "Process Automation"),
        "summary": "I build Python and SQL tools that cut manual reporting work, including a pipeline that cleaned 1,500+ legacy account records.",
        "skills": [
            ("Engineering & Data", "Python, SQL, REST APIs, Webhook Integrations, SQLite WAL, Data Reconciliation."),
            ("Platforms & Stack", "Salesforce, HubSpot CRM, Flask, Typst, Schwab Advisor Center, Fidelity Wealthscape.")
        ]
    },
    "c": {
        "subtitle": "Risk & Regulatory Compliance",
        "keywords": ("Regulatory Compliance", "SEC/FinCEN Filings", "Risk Management", "DocuSign", "Salesforce", "Audit Controls"),
        "summary": "I audit onboarding files across 500+ accounts and draft SEC Form D filings to catch compliance risks before execution.",
        "skills": [
            ("Compliance & Risk", "SEC & FinCEN Filings, Suitability Reviews, Custodial Exception Audits, Form D."),
            ("Systems & Controls", "Salesforce Queue Routing, DocuSign API, Schwab Advisor Center, Fidelity Wealthscape, Excel.")
        ]
    },
    "d": {
        "subtitle": "Business Intelligence & Analytics",
        "keywords": ("Power BI", "SQL", "Data Analytics", "Variance Analysis", "Reporting", "Excel"),
        "summary": "I write SQL and build Power BI dashboards that resolved $250k in ledger variances across institutional custody accounts.",
        "skills": [
            ("Analytics & Modeling", "SQL Aggregations, Variance Analysis, Power BI Dashboards, Advanced Excel Modeling."),
            ("Systems & Data", "Salesforce Reports, bSwift, Schwab Advisor Center, Fidelity Wealthscape, Python (pandas).")
        ]
    },
    "e": {
        "subtitle": "Business Operations & CRM Systems",
        "keywords": ("Business Operations", "Salesforce", "HubSpot CRM", "Process Automation", "Ticket Routing", "Python"),
        "summary": "I design Salesforce queues and DocuSign workflows that cut advisor packet review from 60 minutes to 20.",
        "skills": [
            ("Operations & Workflow", "Queue Routing Optimization, SLA Escalation Controls, CRM Pipeline Management, Process Design."),
            ("Systems & Tools", "Salesforce, HubSpot CRM, DocuSign, Schwab Advisor Center, Fidelity Wealthscape, Python.")
        ]
    }
}

# Above this similarity, a routed pool bullet is treated as the same claim as a static bullet.
_DUPLICATE_BULLET_RATIO = 0.80


def _bullets_of_other_jobs(evidence: dict) -> list:
    """Static bullets belonging to every job EXCEPT the first one.

    Routed bullets replace job 0's bullets, so a pool entry matching job 0 renders in the only
    place it would have appeared anyway. Matching a LATER job is the problem: the same sentence
    then appears twice on the page under two different employers, which reads as padding and
    silently credits one job's work to another.
    """
    return [b for job in evidence.get("experience", [])[1:] for b in job.get("bullets", [])]


def _is_duplicate_of_other_job(bullet: str, other_bullets: list) -> bool:
    text = str(bullet or "").lower()
    return any(
        difflib.SequenceMatcher(None, text, str(other or "").lower()).ratio() >= _DUPLICATE_BULLET_RATIO
        for other in other_bullets
    )


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
    indices = bullet_indices if is_valid else [0, 1, 2, 3]
    indices = [i for i in indices if 0 <= i < len(pool)]

    forbidden = TRACK_TONE_CONSTRAINTS.get((track_key, tone_key), [])
    if forbidden and any(i in forbidden for i in indices):
        original_len = len(indices)
        indices = [i for i in indices if i not in forbidden]
        # Backfill from anywhere in the pool, not a fixed shortlist: a hardcoded [0, 1, 3] is a
        # no-op whenever those indices are already selected, which silently shipped a short
        # experience block instead of replacing the dropped bullet.
        for fallback_i in range(len(pool)):
            if len(indices) >= original_len:
                break
            if fallback_i not in indices and fallback_i not in forbidden:
                indices.append(fallback_i)
        if not indices:
            indices = [0]

    other_job_bullets = _bullets_of_other_jobs(evidence_bank)
    target_len = len(indices)
    kept = [
        i for i in indices
        if not any(bw in str(pool[i]).lower() for bw in banned)
        and not _is_duplicate_of_other_job(pool[i], other_job_bullets)
    ]
    for fallback_i in range(len(pool)):
        if len(kept) >= target_len:
            break
        if fallback_i in kept or fallback_i in forbidden:
            continue
        if any(bw in str(pool[fallback_i]).lower() for bw in banned):
            continue
        if _is_duplicate_of_other_job(pool[fallback_i], other_job_bullets):
            continue
        kept.append(fallback_i)

    selected = [pool[i] for i in kept]
    return selected or pool[:4]

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
            lines.append("#v(6.5pt)")
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
            lines.append("#v(3.5pt)")
        lines.append(f"*{degree}*, {school} #h(1fr) {location} | {start} -- {end}")

    certificates_line = ", ".join(escape_typst(c) for c in evidence.get("certificates", []))
    if certificates_line:
        lines.append("#v(3.0pt)")
        lines.append(f"*Certificates & Licenses:* {certificates_line}")

    activities_list = evidence.get("activities", [])
    if activities_list:
        activities_line = " • ".join(escape_typst(a) for a in activities_list)
        lines.append("#v(3.0pt)")
        lines.append(f"*Leadership & Activities:* {activities_line}")

    return "\n".join(lines)

def _render_projects_block(evidence: dict, tone_mode: str = "conservative") -> str:
    """Renders the Technical Projects section - name/location/dates header line per project,
    followed by its bullets. `tone_mode` scrubs crypto/Web3 phrasing for conservative firms,
    same as the summary and dynamic experience bullets.
    """
    lines = []
    tone_key = str(tone_mode or "conservative").lower()
    projects = evidence.get("projects", [])
    for idx, proj in enumerate(projects):
        # A project name is a proper noun, so it is swapped wholesale via name_conservative rather
        # than run through apply_tone_filter() - the regex turned "Crypto Breakout Alert" into
        # "custodial systems Breakout Alert" on every conservative resume.
        if tone_key == "conservative" and proj.get("name_conservative"):
            name = escape_typst(proj["name_conservative"])
        else:
            name = escape_typst(proj.get("name", ""))
        location = escape_typst(proj.get("location", ""))
        start = escape_typst(proj.get("start", ""))
        end = escape_typst(proj.get("end", ""))
        bullets = proj.get("bullets", [])
        if idx > 0:
            lines.append("#v(6.0pt)")
        repo_raw = str(proj.get("repo", "") or "")
        repo_link = f' | #link("https://{repo_raw}")[Source]' if repo_raw else ""
        lines.append(f"*{name}*{repo_link} #h(1fr) {location} | {start} -- {end}")
        for b in bullets:
            clean_b = apply_tone_filter(b, tone_key)
            lines.append(f"- {escape_typst(clean_b)}")
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
    crypto/Web3 language from the summary for conservative firms (RIAs, banks, custodians). The
    Skills & Systems footer always renders the track's own 2-line category blocks unchanged.
    """
    track_data = TRACKS.get(str(track or "a").lower(), TRACKS["a"])
    tone_key = str(tone_mode or "conservative").lower()
    if tone_key not in ("conservative", "tech"):
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
    github_raw = str(identity.get("github", "") or "")
    website_label = escape_typst(website_raw)

    # Native Typst #link()[] syntax (never markdown [text](url)) - pipe-joined, skipping blank fields
    # so a missing phone/website never leaves a stray " | | " gap in the header.
    contact_fields = [f for f in (email, phone, location) if f]
    if website_raw:
        contact_fields.append(f'#link("https://{website_raw}")[{website_label}]')
    if linkedin_raw:
        contact_fields.append(f'#link("https://{linkedin_raw}")[LinkedIn]')
    if github_raw:
        contact_fields.append(f'#link("https://{github_raw}")[GitHub]')
    contact_line = " • ".join(contact_fields)

    experience_block = _render_experience_block(evidence, dynamic_bullets=selected_bullets)
    projects_block = _render_projects_block(evidence, tone_mode=tone_key)
    education_credentials_block = _render_education_credentials_block(evidence)
    skills_lines = " \\\n".join(f"*{escape_typst(label)}:* {escape_typst(desc)}" for label, desc in track_data["skills"])

    markup = f"""
#set document(
  title: "Kevin Miller - Resume - {clean_company}",
  author: "Kevin Miller",
  date: auto,
  keywords: ({keywords_tuple})
)

// Tuned 1-Page Layout Spacing
#set page(paper: "us-letter", margin: (x: 0.65in, top: 0.40in, bottom: 0.40in))
#set text(font: "Liberation Sans", size: 9.4pt, fill: rgb("#111827"))
#set par(justify: false, leading: 0.52em, spacing: 0.52em)
#set list(spacing: 0.44em, indent: 0em)
#show heading: set block(above: 0.44em, below: 0.18em)

// --- HEADER ---
#align(center)[
  #text(size: 18pt, weight: "bold", fill: rgb("#000000"))[{name}] \\
  #text(size: 10pt, weight: "medium", fill: rgb("#4B5563"))[{escape_typst(track_data["subtitle"])}] \\
  #v(2pt)
  #text(size: 8.8pt, fill: rgb("#6B7280"))[{contact_line}]
]

#v(6.5pt)
#line(length: 100%, stroke: 0.7pt + rgb("#CCCCCC"))
#v(3.8pt)

// --- SUMMARY ---
#text(size: 8.5pt, weight: "bold", tracking: 1.1pt, fill: rgb("#374151"))[SUMMARY]
#v(2pt)
{summary}

#v(6.5pt)
#line(length: 100%, stroke: 0.5pt + rgb("#E5E7EB"))
#v(3.8pt)

// --- PROFESSIONAL EXPERIENCE ---
#text(size: 8.5pt, weight: "bold", tracking: 1.1pt, fill: rgb("#374151"))[PROFESSIONAL EXPERIENCE]
#v(2pt)
{experience_block}

#v(6.5pt)
#line(length: 100%, stroke: 0.5pt + rgb("#E5E7EB"))
#v(3.8pt)

// --- TECHNICAL PROJECTS ---
#text(size: 8.5pt, weight: "bold", tracking: 1.1pt, fill: rgb("#374151"))[TECHNICAL PROJECTS]
#v(2pt)
{projects_block}

#v(6.5pt)
#line(length: 100%, stroke: 0.5pt + rgb("#E5E7EB"))
#v(3.8pt)

// --- EDUCATION & CREDENTIALS ---
#text(size: 8.5pt, weight: "bold", tracking: 1.1pt, fill: rgb("#374151"))[EDUCATION & CREDENTIALS]
#v(2pt)
{education_credentials_block}

#v(6.5pt)
#line(length: 100%, stroke: 0.5pt + rgb("#E5E7EB"))
#v(3.8pt)

// --- SKILLS & SYSTEMS ---
#text(size: 8.5pt, weight: "bold", tracking: 1.1pt, fill: rgb("#374151"))[SKILLS & SYSTEMS]
#v(2pt)
{skills_lines}
"""
    return markup.strip()

def compile_resume_pdf(company_name: str, track: str = "a", bullet_indices: list = None, tone_mode: str = "conservative") -> bytes:
    """Compiles the Typst markup string directly into PDF bytes in memory for the selected persona track."""
    markup = render_typst_markup(company_name, track, bullet_indices, tone_mode)
    return typst.compile(markup.encode("utf-8"))

def render_cover_letter_markup(letter_text: str, company_name: str = "") -> str:
    """Builds Typst markup for a business-letter PDF from ALREADY-RENDERED letter text.

    This module never authors letter prose. `letter_text` arrives fully assembled and sanitized
    from main.generate_cover_letter(), which resolves it deterministically from
    templates/cover_letter_templates.json - so the PDF and the tap-to-copy text on the Telegram
    card are byte-identical by construction, the same guarantee resolve_outreach_body() gives the
    email body. Rendering from a second source here is what would let the two drift.

    The letterhead deliberately reuses the resume's identity block, fonts and rule styling so a
    recruiter opening both attachments sees one document set rather than two.
    """
    evidence = load_evidence_bank()
    identity = evidence.get("identity", {})

    name = escape_typst(identity.get("name", "Kevin Miller"))
    email = escape_typst(identity.get("email", ""))
    phone = escape_typst(identity.get("phone", ""))
    location = escape_typst(identity.get("location", ""))
    website_raw = str(identity.get("website", "") or "")
    linkedin_raw = str(identity.get("linkedin", "") or "")

    contact_fields = [f for f in (email, phone, location) if f]
    if website_raw:
        contact_fields.append(f'#link("https://{website_raw}")[{escape_typst(website_raw)}]')
    if linkedin_raw:
        contact_fields.append(f'#link("https://{linkedin_raw}")[LinkedIn]')
    contact_line = " • ".join(contact_fields)

    # The banked letter is a plain-text block with blank-line paragraph breaks. Typst already
    # treats a blank line as a paragraph break, so escaping per-paragraph and rejoining preserves
    # the structure without needing an explicit #par() per chunk.
    paragraphs = [p.strip() for p in str(letter_text or "").split("\n\n") if p.strip()]
    # A single newline inside a paragraph (the "Best regards,\nKevin Miller" signoff) is a hard
    # line break in the source, so it must become one in Typst rather than being reflowed away.
    body_block = "\n\n".join(
        " \\\n".join(escape_typst(line) for line in para.split("\n"))
        for para in paragraphs
    )

    doc_title = f"Kevin Miller - Cover Letter - {escape_typst(company_name)}" if company_name else "Kevin Miller - Cover Letter"

    markup = f"""
#set document(
  title: "{doc_title}",
  author: "Kevin Miller",
  date: auto
)

#set page(paper: "us-letter", margin: (x: 0.9in, top: 0.75in, bottom: 0.75in))
#set text(font: "Liberation Sans", size: 10.5pt, fill: rgb("#111827"))
#set par(justify: false, leading: 0.68em, spacing: 1.05em)

// --- LETTERHEAD (mirrors the resume header) ---
#align(center)[
  #text(size: 18pt, weight: "bold", fill: rgb("#000000"))[{name}] \\
  #v(2pt)
  #text(size: 8.8pt, fill: rgb("#6B7280"))[{contact_line}]
]

#v(7pt)
#line(length: 100%, stroke: 0.7pt + rgb("#CCCCCC"))
#v(14pt)

{body_block}
"""
    return markup.strip()

def compile_cover_letter_pdf(letter_text: str, company_name: str = "") -> bytes:
    """Compiles already-rendered cover letter text into PDF bytes in memory."""
    markup = render_cover_letter_markup(letter_text, company_name)
    return typst.compile(markup.encode("utf-8"))

