"""
resume_engine.py
================
High-Performance In-Memory Resume Compiler for Kevin Miller.
Strict Deterministic Template Engine (SDTE): resumes are assembled entirely from local JSON
banks (evidence_bank.json, resume_bullets_bank.json). Gemini never authors bullet prose here -
it only ever selects a track letter (a-h) and a list of pool indices, which this module resolves
and bounds-checks against the actual bullet pool before rendering.
"""

import difflib
import io
import json
import logging
import os
import re
import typst

# The track taxonomy lives in exactly one place now (track_registry.py). TRACK_BULLET_POOL_KEYS and
# TRACKS are re-exported here because main.py and the suite already import them from this module.
from track_registry import (
    CRYPTO_SCRUB_TRACKS,
    DEFAULT_TRACK,
    FALLBACK_BULLETS_BY_POOL,
    TRACK_BULLET_POOL_KEYS,
    TRACK_LETTERS,
    TRACK_REGISTRY,
    TRACKS,
    normalize_track,
    pool_key_for,
)

# Company Conservatism & Culture Filter: crypto/Web3 language Gemini might otherwise route into
# a conservative-tone resume (RIAs, banks, custodians) gets scrubbed to institutional-safe phrasing.
_CRYPTO_TERMS_PATTERN = re.compile(r"\b(bitcoin|crypto(?:currency)?|tokeniz\w*|web3|blockchain|trading bots?)\b", re.IGNORECASE)

def should_scrub_crypto(tone_mode: str, track=None) -> bool:
    """True when crypto/Web3 phrasing has to come off this resume.

    Two independent reasons, which is why this is no longer just a tone check. A conservative
    employer is a financial-services business with a compliance view on crypto. Tracks f and g
    sell to a logistics or manufacturing reader who simply reads it as unserious - and those
    tracks route to tone_mode "tech" by design, so keying on tone alone left the crypto wording
    in the resume that most needed it gone.
    """
    if str(tone_mode or "").lower() == "conservative":
        return True
    return track is not None and normalize_track(track) in CRYPTO_SCRUB_TRACKS


def apply_tone_filter(text: str, tone_mode: str, track=None) -> str:
    """Scrubs crypto/Web3 keywords to institutional-safe phrasing when should_scrub_crypto() says
    so; passes text through unchanged otherwise.
    """
    if not text or not should_scrub_crypto(tone_mode, track):
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

# Minimal safe fallback if resume_bullets_bank.json is ever missing/corrupt - keeps PDF compilation
# alive. Built from track_registry so every track letter is represented; see FALLBACK_BULLETS_BY_POOL.
_FALLBACK_RESUME_BULLETS_BANK = dict(FALLBACK_BULLETS_BY_POOL)

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

# Indices within specific (track, tone_mode) pairs to exclude regardless of what Gemini routed.
# apply_tone_filter() already scrubs crypto/Web3 wording from bullet prose, so an index belongs
# here only when the underlying work itself is off-message for the tone - not merely its phrasing.
TRACK_TONE_CONSTRAINTS = {}

# Above this similarity, a routed pool bullet is treated as the same claim as a static bullet.
_DUPLICATE_BULLET_RATIO = 0.80

# How many of job 0's 14 statics render when every routed bullet was tagged to a later employer.
# Matches what jobs 1-3 carry, so the block stays the same size whichever way the tags fall.
_JOB_ZERO_FALLBACK_BULLETS = 3


def bullet_text(entry) -> str:
    """The prose of a bullet bank entry, whether it is a bare string or a {"text", "source_job"} dict.

    Total by construction: anything unrecognized becomes "". /edit writes bare strings back to the
    bank from Kevin's phone (update_template_entry), so a loader that raised on an unexpected shape
    would corrupt the bank on the next phone edit rather than on the next deploy.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        text = entry.get("text")
        return text if isinstance(text, str) else ""
    return ""


def bullet_source_job(entry, evidence: dict = None) -> int:
    """Which `evidence["experience"]` index a bullet's claim actually belongs to. Defaults to 0.

    A bare string means 0 (Signal Advisors), which is every untagged entry in the bank and the
    behavior that shipped before source tags existed. An index outside the experience list, a
    bool, a float or a string digit all degrade to 0 rather than raising - the alternative is a
    phone edit taking the resume renderer down.
    """
    if not isinstance(entry, dict):
        return 0
    raw = entry.get("source_job")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    jobs = (evidence or {}).get("experience", [])
    if isinstance(jobs, list) and jobs and not 0 <= raw < len(jobs):
        return 0
    return max(raw, 0)


def bullet_replaces_static(entry, evidence: dict = None) -> int:
    """Which of its own job's static bullets a routed bullet restates, or -1 for none.

    Source tagging alone fixed the attribution and created a second problem: a pool bullet is
    usually a rephrasing of one of that employer's static bullets, so appending it under the right
    employer printed the same claim twice in a row. "Built structured audit checklists to
    cross-reference multi-plant payroll data ... across 70+ manufacturing facilities" followed by
    "Built the audit checklists that cross-referenced payroll across 70+ manufacturing facilities"
    reads as padding, which is worse on the page than the misattribution was.

    A similarity threshold cannot make this call - measured against their own employer's statics,
    same-claim pairs run 0.52-0.72 and genuinely different claims run 0.41-0.45, which do not
    separate. So the substitution is authored in the bank, not inferred: `replaces` names the static
    index, and its absence means the bullet adds a claim that employer's statics do not cover and
    should append. Total, like its siblings: any malformed value means -1 (append).
    """
    if not isinstance(entry, dict):
        return -1
    raw = entry.get("replaces")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return -1
    jobs = (evidence or {}).get("experience", [])
    src = bullet_source_job(entry, evidence)
    if isinstance(jobs, list) and 0 <= src < len(jobs):
        if raw >= len(jobs[src].get("bullets", []) or []):
            return -1
    return raw


def _duplicate_check_targets(evidence: dict, source_job: int = 0) -> list:
    """Bullets a routed pool entry must not duplicate, given the job it will render under.

    Two different reasons, both ending in the same failure on the page:

    - Other JOBS: a bullet rendered under one employer that repeats another employer's static
      bullet silently credits one job's work to another. The bullet's OWN job is excluded on
      purpose - a correctly-tagged ABC bullet resembling ABC's own statics is attribution working,
      not a collision, and dropping it would be exactly backwards.
    - PROJECTS: projects render in their own section from their own data, so a match there is not
      a misattribution at all. It is simply the same sentence printed twice on one page under two
      headings, which reads as padding. track_h[4] was a live instance at 0.8067.
    """
    jobs = evidence.get("experience", []) or []
    out = [b for idx, job in enumerate(jobs) if idx != source_job for b in job.get("bullets", [])]
    out += [b for proj in evidence.get("projects", []) or [] for b in proj.get("bullets", [])]
    return out


def _bullets_of_other_jobs(evidence: dict) -> list:
    """Back-compat shim for the source_job 0 case. See _duplicate_check_targets()."""
    return _duplicate_check_targets(evidence, 0)


def _is_duplicate_of_other_job(bullet: str, other_bullets: list) -> bool:
    text = str(bullet or "").lower()
    return any(
        difflib.SequenceMatcher(None, text, str(other or "").lower()).ratio() >= _DUPLICATE_BULLET_RATIO
        for other in other_bullets
    )


def filter_ats_bullets(track: str = "a", bullet_indices: list = None, tone_mode: str = "conservative") -> list:
    """The bullet STRINGS for a track + routed indices - the shape three non-renderer call sites
    consume (card routing, format_ats_plaintext, the stage page). A thin wrapper so that adding
    source tags could not change this return type out from under them.
    """
    return [bullet_text(e) for e in filter_ats_bullet_entries(track, bullet_indices, tone_mode)]


def filter_ats_bullet_entries(track: str = "a", bullet_indices: list = None, tone_mode: str = "conservative") -> list:
    """Resolves the actual bullet entries for a track + list of pool indices. Gemini only ever
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
    Returns the raw pool entries - a bare string, or a {"text", "source_job"} dict for a bullet
    whose claim belongs to a later employer. Only the renderer needs that; everything else calls
    filter_ats_bullets() and gets strings.
    """
    evidence_bank = load_evidence_bank()
    resume_bullets_bank = load_resume_bullets_bank()
    track_key = normalize_track(track)
    tone_key = str(tone_mode or "conservative").lower()
    pool_key = pool_key_for(track_key)
    pool = resume_bullets_bank.get(pool_key) or resume_bullets_bank.get(TRACK_BULLET_POOL_KEYS[DEFAULT_TRACK], [])
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

    # Cached per source job: the targets differ by bullet now, and SequenceMatcher over 17 static
    # bullets runs for every candidate index including the backfill sweep.
    targets_cache = {}

    def _is_clean(i) -> bool:
        text = bullet_text(pool[i])
        if any(bw in text.lower() for bw in banned):
            return False
        src = bullet_source_job(pool[i], evidence_bank)
        if src not in targets_cache:
            targets_cache[src] = _duplicate_check_targets(evidence_bank, src)
        return not _is_duplicate_of_other_job(text, targets_cache[src])

    target_len = len(indices)
    kept = [i for i in indices if _is_clean(i)]
    for fallback_i in range(len(pool)):
        if len(kept) >= target_len:
            break
        if fallback_i in kept or fallback_i in forbidden:
            continue
        if not _is_clean(fallback_i):
            continue
        kept.append(fallback_i)

    selected = [pool[i] for i in kept]
    return selected or pool[:4]

def _render_experience_block(evidence: dict, dynamic_bullets: list = None) -> str:
    """Renders the Professional Experience section entirely from Evidence Bank data - every
    injected field is escape_typst()'d since none of this is a hardcoded literal anymore.
    `dynamic_bullets`, if given, is a list of routed bullet bank ENTRIES, each placed under the
    employer its claim actually came from (bullet_source_job). The rule differs by job, and the
    difference is not an inconsistency:

    - Job 0 (Signal Advisors) carries 14 static bullets that exist to feed the duplicate guard and
      to source new copy. They have never rendered, because routed bullets REPLACE them. Stacking
      them would put 18 bullets on a one-page resume.
    - Jobs 1-3 carry exactly 3 statics each and always render all 3, so a routed bullet APPENDS.
      Replacing them would delete two real claims in order to add one.

    If every routed bullet was tagged away from job 0, job 0 falls back to a bounded slice of its
    statics rather than rendering a bare heading with nothing under it.

    A routed bullet appended to jobs 1-3 SUPERSEDES the static it restates (bullet_replaces_static),
    so the section never prints one claim twice in two wordings, and its total line count is never
    higher than before source tags existed.
    """
    lines = []
    routed_by_job = {}
    for entry in dynamic_bullets or []:
        text = bullet_text(entry)
        if text:
            routed_by_job.setdefault(bullet_source_job(entry, evidence), []).append(
                (text, bullet_replaces_static(entry, evidence)))

    for idx, job in enumerate(evidence.get("experience", [])):
        title = escape_typst(job.get("title", ""))
        company = escape_typst(job.get("company", ""))
        location = escape_typst(job.get("location", ""))
        start = escape_typst(job.get("start", ""))
        end = escape_typst(job.get("end", ""))
        if idx > 0:
            lines.append("#v(6.5pt)")
        lines.append(f"*{title}* | {company} #h(1fr) {location} | {start} -- {end}")
        statics = job.get("bullets", []) or []
        routed = [t for t, _ in routed_by_job.get(idx, [])]
        if idx == 0:
            bullets = routed or (statics[:_JOB_ZERO_FALLBACK_BULLETS] if dynamic_bullets else statics)
        else:
            # A routed bullet that restates one of this job's statics takes its place rather than
            # sitting next to it - the claim survives, in the phrasing chosen for this posting.
            superseded = {sub for _, sub in routed_by_job.get(idx, []) if sub >= 0}
            bullets = [b for i, b in enumerate(statics) if i not in superseded] + routed
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

def _render_projects_block(evidence: dict, tone_mode: str = "conservative", track=None) -> str:
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
        if should_scrub_crypto(tone_key, track) and proj.get("name_conservative"):
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
            clean_b = apply_tone_filter(b, tone_key, track)
            lines.append(f"- {escape_typst(clean_b)}")
    return "\n".join(lines)

def render_typst_markup(company_name: str, track: str = "a", bullet_indices: list = None, tone_mode: str = "conservative") -> str:
    """Builds single-page Typst markup for the selected persona track, sourcing every factual
    claim (experience, education, certificates) from the centralized JSON banks (hot-reloaded
    fresh on every call), assembled into 4 sections: Summary, Professional Experience,
    Education & Credentials, Skills & Systems. Dynamic 30% customization is entirely
    track-driven (a-h, already routed by Gemini as an SDTE integer/letter, never free text):
    the header summary, the leading achievement bullet, and skill emphasis all key off the
    same `track` value, so no live job-description text is required at render time - the
    resume still compiles correctly even from a bare "a" default with no cached job.
    `tone_mode` ("conservative" | "tech") is the Company Conservatism & Culture Filter: it scrubs
    crypto/Web3 language from the summary for conservative firms (RIAs, banks, custodians). The
    Skills & Systems footer always renders the track's own 2-line category blocks unchanged.
    """
    track_key = normalize_track(track)
    track_data = TRACKS[track_key]
    tone_key = str(tone_mode or "conservative").lower()
    if tone_key not in ("conservative", "tech"):
        tone_key = "conservative"
    evidence = load_evidence_bank()
    identity = evidence.get("identity", {})
    clean_company = escape_typst(company_name or "Target Operations")

    # ENTRIES, not strings: the renderer is the only caller that needs each bullet's source job.
    selected_bullets = filter_ats_bullet_entries(track_key, bullet_indices, tone_key)

    keywords_tuple = ", ".join(f'"{kw}"' for kw in track_data["keywords"])
    summary = escape_typst(apply_tone_filter(track_data["summary"], tone_key, track_key))

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
    projects_block = _render_projects_block(evidence, tone_mode=tone_key, track=track_key)
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

