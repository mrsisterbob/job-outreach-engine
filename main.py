import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import html
import io
import json
import logging
import os
import re
import contextlib
import hashlib
import sqlite3
import sys
import threading
import time
import urllib.parse
import uuid
from xml.etree import ElementTree
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
import requests
from flask import Flask, jsonify, request, Response
from apscheduler.schedulers.background import BackgroundScheduler
from resume_engine import compile_resume_pdf, filter_ats_bullets, TRACK_BULLET_POOL_KEYS
from response_schema import GeminiJobScreenerResponse
from pipeline_utils import (
    build_apollo_url, build_linkedin_url, build_linkedin_company_posts_url, build_hiring_manager_dork, build_recruiter_dork,
    build_alumni_dork, normalize_priority_value, calculate_followup_interval,
    resolve_smart_target_tab, enforce_sentence_limit, get_fit_score_indicator,
    generate_dedup_hash, generate_short_key, parse_posted_hours, get_age_badge,
    extract_salary, extract_work_style, compute_description_simhash, resolve_email_waterfall,
    derive_job_source, is_unverified_email, status_rank, STATUS_VOCAB,
    followup_action, followup_anchor, is_followup_unscheduled,
    lint_outreach_template, advise_outreach_template,
    is_probable_company_name, ats_slug_guess, build_sent_contact,
    is_guessed_contact_email, resolve_sent_email_backfill,
    is_role_mailbox, company_domain_of, name_from_email_local_part,
    match_email_to_crm_company,
    plan_carmen_followup, CARMEN_LADDER_DAYS,
    is_expired_matched_row, MATCHED_EXPIRY_DAYS,
    parse_job_command, parse_job_page_html, build_ingest_job_dict,
    canonical_job_url, canonical_linkedin_job_url, is_linkedin_job_url,
    strip_tracking_params, strip_html_to_text,
    FOLLOWUP_1_DAYS, FOLLOWUP_2_DAYS, FOLLOWUP_BURY_DAYS, STALE_HOT_DAYS,
    REPLY_FOLLOWUP_DAYS, MAX_AUTO_BURIES_PER_RUN,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)
APP_START_TIME = time.time()

# ==============================================================================
# 1. ENVIRONMENT VARIABLES & DATABASE INITIALIZATION (WAL MODE)
# ==============================================================================
API_KEY = os.environ.get("OPENWEBNINJA_KEY") or os.environ.get("RAPIDAPI_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
CRM_WEBHOOK_URL = os.environ.get("CRM_WEBHOOK_URL")
CRM_SHARED_SECRET = os.environ.get("CRM_SHARED_SECRET")
if not CRM_SHARED_SECRET:
    logging.warning(
        "[CONFIG WARNING] CRM_SHARED_SECRET is unset - outbound CRM requests will carry no secret, "
        "and Code.gs (deployed Execute as: Me, Access: Anyone) now fails closed and will reject them."
    )
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN")
GMAIL_USER = os.environ.get("GMAIL_USER")
# Absolute origin for the links Kevin taps out of Telegram. A relative /stage/<id> href is inert
# inside a Telegram message, so every card link has to carry a real scheme+host. Render injects
# RENDER_EXTERNAL_URL into deployed web services automatically; BASE_URL is the manual override for
# any other host, and localhost is the dev default.
BASE_URL = (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("BASE_URL") or "http://localhost:5000").rstrip("/")
JSEARCH_URL = "https://api.openwebninja.com/jsearch/search"
JSEARCH_TIMEOUT_SECONDS = 12
JSEARCH_MAX_RETRIES = 1  # additional attempts beyond the first, on timeout/429/5xx
JSEARCH_SEMAPHORE = threading.Semaphore(4)  # cap concurrent OpenWebNinja requests to avoid rate-limit timeouts
# 100-Query Rolling Master Engine: one oddball theme per 10-query slice, used to badge wildcard matches
ODDBALL_KEYWORDS = ["supply chain", "revenue operations", "healthcare", "implementation", "erp", "logistics", "claims", "manufacturing", "cloud operations", "procurement", "transformation"]

# Stacking cap for ALL additive score modifiers combined - Layer 1 (calculate_hybrid_score_modifier
# keyword/salary bonuses) plus Layer 2 (process_single_candidate alumni/warm/Clavicular boosts) -
# before they are added to Gemini's raw 1-100 base score. Kevin's sourcing targets are built to
# trip 2-4 bonus categories at once, so without this the additive layers compound past +60 and
# erase the separation Gemini's holistic score was drawing (every match pins to the 100 clamp).
# 30 = "your single best relationship signal counts in full, keyword stacking on top of it does
# not": one warm/Clavicular referral (+30) or one alum (+20) plus a keyword category survives
# intact; the 2nd/3rd/4th stacked bonus is what gets trimmed. Modelled against synthetic batches
# (Gemini base ~N(72,13), bonus-trip rates from the CRM sheet) this roughly halves the share of
# matches pinned to the 100 clamp vs the raw stack, where +35 barely moved it. Only the upside is
# capped; negative modifiers (ghost listing, call-volume, out-of-state hub) pass through uncapped.
# The per-layer min(1, ...)/min(100, ...) clamps stay as a separate safety floor/ceiling. Single
# tunable knob - raise toward 35 to loosen, drop toward 25 to force more spread.
BONUS_STACK_CAP = 30

def build_jsearch_request_config():
    """Prioritizes OPENWEBNINJA_KEY over RAPIDAPI_KEY when both are set."""
    openweb_key = os.environ.get("OPENWEBNINJA_KEY")
    rapidapi_key = os.environ.get("RAPIDAPI_KEY")
    if openweb_key:
        return {"x-api-key": openweb_key}, JSEARCH_URL
    if rapidapi_key:
        return {"X-RapidAPI-Key": rapidapi_key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}, "https://jsearch.p.rapidapi.com/search"
    logging.error("[CONFIG ERROR] No JSearch API key found in environment variables.")
    return {}, JSEARCH_URL
DB_PATH = os.environ.get("JOBS_DB_PATH", "jobs_cache.db")  # override lets tests isolate their own SQLite file
EVIDENCE_BANK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence_bank.json")

def crm_get(params, timeout=10):
    """GET against CRM_WEBHOOK_URL with the shared secret auto-attached. Returns a requests.Response
    or None if CRM_WEBHOOK_URL is unset or the request raised. Centralizes CRM auth in one place -
    defined early so startup-time callers (e.g. hydrate_filters_from_sheets via init_db()) can use it.
    """
    if not CRM_WEBHOOK_URL:
        return None
    merged_params = dict(params or {})
    if CRM_SHARED_SECRET:
        merged_params["secret"] = CRM_SHARED_SECRET
    try:
        return requests.get(CRM_WEBHOOK_URL, params=merged_params, timeout=timeout)
    except Exception as e:
        logging.error(f"CRM GET Error ({merged_params.get('action')}): {e}")
        return None

def crm_post(payload, timeout=10):
    """POST against CRM_WEBHOOK_URL with the shared secret auto-attached. Returns a requests.Response
    or None if CRM_WEBHOOK_URL is unset or the request raised. Centralizes CRM auth in one place.
    """
    if not CRM_WEBHOOK_URL:
        return None
    merged_payload = dict(payload or {})
    if CRM_SHARED_SECRET:
        merged_payload["secret"] = CRM_SHARED_SECRET
    try:
        return requests.post(CRM_WEBHOOK_URL, json=merged_payload, timeout=timeout)
    except Exception as e:
        logging.error(f"CRM POST Error ({merged_payload.get('action')}): {e}")
        return None

# Minimal safe fallback if evidence_bank.json is ever missing/corrupt - keeps AI prompts alive.
_FALLBACK_EVIDENCE_BANK = {
    "identity": {"name": "Kevin Miller", "location": "Detroit, MI"},
    "experience": [], "technical_skills": [], "banned_words": [],
    "voice_and_tone": {"tone": "professional, grounded, low-pressure", "guidance": []}
}

def load_evidence_bank():
    """Loads the centralized fact bank (experience, skills, tone, banned words) used to ground
    every AI-generated output. Falls back to a minimal stub on any read/parse failure. Called
    fresh on every use (no module-level cache) so manual JSON edits go live instantly, without a
    Flask server restart.
    """
    try:
        with open(EVIDENCE_BANK_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Evidence Bank load failed, using fallback stub: {e}")
        return _FALLBACK_EVIDENCE_BANK

def build_evidence_context_block(mode="eval"):
    """Renders a compact, prompt-ready summary of the Evidence Bank for injection into Gemini
    prompts. `mode` trims the token footprint per use case:
      - "email"/"pitch": experience + technical_skills + voice_and_tone only.
      - "eval" (default, the main job screener): the above PLUS banned_words, to strictly
        govern the AI's output where enforcement matters most.
    Always reloads the bank from disk (hot-reload, see load_evidence_bank()).
    """
    evidence_bank = load_evidence_bank()
    identity = evidence_bank.get("identity", {})
    experience_lines = "\n".join(
        f"- {job.get('title')} at {job.get('company')} ({job.get('start')} - {job.get('end')})"
        for job in evidence_bank.get("experience", [])
    )
    skills_line = ", ".join(evidence_bank.get("technical_skills", []))
    tone = evidence_bank.get("voice_and_tone", {})
    tone_lines = "\n".join(f"- {g}" for g in tone.get("guidance", []))
    block = (
        f"CANDIDATE: {identity.get('name', 'Kevin Miller')} ({identity.get('location', 'Detroit, MI')})\n"
        f"VERIFIED EXPERIENCE:\n{experience_lines}\n"
        f"VERIFIED TECHNICAL SKILLS: {skills_line}\n"
        f"VOICE & TONE ({tone.get('tone', 'professional, grounded, low-pressure')}):\n{tone_lines}"
    )
    if mode == "eval":
        banned_line = ", ".join(evidence_bank.get("banned_words", []))
        block += f"\nBANNED WORDS (never use): {banned_line}"
    return block

# ==============================================================================
# STRICT DETERMINISTIC TEMPLATE ENGINE (SDTE): local JSON template banks. Gemini never
# authors outreach/LinkedIn prose - it only routes an integer template id, which Python then
# interpolates deterministically via .format(). Editable live via the /edit Telegram command.
# ==============================================================================
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
OUTREACH_TEMPLATES_PATH = os.path.join(TEMPLATES_DIR, "outreach_templates.json")
LINKEDIN_TEMPLATES_PATH = os.path.join(TEMPLATES_DIR, "linkedin_templates.json")
COVER_LETTER_TEMPLATES_PATH = os.path.join(TEMPLATES_DIR, "cover_letter_templates.json")

# Voice note for anyone editing these stubs or the JSON banks they mirror: "Hi{name}," is
# deliberate, not a typo. interpolate_template() renders {name} WITH its own leading space
# ("Hi Dana,") or as an empty string ("Hi,"), so a template must never put a space of its own
# before the placeholder. Every string here is also held to pipeline_utils.lint_outreach_template().
_FALLBACK_OUTREACH_TEMPLATES = {
    "cold_ops": ["Hi{name},\n\nYour {job_title} posting is what got me to write, but I mostly wanted your perspective on where the manual work still sits.\n\nMy day job is Python and SQL that replaces reporting people used to run by hand. Do you have 10 minutes for a brief call?\n\nHappy to work around your schedule.\n\nBest,\nKevin Miller"],
    "warm_alumni": ["Hi{name},\n\n[how you know them, and the specific occasion you last spoke]. [one concrete detail so this reads like you].\n\n[the one thing you want their perspective on at {company}]. [your ask, and a concrete time window].\n\nBest,\nKevin"],
    "followup_bumps": ["Hi{name},\n\nCircling back on the {job_title} role in case this got buried.\n\nStill interested, and happy to answer anything useful.\n\nThanks,\nKevin Miller"],
    "recruiter": ["Hi{name},\n\nI recently applied for the {job_title} role at {company} and wanted to reach out directly. Most of my recent work is custodial reconciliation and Python that replaces manual reporting.\n\nIs the search still open, and is there a rough timeline for first interviews? A one-line reply is plenty.\n\nBest,\nKevin Miller"]
}

# Follow-up bump copy for PEOPLE-schema rows (Carmen Cold networking contacts): these tabs have
# no Role column, so the followup_bumps bank's "the {job_title} role at {company}" phrasing would
# render "the this role role at your team". Same register as the bank, anchored on the person and
# company instead of a role. build_followup_bump_draft()/generate_bump_email() route here on a
# blank title. Held to lint_outreach_template() by test_pipeline_utils.py like every other bank.
_ROLELESS_FOLLOWUP_BUMPS = [
    "Hi{name},\n\nCircling back on my earlier note to {company} in case it got buried.\n\nStill keen to connect. Happy to answer anything useful.\n\nBest,\nKevin",
    "Hi{name},\n\nI reached out earlier about {company} and wanted to try once more.\n\nMy guess is this isn't the right time, which is completely fine. If that changes, I am around.\n\nBest,\nKevin",
]
_FALLBACK_LINKEDIN_TEMPLATES = {
    "linkedin_templates": ["Hi{name}. Saw you're hiring a {job_title} at {company}. I'd like to connect."]
}

# Cover letter stub. Unlike the outreach banks this one is keyed by the SAME track letters as
# resume_bullets_bank.json, because the letter and the resume PDF must argue the same case - see
# generate_cover_letter().
_FALLBACK_COVER_LETTER_TEMPLATES = {
    "openers": ["I'm writing to apply for the {job_title} position at {company}."],
    "track_a_wealth_ops": ["Most of my work is daily transaction intake, account maintenance, and the documentation exceptions that stall them. At Signal Advisors I audited onboarding paperwork across 500+ accounts, processed cashiering and ACAT transfers through Schwab Advisor Center and Fidelity Wealthscape, and cleared advisor requests inside 1-to-2 hour SLAs."],
    "closers": ["Clean records, clear handoffs, and exceptions caught before they reach anyone downstream are what I'm actually good at. I'd welcome a conversation about the {job_title} role at {company}."]
}

def load_outreach_templates():
    """Hot-reloads the cold/warm/bump email template bank from templates/outreach_templates.json.
    Falls back to a minimal safe stub on any read/parse failure. Called fresh on every use so
    /edit mutations go live instantly, without a Flask server restart.
    """
    try:
        with open(OUTREACH_TEMPLATES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Outreach templates load failed, using fallback stub: {e}")
        return _FALLBACK_OUTREACH_TEMPLATES

def load_linkedin_templates():
    """Hot-reloads the LinkedIn connection note template bank from templates/linkedin_templates.json.
    Falls back to a minimal safe stub on any read/parse failure. See load_outreach_templates().
    """
    try:
        with open(LINKEDIN_TEMPLATES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"LinkedIn templates load failed, using fallback stub: {e}")
        return _FALLBACK_LINKEDIN_TEMPLATES

def load_cover_letter_templates():
    """Hot-reloads the track-keyed cover letter bank from templates/cover_letter_templates.json.
    Falls back to a minimal safe stub on any read/parse failure. See load_outreach_templates().
    """
    try:
        with open(COVER_LETTER_TEMPLATES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Cover letter templates load failed, using fallback stub: {e}")
        return _FALLBACK_COVER_LETTER_TEMPLATES

def resolve_template_text(pool, idx, fallback_text=""):
    """Bounds-checks an integer template index against a template pool, defaulting to index 0
    (or a supplied fallback string) if the pool is empty or the index is missing/out-of-range.
    """
    if not isinstance(pool, list) or not pool:
        return fallback_text
    if not isinstance(idx, int) or idx < 0 or idx >= len(pool):
        idx = 0
    return pool[idx]

def interpolate_template(template, name="", company="", job_title=""):
    """Deterministically fills {name}/{company}/{job_title} placeholders via str.format() - the
    only place candidate-facing outreach/LinkedIn copy is ever assembled. Never calls Gemini.

    {name} renders WITH a leading space when a contact name is known and as an empty string when
    it is not, so a template written "Hi{name}," yields "Hi Dana," or a bare "Hi," - never the old
    "Hi there,". Templates must therefore not supply their own space before the placeholder; a
    hand-typed "Hi {name}," from /edit is normalized here so the phone-editing path stays forgiving.
    The literal "there" is scrubbed too, so any caller still passing the retired sentinel degrades
    to "Hi," rather than reintroducing it.

    The {name_bare} salutation-on-its-own-line form was retired: the forensic analysis of the
    correct mailbox (kjmiller406@gmail.com) shows name-alone openers are a warm marker for people
    already spoken to, not a cold voice, so no shipped template uses it and it is not supported.
    """
    clean_name = str(name or "").strip()
    if clean_name.lower() == "there":
        clean_name = ""
    normalized = str(template or "").replace(" {name}", "{name}")
    try:
        return normalized.format(
            name=f" {clean_name}" if clean_name else "",
            company=company or "your team",
            job_title=job_title or "this role",
        )
    except Exception as e:
        logging.error(f"Template interpolation failed: {e}")
        return template

def first_name_for_greeting(full_name):
    """First name to drop into interpolate_template()'s {name} slot, so a resolved contact
    renders "Hi Dana," and everything else stays a bare "Hi,".

    Returns "" for an empty value, the "Contact" placeholder get_warm_crm_contacts() uses for a
    nameless row, or anything that doesn't look like a person's name (a URL, an email, a bare
    number). interpolate_template() already turns "" - and the retired "there" sentinel - back
    into "Hi,", so a "" here is the safe no-name path.
    """
    raw = str(full_name or "").strip()
    if not raw or raw.lower() == "contact":
        return ""
    if "@" in raw or "/" in raw or raw.lower().startswith(("http:", "https:", "www.")):
        return ""
    first = raw.split()[0].strip(",.\"'")
    if len(first) < 2 or not any(ch.isalpha() for ch in first):
        return ""
    return first

RESUME_BULLETS_BANK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resume_bullets_bank.json")

EDIT_ID_PATTERN = re.compile(r"^(L|C|W|B|T[A-E])(\d+)$", re.IGNORECASE)

def resolve_edit_target(id_str):
    """Maps a /edit ID to (file_path, list_key, index):
      L0-L9 -> templates/linkedin_templates.json[linkedin_templates]
      C0-C5 -> templates/outreach_templates.json[cold_ops]
      W0-W1 -> templates/outreach_templates.json[warm_alumni]
      B0-B1 -> templates/outreach_templates.json[followup_bumps]
      TA0-TA9 ... TE0-TE9 -> resume_bullets_bank.json[track_x_...]
    Returns None if the ID prefix is unrecognized.
    """
    m = EDIT_ID_PATTERN.match(str(id_str or "").strip())
    if not m:
        return None
    prefix, idx = m.group(1).upper(), int(m.group(2))
    if prefix == "L":
        return (LINKEDIN_TEMPLATES_PATH, "linkedin_templates", idx)
    if prefix == "C":
        return (OUTREACH_TEMPLATES_PATH, "cold_ops", idx)
    if prefix == "W":
        return (OUTREACH_TEMPLATES_PATH, "warm_alumni", idx)
    if prefix == "B":
        return (OUTREACH_TEMPLATES_PATH, "followup_bumps", idx)
    if len(prefix) == 2 and prefix[0] == "T":
        pool_key = TRACK_BULLET_POOL_KEYS.get(prefix[1].lower())
        if pool_key:
            return (RESUME_BULLETS_BANK_PATH, pool_key, idx)
    return None

# /edit pools that hold candidate-facing outreach copy, mapped to the linter's `kind`.
# The resume-bullet pools (TA0-TE9) are absent on purpose: they are resume prose, not
# outreach voice, so the email rules do not apply to them and they are never linted.
_EDIT_LINT_KINDS = {
    "linkedin_templates": "linkedin",
    "cold_ops": "email",
    "warm_alumni": "email",
    "followup_bumps": "email",
    "recruiter": "email",
}

def lint_edited_template(list_key, new_text):
    """Returns a Telegram HTML warning block for a /edit'd template, or "" if there is nothing
    to say. Advisory ONLY - update_template_entry() still writes. Kevin edits from his phone
    and has to be able to override the rulebook; a blocked write would strand him.

    Lints the INTERPOLATED render, not the raw string, because that is what the recipient reads
    and what the length caps are measured on. Interpolating also runs the same " {name}" ->
    "{name}" normalization a real send does, so a hand-typed "Hi {name}," is not warned about.
    """
    kind = _EDIT_LINT_KINDS.get(list_key)
    if not kind:
        return ""

    rendered = interpolate_template(new_text, name="", company="", job_title="")
    notes = [("⚠️", v) for v in lint_outreach_template(rendered, kind)]
    notes += [("💡", n) for n in advise_outreach_template(rendered, kind)]
    if not notes:
        return ""

    lines = "\n".join(f"{icon} {html.escape(text)}" for icon, text in notes)
    return f"\n\n<b>Voice check</b> (saved anyway)\n{lines}"

def update_template_entry(file_path, list_key, idx, new_text):
    """Atomically loads a template JSON bank, overwrites the string at (list_key, idx), and
    writes it back to disk via a temp-file + os.replace swap (crash-safe, no partial writes on
    disk). Returns (ok: bool, message: str) - message is a ready-to-send Telegram HTML string.

    Voice-linter findings are appended to that message as a warning; they NEVER stop the write.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return False, f"❌ Failed to load template bank: {html.escape(str(e))}"

    pool = data.get(list_key)
    if not isinstance(pool, list):
        return False, f"❌ Unknown template pool: <code>{html.escape(str(list_key))}</code>"
    if idx < 0 or idx >= len(pool):
        return False, f"❌ Index {idx} out of range for <code>{html.escape(str(list_key))}</code> (valid: 0-{len(pool) - 1})."

    pool[idx] = new_text
    try:
        tmp_path = f"{file_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, file_path)
    except Exception as e:
        return False, f"❌ Failed to write template bank: {html.escape(str(e))}"

    # Linted after the write, so a linter exception can never cost Kevin the edit.
    try:
        warning = lint_edited_template(list_key, new_text)
    except Exception as e:
        logging.error(f"Template voice lint failed (edit still saved): {e}")
        warning = ""

    return True, (
        f"✅ <b>Template Updated:</b> <code>{html.escape(str(list_key))}[{idx}]</code>\n\n"
        f"<code>{html.escape(new_text)}</code>"
        f"{warning}"
    )

# Inbound Email Anti-Spam Gatekeeper: 10 pre-filter shield parameters (raw CSV/string env values,
# parsed lazily in passes_email_prefilter() to avoid depending on helpers defined later in the file)
EMAIL_ALLOW_DOMAINS = os.environ.get("EMAIL_ALLOW_DOMAINS", "")
EMAIL_BLOCK_DOMAINS = os.environ.get("EMAIL_BLOCK_DOMAINS", "quora.com,anytimefitness.com")
EMAIL_REQUIRED_KEYWORDS = os.environ.get("EMAIL_REQUIRED_KEYWORDS", "interview,schedule,offer,opportunity,reply")
EMAIL_EXCLUDED_KEYWORDS = os.environ.get("EMAIL_EXCLUDED_KEYWORDS", "digest,unsubscribe,newsletter,promo,alert")
EMAIL_SENDER_BLACKLIST = os.environ.get("EMAIL_SENDER_BLACKLIST", "no-reply@,noreply@")
EMAIL_SUBJECT_REGEX_FILTER = os.environ.get("EMAIL_SUBJECT_REGEX_FILTER", "")
try:
    EMAIL_MAX_AGE_SECONDS = int(os.environ.get("EMAIL_MAX_AGE_SECONDS", "300"))
except (TypeError, ValueError):
    EMAIL_MAX_AGE_SECONDS = 300
EMAIL_REQUIRE_DIRECT_REPLY = os.environ.get("EMAIL_REQUIRE_DIRECT_REPLY", "False").strip().lower() in ("1", "true", "yes")
try:
    EMAIL_MIN_BODY_LENGTH = int(os.environ.get("EMAIL_MIN_BODY_LENGTH", "50"))
except (TypeError, ValueError):
    EMAIL_MIN_BODY_LENGTH = 50
EMAIL_LABEL_TARGET_INBOX = os.environ.get("EMAIL_LABEL_TARGET_INBOX", "INBOX")

# Mobile Short Key Alias Map
ALIAS_MAP = {
    "min": "min_salary",
    "pay": "min_salary",
    "exp": "experience_salary_floor",
    "floor": "experience_salary_floor",
    "ban": "title_exclusions",
    "bans": "title_exclusions",
    "city": "valid_cities",
    "loc": "valid_cities",
    "query": "target_queries",
    "q": "target_queries",
    "kw": "required_keywords",
    "ats": "ats_company_slugs"
}

@contextlib.contextmanager
def get_db_conn():
    """Yields a SQLite connection tuned for concurrent writers: WAL + NORMAL sync + busy_timeout.

    A context manager, NOT a bare connection: `with sqlite3.connect(...) as conn` only wraps a
    TRANSACTION (commit on clean exit, rollback on exception) and leaves the connection itself
    open. Every `with get_db_conn() as conn:` call site therefore leaked a connection - each
    holding a file handle plus its own WAL page cache and statement cache. crm_outbox_worker_loop
    runs process_crm_outbox_batch every 5 seconds forever, so an idle instance leaked ~720
    connections/hour and walked a 512MB box into an OOM restart roughly every 3.5 hours.

    Commit/rollback semantics are preserved so no call site has to change: the inner
    `with conn:` block still ends the transaction the same way, and the connection is closed
    in the finally.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        with conn:
            yield conn
    finally:
        conn.close()

# Seed values for a brand-new search_filters table. Module-level (not inlined in init_db) so
# restore_core_sourcing_filters() can put target_queries back if a restart ever leaves it blank.
DEFAULT_SEARCH_FILTERS = {
    "min_salary": 50000,
    "experience_salary_floor": 60000,
    "radius_miles": 45,
    "valid_cities": [
        "farmington", "detroit", "ann arbor", "novi", "troy", "southfield",
        "auburn hills", "plymouth", "royal oak", "livonia", "dearborn",
        "birmingham", "bloomfield", "warren", "sterling heights", "canton",
        "rochester", "wixom", "madison heights",
        # Added: real Metro Detroit suburbs within ~35mi of Farmington that were missing
        # from the original list and would otherwise pass JSearch's radius sourcing only
        # to be silently dropped by this filter's exact-string check.
        "redford", "walled lake", "west bloomfield", "waterford", "pontiac",
        "ferndale", "oak park", "northville", "westland", "dearborn heights",
        "garden city", "milford", "south lyon", "commerce", "clawson", "berkley",
        "beverly hills", "franklin", "hazel park", "wyandotte", "allen park",
        "melvindale", "lathrup village", "farmington hills", "highland",
        "white lake", "inkster", "taylor", "southgate", "lincoln park",
        "romulus", "belleville", "davisburg", "clarkston", "lake orion",
        "shelby", "new hudson",
        # Added: full Metro Detroit MSA - Macomb County, Downriver, Grosse Pointes,
        # Livingston edge. These run 35-45mi from Farmington (outside the original
        # anchor's radius_miles), which is why radius_miles was widened to 45 alongside
        # this and dedicated query anchors were added for the regions no existing
        # query anchor reaches even at the wider radius.
        "clinton township", "roseville", "st. clair shores", "saint clair shores",
        "eastpointe", "fraser", "chesterfield", "new baltimore", "macomb township",
        "utica", "washington township", "mount clemens",
        "trenton", "riverview", "woodhaven", "flat rock", "brownstown", "grosse ile",
        "grosse pointe", "harper woods",
        "huntington woods", "pleasant ridge", "keego harbor", "orchard lake",
        "bingham farms", "wolverine lake", "union lake",
        "brighton", "howell", "hartland", "fenton"
    ],
    "title_exclusions": [
        "sales", "account executive", "bdr", "sdr", "financial advisor", "financial planner",
        "client relationship manager", "agent", "wholesaler", "producer", "insurance agent",
        "teller", "branch", "personal banker", "loan officer", "mortgage", "cpa",
        "customer service representative", "call center", "door to door", "cold call",
        "administrative", "receptionist", "office assistant", "logistics clerk",
        "patient intake", "intake coordinator", "front desk", "office coordinator"
    ],
    "company_exclusions": [
        "cybercoders", "robert half", "kforce", "jobot", "actalent", "insight global"
    ],
    "hard_ban_keywords": [
        "lead generation", "upselling", "quota-driven", "client acquisition",
        "hunter mentality", "pipeline development", "uncapped earnings",
        "cold outreach", "deal closing", "solution pitching",
        "uncapped potential", "commission", "hustle", "grind", "door-to-door",
        "phone jockey", "call jockey", "cold calling",
        "physical filing", "answering phones", "switchboard", "data entry clerk",
        "schedule travel arrangements", "clerical duties", "errands"
    ],
    "seniority_exclusions": [
        "senior", " lead", " manager", "director", "vp", " executive", " principal", "head of"
    ],
    "core_skills": [
        "python", "sql", "salesforce", "excel", "schwab sac", "schwab advisor center",
        "fidelity wealthscape", "docusign", "process automation", "reconciliation"
    ],
    "tier1_ecosystem": [
        "downtown detroit", "inveniam", "rivian", "rocket", "quicken", "stockx", "venture"
    ],
    "required_keywords": [],
    "ats_company_slugs": [],
    # Workday tenants as "tenant/wdN/site" triples - see fetch_workday_jobs(). Unlike
    # ats_company_slugs these cannot be auto-discovered from a company name, so they are
    # curated by hand and seeded with the Metro Detroit financial employers Workday hosts.
    "workday_boards": [],
    "target_queries": [
        "Wealth Operations Farmington MI", "Fintech Operations Farmington MI",
        "Business Operations Analyst Farmington MI", "Custodial Operations Schwab Fidelity Farmington MI",
        "Financial Systems Process Automation Farmington MI", "Operations Specialist Farmington MI",
        "Salesforce Administrator Farmington MI", "Business Systems Analyst Farmington MI",
        "Financial Operations Analyst Birmingham MI", "Supply Chain Operations Analyst Farmington MI",

        "Trade Operations Analyst Detroit MI", "Compliance Operations Specialist Detroit MI",
        "Risk Operations Analyst Detroit MI", "Client Operations Associate Detroit MI",
        "Treasury Operations Analyst Detroit MI", "Data Operations Analyst Detroit MI",
        "Process Improvement Analyst Detroit MI", "Onboarding Specialist Detroit MI",
        "Data Operations Analyst Warren MI", "Revenue Operations Analyst Detroit MI",

        "Wealth Management Operations Ann Arbor MI", "Business Intelligence Analyst Ann Arbor MI",
        "Fintech Systems Analyst Ann Arbor MI", "Custodial Reconciliation Analyst Ann Arbor MI",
        "Salesforce Administrator Ann Arbor MI", "Operations Analyst Ann Arbor MI",
        "Business Systems Analyst Ann Arbor MI", "Financial Analyst Operations Ann Arbor MI",
        "Business Operations Analyst Plymouth MI", "Healthcare Operations Analyst Ann Arbor MI",

        "Wealth Operations Novi MI", "Fintech Operations Novi MI",
        "Business Operations Analyst Novi MI", "Custodial Operations Schwab Fidelity Novi MI",
        "Financial Systems Process Automation Novi MI", "Operations Specialist Novi MI",
        "Salesforce Administrator Novi MI", "Business Systems Analyst Novi MI",
        "Client Success Operations Wixom MI", "Implementation Specialist Novi MI",

        "Wealth Operations Troy MI", "Fintech Operations Troy MI",
        "Business Operations Analyst Troy MI", "Custodial Operations Schwab Fidelity Troy MI",
        "Financial Systems Process Automation Troy MI", "Operations Specialist Troy MI",
        "Salesforce Administrator Troy MI", "Business Systems Analyst Troy MI",
        "Process Improvement Analyst Rochester MI", "ERP Systems Analyst Troy MI",

        "Wealth Operations Southfield MI", "Fintech Operations Southfield MI",
        "Business Operations Analyst Southfield MI", "Custodial Operations Schwab Fidelity Southfield MI",
        "Financial Systems Process Automation Southfield MI", "Operations Specialist Southfield MI",
        "Salesforce Administrator Southfield MI", "Business Systems Analyst Southfield MI",
        "Trade Operations Analyst Bloomfield MI", "Logistics Operations Analyst Southfield MI",

        "Wealth Operations Auburn Hills MI", "Fintech Operations Auburn Hills MI",
        "Business Operations Analyst Auburn Hills MI", "Custodial Operations Schwab Fidelity Auburn Hills MI",
        "Financial Systems Process Automation Auburn Hills MI", "Operations Specialist Auburn Hills MI",
        "Salesforce Administrator Auburn Hills MI", "Business Systems Analyst Auburn Hills MI",
        "Compliance Operations Specialist Sterling Heights MI", "Claims Operations Analyst Auburn Hills MI",

        "Wealth Operations Royal Oak MI", "Fintech Operations Royal Oak MI",
        "Business Operations Analyst Royal Oak MI", "Custodial Operations Schwab Fidelity Royal Oak MI",
        "Financial Systems Process Automation Royal Oak MI", "Operations Specialist Royal Oak MI",
        "Salesforce Administrator Royal Oak MI", "Business Systems Analyst Royal Oak MI",
        "Treasury Operations Analyst Madison Heights MI", "Manufacturing Operations Analyst Royal Oak MI",

        "Wealth Operations Livonia MI", "Fintech Operations Livonia MI",
        "Business Operations Analyst Livonia MI", "Custodial Operations Schwab Fidelity Livonia MI",
        "Financial Systems Process Automation Livonia MI", "Operations Specialist Livonia MI",
        "Salesforce Administrator Livonia MI", "Business Systems Analyst Livonia MI",
        "Onboarding Specialist Canton MI", "Cloud Operations Analyst Livonia MI",

        "Wealth Operations Dearborn MI", "Fintech Operations Dearborn MI",
        "Business Operations Analyst Dearborn MI", "Custodial Operations Schwab Fidelity Dearborn MI",
        "Financial Systems Process Automation Dearborn MI", "Operations Specialist Dearborn MI",
        "Salesforce Administrator Dearborn MI", "Business Systems Analyst Dearborn MI",
        "Data Operations Analyst Dearborn MI", "Procurement Operations Analyst Dearborn MI",

        # Macomb County + Downriver + Grosse Pointes: no earlier anchor city reaches these
        # even at the widened 45mi radius_miles, so they get dedicated query anchors instead
        # of relying on overlap from the western/central Oakland-Wayne anchors above.
        "Wealth Operations Clinton Township MI", "Fintech Operations Clinton Township MI",
        "Business Operations Analyst Clinton Township MI", "Operations Specialist Clinton Township MI",
        "Salesforce Administrator Clinton Township MI", "Business Systems Analyst Roseville MI",
        "Financial Systems Process Automation Sterling Heights MI", "Client Operations Associate Mount Clemens MI",
        "Business Operations Analyst Trenton MI", "Operations Specialist Grosse Pointe MI"
    ],
    "query_bank_pointer": 0
}


def init_db():
    """Initializes local SQLite tables with WAL mode enabled for multithreaded concurrency."""
    with get_db_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            short_id TEXT PRIMARY KEY,
            sheet_uuid TEXT UNIQUE,
            job_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_hash TEXT PRIMARY KEY,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            seen_count INTEGER DEFAULT 1
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS company_cooldown (
            company_clean TEXT PRIMARY KEY,
            logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS search_filters (
            key TEXT PRIMARY KEY,
            value_json TEXT
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS gmail_drafts (
            to_email TEXT,
            subject TEXT,
            draft_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (to_email, subject)
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            sheet_uuid TEXT,
            source TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_activity (
            date TEXT PRIMARY KEY,
            drafts_staged INTEGER DEFAULT 0,
            applied_count INTEGER DEFAULT 0,
            notes_logged INTEGER DEFAULT 0
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS api_usage_counters (
            month_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            call_count INTEGER DEFAULT 0,
            PRIMARY KEY (month_key, provider)
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS query_pagination (
            query_text TEXT PRIMARY KEY,
            last_page INTEGER DEFAULT 1
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS crm_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payload_json TEXT NOT NULL,
            status TEXT DEFAULT 'PENDING',
            retry_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_attempt TIMESTAMP
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS application_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sheet_uuid TEXT NOT NULL,
            company TEXT,
            role TEXT,
            source TEXT,
            outreach_path TEXT,
            status TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_application_outcomes_sheet_uuid ON application_outcomes(sheet_uuid)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_application_outcomes_status ON application_outcomes(status)")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS email_enrichment_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sheet_uuid TEXT,
            provider TEXT NOT NULL,
            returned_email TEXT,
            confidence TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS company_identities (
            normalized_name TEXT PRIMARY KEY,
            display_name TEXT,
            primary_domain TEXT,
            aliases TEXT,
            ats_slug TEXT,
            crm_status TEXT,
            applied_at TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS system_alerts (
            alert_key TEXT PRIMARY KEY,
            last_sent TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_content_hashes (
            content_hash TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS sheet_row_map (
            sheet_uuid TEXT PRIMARY KEY,
            sheet_tab TEXT,
            sheet_row_index INTEGER,
            contact_name TEXT,
            contact_company TEXT,
            telegram_message_id INTEGER,
            contact_email TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sheet_row_map_tg_msg ON sheet_row_map(telegram_message_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sheet_row_map_contact_email ON sheet_row_map(contact_email)")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS followup_sequencer_log (
            sheet_uuid TEXT NOT NULL,
            run_date TEXT NOT NULL,
            action TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (sheet_uuid, run_date)
        )""")

        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM search_filters")
        if cursor.fetchone()[0] == 0:
            for k, v in DEFAULT_SEARCH_FILTERS.items():
                conn.execute("INSERT INTO search_filters (key, value_json) VALUES (?, ?)", (k, json.dumps(v)))
            conn.commit()

        # Merge newly-added exclusion tokens into any pre-existing search_filters rows, so upgrades
        # to an already-initialized local DB pick them up immediately without a manual table reset.
        merge_tokens = {
            "title_exclusions": [
                "administrative", "receptionist", "office assistant", "logistics clerk",
                "patient intake", "intake coordinator", "front desk", "office coordinator"
            ],
            "hard_ban_keywords": [
                "physical filing", "answering phones", "switchboard", "data entry clerk",
                "schedule travel arrangements", "clerical duties", "errands"
            ]
        }
        for key, new_tokens in merge_tokens.items():
            row = conn.execute("SELECT value_json FROM search_filters WHERE key = ?", (key,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO search_filters (key, value_json) VALUES (?, ?)", (key, json.dumps(new_tokens)))
                continue
            existing = json.loads(row[0]) if row[0] else []
            merged = existing + [t for t in new_tokens if t not in existing]
            if merged != existing:
                conn.execute("UPDATE search_filters SET value_json = ? WHERE key = ?", (json.dumps(merged), key))

        # Rolling query bank pointer: a scalar cursor, not merged/unioned - just ensured to exist
        # so pre-existing local DBs (created before the 100-query engine) pick it up too.
        if conn.execute("SELECT value_json FROM search_filters WHERE key = 'query_bank_pointer'").fetchone() is None:
            conn.execute("INSERT INTO search_filters (key, value_json) VALUES ('query_bank_pointer', ?)", (json.dumps(0),))
        conn.commit()

    hydrate_filters_from_sheets()
    restore_core_sourcing_filters()

def hydrate_filters_from_sheets():
    """On startup, pull load_system_config from Sheets so local filters reflect any manual spreadsheet edits.

    A remote value may not replace a populated local list with an empty or non-list value. A blank
    or non-JSON System_Config cell comes back from Code.gs as "" and, written straight through,
    silently zeroed target_queries on restart - /t then scanned 0 rules and pulled only the warm
    boards. Sheets can still edit a list; it just cannot blank one.
    """
    res = crm_get({"action": "load_system_config"})
    if not res or res.status_code != 200:
        return
    try:
        remote_filters = res.json().get("filters", {})
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for key, val in remote_filters.items():
                if key == "query_bank_pointer":
                    continue  # rolling-slice cursor is purely local - Sheets must never clobber it
                if not (isinstance(val, list) and val):
                    row = conn.execute("SELECT value_json FROM search_filters WHERE key = ?", (key,)).fetchone()
                    local_val = json.loads(row[0]) if row and row[0] else None
                    if isinstance(local_val, list) and local_val:
                        logging.warning(
                            f"[HYDRATE] Ignored System_Config '{key}' = {val!r}: would have replaced "
                            f"{len(local_val)} local entries with an empty/non-list value"
                        )
                        continue
                conn.execute("INSERT OR REPLACE INTO search_filters (key, value_json) VALUES (?, ?)", (key, json.dumps(val)))
            conn.commit()
        logging.info(f"Hydrated {len(remote_filters)} filters from Google Sheets System_Config")
    except Exception as e:
        logging.error(f"Filter Hydration Error: {e}")

def restore_core_sourcing_filters():
    """Put target_queries back from DEFAULT_SEARCH_FILTERS if it is missing, not a list, or empty.

    Without queries the pipeline skips JSearch entirely and a run pulls a handful of warm-board
    listings with no error anywhere, so an empty value is never a deliberate setting worth keeping.
    Reads the row directly rather than via get_filter, which swallows read errors and returns its
    default: a DB that is merely unreadable must not be mistaken for an empty one and overwritten.
    ats_company_slugs is not restored - its default is [] and the real list is built up live.
    Returns True if it restored the row.
    """
    try:
        with get_db_conn() as conn:
            row = conn.execute("SELECT value_json FROM search_filters WHERE key = 'target_queries'").fetchone()
            current = json.loads(row[0]) if row and row[0] else None
            if isinstance(current, list) and current:
                return False
            defaults = DEFAULT_SEARCH_FILTERS["target_queries"]
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR REPLACE INTO search_filters (key, value_json) VALUES ('target_queries', ?)", (json.dumps(defaults),))
            conn.commit()
    except Exception as e:
        logging.error(f"[FILTER RESTORE] Could not check target_queries, left untouched: {e}")
        return False
    logging.warning(f"[FILTER RESTORE] target_queries was {current!r}; restored {len(defaults)} default queries")
    # Repair the System_Config row too, or the next restart's hydration reads the bad value again.
    try:
        crm_post({"action": "update_system_config", "key": "target_queries", "value": defaults}, timeout=5)
    except Exception as e:
        logging.error(f"System_Config dual-write failed (target_queries restore): {e}")
    return True

init_db()

# ==============================================================================
# 2. FILTER & DYNAMIC CONFIGURATION HELPERS
# ==============================================================================
def safe_int(val, default=0):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

def safe_list(val):
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        return [v.strip() for v in val.split(",") if v.strip()]
    return []

def get_filter(key, default_val=None):
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value_json FROM search_filters WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row:
                return json.loads(row[0])
    except Exception as e:
        logging.error(f"Filter Read Error ({key}): {e}")
    return default_val

def set_filter(key, val):
    """Set filter atomically via BEGIN IMMEDIATE. Dual-write to System_Config sheet."""
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR REPLACE INTO search_filters (key, value_json) VALUES (?, ?)", (key, json.dumps(val)))
            conn.commit()
        # Dual-write to Google Sheets System_Config tab
        try:
            crm_post({"action": "update_system_config", "key": key, "value": val}, timeout=5)
        except Exception as e:
            logging.error(f"System_Config dual-write failed ({key}): {e}")
        return True
    except Exception as e:
        logging.error(f"Filter Write Error ({key}): {e}")
        return False

def update_filter_param(raw_key, raw_val_str):
    key = ALIAS_MAP.get(raw_key.lower().strip(), raw_key.lower().strip())
    current_val = get_filter(key)
    if current_val is None:
        return f"❌ Unknown filter parameter: <code>{raw_key}</code>"
    clean_val = raw_val_str.strip()
    if isinstance(current_val, list):
        op = None
        if clean_val.startswith("+"):
            op = "add"
            clean_val = clean_val[1:].strip()
        elif clean_val.startswith("-"):
            op = "remove"
            clean_val = clean_val[1:].strip()
        if op == "add":
            if clean_val.lower() not in [x.lower() for x in current_val]:
                current_val.append(clean_val)
        elif op == "remove":
            current_val = [x for x in current_val if x.lower() != clean_val.lower()]
        else:
            current_val = [x.strip() for x in clean_val.split(",") if x.strip()]
        set_filter(key, current_val)
        return f"⚙️ Filter <code>{key}</code> updated to: <code>{json.dumps(current_val)}</code>"
    else:
        if clean_val.startswith("+"):
            new_val = safe_int(current_val) + safe_int(clean_val[1:].strip())
        elif clean_val.startswith("-"):
            new_val = safe_int(current_val) - safe_int(clean_val[1:].strip())
        else:
            new_val = safe_int(clean_val)
        set_filter(key, new_val)
        return f"⚙️ Filter <code>{key}</code> updated to <code>{new_val:,}</code>."

def save_job_to_cache(short_id, job_dict, sheet_uuid=None):
    """Save job to cache atomically via BEGIN IMMEDIATE."""
    if sheet_uuid is None:
        sheet_uuid = str(uuid.uuid4())
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR REPLACE INTO jobs (short_id, sheet_uuid, job_json) VALUES (?, ?, ?)", 
                        (short_id, sheet_uuid, json.dumps(job_dict)))
            conn.commit()
    except Exception as e:
        logging.error(f"DB Save Error: {e}")
    return sheet_uuid

def get_job_from_cache(short_id):
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT job_json FROM jobs WHERE short_id = ?", (short_id,))
            row = cursor.fetchone()
            if row:
                return json.loads(row[0])
    except Exception as e:
        logging.error(f"DB Read Error: {e}")
    return {}

def get_sheet_uuid_by_short_id(short_id):
    """Resolve a cached job's sheet_uuid for metric attribution on later callback actions."""
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT sheet_uuid FROM jobs WHERE short_id = ?", (short_id,))
            row = cursor.fetchone()
            return row[0] if row else None
    except Exception as e:
        logging.error(f"DB Read Error (sheet_uuid lookup): {e}")
        return None

def get_short_id_by_sheet_uuid(sheet_uuid):
    """Reverse of get_sheet_uuid_by_short_id: the cached job's short_id for a sheet_uuid, or None.
    Used by the follow-up sequencer card so each row carries a short_id for /replied /interview.
    """
    if not sheet_uuid:
        return None
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT short_id FROM jobs WHERE sheet_uuid = ?", (sheet_uuid,))
            row = cursor.fetchone()
            return row[0] if row else None
    except Exception as e:
        logging.error(f"DB Read Error (short_id lookup): {e}")
        return None

def get_job_by_sheet_uuid(sheet_uuid):
    """Resolve cached job JSON by sheet_uuid, for swipe-reply commands like /prep and /pitch."""
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT job_json FROM jobs WHERE sheet_uuid = ?", (sheet_uuid,))
            row = cursor.fetchone()
            if row:
                return json.loads(row[0])
    except Exception as e:
        logging.error(f"DB Read Error (job by sheet_uuid): {e}")
    return {}

def update_job_target_email(sheet_uuid, new_email):
    """Overwrite the cached job JSON's target_email field by sheet_uuid (used by the manual /e Apollo override)."""
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            cursor.execute("SELECT job_json FROM jobs WHERE sheet_uuid = ?", (sheet_uuid,))
            row = cursor.fetchone()
            if not row:
                return False
            job_dict = json.loads(row[0])
            job_dict["target_email"] = new_email
            conn.execute("UPDATE jobs SET job_json = ? WHERE sheet_uuid = ?", (json.dumps(job_dict), sheet_uuid))
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"DB Job Email Update Error ({sheet_uuid}): {e}")
        return False

def is_job_seen_db(job_hash):
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM seen_jobs WHERE job_hash = ?", (job_hash,))
            return cursor.fetchone() is not None
    except Exception:
        return False

def save_seen_job_db(job_hash):
    """Upsert seen job hash atomically via BEGIN IMMEDIATE, tracking first/last seen + repost count."""
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                INSERT INTO seen_jobs (job_hash, first_seen, last_seen, seen_count)
                VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)
                ON CONFLICT(job_hash) DO UPDATE SET
                    last_seen = CURRENT_TIMESTAMP,
                    seen_count = seen_count + 1
            """, (job_hash,))
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"DB Seen Hash Error ({job_hash}): {e}")
        return False

def get_ghost_listing_penalty(job_hash):
    """Returns (score_penalty, badge) if a job hash has reposted >3 times across >45 days, else (0, "")."""
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT first_seen, seen_count FROM seen_jobs WHERE job_hash = ?", (job_hash,))
            row = cursor.fetchone()
            if not row or not row[0]:
                return 0, ""
            first_seen, seen_count = row
            first_seen_dt = datetime.strptime(str(first_seen)[:19], "%Y-%m-%d %H:%M:%S")
            days_active = (datetime.now() - first_seen_dt).days
            if seen_count > 3 and days_active > 45:
                return -15, " ⚠️ [REPOST / EVERGREEN]"
            return 0, ""
    except Exception as e:
        logging.error(f"Ghost Listing Penalty Error ({job_hash}): {e}")
        return 0, ""

def is_content_seen(content_hash: str) -> bool:
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM seen_content_hashes WHERE content_hash = ?", (content_hash,))
            return cursor.fetchone() is not None
    except Exception:
        return False

def save_content_hash(content_hash: str):
    try:
        with get_db_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO seen_content_hashes (content_hash) VALUES (?)", (content_hash,))
            conn.commit()
    except Exception:
        pass

def add_company_cooldown(company_name):
    """Add company cooldown atomically via BEGIN IMMEDIATE."""
    clean = normalize_company_for_match(company_name)
    if not clean:
        return False
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR REPLACE INTO company_cooldown (company_clean, logged_at) VALUES (?, CURRENT_TIMESTAMP)", (clean,))
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"DB Cooldown Save Error ({clean}): {e}")
        return False

def log_metric_event(event_type, sheet_uuid=None, source=None):
    """Persist a pipeline metric event (e.g. message_sent, interview_set) to SQLite atomically.
    `source` (jsearch/greenhouse/lever/ashby/manual_ingest) is optional, used for per-source
    discovery/screening counts in /outcomes and the Tuesday hub.
    """
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO pipeline_metrics (event_type, sheet_uuid, source, timestamp) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (event_type, sheet_uuid, source)
            )
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"DB Metric Log Error ({event_type}): {e}")
        return False

def get_metric_count(event_type):
    """Return the total persisted count of a given metric event_type."""
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM pipeline_metrics WHERE event_type = ?", (event_type,))
            return cursor.fetchone()[0]
    except Exception as e:
        logging.error(f"DB Metric Count Error ({event_type}): {e}")
        return 0

def record_application_outcome(sheet_uuid, status, company=None, role=None, source=None, outreach_path=None):
    """Append an application_outcomes row (event-sourced, one row per transition) so /outcomes and
    the Tuesday hub can compute evidence-based reply/interview rates and time-to-response, instead
    of relying on gut-feel. status is one of: applied, interview, rejection, offer, withdrawn.
    """
    if not sheet_uuid:
        return False
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO application_outcomes (sheet_uuid, company, role, source, outreach_path, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sheet_uuid, company, role, source, outreach_path, status)
            )
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"Application Outcome Record Error ({sheet_uuid}, {status}): {e}")
        return False

def get_outcome_metrics():
    """Aggregate application_outcomes into evidence-based conversion metrics:
    per-source applied/interview counts + reply rate, per-outreach-path interview rate, and the
    median days between an 'applied' row and its first subsequent response (interview/rejection/offer).
    """
    by_source = {}
    by_path = {}
    response_days = []
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT sheet_uuid, source, outreach_path, status, created_at FROM application_outcomes ORDER BY sheet_uuid, created_at ASC")
            rows = cursor.fetchall()
    except Exception as e:
        logging.error(f"Outcome Metrics Read Error: {e}")
        rows = []

    applied_at_by_uuid = {}
    for sheet_uuid, source, outreach_path, status, created_at in rows:
        source = source or "unknown"
        outreach_path = outreach_path or "unknown"
        by_source.setdefault(source, {"applied": 0, "interview": 0})
        by_path.setdefault(outreach_path, {"applied": 0, "interview": 0})
        if status == "applied":
            by_source[source]["applied"] += 1
            by_path[outreach_path]["applied"] += 1
            applied_at_by_uuid[sheet_uuid] = created_at
        elif status == "interview":
            by_source[source]["interview"] += 1
            by_path[outreach_path]["interview"] += 1
            applied_at = applied_at_by_uuid.get(sheet_uuid)
            if applied_at:
                try:
                    delta = datetime.strptime(str(created_at)[:19], "%Y-%m-%d %H:%M:%S") - datetime.strptime(str(applied_at)[:19], "%Y-%m-%d %H:%M:%S")
                    response_days.append(delta.total_seconds() / 86400.0)
                except Exception:
                    pass
        elif status in ("rejection", "offer"):
            applied_at = applied_at_by_uuid.get(sheet_uuid)
            if applied_at:
                try:
                    delta = datetime.strptime(str(created_at)[:19], "%Y-%m-%d %H:%M:%S") - datetime.strptime(str(applied_at)[:19], "%Y-%m-%d %H:%M:%S")
                    response_days.append(delta.total_seconds() / 86400.0)
                except Exception:
                    pass

    for bucket in (by_source, by_path):
        for stats in bucket.values():
            stats["reply_rate"] = (stats["interview"] / stats["applied"] * 100) if stats["applied"] else 0.0

    median_days = None
    if response_days:
        response_days.sort()
        mid = len(response_days) // 2
        median_days = response_days[mid] if len(response_days) % 2 else (response_days[mid - 1] + response_days[mid]) / 2

    return {"by_source": by_source, "by_outreach_path": by_path, "median_days_to_response": median_days}

def format_outcome_metrics_message():
    """Render get_outcome_metrics() into an HTML Telegram message, shared by /outcomes and the Tuesday hub."""
    metrics = get_outcome_metrics()
    lines = ["📈 <b>Evidence-Based Outcomes</b>\n"]

    if metrics["by_source"]:
        lines.append("<b>By Source (applied → interview, reply rate):</b>")
        for source, stats in sorted(metrics["by_source"].items()):
            lines.append(f"• {html.escape(source)}: {stats['applied']} → {stats['interview']} ({stats['reply_rate']:.1f}%)")
    else:
        lines.append("<b>By Source:</b> No applications recorded yet.")

    lines.append("")
    if metrics["by_outreach_path"]:
        lines.append("<b>By Outreach Path (applied → interview, rate):</b>")
        for path, stats in sorted(metrics["by_outreach_path"].items()):
            lines.append(f"• {html.escape(path)}: {stats['applied']} → {stats['interview']} ({stats['reply_rate']:.1f}%)")
    else:
        lines.append("<b>By Outreach Path:</b> No applications recorded yet.")

    lines.append("")
    if metrics["median_days_to_response"] is not None:
        lines.append(f"⏱️ <b>Median Days to First Response:</b> {metrics['median_days_to_response']:.1f}")
    else:
        lines.append("⏱️ <b>Median Days to First Response:</b> Not enough data yet.")

    return "\n".join(lines)

def get_rolling_metric_counts(days=7):
    """Return metric counts recorded during the trailing `days` window, including zero-count keys."""
    event_types = ("listing_discovered", "ai_screened", "gmail_draft_staged", "applied", "interview_set")
    counts = {event_type: 0 for event_type in event_types}
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT event_type, COUNT(*) FROM pipeline_metrics "
                "WHERE timestamp >= datetime('now', ?) AND event_type IN (?, ?, ?, ?, ?) GROUP BY event_type",
                (f"-{days} days", *event_types)
            )
            for event_type, count in cursor.fetchall():
                counts[event_type] = count
    except Exception as e:
        logging.error(f"Rolling Metric Read Error ({days}d): {e}")
    return counts

# ==============================================================================
# TEMPLATE REPLY-RATE REPORT (READ-ONLY)
# ==============================================================================
# Join path, no schema change: application_outcomes.sheet_uuid -> jobs.sheet_uuid
# (UNIQUE) -> json_extract(jobs.job_json, '$.outreach_template_id' / '$.linkedin_template_id').
# process_single_candidate() persists both ids on the cached job; the Gmail inbound poller
# and /apply, /offer write application_outcomes rows keyed by the same sheet_uuid, so a
# template id maps to its outcomes directly.
#
# Known gap: a GENERAL inbound reply (real human, not an interview/rejection) is routed to
# the CRM by route_inbound_reply_to_crm() but is NOT written to application_outcomes, so it
# is invisible here. "replied" below therefore means "got an interview / rejection / offer
# signal", the reply statuses that are actually persisted with a sheet_uuid.
TEMPLATE_REPLY_STATUSES = ("interview", "rejection", "offer")

def get_template_reply_rates():
    """READ-ONLY. Reply rate grouped by outreach_template_id and by linkedin_template_id,
    keeping the raw (sent, replied) counts visible so a 1/1 never hides behind "100%".

    Definitions (event-sourced application_outcomes, one row per transition):
      sent    - a sheet_uuid with an 'applied' row (/apply records this when outreach goes
                out; it is the send signal joinable to a template id).
      replied - that same sheet_uuid also has an 'interview', 'rejection', or 'offer' row.
                'withdrawn' is Kevin's own action, not a reply, so it never counts.

    Returns:
      {"by_outreach_template": {tid_or_None: {"sent", "replied", "reply_rate"}},
       "by_linkedin_template":  {tid_or_None: {...}},
       "totals": {"sent", "replied", "reply_rate"},
       "unjoinable_applied": <'applied' rows whose sheet_uuid has no cached job>}
    A None template id key means the cached job predates id persistence (json_extract -> NULL).
    Never writes.
    """
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT ao.sheet_uuid, ao.status, "
                "  json_extract(j.job_json, '$.outreach_template_id') AS otid, "
                "  json_extract(j.job_json, '$.linkedin_template_id') AS ltid, "
                "  (j.sheet_uuid IS NULL) AS job_missing "
                "FROM application_outcomes ao "
                "LEFT JOIN jobs j ON j.sheet_uuid = ao.sheet_uuid"
            )
            rows = cursor.fetchall()
    except Exception as e:
        logging.error(f"Template Reply Rate Read Error: {e}")
        rows = []

    # Collapse the event rows to one record per application (sheet_uuid).
    per_uuid = {}
    for sheet_uuid, status, otid, ltid, job_missing in rows:
        rec = per_uuid.setdefault(
            sheet_uuid,
            {"statuses": set(), "otid": None, "ltid": None, "job_missing": bool(job_missing)},
        )
        rec["statuses"].add(status)
        if rec["otid"] is None:
            rec["otid"] = otid
        if rec["ltid"] is None:
            rec["ltid"] = ltid

    by_outreach, by_linkedin = {}, {}
    totals = {"sent": 0, "replied": 0}
    unjoinable_applied = 0

    for rec in per_uuid.values():
        if "applied" not in rec["statuses"]:
            continue
        if rec["job_missing"]:
            unjoinable_applied += 1
            continue
        replied = bool(rec["statuses"].intersection(TEMPLATE_REPLY_STATUSES))
        for bucket, tid in ((by_outreach, rec["otid"]), (by_linkedin, rec["ltid"])):
            stats = bucket.setdefault(tid, {"sent": 0, "replied": 0})
            stats["sent"] += 1
            if replied:
                stats["replied"] += 1
        totals["sent"] += 1
        if replied:
            totals["replied"] += 1

    for bucket in (by_outreach, by_linkedin):
        for stats in bucket.values():
            stats["reply_rate"] = (stats["replied"] / stats["sent"] * 100) if stats["sent"] else 0.0
    totals["reply_rate"] = (totals["replied"] / totals["sent"] * 100) if totals["sent"] else 0.0

    return {
        "by_outreach_template": by_outreach,
        "by_linkedin_template": by_linkedin,
        "totals": totals,
        "unjoinable_applied": unjoinable_applied,
    }

def format_template_reply_rates_message():
    """Render get_template_reply_rates() as an HTML Telegram message (used by /treplies)."""
    data = get_template_reply_rates()
    lines = [
        "📊 <b>Reply Rate by Template</b>",
        "<i>sent = has an 'applied' outcome · replied = also got interview/rejection/offer</i>",
    ]

    for label, bucket in (
        ("Outreach email — by outreach_template_id", data["by_outreach_template"]),
        ("LinkedIn note — by linkedin_template_id", data["by_linkedin_template"]),
    ):
        lines.append("")
        lines.append(f"<b>{label}:</b>")
        if not bucket:
            lines.append("• No sent outreach recorded yet.")
            continue
        for tid in sorted(bucket, key=lambda t: (t is None, t if t is not None else -1)):
            stats = bucket[tid]
            tid_str = "(unset)" if tid is None else f"#{tid}"
            lines.append(
                f"• {tid_str}: {stats['sent']} sent → {stats['replied']} replied "
                f"({stats['reply_rate']:.1f}%)"
            )

    t = data["totals"]
    lines.append("")
    lines.append(
        f"<b>All templates:</b> {t['sent']} sent → {t['replied']} replied ({t['reply_rate']:.1f}%)"
    )
    if data["unjoinable_applied"]:
        lines.append(
            f"<i>{data['unjoinable_applied']} applied row(s) skipped — no cached job to "
            f"read a template id from.</i>"
        )
    return "\n".join(lines)

DAILY_ACTIVITY_COLUMNS = ("drafts_staged", "applied_count", "notes_logged")

def log_daily_activity(activity_type):
    """Increment today's daily_activity counter atomically for a valid activity_type."""
    if activity_type not in DAILY_ACTIVITY_COLUMNS:
        logging.error(f"Invalid daily_activity type: {activity_type}")
        return False
    today_str = datetime.now().strftime("%Y-%m-%d")
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"INSERT INTO daily_activity (date, {activity_type}) VALUES (?, 1) "
                f"ON CONFLICT(date) DO UPDATE SET {activity_type} = {activity_type} + 1",
                (today_str,)
            )
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"DB Daily Activity Error ({activity_type}): {e}")
        return False

def get_daily_activity(date_str):
    """Return {drafts_staged, applied_count, notes_logged} for a given date, zeroed if no row exists."""
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT drafts_staged, applied_count, notes_logged FROM daily_activity WHERE date = ?",
                (date_str,)
            )
            row = cursor.fetchone()
            if row:
                return {"drafts_staged": row[0], "applied_count": row[1], "notes_logged": row[2]}
    except Exception as e:
        logging.error(f"DB Daily Activity Read Error ({date_str}): {e}")
    return {"drafts_staged": 0, "applied_count": 0, "notes_logged": 0}

def get_lifetime_activity_totals():
    """Return lifetime SUM() totals across all daily_activity rows."""
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COALESCE(SUM(drafts_staged),0), COALESCE(SUM(applied_count),0), COALESCE(SUM(notes_logged),0) FROM daily_activity")
            row = cursor.fetchone()
            return {"drafts_staged": row[0], "applied_count": row[1], "notes_logged": row[2]}
    except Exception as e:
        logging.error(f"DB Lifetime Activity Error: {e}")
        return {"drafts_staged": 0, "applied_count": 0, "notes_logged": 0}

def increment_api_usage_counter(provider):
    """Bump this calendar month's local call counter for a paid email-lookup provider
    (e.g. "hunter", "prospeo", "getprospect"). This is a local approximation for /health
    visibility only - the provider's own dashboard is the authoritative quota source.
    """
    month_key = datetime.now().strftime("%Y-%m")
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO api_usage_counters (month_key, provider, call_count) VALUES (?, ?, 1) "
                "ON CONFLICT(month_key, provider) DO UPDATE SET call_count = call_count + 1",
                (month_key, provider)
            )
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"DB API Usage Counter Error ({provider}): {e}")
        return False

def get_monthly_api_usage():
    """Return {"hunter": n, "prospeo": n, "getprospect": n} local call counts for the current
    calendar month.
    """
    month_key = datetime.now().strftime("%Y-%m")
    counts = {"hunter": 0, "prospeo": 0, "getprospect": 0}
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT provider, call_count FROM api_usage_counters WHERE month_key = ?", (month_key,))
            for provider, call_count in cursor.fetchall():
                counts[provider] = call_count
    except Exception as e:
        logging.error(f"DB API Usage Read Error: {e}")
    return counts

def log_email_enrichment_attempt(sheet_uuid, provider, returned_email, confidence):
    """Persist one resolve_email_waterfall() outcome (verified hit vs unverified fallback guess) so
    contact quality can be audited later - never gates behavior on its own, see is_unverified_email().
    """
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO email_enrichment_attempts (sheet_uuid, provider, returned_email, confidence) VALUES (?, ?, ?, ?)",
                (sheet_uuid, provider, returned_email, confidence)
            )
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"Email Enrichment Attempt Log Error ({sheet_uuid}): {e}")
        return False

def get_query_start_page(query_text):
    """Return the next JSearch page offset to resume from for this exact query text, default 1."""
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT last_page FROM query_pagination WHERE query_text = ?", (query_text,))
            row = cursor.fetchone()
            return row[0] if row else 1
    except Exception as e:
        logging.error(f"Query Pagination Read Error ({query_text}): {e}")
        return 1

def save_query_next_page(query_text, next_page):
    """Persist the rolling page offset for this query so the next /t run resumes past this batch instead of re-fetching page 1."""
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO query_pagination (query_text, last_page) VALUES (?, ?) "
                "ON CONFLICT(query_text) DO UPDATE SET last_page = excluded.last_page",
                (query_text, next_page)
            )
            conn.commit()
    except Exception as e:
        logging.error(f"Query Pagination Write Error ({query_text}): {e}")

def calculate_active_day_streak():
    """Count consecutive active days (any activity logged) ending today or yesterday."""
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT date FROM daily_activity WHERE (drafts_staged + applied_count + notes_logged) > 0 ORDER BY date DESC")
            active_dates = {row[0] for row in cursor.fetchall()}
    except Exception as e:
        logging.error(f"DB Streak Calc Error: {e}")
        return 0

    cursor_date = datetime.now().date()
    if cursor_date.strftime("%Y-%m-%d") not in active_dates:
        cursor_date -= timedelta(days=1)  # allow the streak to still count if today has no activity yet

    streak = 0
    while cursor_date.strftime("%Y-%m-%d") in active_dates:
        streak += 1
        cursor_date -= timedelta(days=1)
    return streak

def render_ascii_funnel(stages):
    """Render an ASCII bar funnel from a list of (label, count) tuples, bar widths scaled to the largest count."""
    max_count = max((c for _, c in stages), default=0)
    max_bar_width = 20
    lines = []
    for label, count in stages:
        bar_len = int((count / max_count) * max_bar_width) if max_count > 0 else 0
        bar = "█" * bar_len
        lines.append(f"{label:<22} {bar} {count}")
    return "\n".join(lines)

def is_company_on_cooldown(company_name):
    clean = normalize_company_for_match(company_name)
    if not clean:
        return False
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT logged_at FROM company_cooldown WHERE company_clean = ? AND logged_at >= datetime('now', '-14 days')", (clean,))
            return cursor.fetchone() is not None
    except Exception:
        return False

def save_message_mapping(telegram_message_id, sheet_uuid, sheet_tab="", contact_name="", contact_company="", contact_email=""):
    """Persist (telegram_message_id, sheet_uuid, sheet_tab, contact_email) atomically so swipe-replies
    can resolve the CRM row and inbound mail can be matched against the CRM whitelist.
    """
    if not telegram_message_id or not sheet_uuid:
        return False
    clean_email = str(contact_email or "").split(" [")[0].strip().lower()
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                INSERT OR REPLACE INTO sheet_row_map 
                (sheet_uuid, sheet_tab, contact_name, contact_company, telegram_message_id, contact_email, created_at)
                VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM sheet_row_map WHERE sheet_uuid = ?), CURRENT_TIMESTAMP))
            """, (sheet_uuid, sheet_tab, contact_name, contact_company, telegram_message_id, clean_email, sheet_uuid))
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"DB Message Mapping Save Error ({telegram_message_id}): {e}")
        return False

def record_captured_contact(sheet_uuid, sheet_tab, contact_name="", contact_company="", contact_email=""):
    """Persist a CRM contact that was created without a Telegram card behind it.

    save_message_mapping() is the usual writer for sheet_row_map, but it keys on
    telegram_message_id and bails when there isn't one. Contacts auto-captured from sent mail have
    no card, so they never reached the table - which left is_verified_crm_contact() checking a
    table that could not contain them, and made the sent-mail poller re-capture the same person on
    every cycle. telegram_message_id is left NULL here; nothing reads it for these rows.
    """
    if not sheet_uuid:
        return False
    clean_email = str(contact_email or "").split(" [")[0].strip().lower()
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                INSERT OR REPLACE INTO sheet_row_map
                (sheet_uuid, sheet_tab, contact_name, contact_company, contact_email, created_at)
                VALUES (?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM sheet_row_map WHERE sheet_uuid = ?), CURRENT_TIMESTAMP))
            """, (sheet_uuid, sheet_tab, contact_name, contact_company, clean_email, sheet_uuid))
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"DB Captured Contact Save Error ({sheet_uuid}): {e}")
        return False

def get_mapping_from_message_id(telegram_message_id):
    """Resolve a replied-to Telegram message back to its CRM sheet_uuid/tab, or None if unmapped."""
    if not telegram_message_id:
        return None
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT sheet_uuid, sheet_tab, contact_name, contact_company FROM sheet_row_map WHERE telegram_message_id = ?",
                (telegram_message_id,)
            )
            row = cursor.fetchone()
            if row:
                return {"sheet_uuid": row[0], "sheet_tab": row[1], "contact_name": row[2], "contact_company": row[3]}
            return None
    except Exception as e:
        logging.error(f"DB Message Mapping Lookup Error ({telegram_message_id}): {e}")
        return None

def get_contact_by_sheet_uuid(sheet_uuid):
    """Resolve contact_name/contact_company from sheet_row_map for the auto-stage bump action."""
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT contact_name, contact_company FROM sheet_row_map WHERE sheet_uuid = ?", (sheet_uuid,))
            row = cursor.fetchone()
            if row:
                return {"name": row[0], "company": row[1]}
    except Exception as e:
        logging.error(f"Contact Lookup Error ({sheet_uuid}): {e}")
    return None

def build_crm_payload(action, sheet_uuid=None, **kwargs):
    """Standardize outbound CRM payloads: every action includes rowOperationOrder DESC for bottom-to-top Apps Script loops."""
    payload = {"action": action, "rowOperationOrder": "DESC"}
    if sheet_uuid:
        payload["sheet_uuid"] = sheet_uuid
    payload.update(kwargs)
    return payload

def _parse_company_title_from_card_text(text):
    """Extracts (company, title) from a dispatched job-card's Telegram text via its 💼/🏢 markers,
    for stale swipe-reply recovery when the local sheet_row_map mapping has been lost/evicted.
    Tags are optional since mobile/desktop Telegram clients strip <b>/<code> from reply-to text.
    Returns (None, None) if either marker is missing.
    """
    if not text:
        return None, None
    title_match = re.search(r'💼\s*(?:<b>)?(.*?)(?:</b>)?(?:\n|$)', text)
    company_match = re.search(r'🏢\s*(?:<b>)?(.*?)(?:</b>)?(?:\n|$)', text)
    if not (title_match and company_match):
        return None, None
    return html.unescape(company_match.group(1)).strip(), html.unescape(title_match.group(1)).strip()

def _parse_sheet_uuid_from_card_text(text):
    """Extracts (sheet_uuid, sheet_tab) embedded directly in a dispatched card's own 🆔 marker.
    Unlike sheet_row_map, this survives SQLite wipes from container restarts/redeploys since the
    durable copy lives in the Telegram message itself, not the ephemeral local DB. Tags are optional
    since mobile/desktop Telegram clients strip <code> from reply-to text.
    Returns (None, None) if the marker is missing.
    """
    if not text:
        return None, None
    match = re.search(r'🆔\s*(?:<code>)?([0-9a-fA-F\-]{36})(?:</code>)?(?:\s*·\s*(?:<code>)?([^<\n]*)(?:</code>)?)?', text)
    if not match:
        return None, None
    return match.group(1), html.unescape((match.group(2) or "Pipeline_Candidates").strip())

def _parse_routing_from_card_text(text):
    """Extracts Gemini's routing decisions from a card's own 🧭 marker: the resume track letter,
    tone mode, bullet indices and outreach template id, as "a|conservative|0,1,2,3|4".

    These live only in the ephemeral jobs cache, so before this marker a deploy left /draft and /e
    with no way to rebuild the copy or the resume they had already routed - the card was blocked
    outright rather than degraded. Embedding them in the Telegram message makes the cache a pure
    optimization: the message is the durable copy, exactly as it already is for sheet_uuid.

    Returns {} when the marker is absent (any card dispatched before this shipped).
    """
    if not text:
        return {}
    match = re.search(r'🧭\s*(?:<code>)?([a-e])\|(conservative|tech)\|([0-9,]*)\|(\d+)(?:</code>)?', str(text))
    if not match:
        return {}
    bullets = [int(i) for i in match.group(3).split(",") if i.strip().isdigit()]
    return {
        "track": match.group(1),
        "tone_mode": match.group(2),
        "bullet_indices": bullets or None,
        "outreach_template_id": int(match.group(4)),
    }

def _fuzzy_find_job_in_sheets(company, title):
    """Searches Tetiana Cold then Clavicular via get_followups for a legal-suffix/case-insensitive
    dedup-hash match on (company, title), returning (sheet_uuid, sheet_tab) or None.
    """
    target_hash = generate_dedup_hash(company, title)
    for target_code, sheet_tab in (("TC", "Tetiana Cold"), ("CL", "Clavicular")):
        for record in fetch_networking_cards(target_code, qty=None):
            if generate_dedup_hash(record.get("company"), record.get("title")) == target_hash:
                sheet_uuid = record.get("sheet_uuid")
                if sheet_uuid:
                    return sheet_uuid, sheet_tab
    return None

def _fuzzy_find_job_in_local_cache(company, title):
    """Scans the local SQLite jobs cache for a dedup-hash match on (company, title), returning
    sheet_uuid or None. Cheaper than the Sheets lookup - tried first in resolve_reply_mapping.
    """
    target_hash = generate_dedup_hash(company, title)
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT sheet_uuid, job_json FROM jobs")
            for sheet_uuid, job_json in cursor.fetchall():
                try:
                    job_dict = json.loads(job_json)
                except (json.JSONDecodeError, TypeError):
                    continue
                if generate_dedup_hash(job_dict.get("employer_name"), job_dict.get("job_title")) == target_hash:
                    return sheet_uuid
    except Exception as e:
        logging.error(f"Fuzzy Local Job Cache Lookup Error ({company}/{title}): {e}")
    return None

def resolve_reply_mapping(msg, chat_id, command_label):
    """For swipe-reply commands, resolve reply_to_message -> sheet_uuid mapping.
    Recovery order: 1) local sheet_row_map (fast path), 2) the sheet_uuid embedded directly in
    the card's own 🆔 marker - survives sheet_row_map being wiped by a container restart/redeploy
    since it never depended on local SQLite in the first place, 3) fuzzy company/title recovery
    (local jobs cache, then Sheets Tetiana Cold / Clavicular) for older cards sent before the 🆔
    marker existed. Any successful recovery re-persists the mapping so future replies to the same
    card resolve instantly again.
    Sends a Telegram warning and returns None if reply context or mapping is missing.
    """
    reply_msg = msg.get("reply_to_message")
    if not reply_msg:
        send_telegram_message(chat_id, "⚠️ <b>Context Missing:</b> Please swipe-reply directly to a job card or contact card to use this command.")
        return None
    reply_message_id = reply_msg.get("message_id")
    mapping = get_mapping_from_message_id(reply_message_id)
    if mapping:
        return mapping

    card_text = reply_msg.get("text", "")
    sheet_uuid, sheet_tab = _parse_sheet_uuid_from_card_text(card_text)
    if sheet_uuid:
        save_message_mapping(reply_message_id, sheet_uuid, sheet_tab)
        logging.info(f"[RECOVERY] Recovered sheet_uuid={sheet_uuid} directly from card's 🆔 marker (message_id={reply_message_id})")
        return {"sheet_uuid": sheet_uuid, "sheet_tab": sheet_tab, "contact_name": "", "contact_company": ""}

    company, title = _parse_company_title_from_card_text(card_text)
    if company and title:
        sheet_uuid = _fuzzy_find_job_in_local_cache(company, title)
        sheet_tab = "Pipeline_Candidates"
        if not sheet_uuid:
            recovered = _fuzzy_find_job_in_sheets(company, title)
            if recovered:
                sheet_uuid, sheet_tab = recovered
        if sheet_uuid:
            save_message_mapping(reply_message_id, sheet_uuid, sheet_tab, "", company, "")
            logging.info(f"[RECOVERY] Fuzzy-matched lost mapping for '{company}' / '{title}' -> {sheet_uuid} ({sheet_tab})")
            return {"sheet_uuid": sheet_uuid, "sheet_tab": sheet_tab, "contact_name": "", "contact_company": company}

    send_telegram_message(chat_id, f"⚠️ <b>Record Not Found:</b> No CRM record is mapped to this card for <code>{html.escape(command_label)}</code>. Please retry with /t or /c to regenerate it.")
    return None

# Sent when a swipe-reply command resolves a mapping but has no company data behind it - see
# _job_data_available. Names the real cause (a restart wiped the cache, nothing expired by TTL)
# so the fix - resurface the card - is obvious from the message itself.
STALE_CARD_WARNING = (
    "⚠️ <b>Card Data Gone:</b> This card predates the last deploy/restart, and the jobs cache "
    "it was built from lives on ephemeral disk. Reply <code>/t</code> or <code>/c</code> to "
    "resurface fresh cards, then re-run the command on one of those."
)

# Sent when a command rebuilt its job from the card text instead of the wiped cache. Never silent:
# a card dispatched before the 🧭 marker shipped recovers company/title but falls back to a
# default-routed resume, and shipping a track-A PDF for a compliance role without saying so is
# worse than the old hard block.
CARD_RECOVERED_NOTICE = (
    "♻️ <b>Rebuilt from the card</b> - the jobs cache was wiped by a deploy. Copy and routing "
    "came from the card itself. If it carries no <code>🧭</code> line, the resume is default-routed; "
    "reply <code>/t</code> for a fresh card when the tailored one matters."
)

def rebuild_job_from_card(job, card_text):
    """Backfill a wiped job dict from the card's own text so /draft and /e still work.

    The jobs cache lives on Render's ephemeral disk and is wiped by every deploy, but the card is
    a Telegram message and is not: company, title, sheet_uuid and (since the 🧭 marker) Gemini's
    routing all survive there. Everything /draft and /e actually need is therefore recoverable -
    the email body is interpolated in Python from the local template banks, never from the job
    description, so no cached prose is required.

    Returns (job, recovered) where `recovered` is True only when the cache was empty and the card
    supplied the data, so the caller can say so rather than silently shipping a default-routed
    resume. A card with no 🧭 marker (dispatched before this shipped) still recovers company and
    title; only the routing falls back to defaults.
    """
    job = dict(job or {})
    if job.get("employer_name"):
        return job, False
    company, title = _parse_company_title_from_card_text(card_text)
    if not (company and title):
        return job, False
    job["employer_name"] = company
    job["job_title"] = title
    for key, value in _parse_routing_from_card_text(card_text).items():
        if value is not None:
            job.setdefault(key, value)
    return job, True

def _job_data_available(job, mapping):
    """True if either the cached job JSON or the CRM mapping has real company data to work from.
    False only when both are empty, meaning the caller is about to fall through to a hardcoded
    placeholder like 'Target Firm' - the signal that a restart wiped the jobs cache for this
    sheet_uuid with no CRM fallback available. A /quick Carmen contact (no cached job, but a
    contact_company on the mapping) is a legitimate path, not a symptom, and passes.
    """
    return bool(job.get("employer_name")) or bool(mapping.get("contact_company"))

# ==============================================================================
# 3. DYNAMIC PRIORITY DECAY & ANTI-FLUFF EMAIL ENGINE
# ==============================================================================
_WARM_CRM_CACHE = {"data": {}, "fetched_at": 0.0}
_WARM_CRM_CACHE_TTL_SECONDS = 300
_APPLIED_CRM_CACHE = {"data": set(), "fetched_at": 0.0}
_APPLIED_CRM_CACHE_TTL_SECONDS = 300

def normalize_company_for_match(company_name):
    """Lowercase and strip legal suffixes so CRM and scraped company-name variants compare reliably."""
    company = str(company_name or "").strip().lower()
    company = re.sub(r'\b(inc|llc|ltd|corp|corporation|co|holdings|plc|group)\b\.?', '', company, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', company).strip()

def upsert_company_identity(company_name, ats_slug=None, crm_status=None, applied=False):
    """Merge newly-learned facts about a company into the canonical company_identities record,
    keyed by normalize_company_for_match() so 'Acme Corp' and 'Acme Corp Inc.' share one row.
    Never overwrites a field with an empty value - only adds/updates what's newly known.
    """
    normalized = normalize_company_for_match(company_name)
    if not normalized:
        return False
    display_name = str(company_name or "").strip()
    applied_at = datetime.now().strftime("%Y-%m-%d") if applied else None
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            cursor.execute("SELECT display_name, primary_domain, aliases, ats_slug, crm_status, applied_at FROM company_identities WHERE normalized_name = ?", (normalized,))
            row = cursor.fetchone()
            if row:
                existing_display, existing_domain, existing_aliases, existing_slug, existing_status, existing_applied_at = row
                aliases = set(filter(None, (existing_aliases or "").split("|")))
                if display_name and display_name != existing_display:
                    aliases.add(display_name)
                merged_aliases = "|".join(sorted(aliases))
                conn.execute(
                    "UPDATE company_identities SET display_name = ?, aliases = ?, ats_slug = COALESCE(?, ats_slug), "
                    "crm_status = COALESCE(?, crm_status), applied_at = COALESCE(?, applied_at), updated_at = CURRENT_TIMESTAMP "
                    "WHERE normalized_name = ?",
                    (existing_display or display_name, merged_aliases, ats_slug, crm_status, applied_at, normalized)
                )
            else:
                conn.execute(
                    "INSERT INTO company_identities (normalized_name, display_name, aliases, ats_slug, crm_status, applied_at) "
                    "VALUES (?, ?, '', ?, ?, ?)",
                    (normalized, display_name, ats_slug, crm_status, applied_at)
                )
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"Company Identity Upsert Error ({normalized}): {e}")
        return False

def get_applied_crm_companies():
    """Fetch Tetiana Warm companies as a short-lived suppression set for fresh job discovery."""
    now = time.time()
    if now - _APPLIED_CRM_CACHE["fetched_at"] < _APPLIED_CRM_CACHE_TTL_SECONDS:
        return _APPLIED_CRM_CACHE["data"]
    res = crm_post({"action": "get_followups", "tab": "TW"})
    if not res:
        return _APPLIED_CRM_CACHE["data"]
    try:
        if res.status_code != 200:
            return _APPLIED_CRM_CACHE["data"]
        data = res.json()
        if data.get("status") != "success":
            return _APPLIED_CRM_CACHE["data"]
        applied_companies = set()
        for row in data.get("followups", []):
            raw_company = str(row.get("company") or "").strip().lower()
            if raw_company:
                applied_companies.add(raw_company)
                normalized_company = normalize_company_for_match(raw_company)
                if normalized_company:
                    applied_companies.add(normalized_company)
        _APPLIED_CRM_CACHE["data"] = applied_companies
        _APPLIED_CRM_CACHE["fetched_at"] = now
    except Exception as e:
        logging.error(f"get_applied_crm_companies Error: {e}")
    return _APPLIED_CRM_CACHE["data"]

def get_warm_crm_contacts():
    """Fetch every Carmen Warm CRM contact keyed by lowercased company name, each tagged with a
    normalized 1-10 priority_score for the Dynamic Contact Quality Multiplier.
    Cached in-process for a few minutes so parallel candidate evaluation doesn't hammer the CRM webhook.
    """
    now = time.time()
    if now - _WARM_CRM_CACHE["fetched_at"] < _WARM_CRM_CACHE_TTL_SECONDS:
        return _WARM_CRM_CACHE["data"]
    res = crm_post({"action": "get_followups", "tab": "CW"})
    if not res:
        return _WARM_CRM_CACHE["data"]
    try:
        if res.status_code != 200:
            return _WARM_CRM_CACHE["data"]
        data = res.json()
        if data.get("status") != "success":
            return _WARM_CRM_CACHE["data"]
        contacts = {}
        for row in data.get("followups", []):
            company = str(row.get("company") or "").strip()
            raw_name = str(row.get("name") or "").strip()
            # Fallback: infer company from the trailing token of the contact name when Column C is blank
            if not company and raw_name and not raw_name.lower().startswith("http"):
                name_parts = raw_name.split()
                if len(name_parts) >= 2:
                    company = name_parts[-1]
            if not company:
                continue
            contacts[normalize_company_for_match(company)] = {
                "name": row.get("name") or "Contact",
                "raw_company": company,
                "email": row.get("email", ""),
                "note": row.get("note") or "Active relationship",
                "priority_score": normalize_priority_value(row.get("raw_priority", row.get("priority"))),
                "sheet_uuid": row.get("sheet_uuid", "")
            }
        _WARM_CRM_CACHE["data"] = contacts
        _WARM_CRM_CACHE["fetched_at"] = now
    except Exception as e:
        logging.error(f"get_warm_crm_contacts Error: {e}")
    return _WARM_CRM_CACHE["data"]

def sanitize_text(text):
    """Strip corporate fluff/AI clichés while preserving apostrophes, hyphens, and paragraph breaks.
    Buzzword list is hot-reloaded from evidence_bank.json's banned_words on every call.
    """
    if not text:
        return ""
    cleaned = str(text)
    cleaned = re.sub(r'[\u2014\u2013]', "", cleaned)  # em-dash / en-dash only
    cleaned = re.sub(r'[;:]', "", cleaned)
    buzzwords = load_evidence_bank().get("banned_words", []) or ["leveraging", "passionate", "seamless", "synergy", "cutting-edge", "paradigm"]
    for bw in buzzwords:
        cleaned = re.sub(rf'\b{re.escape(bw)}\b', "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b(\w+),\s*(\w+),\s*and\s*(\w+)\b', r'\1 and \2', cleaned)
    cleaned = re.sub(r'[ \t]+', ' ', cleaned)  # collapse horizontal whitespace only
    cleaned = re.sub(r' *\n *', '\n', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)  # cap excess blank lines, keep \n\n breaks
    return cleaned.strip()

def get_current_role_blurb():
    """Returns (core_exp_phrase, full_sentence) cleaned of job-title suffixes
    so sentences like 'background in wealth operations' read naturally.
    """
    experience = load_evidence_bank().get("experience", [])
    if not experience:
        return "wealth operations and process automation", "I am currently working in wealth operations and process automation."
    current = experience[0]
    title = current.get("title", "")
    company = current.get("company", "")
    location = current.get("location", "")

    # Strip trailing role nouns (Specialist, Intern, Lead, Analyst, Manager)
    clean_domain = re.sub(r'\b(specialist|intern|lead|analyst|manager|associate|coordinator)\b', '', title, flags=re.IGNORECASE).strip().lower()
    clean_domain = re.sub(r'\s+', ' ', clean_domain)

    core_exp = f"{clean_domain} and process automation" if clean_domain else "wealth operations and process automation"
    sentence = f"I am currently working as a {title} at {company}" + (f" in {location}" if location else "") + "."
    return core_exp, sentence

def clean_company_for_copy(company_name):
    """Drops legal-entity suffixes so outreach copy reads 'Atwell', not 'Atwell Group, Inc.'.
    Falls back to the raw value whenever stripping would leave nothing behind.
    """
    raw = str(company_name or "")
    clean = re.sub(r'\b(inc|llc|ltd|corp|corporation|co|holdings|plc|group)\b\.?', '', raw, flags=re.IGNORECASE).strip().rstrip(',')
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean or raw or "your team"

def resume_pdf_filename(company_name):
    """The filename a recruiter sees on the resume attachment: "Kevin_Miller_Resume_Atwell.pdf".

    Three things this fixes over the old inline f-string, which was copy-pasted at five call sites:

      - No Track letter. Track A-E is the internal bullet-pool routing key
        (TRACK_BULLET_POOL_KEYS); to a recruiter "TrackE" is meaningless, and it advertises that
        the resume is one of five machine-generated variants. The track is still shown in the
        Telegram caption, so Kevin can see which one he sent.
      - Separators are converted, not deleted. The old re.sub(r'[^a-zA-Z0-9]', '') welded
        "thyssenkrupp Materials CA Ltd" into "thyssenkruppMaterialsCALtd".
      - Legal suffixes are stripped, reusing clean_company_for_copy() so the attachment and the
        email body refer to the company the same way.

    Company casing is preserved verbatim: "thyssenkrupp" styles its own name lowercase, and
    title-casing it would be wrong in a way a recruiter there would notice.
    """
    raw = str(company_name or "").strip()
    # clean_company_for_copy() falls back to "your team" for an empty company, which reads fine
    # mid-sentence but not as "Kevin_Miller_Resume_your_team.pdf" - guard before calling it.
    if not raw or is_placeholder_company_name(raw):
        return "Kevin_Miller_Resume.pdf"
    clean = clean_company_for_copy(raw)
    # "LLP" outlives clean_company_for_copy()'s suffix list (it has llc/plc, not llp), and it is
    # as much noise on a filename as "Inc." is.
    clean = re.sub(r'\bllp\b\.?', '', clean, flags=re.IGNORECASE).strip().rstrip(',')
    # "&" joins words rather than separating them: "AT&T" -> "ATT", not "AT_T".
    clean = clean.replace("&", "")
    # Collapse each remaining run of non-alphanumerics to one underscore, so "A.B. Smith" -> "A_B_Smith".
    slug = re.sub(r'[^A-Za-z0-9]+', '_', clean).strip('_')
    # 64 chars keeps the whole filename comfortably clear of the 255-byte limit some ATS
    # upload forms and Windows paths enforce, without truncating any realistic company name.
    slug = slug[:64].rstrip('_')
    return f"Kevin_Miller_Resume_{slug}.pdf" if slug else "Kevin_Miller_Resume.pdf"

def render_outreach_email(pool_key, template_id=0, name="", company="", job_title=""):
    """THE single rendering path for every candidate-facing email body, cold or warm or bump.

    Both consumers go through here - the Telegram card (process_single_candidate) and the Gmail
    draft (/draft, /e, /eh, batch bumps) - so the email Kevin approves on the card is the exact
    string that lands in the draft. Keeping one path is the whole point: the previous split, where
    the card read templates/outreach_templates.json and Gmail used hardcoded f-strings, is what let
    the two voices drift apart.

    Still Strict Deterministic Template Engine: Gemini only routed `template_id`, an integer that
    resolve_template_text() bounds-checks; no prose is authored in Python or by the model.
    """
    pool = load_outreach_templates().get(pool_key, [])
    fallback_pool = _FALLBACK_OUTREACH_TEMPLATES.get(pool_key) or [""]
    fallback_text = fallback_pool[template_id] if isinstance(template_id, int) and 0 <= template_id < len(fallback_pool) else fallback_pool[0]
    template = resolve_template_text(pool, template_id, fallback_text)
    return sanitize_text(interpolate_template(
        template, name=name, company=clean_company_for_copy(company), job_title=job_title
    ))

def generate_cold_email(job_title, company_name, template_id=0, contact_name=""):
    """Cold email body from the cold_ops bank. `template_id` is the Gemini-routed
    outreach_template_id persisted on the cached job, so /draft re-renders the same entry the
    card showed instead of always falling back to cold_ops[0]."""
    return render_outreach_email("cold_ops", template_id, name=contact_name, company=company_name, job_title=job_title)

def resolve_outreach_body(job, mapping, job_title, company_name, is_warm):
    """THE body every Telegram command (/draft, /eh, /e) shows AND drafts into Gmail.

    Reads the greeting name and the Gemini-routed template id off the cached job, which is what
    the three handlers used to drop on the floor: calling generate_cold_email(title, comp)
    positionally left contact_name="" and template_id=0, so a card routed to cold_ops[3] with a
    resolved contact still rendered a nameless cold_ops[0]. Callers pass the result to
    create_gmail_draft(custom_body=...) so the draft is the same string, rendered once.
    """
    # Gemini's outreach_template_id routes cold_ops only (response_schema caps it at le=5), and
    # warm copy is a hand-finished scaffold anyway, so warm stays on index 0 by design.
    if is_warm:
        return generate_warm_email(
            first_name_for_greeting((mapping or {}).get("contact_name", "")),
            company_name=company_name,
        )
    greeting_name = str((job or {}).get("outreach_contact_first_name") or "")
    if not greeting_name:
        greeting_name = first_name_for_greeting((mapping or {}).get("contact_name", ""))
    return generate_cold_email(
        job_title, company_name,
        template_id=(job or {}).get("outreach_template_id") or 0,
        contact_name=greeting_name,
    )

def generate_warm_email(contact_name="", company_name="", template_id=0):
    """Render a warm_alumni SCAFFOLD, not sendable copy.

    Warm outreach is written by hand now. Its value is in the specific detail a template cannot
    produce ("great catching up with Don at my birthday dinner last Saturday"), and a generic
    warm email actively damages a real relationship. Every warm_alumni entry is therefore an
    obviously-unfinished skeleton with bracketed blanks; this interpolates one so the /warm path
    still works as a drafting aid, but nothing generic can be fired off by accident.
    """
    return render_outreach_email("warm_alumni", template_id, name=contact_name, company=company_name)

def generate_bump_email(contact_name="", job_title="", company_name="", template_id=0):
    """Follow-up nudge from the followup_bumps bank, for threads that went unanswered.

    A blank job_title (PEOPLE-schema Carmen Cold rows have no Role column) routes to
    _ROLELESS_FOLLOWUP_BUMPS so the copy never renders "the this role role at your team".
    """
    if not str(job_title or "").strip():
        pool = _ROLELESS_FOLLOWUP_BUMPS
        template = pool[template_id] if isinstance(template_id, int) and 0 <= template_id < len(pool) else pool[0]
        return sanitize_text(interpolate_template(template, name=contact_name, company=clean_company_for_copy(company_name)))
    return render_outreach_email("followup_bumps", template_id, name=contact_name, company=company_name, job_title=job_title)

def format_email_block(email_text):
    sanitized = sanitize_text(email_text)
    return f"<code>{html.escape(sanitized)}</code>"

# ==============================================================================
# 4. HELPER FUNCTIONS & PIPELINE UTILITIES
# ==============================================================================
def build_system_prompt():
    """Builds the Gemini job-screener system prompt fresh on every call so Evidence Bank edits
    apply instantly (hot-reload, see build_evidence_context_block()/load_evidence_bank()).
    Strict Deterministic Template Engine (SDTE): Gemini acts ONLY as a classifier/router - it
    returns a score/reason plus integer routing keys (track, bullet_indices, linkedin_template_id,
    outreach_template_id). It never authors resume bullets, email bodies, or LinkedIn notes
    itself; all candidate-facing text is interpolated deterministically in Python from local
    JSON template banks (see load_outreach_templates()/load_linkedin_templates()/resume_engine.filter_ats_bullets()).
    """
    evidence_block = build_evidence_context_block(mode="eval")
    return f"""You are a strict technical job screener and template router evaluating roles for an early-career candidate (0-2 years experience). Target Profile: Non-sales W-2 roles in Tech, FinTech, Auto Tech, or Back-Office Systems/Operations in Metro Detroit or Remote.
High Priority Skills: Python, SQL, Salesforce, Excel, Schwab SAC, Fidelity Wealthscape, DocuSign, Process Automation.
Strictly FORBIDDEN: Sales, cold calling, client pitching, commission-based roles, retail bank tellers, CPA tracks, Senior/Lead/Manager roles.
ALSO FORBIDDEN - software engineering job families. The candidate writes Python and SQL to automate his own operations work; he is NOT a professional software engineer and cannot compete for engineering reqs. Score 1-24 any role whose TITLE is Developer, Engineer, Architect, Programmer, SWE, SDET or DevOps (for example "Salesforce Developer", "Data Engineer", "Software Engineer"), however many of his tools the description names. A posting naming Salesforce, SQL or Python is not a match on that basis alone - what matters is whether the ROLE is operations work.

EVIDENCE BANK (the only source of truth for this candidate's real background):
{evidence_block}

NEGATIVE CONSTRAINTS: You must strictly use facts from the Evidence Bank above. Never invent skills, employers, or experiences not listed there. You are STRICTLY a classifier/router - NEVER generate prose, sentences, resume bullets, email bodies, or LinkedIn notes yourself. Only return integer indices selecting from pre-approved local template banks; all actual text is interpolated deterministically in Python from those banks.

Determine the target firm's conservatism level. If the company is a traditional bank, broker-dealer, legacy RIA, or insurance carrier, set "tone_mode" to "conservative". If the company is a fintech, crypto platform, tokenization startup, or software vendor, set "tone_mode" to "tech". When "tone_mode" is "conservative", DO NOT select bullet indices referencing crypto, Bitcoin, or Web3.

SCORING BANDS - use the whole range. Most real postings land in 50-79; reserve 90+ for a genuine match, not merely a plausible one:
90-100: Does what the candidate already does. Names his actual tools (Salesforce, Schwab/Fidelity, DocuSign, Python, SQL) AND is clearly entry-level (0-2 yrs).
80-89: Strong match on the work itself, but one real gap - a tool he has not used, or 3+ years requested.
70-79: Adjacent. Transferable skills, different function or domain. He could do it; it is not what he does.
50-69: Plausible stretch. Meaningful gaps in function, seniority or industry.
25-49: Weak. Wrong function, or seniority he cannot credibly claim.
1-24: Disqualifying - sales/commission, senior/lead/manager, CPA track, or a domain with no overlap.

Evaluate the job description and respond ONLY with a JSON object containing:
{{
"score": <integer 1-100, anchored to the bands below - an unanchored score clusters in the 70s and 80s and makes every candidate look alike, which is useless for ranking>,
"reason": "<1-sentence concise explanation of why this role fits or does not fit>",
"track": "<one letter a|b|c|d|e selecting the resume bullet pool that best matches this role: a=wealth operations, b=data/systems engineering, c=risk & regulatory compliance, d=business intelligence & analytics, e=business operations & CRM systems>",
"tone_mode": "<'conservative' or 'tech' - conservative for traditional banks/broker-dealers/legacy RIAs/insurance carriers, tech for fintech/crypto/tokenization startups/software vendors>",
"bullet_indices": [<int>, <int>, <int>],
"linkedin_template_id": <integer 0-9 selecting a LinkedIn connection note template>,
"outreach_template_id": <integer 0-5 selecting a cold outreach email template>
}}"""

def send_health_alert(error_msg):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        text = f"⚠️ <b>Pipeline Operational Warning</b>\n<code>{html.escape(str(error_msg))}</code>"
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=5
            )
        except Exception:
            pass

def send_status_update(chat_id, text):
    if TELEGRAM_BOT_TOKEN and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": f"<b>Pipeline Telemetry:</b>\n{text}", "parse_mode": "HTML"},
                timeout=5
            )
        except Exception:
            pass

def calculate_keyword_overlap(job_desc):
    desc = str(job_desc or "").lower()
    core_skills = get_filter("core_skills", [])
    matches = [skill for skill in core_skills if skill in desc]
    overlap_pct = int((len(matches) / len(core_skills)) * 100) if core_skills else 0
    return overlap_pct, matches

SOFT_CAP_KNEE = 90


def soft_cap_score(raw_score):
    """Clamp to 1-100, but compress above SOFT_CAP_KNEE instead of flattening.

    A hard min(100, ...) destroys ordering exactly where it matters most: everything from 100 to
    135 collapsed onto the same value, so the Tier-1 top-5 cut was slicing a pile of ties and the
    "best" five were whichever the sort happened to touch first. Above the knee each additional
    raw point is worth a tenth of a point, so a 130 still outranks a 105 while both stay inside
    the 1-100 scale the cards, sheet and filters already assume.
    """
    score = int(raw_score)
    if score <= SOFT_CAP_KNEE:
        return max(1, score)
    return min(100, SOFT_CAP_KNEE + int(round((score - SOFT_CAP_KNEE) / 10.0)))


def calculate_hybrid_score_modifier(job, base_ai_score):
    """Layer 1 of the additive scoring: keyword/salary modifiers on top of Gemini's holistic base.
    Returns (final_score, layer1_bonus) where final_score is the clamped 1-100 result (unchanged
    from before this function grew a second return value) and layer1_bonus is the signed shift
    this layer applied to base_ai_score, with the remote 90-cap already folded in but the final
    1-100 clamp not - so a caller can reconstruct final_score as clamp(base_ai_score + layer1_bonus).
    process_single_candidate needs it to enforce BONUS_STACK_CAP across Layer 1 + Layer 2 together."""
    bonus = 0
    desc = str(job.get("job_description") or "").lower()
    title = str(job.get("job_title") or "").lower()
    company = str(job.get("employer_name") or "").lower()
    city = str(job.get("job_city") or "").lower()
    salary_str, max_sal = extract_salary(job)
    tier1_ecosystem = get_filter("tier1_ecosystem", [])
    if any(k in desc or k in company for k in tier1_ecosystem):
        bonus += 10
    # Graduated rather than a single cliff at 60k: a binary +5 treated a 62k listing and a 130k
    # listing as identical, throwing away the clearest quality signal a posting carries.
    if max_sal >= 110000:
        bonus += 10
    elif max_sal >= 90000:
        bonus += 8
    elif max_sal >= 75000:
        bonus += 6
    elif max_sal >= 60000:
        bonus += 4
    # Scaled by DISTINCT matches rather than firing a flat bonus on any single hit. Nearly every
    # ops posting mentions Salesforce or SQL somewhere, so a flat +10/+15 was close to a constant:
    # it lifted good and mediocre jobs equally, then the 100-clamp erased what was left of the
    # spread. Counting distinct terms makes a role wanting Python AND SQL AND Salesforce actually
    # outrank one that says "Salesforce" once.
    domain_hits = sum(1 for k in ["fintech", "payments", "autotech", "saas", "tokenization", "digital assets", "web3", "trading bot"] if k in desc)
    bonus += min(12, domain_hits * 6)
    tool_hits = sum(1 for k in ["schwab", "fidelity", "docusign", "orion", "salesforce", "python", "sql", "etl"] if k in desc)
    bonus += min(12, tool_hits * 4)
    # Seniority, read off the title. The system prompt forbids senior/lead/manager roles, but
    # nothing downstream enforced it, so a "Senior Operations Manager" could still outscore a
    # genuine entry-level opening on keywords alone. Entry-level markers earn a bonus for the
    # same reason: they are the roles actually winnable at 0-2 years.
    if re.search(r'\b(senior|sr\.?|lead|principal|staff|head of|director|vp|manager|mgr)\b', title):
        bonus -= 18
    # Wrong job family, read off the title. Seniority words were the only title filter, so a
    # "Salesforce Developer" or "Data Engineer" - a different profession, not a senior version of
    # this one - took no penalty at all, while its description maxed out the tool-keyword bonus
    # (+12) by naming Salesforce/SQL/Python repeatedly and the salary bonus (+10) on a six-figure
    # engineering ceiling. That is how a $178k Salesforce Developer req scored 83 and cleared the
    # 80-point Tier-1 card gate. -30 so a maxed keyword+salary stack cannot buy it back.
    elif re.search(r'\b(developer|engineer|engineering|architect|programmer|swe|sdet|devops)\b', title):
        bonus -= 30
    elif re.search(r'\b(junior|jr\.?|associate|entry[ -]?level|analyst i|i{1,2}\b|coordinator|specialist)\b', title):
        bonus += 6

    # Years-of-experience demand: a 0-2yr candidate is a real match at 0-3 and a stretch past 5.
    exp_match = re.search(r'(\d+)\+?\s*(?:-\s*\d+\s*)?year', desc)
    if exp_match:
        years = safe_int(exp_match.group(1), 0)
        if years >= 7:
            bonus -= 15
        elif years >= 5:
            bonus -= 10
        elif years >= 3:
            bonus -= 4
        else:
            bonus += 6

    # Posting freshness. get_age_badge() already computes this for the card; feeding it into the
    # score too means a role posted yesterday outranks an identical one going stale, which is the
    # closest cheap proxy for "winnable" the pipeline has.
    posted_hours = parse_posted_hours(job)
    if posted_hours is not None:
        if posted_hours <= 72:
            bonus += 8
        elif posted_hours <= 168:
            bonus += 4
        elif posted_hours >= 720:
            bonus -= 8

    if any(k in desc for k in ["high call volume", "outbound calling", "phone queue", "call center", "inbound calls", "dialer"]):
        bonus -= 20
    if any(k in title for k in ["data entry", "admin coordinator", "administrative assistant"]) and max_sal < 60000:
        bonus -= 15
    if "wealth" in desc and not any(k in desc for k in ["python", "sql", "automation", "systems"]):
        bonus -= 15

    non_mi_hubs = ["chicago", "new york", "austin", "boston", "dallas", "atlanta", "denver", "seattle", "san francisco", "charlotte", "nyc"]
    valid_cities = get_filter("valid_cities", [])
    # passes_strict_filter already requires an in-metro city, so this only catches an out-of-state
    # hub name that slipped past that check (e.g. mentioned in the description, not job_city).
    if any(hub in city or hub in desc[:300] for hub in non_mi_hubs) and not any(c in city for c in valid_cities):
        bonus -= 15

    score = base_ai_score + bonus
    layer1_bonus = score - base_ai_score
    return soft_cap_score(score), layer1_bonus

def resolve_live_alumni_at_company(company_name, school="Hope College"):
    """JIT alumni resolution: live-queries DuckDuckGo HTML search for a LinkedIn profile ath
    company_name sharing `school` as alma mater, instead of relying on a static spreadsheet.
    Returns {"name", "company", "linkedin_url", "headline"} for the top matching profile, or None
    on no-match/timeout/failure.
    """
    if not company_name:
        return None
    clean_company = re.sub(r'\b(inc|llc|corp|corporation|co|ltd|plc)\b\.?', '', str(company_name), flags=re.IGNORECASE)
    clean_company = re.sub(r'[.,]', '', clean_company).strip()
    if not clean_company:
        return None

    query = f'site:linkedin.com/in "{clean_company}" "{school}"'
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        res = requests.get("https://html.duckduckgo.com/html/", params={"q": query}, headers=headers, timeout=3)
        if res.status_code != 200:
            return None
        body = res.text

        link_match = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, re.IGNORECASE | re.DOTALL)
        if not link_match:
            return None
        raw_href, raw_title = link_match.groups()

        # DuckDuckGo HTML wraps result links in a redirect: //duckduckgo.com/l/?uddg=<url-encoded-target>
        linkedin_url = raw_href
        if "uddg=" in raw_href:
            parsed_qs = urllib.parse.parse_qs(urllib.parse.urlparse(raw_href).query)
            linkedin_url = parsed_qs.get("uddg", [raw_href])[0]
        if not linkedin_url.startswith("http"):
            linkedin_url = f"https:{linkedin_url}"
        if "linkedin.com/in/" not in linkedin_url:
            return None

        title_text = re.sub(r'<[^>]+>', '', raw_title).strip()
        parsed_name = re.split(r' - | \| ', title_text)[0].strip() or "Alumnus Contact"

        snippet_match = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', body, re.IGNORECASE | re.DOTALL)
        snippet_text = re.sub(r'<[^>]+>', '', snippet_match.group(1)).strip() if snippet_match else ""

        return {
            "name": parsed_name,
            "company": company_name,
            "linkedin_url": linkedin_url,
            "headline": snippet_text[:200]
        }
    except requests.exceptions.Timeout:
        return None  # fast-fail: never retry a slow DuckDuckGo scrape
    except Exception as e:
        logging.warning(f"resolve_live_alumni_at_company failed for '{company_name}': {e}")
        return None

def discover_ecosystem_network(target_entity: str) -> dict:
    """Queries Gemini to discover ecosystem keywords and probable ATS board slugs for a target entity.
    Returns a dict with canonical_name, ecosystem_keywords, and probable_ats_slugs.
    """
    if not target_entity or not GEMINI_API_KEY:
        return {
            "canonical_name": str(target_entity or "Unknown"),
            "ecosystem_keywords": [],
            "probable_ats_slugs": []
        }

    prompt = f"""Given the company or organization: "{html.escape(str(target_entity))}"

Provide a JSON response ONLY with these exact keys:
{{
  "canonical_name": "official company name",
  "ecosystem_keywords": ["keyword1", "parent_company", "subsidiary1", "brand_name", ...],
  "probable_ats_slugs": ["slug1", "slug2", "slug3", ...]
}}

The ecosystem_keywords should include the company itself, parent companies, subsidiaries, portfolio brands, and related terms that could match job postings for this organization.
The probable_ats_slugs should be plausible URL slugs for their job boards (e.g., "acme-corp", "acmecorp", "acme-careers", "jobs-acme").

Respond with ONLY the JSON, no markdown formatting or extra text."""

    raw_response = call_gemini_api(prompt)
    if not raw_response:
        logging.warning(f"Ecosystem discovery failed for {target_entity}: Gemini API unavailable")
        return {
            "canonical_name": str(target_entity),
            "ecosystem_keywords": [str(target_entity)],
            "probable_ats_slugs": []
        }

    try:
        cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', "", raw_response).strip()
        data = json.loads(cleaned)
        return {
            "canonical_name": str(data.get("canonical_name", target_entity)),
            "ecosystem_keywords": list(data.get("ecosystem_keywords", [str(target_entity)])),
            "probable_ats_slugs": list(data.get("probable_ats_slugs", []))
        }
    except Exception as e:
        logging.error(f"Ecosystem discovery JSON parse error ({target_entity}): {e}")
        return {
            "canonical_name": str(target_entity),
            "ecosystem_keywords": [str(target_entity)],
            "probable_ats_slugs": []
        }

def probe_ats_slug(slug: str) -> bool:
    """Attempts a quick HEAD/GET to three major ATS board APIs to verify a slug is live.
    Returns True if any endpoint returns HTTP 200 with content.
    """
    if not slug or not str(slug).strip():
        return False
    slug = str(slug).strip().lower()

    endpoints = [
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        f"https://api.lever.co/v0/postings/{slug}?mode=json",
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    ]

    for endpoint in endpoints:
        try:
            res = requests.get(endpoint, timeout=3)
            if res.status_code == 200 and len(res.content) > 0:
                logging.info(f"ATS slug verified: {slug} (endpoint: {endpoint})")
                return True
        except requests.exceptions.Timeout:
            pass
        except requests.exceptions.RequestException:
            pass

    return False

def verify_live_slugs(candidate_slugs: list) -> list:
    """Probes multiple ATS slugs in parallel (max_workers=8) and returns only the live ones."""
    if not candidate_slugs:
        return []

    live_slugs = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(probe_ats_slug, slug): slug for slug in candidate_slugs}
        for future in futures:
            try:
                is_live = future.result()
                if is_live:
                    live_slugs.append(futures[future])
            except Exception as e:
                logging.error(f"ATS slug probe error: {e}")

    return live_slugs

def expand_ecosystem_filter(target_entity: str) -> str:
    """Orchestrates ecosystem discovery, ATS verification, and filter merge atomically.
    Returns an HTML-formatted Telegram message reporting the results.
    """
    if not target_entity or not str(target_entity).strip():
        return "⚠️ <b>Ecosystem Expansion Error:</b> No entity name provided."

    target_entity = str(target_entity).strip()
    logging.info(f"Ecosystem expansion triggered for: {target_entity}")

    # Step 1: Discover ecosystem network
    discovery = discover_ecosystem_network(target_entity)
    canonical = discovery.get("canonical_name", target_entity)
    new_keywords = discovery.get("ecosystem_keywords", [target_entity])
    candidate_slugs = discovery.get("probable_ats_slugs", [])

    logging.info(f"Discovered {len(new_keywords)} keywords and {len(candidate_slugs)} candidate ATS slugs for {canonical}")

    # Step 2: Verify live ATS slugs
    live_slugs = verify_live_slugs(candidate_slugs)
    logging.info(f"Verified {len(live_slugs)} live ATS slugs for {canonical}")

    # Step 3: Atomically merge into filters
    added_keywords = []
    added_slugs = []

    try:
        # Merge ecosystem keywords into tier1_ecosystem
        current_keywords = get_filter("tier1_ecosystem") or []
        for kw in new_keywords:
            kw_lower = str(kw).lower().strip()
            if kw_lower and not any(str(existing).lower() == kw_lower for existing in current_keywords):
                current_keywords.append(kw)
                added_keywords.append(kw)

        if added_keywords:
            set_filter("tier1_ecosystem", current_keywords)
            logging.info(f"Added {len(added_keywords)} keywords to tier1_ecosystem: {added_keywords}")

        # Merge live slugs into ats_company_slugs
        current_slugs = get_filter("ats_company_slugs") or []
        for slug in live_slugs:
            slug_lower = str(slug).lower().strip()
            if slug_lower and not any(str(existing).lower() == slug_lower for existing in current_slugs):
                current_slugs.append(slug)
                added_slugs.append(slug)

        if added_slugs:
            set_filter("ats_company_slugs", current_slugs)
            logging.info(f"Added {len(added_slugs)} live ATS slugs: {added_slugs}")

    except Exception as e:
        logging.error(f"Ecosystem filter merge error: {e}")
        return f"❌ <b>Ecosystem Expansion Error:</b> Failed to update filters. Check logs."

    # Step 4: Format and return result
    keywords_display = ", ".join(f"<code>{html.escape(str(k)[:30])}</code>" for k in added_keywords[:5]) if added_keywords else "None"
    slugs_display = ", ".join(f"<code>{html.escape(str(s))}</code>" for s in added_slugs[:5]) if added_slugs else "None"

    result_msg = (
        f"✅ <b>Ecosystem Expanded: {html.escape(canonical)}</b>\n\n"
        f"📌 <b>New Keywords Added:</b> {keywords_display}"
        f"{f' (+{len(added_keywords)-5} more)' if len(added_keywords) > 5 else ''}\n"
        f"🎯 <b>Live ATS Boards Found:</b> {slugs_display}"
        f"{f' (+{len(added_slugs)-5} more)' if len(added_slugs) > 5 else ''}\n\n"
        f"<i>Tier-1 ecosystem now has {len(current_keywords)} keywords | "
        f"{len(current_slugs)} ATS board slugs active.</i>"
    )
    return result_msg

def extract_domain_from_website(url):
    """Parse a root domain (no scheme/www/path) out of a company website URL, or None if unusable."""
    if not url:
        return None
    url = str(url).strip()
    if not url:
        return None
    if "://" not in url:
        url = f"http://{url}"
    try:
        netloc = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return None
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc or None

def resolve_target_email(company_name, job_title="", employer_website=None):
    """Resolve target email. Prefers the real domain parsed from employer_website;
    falls back to a cleaned company-name guess flagged with [⚠️ Fallback Email].
    """
    domain = extract_domain_from_website(employer_website)
    is_fallback = domain is None
    if is_fallback:
        domain = re.sub(r'[^a-zA-Z0-9]', "", str(company_name or "")).lower() + ".com"
    title_lower = str(job_title or "").lower()
    fallback_warning = " [⚠️ Fallback Email]" if is_fallback else ""
    if "compliance" in title_lower:
        return f"compliance@{domain}{fallback_warning}"
    elif any(kw in title_lower for kw in ["wealth", "custody", "brokerage", "ria"]):
        return f"wealthops@{domain}{fallback_warning}"
    elif any(kw in title_lower for kw in ["systems", "automation", "revops"]):
        return f"bizops@{domain}{fallback_warning}"
    return f"operations@{domain}{fallback_warning}"

def parse_quick_command(text_input):
    """
    Format: /quick Name @ Company [1-10] Note  (also reused by the /cold and /warm quick-add variants)
    Handles company names with numbers and special symbols safely (e.g. 3M, Web3 Labs, 1Password, 7-Eleven).
    """
    clean = re.sub(r'^/\S+\s*', '', text_input.strip())
    if "@" not in clean:
        return None
    name_part, rest = clean.split("@", 1)
    name = name_part.strip()
    rest = rest.strip()
    if not name or not rest:
        return None

    # Extract priority if present as standalone integer
    tokens = rest.split()
    priority = 5
    company_tokens = []
    note_tokens = []
    found_priority = False

    for idx, token in enumerate(tokens):
        if token.isdigit() and 1 <= int(token) <= 10 and not found_priority and idx > 0:
            priority = int(token)
            found_priority = True
            note_tokens = tokens[idx + 1:]
            break
        else:
            company_tokens.append(token)

    company = " ".join(company_tokens).strip()
    note = " ".join(note_tokens).strip() if found_priority else ""
    return name, company, priority, note

# ==============================================================================
# 5. GEMINI REST API INTEGRATION (TRUNCATED PAYLOAD)
# ==============================================================================
def call_gemini_api(prompt, system_prompt=None, response_mime="application/json", max_retries=3):
    """Call Gemini API with resilience handling: exponential backoff retry on 429/5xx. Return None on final failure."""
    if not GEMINI_API_KEY:
        return None
    full_prompt = f"{system_prompt}\n\n{prompt}".strip() if system_prompt else prompt
    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {"response_mime_type": response_mime}
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={GEMINI_API_KEY}"
    delay = 1.0
    for attempt in range(max_retries):
        try:
            res = requests.post(url, json=payload, timeout=12)
            if res.status_code == 200:
                return res.json()["candidates"][0]["content"]["parts"][0]["text"]
            if res.status_code == 429 or res.status_code >= 500:
                logging.warning(f"Gemini API {res.status_code} (attempt {attempt+1}/{max_retries}) - backing off {delay}s")
                if attempt == max_retries - 1:
                    send_health_alert(f"Gemini API {res.status_code} - halting evaluation after {max_retries} retries")
                    return None
                time.sleep(delay)
                delay *= 2.0
                continue
            return None
        except requests.exceptions.Timeout:
            logging.error(f"Gemini API Timeout (attempt {attempt+1}/{max_retries})")
            if attempt == max_retries - 1:
                return None
            time.sleep(delay)
            delay *= 2.0
        except Exception as e:
            logging.error(f"Gemini API Exception: {e}")
            return None
    return None

def evaluate_job_with_gemini(job):
    """Evaluate job with Gemini acting strictly as a classifier/router (Strict Deterministic
    Template Engine). Gemini returns ONLY a score/reason plus integer routing keys - never
    prose. On failure/timeout, set score=0 and status 'Evaluation Pending'. Thread-safe with
    timeout handling: DO NOT assign fake scores on failure.
    Returns (pass_bool, score, reason, track, tone_mode, bullet_indices, linkedin_template_id,
    outreach_template_id, layer1_bonus, gemini_base). The last two thread the raw Layer 1 modifier
    sum and Gemini's un-modified base score through to process_single_candidate so it can cap the
    Layer 1 + Layer 2 bonus stack (BONUS_STACK_CAP) against the true base.
    """
    if not GEMINI_API_KEY:
        # Fail closed like every other failure path below: a missing key means nothing was screened,
        # and passing at a fabricated 75 would clear the >= 65 gate and ship every job unscreened.
        # Debounced because this runs inside a 20-worker pool - send_health_alert has no cooldown
        # of its own, so without should_send_alert one keyless run would fire 20 Telegram messages.
        if should_send_alert("gemini_key_missing"):
            send_health_alert("GEMINI_API_KEY is unset - every candidate is failing closed as 'Evaluation Pending'. Expect a zero-result run until the key is restored.")
        return False, 0, "Evaluation Pending", "a", "conservative", [0, 1, 2], 0, 0, 0, 0

    try:
        desc_truncated = str(job.get("job_description") or "")[:1800]
        prompt = f"Job Title: {job.get('job_title')}\nCompany: {job.get('employer_name')}\nDescription:\n{desc_truncated}"
        
        # Call API with timeout handling
        raw_text = call_gemini_api(prompt, build_system_prompt())
        
        if raw_text:
            try:
                cleaned_text = re.sub(r'^```(?:json)?\s*|\s*```$', "", raw_text).strip()
                validated = GeminiJobScreenerResponse.model_validate_json(cleaned_text)

                final_score, layer1_bonus = calculate_hybrid_score_modifier(job, validated.score)
                return (
                    (final_score >= 65), final_score, validated.reason, validated.track, validated.tone_mode,
                    validated.bullet_indices, validated.linkedin_template_id, validated.outreach_template_id,
                    layer1_bonus, validated.score
                )
            except Exception as e:
                logging.error(f"Gemini evaluation JSON parse/validation failure: {e}")
                # On parse/validation error, return 0 score with Evaluation Pending status
                return False, 0, "Evaluation Pending", "a", "conservative", [0, 1, 2], 0, 0, 0, 0

        # On API failure/timeout, set score to 0 and status to "Evaluation Pending" (NO fake scores)
        return False, 0, "Evaluation Pending", "a", "conservative", [0, 1, 2], 0, 0, 0, 0
    
    except Exception as e:
        logging.error(f"Gemini evaluation exception: {e}")
        return False, 0, "Evaluation Pending", "a", "conservative", [0, 1, 2], 0, 0, 0, 0

def generate_interview_prep(company, job_title, job_description=""):
    """3 talking points + 2 reverse questions tailored to a role; safe static fallback if Gemini is unavailable."""
    fallback = {
        "talking_points": [
            f"My experience automating reporting workflows with Python and SQL directly maps to the operational efficiency {company or 'this team'} is likely optimizing for.",
            f"In wealth ops, I've reconciled data across custodial platforms - the same rigor applies to {job_title or 'this role'}'s process ownership.",
            "I like building lightweight automation that removes manual steps without adding fragile complexity."
        ],
        "reverse_questions": [
            "What does a successful first 90 days look like for this role from an operations standpoint?",
            "Where are the biggest manual bottlenecks the team is hoping this hire will help automate?"
        ]
    }
    if not GEMINI_API_KEY:
        return fallback
    desc_truncated = str(job_description or "")[:800]
    prompt = (
        f"Job Title: {job_title or 'N/A'}\nCompany: {company or 'N/A'}\nDescription:\n{desc_truncated}\n\n"
        "Generate interview prep for an early-career candidate whose background is Python, SQL, Salesforce, "
        "process automation, and wealth operations. Respond ONLY with JSON: "
        '{"talking_points": ["<3 items bridging Python/SQL/process automation/wealth ops experience to this role>"], '
        '"reverse_questions": ["<2 high-leverage operational questions to ask the interviewer>"]}'
    )
    raw_text = call_gemini_api(prompt)
    if raw_text:
        try:
            cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', "", raw_text).strip()
            data = json.loads(cleaned)
            talking_points = data.get("talking_points", [])
            reverse_questions = data.get("reverse_questions", [])
            if isinstance(talking_points, list) and isinstance(reverse_questions, list) and talking_points and reverse_questions:
                return {
                    "talking_points": [str(t) for t in talking_points][:3],
                    "reverse_questions": [str(q) for q in reverse_questions][:2]
                }
        except Exception as e:
            logging.error(f"Interview prep parse failure: {e}")
    return fallback

def generate_elevator_pitch(company, job_title):
    """Tight 3-sentence elevator pitch tailored to a company/role; safe static fallback if Gemini is unavailable."""
    core_exp, _ = get_current_role_blurb()
    fallback = (
        f"Hi, I'm Kevin - I work in {core_exp}, building Python and SQL tools that cut manual reconciliation time. "
        f"I've been following {company or 'your team'} and think my background lines up well with {job_title or 'the operations work'} you're doing. "
        "Would love to grab 15 minutes to see where I could help."
    )
    if not GEMINI_API_KEY:
        return sanitize_text(fallback)
    prompt = (
        f"Company: {company or 'N/A'}\nRole: {job_title or 'N/A'}\n\n"
        f"EVIDENCE BANK (only source of truth for this candidate - never invent facts outside it):\n{build_evidence_context_block(mode='pitch')}\n\n"
        "Write a tight 3-sentence conversational 30-second elevator pitch for this candidate, tailored to this company "
        "and role, using ONLY the Evidence Bank above. Avoid all banned words. Sound like a direct human communicator. "
        'Respond ONLY with JSON: {"pitch": "<3-sentence pitch>"}'
    )
    raw_text = call_gemini_api(prompt)
    if raw_text:
        try:
            cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', "", raw_text).strip()
            data = json.loads(cleaned)
            pitch = data.get("pitch", "")
            if pitch:
                return sanitize_text(str(pitch))
        except Exception as e:
            logging.error(f"Elevator pitch parse failure: {e}")
    return fallback

def generate_cover_letter(company, job_title, track="a", letter_index=0, job_location="", tone_mode="conservative"):
    """Assembles a 3-paragraph plain-text cover letter deterministically from the track-keyed
    bank in templates/cover_letter_templates.json. Never calls Gemini.

    This used to be a free-text Gemini call, which made it the one candidate-facing surface in the
    pipeline that could invent experience - the exact failure the Strict Deterministic Template
    Engine exists to prevent everywhere else (see filter_ats_bullets / interpolate_template). It
    also skipped sanitize_text(), so it was the only path where a banned word could reach a
    recruiter, and it ignored the routed track entirely - meaning the letter could argue wealth-ops
    while the attached PDF argued bizops.

    Now it takes the SAME (track, index) routing Gemini already returned for the resume, so the
    letter and the PDF make one case. Body paragraphs are keyed by TRACK_BULLET_POOL_KEYS, the same
    map resume_engine.py resolves bullets through, so the two banks cannot drift apart on naming.

    `tone_mode` picks paragraph 2 the same way it constrains bullet selection in filter_ats_bullets:
    a "tech" company hears the automation framed as engineering, a conservative one hears it framed
    as process discipline. The underlying facts are identical - only the register moves.

    `job_location` is appended to the opener only when known; a letter that guesses a city is worse
    than one that omits it.
    """
    bank = load_cover_letter_templates()
    track_key = str(track or "a").lower()
    tone_key = "tech" if str(tone_mode or "").lower() == "tech" else "conservative"
    pool_key = TRACK_BULLET_POOL_KEYS.get(track_key, TRACK_BULLET_POOL_KEYS["a"])

    clean_company = clean_company_for_copy(company)
    role = job_title or "this role"

    idx = letter_index if isinstance(letter_index, int) and letter_index >= 0 else 0

    openers = bank.get("openers") or _FALLBACK_COVER_LETTER_TEMPLATES["openers"]
    bodies = bank.get(pool_key) or bank.get(TRACK_BULLET_POOL_KEYS["a"]) or _FALLBACK_COVER_LETTER_TEMPLATES["track_a_wealth_ops"]
    bridges = bank.get(f"bridges_{tone_key}") or bank.get("bridges_conservative") or []
    closers = bank.get("closers") or _FALLBACK_COVER_LETTER_TEMPLATES["closers"]

    # Index 0 of the shared bridge/closer pools is deliberately billing-flavored ("before an
    # invoice goes out", "order-to-cash"): it is the strongest copy for a billing role and the
    # wrong copy for anything else. Skip past it unless the title actually says billing, so a
    # Client Onboarding Specialist is never told Kevin wants "a dedicated billing seat".
    if not re.search(r"\b(billing|invoic|revenue|order.to.cash|accounts receivable|\bAR\b)", role, re.I):
        if len(bridges) > 1:
            bridges = bridges[1:]
        if len(closers) > 1:
            closers = closers[1:]

    # Each pool is sized independently, so wrap per-pool rather than bounds-failing to index 0 -
    # a routed index of 2 should still vary the opener even if the openers pool is shorter.
    opener = str(openers[idx % len(openers)])
    body = str(bodies[idx % len(bodies)])
    bridge = str(bridges[idx % len(bridges)]) if bridges else ""
    closer = str(closers[idx % len(closers)])

    # The location rides in the opener only for a role whose posting is location-defining (a
    # Detroit desk job). It is deliberately omitted otherwise: a remote or multi-site posting that
    # gets "in Detroit, MI" appended reads as a candidate who misread the listing.
    loc = str(job_location or "").strip()
    if loc:
        opener = opener.rstrip(".") + f" in {loc}."

    def fill(text):
        return str(text).replace("{company}", clean_company).replace("{job_title}", role)

    paragraphs = [f"{fill(opener)} {fill(body)}"]
    if bridge:
        paragraphs.append(fill(bridge))
    paragraphs.append(fill(closer))

    signoffs = bank.get("signoffs") or ["Thank you for your time and consideration."]
    paragraphs.append(fill(signoffs[idx % len(signoffs)]))

    letter = (
        f"Dear {clean_company} Hiring Team,\n\n"
        + "\n\n".join(paragraphs)
        + "\n\nBest regards,\nKevin Miller"
    )
    return sanitize_text(letter)

# ==============================================================================
# 6. STAGE 1 STRICT FILTER & SINGLE CANDIDATE EVALUATION
# ==============================================================================
def _passes_remote_filter(job):
    """passes_strict_filter minus the geography gates, for genuinely remote postings.

    The metro gates (valid_cities allowlist + the Michigan state check) exist to keep a Detroit
    desk search local, and they reject every remote feed posting on sight since job_city is the
    literal string "Remote". Every OTHER gate still has to apply - a commission sales role or a
    senior title is just as wrong remote as it is in Farmington - so this reuses the same filter
    lists rather than reimplementing them, and any gate added to passes_strict_filter's
    non-geographic half should be added here too.
    """
    title = str(job.get("job_title") or "").lower()
    description = str(job.get("job_description") or "").lower()
    company = str(job.get("employer_name") or "").lower()
    if not title or not company:
        return False
    if not job.get("job_is_remote"):
        return False

    if is_company_on_cooldown(company):
        return False
    applied_companies = get_applied_crm_companies()
    clean_company = normalize_company_for_match(company)
    if company in applied_companies or clean_company in applied_companies:
        return False

    _, max_sal = extract_salary(job)
    if max_sal > 0 and max_sal < safe_int(get_filter("min_salary"), 50000):
        return False

    if any(re.search(rf"\b{re.escape(term)}\b", title) for term in get_filter("title_exclusions", [])):
        return False
    if any(comp in company for comp in get_filter("company_exclusions", [])):
        return False
    if any(trigger in description for trigger in get_filter("hard_ban_keywords", [])):
        return False
    if any(sen in title for sen in get_filter("seniority_exclusions", [])):
        return False

    # Remote feeds skew heavily engineering, so require at least one core skill rather than
    # surfacing every open SRE req. Matched on word boundaries, not substrings: a plain
    # `"excel" in description` also fires on "excellent communication skills", which is boilerplate
    # in almost every posting - that one bug alone passed 22 of 39 otherwise-irrelevant remote
    # jobs when this gate was first written.
    core_skills = [str(s).lower() for s in get_filter("core_skills", []) if str(s).strip()]
    haystack = f"{title} {description}"
    if core_skills and not any(re.search(rf"\b{re.escape(s)}\b", haystack) for s in core_skills):
        return False
    return True


# ==============================================================================
# FUNNEL TELEMETRY
# ==============================================================================
# Every gate below silently returns False, which is correct for the pipeline and terrible for
# diagnosis: a run reporting "112 raw listings, 2 passed strict criteria" gives no way to tell
# whether 110 jobs died on the city allowlist, the salary floor, or content dedup. That ambiguity
# is what made a dedup bug look for weeks like an over-tuned filter.
#
# FunnelTrace is a per-run counter, not a persisted table: the question it answers ("where did
# THIS run's jobs go?") is always about the run in front of you, and pipeline_metrics already
# covers long-horizon counts. Kept deliberately dumb - a dict, a note() call per gate, and one
# formatted summary - so adding a gate is a one-line change and can never raise into the pipeline.

FUNNEL_REJECTION_LABELS = {
    "dedup_title": "Already seen (company+title)",
    "dedup_content": "Already seen (description)",
    "board_id_employer": "Employer looks like a board ID",
    "company_cooldown": "Company on 14-day cooldown",
    "already_applied": "Already applied (Tetiana Warm)",
    "salary_floor": "Below minimum salary",
    "out_of_state": "Outside Michigan",
    "city_allowlist": "City not in metro allowlist",
    "expired": "Posting expired",
    "experience_salary": "Experience demand vs. salary",
    "title_exclusion": "Excluded title",
    "company_exclusion": "Excluded company",
    "hard_ban_keyword": "Hard-ban keyword in description",
    "seniority": "Too senior",
    "no_systems_anchor": "Finance role, no systems anchor",
}


class FunnelTrace:
    """Counts why candidates were dropped during one pipeline run."""

    def __init__(self):
        self.raw = 0
        self.passed = 0
        self.reasons = {}

    def note(self, reason):
        """Record one rejection. Unknown reasons are counted under their raw key rather than
        dropped, so a gate added without a label still shows up in the summary."""
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def summary_line(self):
        """One-line funnel breakdown, most common rejection first, or "" when nothing was dropped."""
        if not self.reasons:
            return ""
        ranked = sorted(self.reasons.items(), key=lambda kv: kv[1], reverse=True)
        parts = [f"{FUNNEL_REJECTION_LABELS.get(key, key)}: {count}" for key, count in ranked]
        return f"{self.raw} raw -> {self.passed} passed | " + " · ".join(parts)


def passes_strict_filter(job, trace=None):
    """True when a job clears every hard gate. `trace`, if given, records WHICH gate rejected it -
    see FunnelTrace above for why that is worth threading through."""
    def reject(reason):
        if trace is not None:
            trace.note(reason)
        return False

    title = str(job.get("job_title") or "").lower()
    description = str(job.get("job_description") or "").lower()
    company = str(job.get("employer_name") or "").lower()
    city = str(job.get("job_city") or "").lower()
    state = str(job.get("job_state") or "").strip().lower()
    salary_str, max_sal = extract_salary(job)

    # Reject employer names that look like a job-board's internal listing ID rather than a real
    # company (e.g. "AWSPRODVK1" from a ZipRecruiter aggregator posting with no employer resolved) -
    # no spaces, no lowercase letters, and at least one digit. A real company name may be all-caps
    # (e.g. "IBM") but won't also contain a digit with zero spaces.
    raw_employer = str(job.get("employer_name") or "").strip()
    if raw_employer and " " not in raw_employer and raw_employer == raw_employer.upper() and any(c.isdigit() for c in raw_employer):
        logging.info(f"[EXCLUDED] employer_name '{raw_employer}' looks like a job-board ID, not a real company.")
        return reject("board_id_employer")

    if is_company_on_cooldown(company):
        return reject("company_cooldown")
    applied_companies = get_applied_crm_companies()
    clean_company = normalize_company_for_match(company)
    if company in applied_companies or clean_company in applied_companies:
        logging.info(f"[EXCLUDED] {company} is already in Tetiana Warm (applied).")
        return reject("already_applied")

    min_sal_floor = safe_int(get_filter("min_salary"), 50000)
    if max_sal > 0 and max_sal < min_sal_floor:
        return reject("salary_floor")

    # Same-named city in another state (e.g. Birmingham AL vs. Birmingham MI) would otherwise
    # pass the city substring check below - reject it first when the API told us the state.
    if state and state not in ("mi", "michigan"):
        return reject("out_of_state")

    valid_cities = get_filter("valid_cities", [])
    # Metro-area allowlist only (~35mi of Farmington MI via radius_miles) - state=="MI" alone is NOT
    # sufficient, since that would also admit Grand Rapids/Lansing/Traverse City etc. outside the radius.
    # Remote is intentionally NOT a pass condition here - local-only by design, and the substring
    # "remote"/"work from home" check was unreliable anyway (no negation handling, e.g. "not remote").
    # valid_cities is a hand-maintained suburb allowlist, not a computed geofence - a real in-radius
    # city missing from the list is silently dropped here, not sourced-but-then-filtered.
    is_in_metro_area = any(c in city for c in valid_cities)
    if not is_in_metro_area:
        # Logged with the city, not just counted: this allowlist is hand-maintained, so a real
        # in-radius suburb missing from it looks identical to a genuine out-of-area reject. The
        # city name is the only way to tell them apart, and the only way to know what to add.
        if trace is not None:
            logging.info(f"[EXCLUDED] city '{city}' not in valid_cities allowlist")
        return reject("city_allowlist")

    # JSearch/OpenWebNinja can flag a posting as expired (job board removed it since being scraped)
    # even though it's still returned for the "month" date_posted window - reject it outright when
    # the API tells us; a missing/blank field is not treated as "expired" (fail open, not closed).
    expiration = job.get("job_offer_expiration_datetime_utc")
    if expiration:
        try:
            expiry_dt = datetime.fromisoformat(str(expiration).replace("Z", "+00:00"))
            if expiry_dt < datetime.now(timezone.utc):
                return reject("expired")
        except (ValueError, TypeError):
            pass

    exp_floor = safe_int(get_filter("experience_salary_floor"), 60000)
    if any(k in description for k in ["3+ years", "3-5 years", "4+ years"]) and (0 < max_sal < exp_floor):
        return reject("experience_salary")

    if any(re.search(rf"\b{re.escape(term)}\b", title) for term in get_filter("title_exclusions", [])):
        return reject("title_exclusion")
    if any(comp in company for comp in get_filter("company_exclusions", [])):
        return reject("company_exclusion")
    if any(trigger in description for trigger in get_filter("hard_ban_keywords", [])):
        return reject("hard_ban_keyword")
    if any(sen in title for sen in get_filter("seniority_exclusions", [])):
        return reject("seniority")

    # Gate wealth/finance roles: require at least one systems, automation, or tooling anchor
    if any(term in title or term in description for term in ["wealth", "financial", "advisor", "branch", "banking"]):
        core_systems_keywords = [
            "python", "sql", "salesforce", "automation", "schwab", "fidelity",
            "docusign", "reconciliation", "excel", "hubspot", "api", "etl"
        ]
        if not any(k in description for k in core_systems_keywords):
            return reject("no_systems_anchor")

    return True

def resolve_outreach_copy(job):
    """Re-resolves (linkedin_note, outreach_email) for a cached job from the local template banks.

    Same path process_single_candidate() uses - pool -> persisted template id -> interpolate ->
    sanitize - so the /stage page can never drift from the copy the pipeline picked. Jobs cached
    before linkedin_template_id/outreach_template_id were persisted have no id, and
    resolve_template_text() bounds-checks a missing id down to template 0, so those render the
    first template in the bank rather than failing.
    """
    company_name = job.get("employer_name") or "your team"
    job_title = job.get("job_title") or "this role"
    # process_single_candidate() persists the resolved contact first name (or "" when none was
    # found) alongside the template ids; a job cached before that has no key -> "" -> "Hi,".
    greeting_name = str(job.get("outreach_contact_first_name") or "")
    linkedin_pool = load_linkedin_templates().get("linkedin_templates", [])
    linkedin_template = resolve_template_text(linkedin_pool, job.get("linkedin_template_id"))
    linkedin_note = sanitize_text(interpolate_template(linkedin_template, name=greeting_name, company=company_name, job_title=job_title))[:300]
    cold_pool = load_outreach_templates().get("cold_ops", [])
    cold_template = resolve_template_text(cold_pool, job.get("outreach_template_id"))
    outreach_email = sanitize_text(interpolate_template(cold_template, name=greeting_name, company=company_name, job_title=job_title))
    return linkedin_note, outreach_email

def process_single_candidate(job):
    log_metric_event("ai_screened", source=derive_job_source(job.get("job_id")))
    ai_pass, score, reason, track, tone_mode, bullet_indices, linkedin_template_id, outreach_template_id, layer1_bonus, gemini_base = evaluate_job_with_gemini(job)
    if ai_pass:
        raw_id = job.get("job_id") or f"{job.get('employer_name')}_{job.get('job_title')}"
        short_id = generate_short_key(raw_id, fallback=time.time())
        job_title = job.get("job_title") or "this role"
        company_name = job.get("employer_name") or "your team"

        # Strict Deterministic Template Engine: Gemini only routed a track + integer indices -
        # Python resolves/bounds-checks the actual bullet text and interpolates the actual
        # LinkedIn/outreach copy from local JSON banks. Gemini never authors this text directly.
        ats_bullets = filter_ats_bullets(track, bullet_indices, tone_mode)
        # Greet a resolved Carmen Warm CRM contact by first name ("Hi Dana,"); fall back to a
        # bare "Hi," when the company has no known contact. Read-only lookup - get_warm_crm_contacts()
        # is the same in-process cache the Layer 2 warm/Clavicular scoring below reuses.
        _warm_contact = get_warm_crm_contacts().get(normalize_company_for_match(job.get("employer_name")))
        greeting_name = first_name_for_greeting(_warm_contact.get("name") if _warm_contact else "")
        linkedin_pool = load_linkedin_templates().get("linkedin_templates", [])
        linkedin_template = resolve_template_text(linkedin_pool, linkedin_template_id)
        linkedin_note = sanitize_text(interpolate_template(linkedin_template, name=greeting_name, company=company_name, job_title=job_title))[:300]
        cold_pool = load_outreach_templates().get("cold_ops", [])
        cold_template = resolve_template_text(cold_pool, outreach_template_id)
        outreach_email = sanitize_text(interpolate_template(cold_template, name=greeting_name, company=company_name, job_title=job_title))

        # Persist routing keys on the cached job so /cv, /stage, and ATS plaintext all resolve
        # the exact same bullets and copy later (bounds-checked again by filter_ats_bullets /
        # resolve_template_text). The template ids - not the rendered text - are what gets stored,
        # so an /edit to a template bank changes what /stage shows without re-running the pipeline.
        job["track"] = track
        job["bullet_indices"] = bullet_indices
        job["tone_mode"] = tone_mode
        job["linkedin_template_id"] = linkedin_template_id
        job["outreach_template_id"] = outreach_template_id
        # Persist the greeting name (or "") so resolve_outreach_copy() and /stage re-render the
        # exact "Hi Dana," / "Hi," the pipeline picked without re-querying the CRM.
        job["outreach_contact_first_name"] = greeting_name
        sheet_uuid = save_job_to_cache(short_id, job)
        target_email = resolve_target_email(job.get("employer_name"), job.get("job_title"), job.get("employer_website"))
        age_badge = get_age_badge(parse_posted_hours(job.get("job_posted_at_datetime_utc")))
        salary_str, _ = extract_salary(job)
        work_style = extract_work_style(job)
        overlap_pct, matched_skills = calculate_keyword_overlap(job.get("job_description"))

        # Oddball Wildcard Badge: flags roles matching the rolling query bank's oddball keyword themes
        oddball_text = f"{job_title.lower()} {str(job.get('job_description') or '')[:300].lower()}"
        if any(kw in oddball_text for kw in ODDBALL_KEYWORDS):
            age_badge = f"{age_badge} 🎲 [WILDCARD ROLE]"

        # Running total of the Layer 2 points added/subtracted below (ghost penalty, alumni, warm/
        # Clavicular). Consumed two ways after the walk: (1) folded into the BONUS_STACK_CAP check
        # alongside Layer 1's bonus, (2) recomputed into the card's (+N) so it shows the capped
        # relationship delta, not the raw stacked sum. Layer 1's own bonus is NOT in here - it
        # rides in via layer1_bonus from evaluate_job_with_gemini.
        total_boost = 0

        # Ghost Listing Penalty: dock score + badge for reposted/evergreen listings (>3 sightings across >45 days)
        job_hash = generate_dedup_hash(job.get("employer_name"), job.get("job_title"))
        penalty, ghost_badge = get_ghost_listing_penalty(job_hash)
        if penalty:
            score = max(1, score + penalty)
            age_badge = f"{age_badge}{ghost_badge}"
            total_boost += penalty

        # JIT Hope College Alumni Resolution: live public-search lookup, score boost, auto-log Carmen Warm contact
        alumni_line = ""
        alum = resolve_live_alumni_at_company(job.get("employer_name"))
        if alum:
            score = min(100, score + 20)
            total_boost += 20
            alum_url_safe = html.escape(alum["linkedin_url"], quote=True)
            alumni_line = f"🎓 <b>Hope Alum Connection:</b> <a href='{alum_url_safe}'>{html.escape(alum['name'])}</a> ({html.escape(alum['headline'])})\n"
            today_str = datetime.now().strftime("%Y-%m-%d")
            alumni_payload = build_crm_payload(
                "quick_add",
                target_code="CW",
                sheet_uuid=str(uuid.uuid4()),
                first_contact=today_str,
                last_contact=today_str,
                name=alum["name"],
                company=job.get("employer_name"),
                priority=8,
                status="Warm Alum",
                next_followup=(datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d"),
                source="JIT Hope Alumni Discovery",
                note=f"[{today_str}] Auto-discovered via pipeline for {job.get('job_title')}. LinkedIn: {alum['linkedin_url']}"
            )
            enqueue_crm_payload(alumni_payload)
            log_daily_activity("notes_logged")

        # Dynamic Contact Quality Multiplier / Clavicular Routing: warm CRM contacts boost score.
        # ATS-sourced pulls (gh_/lever_/ashby_) at a Carmen Warm company route through the stricter
        # Clavicular pathway (flat +30, gated at raw score >= 70) instead of the priority multiplier.
        contact_info = get_warm_crm_contacts().get(normalize_company_for_match(job.get("employer_name")))
        is_clavicular = False
        contact_name = ""
        contact_note = ""
        if contact_info:
            is_ats_sourced = str(job.get("job_id") or "").startswith(("gh_", "lever_", "ashby_"))
            if is_ats_sourced:
                if score >= 70:
                    score = min(100, score + 30)
                    total_boost += 30
                    is_clavicular = True
                    contact_name = contact_info.get("name", "Contact")
                    contact_note = contact_info.get("note", "Active relationship")
                    alumni_line += (
                        f"🎯 <b>CLAVICULAR WARM REFERRAL (+30 pts):</b> "
                        f"{html.escape(contact_name)} "
                        f"<i>({html.escape(contact_info.get('raw_company', 'Firm'))})</i>\n"
                        f"📝 <b>Note:</b> {html.escape(contact_note)}\n"
                    )
                # raw score < 70: ATS + warm match does not qualify for any boost or Clavicular routing
            else:
                priority_score = contact_info.get("priority_score", 5)
                priority_boost = min(30, priority_score * 3)
                score = min(100, score + priority_boost)
                total_boost += priority_boost
                alumni_line += (
                    f"🔥 <b>WARM REFERRAL AVAILABLE (+{priority_boost} pts):</b> "
                    f"{html.escape(contact_info.get('name', 'Contact'))} "
                    f"<i>({html.escape(contact_info.get('raw_company', 'Firm'))} - Priority {priority_score}/10)</i>\n"
                    f"📝 <b>Note:</b> {html.escape(contact_info.get('note', 'Active relationship'))}\n"
                )

        # ---- Combined bonus stacking cap (Layer 1 + Layer 2) ----
        # score was walked up incrementally above so the Clavicular raw-score gate (>= 70) and the
        # per-step min(100, ...) clamps still saw the values they always did. Now recompute the
        # final score once from Gemini's true base: BONUS_STACK_CAP limits the SUM of every
        # positive additive bonus (Layer 1 keyword/salary + Layer 2 alumni/warm/Clavicular), then
        # the negative modifiers (Layer 1 hub/call-volume net, ghost-listing dock) apply on top
        # uncapped so a bad listing still drops. The inner min(100, ...) mirrors the pre-existing
        # per-step ceiling: bonuses can't push past 100 before a penalty bites into that headroom.
        ghost_pen = min(0, penalty)                     # ghost-listing dock (<= 0), already in total_boost
        l1_pos, l1_neg = max(0, layer1_bonus), min(0, layer1_bonus)
        l2_pos = total_boost - ghost_pen                # alumni + warm/Clavicular only (>= 0)
        capped_pos = min(BONUS_STACK_CAP, l1_pos + l2_pos)
        score = max(1, min(100, min(100, gemini_base + capped_pos) + l1_neg + ghost_pen))
        # Baseline the card's "(+N)" reconstructs to: Gemini + Layer 1 alone, Layer 1's positive
        # share already cap-limited, Layer 1 negatives kept. score - total_boost == this value
        # (clamps permitting), never an inflated phantom from summing raw un-applied boosts.
        score_before_layer2 = max(1, min(100, min(100, gemini_base + min(BONUS_STACK_CAP, l1_pos)) + l1_neg))
        total_boost = score - score_before_layer2

        # Second write, same sheet_uuid: the fit reason, matched skills and final score are all
        # computed/adjusted below the first save, and /stage is now the only place they are shown.
        # INSERT OR REPLACE makes this idempotent; without it the stage page has no source for them.
        job["fit_reason"] = reason
        job["matched_skills"] = matched_skills
        job["fit_score"] = score
        save_job_to_cache(short_id, job, sheet_uuid=sheet_uuid)

        return {
            "job": job, "score": score, "reason": reason,
            "linkedin_note": linkedin_note, "ats_bullets": ats_bullets,
            "outreach_email": outreach_email, "tone_mode": tone_mode,
            "target_email": target_email, "age_badge": age_badge,
            "salary_str": salary_str, "work_style": work_style,
            "overlap_pct": overlap_pct, "matched_skills": matched_skills,
            "short_id": short_id, "sheet_uuid": sheet_uuid,
            "alumni_line": alumni_line,
            "score_boost": total_boost,
            "is_clavicular": is_clavicular,
            "contact_name": contact_name,
            "contact_note": contact_note
        }
    return None
    # ==============================================================================
# 7. GMAIL API DRAFTING & CRM LOGGING
# ==============================================================================
def save_gmail_draft_record(to_email, subject, draft_id):
    """Persist a created Gmail draft's identity atomically for 24h dedup checks."""
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR REPLACE INTO gmail_drafts (to_email, subject, draft_id, created_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (to_email, subject, draft_id)
            )
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"DB Gmail Draft Save Error ({to_email}): {e}")
        return False

def check_existing_gmail_draft(to_email, subject):
    """Return existing draft metadata if (to_email, subject) was drafted in the last 24h, else None."""
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT draft_id, created_at FROM gmail_drafts WHERE to_email = ? AND subject = ? AND created_at >= datetime('now', '-1 day')",
                (to_email, subject)
            )
            row = cursor.fetchone()
            return {"draft_id": row[0], "created_at": row[1]} if row else None
    except Exception as e:
        logging.error(f"DB Gmail Draft Lookup Error ({to_email}): {e}")
        return None

def should_send_alert(alert_key: str, cooldown_hours: int = 6) -> bool:
    """Returns True if the alert has not been triggered within cooldown_hours (debounces repetitive alerts)."""
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT last_sent FROM system_alerts WHERE alert_key = ? AND last_sent >= datetime('now', ?)",
                (alert_key, f"-{cooldown_hours} hours")
            )
            if cursor.fetchone():
                return False
            conn.execute(
                "INSERT OR REPLACE INTO system_alerts (alert_key, last_sent) VALUES (?, CURRENT_TIMESTAMP)",
                (alert_key,)
            )
            conn.commit()
            return True
    except Exception as e:
        logging.error(f"Alert Debounce Error: {e}")
        return True

def get_gmail_access_token():
    """Refresh a Gmail OAuth access token. Alerts Telegram (debounced) and returns None on any failure."""
    token_url = "https://oauth2.googleapis.com/token"
    token_data = {
        "client_id": GMAIL_CLIENT_ID,
        "client_secret": GMAIL_CLIENT_SECRET,
        "refresh_token": GMAIL_REFRESH_TOKEN,
        "grant_type": "refresh_token"
    }
    try:
        token_res = requests.post(token_url, data=token_data, timeout=10)
        token_json = token_res.json()

        # Any OAuth failure (invalid_grant, revoked, etc.) needs an explicit re-auth alert
        if "error" in token_json:
            error_code = token_json.get("error", "unknown_error")
            if should_send_alert(f"oauth_{error_code}", cooldown_hours=6):
                oauth_link = (
                    "https://accounts.google.com/o/oauth2/v2/auth?"
                    f"client_id={GMAIL_CLIENT_ID}&redirect_uri=http://localhost&"
                    "scope=https://www.googleapis.com/auth/gmail.compose&response_type=code&"
                    "access_type=offline&prompt=consent"
                )
                alert_msg = (
                    f"⚠️ <b>Gmail OAuth Failure ({html.escape(error_code)})</b>\n"
                    f"Please re-authorize production access:\n"
                    f"<a href='{html.escape(oauth_link, quote=True)}'>Authorize Gmail</a>"
                )
                if TELEGRAM_CHAT_ID:
                    send_telegram_message(TELEGRAM_CHAT_ID, alert_msg)
            return None
        return token_json.get("access_token")
    except Exception as e:
        logging.error(f"Gmail OAuth Token Refresh Exception: {e}")
        return None

# UI-only company-name fallbacks (see /draft, /eh, /e, process_overdue_batch, /stage). Harmless
# on a Telegram card, but a real placeholder in an email body to a recruiter ("Saw the role at
# your team.") - so the Gmail send path refuses them rather than shipping one. Compared
# case-insensitively after trimming; an empty/whitespace company_name is refused too.
PLACEHOLDER_COMPANY_NAMES = frozenset({
    "target firm", "target company", "your team", "your company",
})

def is_placeholder_company_name(company_name):
    """True when company_name is empty or one of the known UI placeholder strings."""
    cleaned = str(company_name or "").strip()
    return not cleaned or cleaned.lower() in PLACEHOLDER_COMPANY_NAMES

def create_gmail_draft(to_email, company_name, job_title, is_warm=False, custom_note="", custom_body=None, custom_subject=None, pdf_bytes=None, pdf_filename="Kevin_Miller_Resume.pdf"):
    """Create Gmail draft with 24h dedup check and OAuth token expiry handling.
    Returns (success, message, draft_id) - draft_id is populated on success or when a duplicate is found.
    """
    missing_vars = [v for v in ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_USER"] if not os.environ.get(v)]
    if missing_vars:
        return False, f"Missing Env Vars: {', '.join(missing_vars)}", None

    # A placeholder company name means the caller never resolved a real employer - blocking here
    # keeps "Saw the role at your team." out of a recruiter's inbox. Callers already handle a
    # False return (manual-copy fallback path), so this degrades safely.
    if is_placeholder_company_name(company_name):
        blocked_name = str(company_name or "").strip()
        blocked_to = str(to_email or "").split(" [")[0].strip()
        logging.error(
            f"Gmail draft BLOCKED: placeholder company name {blocked_name!r} for {blocked_to} "
            f"(job_title={job_title!r}) - refusing to email a recruiter an unresolved company."
        )
        return False, f"Blocked: placeholder company name ({blocked_name!r})", None

    # Strip bracketed confidence tags (e.g. "user@x.com [⚠️ Fallback Email]") before this ever
    # reaches an SMTP header - the tag is a UI-only warning, never part of the real address.
    clean_to_email = str(to_email or "").split(" [")[0].strip()

    # Body and subject are resolved independently. A caller-supplied custom_body is used verbatim
    # (that is how /draft, /eh and /e hand over the exact string the Telegram card showed), but it
    # no longer drags the subject to the bump's "Re:" form with it: a first-touch cold email passed in
    # as custom_body still gets the cold subject. Only an explicit custom_subject overrides, and
    # the bump path passes one. Note check_existing_gmail_draft() dedups on subject, so changing
    # this changes what counts as a duplicate.
    if custom_body is not None:
        body_content = custom_body
    elif is_warm:
        body_content = generate_warm_email(custom_note)
    else:
        body_content = generate_cold_email(job_title, company_name)

    # Subjects front-load the role and carry no prefix. "Operations & Systems Alignment - " said
    # nothing a hiring manager could act on and pushed the role past the ~45-char mobile preview
    # cutoff, so the one fact that earns the open was the part that got truncated. Dash-joined
    # prefixes are retired across all three paths (cold/warm/bump) - see also the bump's
    # custom_subject at the /followup sendall path.
    if custom_subject:
        subject = custom_subject
    elif is_warm:
        subject = f"Reconnecting about {company_name}"
    else:
        subject = f"{job_title} @ {company_name}"

    existing = check_existing_gmail_draft(clean_to_email, subject)
    if existing:
        if TELEGRAM_CHAT_ID:
            send_telegram_message(
                TELEGRAM_CHAT_ID,
                f"ℹ️ <b>Draft Already Exists</b>\n"
                f"<b>To:</b> <code>{html.escape(clean_to_email)}</code>\n"
                f"<b>Subject:</b> <code>{html.escape(subject)}</code>\n"
                f"<b>Draft ID:</b> <code>{html.escape(str(existing['draft_id']))}</code>\n"
                f"<b>Created:</b> {html.escape(str(existing['created_at']))}"
            )
        return False, "Draft already exists in Gmail", existing["draft_id"]

    try:
        access_token = get_gmail_access_token()
        if not access_token:
            return False, "OAuth Token Unavailable", None

        message = EmailMessage()
        message["To"] = clean_to_email
        message["From"] = GMAIL_USER
        message["Subject"] = subject
        message.set_content(body_content)
        if pdf_bytes:
            message.add_attachment(
                pdf_bytes,
                maintype="application",
                subtype="pdf",
                filename=pdf_filename
            )
            logging.info(f"Gmail draft attachment: '{pdf_filename}' attached ({len(pdf_bytes)} bytes) for {clean_to_email}")
        else:
            logging.info(f"Gmail draft attachment: no pdf_bytes provided - draft for {clean_to_email} will be text-only")
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        draft_url = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        res = requests.post(draft_url, headers=headers, json={"message": {"raw": raw_message}}, timeout=10)
        if res.status_code in [200, 201]:
            draft_id = res.json().get("id", "")
            save_gmail_draft_record(clean_to_email, subject, draft_id)
            log_metric_event("gmail_draft_staged")
            return True, "Success", draft_id
        return False, f"Gmail Error {res.status_code}", None
    except Exception as e:
        return False, str(e), None

def compile_resume_pdf_resilient(chat_id, comp, track, bullet_indices, command_label, tone_mode="conservative"):
    """Compiles the tailored resume PDF with a fallback retry (track 'a', bullet_indices [0,1,2])
    if the first attempt raises or returns empty bytes. Sends a Telegram warning on final failure
    so a broken attachment is never silent. Returns pdf_bytes, or None if both attempts failed.
    """
    try:
        pdf_bytes = compile_resume_pdf(comp, track=track, bullet_indices=bullet_indices, tone_mode=tone_mode)
        if pdf_bytes:
            return pdf_bytes
        raise ValueError("compile_resume_pdf returned empty bytes")
    except Exception as e:
        logging.error(f"{command_label} resume compilation failed for {comp} (track={track}): {e} - retrying with fallback track 'a'")
        try:
            pdf_bytes = compile_resume_pdf(comp, track="a", bullet_indices=[0, 1, 2], tone_mode=tone_mode)
            if pdf_bytes:
                return pdf_bytes
            raise ValueError("fallback compile_resume_pdf returned empty bytes")
        except Exception as e:
            logging.error(f"{command_label} fallback resume compilation failed for {comp}: {e}")
            send_telegram_message(chat_id, f"⚠️ Resume compilation warning: {e}")
            return None

def is_verified_crm_contact(sender_raw):
    """Strict, exact-match CRM whitelist check for the inbound email anti-spam gatekeeper.
    Checks the local SQLite cache (sheet_row_map.contact_email, jobs.job_json target_email) first,
    then falls back to a live Google Sheets CRM lookup (find_contact_by_email) as the source of truth.
    Returns {"name", "company", "tab", "sheet_uuid"} on an exact match, or None if the sender is unverified.
    """
    email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", sender_raw or "")
    sender_email = email_match.group(0).lower().strip() if email_match else ""
    if not sender_email:
        logging.info("[BLOCKED] CRM whitelist check: no parsable sender email address")
        return None

    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT contact_name, contact_company, sheet_tab, sheet_uuid FROM sheet_row_map WHERE LOWER(contact_email) = ? ORDER BY created_at DESC LIMIT 1",
                (sender_email,)
            )
            row = cursor.fetchone()
            if row:
                logging.info(f"CRM whitelist DB query: match found for {sender_email} in sheet_row_map ({row[2]})")
                return {"name": row[0], "company": row[1], "tab": row[2], "sheet_uuid": row[3]}
            logging.info(f"CRM whitelist DB query: no sheet_row_map match for {sender_email}, checking jobs cache")

            # Fallback: exact target_email match inside cached job_json blobs (auto-generated job outreach targets)
            cursor.execute("SELECT sheet_uuid, job_json FROM jobs WHERE LOWER(job_json) LIKE ?", (f"%{sender_email}%",))
            for sheet_uuid, job_json in cursor.fetchall():
                try:
                    job_dict = json.loads(job_json)
                    cached_target = str(job_dict.get("target_email", "")).split(" [")[0].strip().lower()
                    if cached_target == sender_email:
                        logging.info(f"CRM whitelist DB query: match found for {sender_email} in jobs cache")
                        return {
                            "name": "",
                            "company": job_dict.get("employer_name", "Unknown"),
                            "tab": "Pipeline_Candidates",
                            "sheet_uuid": sheet_uuid
                        }
                except (json.JSONDecodeError, TypeError):
                    continue
            logging.info(f"CRM whitelist DB query: no local match for {sender_email}")
    except Exception as e:
        logging.error(f"CRM Whitelist Local Lookup Error: {e}")

    # Live authoritative check against the Google Sheets CRM (catches manual edits not yet cached locally)
    res = crm_get({"action": "find_contact_by_email", "email": sender_email})
    if res:
        try:
            logging.info(f"CRM whitelist remote query response status: {res.status_code} (email={sender_email})")
            if res.status_code == 200:
                data = res.json()
                if data.get("found"):
                    logging.info(f"CRM whitelist remote query: match found for {sender_email}")
                    return {
                        "name": data.get("name", ""),
                        "company": data.get("company", "Unknown"),
                        "tab": data.get("sheet_tab", "Unknown"),
                        "sheet_uuid": data.get("sheet_uuid", "")
                    }
                logging.info(f"CRM whitelist remote query: no match found for {sender_email}")
        except Exception as e:
            logging.error(f"CRM Whitelist Remote Lookup Error: {e}")

    return None

def match_unknown_sender_to_crm_company(sender_raw):
    """Second-chance lookup for a sender the strict whitelist rejected: does their DOMAIN belong
    to a company already tracked in the CRM?

    is_verified_crm_contact() is an exact-address match, which is what keeps a 7,000-message inbox
    out of Telegram. But it also silently swallows the one case that matters most - a reply from
    someone at the target firm Kevin never emailed directly: a colleague looped into the thread, an
    assistant answering for the manager, or an in-house recruiter picking up the req. Those land on
    a brand-new address at a KNOWN company, get dropped, and are marked read, so they are invisible
    in Telegram and in Gmail both.

    Reuses the same domain->company matcher the sent-mail capture path uses, so "Signal Advisors"
    still matches signaladvisors.com and a random unrelated domain still matches nothing. Returns a
    crm_match-shaped dict (sheet_uuid deliberately blank - there is no row for this person) or None.

    The caller alerts on this as UNVERIFIED and performs no CRM writes: a domain match is a reason
    to show Kevin the message, never a reason to move a stage or record an interview outcome.
    """
    email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", sender_raw or "")
    sender_email = email_match.group(0).lower().strip() if email_match else ""
    if not sender_email or is_role_mailbox(sender_email):
        return None
    try:
        company = match_email_to_crm_company(sender_email, get_all_crm_job_companies())
    except Exception as e:
        logging.error(f"Unknown-sender domain match error ({sender_email}): {e}")
        return None
    if not company:
        return None
    logging.info(f"[UNVERIFIED] {sender_email} is not a CRM contact, but its domain matches tracked company '{company}'")
    return {
        "name": name_from_email_local_part(sender_email),
        "company": company,
        "tab": "Unverified",
        "sheet_uuid": "",
    }

def is_thread_kevin_started(thread_id, access_token):
    """True when this Gmail thread already contains a message Kevin SENT.

    The last and widest of the three gates, and the one that catches what the other two
    structurally cannot: a NEW participant in an existing conversation. An introduction
    ("Kevin, meet Sarah at Vanguard - she's hiring"), a hiring manager looping in their
    recruiter, or a contact replying from a personal address all arrive from a sender that
    is neither a CRM contact (Gate 2) nor at a CRM company's domain (Gate 2b) - so both
    drop them silently, and an introduction is the single highest-value email this pipeline
    can receive.

    Thread participation is the right trust signal because Kevin STARTED the conversation:
    a stranger cannot inject themselves into a thread he began. That makes this safe to
    trust where a bare sender check is not. Spam arrives in new threads, so Gate 1's
    pre-filter and the two sender gates still carry the anti-spam load for first contact.

    Checks the SENT label on the thread rather than parsing participants, so one API call
    answers it. Errs toward False: a lookup failure silently falls through to the existing
    behaviour rather than opening the gate.
    """
    if not thread_id or not access_token:
        return False
    try:
        res = requests.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"format": "minimal"},
            timeout=10,
        )
        if res.status_code != 200:
            logging.info(f"Thread-participation check: HTTP {res.status_code} for thread {thread_id}")
            return False
        for message in res.json().get("messages", []):
            if "SENT" in (message.get("labelIds") or []):
                return True
        return False
    except Exception as e:
        logging.error(f"Thread-participation check error ({thread_id}): {e}")
        return False


def classify_inbound_ats_email(sender: str, subject: str, snippet: str):
    """
    Classifies ATS email into 'interview', 'rejection', or 'general'.
    Returns (status_label, crm_action)
    """
    text = f"{subject} {snippet}".lower()

    # Rejection is checked FIRST: "we were impressed but decided to move forward with other
    # candidates" contains an acceptance-shaped phrase inside a decline, and mislabelling a
    # rejection as an interview corrupts the outcome metrics in the direction that flatters.
    rejection_patterns = [
        r"unfortunately", r"not moving forward", r"other candidates",
        r"decided to pursue", r"(?:position|role|it)\s+(?:has been|was)\s+filled",
        r"impressed with your background, but", r"will not be", r"no longer (?:available|open)",
        r"pursuing other", r"not a (?:fit|match) at this time", r"keep your (?:resume|application) on file",
    ]
    if any(re.search(p, text) for p in rejection_patterns):
        return "REJECTION", "update_rejected"

    # Two families. The formal ATS phrasings were all this used to match, but Kevin's outreach is
    # peer-to-peer cold email, and a peer agreeing to talk does not write "invitation to
    # interview" - they write "happy to chat, do you have 15 minutes Thursday?". Those replies
    # scored GENERAL, so the interview metric and the outcome record never fired on exactly the
    # conversations the whole pipeline exists to produce.
    interview_patterns = [
        # formal / ATS
        r"invit(?:ation|e you|ing you) to (?:an? )?interview", r"interview request",
        r"schedule a (?:call|time|screen|chat|meeting)",
        r"selected for an interview", r"next steps with", r"speaking with our team",
        r"move forward with your application", r"set (?:up|something up)",
        # peer-to-peer acceptance
        r"happy to (?:chat|talk|connect|hop on)", r"(?:would|i'?d) love to (?:chat|talk|connect)",
        r"(?:are|r) you (?:free|available)", r"do you have (?:a few|some|\d+)\s*(?:minutes|mins)",
        r"send (?:over|me) some times", r"what(?:'s| is) your availability",
        r"works for me", r"let'?s (?:chat|talk|connect|set)", r"grab (?:15|20|30|a few)",
        r"calendly\.com", r"book a time",
    ]
    if any(re.search(p, text) for p in interview_patterns):
        return "INTERVIEW_SET", "update_interview"

    return "GENERAL", None

def passes_email_prefilter(sender: str, subject: str, snippet: str, internal_date_ms=None, in_reply_to="", references=""):
    """Zero-tolerance anti-spam pre-filter shield. Enforces the 10 EMAIL_* environment parameters
    BEFORE any CRM whitelist check runs. Returns (passed: bool, rejection_reason: str).
    """
    email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", sender or "")
    sender_email = email_match.group(0).lower().strip() if email_match else ""
    sender_domain = sender_email.split("@")[-1] if sender_email else ""
    subject_l = str(subject or "")
    combined_text = f"{subject} {snippet}".lower()

    # 1. Sender blacklist (substring match, e.g. "no-reply@", "noreply@")
    blacklist = [s.strip().lower() for s in EMAIL_SENDER_BLACKLIST.split(",") if s.strip()]
    if blacklist and any(b in sender_email for b in blacklist):
        return False, f"sender blacklisted ({sender_email})"

    # 2. Blocked domains
    block_domains = [d.strip().lower() for d in EMAIL_BLOCK_DOMAINS.split(",") if d.strip()]
    if sender_domain and block_domains and sender_domain in block_domains:
        return False, f"domain blocked ({sender_domain})"

    # 3. Allow-list domains (if configured, sender domain MUST be present)
    allow_domains = [d.strip().lower() for d in EMAIL_ALLOW_DOMAINS.split(",") if d.strip()]
    if allow_domains and sender_domain not in allow_domains:
        return False, f"domain not in allow-list ({sender_domain})"

    # 4. Excluded keywords (subject/body)
    excluded_kws = [k.strip().lower() for k in EMAIL_EXCLUDED_KEYWORDS.split(",") if k.strip()]
    if excluded_kws and any(kw in combined_text for kw in excluded_kws):
        return False, "excluded keyword matched"

    # 5. Required keywords (at least one must be present, if configured)
    required_kws = [k.strip().lower() for k in EMAIL_REQUIRED_KEYWORDS.split(",") if k.strip()]
    if required_kws and not any(kw in combined_text for kw in required_kws):
        return False, "no required keyword present"

    # 6. Subject regex filter
    if EMAIL_SUBJECT_REGEX_FILTER:
        try:
            if not re.search(EMAIL_SUBJECT_REGEX_FILTER, subject_l, re.IGNORECASE):
                return False, "subject regex mismatch"
        except re.error as e:
            logging.error(f"Invalid EMAIL_SUBJECT_REGEX_FILTER pattern: {e}")

    # 7. Minimum body/snippet length
    if len(str(snippet or "").strip()) < EMAIL_MIN_BODY_LENGTH:
        return False, f"body too short (< {EMAIL_MIN_BODY_LENGTH} chars)"

    # 8. Max message age (reject stale/backlog messages)
    if internal_date_ms is not None:
        try:
            age_seconds = time.time() - (int(internal_date_ms) / 1000.0)
            if age_seconds > EMAIL_MAX_AGE_SECONDS:
                return False, f"message too old ({int(age_seconds)}s > {EMAIL_MAX_AGE_SECONDS}s)"
        except (TypeError, ValueError):
            pass

    # 9. Require direct reply (In-Reply-To/References header or "Re:" subject prefix)
    if EMAIL_REQUIRE_DIRECT_REPLY:
        is_direct_reply = bool(in_reply_to) or bool(references) or subject_l.strip().lower().startswith("re:")
        if not is_direct_reply:
            return False, "not a direct reply"

    return True, ""

def route_inbound_reply_to_crm(crm_match, status_label, subject, snippet):
    """Carmen Cold is the hot seat: every verified inbound reply gets its follow-up pulled in, and a
    live human conversation additionally gets its contact moved into Carmen Cold with a dated note.

    Called only after BOTH anti-spam gates have already passed (pre-filter shield + CRM whitelist),
    so every sender reaching here is a known contact who actually wrote back. Two behaviors:

      1. Always (GENERAL / INTERVIEW_SET / REJECTION alike): push Next Followup Date to
         today + REPLY_FOLLOWUP_DAYS. Any reply is a reason to check back in soon.
      2. GENERAL only: move the contact into "Carmen Cold" (unless it already lives in a
         Carmen-family tab) and append a dated note recording the exchange.

    The GENERAL/INTERVIEW_SET/REJECTION split is the line that matters, not the contact's origin
    tab. INTERVIEW_SET and REJECTION are job-application *status* events - an ATS or recruiter
    reporting the outcome of one application - which is Tetiana's pipeline, so those rows stay put.
    GENERAL is what survives the no-reply@ blacklist without looking like a status change: a real
    person mid-conversation. That belongs in Carmen regardless of which tab first generated it.

    Every write goes through the durable outbox, which drains FIFO (ORDER BY id ASC), so the move
    is enqueued first and the note/date writes land on the row in its new home. Returns the list of
    enqueued payloads (for logging and tests); returns [] when there is no usable sheet_uuid.
    """
    sheet_uuid = str(crm_match.get("sheet_uuid") or "").strip()
    if not sheet_uuid:
        logging.info("[REPLY ROUTING] No sheet_uuid on the CRM match - skipping CRM writes")
        return []

    source_tab = str(crm_match.get("tab") or "")
    company = str(crm_match.get("company") or "").strip()
    # Same Carmen-family test resolve_smart_target_tab uses for /warm, /cold and /x.
    is_carmen = source_tab.startswith("Carmen")
    is_conversation = status_label == "GENERAL"
    today_str = datetime.now().strftime("%Y-%m-%d")
    payloads = []

    # Move first so the note and the follow-up date land on the row after it has been transposed
    # into the PEOPLE schema. Reuses /warm's exact mechanism - no new CRM action.
    if is_conversation and not is_carmen:
        payloads.append(build_crm_payload("update_status", sheet_uuid=sheet_uuid, new_tab="Carmen Cold"))
        # Same side effect the /warm and /cold handlers fire on any move landing in a Carmen tab:
        # a company Kevin is now actively talking to is worth sourcing future /t runs from.
        # auto_expand_ats_slug self-guards on is_probable_company_name, so a personal-contact
        # "company" is skipped before any board probe.
        if company:
            threading.Thread(target=auto_expand_ats_slug, args=(company,), daemon=True).start()

    # Part 1: every verified reply pulls the follow-up in. update_snooze writes Column G in place
    # (never a tab move) - the same action /f uses.
    next_followup = (datetime.now() + timedelta(days=REPLY_FOLLOWUP_DAYS)).strftime("%Y-%m-%d")
    payloads.append(build_crm_payload("update_snooze", sheet_uuid=sheet_uuid, next_followup=next_followup))

    if is_conversation:
        # Kevin asked for "the date and notes of the next follow up too" - reuses the subject and
        # snippet already pulled for the Telegram alert rather than refetching the message.
        clean_subject = " ".join(str(subject or "(No Subject)").split())[:120]
        clean_snippet = " ".join(str(snippet or "").split())[:200]
        move_clause = "" if is_carmen else f" Auto-moved to Carmen Cold from {source_tab or 'Unknown'}."
        note = (
            f"[{today_str}] Inbound reply received (they wrote to Kevin, not a send). "
            f"Subject: {clean_subject}. Preview: {clean_snippet}"
            f"{move_clause} Next follow-up {next_followup}."
        )
        payloads.append(build_crm_payload("append_note", sheet_uuid=sheet_uuid, note=note))

    for payload in payloads:
        enqueue_crm_payload(payload)
    if is_conversation:
        log_daily_activity("notes_logged")
    logging.info(
        f"[REPLY ROUTING] {status_label} reply from {source_tab or 'Unknown'} -> "
        f"{[p['action'] for p in payloads]} (next_followup={next_followup})"
    )
    return payloads

def check_inbound_gmail_replies():
    """Poll Gmail for unread inbound replies. Zero-tolerance anti-spam gatekeeper:
    1) Runs the 10-parameter pre-filter shield, 2) Requires an exact CRM whitelist match.
    Unverified/spam mail is silently dropped (label removed, no Telegram alert) - never surfaced.
    """
    missing_vars = [v for v in ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_USER"] if not os.environ.get(v)]
    if missing_vars or not TELEGRAM_CHAT_ID:
        return
    access_token = get_gmail_access_token()
    if not access_token:
        return
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        list_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
        params = {"q": f"is:unread -from:me label:{EMAIL_LABEL_TARGET_INBOX}", "maxResults": 10}
        res = requests.get(list_url, headers=headers, params=params, timeout=10)
        if res.status_code != 200:
            logging.error(f"Gmail Poll List Error: {res.status_code}")
            return
        message_ids = [m["id"] for m in res.json().get("messages", [])]
        logging.info(f"[POLL] Gmail list query returned {len(message_ids)} unread message(s) in label:{EMAIL_LABEL_TARGET_INBOX}")
    except Exception as e:
        logging.error(f"Gmail Poll List Exception: {e}")
        return

    for msg_id in message_ids:
        try:
            detail_url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}"
            detail_params = {"format": "metadata", "metadataHeaders": ["From", "Subject", "In-Reply-To", "References"]}
            detail_res = requests.get(detail_url, headers=headers, params=detail_params, timeout=10)
            if detail_res.status_code != 200:
                continue
            detail = detail_res.json()
            header_list = detail.get("payload", {}).get("headers", [])
            sender = next((h["value"] for h in header_list if h["name"] == "From"), "Unknown Sender")
            subject = next((h["value"] for h in header_list if h["name"] == "Subject"), "(No Subject)")
            in_reply_to = next((h["value"] for h in header_list if h["name"] == "In-Reply-To"), "")
            references = next((h["value"] for h in header_list if h["name"] == "References"), "")
            snippet = detail.get("snippet", "")
            internal_date_ms = detail.get("internalDate")
            thread_id = detail.get("threadId", msg_id)
            modify_url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}/modify"

            # GATE 1: Pre-filter shield (10 EMAIL_* parameters)
            passed, reject_reason = passes_email_prefilter(sender, subject, snippet, internal_date_ms, in_reply_to, references)
            if not passed:
                logging.info(f"[BLOCKED] Pre-filter rejected message from {sender} - reason: {reject_reason}")
                requests.post(modify_url, headers=headers, json={"removeLabelIds": ["UNREAD"]}, timeout=10)
                continue

            # GATE 2: Strict CRM whitelist - zero tolerance for unverified senders
            crm_match = is_verified_crm_contact(sender)
            is_unverified = False
            match_reason = ""
            if not crm_match:
                # GATE 2b: not a known contact, but the domain may belong to a tracked company -
                # a colleague, assistant or in-house recruiter replying from an address Kevin
                # never emailed. Surfaced as unverified rather than dropped; see
                # match_unknown_sender_to_crm_company() for why that case is worth the noise.
                crm_match = match_unknown_sender_to_crm_company(sender)
                is_unverified = bool(crm_match)
            if not crm_match and is_thread_kevin_started(thread_id, access_token):
                # GATE 2c: sender is a stranger, but this is a thread KEVIN STARTED - an
                # introduction, a looped-in colleague, or a contact writing from a personal
                # address. The widest gate and the last one, because it is the only one that
                # can see a new participant in an existing conversation.
                crm_match = {
                    "name": name_from_email_local_part(sender),
                    "company": "Unknown",
                    "tab": "Thread participant",
                    "sheet_uuid": "",
                }
                is_unverified = True
                match_reason = "thread participant"
            elif is_unverified:
                match_reason = "domain match"
            if not crm_match:
                logging.info(f"[BLOCKED] Unverified sender (not found in SQLite/Sheets CRM): {sender}")
                requests.post(modify_url, headers=headers, json={"removeLabelIds": ["UNREAD"]}, timeout=10)
                continue

            logging.info(f"[ALLOWED] {'Unverified (' + match_reason + ')' if is_unverified else 'Verified CRM'} sender {sender} matched to {crm_match.get('company')} ({crm_match.get('tab')})")

            thread_link = html.escape(f"https://mail.google.com/mail/u/0/#inbox/{thread_id}", quote=True)
            match_name = html.escape(str(crm_match.get("name") or "Unknown"))
            match_company = html.escape(str(crm_match.get("company") or "Unknown"))
            match_tab = html.escape(str(crm_match.get("tab") or "Unknown"))
            crm_line = f"<b>CRM Match:</b> {match_name} @ {match_company} <i>({match_tab})</i>\n"

            status_label, _crm_action = classify_inbound_ats_email(sender, subject, snippet)
            status_badges = {
                "INTERVIEW_SET": "🎉 <b>Interview Signal Detected!</b>\n",
                "REJECTION": "⚠️ <b>Rejection Detected</b>\n"
            }
            status_line = status_badges.get(status_label, "")
            # Outcome metrics and CRM routing are skipped for a domain-only match: there is no
            # sheet_uuid to attach them to, and a guess about WHO replied must never move a stage
            # or book an interview against the wrong row. Kevin gets the alert and decides.
            if is_unverified:
                logging.info(f"[UNVERIFIED] Skipping CRM writes for domain-match sender {sender}")
            elif status_label == "INTERVIEW_SET":
                log_metric_event("interview_set")
                record_application_outcome(crm_match.get("sheet_uuid"), "interview", company=crm_match.get("company"))
            elif status_label == "REJECTION":
                record_application_outcome(crm_match.get("sheet_uuid"), "rejection", company=crm_match.get("company"))

            # Follow-up bump for every verified reply; Carmen Cold move + dated note for a live
            # human conversation (GENERAL). Never changes the alert text below - this is what makes
            # the *system* treat the thread as a priority, not a second notification.
            if not is_unverified:
                try:
                    route_inbound_reply_to_crm(crm_match, status_label, subject, snippet)
                except Exception as e:
                    logging.error(f"Inbound Reply CRM Routing Error ({msg_id}): {e}")

            header_line = (
                "📬 <b>New Gmail Reply!</b>" if not is_unverified
                else f"⚠️ <b>Unverified Reply ({html.escape(match_reason)})</b>"
            )
            unverified_notes = {
                "domain match": "<i>Not a CRM contact - matched by company domain. No CRM changes were made.</i>\n",
                "thread participant": "<i>New person in a thread you started - possibly an introduction. No CRM changes were made.</i>\n",
            }
            unverified_note = "" if not is_unverified else unverified_notes.get(match_reason, "")
            alert_msg = (
                f"{header_line}\n\n"
                f"{status_line}"
                f"{unverified_note}"
                f"<b>From:</b> {html.escape(sender)}\n"
                f"{crm_line}"
                f"<b>Subject:</b> {html.escape(subject)}\n"
                f"<b>Preview:</b> <i>{html.escape(snippet)}</i>\n\n"
                f"<a href='{thread_link}'>Open Thread in Gmail</a>"
            )
            send_telegram_message(TELEGRAM_CHAT_ID, alert_msg)

            requests.post(modify_url, headers=headers, json={"removeLabelIds": ["UNREAD"]}, timeout=10)
        except Exception as e:
            logging.error(f"Gmail Poll Message Processing Error ({msg_id}): {e}")

# Dedicated scheduler instance: Gmail polling runs strictly once every 15 minutes,
# decoupled from Telegram webhook traffic (never triggered by incoming webhook pings).
EMAIL_POLL_SCHEDULER = BackgroundScheduler(daemon=True)

# Sent mail is rescanned over a rolling window rather than tracked by a stored watermark: the
# SQLite backing that would hold one is wiped by every Render deploy, which would silently reset
# the baseline and skip whatever was sent in between. Rescanning is safe because
# is_logged_person_contact() already makes capture idempotent - a person already in a PEOPLE tab
# is skipped - so the only cost of an overlap is a few extra lookups.
#
# The window is the ONLY thing standing between a sent message and permanent silent loss: mail
# that ages out before a poll sees it is never captured, and nothing reports that it was missed.
# 6h only survived a clean deploy gap - not a crashed worker, a Gmail 5xx, a free-tier spin-down,
# or mail sent late in the evening before an overnight restart. 72h gives three days of slack
# against all of those. It costs nothing: the dedup guard is per-message, not per-window, so a
# wider sweep re-examines already-captured people and skips them.
SENT_CAPTURE_LOOKBACK_HOURS = 72


def get_all_crm_job_companies():
    """Every company that has ever appeared as a job, across all job tabs.

    This is the gate for sent-mail contact capture: a person is only worth a Carmen Cold row
    when Kevin emailed them because of a job he is tracking. Tetiana Warm (applied) is included
    via get_applied_crm_companies(); the rest come from the live job tabs.
    """
    companies = set()
    for target_code in ("TC", "TW", "CL"):
        for record in fetch_networking_cards(target_code, qty=None):
            company = str(record.get("company") or "").strip()
            if company:
                companies.add(company)
    return companies


def is_logged_person_contact(email):
    """True when this address is already a row in a PEOPLE tab (Carmen Cold / Carmen Warm / Killed).

    The sent-mail capture gate. Deliberately NOT is_verified_crm_contact(), which searches every
    tab: an address on a JOBS row is the pipeline's outreach TARGET for that job, not a logged
    person, and the two records are both supposed to exist. Using the broad check here meant every
    contact Kevin reached via /e already "existed", so capture skipped exactly the people it was
    built to log - the Sheets whitelist query returned "match found for lvezzetti@crain.com" off
    the Crain job row and Angela was never written to Carmen Cold.

    Local sheet_row_map first (fast, and the only record of a contact captured on a previous run),
    then the live PEOPLE-tab lookup as the authority. Errs toward False: a lookup failure means the
    contact is written and the quick_add dedup guard in Code.gs collapses any duplicate, which is
    the safer direction than silently dropping a real contact.
    """
    clean = str(email or "").split(" [")[0].strip().lower()
    if not clean:
        return True  # nothing to capture
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM sheet_row_map WHERE LOWER(contact_email) = ? AND sheet_tab IN ('Carmen Cold','Carmen Warm','Killed') LIMIT 1",
                (clean,)
            )
            if cursor.fetchone():
                return True
    except Exception as e:
        logging.error(f"[SENT] Local person lookup error for {clean}: {e}")

    res = crm_get({"action": "find_contact_by_email", "email": clean, "people_only": "1"})
    if res is None:
        logging.warning(f"[SENT] PEOPLE-tab lookup unavailable for {clean} - treating as new")
        return False
    try:
        if res.status_code == 200:
            return bool(res.json().get("found"))
    except Exception as e:
        logging.error(f"[SENT] PEOPLE-tab lookup parse error for {clean}: {e}")
    return False


def log_addressed_contact_to_carmen_cold(email, company="", name="", note=""):
    """Log a person Kevin explicitly addressed via /e or /eh into Carmen Cold.

    The passive sent-mail sweep gates on match_email_to_crm_company(), which is right for a bulk
    scan: without it, every vendor, retailer and support thread in Sent becomes a "Cold Lead" with
    a follow-up date. But that gate also drops agency recruiters - a NextPath recruiter working a
    Raymond James role is a live conversation at an untracked company - and it throws away the
    strongest signal available, which is Kevin typing the address himself. A hand-typed /e IS the
    intent, so this path skips the company check entirely.

    Consumer domains and role mailboxes are still refused: /e on careers@ is addressing an inbox,
    not a person. Idempotent via is_logged_person_contact(), and the quick_add dedup guard in
    Code.gs is the backstop if two commands race.
    """
    clean = str(email or "").split(" [")[0].strip().lower()
    if not clean or is_role_mailbox(clean) or not company_domain_of(clean):
        return False
    if is_logged_person_contact(clean):
        return False

    today_str = datetime.now().strftime("%Y-%m-%d")
    sheet_uuid = str(uuid.uuid4())
    next_followup = (datetime.now() + timedelta(days=CARMEN_LADDER_DAYS[0])).strftime("%Y-%m-%d")
    contact_name = str(name or "").strip() or name_from_email_local_part(clean)
    contact_company = str(company or "").strip() or (company_domain_of(clean) or "").split(".")[0].title()
    payload = build_crm_payload(
        "quick_add",
        target_code="CC",
        sheet_uuid=sheet_uuid,
        first_contact=today_str,
        last_contact=today_str,
        name=contact_name,
        company=contact_company,
        email=clean,
        priority=5,
        status="Cold Lead",
        next_followup=next_followup,
        source="Addressed via /e",
        note=(note or f"[{today_str}] Emailed directly").strip(),
    )
    if not log_to_sheets_crm(payload):
        return False
    record_captured_contact(
        sheet_uuid=sheet_uuid,
        sheet_tab="Carmen Cold",
        contact_name=contact_name,
        contact_company=contact_company,
        contact_email=clean,
    )
    logging.info(f"[/e] Logged addressed contact {clean} ({contact_company}) to Carmen Cold")
    return True


def capture_contacts_from_sent_mail(lookback_hours=None, max_messages=25, dry_run=False):
    """Auto-populate Carmen Cold with every unique person emailed at a tracked job company.

    Scans Gmail SENT mail and writes one Carmen Cold row per new person. Role mailboxes
    (operations@, careers@) are skipped - those are the job pipeline's own targets and already
    live as job rows - as are consumer/ATS domains and anyone already in the CRM. Writes go
    straight to Sheets rather than through crm_outbox, whose SQLite backing is wiped by every
    Render deploy.

    Defaults scan the rolling SENT_CAPTURE_LOOKBACK_HOURS window, so the scheduled poll only ever
    considers recent mail. `lookback_hours=0` drops the date filter entirely and `max_messages`
    raises the page size, which is how /backfillcontacts sweeps the whole Sent backlog through
    this same path rather than duplicating the capture rules. `dry_run` resolves and reports
    contacts without writing anything, so a bulk sweep can be previewed before it touches Sheets.

    Returns the number of contacts captured (or, in dry_run, the number that would be).
    """
    missing_vars = [v for v in ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_USER"] if not os.environ.get(v)]
    if missing_vars:
        return 0
    access_token = get_gmail_access_token()
    if not access_token:
        return 0

    hours = SENT_CAPTURE_LOOKBACK_HOURS if lookback_hours is None else lookback_hours
    crm_companies = get_all_crm_job_companies()
    if not crm_companies:
        return 0

    headers = {"Authorization": f"Bearer {access_token}"}
    query = "in:sent"
    if hours:
        query += f" after:{int(time.time()) - int(hours) * 3600}"
    # Gmail caps maxResults at 500 per page, so a full-backlog sweep has to follow nextPageToken.
    message_ids = []
    page_token = None
    try:
        while len(message_ids) < max_messages:
            params = {"q": query, "maxResults": min(500, max_messages - len(message_ids))}
            if page_token:
                params["pageToken"] = page_token
            res = requests.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                headers=headers, params=params, timeout=10,
            )
            if res.status_code != 200:
                logging.error(f"[SENT] Gmail list error: {res.status_code}")
                return 0
            body = res.json()
            message_ids.extend(m["id"] for m in body.get("messages", []))
            page_token = body.get("nextPageToken")
            if not page_token:
                break
    except Exception as e:
        logging.error(f"[SENT] Gmail list exception: {e}")
        return 0

    captured = 0
    # Sent mail holds several messages to the same person (an outreach note, then a follow-up),
    # so a multi-message sweep would resolve the same contact repeatedly. is_logged_person_contact()
    # only sees contacts recorded on a PREVIOUS run, so track this run's own captures too.
    seen_emails = set()
    for msg_id in message_ids:
        try:
            detail_res = requests.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}",
                headers=headers,
                params={"format": "metadata", "metadataHeaders": ["To", "Subject"]},
                timeout=10,
            )
            if detail_res.status_code != 200:
                continue
            header_list = detail_res.json().get("payload", {}).get("headers", [])
            to_header = next((h["value"] for h in header_list if h["name"] == "To"), "")
            subject = next((h["value"] for h in header_list if h["name"] == "Subject"), "")

            contact = build_sent_contact(to_header, crm_companies)
            if not contact:
                continue
            email_key = str(contact["email"]).strip().lower()
            if email_key in seen_emails:
                continue
            if is_logged_person_contact(contact["email"]):
                continue
            seen_emails.add(email_key)

            if dry_run:
                captured += 1
                logging.info(f"[SENT][DRY RUN] would capture {contact['email']} ({contact['company']})")
                continue

            today_str = datetime.now().strftime("%Y-%m-%d")
            sheet_uuid = str(uuid.uuid4())
            # First rung of the Carmen Cold ladder, not the priority-derived interval: a cold
            # contact is worth a nudge in 3 days, where calculate_followup_interval(5) waits 19.
            next_followup = (datetime.now() + timedelta(days=CARMEN_LADDER_DAYS[0])).strftime("%Y-%m-%d")
            payload = build_crm_payload(
                "quick_add",
                target_code="CC",
                sheet_uuid=sheet_uuid,
                first_contact=today_str,
                last_contact=today_str,
                name=contact["name"],
                company=contact["company"],
                email=contact["email"],
                priority=5,
                status="Cold Lead",
                next_followup=next_followup,
                source="Auto-captured from Sent",
                note=f"[{today_str}] Emailed: {subject}".strip(),
            )
            if log_to_sheets_crm(payload):
                captured += 1
                # Record the contact locally BEFORE the Telegram send. is_logged_person_contact()
                # above reads sheet_row_map, so without this row the next poll re-captures the
                # same sent message as a brand-new contact - the sheet-side guard would suppress
                # the duplicate row, but the poller would still burn a write and fire a second
                # "Contact Captured" alert every 15 minutes for as long as the message stays in
                # the lookback window. save_message_mapping() requires a telegram_message_id and
                # returns False without one, so this path records the mapping directly.
                record_captured_contact(
                    sheet_uuid=sheet_uuid,
                    sheet_tab="Carmen Cold",
                    contact_name=contact["name"],
                    contact_company=contact["company"],
                    contact_email=contact["email"],
                )
                logging.info(f"[SENT] Captured {contact['email']} ({contact['company']}) to Carmen Cold")
                # One alert per contact is right for the daily poll's handful, and spam for a
                # backlog sweep - /backfillcontacts reports a single summary instead.
                if TELEGRAM_CHAT_ID and max_messages <= 25:
                    send_telegram_message(
                        TELEGRAM_CHAT_ID,
                        f"👤 <b>Contact Captured</b> · <code>Carmen Cold</code>\n"
                        f"<b>{html.escape(contact['name'])}</b> at {html.escape(contact['company'])}\n"
                        f"📧 <code>{html.escape(contact['email'])}</code>"
                    )
        except Exception as e:
            logging.error(f"[SENT] Capture error on message {msg_id}: {e}")

    if captured:
        logging.info(f"[SENT] Sent-mail capture complete: {captured} new contact(s).")
    return captured


def get_job_rows_with_guessed_email():
    """Every live JOBS row whose Contact Email is still a pipeline guess.

    Scanned across the job tabs (Tetiana Cold / Warm / Clavicular) so the back-fill can replace a
    placeholder with the address actually emailed. Rows already carrying a real address are
    filtered out here so the per-message match loop stays small.
    """
    rows = []
    for target_code in ("TC", "TW", "CL"):
        for record in fetch_networking_cards(target_code, qty=None):
            if not str(record.get("sheet_uuid") or "").strip():
                continue
            if not is_guessed_contact_email(record.get("email")):
                continue
            rows.append({
                "sheet_uuid": record.get("sheet_uuid"),
                "company": record.get("company"),
                "email": record.get("email"),
            })
    return rows


def backfill_contact_emails_from_sent_mail():
    """Replace guessed JOBS Contact Emails with the address actually emailed.

    resolve_target_email() writes an invented `operations@<company>.com` when a job is first
    logged, because there is no real contact yet. Once a human is emailed at that company, the
    Sent message carries the address that actually works - this promotes it onto the job row so
    the CRM shows who was contacted instead of a placeholder that may not even resolve.

    Only placeholder cells are ever overwritten (is_guessed_contact_email), so a hand-typed /e
    address and an already-back-filled one both survive, and rescanning the rolling Sent window
    is idempotent. Complements capture_contacts_from_sent_mail(), which files the same person as
    a Carmen Cold row; this one fixes the job row they were emailed about.
    """
    missing_vars = [v for v in ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_USER"] if not os.environ.get(v)]
    if missing_vars:
        return 0
    access_token = get_gmail_access_token()
    if not access_token:
        return 0

    job_rows = get_job_rows_with_guessed_email()
    if not job_rows:
        return 0

    after_epoch = int(time.time()) - SENT_CAPTURE_LOOKBACK_HOURS * 3600
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        res = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=headers,
            params={"q": f"in:sent after:{after_epoch}", "maxResults": 25},
            timeout=10,
        )
        if res.status_code != 200:
            logging.error(f"[BACKFILL] Gmail list error: {res.status_code}")
            return 0
        message_ids = [m["id"] for m in res.json().get("messages", [])]
    except Exception as e:
        logging.error(f"[BACKFILL] Gmail list exception: {e}")
        return 0

    updated = 0
    for msg_id in message_ids:
        try:
            detail_res = requests.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}",
                headers=headers,
                params={"format": "metadata", "metadataHeaders": ["To"]},
                timeout=10,
            )
            if detail_res.status_code != 200:
                continue
            header_list = detail_res.json().get("payload", {}).get("headers", [])
            to_header = next((h["value"] for h in header_list if h["name"] == "To"), "")

            match = resolve_sent_email_backfill(to_header, job_rows)
            if not match:
                continue
            sheet_uuid, real_email = match

            if not log_to_sheets_crm(build_crm_payload("update_contact_email", sheet_uuid=sheet_uuid, email=real_email)):
                continue
            updated += 1
            # Drop the row from the working set so a second message to the same company in this
            # same batch can't overwrite the address just written.
            job_rows = [r for r in job_rows if r.get("sheet_uuid") != sheet_uuid]
            logging.info(f"[BACKFILL] Contact email set to {real_email} on job row {sheet_uuid}")
            if TELEGRAM_CHAT_ID:
                send_telegram_message(
                    TELEGRAM_CHAT_ID,
                    f"📧 <b>Contact Email Back-filled</b>\n"
                    f"<code>{html.escape(real_email)}</code>\n"
                    f"<i>replaced a guessed address on the job row</i>"
                )
        except Exception as e:
            logging.error(f"[BACKFILL] Error on message {msg_id}: {e}")

    if updated:
        logging.info(f"[BACKFILL] Contact-email back-fill complete: {updated} row(s) updated.")
    return updated


def scheduled_email_poll_job():
    """APScheduler job target: fires on the EMAIL_POLL_HOURS cadence, independent of webhook load."""
    logging.info("[POLL] Email poll cycle triggered")
    try:
        check_inbound_gmail_replies()
    except Exception as e:
        logging.error(f"[POLL] Gmail Poller Cycle Error: {e}")
    try:
        capture_contacts_from_sent_mail()
    except Exception as e:
        logging.error(f"[POLL] Sent-mail Capture Error: {e}")
    try:
        backfill_contact_emails_from_sent_mail()
    except Exception as e:
        logging.error(f"[POLL] Contact-email Back-fill Error: {e}")
    logging.info("[POLL] Email poll cycle completed")

# How often the Gmail poller runs, in hours. Was a hardcoded 15 minutes, which cost more than it
# returned: each cycle takes SQLite write locks (BEGIN IMMEDIATE, 5s busy_timeout) for inbound
# replies, sent-mail capture and email back-fill, so a cycle landing mid-/t contends with the
# pipeline's own writes across 20 concurrently scored jobs. Once a day is the default; set
# EMAIL_POLL_HOURS to tune it, or EMAIL_POLL_ENABLED=false to turn scheduled polling off
# entirely. /poll always runs a cycle on demand regardless of either setting.
EMAIL_POLL_HOURS = float(os.environ.get("EMAIL_POLL_HOURS", "24"))
EMAIL_POLL_ENABLED = os.environ.get("EMAIL_POLL_ENABLED", "true").strip().lower() not in ("false", "0", "no", "off")

def start_gmail_poller():
    """Register the Gmail reply poller on an EMAIL_POLL_HOURS interval trigger (APScheduler),
    replacing the old fixed-sleep thread loop. Ensures polling never runs on webhook requests.

    No longer fires immediately on boot. Render restarts the container on every deploy, so an
    immediate run meant a full poll cycle competing with startup - and with whatever /t was
    dispatched right after - each time the service came up.
    """
    if not EMAIL_POLL_ENABLED:
        EMAIL_POLL_SCHEDULER.start()
        logging.info("[POLL] Scheduled Gmail polling DISABLED (EMAIL_POLL_ENABLED=false). Use /poll to run on demand.")
        return
    EMAIL_POLL_SCHEDULER.add_job(
        scheduled_email_poll_job,
        trigger="interval",
        hours=EMAIL_POLL_HOURS,
        id="gmail_inbound_poll",
        next_run_time=datetime.now() + timedelta(hours=EMAIL_POLL_HOURS),
        max_instances=1,
        coalesce=True
    )
    EMAIL_POLL_SCHEDULER.start()
    logging.info(f"[POLL] Gmail inbound poller scheduled: every {EMAIL_POLL_HOURS} hour(s)")

# Overridable so the host can point backups at the same mounted disk as JOBS_DB_PATH. Backups
# written next to the code are lost with the container on every deploy - a snapshot that dies
# with the thing it was protecting is not a backup.
BACKUP_DIR = os.environ.get(
    "BACKUP_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups"),
)
BACKUP_RETENTION_COUNT = 8  # ~2 months of weekly snapshots
BACKUP_CRITICAL_TABLES = ("jobs", "sheet_row_map", "crm_outbox", "application_outcomes", "pipeline_metrics")

def verify_backup_snapshot(dest_path, min_expected_counts):
    """Restore-verify a snapshot: PRAGMA integrity_check plus a row-count floor per critical table
    (captured from the live DB immediately before the backup). Never raises - returns (ok, details).
    """
    details = {}
    verify_conn = None
    try:
        verify_conn = sqlite3.connect(dest_path)
        cursor = verify_conn.cursor()
        cursor.execute("PRAGMA integrity_check")
        integrity_result = cursor.fetchone()[0]
        details["integrity_check"] = integrity_result
        if integrity_result != "ok":
            return False, details
        for table in BACKUP_CRITICAL_TABLES:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                backup_count = cursor.fetchone()[0]
            except sqlite3.OperationalError:
                backup_count = None
            expected_min = min_expected_counts.get(table, 0)
            details[table] = {"backup_count": backup_count, "expected_min": expected_min}
            if backup_count is None or backup_count < expected_min:
                return False, details
        return True, details
    except Exception as e:
        details["error"] = str(e)
        return False, details
    finally:
        if verify_conn:
            verify_conn.close()

def backup_sqlite_db():
    """Snapshot jobs_cache.db via the SQLite online backup API (safe under concurrent WAL writers)
    into backups/, restore-verify it (integrity_check + row-count floor vs pre-backup counts), then
    prune down to the most recent BACKUP_RETENTION_COUNT snapshots. Alerts Telegram if verification
    fails - a backup that was never restore-tested is not a proven durability net.
    """
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_path = os.path.join(BACKUP_DIR, f"jobs_cache_{stamp}.db")

        pre_backup_counts = {}
        with get_db_conn() as src_conn:
            cursor = src_conn.cursor()
            for table in BACKUP_CRITICAL_TABLES:
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {table}")
                    pre_backup_counts[table] = cursor.fetchone()[0]
                except sqlite3.OperationalError:
                    pre_backup_counts[table] = 0
            dest_conn = sqlite3.connect(dest_path)
            src_conn.backup(dest_conn)
            dest_conn.close()
        logging.info(f"[BACKUP] SQLite snapshot written: {dest_path}")

        verified, verify_details = verify_backup_snapshot(dest_path, pre_backup_counts)
        if verified:
            logging.info(f"[BACKUP] Restore verification passed: {dest_path}")
        else:
            logging.error(f"[BACKUP] Restore verification FAILED for {dest_path}: {verify_details}")
            send_health_alert(f"Backup restore verification failed for {os.path.basename(dest_path)}: {verify_details}")

        snapshots = sorted(
            (f for f in os.listdir(BACKUP_DIR) if f.startswith("jobs_cache_") and f.endswith(".db")),
            reverse=True
        )
        for stale_file in snapshots[BACKUP_RETENTION_COUNT:]:
            try:
                os.remove(os.path.join(BACKUP_DIR, stale_file))
            except OSError:
                pass
        return verified
    except Exception as e:
        logging.error(f"[BACKUP] SQLite snapshot failed: {e}")
        send_health_alert(f"Weekly SQLite backup failed: {e}")
        return False

def scheduled_backup_job():
    logging.info("[BACKUP] Weekly SQLite backup cycle triggered")
    backup_sqlite_db()

def start_backup_scheduler():
    """Register the weekly SQLite backup on the existing background scheduler (Sunday 3 AM local)."""
    EMAIL_POLL_SCHEDULER.add_job(
        scheduled_backup_job,
        trigger="cron",
        day_of_week="sun",
        hour=3,
        id="sqlite_weekly_backup",
        max_instances=1,
        coalesce=True
    )
    logging.info("[BACKUP] Weekly SQLite backup scheduled: Sundays 03:00 local")

PERSISTENCE_WATCH_TABLES = ("seen_jobs", "pipeline_metrics", "application_outcomes", "daily_activity")

def count_backup_snapshots(backup_dir):
    """Whether backup_dir exists and how many jobs_cache_*.db snapshots are in it.

    Pulled out of get_persistence_status() so the directory-scan logic (the part most likely
    to silently do the wrong thing on a misconfigured mount) can be unit-tested against a
    real tmp_path without needing a live DB connection.
    """
    if not os.path.isdir(backup_dir):
        return False, 0
    count = sum(1 for f in os.listdir(backup_dir) if f.startswith("jobs_cache_") and f.endswith(".db"))
    return True, count

def get_persistence_status():
    """Row counts for the tables a wiped/misconfigured disk used to silently zero out, plus the
    resolved DB_PATH/BACKUP_DIR and on-disk snapshot count - so /health (both the JSON route and
    the Telegram card) can show whether the Render persistent disk is actually mounted and data
    is surviving redeploys, instead of that only being discoverable after weeks of silent loss.
    """
    row_counts = {}
    with get_db_conn() as conn:
        cursor = conn.cursor()
        for table in PERSISTENCE_WATCH_TABLES:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            row_counts[table] = cursor.fetchone()[0]

    backup_dir_exists, snapshot_count = count_backup_snapshots(BACKUP_DIR)

    return {
        "db_path": os.path.abspath(DB_PATH),
        "row_counts": row_counts,
        "backup_dir": os.path.abspath(BACKUP_DIR),
        "backup_dir_exists": backup_dir_exists,
        "backup_snapshot_count": snapshot_count,
    }

# The morning digest previews only the most overdue handful and points at /overdue for the
# rest. Kevin was getting 100+ lines split across several Telegram messages before he had
# had coffee, which is the same as getting none of them.
OVERDUE_DIGEST_PREVIEW_LIMIT = 10

# Telegram hard-caps a message at 4096 chars; this is the existing safety margin.
TELEGRAM_CHUNK_CHARS = 3900

def format_followup_due(value):
    """The 'due' cell of one digest line, with Code.gs's blank-date sentinel translated back.

    get_overdue_followups() already drops unscheduled records, so in the normal path this
    never fires - it is the second line of defence that keeps a literal 'due 1970-01-01'
    (which is what Kevin saw on every line) out of the UI for any other caller.
    """
    return "no date set" if is_followup_unscheduled(value) else str(value)

def render_overdue_lines(overdue):
    """One HTML-escaped '• <b>Company</b> - Name | Tab | due X' line per overdue record."""
    lines = []
    for record in overdue:
        tab = html.escape(str(record.get("sheet_tab") or "Unknown"))
        company = html.escape(str(record.get("company") or "N/A"))
        name = html.escape(str(record.get("name") or ""))
        due = html.escape(format_followup_due(record.get("next_followup")))
        lines.append(f"• <b>{company}</b>{f' - {name}' if name else ''} | {tab} | due {due}")
    return lines

def send_overdue_digest(chat_id, overdue, limit=OVERDUE_DIGEST_PREVIEW_LIMIT):
    """Send the overdue list to Telegram, chunked to stay under the message size cap.

    limit=None sends everything (that is /overdue); an integer limit sends the N most overdue
    followed by a pointer to /overdue for the remainder (that is the morning digest). Records
    arrive next_followup ASC, so the head of the list is the most overdue.
    """
    if not overdue:
        send_telegram_message(chat_id, "✅ <b>No overdue records.</b> Nothing is past its follow-up date.")
        return

    shown = overdue if limit is None else overdue[:limit]
    if limit is None:
        lines = [f"⚠️ <b>All Overdue Records ({len(overdue)}, next follow-up ASC):</b>"]
    else:
        lines = [f"⚠️ <b>Most Overdue ({len(shown)} of {len(overdue)}, next follow-up ASC):</b>"]
    lines.extend(render_overdue_lines(shown))
    remaining = len(overdue) - len(shown)
    if remaining > 0:
        lines.append(f"<i>...and {remaining} more.</i> <code>/overdue</code> for the full list.")

    # Chunked rather than truncated: the capped preview always fits in one message, and
    # /overdue must not silently drop the tail of a long list.
    chunk = ""
    for line in lines:
        if chunk and len(chunk) + len(line) + 1 > TELEGRAM_CHUNK_CHARS:
            send_telegram_message(chat_id, chunk)
            chunk = ""
        chunk = f"{chunk}\n{line}".strip()
    if chunk:
        send_telegram_message(chat_id, chunk)

def send_tuesday_pipeline_executive_hub(chat_id):
    """Send Tuesday's weekly operations hub and all overdue records in Telegram-safe chunks."""
    weekly = get_rolling_metric_counts(days=7)
    golden_ratio = (weekly["interview_set"] / weekly["gmail_draft_staged"] * 100) if weekly["gmail_draft_staged"] else 0.0
    api_usage = get_monthly_api_usage()
    ats_count = len(safe_list(get_filter("ats_company_slugs", [])))
    overdue = get_overdue_followups()
    today_str = datetime.now().strftime("%Y-%m-%d")
    hub = (
        f"📈 <b>Tuesday Pipeline Executive &amp; Batch Hub ({today_str})</b>\n\n"
        f"<b>Rolling 7-Day Pipeline:</b>\n"
        f"• Discovered: {weekly['listing_discovered']} | AI Screened: {weekly['ai_screened']}\n"
        f"• Drafted: {weekly['gmail_draft_staged']} | Applied: {weekly['applied']} | Interviews: {weekly['interview_set']}\n"
        f"• <b>Golden Ratio:</b> {golden_ratio:.1f}% (interviews / staged drafts)\n\n"
        f"<b>Coverage &amp; Enrichment:</b>\n"
        f"• ATS boards: {ats_count}\n"
        f"• Hunter.io: {api_usage['hunter']} | Prospeo: {api_usage['prospeo']} | GetProspect: {api_usage['getprospect']} (month-to-date local calls)\n\n"
        f"⚠️ <b>Overdue:</b> {len(overdue)} records\n"
        f"<code>/sendall</code> Draft bumps + set all eligible records to +14d\n"
        f"<code>/snoozeall 7</code> Move all overdue follow-ups by N days\n"
        f"<code>/overdue</code> Full overdue list on demand"
    )
    send_telegram_message(chat_id, hub)
    send_telegram_message(chat_id, format_outcome_metrics_message())
    if not overdue:
        return
    send_overdue_digest(chat_id, overdue)

def send_daily_standup(chat_id):
    """Send the compact 08:30 standup used on every non-Tuesday morning."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    activity = get_daily_activity(today_str)
    streak = calculate_active_day_streak()
    overdue_count = len(get_overdue_followups())
    digest = (
        f"🌅 <b>Daily Standup ({today_str})</b>\n\n"
        f"🔥 <b>Active Streak:</b> {streak} days\n"
        f"🎯 <b>Today's Staged Goal:</b> {activity['drafts_staged']} / 5\n"
        f"⚠️ <b>Overdue Actions:</b> {overdue_count}\n\n"
        f"Run <code>/s</code> to review overdue contacts or <code>/t</code> to trigger the search pipeline."
    )
    health_warnings = check_system_health()
    if health_warnings:
        digest += "\n\n🚨 <b>Config Health Warnings:</b>\n" + "\n".join(f"• {html.escape(w)}" for w in health_warnings)
    send_telegram_message(chat_id, digest)

def morning_digest_loop():
    """Dispatch Tuesday's executive hub or the compact daily standup at 08:30 local time."""
    while True:
        now = datetime.now()
        target_time = now.replace(hour=8, minute=30, second=0, microsecond=0)
        if now >= target_time:
            target_time += timedelta(days=1)

        time.sleep((target_time - now).total_seconds())
        try:
            if TELEGRAM_CHAT_ID:
                if datetime.now().weekday() == 1:  # Tuesday
                    send_tuesday_pipeline_executive_hub(TELEGRAM_CHAT_ID)
                else:
                    send_daily_standup(TELEGRAM_CHAT_ID)
        except Exception as e:
            logging.error(f"Morning Digest Dispatch Error: {e}")

def check_system_health():
    """Returns human-readable warnings for missing critical config. Surfaced daily in the morning
    digest so a lost env var (bad redeploy, expired secret) doesn't silently degrade the pipeline
    for weeks before anyone notices - the single biggest risk for a years-long unattended system.
    """
    warnings = []
    if not CRM_WEBHOOK_URL:
        warnings.append("CRM_WEBHOOK_URL is unset - CRM sync is fully disabled.")
    if not GEMINI_API_KEY:
        warnings.append("GEMINI_API_KEY is unset - AI screening will fail every candidate.")
    if not (os.environ.get("RAPIDAPI_KEY") or os.environ.get("OPENWEBNINJA_KEY")):
        warnings.append("No JSearch API key set (RAPIDAPI_KEY/OPENWEBNINJA_KEY) - job sourcing is disabled.")
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        warnings.append("Telegram credentials missing - operator notifications are disabled.")
    if not CRM_SHARED_SECRET:
        warnings.append("CRM_SHARED_SECRET is unset - the CRM webhook is unauthenticated.")
    return warnings

def start_morning_digest():
    """Spin up the 8:30 AM daily standup digest as a daemon thread."""
    threading.Thread(target=morning_digest_loop, daemon=True).start()

def log_to_sheets_crm(payload, max_retries=3):
    """Log to Google Sheets CRM. Payload may include row UUID and note timestamp.
    Support apps script bottom-to-top search loops via rowOperationOrder: 'DESC'.

    HTTP 200 is NOT success here. An Apps Script web app answers 200 for everything it handles,
    including its own {"status": "error"} bodies - an unset/mismatched CRM_SHARED_SECRET Script
    Property makes doPost reject every write as "Unauthorized" behind a 200. Trusting the status
    code alone let a fully-rejected batch read as a successful write, which opened the Tier-1 card
    gate in run_job_pipeline() and dispatched Telegram cards for rows that never reached the sheet.
    So the body decides: status must be "success", and for a row-writing action the reported count
    must match the number of rows sent (a short count means the Apps Script dedup guard suppressed
    some, which the caller needs to know about rather than read as a clean write).

    An auth rejection is not retryable - the secret will not fix itself between attempts - so it
    alerts and returns immediately instead of burning the backoff.
    """
    if not CRM_WEBHOOK_URL:
        return False
    # Ensure row operation order is DESC for backwards loop searches
    if "rowOperationOrder" not in payload:
        payload["rowOperationOrder"] = "DESC"
    action = payload.get("action", "unknown")
    expected_rows = len(payload.get("rows") or []) if action == "batch_add_rows" else None
    delay = 1.0
    for attempt in range(max_retries):
        try:
            res = crm_post(payload)
            if res and res.status_code == 200:
                try:
                    body = res.json()
                except Exception:
                    # A non-JSON 200 is the Apps Script HTML error/login page, not a written row.
                    logging.error(f"CRM '{action}': non-JSON 200 response: {res.text[:200]}")
                    body = None

                if isinstance(body, dict):
                    status = str(body.get("status", "")).lower()
                    message = str(body.get("message", ""))
                    if status == "success":
                        if expected_rows is not None:
                            written = safe_int(body.get("count"), 0)
                            if written < expected_rows:
                                logging.error(
                                    f"CRM batch_add_rows wrote {written}/{expected_rows} rows: {message}"
                                )
                                send_health_alert(
                                    f"CRM batch wrote only {written} of {expected_rows} row(s) - "
                                    f"{expected_rows - written} suppressed as duplicate(s). {message}"
                                )
                                return False
                        return True

                    logging.error(f"CRM '{action}' rejected by Apps Script: {message}")
                    if "unauthorized" in message.lower():
                        send_health_alert(
                            "CRM webhook is rejecting every write as Unauthorized - rows are NOT "
                            "reaching the sheet. Set the CRM_SHARED_SECRET Script Property in the "
                            "Apps Script project to match Render's CRM_SHARED_SECRET, then redeploy "
                            "the web app (Deploy > Manage deployments > New version)."
                        )
                        return False
        except Exception as e:
            logging.error(f"CRM Webhook Attempt {attempt+1} Failed: {e}")
        time.sleep(delay)
        delay *= 2.0
    send_health_alert(f"Failed to log payload to Google Sheets after {max_retries} attempts.")
    return False

def enqueue_crm_payload(payload):
    """Enqueues an outbound Sheets write to local SQLite atomically (durable outbox pattern)."""
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO crm_outbox (payload_json, status) VALUES (?, 'PENDING')",
                (json.dumps(payload),)
            )
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"CRM Outbox Enqueue Error: {e}")
        return False

def process_crm_outbox_batch(inter_job_sleep=1.0):
    """One outbox drain pass (<=5 pending rows): dispatch each to Sheets, delete on success or bump
    retry_count/status on failure. Split out from crm_outbox_worker_loop so a single pass is unit-testable.
    """
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, payload_json, retry_count 
            FROM crm_outbox 
            WHERE status = 'PENDING' AND retry_count < 10 
            ORDER BY id ASC LIMIT 5
        """)
        pending_jobs = cursor.fetchall()

    for job_id, payload_str, retries in pending_jobs:
        payload = json.loads(payload_str)
        success = log_to_sheets_crm(payload, max_retries=1)

        with get_db_conn() as conn:
            if success:
                conn.execute("DELETE FROM crm_outbox WHERE id = ?", (job_id,))
            else:
                conn.execute("""
                    UPDATE crm_outbox 
                    SET retry_count = retry_count + 1, 
                        last_attempt = CURRENT_TIMESTAMP,
                        status = CASE WHEN retry_count + 1 >= 10 THEN 'FAILED' ELSE 'PENDING' END
                    WHERE id = ?
                """, (job_id,))
            conn.commit()
        if inter_job_sleep:
            time.sleep(inter_job_sleep)

def crm_outbox_worker_loop():
    """Background daemon processing queued Sheets writes with exponential backoff."""
    while True:
        try:
            process_crm_outbox_batch()
        except Exception as e:
            logging.error(f"CRM Outbox Worker Error: {e}")
        time.sleep(5)

def start_crm_outbox_worker():
    """Spin up the persistent CRM outbox worker as a daemon thread."""
    threading.Thread(target=crm_outbox_worker_loop, daemon=True).start()

def fetch_networking_cards(target_code="CW", qty=2):
    res = crm_post({"action": "get_followups", "tab": target_code})
    if not res:
        return []
    try:
        if res.status_code == 200:
            leads = res.json().get("followups", [])
            return leads if qty is None else leads[:qty]
    except Exception as e:
        logging.error(f"Error fetching networking cards: {e}")
    return []

def get_overdue_followups():
    """Return every overdue Carmen Cold, Carmen Warm and Tetiana Cold record sorted by
    next_followup ASC.

    Carmen Cold is scanned because it is now the active outreach cycle - replies auto-move
    contacts here, so its overdue rows are the hottest leads and must reach the morning digest.
    Carmen Warm stays scanned even though it is now mostly storage: the is_followup_unscheduled()
    filter below already drops its undated bulk, and a warm contact with an explicit Next
    Followup Date is a reminder Kevin set by hand and would want surfaced.

    UNSCHEDULED RECORDS ARE NOT OVERDUE. Code.gs's formatFollowupDate() turns a blank Next
    Followup Date cell into the string "1970-01-01" so its own overdue sort never feeds NaN
    to `new Date(...)`. That sentinel is <= today, so every undated row used to land here and
    sort to the very front - and since Carmen Warm is personal contacts that are almost never
    dated, Kevin's entire warm network showed up as maximally overdue every morning.

    This is also what /sendall and /snoozeall iterate (process_overdue_batch), so the filter
    additionally stops those from drafting bump emails to rows that were never scheduled.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    overdue = []
    for target_code, tab_name in (("CC", "Carmen Cold"), ("CW", "Carmen Warm"), ("TC", "Tetiana Cold")):
        for record in fetch_networking_cards(target_code, qty=None):
            next_followup = str(record.get("next_followup") or "")
            if is_followup_unscheduled(next_followup):
                continue
            if next_followup <= today_str:
                overdue.append({**record, "sheet_tab": tab_name})
    return sorted(overdue, key=lambda record: str(record.get("next_followup") or ""))

def process_overdue_batch(mode, snooze_days=7):
    """Apply a batch follow-up action to every overdue record using the durable CRM outbox.
    `sendall` drafts a personalized bump first and only advances rows whose draft was created or
    already exists; `snoozeall` advances every overdue row without creating a draft.
    """
    overdue = get_overdue_followups()
    next_followup = (datetime.now() + timedelta(days=snooze_days)).strftime("%Y-%m-%d")
    result = {"total": len(overdue), "updated": 0, "drafted": 0, "skipped": 0}
    for record in overdue:
        if not record.get("sheet_uuid"):
            result["skipped"] += 1
            continue
        if mode == "sendall":
            email = str(record.get("email") or "").strip()
            if not email or "[" in email:
                result["skipped"] += 1
                continue
            draft_ok, draft_message, _ = create_gmail_draft(
                to_email=email,
                company_name=record.get("company") or "Target Firm",
                job_title=record.get("title") or "",
                custom_body=generate_bump_email(
                    first_name_for_greeting(record.get("name") or ""),
                    job_title=record.get("title") or "",
                    company_name=record.get("company") or "",
                ),
                # "Re:" because this genuinely follows a prior send, so it threads in the
                # recipient's inbox. A Carmen Cold PEOPLE row has no Role column, so fall back to
                # the company-only form rather than rendering "Re:  @ Atwell".
                custom_subject=(
                    f"Re: {record.get('title')} @ {record.get('company') or 'Target Firm'}"
                    if str(record.get("title") or "").strip()
                    else f"Re: {record.get('company') or 'Target Firm'}"
                )
            )
            if not draft_ok and draft_message != "Draft already exists in Gmail":
                result["skipped"] += 1
                continue
            if draft_ok:
                result["drafted"] += 1
        if enqueue_crm_payload(build_crm_payload(
            "update_snooze", sheet_uuid=record.get("sheet_uuid"), next_followup=next_followup
        )):
            result["updated"] += 1
    return result, next_followup

# ==============================================================================
# NIGHTLY FOLLOW-UP SEQUENCER
# Reads every live JOBS row via the existing get_followups path, runs the pure
# pipeline_utils.followup_action() policy on each, buries ghosted rows (the only
# automatic write), and queues everything else for the morning "needs you today"
# card. No schema change - state is derived from Status / Date Added / Next
# Followup Date only.
# ==============================================================================
SEQUENCER_SCAN_TABS = (("TC", "Tetiana Cold"), ("TW", "Tetiana Warm"), ("CL", "Clavicular"),
                       ("CC", "Carmen Cold"))

# PEOPLE-schema tabs the sequencer scans. Carmen Cold is now the active outreach cycle - every
# verified inbound reply auto-moves its contact here (route_inbound_reply_to_crm), so it receives
# the hottest leads and must sit in the same follow-up cadence as the JOBS tabs. But these rows
# are people, not job applications: followup_action() can still flag one "bury_ghosted" off a
# stale "Applied" Status carried over by the tab move, and auto-moving a live networking contact
# into the Died JOBS tab on a 16-day clock is wrong (and crosses schemas). So a would-be bury on
# a PEOPLE row is surfaced on the card as "going cold" for a human call, never written.
SEQUENCER_PEOPLE_SCHEMA_TABS = frozenset({"Carmen Cold"})

def _sequencer_already_actioned(sheet_uuid, run_date):
    """True if this row was already actioned by the sequencer earlier today (same-day idempotency
    guard - a re-run, or /queue's sibling, must not double-queue or double-bury)."""
    if not sheet_uuid:
        return False
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM followup_sequencer_log WHERE sheet_uuid = ? AND run_date = ?",
                (sheet_uuid, run_date)
            )
            return cursor.fetchone() is not None
    except Exception as e:
        logging.error(f"Sequencer log read error ({sheet_uuid}): {e}")
        return False

def _record_sequencer_action(sheet_uuid, run_date, action):
    """Mark (sheet_uuid, today) as handled so a same-day re-run skips it."""
    if not sheet_uuid:
        return
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO followup_sequencer_log (sheet_uuid, run_date, action) VALUES (?, ?, ?)",
                (sheet_uuid, run_date, action)
            )
            conn.commit()
    except Exception as e:
        logging.error(f"Sequencer log write error ({sheet_uuid}): {e}")

def _parse_fit_score(value):
    """Best-effort numeric Fit Score from the get_followups raw_priority field (Column E for JOBS)."""
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(value or "")) or 0)
    except (ValueError, TypeError):
        return 0.0

def build_followup_bump_draft(record, attempt):
    """Draft the follow-up text from the followup_bumps template bank via the existing
    resolve_template_text + interpolate_template path. No LLM, no prose authored in Python.
    Rotates the two bank entries by attempt number (#1 -> entry 0, #2 -> entry 1).

    PEOPLE-schema rows (Carmen Cold networking contacts) have no Role column, so a blank title
    routes to the roleless bump pair instead - the followup_bumps bank's "the {job_title} role at
    {company}" phrasing would otherwise render "the this role role at X".
    """
    title = str(record.get("title") or "").strip()
    idx = 0 if attempt <= 1 else 1
    if not title:
        pool = _ROLELESS_FOLLOWUP_BUMPS
        template = pool[idx] if idx < len(pool) else pool[0]
    else:
        pool = load_outreach_templates().get("followup_bumps", [])
        fallback_pool = _FALLBACK_OUTREACH_TEMPLATES["followup_bumps"]
        fallback_text = fallback_pool[idx] if idx < len(fallback_pool) else fallback_pool[0]
        template = resolve_template_text(pool, idx, fallback_text)
    return interpolate_template(
        template,
        name=record.get("name") or "",
        company=record.get("company") or "",
        job_title=title,
    )

def run_followup_sequencer(today=None, dry_run=False):
    """Scan Tetiana Cold/Warm + Clavicular via get_followups, run followup_action() on every row,
    and return a structured plan: followups_ready / going_cold / buried / top_matched / counts.

    Unless dry_run: queues a +window snooze via update_snooze for each drafted follow-up, buries
    each ghosted row (append_note '[reason: ghosted]' + update_status -> Died), and records each
    actioned row in followup_sequencer_log. dry_run=True (the /queue path) performs ZERO writes.

    Idempotent: (a) same-day - followup_sequencer_log skips a row already actioned today;
    (b) across days - a queued follow-up pushes Next Followup Date to the next window boundary
    (so followup_action()'s future gate returns "none" until then) and a bury moves the row off
    the scanned tabs entirely.

    Buries are capped at MAX_AUTO_BURIES_PER_RUN per pass (see counts["buries_suppressed"]).
    """
    if isinstance(today, datetime):
        today = today.date()
    today = today or datetime.now().date()
    run_date = today.strftime("%Y-%m-%d")

    seen_uuids = set()
    records = []
    for code, tab_name in SEQUENCER_SCAN_TABS:
        for rec in fetch_networking_cards(code, qty=None) or []:
            uuid_val = rec.get("sheet_uuid")
            if uuid_val and uuid_val in seen_uuids:
                continue
            if uuid_val:
                seen_uuids.add(uuid_val)
            records.append({**rec, "sheet_tab": tab_name})

    result = {"followups_ready": [], "going_cold": [], "buried": [], "top_matched": [], "counts": {}}
    buries_written = 0
    buries_suppressed = 0

    for rec in records:
        # Carmen Cold runs the 4/11/21 people ladder instead of the JOBS +4/+9/+16 windows: these
        # are networking contacts, so the cadence is tighter and the sequence ends quietly rather
        # than burying. Rung is read from the row's own dates, so a contact dragged in by hand
        # joins the ladder on this pass with nothing to configure.
        if rec.get("sheet_tab") in SEQUENCER_PEOPLE_SCHEMA_TABS:
            ladder_action, ladder_next = plan_carmen_followup(
                rec.get("date_added"), rec.get("next_followup"), today
            )
            if ladder_action == "none":
                continue
            sheet_uuid = rec.get("sheet_uuid")
            if ladder_action == "exhausted":
                # All three nudges sent and still nothing back. Surface it once for a human call
                # rather than burying - a networking contact is not a job application - and write
                # nothing, so the row stops appearing after Kevin acts on it.
                ladder_anchor = followup_anchor(rec.get("date_added"), rec.get("next_followup"))
                result["going_cold"].append({
                    "company": rec.get("company") or "N/A",
                    "role": rec.get("title") or "",
                    "short_id": get_short_id_by_sheet_uuid(sheet_uuid) if sheet_uuid else None,
                    "sheet_uuid": sheet_uuid,
                    "status": rec.get("status") or "",
                    "days": (today - ladder_anchor).days if ladder_anchor else None,
                })
                continue
            if ladder_action == "schedule":
                if not (dry_run or not sheet_uuid):
                    enqueue_crm_payload(build_crm_payload(
                        "update_snooze", sheet_uuid=sheet_uuid,
                        next_followup=ladder_next.strftime("%Y-%m-%d"),
                    ))
                continue

            attempt = int(ladder_action.rsplit("_", 1)[1])
            result["followups_ready"].append({
                "company": rec.get("company") or "N/A",
                "role": rec.get("title") or "",
                "short_id": get_short_id_by_sheet_uuid(sheet_uuid) if sheet_uuid else None,
                "sheet_uuid": sheet_uuid,
                "attempt": attempt,
                "draft_text": build_followup_bump_draft(rec, attempt),
                "sheet_tab": rec.get("sheet_tab"),
                "ladder_day": CARMEN_LADDER_DAYS[attempt - 1],
            })
            if dry_run or not sheet_uuid or _sequencer_already_actioned(sheet_uuid, run_date):
                continue
            if ladder_next is not None:
                enqueue_crm_payload(build_crm_payload(
                    "update_snooze", sheet_uuid=sheet_uuid,
                    next_followup=ladder_next.strftime("%Y-%m-%d"),
                ))
            _record_sequencer_action(sheet_uuid, run_date, ladder_action)
            continue

        # Retire untouched pipeline output. followup_action() returns "none" for "Matched", so
        # without this a row Kevin never engaged with sits in Tetiana Cold forever and the tab
        # grows without bound. Shares the bury cap and the same note-then-move writes.
        if is_expired_matched_row(rec.get("status"), rec.get("date_added"), today):
            sheet_uuid = rec.get("sheet_uuid")
            result["buried"].append({
                "company": rec.get("company") or "N/A",
                "role": rec.get("title") or "",
                "short_id": get_short_id_by_sheet_uuid(sheet_uuid) if sheet_uuid else None,
                "sheet_uuid": sheet_uuid,
            })
            if dry_run or not sheet_uuid or _sequencer_already_actioned(sheet_uuid, run_date):
                continue
            if buries_written >= MAX_AUTO_BURIES_PER_RUN:
                buries_suppressed += 1
                continue
            enqueue_crm_payload(build_crm_payload(
                "append_note", sheet_uuid=sheet_uuid,
                note=f"[reason: never actioned after {MATCHED_EXPIRY_DAYS}d]",
            ))
            enqueue_crm_payload(build_crm_payload("update_status", sheet_uuid=sheet_uuid, new_tab="Died"))
            _record_sequencer_action(sheet_uuid, run_date, "expire_matched")
            buries_written += 1
            continue

        action = followup_action(rec.get("status"), rec.get("date_added"), rec.get("next_followup"), today)
        if action == "none":
            continue
        sheet_uuid = rec.get("sheet_uuid")
        company = rec.get("company") or "N/A"
        role = rec.get("title") or ""
        short_id = get_short_id_by_sheet_uuid(sheet_uuid) if sheet_uuid else None
        anchor = followup_anchor(rec.get("date_added"), rec.get("next_followup"))
        days_since = (today - anchor).days if anchor else None
        already = (not dry_run) and _sequencer_already_actioned(sheet_uuid, run_date)

        if action in ("send_followup_1", "send_followup_2"):
            attempt = 1 if action == "send_followup_1" else 2
            result["followups_ready"].append({
                "company": company, "role": role, "short_id": short_id, "sheet_uuid": sheet_uuid,
                "attempt": attempt, "draft_text": build_followup_bump_draft(rec, attempt),
                "sheet_tab": rec.get("sheet_tab"),
            })
            if dry_run or already or not sheet_uuid:
                continue
            base = anchor or today
            push_days = FOLLOWUP_2_DAYS if attempt == 1 else FOLLOWUP_BURY_DAYS
            new_nf = (base + timedelta(days=push_days)).strftime("%Y-%m-%d")
            enqueue_crm_payload(build_crm_payload("update_snooze", sheet_uuid=sheet_uuid, next_followup=new_nf))
            _record_sequencer_action(sheet_uuid, run_date, action)

        elif action == "bury_ghosted":
            if rec.get("sheet_tab") in SEQUENCER_PEOPLE_SCHEMA_TABS:
                # Networking contact, not a job application - never auto-bury to Died. Report it
                # as going cold so Kevin decides; write nothing. See SEQUENCER_PEOPLE_SCHEMA_TABS.
                result["going_cold"].append({
                    "company": company, "role": role, "short_id": short_id, "sheet_uuid": sheet_uuid,
                    "status": rec.get("status") or "", "days": days_since,
                })
                continue
            result["buried"].append({
                "company": company, "role": role, "short_id": short_id, "sheet_uuid": sheet_uuid,
            })
            if dry_run or already or not sheet_uuid:
                continue
            if buries_written >= MAX_AUTO_BURIES_PER_RUN:
                # Over the safety cap: report the row on the card but write nothing. Deliberately
                # skipping _record_sequencer_action too - logging it would mark the row actioned
                # and it would never be retried, turning a deferred bury into a lost one.
                buries_suppressed += 1
                continue
            # The one automatic write: note the reason (row still in its source tab), then move to Died.
            enqueue_crm_payload(build_crm_payload("append_note", sheet_uuid=sheet_uuid, note="[reason: ghosted]"))
            enqueue_crm_payload(build_crm_payload("update_status", sheet_uuid=sheet_uuid, new_tab="Died"))
            _record_sequencer_action(sheet_uuid, run_date, action)
            buries_written += 1

        elif action == "stale_nudge":
            result["going_cold"].append({
                "company": company, "role": role, "short_id": short_id, "sheet_uuid": sheet_uuid,
                "status": rec.get("status") or "", "days": days_since,
            })

    result["going_cold"].sort(key=lambda r: r["days"] if r["days"] is not None else -1, reverse=True)

    matched = [r for r in records if status_rank(r.get("status")) == status_rank("Matched")]
    matched.sort(key=lambda r: _parse_fit_score(r.get("raw_priority")), reverse=True)
    for r in matched[:3]:
        su = r.get("sheet_uuid")
        result["top_matched"].append({
            "company": r.get("company") or "N/A", "role": r.get("title") or "",
            "short_id": get_short_id_by_sheet_uuid(su) if su else None,
            "sheet_uuid": su, "fit_score": _parse_fit_score(r.get("raw_priority")),
        })

    result["counts"] = {k: len(result[k]) for k in ("followups_ready", "going_cold", "buried", "top_matched")}
    # Not a section length like the four above: how many of result["buried"] were reported but
    # left unwritten by the cap. Never nonzero on its own (it implies buried > 0), so the card's
    # all-empty early return stays correct.
    result["counts"]["buries_suppressed"] = buries_suppressed
    return result

def _seq_id_tag(entry):
    """short_id for /replied /interview, falling back to a sheet_uuid stub, or an em dash."""
    return entry.get("short_id") or (str(entry.get("sheet_uuid") or "")[:8]) or "—"

def render_followup_needs_card(result, on_demand=False):
    """Render the single 'needs you today' Telegram message from a run_followup_sequencer() result.
    Sections are omitted when empty; an all-empty result collapses to one short line. Pure - no I/O.
    `on_demand=True` labels it as the read-only /queue preview.
    """
    counts = result.get("counts", {})
    today_str = datetime.now().strftime("%Y-%m-%d")
    if not any(counts.values()):
        tail = "queue is clear" if on_demand else "nothing needs you today"
        return f"✅ <b>{tail.capitalize()} · {today_str}</b>"

    header = "Queue Preview" if on_demand else "Needs You Today"
    note = " <i>(read-only, no writes)</i>" if on_demand else ""
    lines = [f"🗂️ <b>{header} · {today_str}</b>{note}"]

    ready = result.get("followups_ready", [])
    if ready:
        lines.append(f"\n▶ <b>Follow-ups ready ({len(ready)})</b>")
        for e in ready:
            role = html.escape(str(e.get("role") or "—"))
            company = html.escape(str(e.get("company") or "—"))
            draft = html.escape(str(e.get("draft_text") or "")[:600])
            ladder_day = e.get("ladder_day")
            attempt_tag = f"#{e.get('attempt', 1)}" + (f" · day {ladder_day}" if ladder_day else "")
            lines.append(f"💼 <b>{role}</b> — {company}  ·  {attempt_tag}  ·  🆔 <code>{html.escape(_seq_id_tag(e))}</code>")
            lines.append(f"<code>{draft}</code>")

    cold = result.get("going_cold", [])
    if cold:
        lines.append(f"\n▶ <b>Going cold ({len(cold)})</b> <i>— Replied/Screening/Interviewing, oldest first</i>")
        for e in cold:
            company = html.escape(str(e.get("company") or "—"))
            role = html.escape(str(e.get("role") or "—"))
            status = html.escape(str(e.get("status") or ""))
            days = e.get("days")
            days_str = f"{days}d untouched" if isinstance(days, int) else "stale"
            lines.append(f"• <b>{company}</b> — {role} · {status} · {days_str} · <code>{html.escape(_seq_id_tag(e))}</code>")

    buried = result.get("buried", [])
    if buried:
        lines.append(f"\n▶ <b>Buried overnight ({len(buried)})</b> <i>— auto-moved to Died as ghosted</i>")
        for e in buried:
            company = html.escape(str(e.get("company") or "—"))
            role = html.escape(str(e.get("role") or "—"))
            lines.append(f"• <b>{company}</b> — {role} · <code>{html.escape(_seq_id_tag(e))}</code>")
        suppressed = counts.get("buries_suppressed", 0)
        if suppressed:
            # The listed rows above are a mix of written and withheld - say so, or the card
            # would claim buries the CRM never received.
            lines.append(
                f"🛑 <i>{suppressed} of these were withheld by the safety cap "
                f"(max {MAX_AUTO_BURIES_PER_RUN}/run) and not written — re-run to process the rest.</i>"
            )

    top = result.get("top_matched", [])
    if top:
        lines.append("\n▶ <b>Top 3 untouched matches</b> <i>— highest Fit Score, still Matched</i>")
        for e in top:
            company = html.escape(str(e.get("company") or "—"))
            role = html.escape(str(e.get("role") or "—"))
            fit = e.get("fit_score") or 0
            lines.append(f"• {fit:g} · <b>{company}</b> — {role} · <code>{html.escape(_seq_id_tag(e))}</code>")

    summary = (
        f"\n<b>Summary:</b> {counts.get('followups_ready', 0)} follow-ups · "
        f"{counts.get('going_cold', 0)} going cold · {counts.get('buried', 0)} buried · "
        f"{counts.get('top_matched', 0)} top matches"
    )
    if counts.get("buries_suppressed", 0):
        summary += f" · {counts['buries_suppressed']} buries capped"
    lines.append(summary)
    return "\n".join(lines)

def _send_telegram_card_chunked(chat_id, text, limit=3900):
    """Send a long HTML card as newline-split chunks so it never trips Telegram's length cap."""
    chunk = ""
    for line in text.split("\n"):
        if chunk and len(chunk) + len(line) + 1 > limit:
            send_telegram_message(chat_id, chunk)
            chunk = ""
        chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        send_telegram_message(chat_id, chunk)

def scheduled_followup_sequencer_job():
    """APScheduler target: nightly follow-up sequencer pass (07:00 local, before the digest).
    Applies the automatic bury, queues follow-up drafts, and posts the single 'needs you today'
    card. This is the only morning message at this hour - the standup digest posts at 08:30.
    """
    logging.info("[SEQUENCER] Nightly follow-up sequencer cycle triggered")
    try:
        result = run_followup_sequencer()
        c = result["counts"]
        logging.info(
            f"[SEQUENCER] followups_ready={c['followups_ready']} going_cold={c['going_cold']} "
            f"buried={c['buried']} top_matched={c['top_matched']}"
        )
        if TELEGRAM_CHAT_ID:
            _send_telegram_card_chunked(TELEGRAM_CHAT_ID, render_followup_needs_card(result))
    except Exception as e:
        logging.error(f"[SEQUENCER] Nightly cycle error: {e}")
    logging.info("[SEQUENCER] Nightly follow-up sequencer cycle completed")

def start_followup_sequencer():
    """Register the nightly sequencer on the shared background scheduler (07:00 local, ahead of
    the 08:30 morning digest). Bury-to-Died is its only automatic write.
    """
    EMAIL_POLL_SCHEDULER.add_job(
        scheduled_followup_sequencer_job,
        trigger="cron",
        hour=7,
        minute=0,
        id="followup_sequencer",
        max_instances=1,
        coalesce=True,
    )
    logging.info("[SEQUENCER] Nightly follow-up sequencer scheduled: 07:00 local")

def edit_telegram_message(chat_id, message_id, text):
    """Edit an existing Telegram message in-place instead of sending a redundant new one."""
    if not (TELEGRAM_BOT_TOKEN and chat_id and message_id):
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        res = requests.post(url, json=payload, timeout=5)
        return res.status_code == 200
    except Exception as e:
        logging.error(f"editMessageText error: {e}")
        return False

def send_telegram_message(chat_id, text):
    """Send a plain-text Telegram message (no inline keyboards - pure text-based swipe-reply CLI).
    Returns the sent message's telegram_message_id, or None on failure.
    """
    if not (TELEGRAM_BOT_TOKEN and chat_id):
        return None

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        res = requests.post(url, json=payload, timeout=5)
        logging.info(f"Telegram sendMessage response status: {res.status_code} (chat_id={chat_id})")
        if res.status_code == 429:
            retry_after = res.json().get("parameters", {}).get("retry_after", 1)
            logging.warning(f"Telegram 429 Rate Limit - retrying after {retry_after}s")
            time.sleep(retry_after)
            res = requests.post(url, json=payload, timeout=5)
            logging.info(f"Telegram sendMessage retry response status: {res.status_code} (chat_id={chat_id})")
        if res.status_code == 200:
            log_metric_event("message_sent")
            return res.json().get("result", {}).get("message_id")
        else:
            logging.error(f"Telegram sendMessage failed: {res.status_code} {res.text[:200]}")
    except Exception as e:
        logging.error(f"Telegram Post Error: {e}")
    return None

# extract_salary()/extract_work_style()'s literal not-found sentinels (pipeline_utils.py). Printing
# these on the card burns the most valuable row telling Kevin nothing is known, so the metadata
# line drops a part when it equals its sentinel instead - extract_salary/extract_work_style
# themselves stay untouched since passes_strict_filter and other callers depend on the sentinels.
SALARY_UNLISTED_SENTINEL = "Salary Unlisted"
WORK_STYLE_UNSPECIFIED_SENTINEL = "On-Site / Unspecified"

def send_telegram_card(job, score, target_email, age_badge, salary_str, work_style, overlap_pct, short_id, sheet_uuid=None, alumni_line="", sheet_tab="Pipeline_Candidates", score_boost=0):
    """Send an executive-scannable job card as pure text - no inline keyboards, swipe-reply only.
    Captures the telegram_message_id and maps it to sheet_uuid for later swipe-reply resolution.

    The card is a home page, not a document: identity, the one metadata line, the target email,
    the people-search links, and one link to everything else. Fit reason, matched skills, ATS
    bullets, the LinkedIn note and the cold draft all live on /stage/<short_id>, where a real copy
    button beats tap-to-copy on a <code> span. Apply and the three decision-maker links stay inline
    - they're triage-moment actions Kevin clicks while deciding, not after, and routing them through
    a sleeping free-tier web service would cost ~50s per cold tap. The swipe legend is the bare
    command list; /help explains them.
    """
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return None
    company_raw = str(job.get("employer_name") or "N/A")
    title_raw = str(job.get("job_title") or "N/A")
    company = html.escape(company_raw)
    title = html.escape(title_raw)
    apply_link = html.escape(str(job.get("job_apply_link") or "#"), quote=True)
    stage_url = html.escape(f"{BASE_URL}/stage/{short_id}?track={job.get('track', 'a')}", quote=True)
    # Built from the RAW company/title, not the HTML-escaped ones above - escaping first and then
    # URL-encoding double-escapes ("Smith & Sons" would search for "Smith &amp; Sons"). Escaped
    # only here, for the href, with quote=True - matching apply_link's own handling.
    dork_url = html.escape(build_hiring_manager_dork(company_raw, title_raw), quote=True)
    recruiter_dork_url = html.escape(build_recruiter_dork(company_raw), quote=True)
    apollo_url = html.escape(build_apollo_url(company_raw), quote=True)
    company_posts_url = html.escape(build_linkedin_company_posts_url(company_raw), quote=True)
    # Truncate raw dynamic content BEFORE HTML-escaping/tag-wrapping so tags never get cut mid-string.
    # alumni_line arrives with a trailing newline from process_single_candidate but without one from
    # some callers, so it is normalized here rather than leaving a stray blank line on the card.
    alumni_line_safe = str(alumni_line or "")[:400].rstrip("\n")
    alumni_block = f"{alumni_line_safe}\n" if alumni_line_safe.strip() else ""
    fit_dot = get_fit_score_indicator(score)

    # Gemini's routing decisions, carried in the message itself so a deploy that wipes the jobs
    # cache degrades /draft and /e instead of blocking them. Parsed back by
    # _parse_routing_from_card_text(); keep the two in sync.
    _bullets = job.get("bullet_indices") or []
    routing_tag = "{}|{}|{}|{}".format(
        str(job.get("track") or "a").lower()[:1],
        "tech" if str(job.get("tone_mode") or "").lower() == "tech" else "conservative",
        ",".join(str(int(i)) for i in _bullets if str(i).lstrip("-").isdigit()),
        int(job.get("outreach_template_id") or 0),
    )

    # Metadata line: score (+ the boost that got it there, if any) always first, then salary and
    # work style only when actually known, then age and keyword overlap unconditionally.
    boost_suffix = f" ({score_boost:+d})" if score_boost else ""
    meta_parts = [f"<b>{score}/100</b>{boost_suffix}"]
    salary_str_val = str(salary_str)
    if salary_str_val != SALARY_UNLISTED_SENTINEL:
        meta_parts.append(html.escape(salary_str_val))
    work_style_val = str(work_style)
    if work_style_val != WORK_STYLE_UNSPECIFIED_SENTINEL:
        meta_parts.append(html.escape(work_style_val))
    meta_parts.append(html.escape(str(age_badge)))
    meta_parts.append(f"Skills {overlap_pct}%")
    meta_line = f"{fit_dot} " + " · ".join(meta_parts)

    card_text = (
        f"💼 <b>{title}</b>\n"
        f"🏢 <b>{company}</b>\n"
        f"{meta_line}\n"
        f"{alumni_block}"
        f"📧 <code>{html.escape(target_email)}</code>\n\n"
        f"🔗 <a href='{apply_link}'>Apply</a> · 🎯 <a href='{dork_url}'>Hiring Mgr</a> · "
        f"🤝 <a href='{recruiter_dork_url}'>Recruiter</a> · 🔍 <a href='{apollo_url}'>Apollo</a> · "
        f"📣 <a href='{company_posts_url}'>Co. Posts</a>\n"
        f"📋 <a href='{stage_url}'>Full Card</a> - bullets, LinkedIn note, draft, links, PDF\n"
        f"🆔 <code>{html.escape(str(sheet_uuid or ''))}</code> · <code>{html.escape(sheet_tab)}</code> · 🧭 <code>{routing_tag}</code>\n\n"
        f"⚡ <code>/apply</code> <code>/draft</code> <code>/warm</code> <code>/cold</code> "
        f"<code>/x</code> <code>/f</code> <code>/n</code> <code>/e</code> <code>/eh</code> · <code>/help</code>"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": card_text[:3990],
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 429:
            retry_after = res.json().get("parameters", {}).get("retry_after", 1)
            logging.warning(f"Telegram 429 Rate Limit (card) - retrying after {retry_after}s")
            time.sleep(retry_after)
            res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            telegram_message_id = res.json().get("result", {}).get("message_id")
            log_metric_event("message_sent", sheet_uuid)
            if telegram_message_id and sheet_uuid:
                save_message_mapping(telegram_message_id, sheet_uuid, sheet_tab, company, "", target_email)
            return telegram_message_id
    except Exception as e:
        logging.error(f"Failed to post card to Telegram: {e}")
    return None

def send_warm_radar_card(job, contact_name, contact_note, sheet_uuid):
    """Lean /w warm-radar card: no AI fit score, no fit reason, no tailored outreach copy - just
    the role, the warm contact it maps to, and Apply. Swipe-replies (/apply, /x, /n, /f) resolve
    against the Clavicular tab via the embedded 🆔 marker, exactly like a Clavicular pipeline card.
    """
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return None
    company = html.escape(str(job.get("employer_name") or "N/A"))
    title = html.escape(str(job.get("job_title") or "N/A"))
    apply_link = html.escape(str(job.get("job_apply_link") or "#"), quote=True)
    card_text = (
        f"🤝 <b>{title}</b>\n"
        f"🏢 <b>{company}</b>\n"
        f"👤 <b>Warm contact:</b> {html.escape(str(contact_name or 'Contact'))}\n"
        f"📝 <i>{html.escape(str(contact_note or 'Active relationship'))}</i>\n\n"
        f"🔗 <a href='{apply_link}'>Apply</a>\n"
        f"🆔 <code>{html.escape(str(sheet_uuid or ''))}</code> · <code>Clavicular</code>\n\n"
        f"⚡ <code>/apply</code> <code>/draft</code> <code>/warm</code> <code>/cold</code> "
        f"<code>/x</code> <code>/f</code> <code>/n</code> <code>/e</code> <code>/eh</code> · <code>/help</code>"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": card_text[:3990],
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 429:
            retry_after = res.json().get("parameters", {}).get("retry_after", 1)
            logging.warning(f"Telegram 429 Rate Limit (warm radar card) - retrying after {retry_after}s")
            time.sleep(retry_after)
            res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            telegram_message_id = res.json().get("result", {}).get("message_id")
            log_metric_event("message_sent", sheet_uuid)
            if telegram_message_id and sheet_uuid:
                save_message_mapping(telegram_message_id, sheet_uuid, "Clavicular", company, "", "")
            return telegram_message_id
    except Exception as e:
        logging.error(f"Failed to post warm radar card to Telegram: {e}")
    return None

# ==============================================================================
# 8. PARALLEL PIPELINE EXECUTION (PARALLEL JSEARCH + EARLY-EXIT CIRCUIT BREAKER)
# ==============================================================================
def _fetch_jsearch_page_with_retry(api_url, headers, params, query, page):
    """GETs one JSearch page with exponential backoff on timeout/429/5xx.
    Returns (jobs, stop_pagination) - stop_pagination is True once retries are exhausted or a
    non-retryable status is hit, so one query's failure never raises past this function.
    """
    delay = 2.0
    for attempt in range(JSEARCH_MAX_RETRIES + 1):
        try:
            logging.info(f"[JSEARCH OUTBOUND] Calling {api_url} with headers: {list(headers.keys())} for query: '{query}' page: {page}")
            with JSEARCH_SEMAPHORE:
                res = requests.get(api_url, headers=headers, params=params, timeout=JSEARCH_TIMEOUT_SECONDS)
            if res.status_code == 200:
                return res.json().get("data", []), False
            if res.status_code == 429 or res.status_code >= 500:
                if attempt == JSEARCH_MAX_RETRIES:
                    logging.error(f"JSearch {res.status_code} on page {page} ({query}) - retries exhausted")
                    return [], True
                logging.warning(f"JSearch {res.status_code} on page {page} ({query}), attempt {attempt+1}/{JSEARCH_MAX_RETRIES+1} - retrying in {delay}s")
                time.sleep(delay)
                delay *= 2.0
                continue
            logging.warning(f"JSearch {res.status_code} on page {page} ({query}) - non-retryable")
            return [], True
        except requests.exceptions.Timeout:
            logging.warning(f"JSearch timeout on page {page} ({query}) - skipping page")
            return [], True
        except Exception as e:
            logging.error(f"JSearch fetch exception on page {page} ({query}): {e}")
            return [], True
    return [], True

def fetch_single_query_jobs(query_args):
    """Worker function for parallel JSearch API query execution.
    Fetches a rolling 3-page window per query, resuming from this query's persisted query_pagination
    offset (instead of always re-fetching page 1) and wrapping back to page 1 past page 20 - so every
    /t run surfaces deeper/fresher listings instead of re-evaluating the same first page each time.
    Stops early on empty page, 429, or exhausted retries (see _fetch_jsearch_page_with_retry).
    Non-"Remote" queries are radius-limited (radius_miles filter, anchored to the location text in
    the query itself); "Remote" queries are capped to at most 1 result so nationwide remote postings
    don't crowd out the local metro-area focus.
    """
    query, api_url, headers = query_args
    is_remote_query = "remote" in query.lower()
    radius_miles = safe_int(get_filter("radius_miles"), 45)
    start_page = get_query_start_page(query)
    all_jobs = []
    for offset in range(3):
        page = start_page + offset
        params = {"query": query, "page": str(page), "num_pages": "1", "date_posted": "month"}
        if not is_remote_query and radius_miles:
            params["radius"] = str(radius_miles)
        time.sleep(0.3)  # stagger outbound requests to avoid slamming JSearch concurrently
        page_jobs, should_stop = _fetch_jsearch_page_with_retry(api_url, headers, params, query, page)
        if page_jobs:
            all_jobs.extend(page_jobs)
        if should_stop or not page_jobs:
            break  # no more results or retries exhausted, stop paging early
    if is_remote_query:
        all_jobs = all_jobs[:1]
    next_page = start_page + 3
    if next_page > 20:
        next_page = 1
    save_query_next_page(query, next_page)
    return all_jobs

def fetch_greenhouse_jobs(slug):
    """Pull unauthenticated postings from a Greenhouse job board for a company slug.

    `content=true` is REQUIRED: without it Greenhouse omits the `content` field entirely, so
    every posting arrived with job_description="" and the description-driven gates silently
    stopped working on Greenhouse jobs - passes_strict_filter's hard_ban_keywords check had
    no text to scan, and the Gemini scorer graded on title and company alone. Costs ~12x the
    payload (0.7MB -> 9MB on a large board, ~124MB peak across a full 22-slug fan-out), which
    only became affordable once the get_db_conn() connection leak was fixed and the idle
    baseline dropped from ~477MB to ~95MB.
    """
    try:
        # 30s, not the 10s the other boards use: content=true returns ~12x the bytes (a large
        # board goes 0.7MB -> 9MB), and on a 0.5-CPU instance the old timeout started dropping
        # whole boards - every ATS board timing out at once is what a 0-candidate /t run looks like.
        res = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", timeout=30)
        if res.status_code != 200:
            return []
        postings = res.json().get("jobs", [])
        jobs = []
        for p in postings:
            location = (p.get("location") or {}).get("name", "")
            raw_desc = p.get("content", "") or ""
            clean_desc = html.unescape(re.sub(r'<[^>]+>', ' ', raw_desc)).strip()
            jobs.append({
                "job_id": f"gh_{slug}_{p.get('id')}",
                "employer_name": slug.replace("-", " ").title(),
                "job_title": p.get("title", ""),
                "job_description": clean_desc,
                "job_apply_link": p.get("absolute_url", ""),
                "job_city": location,
                "job_state": "",
                "job_is_remote": "remote" in location.lower(),
                "job_posted_at_datetime_utc": p.get("updated_at", "")
            })
        return jobs
    except Exception as e:
        logging.error(f"Greenhouse Fetch Exception ({slug}): {e}")
        return []

def fetch_lever_jobs(slug):
    """Pull unauthenticated postings from a Lever job board for a company slug."""
    try:
        res = requests.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}, timeout=10)
        if res.status_code != 200:
            return []
        postings = res.json()
        jobs = []
        for p in postings:
            location = (p.get("categories") or {}).get("location", "")
            created_ms = p.get("createdAt") or 0
            posted_iso = ""
            if created_ms:
                try:
                    posted_iso = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).isoformat()
                except Exception:
                    posted_iso = ""
            jobs.append({
                "job_id": f"lever_{slug}_{p.get('id')}",
                "employer_name": slug.replace("-", " ").title(),
                "job_title": p.get("text", ""),
                "job_description": p.get("descriptionPlain") or p.get("description") or "",
                "job_apply_link": p.get("hostedUrl", ""),
                "job_city": location,
                "job_state": "",
                "job_is_remote": "remote" in str(location).lower(),
                "job_posted_at_datetime_utc": posted_iso
            })
        return jobs
    except Exception as e:
        logging.error(f"Lever Fetch Exception ({slug}): {e}")
        return []

# Workday's CXS endpoints 403 a default python-requests UA; a browser UA is required to read the
# same public board a candidate sees in their browser.
WORKDAY_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
WORKDAY_SEARCH_TERMS = ("operations analyst", "business operations", "financial analyst", "operations specialist")
WORKDAY_MAX_PER_TERM = 20
WORKDAY_DETAIL_FETCH_CAP = 12


def fetch_workday_jobs(board):
    """Pull postings from one Workday tenant's public CXS API.

    Workday is the gap Google-for-Jobs (and therefore JSearch) indexes least reliably, and it is
    where mid-size banks, credit unions and wealth-management firms post - exactly Kevin's target
    sector. Greenhouse/Lever/Ashby cover tech; this covers finance.

    `board` is a "tenant/wd-host/site" triple, e.g. "flagstar/wd1/Flagstar_Careers", because a
    Workday URL needs all three: https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}.
    A wrong site path returns HTTP 422 (the tenant exists, the board name does not), which is the
    normal failure for a guessed slug and is logged at DEBUG rather than ERROR.

    Two-stage by necessity: the /jobs list endpoint returns only title/location/postedOn and NO
    description, so the description-driven gates (hard_ban_keywords, the Gemini scorer) would see
    empty text - the same bug Greenhouse had. Each posting's detail endpoint is fetched for its
    jobDescription, capped at WORKDAY_DETAIL_FETCH_CAP per board so one large tenant cannot spend
    the whole run's request budget. Searches are server-side filtered by WORKDAY_SEARCH_TERMS so
    the cap is spent on plausible roles rather than the first 12 postings alphabetically.
    """
    parts = str(board or "").strip().split("/")
    if len(parts) != 3 or not all(parts):
        logging.error(f"Workday board '{board}' is malformed - expected 'tenant/wdN/site'")
        return []
    tenant, wd_host, site = parts
    base = f"https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": WORKDAY_USER_AGENT}

    seen_paths = {}
    for term in WORKDAY_SEARCH_TERMS:
        try:
            res = requests.post(
                f"{base}/jobs",
                json={"appliedFacets": {}, "limit": WORKDAY_MAX_PER_TERM, "offset": 0, "searchText": term},
                headers=headers,
                timeout=15,
            )
            if res.status_code == 422:
                logging.debug(f"Workday board not found (422): {board}")
                return []
            if res.status_code != 200:
                logging.error(f"Workday list error {res.status_code} for {board} (term='{term}')")
                continue
            for posting in res.json().get("jobPostings", []):
                path = posting.get("externalPath")
                if path and path not in seen_paths:
                    seen_paths[path] = posting
        except Exception as e:
            logging.error(f"Workday list exception ({board}, term='{term}'): {e}")

    jobs = []
    employer = tenant.replace("-", " ").replace("_", " ").title()
    for path, posting in list(seen_paths.items())[:WORKDAY_DETAIL_FETCH_CAP]:
        location = posting.get("locationsText", "") or ""
        description = ""
        apply_link = f"https://{tenant}.{wd_host}.myworkdayjobs.com/{site}{path}"
        posted_iso = ""
        try:
            detail_res = requests.get(f"{base}{path}", headers=headers, timeout=15)
            if detail_res.status_code == 200:
                info = detail_res.json().get("jobPostingInfo", {}) or {}
                description = strip_html_to_text(info.get("jobDescription") or "")
                apply_link = info.get("externalUrl") or apply_link
                posted_iso = info.get("startDate") or ""
                location = info.get("location") or location
        except Exception as e:
            # A failed detail fetch still yields a usable card from the list payload; it just
            # reaches the filters with no description, so log it rather than dropping the row.
            logging.error(f"Workday detail exception ({board}, {path}): {e}")
        jobs.append({
            "job_id": f"wd_{tenant}_{(posting.get('bulletFields') or [path])[0]}",
            "employer_name": employer,
            "job_title": posting.get("title", ""),
            "job_description": description,
            "job_apply_link": apply_link,
            "job_city": location,
            "job_state": "",
            "job_is_remote": "remote" in str(location).lower(),
            "job_posted_at_datetime_utc": posted_iso,
        })
    logging.info(f"Workday {board}: {len(seen_paths)} matched search terms, {len(jobs)} enriched")
    return jobs


def fetch_ashby_jobs(slug):
    """Pull unauthenticated postings from an Ashby job board for a company slug."""
    try:
        res = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=10)
        if res.status_code != 200:
            return []
        postings = res.json().get("jobs", [])
        jobs = []
        for p in postings:
            location = p.get("locationName", "")
            raw_desc = p.get("descriptionHtml") or p.get("descriptionPlain") or ""
            clean_desc = html.unescape(re.sub(r'<[^>]+>', ' ', raw_desc)).strip()
            jobs.append({
                "job_id": f"ashby_{slug}_{p.get('id')}",
                "employer_name": slug.replace("-", " ").title(),
                "job_title": p.get("title", ""),
                "job_description": clean_desc,
                "job_apply_link": p.get("jobUrl", ""),
                "job_city": location,
                "job_state": "",
                "job_is_remote": p.get("isRemote", False) or "remote" in str(location).lower(),
                "job_posted_at_datetime_utc": p.get("publishedAt", "")
            })
        return jobs
    except Exception as e:
        logging.error(f"Ashby Fetch Exception ({slug}): {e}")
        return []

# ==============================================================================
# SEARCH BREADTH GEARS
# ==============================================================================
# Breadth used to be five uncoordinated knobs: target_queries, ats_watchlist_enabled,
# remote_feeds_enabled, radius_miles and min_salary, spread across /ats, /remote and /edit. You
# could not tell what state the pipeline was in without checking all five, and the settings
# interact - radius 45 with remote on and 22 boards is not three decisions, it is one decision
# about how much noise tonight's run should carry.
#
# Deliberately a throttle, not a gearbox: unlike a car's gears these are CUMULATIVE. Every gear
# keeps the local JSearch queries and adds sources on top, because the Detroit metro results are
# always wanted - wider settings supplement them, never replace them.
#
# passes_strict_filter still applies at every gear. A higher gear widens what gets SOURCED, never
# what gets accepted, so a commission sales role is rejected in gear 5 exactly as in gear 1.
SEARCH_GEARS = {
    1: {
        "label": "Tight",
        "blurb": "JSearch metro queries only, 25 mile radius.",
        "radius_miles": 25, "ats_watchlist_enabled": False, "remote_feeds_enabled": False,
        "remote_feed_cap": 0,
    },
    2: {
        "label": "Local",
        "blurb": "Metro queries plus warm-contact ATS boards.",
        "radius_miles": 45, "ats_watchlist_enabled": False, "remote_feeds_enabled": False,
        "remote_feed_cap": 0,
    },
    3: {
        "label": "Boards",
        "blurb": "Adds the company ATS watchlist (Greenhouse/Lever/Ashby).",
        "radius_miles": 45, "ats_watchlist_enabled": True, "remote_feeds_enabled": False,
        "remote_feed_cap": 0,
    },
    4: {
        "label": "Wide",
        "blurb": "Adds keyless remote feeds, capped at 40 per run.",
        "radius_miles": 45, "ats_watchlist_enabled": True, "remote_feeds_enabled": True,
        "remote_feed_cap": 40,
    },
    5: {
        "label": "Everything",
        "blurb": "60 mile radius, remote uncapped to 100. Expect noise.",
        "radius_miles": 60, "ats_watchlist_enabled": True, "remote_feeds_enabled": True,
        "remote_feed_cap": 100,
    },
}
DEFAULT_SEARCH_GEAR = 3


def apply_search_gear(gear):
    """Write one gear's settings into the filter store. Returns the applied gear dict.

    Out-of-range gears clamp rather than raise: this is reachable from a Telegram command, and a
    typed /gear 9 should land in the widest gear instead of erroring or leaving a half-applied
    mix of settings behind.
    """
    gear_num = safe_int(gear, DEFAULT_SEARCH_GEAR)
    gear_num = max(min(gear_num, max(SEARCH_GEARS)), min(SEARCH_GEARS))
    config = SEARCH_GEARS[gear_num]
    for key in ("radius_miles", "ats_watchlist_enabled", "remote_feeds_enabled", "remote_feed_cap"):
        set_filter(key, config[key])
    set_filter("search_gear", gear_num)
    logging.info(f"[GEAR] Search breadth set to {gear_num} ({config['label']})")
    return config


def current_search_gear():
    """The gear the filters are actually in, not merely the last one set.

    A later /ats off or /remote on edits one setting without touching search_gear, so the stored
    number can disagree with reality. Comparing the live settings against each gear's definition
    keeps /gear honest about that instead of reporting a gear the pipeline is no longer in.
    """
    live = (
        safe_int(get_filter("radius_miles"), 45),
        bool(get_filter("ats_watchlist_enabled")),
        bool(get_filter("remote_feeds_enabled")),
    )
    for num, config in sorted(SEARCH_GEARS.items()):
        if live == (config["radius_miles"], config["ats_watchlist_enabled"], config["remote_feeds_enabled"]):
            return num, config
    return None, None


def describe_search_gear():
    """Telegram-ready summary of the current breadth, including the live source counts."""
    gear_num, config = current_search_gear()
    slug_count = len(safe_list(get_filter("ats_company_slugs", [])))
    query_count = len(safe_list(get_filter("target_queries", [])))
    lines = []
    for num, cfg in sorted(SEARCH_GEARS.items()):
        marker = "▶️" if num == gear_num else "　"
        lines.append(f"{marker} <b>{num}. {cfg['label']}</b> - {cfg['blurb']}")
    if gear_num:
        header = f"⚙️ <b>Search breadth: gear {gear_num} ({config['label']})</b>"
    else:
        header = "⚙️ <b>Search breadth: custom</b> (settings don't match a gear)"
    sources = [f"JSearch queries: {query_count}"]
    sources.append(f"Warm-contact ATS boards: always on")
    sources.append(f"Watchlist boards: {slug_count} ({'ON' if get_filter('ats_watchlist_enabled') else 'off'})")
    cap = safe_int(get_filter("remote_feed_cap"), 0)
    sources.append(f"Remote feeds: {'ON, cap ' + str(cap) if get_filter('remote_feeds_enabled') else 'off'}")
    sources.append(f"Radius: {safe_int(get_filter('radius_miles'), 45)} mi")
    return (
        f"{header}\n\n" + "\n".join(lines) +
        "\n\n<b>Sourcing now</b>\n" + "\n".join(f"· {s}" for s in sources) +
        "\n\nSet with /gear 1-5. Filters still reject bad roles at every gear."
    )


def _strip_html_to_text(raw):
    """Board descriptions arrive as HTML. The AI prompt and the CRM both want plain text."""
    return html.unescape(re.sub(r'<[^>]+>', ' ', str(raw or ''))).strip()


# Keyless public remote-job feeds. These need no API key, no account and no scraping - each one
# publishes JSON or RSS openly. They are REMOTE-ONLY by nature, so passes_strict_filter's city
# gate would reject every posting; _add_candidate is fed these only when the remote path is
# allowed, and each job is stamped job_is_remote=True so the existing remote handling applies.
REMOTE_FEED_TIMEOUT = 12
_REMOTE_FEED_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-outreach-engine)"}


def fetch_remoteok_jobs(limit=100):
    """RemoteOK public JSON. Element 0 is a legal/metadata stub, not a posting - skip it."""
    try:
        res = requests.get("https://remoteok.com/api", timeout=REMOTE_FEED_TIMEOUT, headers=_REMOTE_FEED_HEADERS)
        if res.status_code != 200:
            return []
        postings = res.json()
        jobs = []
        for p in postings[1:limit + 1]:
            if not isinstance(p, dict) or not p.get("position"):
                continue
            jobs.append({
                "job_id": f"remoteok_{p.get('id')}",
                "employer_name": p.get("company", ""),
                "job_title": p.get("position", ""),
                "job_description": _strip_html_to_text(p.get("description")),
                "job_apply_link": p.get("apply_url") or p.get("url", ""),
                "job_city": "Remote",
                "job_state": "",
                "job_is_remote": True,
                "job_posted_at_datetime_utc": p.get("date", ""),
            })
        return jobs
    except Exception as e:
        logging.error(f"RemoteOK Fetch Exception: {e}")
        return []


def fetch_himalayas_jobs(limit=100):
    """Himalayas public JSON. pubDate is a unix epoch string, not ISO."""
    try:
        res = requests.get("https://himalayas.app/jobs/api", params={"limit": limit},
                           timeout=REMOTE_FEED_TIMEOUT, headers=_REMOTE_FEED_HEADERS)
        if res.status_code != 200:
            return []
        jobs = []
        for p in res.json().get("jobs", []):
            posted = ""
            try:
                posted = datetime.fromtimestamp(int(p.get("pubDate") or 0), tz=timezone.utc).isoformat()
            except Exception:
                posted = ""
            jobs.append({
                "job_id": f"himalayas_{p.get('guid')}",
                "employer_name": p.get("companyName", ""),
                "job_title": p.get("title", ""),
                "job_description": _strip_html_to_text(p.get("description") or p.get("excerpt")),
                "job_apply_link": p.get("applicationLink", ""),
                "job_city": "Remote",
                "job_state": "",
                "job_is_remote": True,
                "job_posted_at_datetime_utc": posted,
            })
        return jobs
    except Exception as e:
        logging.error(f"Himalayas Fetch Exception: {e}")
        return []


def fetch_remotive_jobs(limit=100):
    """Remotive public JSON."""
    try:
        res = requests.get("https://remotive.com/api/remote-jobs", params={"limit": limit},
                           timeout=REMOTE_FEED_TIMEOUT, headers=_REMOTE_FEED_HEADERS)
        if res.status_code != 200:
            return []
        jobs = []
        for p in res.json().get("jobs", []):
            jobs.append({
                "job_id": f"remotive_{p.get('id')}",
                "employer_name": p.get("company_name", ""),
                "job_title": p.get("title", ""),
                "job_description": _strip_html_to_text(p.get("description")),
                "job_apply_link": p.get("url", ""),
                "job_city": "Remote",
                "job_state": "",
                "job_is_remote": True,
                "job_posted_at_datetime_utc": p.get("publication_date", ""),
            })
        return jobs
    except Exception as e:
        logging.error(f"Remotive Fetch Exception: {e}")
        return []


# WeWorkRemotely publishes one RSS feed per category. These are the categories that overlap the
# operations/finance/data work this pipeline targets; the programming feeds are deliberately
# omitted since passes_strict_filter would drop almost all of them anyway.
WWR_FEEDS = (
    "https://weworkremotely.com/categories/remote-business-exec-management-jobs.rss",
    "https://weworkremotely.com/categories/remote-customer-support-jobs.rss",
    "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
)


def fetch_weworkremotely_jobs(limit=100):
    """WeWorkRemotely RSS. Titles arrive as 'Company: Role', so the employer is split off the
    title rather than carried in its own element."""
    jobs = []
    for feed_url in WWR_FEEDS:
        try:
            res = requests.get(feed_url, timeout=REMOTE_FEED_TIMEOUT, headers=_REMOTE_FEED_HEADERS)
            if res.status_code != 200:
                continue
            root = ElementTree.fromstring(res.content)
            for item in root.findall(".//item")[:limit]:
                def _text(tag):
                    node = item.find(tag)
                    return (node.text or "").strip() if node is not None else ""
                raw_title = _text("title")
                company, _, role = raw_title.partition(":")
                if not role:
                    company, role = "", raw_title
                jobs.append({
                    "job_id": f"wwr_{_text('guid') or raw_title}",
                    "employer_name": company.strip(),
                    "job_title": role.strip(),
                    "job_description": _strip_html_to_text(_text("description")),
                    "job_apply_link": _text("link"),
                    "job_city": "Remote",
                    "job_state": "",
                    "job_is_remote": True,
                    "job_posted_at_datetime_utc": _text("pubDate"),
                })
        except Exception as e:
            logging.error(f"WeWorkRemotely Fetch Exception ({feed_url}): {e}")
    return jobs


def fetch_remote_feed_jobs(limit=100):
    """Pull every keyless remote feed in parallel. One dead feed never blocks the others."""
    fetchers = (fetch_remoteok_jobs, fetch_himalayas_jobs, fetch_remotive_jobs, fetch_weworkremotely_jobs)
    all_jobs = []
    with ThreadPoolExecutor(max_workers=len(fetchers)) as executor:
        futures = [executor.submit(fn, limit) for fn in fetchers]
        for future in futures:
            try:
                all_jobs.extend(future.result(timeout=REMOTE_FEED_TIMEOUT + 8))
            except Exception as e:
                logging.error(f"Remote Feed Future Error: {e}")
    return all_jobs


def fetch_ats_jobs(company_slugs, include_workday=False):
    """Pull unauthenticated Greenhouse + Lever + Ashby postings for a list of company slugs, in
    parallel; with include_workday=True, also every board in the `workday_boards` filter.

    Workday is OPT-IN per caller, not per slug, because it is scoped differently from the other
    three: a Workday address needs a tenant/host/site triple ("flagstar/wd1/Flagstar_Careers")
    rather than a single company slug, so auto_expand_ats_slug() cannot discover one and the list
    is curated by hand. Those hand-picked boards are national employers, which makes them Stage 1c
    watchlist material - NOT Stage 1b, which is deliberately scoped to Kevin's Carmen Warm network
    only. Defaulting to False keeps the warm stage warm: sourcing national boards there would
    contaminate the one stage whose whole purpose is that every posting traces to a real contact.
    """
    workday_boards = (
        [str(b).strip() for b in safe_list(get_filter("workday_boards", [])) if str(b).strip()]
        if include_workday else []
    )
    if not company_slugs and not workday_boards:
        return []
    all_jobs = []
    worker_count = min(len(company_slugs) * 3 + len(workday_boards), 18) or 1
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = []
        for slug in company_slugs:
            futures.append(executor.submit(fetch_greenhouse_jobs, slug))
            futures.append(executor.submit(fetch_lever_jobs, slug))
            futures.append(executor.submit(fetch_ashby_jobs, slug))
        # Workday is two-stage (list + a detail fetch per posting), so it needs a longer timeout
        # than the single-request boards - collected separately rather than raising the shared one.
        workday_futures = [executor.submit(fetch_workday_jobs, board) for board in workday_boards]
        for future in futures:
            try:
                # Must exceed the slowest single-board request (Greenhouse at 30s) or a board that
                # is merely slow gets counted as a failure while its thread keeps running.
                all_jobs.extend(future.result(timeout=45))
            except Exception as e:
                logging.error(f"ATS Fetch Future Error: {e}")
        for future in workday_futures:
            try:
                all_jobs.extend(future.result(timeout=90))
            except Exception as e:
                logging.error(f"Workday Fetch Future Error: {e}")
    return all_jobs

def auto_expand_ats_slug(company_name):
    """Silent Auto-ATS Expansion: best-effort guess of a company's Greenhouse/Lever/Ashby board slug from its
    name; if any board actually resolves, appends the slug to the ats_company_slugs filter so future
    /t runs source directly from it. Meant to run on a background daemon thread - silent on no match.
    Distinct from expand_ecosystem_filter() (the Gemini-powered /ecosystem add command), which also
    discovers keyword aliases and returns a Telegram report string - this one is fire-and-forget.

    Names that are obviously people or notes rather than companies are skipped before any HTTP
    request: resolve_warm_company_ats_slugs() feeds this every unique Carmen Warm "company",
    and that column holds personal contacts. See pipeline_utils.is_probable_company_name().
    """
    if not is_probable_company_name(company_name):
        # DEBUG, not INFO: on a warm network of personal contacts this is the common case, and
        # at INFO it would just replace the probe spam it exists to prevent.
        logging.debug(f"[ATS EXPANSION] Skipped '{company_name}' - not a probable company name, no board probe")
        return
    slug_guess = ats_slug_guess(company_name)
    existing_slugs = safe_list(get_filter("ats_company_slugs", []))
    if slug_guess in existing_slugs:
        return  # already tracked
    board_checks = (
        ("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{slug_guess}/jobs"),
        ("lever", f"https://api.lever.co/v0/postings/{slug_guess}?mode=json"),
        ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{slug_guess}"),
    )
    for board_name, url in board_checks:
        try:
            res = requests.get(url, timeout=8)
            if res.status_code == 200 and res.json():
                existing_slugs.append(slug_guess)
                set_filter("ats_company_slugs", existing_slugs)
                upsert_company_identity(company_name, ats_slug=slug_guess)
                logging.info(f"[ATS EXPANSION] '{company_name}' resolved to '{slug_guess}' on {board_name} - added to ats_company_slugs")
                return
        except Exception as e:
            logging.error(f"[ATS EXPANSION] {board_name} check failed for '{slug_guess}': {e}")
    # DEBUG: a miss is the normal outcome of a speculative slug guess, so this at INFO was
    # one log line per company per pipeline run for no signal. The match above stays at INFO.
    logging.debug(f"[ATS EXPANSION] No ATS board match found for '{company_name}' (guessed slug '{slug_guess}')")

def resolve_warm_company_ats_slugs():
    """Resolves an ATS board slug for every unique Carmen Warm CRM company: prefers the
    already-verified company_identities.ats_slug, else synchronously probes Greenhouse/Lever/Ashby
    via auto_expand_ats_slug(). Stage 1b sources ONLY from this warm-network list, never the
    untracked general ats_company_slugs enterprise board filter.
    """
    warm_companies = {c.get("raw_company") for c in get_warm_crm_contacts().values() if c.get("raw_company")}
    slugs = []
    for company in warm_companies:
        normalized = normalize_company_for_match(company)
        try:
            with get_db_conn() as conn:
                row = conn.execute("SELECT ats_slug FROM company_identities WHERE normalized_name = ?", (normalized,)).fetchone()
        except Exception as e:
            logging.error(f"Warm Company Identity Lookup Error ({company}): {e}")
            row = None
        if row and row[0]:
            slugs.append(row[0])
            continue
        auto_expand_ats_slug(company)  # synchronous probe + company_identities upsert on success
        try:
            with get_db_conn() as conn:
                row = conn.execute("SELECT ats_slug FROM company_identities WHERE normalized_name = ?", (normalized,)).fetchone()
        except Exception as e:
            logging.error(f"Warm Company Identity Re-Lookup Error ({company}): {e}")
            row = None
        if row and row[0]:
            slugs.append(row[0])
    return list(dict.fromkeys(slugs))  # de-dup while preserving discovery order

def dispatch_tier1_matches(matches, note_prefix="Matched via Pipeline"):
    """Land a set of scored matches in the CRM, then card the ones whose row actually wrote.

    This is the single Tier-1 delivery path, shared by run_job_pipeline() and the manual-ingest
    entrypoints (/job and POST /ingest). Extracted rather than duplicated because the ordering is
    load-bearing: the CRM write happens FIRST and a card is withheld when its write failed, so a
    card can never point at a sheet_uuid with no row behind it - which would leave /apply, /n, /f
    and the follow-up sequencer resolving against nothing.

    Rows route by the Clavicular flag (CL vs TC), matching what the caller already computed in
    process_single_candidate. `note_prefix` distinguishes a hand-pasted row from a sourced one in
    the sheet's note column; the Clavicular warm-referral note overrides it as before.

    Returns the number of cards dispatched.
    """
    if not matches:
        return 0

    today_str = datetime.now().strftime("%Y-%m-%d")
    followup_date = (datetime.now() + timedelta(days=calculate_followup_interval(5))).strftime("%Y-%m-%d")

    clavicular_rows = []
    standard_rows = []
    for item in matches:
        job = item["job"]
        is_clavicular = item.get("is_clavicular", False)
        note = (
            f"Warm Referral Matched: {item.get('contact_name', 'Contact')} | {item['reason']} | Tone: {item.get('tone_mode', 'conservative')}"
            if is_clavicular else f"{note_prefix} | {item['reason']} | Tone: {item.get('tone_mode', 'conservative')}"
        )
        row = {
            "sheet_uuid": item.get("sheet_uuid"),
            "row_data": [
                today_str,
                job.get("employer_name"),
                job.get("job_title"),
                item["target_email"],
                item["score"],
                "Matched",
                followup_date,
                job.get("job_apply_link", ""),
                note
            ]
        }
        (clavicular_rows if is_clavicular else standard_rows).append(row)

    written = {
        True: log_to_sheets_crm(build_crm_payload("batch_add_rows", target_code="CL", rows=clavicular_rows)) if clavicular_rows else True,
        False: log_to_sheets_crm(build_crm_payload("batch_add_rows", target_code="TC", rows=standard_rows)) if standard_rows else True,
    }
    for is_clavicular, ok in written.items():
        if not ok:
            withheld = [
                f"{m['job'].get('employer_name')} - {m['job'].get('job_title')} ({m['score']})"
                for m in matches if m.get("is_clavicular", False) == is_clavicular
            ]
            tab = "Clavicular" if is_clavicular else "Tetiana Cold"
            logging.error(f"Tier-1 CRM write to {tab} FAILED; withholding {len(withheld)} card(s): {withheld}")
            send_health_alert(
                f"Tier-1 CRM write to {tab} failed - {len(withheld)} card(s) withheld, rows NOT in the sheet: "
                + "; ".join(withheld)
            )

    cards_sent = 0
    for item in matches:
        job = item["job"]
        is_clavicular = item.get("is_clavicular", False)
        if not written[is_clavicular]:
            continue
        send_telegram_card(
            job, item["score"], item["target_email"],
            item["age_badge"], item["salary_str"], item["work_style"],
            item["overlap_pct"], item["short_id"],
            sheet_uuid=item.get("sheet_uuid"),
            alumni_line=item.get("alumni_line", ""),
            sheet_tab="Clavicular" if is_clavicular else "Pipeline_Candidates",
            score_boost=item.get("score_boost", 0)
        )
        cards_sent += 1
        time.sleep(1.1)

    return cards_sent

# LinkedIn's guest job endpoint: the one public surface that renders a posting without a session.
# The logged-in /jobs/view/ page returns an auth wall to any server-side fetch, so a bare GET on
# the URL Kevin copies out of his address bar yields nothing usable.
_LINKEDIN_GUEST_JOB_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
_JOB_SCRAPE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


def scrape_job_page(url, timeout=12):
    """Best-effort server-side fetch of ANY job posting page -> (title, company, description).

    Not LinkedIn-specific. The parser keys on the schema.org JobPosting JSON-LD block that nearly
    every ATS and corporate careers site emits (Workday, iCIMS, Greenhouse, Phenom, and the
    employer-hosted pages LinkedIn's "Apply" button forwards to), falling back to page markup and
    the <title> tag. An employer careers page is usually a BETTER source than LinkedIn: it answers
    a plain GET with the full description, where LinkedIn serves an auth wall.

    LinkedIn is the one host needing special handling - its posting id is rewritten to the public
    jobs-guest endpoint first, since the canonical /jobs/view/ URL is the one that gets walled.

    Returns ("", "", "") when nothing usable comes back, which is a NORMAL outcome (auth wall, JS-
    only page, bot challenge) - callers must treat it as "ask Kevin to type it", never as an error.
    """
    attempts = []
    job_id = None
    try:
        from pipeline_utils import extract_linkedin_job_id
        if is_linkedin_job_url(url):
            job_id = extract_linkedin_job_id(url)
    except Exception:
        pass

    if job_id:
        attempts.append(_LINKEDIN_GUEST_JOB_URL.format(job_id=job_id))
        attempts.append(canonical_linkedin_job_url(url))
    elif url:
        attempts.append(str(url))

    for attempt_url in attempts:
        try:
            res = requests.get(attempt_url, headers=_JOB_SCRAPE_HEADERS, timeout=timeout, allow_redirects=True)
        except Exception as e:
            logging.warning(f"[INGEST] Fetch failed for {attempt_url}: {e}")
            continue
        if res.status_code != 200 or not res.text:
            logging.warning(f"[INGEST] HTTP {res.status_code} for {attempt_url}")
            continue
        title, company, description = parse_job_page_html(res.text)
        if title or company:
            logging.info(f"[INGEST] Scraped '{title}' @ '{company}' ({len(description)} desc chars) from {attempt_url}")
            return (title, company, description)

    logging.info(f"[INGEST] No usable content scraped from {url} - auth wall, JS-only page, or unsupported layout")
    return ("", "", "")


def ingest_manual_job(url="", title="", company="", description="", chat_id=None, source_label="/job"):
    """Run one hand-picked posting through the exact Stage 2 path /t uses, then land it in the CRM.

    This is the shared core behind the Telegram /job command and the desktop bookmarklet's
    POST /ingest. It deliberately reuses process_single_candidate() + dispatch_tier1_matches()
    rather than reimplementing either, so a pasted job gets the same Gemini scoring, deterministic
    bullet/template routing, JIT Hope-alumni lookup (which auto-creates the Carmen Warm contact),
    warm/Clavicular routing and Tetiana Cold row that a pipeline-sourced one does.

    One deliberate divergence from run_job_pipeline: there is NO score>=80 Tier-1 gate. Kevin
    already vetted this posting by choosing to paste it, so the score is reported on the card but
    never used to discard the row. A pasted job that scores 61 still lands in Tetiana Cold; the
    gate exists to triage hundreds of machine-sourced listings, which is not this.

    Returns (ok, message) for the caller to relay.
    """
    # Scrape ANY posting URL, not just LinkedIn: employer careers pages (the destination behind
    # LinkedIn's own Apply button) answer a plain GET with a full schema.org JobPosting block,
    # so they are the better source whenever Kevin has that link.
    scraped_title, scraped_company, scraped_desc = ("", "", "")
    if url and not (title and company):
        scraped_title, scraped_company, scraped_desc = scrape_job_page(url)

    final_title = (title or scraped_title or "").strip()
    final_company = (company or scraped_company or "").strip()
    final_desc = (description or scraped_desc or "").strip()

    if not (final_title and final_company):
        blocked_host = "LinkedIn" if is_linkedin_job_url(url) else "That page"
        return (False, (
            f"🔒 <b>{blocked_host} didn't return a readable posting.</b> Usually an auth wall or a "
            "JavaScript-only page.\n\n"
            "Send it with the title and company spelled out instead:\n"
            f"<code>/job Foreign Exchange Ops Analyst 2 @ Huntington National Bank {html.escape(str(url or ''))}</code>"
        ))

    job = build_ingest_job_dict(final_title, final_company, final_desc, url)

    # Dedup against everything /t and previous pastes have already surfaced, so re-pasting a link
    # you already ingested doesn't create a second Tetiana Cold row for the same posting.
    job_hash = generate_dedup_hash(job["employer_name"], job["job_title"])
    if is_job_seen_db(job_hash):
        return (False, (
            f"♻️ <b>Already in the pipeline:</b> {html.escape(final_title)} @ {html.escape(final_company)}.\n"
            "This posting was surfaced before, so no duplicate row was written."
        ))

    log_metric_event("listing_discovered", source="manual_ingest")
    result = process_single_candidate(job)
    if not result:
        # AI screening rejected it. The row is still Kevin's call, but process_single_candidate
        # returns nothing to write - no score, no sheet_uuid, no resolved copy - so there is no
        # row to land. Report the rejection rather than fabricating a partial record.
        save_seen_job_db(job_hash)
        return (False, (
            f"⚠️ <b>Did not pass AI screening:</b> {html.escape(final_title)} @ {html.escape(final_company)}.\n"
            "No row written. If you still want it tracked, add it with "
            f"<code>/quick</code> or re-check the posting text."
        ))

    save_seen_job_db(job_hash)
    cards = dispatch_tier1_matches([result], note_prefix=f"Manually ingested via {source_label}")
    if not cards:
        return (False, (
            f"❌ <b>CRM write failed</b> for {html.escape(final_title)} @ {html.escape(final_company)}. "
            "The card was withheld so it can't point at a missing row - check /health and retry."
        ))

    # Pull the company onto the ATS watchlist so /t and /w source it directly from here on.
    threading.Thread(target=auto_expand_ats_slug, args=(final_company,), daemon=True).start()
    return (True, "")


def run_job_pipeline(chat_id=None, top_n=2):
    """Job search pipeline with two-stage architecture:
    Stage 1: Pre-filter candidates (JSearch multi-page + ATS direct-source, strict filters)
    Stage 2: Concurrent Gemini AI evaluation (uncapped, ThreadPoolExecutor max_workers=20)
    Tiered delivery: Tier-1 (score>=80, top 5) get full interactive cards; Tier-2 (65-79) get a bundled digest.
    """
    logging.info(">>> Starting Job Search Pipeline...")
    # Pre-warm CRM caches synchronously so parallel Stage 2 evaluations never contend for the
    # Google Apps Script lock on their first cache-miss call.
    get_applied_crm_companies()
    get_warm_crm_contacts()

    # 100-Query Rolling Master Engine: scan a fresh 10-query slice each run instead of all 100 at
    # once, then atomically advance query_bank_pointer so the next /t run resumes at the next slice.
    target_queries = safe_list(get_filter("target_queries", []))
    query_bank_pointer = safe_int(get_filter("query_bank_pointer"), 0)
    if target_queries:
        query_bank_pointer = query_bank_pointer % len(target_queries)
        active_queries = [target_queries[(query_bank_pointer + i) % len(target_queries)] for i in range(10)]
        slice_num = (query_bank_pointer // 10) + 1
        new_pointer = (query_bank_pointer + 10) % len(target_queries)
        try:
            with get_db_conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT OR REPLACE INTO search_filters (key, value_json) VALUES ('query_bank_pointer', ?)", (json.dumps(new_pointer),))
                conn.commit()
        except Exception as e:
            logging.error(f"Query Bank Pointer Persist Error: {e}")
    else:
        active_queries = []
        slice_num = 1

    slice_status = f"Stage 1: Scanning Slice {slice_num}/10 (Queries {query_bank_pointer+1}-{query_bank_pointer+10})..."
    logging.info(slice_status)
    if chat_id:
        send_status_update(chat_id, slice_status)

    seen_hashes = set()
    candidate_pool = []
    raw_discovered_count = 0
    funnel = FunnelTrace()

    def _add_candidate(job):
        nonlocal raw_discovered_count
        raw_discovered_count += 1
        funnel.raw += 1
        company = job.get("employer_name") or ""
        title = job.get("job_title") or ""
        job_hash = generate_dedup_hash(company, title)
        if job_hash in seen_hashes or is_job_seen_db(job_hash):
            funnel.note("dedup_title")
            return
        seen_hashes.add(job_hash)

        # Fuzzy content dedup: catches identical postings cross-posted under reworded titles.
        # "" means the description was missing or too short to identify a job - never dedup on
        # that, or the first description-less posting buries every later one (see
        # compute_description_simhash).
        content_hash = compute_description_simhash(job.get("job_description"))
        if content_hash and is_content_seen(content_hash):
            funnel.note("dedup_content")
            return

        log_metric_event("listing_discovered", source=derive_job_source(job.get("job_id")))
        if not passes_strict_filter(job, trace=funnel):
            # Deliberately NOT marked seen. A rejected job is not a job Kevin has considered - it
            # failed today's filters, in today's posted state. Recording it here is what buried
            # hundreds of roles: a posting rejected once for a missing salary or a city not yet on
            # the allowlist could never be reconsidered, even after the filters changed or the
            # employer reposted it with better data. Only jobs that actually reach the candidate
            # pool are remembered, so the ledger means "seen and judged", not "glanced at once".
            return

        save_seen_job_db(job_hash)
        if content_hash:
            save_content_hash(content_hash)
        funnel.passed += 1
        candidate_pool.append(job)
    
    # Stage 1: Parallel JSearch fetching (rolling 10-query slice) + strict filtering
    headers, api_url = build_jsearch_request_config()
    
    query_tasks = [(q, api_url, headers) for q in active_queries]
    
    with ThreadPoolExecutor(max_workers=min(len(active_queries), 8) or 4) as executor:
        query_results = executor.map(fetch_single_query_jobs, query_tasks)
        for jobs in query_results:
            for job in jobs:
                _add_candidate(job)

    # Stage 1b: ATS direct-source expansion strictly scoped to Carmen Warm network companies.
    warm_ats_slugs = resolve_warm_company_ats_slugs()
    if warm_ats_slugs:
        if chat_id:
            send_status_update(chat_id, f"Stage 1b: Sourcing direct ATS postings from {len(warm_ats_slugs)} Carmen Warm companies...")
        for job in fetch_ats_jobs(warm_ats_slugs):
            _add_candidate(job)

    # Stage 1c: the general enterprise watchlist (ats_company_slugs), sourced straight from
    # Greenhouse/Lever/Ashby with no API key and no aggregator in between. This is off by default
    # and gated on the ats_watchlist_enabled filter, because these boards are national - a single
    # large employer can return 1,700+ postings and drown the metro-area JSearch queries this
    # pipeline is tuned around. passes_strict_filter() still applies the city/radius/salary gates
    # inside _add_candidate, so what survives is genuinely local, but the raw volume is the reason
    # this is opt-in rather than always on. Enable with /ats on.
    if get_filter("ats_watchlist_enabled"):
        watchlist = safe_list(get_filter("ats_company_slugs", []))
        # Warm slugs already ran in 1b; re-fetching them here would double the HTTP calls.
        watchlist = [s for s in watchlist if s not in set(warm_ats_slugs or [])]
        workday_count = len(safe_list(get_filter("workday_boards", [])))
        if watchlist or workday_count:
            if chat_id:
                workday_note = f" + {workday_count} Workday board(s)" if workday_count else ""
                send_status_update(chat_id, f"Stage 1c: Sourcing direct ATS postings from {len(watchlist)} watchlist companies{workday_note}...")
            # Workday rides with the watchlist, never with Stage 1b: both are national boards
            # behind the same /gear opt-in, so /ats off silences them together.
            for job in fetch_ats_jobs(watchlist, include_workday=True):
                _add_candidate(job)

    # Stage 1d: keyless public remote feeds (RemoteOK, Himalayas, Remotive, WeWorkRemotely).
    # These are remote-only, so passes_strict_filter's valid_cities gate rejects all of them - that
    # allowlist is a Detroit suburb list and "Remote" is in none of it. Rather than weaken the
    # metro geofence every local posting depends on, remote jobs bypass the city check explicitly
    # here via _passes_remote_filter, which applies every OTHER gate (salary, seniority, title and
    # company exclusions, hard bans, cooldown, already-applied). Off by default: this pipeline is
    # tuned for a Detroit desk, and nationwide remote listings crowd that out - which is why the
    # JSearch "Remote" query is already capped at 1 result per run.
    if get_filter("remote_feeds_enabled"):
        if chat_id:
            send_status_update(chat_id, "Stage 1d: Sourcing keyless remote feeds (RemoteOK, Himalayas, Remotive, WWR)...")
        remote_cap = safe_int(get_filter("remote_feed_cap"), 40)
        remote_added = 0
        for job in fetch_remote_feed_jobs(100):
            if remote_added >= remote_cap:
                break
            if _passes_remote_filter(job):
                _add_candidate(job)
                remote_added += 1


    logging.info(f"Stage 1 Complete: {raw_discovered_count} raw listings pulled, {len(candidate_pool)} candidates passed strict filter.")
    if raw_discovered_count == 0:
        send_health_alert(
            "JSearch/ATS sourcing returned 0 raw listings this run. Check RAPIDAPI_KEY/OPENWEBNINJA_KEY "
            "validity and the target_queries filter - this usually means the API key expired or every "
            "query is misconfigured, and it will silently produce zero candidates every run until fixed."
        )
    funnel_line = funnel.summary_line()
    if funnel_line:
        logging.info(f"[FUNNEL] {funnel_line}")
    if chat_id:
        # The funnel breakdown rides along with the ingest count so a 0-candidate run explains
        # itself in Telegram instead of requiring a log dig.
        funnel_block = f"🔎 <b>Dropped:</b> {html.escape(funnel_line)}\n" if funnel_line else ""
        send_status_update(
            chat_id,
            f"📊 <b>Batch Ingested:</b> {raw_discovered_count} raw listings pulled.\n"
            f"🎯 <b>Filtered:</b> {len(candidate_pool)} passed strict criteria.\n"
            f"{funnel_block}"
            f"🧠 <b>Stage 2:</b> Running Gemini AI scoring & Hope Alumni cross-referencing..."
        )
    
    # Stage 2: Evaluate ALL strict-filtered candidates concurrently (uncapped Tier-1 capacity)
    eval_candidates = candidate_pool
    logging.info(f"Stage 2: Evaluating {len(eval_candidates)} candidates with Gemini AI (uncapped)...")
    
    top_matches = []
    with ThreadPoolExecutor(max_workers=8) as eval_executor:
        # Map candidate evaluation across thread pool
        eval_futures = [eval_executor.submit(process_single_candidate, candidate) for candidate in eval_candidates]
        
        for future in eval_futures:
            try:
                result = future.result(timeout=45)  # accommodate 3 Gemini attempts at 12s + backoff without outer preemption
                if result:
                    top_matches.append(result)
            except Exception as e:
                logging.error(f"Candidate evaluation failed (timeout or error): {e}")
                # On timeout/error: score=0, status='Evaluation Pending' is handled in evaluate_job_with_gemini
    
    # Sort by score descending, then split into Tier-1 (cards + CRM) and Tier-2 (digest only, capped at 5)
    top_matches.sort(key=lambda x: x["score"], reverse=True)
    tier1_matches = [m for m in top_matches if m["score"] >= 80][:5]
    tier2_matches = [m for m in top_matches if 65 <= m["score"] < 80][:5]

    # Write Tier-1 rows to CRM first, routed by Clavicular flag, then dispatch cards only for rows
    # that landed. Shared with the manual-ingest path - see dispatch_tier1_matches().
    cards_sent = dispatch_tier1_matches(tier1_matches)

    # Dispatch Tier-2 as leaderboard digest ONLY (do NOT add to batch_rows/CRM)
    if tier2_matches:
        digest_lines = []
        for item in tier2_matches:
            job = item["job"]
            comp = str(job.get("employer_name") or "N/A")[:28]
            title = str(job.get("job_title") or "N/A")[:40]
            digest_lines.append(f"{item['score']:>3}  {comp} - {title}")

        digest_ascii = "\n".join(digest_lines)
        digest_msg = (
            f"📋 <b>Secondary Match Leaderboard (Top {len(tier2_matches)} roles, score 65-79)</b>\n"
            f"<pre>{html.escape(digest_ascii)}</pre>"
        )
        send_telegram_message(TELEGRAM_CHAT_ID, digest_msg)

    withheld_note = f" ({len(tier1_matches) - cards_sent} withheld: CRM write failed)" if cards_sent < len(tier1_matches) else ""
    logging.info(f"Stage 2 Complete: {cards_sent} Tier-1 cards{withheld_note} + {len(tier2_matches)} Tier-2 digest entries dispatched.")
    if chat_id:
        send_status_update(chat_id, f"Pipeline Complete: {cards_sent} Tier-1 cards{withheld_note} + {len(tier2_matches)} Tier-2 digest entries dispatched.")
    
    return len(tier1_matches) + len(tier2_matches)

def run_warm_radar_scan(chat_id=None):
    """/w Warm Network Radar: a zero-LLM, near-instant scan of ONLY the companies where a Carmen
    Warm CRM contact already exists. Resolves each warm company's ATS slug straight from the
    company_identities cache (no live board probing at all - resolve_warm_company_ats_slugs() is
    deliberately not called), pulls its Greenhouse/Lever/Ashby postings, and reports any new role
    against the contact it maps to. Shares the /t dedup ledger (seen_jobs) so it never re-alerts a
    role /t already surfaced, and only stamps the ones it newly surfaces itself. No Gemini scoring.
    """
    warm_contacts = get_warm_crm_contacts()
    if not warm_contacts:
        if chat_id:
            send_telegram_message(chat_id, "⚠️ <b>Warm Radar:</b> no Carmen Warm contacts found.")
        return 0

    # Cache-only slug resolution: every warm company that already has a verified ats_slug row.
    # A warm company with no cached slug is skipped silently - it gets picked up whenever /t or
    # /eco add next populates company_identities for it.
    try:
        with get_db_conn() as conn:
            identity_rows = conn.execute(
                "SELECT normalized_name, ats_slug FROM company_identities WHERE ats_slug IS NOT NULL AND ats_slug != ''"
            ).fetchall()
    except Exception as e:
        logging.error(f"Warm Radar Identity Lookup Error: {e}")
        identity_rows = []
    slug_to_contact = {}
    for normalized_name, slug in identity_rows:
        contact = warm_contacts.get(normalized_name)
        if contact:
            slug_to_contact.setdefault(slug, contact)

    if not slug_to_contact:
        if chat_id:
            send_telegram_message(chat_id, "🔭 <b>Warm Radar:</b> no warm companies have a cached ATS board yet - run /t or /eco add first.")
        return 0

    if chat_id:
        send_status_update(chat_id, f"Warm Radar: scanning {len(slug_to_contact)} warm companies with a cached ATS board (no AI scoring)...")

    slugs = list(slug_to_contact)
    seen_hashes = set()
    today_str = datetime.now().strftime("%Y-%m-%d")
    followup_date = (datetime.now() + timedelta(days=calculate_followup_interval(5))).strftime("%Y-%m-%d")
    clavicular_rows = []
    match_count = 0

    for job in fetch_ats_jobs(slugs):
        company = job.get("employer_name") or ""
        title = job.get("job_title") or ""
        job_hash = generate_dedup_hash(company, title)
        # Mirror _add_candidate's ordering: check the in-run set AND the shared seen_jobs ledger
        # before stamping anything, so a role /t already surfaced is skipped without a re-stamp.
        if job_hash in seen_hashes or is_job_seen_db(job_hash):
            continue

        # Tie the posting back to its warm contact by the slug baked into its job_id
        # (gh_/lever_/ashby_<slug>_...), falling back to a normalized-name match on the ATS
        # employer label. Postings we can't attribute to a warm contact aren't /w's to surface.
        job_id = str(job.get("job_id") or "")
        contact = next(
            (c for s, c in slug_to_contact.items() if job_id.startswith((f"gh_{s}_", f"lever_{s}_", f"ashby_{s}_"))),
            None
        ) or warm_contacts.get(normalize_company_for_match(company))
        if not contact:
            continue

        seen_hashes.add(job_hash)
        save_seen_job_db(job_hash)
        match_count += 1

        raw_id = job.get("job_id") or f"{company}_{title}"
        short_id = generate_short_key(raw_id, fallback=time.time())
        target_email = resolve_target_email(company, title, job.get("employer_website"))
        job["target_email"] = target_email
        sheet_uuid = save_job_to_cache(short_id, job)

        contact_name = contact.get("name", "Contact")
        send_warm_radar_card(job, contact_name, contact.get("note", "Active relationship"), sheet_uuid)

        clavicular_rows.append({
            "sheet_uuid": sheet_uuid,
            "row_data": [
                today_str, company, title, target_email, "",
                "Matched", followup_date, job.get("job_apply_link", ""),
                f"Warm Radar Match (no AI score): {contact_name}"
            ]
        })
        time.sleep(1.1)  # same inter-card Telegram pacing run_job_pipeline uses

    if clavicular_rows:
        enqueue_crm_payload(build_crm_payload("batch_add_rows", target_code="CL", rows=clavicular_rows))

    logging.info(f"Warm Radar Complete: {match_count} new matches across {len(slugs)} warm companies.")
    if chat_id:
        send_telegram_message(chat_id, f"🏁 Warm scan complete. {match_count} new matches across {len(slugs)} warm companies checked.")
    return match_count

# ==============================================================================
# 9. ASYNC WORKLOAD PROCESSOR & WEBHOOK CONTROLLER
# ==============================================================================
def process_webhook_payload_async(data):
    """Executes heavy workloads in background worker threads so HTTP return is instant.
    Pure text-based swipe-reply CLI - no inline keyboards/callback_query handling at all.
    """
    try:
        if "message" not in data:
            logging.info("Webhook payload contained no message key - ignored")
            return

        msg = data["message"]
        chat_id = msg["chat"]["id"]
        raw_text = msg.get("text", "").strip()
        text = re.sub(r"@\w+bot", "", raw_text, flags=re.IGNORECASE).strip()
        today_str = datetime.now().strftime("%Y-%m-%d")
        logging.info(f"Telegram command received: '{text}' (chat_id={chat_id})")

        # 1b. Tuesday Batch Hub Commands (/sendall, /snoozeall)
        if text == "/sendall":
            send_telegram_message(chat_id, "⏳ <b>Send-All Started:</b> creating bump drafts and queueing +14-day follow-ups...")
            threading.Thread(target=_run_overdue_batch_and_notify, args=(chat_id, "sendall", 14), daemon=True).start()
            return

        snooze_match = re.match(r"^/snoozeall(?:\s+(\d+))?$", text)
        if snooze_match:
            snooze_days = safe_int(snooze_match.group(1), 7)
            if snooze_days < 1 or snooze_days > 365:
                send_telegram_message(chat_id, "❌ <b>Usage:</b> <code>/snoozeall &lt;days 1-365&gt;</code>")
                return
            send_telegram_message(chat_id, f"⏳ <b>Snooze-All Started:</b> queueing overdue follow-ups to +{snooze_days} days...")
            threading.Thread(target=_run_overdue_batch_and_notify, args=(chat_id, "snoozeall", snooze_days), daemon=True).start()
            return

        # 2. Pipeline Run Trigger (/t [qty])
        if re.match(r"^/t(?:\s+(\d+))?$", text):
            m = re.match(r"^/t(?:\s+(\d+))?$", text)
            qty = safe_int(m.group(1), 2)
            target_queries = safe_list(get_filter("target_queries", []))
            ats_slugs = safe_list(get_filter("ats_company_slugs", []))
            if not target_queries:
                # Without queries JSearch is skipped and the run quietly pulls only warm-board
                # listings, which looks like a slow day rather than a broken config. Say so up front.
                send_telegram_message(
                    chat_id,
                    "⚠️ <b>target_queries is empty or unreadable.</b> JSearch will be skipped, so this "
                    "run only covers ATS boards. Check /health and the Render logs for "
                    "<code>Filter Read Error</code>. If the value was blanked, a restart restores the default queries."
                )
            send_telegram_message(
                chat_id,
                f"🚀 <b>Triggering Job Search Pipeline (Top {qty})</b>\n"
                f"🔍 Scanning {len(target_queries)} target rules & {len(ats_slugs)} ATS boards (with live Hope Alumni resolution)..."
            )
            count = run_job_pipeline(chat_id, top_n=qty)
            send_telegram_message(chat_id, f"🏁 Pipeline Completed. {count} cards dispatched.")
            return

        # 2b. Warm Network Radar (/w): zero-LLM, cache-only scan of warm-contact companies
        if re.match(r"^/w$", text):
            send_telegram_message(chat_id, "🔭 <b>Warm Network Radar:</b> checking cached ATS boards for your warm contacts (no AI scoring)...")
            run_warm_radar_scan(chat_id)
            return

        # 2c. Manual Job Ingest (/job, /j): push a single hand-picked posting through the exact
        # Stage 2 path /t uses, so it lands in Tetiana Cold (or Clavicular) as a real card with a
        # live sheet_uuid - swipe-reply, the follow-up sequencer and /funnel all work on it after.
        if re.match(r"^/(job|j)\b", text, re.IGNORECASE):
            parsed = parse_job_command(text)
            if not parsed:
                send_telegram_message(
                    chat_id,
                    "📋 <b>Add a job to the pipeline</b>\n\n"
                    "Paste the link:\n"
                    "<code>/job https://www.linkedin.com/jobs/view/4461280495/</code>\n\n"
                    "If LinkedIn blocks the read, spell it out:\n"
                    "<code>/job Foreign Exchange Ops Analyst 2 @ Huntington National Bank</code>"
                )
                return
            ing_title, ing_company, ing_url = parsed
            send_telegram_message(
                chat_id,
                "⏳ <b>Ingesting job...</b> scoring it through the same pipeline as /t "
                "(Gemini fit, alumni lookup, warm routing)."
            )

            def _ingest_and_report(u=ing_url, t=ing_title, c=ing_company, cid=chat_id):
                try:
                    ok, message = ingest_manual_job(url=u, title=t or "", company=c or "", chat_id=cid, source_label="/job")
                    if not ok and message:
                        send_telegram_message(cid, message)
                except Exception as e:
                    logging.error(f"/job Ingest Error: {e}", exc_info=True)
                    send_telegram_message(cid, f"❌ <b>Ingest failed:</b> {html.escape(str(e))}")

            # Backgrounded for the same reason /t is: Gemini scoring plus the alumni lookup can run
            # well past Telegram's webhook timeout, and a timed-out webhook is retried - which would
            # score and write the same posting twice.
            threading.Thread(target=_ingest_and_report, daemon=True).start()
            return

        # 3. Networking Cards Pull Triggers (/c, /cw, /cc [qty])
        if re.match(r"^/(c|cw|cc)(?:\s+(\d+))?$", text):
            m = re.match(r"^/(c|cw|cc)(?:\s+(\d+))?$", text)
            cmd_type = m.group(1)
            qty = safe_int(m.group(2), 5)
            target_code = "CW" if cmd_type in ["c", "cw"] else "TC"
            loading_msg_id = send_telegram_message(chat_id, "⏳ <i>Fetching CRM data...</i>")
            cards = fetch_networking_cards(target_code, qty)
            if not cards:
                if loading_msg_id:
                    edit_telegram_message(chat_id, loading_msg_id, "❌ <b>No active records found.</b>")
                else:
                    send_telegram_message(chat_id, f"No active networking cards found for <code>/{cmd_type}</code>.")
                return
            if loading_msg_id:
                edit_telegram_message(chat_id, loading_msg_id, "✅ <b>Data retrieved.</b>")
            for c in cards:
                is_warm = (cmd_type in ["c", "cw"])
                # A CRM contact row has no cached job behind it, so there is no routed
                # outreach_template_id to honour here - index 0 is correct. The contact's name is
                # not: these rows always have one, so the greeting is filled rather than bare.
                # The warm path took c["note"] in the contact_name slot, which rendered the CRM
                # note itself as the salutation ("Hi Met at the SEC panel,"); it takes the name,
                # first-name-reduced like every other greeting.
                draft_text = (
                    generate_warm_email(first_name_for_greeting(c.get("name", "")))
                    if is_warm else
                    generate_cold_email(
                        c.get("title") or "", c.get("company", "Target Firm"),
                        contact_name=first_name_for_greeting(c.get("name", "")),
                    )
                )
                monospaced_draft = format_email_block(draft_text)
                contact_sheet_uuid = c.get("sheet_uuid", "")
                card_msg = (
                    f"👤 <b>{c.get('name', 'Contact')}</b> | {c.get('company', 'Company')}\n"
                    f"🆔 <code>{html.escape(str(contact_sheet_uuid))}</code> · <code>{html.escape(target_code)}</code>\n"
                    f"<b>Priority Tier:</b> {c.get('priority', 5)}/10\n"
                    f"<b>Last Note:</b> <i>{c.get('note', 'N/A')}</i>\n\n"
                    f"<b>Tap-to-Copy Email Draft:</b>\n{monospaced_draft}"
                )
                sent_msg_id = send_telegram_message(chat_id, card_msg)
                if sent_msg_id and contact_sheet_uuid:
                    save_message_mapping(sent_msg_id, contact_sheet_uuid, target_code, c.get("name", ""), c.get("company", ""), c.get("email", ""))
            return

        # 4. Priority Batcher (/p 1-10)
        if re.match(r"^/p\s+(\d+)$", text):
            priority_lvl = safe_int(re.match(r"^/p\s+(\d+)$", text).group(1))
            loading_msg_id = send_telegram_message(chat_id, "⏳ <i>Fetching CRM data...</i>")
            contacts = []
            resp_obj = crm_get({"action": "get_priority", "level": priority_lvl})
            if resp_obj:
                try:
                    resp = resp_obj.json()
                    contacts = resp.get("contacts", [])
                except Exception:
                    contacts = []
            if not contacts:
                if loading_msg_id:
                    edit_telegram_message(chat_id, loading_msg_id, "❌ <b>No active records found.</b>")
                else:
                    send_telegram_message(chat_id, f"No active contacts found at Priority Tier {priority_lvl}.")
                return
            if loading_msg_id:
                edit_telegram_message(chat_id, loading_msg_id, "✅ <b>Data retrieved.</b>")
            out_msg = f"📌 <b>PRIORITY {priority_lvl} CONTACTS ({len(contacts)} Total)</b>\n\n"
            for idx, c in enumerate(contacts, 1):
                out_msg += f"{idx}. <b>{c.get('name')}</b> | {c.get('company')}\n"
                out_msg += f"   Last Contact: {c.get('last_contact')} | Next: {c.get('next_followup')}\n"
                out_msg += f"   Note: <i>{c.get('latest_note', 'No notes logged')}</i>\n\n"
            send_telegram_message(chat_id, out_msg)
            return

        # 5. Monospaced /quick Template & Quick Add Parser
        if text == "/quick":
            template_msg = (
                "Tap the code block below to copy, adjust details, and send:\n\n"
                "<code>/quick Jane Van Der Bilt @ Acme Corp 9 Spoke at event interested in back-office systems</code>"
            )
            send_telegram_message(chat_id, template_msg)
            return

        if text.startswith("/quick "):
            result = parse_quick_command(text)
            if result is None:
                send_telegram_message(chat_id, "❌ Invalid /quick format. Use: <code>/quick Name@Company [Priority 1-10] [Note]</code>")
                return
            name, company, priority, note = result
            sheet_uuid = str(uuid.uuid4())
            next_followup = (datetime.now() + timedelta(days=calculate_followup_interval(priority))).strftime("%Y-%m-%d")
            payload = build_crm_payload(
                "quick_add",
                sheet_uuid=sheet_uuid,
                first_contact=today_str,
                last_contact=today_str,
                name=name,
                company=company,
                priority=priority,
                next_followup=next_followup,
                note=f"[{today_str}] {note}"
            )
            log_to_sheets_crm(payload)
            resp = (
                f"✅ <b>Contact Created</b>\n"
                f"🆔 <code>{html.escape(sheet_uuid)}</code> · <code>Carmen Warm</code>\n"
                f"<b>Name:</b> {html.escape(name)}\n"
                f"<b>Company:</b> {html.escape(company)}\n"
                f"<b>Priority:</b> {priority}/10\n"
                f"<b>Next Follow-up:</b> {next_followup}"
            )
            sent_msg_id = send_telegram_message(chat_id, resp)
            save_message_mapping(sent_msg_id, sheet_uuid, "Carmen Warm", name, company)
            threading.Thread(target=auto_expand_ats_slug, args=(company,), daemon=True).start()
            return

        # 5b. Standalone /cold and /warm Quick-Add (distinct from the bare /cold, /warm swipe-reply
        # stage-move below - these always require trailing "Name @ Company" text, so they never collide)
        if text.startswith("/cold ") or text.startswith("/warm "):
            is_warm_quickadd = text.startswith("/warm ")
            target_tab = "Carmen Warm" if is_warm_quickadd else "Carmen Cold"
            cmd_token = "/warm" if is_warm_quickadd else "/cold"
            result = parse_quick_command(text)
            if result is None:
                send_telegram_message(chat_id, f"❌ Invalid {cmd_token} format. Use: <code>{cmd_token} Name@Company [Priority 1-10] [Note]</code>")
                return
            name, company, priority, note = result
            sheet_uuid = str(uuid.uuid4())
            next_followup = (datetime.now() + timedelta(days=calculate_followup_interval(priority))).strftime("%Y-%m-%d")
            payload = build_crm_payload(
                "quick_add",
                target_code="CW" if is_warm_quickadd else "CC",
                sheet_uuid=sheet_uuid,
                first_contact=today_str,
                last_contact=today_str,
                name=name,
                company=company,
                priority=priority,
                status="Warm Lead" if is_warm_quickadd else "Cold Lead",
                next_followup=next_followup,
                note=f"[{today_str}] {note}"
            )
            log_to_sheets_crm(payload)
            resp = (
                f"✅ <b>Contact Created ({target_tab})</b>\n"
                f"🆔 <code>{html.escape(sheet_uuid)}</code> · <code>{html.escape(target_tab)}</code>\n"
                f"<b>Name:</b> {html.escape(name)}\n"
                f"<b>Company:</b> {html.escape(company)}\n"
                f"<b>Priority:</b> {priority}/10\n"
                f"<b>Next Follow-up:</b> {next_followup}"
            )
            sent_msg_id = send_telegram_message(chat_id, resp)
            save_message_mapping(sent_msg_id, sheet_uuid, target_tab, name, company)
            threading.Thread(target=auto_expand_ats_slug, args=(company,), daemon=True).start()
            return

        # 6. Dynamic /search Filters Overview & Inline Adjustments
        if text == "/search":
            min_sal = safe_int(get_filter("min_salary"), 50000)
            exp_sal = safe_int(get_filter("experience_salary_floor"), 60000)
            bans = safe_list(get_filter("title_exclusions"))
            cities = safe_list(get_filter("valid_cities"))
            kws = safe_list(get_filter("required_keywords"))
            card_text = (
                "🔍 <b>Active Search Filters</b>\n"
                f"💰 <b>Min Pay:</b> ${min_sal:,} | <b>Exp Floor:</b> ${exp_sal:,}\n"
                f"📍 <b>Cities ({len(cities)}):</b> {', '.join(cities[:4]) if cities else 'All'}\n"
                f"🚫 <b>Banned ({len(bans)}):</b> {', '.join(bans[:3]) if bans else 'None'}\n"
                f"🔑 <b>Keywords ({len(kws)}):</b> {', '.join(kws[:3]) if kws else 'Any'}\n\n"
                "<b>Tap-to-Copy Quick Adjustments</b>\n"
                "<code>pay = 65000</code>\n"
                "<code>kw + python</code>\n"
                "<code>ban + sales</code>\n"
                "<code>city + canton</code>"
            )
            send_telegram_message(chat_id, card_text)
            return

        # 7. Telemetry & Utility Commands (/s, /health, /efficiency)
        if text == "/s":
            cw_cards = fetch_networking_cards("CW", qty=5)
            tc_cards = fetch_networking_cards("TC", qty=5)
            today_date = datetime.now().date()
            overdue = []
            for c in (cw_cards + tc_cards):
                try:
                    nf_date = datetime.strptime(str(c.get("next_followup")), "%Y-%m-%d").date()
                except Exception:
                    continue
                if nf_date <= today_date:
                    overdue.append({**c, "days_overdue": (today_date - nf_date).days})
            overdue.sort(key=lambda x: x["days_overdue"], reverse=True)

            if not overdue:
                send_telegram_message(chat_id, "📊 <b>Overdue Pipeline:</b> 0 contacts require immediate action. All caught up!")
                return

            lines = [f"📊 <b>Overdue Pipeline:</b> {len(overdue)} contacts require immediate action.\n"]
            for item in overdue[:3]:
                comp = html.escape(str(item.get("company") or "N/A"))
                name = html.escape(str(item.get("name") or "N/A"))
                lines.append(f"• <b>{comp}</b> - {name} | {item['days_overdue']}d overdue | <code>/f 7</code>")
            send_telegram_message(chat_id, "\n".join(lines))
            return
        if text == "/unbury" or text == "/unbury go":
            # Repairs the damage the empty-description simhash collision already did. Every job
            # dropped by that bug was recorded against the SAME poisoned hash (the empty-string
            # MD5), so deleting that one row makes every posting hidden behind it eligible again.
            # Also clears seen_jobs rows that were written before a job had been judged - the
            # pre-fix _add_candidate marked jobs seen BEFORE passes_strict_filter ran, so rejected
            # postings are sitting in the ledger as if Kevin had already considered them.
            commit = text.endswith(" go")
            EMPTY_DESC_HASH = hashlib.md5(b"").hexdigest()
            try:
                with get_db_conn() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM seen_content_hashes WHERE content_hash = ?", (EMPTY_DESC_HASH,))
                    poisoned = cursor.fetchone()[0]
                    cursor.execute("SELECT COUNT(*) FROM seen_jobs")
                    total_seen = cursor.fetchone()[0]
                    cursor.execute("SELECT COUNT(*) FROM seen_jobs WHERE seen_count <= 1 AND first_seen < datetime('now', '-1 day')")
                    stale_singles = cursor.fetchone()[0]
                    if commit:
                        conn.execute("BEGIN IMMEDIATE")
                        conn.execute("DELETE FROM seen_content_hashes WHERE content_hash = ?", (EMPTY_DESC_HASH,))
                        conn.execute("DELETE FROM seen_jobs WHERE seen_count <= 1 AND first_seen < datetime('now', '-1 day')")
                        conn.commit()
                send_telegram_message(
                    chat_id,
                    (f"🪦 <b>Unbury {'complete' if commit else '(preview)'}</b>\n\n"
                     f"Poisoned empty-description hash rows: <b>{poisoned}</b>\n"
                     f"seen_jobs total: <b>{total_seen}</b>\n"
                     f"Seen-once rows older than a day: <b>{stale_singles}</b>\n\n"
                     + ("✅ Cleared. Those jobs can surface again on the next <code>/t</code>."
                        if commit else
                        "Nothing was deleted. Run <code>/unbury go</code> to clear them."))
                )
            except Exception as e:
                send_telegram_message(chat_id, f"⚠️ Unbury failed: {html.escape(str(e))}")
            return

        if text == "/health":
            db_check_start = time.time()
            try:
                with get_db_conn() as conn:
                    cursor = conn.cursor()
                    cursor.execute("PRAGMA journal_mode")
                    wal_mode = cursor.fetchone()[0]
            except Exception as e:
                wal_mode = f"error: {e}"
            db_elapsed_ms = round((time.time() - db_check_start) * 1000, 2)
            uptime_str = str(timedelta(seconds=int(time.time() - APP_START_TIME)))
            api_usage = get_monthly_api_usage()
            month_label = datetime.now().strftime("%B %Y")
            try:
                persistence = get_persistence_status()
                rc = persistence["row_counts"]
                persistence_lines = (
                    f"💽 <b>Persistence:</b>\n"
                    f"  DB: <code>{html.escape(persistence['db_path'])}</code>\n"
                    f"  seen_jobs: {rc['seen_jobs']} | pipeline_metrics: {rc['pipeline_metrics']} | "
                    f"application_outcomes: {rc['application_outcomes']} | daily_activity: {rc['daily_activity']}\n"
                    f"  Backups: <code>{html.escape(persistence['backup_dir'])}</code> "
                    f"({'exists' if persistence['backup_dir_exists'] else 'MISSING'}, "
                    f"{persistence['backup_snapshot_count']} snapshot(s))\n"
                )
            except Exception as e:
                persistence_lines = f"💽 <b>Persistence:</b> error reading status ({html.escape(str(e))})\n"
            send_telegram_message(
                chat_id,
                f"🟢 <b>System Health:</b> Operational\n"
                f"💾 <b>SQLite Mode:</b> {html.escape(str(wal_mode)).upper()} ({db_elapsed_ms}ms)\n"
                f"⏱️ <b>Uptime:</b> {uptime_str}\n"
                f"{persistence_lines}"
                f"📇 <b>Email Waterfall Usage ({month_label}, local count):</b>\n"
                f"  Hunter.io: {api_usage['hunter']} | Prospeo: {api_usage['prospeo']} | GetProspect: {api_usage['getprospect']}"
            )
            return
        if text == "/efficiency":
            messages_sent = get_metric_count("message_sent")
            interviews_set = get_metric_count("interview_set")
            ratio = (interviews_set / messages_sent * 100) if messages_sent > 0 else 0.0
            send_telegram_message(chat_id, f"📈 <b>Golden Ratio:</b> {ratio:.1f}% ({interviews_set} interviews / {messages_sent} sent)")
            return
        if text == "/funnel":
            res = crm_get({"action": "funnel_stats"})
            payload = {}
            if res is not None:
                try:
                    payload = res.json()
                except Exception:
                    payload = {}
            if not payload or payload.get("status") != "success":
                send_telegram_message(chat_id, "⚠️ <b>Funnel unavailable:</b> couldn't read the CRM funnel endpoint.")
                return

            def _fmt_buckets(b):
                b = b or {}
                return (
                    f"Matched {b.get('Matched', 0)} | Applied {b.get('Applied', 0)} | Replied {b.get('Replied', 0)}\n"
                    f"Screening {b.get('Screening', 0)} | Interviewing {b.get('Interviewing', 0)} | "
                    f"Offer {b.get('Offer', 0)} | Rejected {b.get('Rejected', 0)}"
                )

            rates = payload.get("rates", {})
            _pct = lambda key: f"{float(rates.get(key) or 0):.1f}"
            funnel_lines = [
                "📊 <b>CRM Pipeline Funnel</b>\n",
                "<b>OVERALL</b>",
                _fmt_buckets(payload.get("overall", {})),
                (f"<b>Rates:</b> Matched→Applied {_pct('matched_to_applied')}% · "
                 f"Applied→Replied {_pct('applied_to_reply')}% · "
                 f"Replied→Interviewing {_pct('reply_to_interview')}% · "
                 f"Interviewing→Offer {_pct('interview_to_offer')}%"),
            ]
            for persona, buckets in (payload.get("by_persona") or {}).items():
                funnel_lines.append(f"\n<b>{html.escape(str(persona)).upper()}</b>")
                funnel_lines.append(_fmt_buckets(buckets))
            send_telegram_message(chat_id, "\n".join(funnel_lines))
            return

        if text == "/queue":
            # On-demand, read-only preview of the nightly follow-up sequencer: no writes,
            # no burying, no snooze advancement (run_followup_sequencer(dry_run=True)).
            result = run_followup_sequencer(dry_run=True)
            send_telegram_message(chat_id, render_followup_needs_card(result, on_demand=True))
            return

        if text == "/overdue":
            # The full list the Tuesday hub used to push unrequested every week. Same lines,
            # same chunking - it just arrives when Kevin asks for it.
            send_overdue_digest(chat_id, get_overdue_followups(), limit=None)
            return

        if text == "/outcomes":
            send_telegram_message(chat_id, format_outcome_metrics_message())
            return

        if text in ("/treplies", "/templatereplies"):
            send_telegram_message(chat_id, format_template_reply_rates_message())
            return

        if text in ["/streak", "/daily"]:
            today_activity = get_daily_activity(datetime.now().strftime("%Y-%m-%d"))
            lifetime = get_lifetime_activity_totals()
            streak_days = calculate_active_day_streak()
            goal_target = 5
            scorecard_msg = (
                "🏆 <b>Daily Outreach Scorecard</b>\n\n"
                f"🎯 <b>Today's Goal:</b> {today_activity['drafts_staged']} / {goal_target} Staged Drafts\n"
                f"🔥 <b>Current Streak:</b> {streak_days} Active Days\n"
                f"📊 <b>Lifetime Totals:</b> Staged: {lifetime['drafts_staged']} | Applied: {lifetime['applied_count']} | Notes: {lifetime['notes_logged']}"
            )
            send_telegram_message(chat_id, scorecard_msg)
            return

        # 7. Corporate Ecosystem Expansion (/ecosystem add <entity> | /ecosystem)
        if text.startswith("/ecosystem add ") or text.startswith("/eco add "):
            try:
                # Handle both /ecosystem and /eco variants
                if text.startswith("/eco add "):
                    entity_name = text[8:].strip()
                else:
                    entity_name = text[14:].strip()

                if not entity_name:
                    send_telegram_message(chat_id, "⚠️ <b>Usage:</b> /ecosystem add <company_name>")
                    return

                result_msg = expand_ecosystem_filter(entity_name)
                send_telegram_message(chat_id, result_msg)
            except Exception as e:
                logging.error(f"Ecosystem add command error: {e}")
                send_telegram_message(chat_id, f"❌ <b>Ecosystem Error:</b> {html.escape(str(e)[:100])}")
            return

        if text == "/ecosystem":
            try:
                tier1_list = get_filter("tier1_ecosystem") or []
                ats_list = get_filter("ats_company_slugs") or []

                keywords_display = ", ".join(f"<code>{html.escape(str(k)[:25])}</code>" for k in tier1_list[:10]) if tier1_list else "No keywords"
                slugs_display = ", ".join(f"<code>{html.escape(str(s))}</code>" for s in ats_list[:10]) if ats_list else "No active boards"

                ecosystem_overview = (
                    f"🌐 <b>Active Ecosystem Overview</b>\n\n"
                    f"🏢 <b>Tier-1 Keywords ({len(tier1_list)}):</b>\n{keywords_display}"
                    f"{f'<br/>... and {len(tier1_list)-10} more' if len(tier1_list) > 10 else ''}\n\n"
                    f"🔗 <b>ATS Board Slugs ({len(ats_list)}):</b>\n{slugs_display}"
                    f"{f'<br/>... and {len(ats_list)-10} more' if len(ats_list) > 10 else ''}"
                )
                send_telegram_message(chat_id, ecosystem_overview)
            except Exception as e:
                logging.error(f"Ecosystem overview command error: {e}")
                send_telegram_message(chat_id, f"❌ <b>Ecosystem Overview Error:</b> {html.escape(str(e)[:100])}")
            return

        # 8. Mobile Parameter Mutation & Inline Action Shortcuts
        cmd_body = re.sub(r"^/search\s*", "", text).strip()
        if any(op in cmd_body for op in ["=", "+", "-"]):
            match = re.match(r"^([a-zA-Z_]+)\s*(=|\+|-)\s*(.+)$", cmd_body)
            if match:
                raw_key = match.group(1).strip()
                op = match.group(2).strip()
                val_str = match.group(3).strip()
                val_arg = f"{op} {val_str}" if op in ["+", "-"] else val_str
                update_res = update_filter_param(raw_key, val_arg)
                send_telegram_message(chat_id, update_res)
                return

        # 9. Swipe-Reply CRM Actions (/f, /n, /apply, /warm, /cold, /x, /e) - require reply context
        if text.startswith("/f ") or text == "/f":
            mapping = resolve_reply_mapping(msg, chat_id, "/f")
            if not mapping:
                return
            parts = text.split()
            days = safe_int(parts[1], 7) if len(parts) > 1 else 7
            next_followup = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
            # Optimistic UI: confirm to Telegram first, dispatch the Sheets write in the background
            send_telegram_message(chat_id, f"📅 Follow-up snoozed to {next_followup}.")
            enqueue_crm_payload(build_crm_payload("update_snooze", sheet_uuid=mapping["sheet_uuid"], next_followup=next_followup))
            return

        if text.startswith("/n "):
            note_str = text[3:].strip()
            if not note_str:
                send_telegram_message(chat_id, "❌ Note cannot be empty.")
                return
            mapping = resolve_reply_mapping(msg, chat_id, "/n")
            if not mapping:
                return
            timestamped_note = f"[{today_str}] {html.escape(note_str)}"
            # Optimistic UI: confirm to Telegram first, dispatch the Sheets write in the background - Code.gs auto-timestamps if this ever changes
            send_telegram_message(chat_id, "📝 Note logged.")
            enqueue_crm_payload(build_crm_payload("append_note", sheet_uuid=mapping["sheet_uuid"], note=timestamped_note))
            log_daily_activity("notes_logged")
            return

        if text == "/draft":
            mapping = resolve_reply_mapping(msg, chat_id, "/draft")
            if not mapping:
                return
            job = get_job_by_sheet_uuid(mapping["sheet_uuid"])
            job, from_card = rebuild_job_from_card(job, (msg.get("reply_to_message") or {}).get("text", ""))
            if not _job_data_available(job, mapping):
                send_telegram_message(chat_id, STALE_CARD_WARNING)
                return
            if from_card:
                send_telegram_message(chat_id, CARD_RECOVERED_NOTICE)
            comp = job.get("employer_name") or mapping.get("contact_company") or "Target Firm"
            title = job.get("job_title") or "Operations Specialist"
            is_warm = mapping.get("sheet_tab") in ("Carmen Warm", "Carmen Cold")
            domain_hint = extract_domain_from_website(job.get("employer_website")) if job else None
            if mapping.get("contact_name"):
                # Named CRM contact (not a generic job-alert row) - resolve a real person's email via the waterfall
                target = resolve_email_waterfall(mapping["contact_name"], comp, domain_hint, on_provider_attempt=increment_api_usage_counter)
                confidence = "unverified" if is_unverified_email(target) else "verified"
                log_email_enrichment_attempt(mapping["sheet_uuid"], "waterfall", target, confidence)
                if confidence == "unverified":
                    send_telegram_message(
                        chat_id,
                        f"⚠️ <b>Unverified Contact Email - Draft Not Created</b>\n"
                        f"<b>Best guess:</b> <code>{html.escape(target)}</code>\n\n"
                        f"Reply <code>/e actual@email.com</code> to confirm the real address and create the draft."
                    )
                    return
            else:
                target = resolve_target_email(comp, title, job.get("employer_website"))
            track = job.get("track", "a")
            bullet_indices = job.get("bullet_indices")
            tone_mode = job.get("tone_mode", "conservative")
            pdf_filename = resume_pdf_filename(comp)
            pdf_bytes = compile_resume_pdf_resilient(chat_id, comp, track, bullet_indices, "/draft", tone_mode=tone_mode)
            logging.info(f"/draft command: staging Gmail draft for {comp} <{target}> (chat_id={chat_id})")
            raw_email_text = resolve_outreach_body(job, mapping, title, comp, is_warm)
            ok, gmail_msg, draft_id = create_gmail_draft(
                to_email=target, company_name=comp, job_title=title, is_warm=is_warm,
                custom_body=raw_email_text, pdf_bytes=pdf_bytes, pdf_filename=pdf_filename
            )
            monospaced_body = format_email_block(raw_email_text)
            draft_link_line = ""
            if draft_id:
                draft_url = html.escape(f"https://mail.google.com/mail/u/0/#drafts/{draft_id}", quote=True)
                draft_link_line = f"📱 <a href='{draft_url}'>Open Draft in Gmail</a>\n\n"
            if ok:
                status_hdr = "✉️ <b>Gmail Draft Created & Ready!</b>"
                log_daily_activity("drafts_staged")
            else:
                status_hdr = f"⚠️ <b>Gmail API Alert ({html.escape(gmail_msg)})</b> - Manual Copy Below:"
            draft_msg = (
                f"{status_hdr}\n"
                f"{draft_link_line}"
                f"<b>To:</b> <code>{html.escape(target)}</code>\n\n"
                f"<b>Tap-to-Copy Email Body:</b>\n{monospaced_body}"
            )
            send_telegram_message(chat_id, draft_msg)
            return

        if text == "/eh" or text.startswith("/eh "):
            custom_name = text[len("/eh"):].strip()
            mapping = resolve_reply_mapping(msg, chat_id, "/eh")
            if not mapping:
                return
            job = get_job_by_sheet_uuid(mapping["sheet_uuid"])
            job, from_card = rebuild_job_from_card(job, (msg.get("reply_to_message") or {}).get("text", ""))
            if not _job_data_available(job, mapping):
                send_telegram_message(chat_id, STALE_CARD_WARNING)
                return
            if from_card:
                send_telegram_message(chat_id, CARD_RECOVERED_NOTICE)
            comp = job.get("employer_name") or mapping.get("contact_company") or "Target Firm"
            title = job.get("job_title") or "Operations Specialist"
            is_warm = mapping.get("sheet_tab") in ("Carmen Warm", "Carmen Cold")
            domain_hint = extract_domain_from_website(job.get("employer_website")) if job else None
            contact_name = custom_name or "Operations Lead"

            target = resolve_email_waterfall(contact_name, comp, domain_hint=domain_hint, on_provider_attempt=increment_api_usage_counter)
            confidence = "unverified" if is_unverified_email(target) else "verified"
            log_email_enrichment_attempt(mapping["sheet_uuid"], "waterfall", target, confidence)
            update_job_target_email(mapping["sheet_uuid"], target)
            enqueue_crm_payload(build_crm_payload("update_contact_email", sheet_uuid=mapping["sheet_uuid"], email=target))
            # Same reasoning as /e: an address resolved and drafted to here is one Kevin is
            # actively working, so it belongs in Carmen Cold regardless of company tracking.
            # Unverified waterfall guesses never reach this line - that branch returns above.
            log_addressed_contact_to_carmen_cold(
                target, company=comp, name=mapping.get("contact_name", ""),
                note=f"[{datetime.now().strftime('%Y-%m-%d')}] Emailed: {title}"
            )

            # Compile the same tailored resume PDF /draft and /e attach, so /eh never regresses to a bare-text draft
            track = job.get("track", "a")
            bullet_indices = job.get("bullet_indices")
            tone_mode = job.get("tone_mode", "conservative")
            pdf_filename = resume_pdf_filename(comp)
            pdf_bytes = compile_resume_pdf_resilient(chat_id, comp, track, bullet_indices, "/eh", tone_mode=tone_mode)

            raw_email_text = resolve_outreach_body(job, mapping, title, comp, is_warm)
            ok, gmail_msg, draft_id = create_gmail_draft(
                to_email=target, company_name=comp, job_title=title, is_warm=is_warm,
                custom_body=raw_email_text, pdf_bytes=pdf_bytes, pdf_filename=pdf_filename
            )
            monospaced_body = format_email_block(raw_email_text)
            draft_link_line = ""
            if draft_id:
                draft_url = html.escape(f"https://mail.google.com/mail/u/0/#drafts/{draft_id}", quote=True)
                draft_link_line = f"📱 <a href='{draft_url}'>Open Draft in Gmail</a>\n\n"
            confidence_badge = "⚠️ Unverified guess" if confidence == "unverified" else "✅ Verified"
            confirm_msg = (
                f"🔍 <b>API Lookup Resolved ({confidence_badge}):</b> <code>{html.escape(target)}</code>\n\n"
                f"{draft_link_line}"
                f"<b>Tap-to-Copy Email Body:</b>\n{monospaced_body}"
            )
            # Optimistic UI: confirm to Telegram first, dispatch the Sheets write in the background
            send_telegram_message(chat_id, confirm_msg)
            if ok:
                log_daily_activity("drafts_staged")
            return

        if text.startswith("/e ") or text.startswith("/email "):
            raw_email = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
            email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
            if not re.match(email_pattern, raw_email):
                send_telegram_message(chat_id, "❌ Invalid email format. Use: <code>/e name@company.com</code>")
                return
            mapping = resolve_reply_mapping(msg, chat_id, "/e")
            if not mapping:
                return
            new_email = raw_email
            job = get_job_by_sheet_uuid(mapping["sheet_uuid"])
            job, from_card = rebuild_job_from_card(job, (msg.get("reply_to_message") or {}).get("text", ""))
            if not _job_data_available(job, mapping):
                send_telegram_message(chat_id, STALE_CARD_WARNING)
                return
            if from_card:
                send_telegram_message(chat_id, CARD_RECOVERED_NOTICE)
            comp = job.get("employer_name") or mapping.get("contact_company") or "Target Firm"
            title = job.get("job_title") or "Operations Specialist"
            is_warm = mapping.get("sheet_tab") in ("Carmen Warm", "Carmen Cold")
            update_job_target_email(mapping["sheet_uuid"], new_email)

            # Compile the same tailored resume PDF /draft attaches, so /e never regresses to a bare-text draft
            track = job.get("track", "a")
            bullet_indices = job.get("bullet_indices")
            tone_mode = job.get("tone_mode", "conservative")
            pdf_filename = resume_pdf_filename(comp)
            pdf_bytes = compile_resume_pdf_resilient(chat_id, comp, track, bullet_indices, "/e", tone_mode=tone_mode)

            raw_email_text = resolve_outreach_body(job, mapping, title, comp, is_warm)
            ok, gmail_msg, draft_id = create_gmail_draft(
                to_email=new_email, company_name=comp, job_title=title, is_warm=is_warm,
                custom_body=raw_email_text, pdf_bytes=pdf_bytes, pdf_filename=pdf_filename
            )
            monospaced_body = format_email_block(raw_email_text)
            draft_link_line = ""
            if draft_id:
                draft_url = html.escape(f"https://mail.google.com/mail/u/0/#drafts/{draft_id}", quote=True)
                draft_link_line = f"📱 <a href='{draft_url}'>Open Draft in Gmail</a>\n\n"
            confirm_msg = (
                f"🎯 <b>Apollo Email Locked:</b> <code>{html.escape(new_email)}</code>\n\n"
                f"{draft_link_line}"
                f"<b>Tap-to-Copy Email Body:</b>\n{monospaced_body}"
            )
            # Optimistic UI: confirm to Telegram first, dispatch the Sheets write in the background
            send_telegram_message(chat_id, confirm_msg)
            if ok:
                log_daily_activity("drafts_staged")
            enqueue_crm_payload(build_crm_payload("update_contact_email", sheet_uuid=mapping["sheet_uuid"], email=new_email))
            # Typing the address IS the intent to track this person, so log them to Carmen Cold
            # without the company gate the passive sweep uses - that gate drops agency recruiters
            # at untracked firms, which is most of who /e gets used on.
            if log_addressed_contact_to_carmen_cold(
                new_email, company=comp, note=f"[{datetime.now().strftime('%Y-%m-%d')}] Emailed: {title}"
            ):
                send_telegram_message(chat_id, f"👤 Logged <code>{html.escape(new_email)}</code> to Carmen Cold.")
            return

        if text == "/backfillcontacts" or text == "/backfillcontacts go":
            # One-off sweep of the whole Sent backlog, which the rolling lookback window can never
            # reach. Previews by default: this writes to the same Carmen Cold tab Kevin curates by
            # hand, so the contact list is shown before anything is created.
            commit = text.endswith(" go")
            send_telegram_message(
                chat_id,
                ("📇 Scanning full Sent history and capturing..." if commit
                 else "📇 Scanning full Sent history (preview, nothing will be written)...")
            )
            def _backfill_and_notify():
                try:
                    n = capture_contacts_from_sent_mail(
                        lookback_hours=0, max_messages=1000, dry_run=not commit
                    )
                    if commit:
                        send_telegram_message(chat_id, f"✅ <b>Backfill complete.</b> {n} contact(s) added to Carmen Cold.")
                    else:
                        send_telegram_message(
                            chat_id,
                            f"🔍 <b>Preview:</b> {n} new contact(s) would be added.\n"
                            f"Check the logs for the list, then reply <code>/backfillcontacts go</code> to write them."
                        )
                except Exception as e:
                    logging.error(f"/backfillcontacts Error: {e}")
                    send_telegram_message(chat_id, f"❌ Backfill error: {html.escape(str(e)[:200])}")
            threading.Thread(target=_backfill_and_notify, daemon=True).start()
            return

        if text == "/poll":
            # On-demand email poll. Exists because the scheduled cadence is now daily (or off):
            # when a reply is expected right now, this runs the same cycle without waiting for it.
            # Off the request thread so the webhook still acknowledges instantly.
            send_telegram_message(chat_id, "📬 Running email poll cycle...")
            def _poll_and_notify():
                try:
                    scheduled_email_poll_job()
                    send_telegram_message(chat_id, "✅ Email poll cycle complete.")
                except Exception as e:
                    logging.error(f"/poll Error: {e}")
                    send_telegram_message(chat_id, f"❌ Poll error: {html.escape(str(e)[:200])}")
            threading.Thread(target=_poll_and_notify, daemon=True).start()
            return

        if text == "/prep":
            mapping = resolve_reply_mapping(msg, chat_id, "/prep")
            if not mapping:
                return
            job = get_job_by_sheet_uuid(mapping["sheet_uuid"])
            if not _job_data_available(job, mapping):
                send_telegram_message(chat_id, STALE_CARD_WARNING)
                return
            comp = job.get("employer_name") or mapping.get("contact_company") or "Target Firm"
            job_title = job.get("job_title") or "this role"
            prep = generate_interview_prep(comp, job_title, job.get("job_description", ""))
            talking_points_block = "\n".join(f"{i+1}. {tp}" for i, tp in enumerate(prep["talking_points"]))
            reverse_questions_block = "\n".join(f"{i+1}. {q}" for i, q in enumerate(prep["reverse_questions"]))
            prep_msg = (
                f"🎓 <b>Interview Prep - {html.escape(comp)}</b>\n\n"
                f"<b>💬 Talking Points:</b>\n{html.escape(talking_points_block)}\n\n"
                f"<b>❓ Reverse Questions:</b>\n{html.escape(reverse_questions_block)}"
            )
            send_telegram_message(chat_id, prep_msg)
            return

        if text == "/pitch":
            mapping = resolve_reply_mapping(msg, chat_id, "/pitch")
            if not mapping:
                return
            job = get_job_by_sheet_uuid(mapping["sheet_uuid"])
            if not _job_data_available(job, mapping):
                send_telegram_message(chat_id, STALE_CARD_WARNING)
                return
            comp = job.get("employer_name") or mapping.get("contact_company") or "Target Firm"
            job_title = job.get("job_title") or "this role"
            pitch = generate_elevator_pitch(comp, job_title)
            pitch_msg = (
                f"🎤 <b>30-Second Elevator Pitch - {html.escape(comp)}</b>\n\n"
                f"<code>{html.escape(pitch)}</code>"
            )
            send_telegram_message(chat_id, pitch_msg)
            return

        gear_match = re.match(r"^/gear(?:\s+([1-9]\d*))?$", text, re.IGNORECASE)
        if gear_match:
            if gear_match.group(1):
                config = apply_search_gear(gear_match.group(1))
                gear_num, _ = current_search_gear()
                send_telegram_message(
                    chat_id,
                    f"⚙️ <b>Gear {gear_num} - {config['label']}</b>\n{html.escape(config['blurb'])}\n\n"
                    "Run /t to source with the new breadth."
                )
            else:
                send_telegram_message(chat_id, describe_search_gear())
            return

        remote_match = re.match(r"^/remote(?:\s+(on|off))?$", text, re.IGNORECASE)
        if remote_match:
            action = (remote_match.group(1) or "").lower()
            if action == "on":
                set_filter("remote_feeds_enabled", True)
                send_telegram_message(chat_id, "✅ <b>Remote feeds ON</b> - /t will also pull RemoteOK, Himalayas, Remotive and WeWorkRemotely.")
            elif action == "off":
                set_filter("remote_feeds_enabled", False)
                send_telegram_message(chat_id, "🚫 <b>Remote feeds OFF</b> - /t stays local to the Detroit metro.")
            else:
                state = "ON" if get_filter("remote_feeds_enabled") else "OFF"
                cap = safe_int(get_filter("remote_feed_cap"), 40)
                send_telegram_message(
                    chat_id,
                    f"🌐 <b>Remote feeds: {state}</b> (cap {cap}/run)\n\n"
                    "Keyless public feeds: RemoteOK, Himalayas, Remotive, WeWorkRemotely.\n"
                    "These skip the Detroit city filter but keep every other gate.\n\n"
                    "/remote on · /remote off"
                )
            return

        ats_match = re.match(r"^/ats(?:\s+(on|off|list|add|remove)\s*(.*))?$", text, re.IGNORECASE)
        if ats_match:
            action = (ats_match.group(1) or "list").lower()
            arg = (ats_match.group(2) or "").strip().lower()
            slugs = safe_list(get_filter("ats_company_slugs", []))
            if action == "on":
                set_filter("ats_watchlist_enabled", True)
                send_telegram_message(chat_id, f"✅ <b>ATS watchlist ON</b> - /t will now also source {len(slugs)} company boards directly.")
            elif action == "off":
                set_filter("ats_watchlist_enabled", False)
                send_telegram_message(chat_id, "🚫 <b>ATS watchlist OFF</b> - /t sources JSearch + warm companies only.")
            elif action == "add" and arg:
                # Only persist a slug whose board actually resolves, so a typo never becomes a
                # permanent no-op costing three HTTP calls on every future run.
                added = []
                for slug in re.split(r"[\s,]+", arg):
                    if not slug or slug in slugs:
                        continue
                    found = len(fetch_greenhouse_jobs(slug)) + len(fetch_lever_jobs(slug)) + len(fetch_ashby_jobs(slug))
                    if found:
                        slugs.append(slug)
                        added.append(f"{slug} ({found})")
                set_filter("ats_company_slugs", slugs)
                send_telegram_message(chat_id, f"➕ Added: {html.escape(', '.join(added))}" if added else "⚠️ No board resolved for that slug.")
            elif action == "remove" and arg:
                removed = [s for s in re.split(r"[\s,]+", arg) if s in slugs]
                slugs = [s for s in slugs if s not in removed]
                set_filter("ats_company_slugs", slugs)
                send_telegram_message(chat_id, f"➖ Removed: {html.escape(', '.join(removed))}" if removed else "⚠️ Not on the list.")
            else:
                state = "ON" if get_filter("ats_watchlist_enabled") else "OFF"
                send_telegram_message(
                    chat_id,
                    f"📋 <b>ATS Watchlist ({state})</b> - {len(slugs)} companies\n\n"
                    f"<code>{html.escape(', '.join(slugs)) or 'empty'}</code>\n\n"
                    "/ats on · /ats off · /ats add &lt;slug&gt; · /ats remove &lt;slug&gt;"
                )
            return

        if text == "/letter":
            mapping = resolve_reply_mapping(msg, chat_id, "/letter")
            if not mapping:
                return
            job = get_job_by_sheet_uuid(mapping["sheet_uuid"])
            if not _job_data_available(job, mapping):
                send_telegram_message(chat_id, STALE_CARD_WARNING)
                return
            comp = job.get("employer_name") or mapping.get("contact_company") or "Target Firm"
            job_title = job.get("job_title") or "this role"
            # Reuse the resume's own routing so the letter and the attached PDF argue one case.
            track = job.get("track", "a")
            indices = job.get("bullet_indices") or [0]
            letter_index = indices[0] if isinstance(indices, list) and indices and isinstance(indices[0], int) else 0
            city = str(job.get("job_city") or "").strip()
            state = str(job.get("job_state") or "").strip()
            job_location = ", ".join([p for p in (city, state) if p])
            letter = generate_cover_letter(
                comp, job_title, track, letter_index, job_location,
                job.get("tone_mode", "conservative"),
            )
            letter_msg = (
                f"✉️ <b>Cover Letter - {html.escape(comp)}</b> · Track {html.escape(str(track).upper())}\n\n"
                f"<code>{html.escape(letter)}</code>"
            )
            send_telegram_message(chat_id, letter_msg)
            return

        cv_match = re.match(r"^/(cv|resume)(?:\s+([a-eA-E]))?$", text, re.IGNORECASE)
        if cv_match:
            requested_track = (cv_match.group(2) or "").lower()
            mapping = resolve_reply_mapping(msg, chat_id, cv_match.group(0).split()[0])
            if not mapping:
                return
            job = get_job_by_sheet_uuid(mapping["sheet_uuid"])
            if not _job_data_available(job, mapping):
                send_telegram_message(chat_id, STALE_CARD_WARNING)
                return

            # Fallback to the networking-record mapping (e.g. /quick contacts with no cached job) instead of blocking
            comp = job.get("employer_name") or mapping.get("contact_company") or "Target Company"
            track = requested_track or job.get("track") or "a"
            bullet_indices = job.get("bullet_indices")
            tone_mode = job.get("tone_mode", "conservative")
            short_id = job.get("short_id") or generate_short_key(job.get("job_id") or mapping["sheet_uuid"], fallback=time.time())

            try:
                pdf_bytes = compile_resume_pdf(comp, track=track, bullet_indices=bullet_indices, tone_mode=tone_mode)
                filename = resume_pdf_filename(comp)

                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
                files = {"document": (filename, io.BytesIO(pdf_bytes), "application/pdf")}
                caption_text = (
                    f"📄 <b>Tailored Resume ({track.upper()}): {html.escape(comp)}</b>\n\n"
                    f"🖥️ <b>Desktop Staging Link:</b>\n"
                    f"<code>{html.escape(f'{BASE_URL}/stage/{short_id}?track={track}')}</code>"
                )
                requests.post(url, data={"chat_id": chat_id, "caption": caption_text, "parse_mode": "HTML"}, files=files, timeout=10)
            except Exception as e:
                send_telegram_message(chat_id, f"❌ Resume Compilation Error: <code>{html.escape(str(e))}</code>")
            return

        if text == "/apply":
            mapping = resolve_reply_mapping(msg, chat_id, "/apply")
            if not mapping:
                return
            sheet_uuid = mapping["sheet_uuid"]
            job = get_job_by_sheet_uuid(sheet_uuid)
            company = mapping.get("contact_company") or job.get("employer_name")
            if company:
                _APPLIED_CRM_CACHE["data"].add(str(company).strip().lower())
                normalized_company = normalize_company_for_match(company)
                if normalized_company:
                    _APPLIED_CRM_CACHE["data"].add(normalized_company)
                add_company_cooldown(company)
                upsert_company_identity(company, crm_status="Tetiana Warm", applied=True)
            applied_date = datetime.now().strftime("%Y-%m-%d")
            reply_card = msg.get("reply_to_message") or {}
            if reply_card.get("message_id"):
                original_text = html.escape(reply_card.get("text", ""))
                edit_telegram_message(chat_id, reply_card["message_id"], f"{original_text}\n\n✅ <b>Applied - {applied_date}</b>")
            # Optimistic UI: confirm to Telegram first, dispatch the Sheets write in the background
            send_telegram_message(chat_id, f"✅ Applied - {applied_date}")
            log_metric_event("applied", sheet_uuid)
            log_daily_activity("applied_count")
            record_application_outcome(
                sheet_uuid, "applied",
                company=company, role=job.get("job_title"),
                source=derive_job_source(job.get("job_id")),
                outreach_path="warm" if mapping.get("contact_name") else "ats"
            )
            # Canonical Status write on the row in place - no tab move (see set_status in Code.gs).
            enqueue_crm_payload(build_crm_payload("set_status", sheet_uuid=sheet_uuid, status="Applied"))
            return

        if text in ("/offer", "/withdraw"):
            mapping = resolve_reply_mapping(msg, chat_id, text)
            if not mapping:
                return
            sheet_uuid = mapping["sheet_uuid"]
            status = "offer" if text == "/offer" else "withdrawn"
            confirm_text = "🎉 <b>Offer Logged!</b>" if status == "offer" else "🚪 <b>Application Withdrawn.</b>"
            send_telegram_message(chat_id, confirm_text)
            record_application_outcome(sheet_uuid, status, company=mapping.get("contact_company"))
            return

        if text in ("/warm", "/cold"):
            mapping = resolve_reply_mapping(msg, chat_id, text)
            if not mapping:
                return
            sheet_uuid = mapping["sheet_uuid"]
            direction = "warm" if text == "/warm" else "cold"
            new_tab = resolve_smart_target_tab(mapping.get("sheet_tab"), direction)
            confirm_text = "🔥 Moved to Warm" if direction == "warm" else "🧊 Moved to Cold"

            # /warm on a JOB row also starts the follow-up ladder. Moving a row to Tetiana Warm is
            # how Kevin marks "I have engaged with this one", but the tab is a LOCATION and the
            # ladder keys off Status: followup_action() returns "none" for Matched at every age, so
            # a row could sit in Warm for weeks having been emailed and still report Overdue: 0.
            # Carmen tabs are excluded - a networking contact is not an application, and the
            # Carmen ladder (plan_carmen_followup) drives those rows instead.
            starts_ladder = direction == "warm" and new_tab == "Tetiana Warm"
            if starts_ladder:
                confirm_text += " · Applied (follow-up ladder started)"

            # Optimistic UI: confirm to Telegram first, dispatch the Sheets write in the background
            send_telegram_message(chat_id, confirm_text)
            # Auto-ATS Expansion: Carmen-family contacts (Cold or Warm) get monitored for future /t job runs
            if new_tab.startswith("Carmen") and mapping.get("contact_company"):
                threading.Thread(target=auto_expand_ats_slug, args=(mapping["contact_company"],), daemon=True).start()
            enqueue_crm_payload(build_crm_payload("update_status", sheet_uuid=sheet_uuid, new_tab=new_tab))
            if starts_ladder:
                # Queued AFTER the move so the row is in its destination tab when the Status write
                # lands; set_status finds the row by sheet_uuid in whatever tab it now lives in.
                enqueue_crm_payload(build_crm_payload("set_status", sheet_uuid=sheet_uuid, status="Applied"))
                # Re-anchor the follow-up date onto the ladder. Cards are created with a
                # priority-derived date (calculate_followup_interval(5) = +19d), and
                # followup_action() hard-skips any row whose Next Followup Date is still in the
                # future - so without this the row would sit Applied and silently overdue-free for
                # another two weeks. FOLLOWUP_1_DAYS from today puts it on rung 1.
                ladder_start = (datetime.now() + timedelta(days=FOLLOWUP_1_DAYS)).strftime("%Y-%m-%d")
                enqueue_crm_payload(build_crm_payload("update_snooze", sheet_uuid=sheet_uuid, next_followup=ladder_start))
                log_metric_event("applied", sheet_uuid)
                log_daily_activity("applied_count")
                company = mapping.get("contact_company")
                if company:
                    _APPLIED_CRM_CACHE["data"].add(str(company).strip().lower())
                    normalized_company = normalize_company_for_match(company)
                    if normalized_company:
                        _APPLIED_CRM_CACHE["data"].add(normalized_company)
                    add_company_cooldown(company)
                    upsert_company_identity(company, crm_status="Tetiana Warm", applied=True)
            return

        if text == "/x":
            mapping = resolve_reply_mapping(msg, chat_id, "/x")
            if not mapping:
                return
            sheet_uuid = mapping["sheet_uuid"]
            new_tab = resolve_smart_target_tab(mapping.get("sheet_tab"), "kill")
            # Optimistic UI: confirm to Telegram first, dispatch the Sheets write in the background
            send_telegram_message(chat_id, f"❌ Archived to {new_tab}.")
            enqueue_crm_payload(build_crm_payload("update_status", sheet_uuid=sheet_uuid, new_tab=new_tab))
            return

        # Canonical Status advance by short_id (no reply context): /replied <id>, /interview <id>.
        # Resolves the short_id to a sheet_uuid the same way callbacks do (get_sheet_uuid_by_short_id)
        # and writes only the Status field - never a tab move.
        status_cmd_match = re.match(r"^/(replied|interview)(?:\s+(\S+))?$", text)
        if status_cmd_match:
            cmd, short_id = status_cmd_match.group(1), (status_cmd_match.group(2) or "").strip()
            new_status = "Replied" if cmd == "replied" else "Interviewing"
            if not short_id:
                send_telegram_message(chat_id, f"⚠️ <b>Usage:</b> <code>/{cmd} &lt;short_id&gt;</code>")
                return
            sheet_uuid = get_sheet_uuid_by_short_id(short_id)
            if not sheet_uuid:
                send_telegram_message(
                    chat_id,
                    f"⚠️ <b>Record Not Found:</b> No CRM record is mapped to <code>{html.escape(short_id)}</code> "
                    f"for <code>/{cmd}</code>. Please retry with /t or /c to regenerate it."
                )
                return
            # Optimistic UI: confirm to Telegram first, dispatch the Sheets write in the background
            send_telegram_message(chat_id, f"✅ {new_status} - {datetime.now().strftime('%Y-%m-%d')}")
            enqueue_crm_payload(build_crm_payload("set_status", sheet_uuid=sheet_uuid, status=new_status))
            return

        # Deterministic Template Bank Editor (/edit ID New Text) - no reply context required
        if text.startswith("/edit"):
            body = text[5:].strip()
            parts = body.split(None, 1)
            if len(parts) < 2:
                send_telegram_message(
                    chat_id,
                    "❌ <b>Usage:</b> <code>/edit ID New Text</code>\n"
                    "IDs: <code>L0-L5</code> (LinkedIn), <code>C0-C2</code> (Cold), "
                    "<code>W0-W1</code> (Warm), <code>B0-B1</code> (Bump), "
                    "<code>TA0-TA9</code>...<code>TE0-TE9</code> (Resume Bullets)"
                )
                return
            edit_id, new_text = parts[0], parts[1].strip()
            target = resolve_edit_target(edit_id)
            if not target:
                send_telegram_message(chat_id, f"❌ Unknown template ID: <code>{html.escape(edit_id)}</code>. Valid: L0-L5, C0-C2, W0-W1, B0-B1, TA0-TA9...TE0-TE9.")
                return
            file_path, list_key, idx = target
            ok, result_msg = update_template_entry(file_path, list_key, idx, new_text)
            send_telegram_message(chat_id, result_msg)
            return

        # Muscle Memory Safety Net: catches old finger-memory taps of retired swipe commands
        # ("/cw"/"/cc" are NOT retired - they're live Networking Card pull triggers handled in
        # section 3 above, which always matches first and returns before reaching this block)
        if text in ["/tw", "/tc", "/conv", "/int", "/pivot"]:
            send_telegram_message(
                chat_id,
                f"⚠️ <code>{html.escape(text)}</code> has been retired. Use <code>/warm</code>, <code>/cold</code>, or <code>/apply</code> instead."
            )
            return

        # 10. Help. /help asks for the menu; any other unrecognized slash command gets the same menu
        # behind an "unrecognized" header instead of silence. The job card links here rather than
        # reprinting the swipe legend on every send, so this is now the canonical command reference.
        if text.startswith("/"):
            send_telegram_message(
                chat_id,
                ("📖 <b>Command Reference</b>\n\n" if text == "/help" else "⚠️ <b>Command Unrecognized</b>\n\n") +
                "<b>CORE COMMANDS:</b>\n"
                "/t - Pull fresh job cards\n"
                "/job, /j &lt;url&gt; - Add a job you found yourself (scores &amp; files it like /t)\n"
                "/w - Warm radar: new roles at your warm-contact companies (no AI, instant)\n"
                "/search - View or update live search filters\n"
                "/quick - Create contact (Name @ Firm Priority Note)\n"
                "/cold, /warm - Quick-add a Cold/Warm contact (Name @ Firm Priority Note)\n"
                "/edit - Edit a template (e.g. /edit L0 New note)\n"
                "/ecosystem, /eco add - View/expand tracked ATS boards\n\n"
                "<b>PULL CRM DATA:</b>\n"
                "/c - Pull combined networking cards\n"
                "/cw - Pull Warm Rolodex cards\n"
                "/cc - Pull Cold VP Sprint cards\n"
                "/p - Query priority tier contacts\n\n"
                "<b>SWIPE-REPLY ACTIONS (reply to a card):</b>\n"
                "Every card carries a 📋 Full Card link - ATS bullets, LinkedIn note, cold draft,\n"
                "decision-maker searches and the tailored PDF, each with a copy button.\n"
                "/apply - Mark Applied (Status only, no tab move)\n"
                "/replied &lt;id&gt; - Set Status to Replied\n"
                "/interview &lt;id&gt; - Set Status to Interviewing\n"
                "/offer - Log an offer for this record\n"
                "/withdraw - Log a withdrawn application\n"
                "/warm - Smart-route lead to its Warm tab\n"
                "/cold - Smart-route lead to its Cold tab\n"
                "/x - Archive lead to Died/Killed tab\n"
                "/n - Append timestamped note\n"
                "/f - Snooze follow-up by [days]\n"
                "/e <email> - Lock Apollo email override & re-draft\n"
                "/eh [Name] - On-demand API email lookup & re-draft\n"
                "/draft - Generate Gmail draft\n"
                "/cv, /resume - Compile tailored resume PDF\n"
                "/prep - Interview talking points & reverse questions\n"
                "/pitch - 30-second elevator pitch\n"
                "/letter - Cover letter (same track as the resume)\n"
                "/gear - Search breadth 1-5 (one dial for all sources)\n"
                "/ats - Company board watchlist (on/off/add/remove)\n"
                "/remote - Keyless remote feeds (on/off)\n"
                "/poll - Run the email poll cycle now (scheduled: daily)\n"
                "/backfillcontacts - Preview a full Sent-history contact sweep (add 'go' to write)\n\n"
                "<b>TUESDAY BATCH HUB:</b>\n"
                "/sendall - Draft bumps + queue eligible overdue records to +14 days\n"
                "/snoozeall [days] - Move every overdue follow-up by 7 days (or the specified number)\n"
                "/overdue - Full overdue list (the morning digest shows only the 10 most overdue)\n\n"
                "<b>TELEMETRY:</b>\n"
                "/health - View system telemetry and status\n"
                "/efficiency - View Input to Interview Golden Ratio\n"
                "/funnel - View pipeline conversion funnel\n"
                "/queue - Preview what the nightly follow-up sequencer would do (read-only)\n"
                "/outcomes - View evidence-based reply/interview rates by source & path\n"
                "/treplies - View reply rate grouped by outreach & LinkedIn template id (read-only)\n"
                "/streak, /daily - View daily outreach scorecard\n"
                "/help - Show this reference"
            )
            return

    except Exception as e:
        logging.error(f"Async Webhook Processing Error: {e}")

def _is_oneshot_invocation():
    """True when this process was launched as `python main.py --once` (the CI batch entrypoint).

    Reads sys.argv directly rather than the argparse result in __main__: the scheduler block below
    runs while this module is still being imported, which is strictly before __main__ executes, so
    an args object would not exist yet. sys.argv is populated by the interpreter before any module
    code runs at all, making it the earliest thing that can answer the question.
    """
    return "--once" in sys.argv[1:]

# Background daemons belong to the long-lived Flask server only. Two exclusions, both at import
# time: pytest (must never poll Gmail or write to the live CRM) and --once (a one-shot batch run -
# these threads would keep the process alive past the pipeline and could write to the CRM mid-run).
if not os.environ.get("PYTEST_CURRENT_TEST") and not _is_oneshot_invocation():
    start_gmail_poller()
    start_crm_outbox_worker()
    start_morning_digest()
    start_backup_scheduler()
    start_followup_sequencer()

# ==============================================================================
# 10. FLASK SERVER & STACKED WEBHOOK ROUTER
# ==============================================================================
@app.route('/', methods=['GET'])
@app.route('/health', methods=['GET'])
def health_check():
    """Return JSON health status in <5ms."""
    start_time = time.time()
    try:
        # Quick SQLite WAL check
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode")
            mode = cursor.fetchone()[0]
        persistence = get_persistence_status()
        elapsed_ms = (time.time() - start_time) * 1000
        return jsonify({
            "status": "ok",
            "mode": mode,
            "elapsed_ms": round(elapsed_ms, 2),
            "persistence": persistence,
        }), 200
    except Exception as e:
        elapsed_ms = (time.time() - start_time) * 1000
        return jsonify({"status": "error", "error": str(e), "elapsed_ms": round(elapsed_ms, 2)}), 500

def _run_pipeline_and_notify(chat_id, qty):
    """Background thread target for /t: runs the heavy pipeline off the request thread,
    in its own isolated daemon thread, then posts the completion message.
    """
    try:
        count = run_job_pipeline(chat_id, top_n=qty)
        send_telegram_message(chat_id, f"🏁 Pipeline Completed. {count} cards dispatched.")
    except Exception as e:
        logging.error(f"/t Background Pipeline Error: {e}")
        send_telegram_message(chat_id, f"❌ Pipeline error: {html.escape(str(e)[:200])}")

def _run_overdue_batch_and_notify(chat_id, mode, snooze_days):
    """Background target for the Tuesday batch-hub commands so webhook acknowledgement remains instant."""
    try:
        result, next_followup = process_overdue_batch(mode, snooze_days)
        if mode == "sendall":
            send_telegram_message(
                chat_id,
                f"✅ <b>Send-All Complete</b>\n"
                f"• Overdue records found: {result['total']}\n"
                f"• New bump drafts: {result['drafted']}\n"
                f"• CRM follow-ups queued to {next_followup}: {result['updated']}\n"
                f"• Skipped (missing/unverified email or draft failure): {result['skipped']}"
            )
        else:
            send_telegram_message(
                chat_id,
                f"✅ <b>Snooze-All Complete</b>\n"
                f"• Overdue records: {result['total']}\n"
                f"• CRM follow-ups queued to {next_followup}: {result['updated']}"
            )
    except Exception as e:
        logging.error(f"/{mode} batch error: {e}")
        send_telegram_message(chat_id, f"❌ <b>Batch Error:</b> {html.escape(str(e)[:200])}")

@app.route("/telegram", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    """
    Instant non-blocking execution (<0.05s return). Validates Telegram's secret token header,
    then spawns an isolated daemon thread immediately (no bounded queue/worker pool - unbounded
    thread-per-update). process_webhook_payload_async() is the single source of truth for every command.
    """
    webhook_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if webhook_secret:
        incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if incoming_secret != webhook_secret:
            logging.warning("Telegram Webhook Rejected: invalid secret token")
            return jsonify({"status": "error", "message": "Unauthorized"}), 403

    try:
        data = request.get_json(silent=True)
        if not data:
            logging.warning("Telegram Webhook: empty or non-JSON payload received - ignored")
            return jsonify({"status": "ignored"}), 200

        update_kind = "callback_query" if "callback_query" in data else ("message" if "message" in data else "unknown")
        logging.info(f"Telegram Webhook: received update_kind={update_kind}")

        threading.Thread(target=process_webhook_payload_async, args=(data,), daemon=True).start()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logging.error(f"Telegram Webhook Dispatch Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 200

def format_ats_plaintext(job, track="a"):
    """Builds a plain-text ATS-safe fallback block (identity/experience/education/bullets) for the
    staging portal textarea. Experience/education are dynamically pulled from the hot-reloaded
    Evidence Bank, and the achievement bullets go through resume_engine's filter_ats_bullets() so
    this text can never drift from what the compiled Typst PDF actually renders.
    """
    evidence = load_evidence_bank()
    identity = evidence.get("identity", {})
    lines = [str(identity.get("name", "Kevin Miller"))]
    contact_bits = [identity.get("email", ""), identity.get("phone", ""), identity.get("location", "")]
    lines.append(" | ".join(b for b in contact_bits if b))
    lines.append("")

    lines.append("PROFESSIONAL EXPERIENCE")
    for job_entry in evidence.get("experience", []):
        lines.append(f"{job_entry.get('title', '')} | {job_entry.get('company', '')} ({job_entry.get('start', '')} - {job_entry.get('end', '')})")
        for b in job_entry.get("bullets", []):
            lines.append(f"- {b}")
        lines.append("")

    lines.append("EDUCATION")
    for edu in evidence.get("education", []):
        lines.append(f"{edu.get('school', '')} | {edu.get('degree', '')} ({edu.get('start', '')} - {edu.get('end', '')})")
        for c in edu.get("credentials", []):
            lines.append(f"- {c}")
        lines.append("")

    lines.append("TARGETED ACHIEVEMENTS")
    validated_bullets = filter_ats_bullets(track, job.get("bullet_indices"), job.get("tone_mode", "conservative"))
    for b in validated_bullets:
        lines.append(f"- {b}")

    return "\n".join(lines).strip()

@app.route("/stage/<short_id>", methods=["GET"])
def desktop_stage_view(short_id):
    """Desktop review page - the card's "Full Card" target. Everything the Telegram card used to
    print inline lives here: fit reason, matched skills, tailored bullets, then one combined
    "find someone, then message them" block (the five research dorks followed immediately by the
    LinkedIn note), the cold draft, the ATS fallback text, and the PDF preview. Each copy block
    is a readonly textarea plus a Copy button, which beats tap-to-copy on a Telegram <code> span.
    """
    job = get_job_from_cache(short_id)
    if not job:
        # Nothing here has a TTL - the jobs cache sits on Render's ephemeral disk and is wiped by
        # every deploy/restart, so say that instead of blaming an expiry that does not exist.
        return (
            "<div style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:520px;"
            "margin:80px auto;padding:0 20px;color:#333;'>"
            "<h3 style='margin-bottom:8px;'>This card's data is gone.</h3>"
            "<p style='color:#666;line-height:1.5;'>It predates the last deploy/restart, which "
            "wipes the job cache. Reply <code>/t</code> in Telegram to resurface fresh cards.</p>"
            "</div>"
        ), 404

    track = request.args.get("track") or job.get("track") or "a"
    comp = job.get("employer_name", "Target Firm")
    title = job.get("job_title", "Role")
    apply_link = job.get("job_apply_link", "#")
    bullets = filter_ats_bullets(track, job.get("bullet_indices"), job.get("tone_mode", "conservative"))
    bullets_html = "".join([f"<li>{html.escape(str(b))}</li>" for b in bullets])
    ats_plaintext = format_ats_plaintext(job, track)

    linkedin_note, outreach_email = resolve_outreach_copy(job)
    fit_reason = str(job.get("fit_reason") or "Not recorded for this job.")
    matched_skills = job.get("matched_skills") or []
    matched_str = ", ".join(str(s) for s in matched_skills[:8]).title() if matched_skills else "General Ops"
    fit_score = job.get("fit_score")
    score_str = f"{fit_score}/100" if fit_score is not None else "n/a"

    # Same five decision-maker searches the card used to carry on two link lines. Built from the
    # raw company name here - the card was passing the HTML-escaped name into these builders.
    research_links = [
        ("Apollo", build_apollo_url(comp)),
        ("LinkedIn Leadership Search", build_linkedin_url(comp)),
        ("🎓 Hope College Alumni", build_alumni_dork(comp)),
        ("Hiring Manager", build_hiring_manager_dork(comp, title)),
        ("Recruiter", build_recruiter_dork(comp)),
    ]
    research_html = "".join(
        f'<a class="btn btn-secondary" href="{html.escape(url, quote=True)}" target="_blank">{html.escape(label)}</a>'
        for label, url in research_links
    )

    html_page = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Resume Stage: {html.escape(comp)}</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 40px; background: #f8f9fa; color: #212529; }}
            .card {{ background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.08); max-width: 750px; margin: auto; }}
            h2 {{ color: #1B2A4A; margin-top: 0; }}
            .btn {{ display: inline-block; padding: 10px 18px; margin-right: 10px; border-radius: 6px; text-decoration: none; font-weight: bold; }}
            .btn-primary {{ background: #1B2A4A; color: white; }}
            .btn-secondary {{ background: #e9ecef; color: #333; margin-bottom: 8px; }}
            .meta {{ color: #444; line-height: 1.5; }}
            ul {{ line-height: 1.6; }}
            iframe {{ width: 100%; height: 500px; border: 1px solid #ddd; margin-top: 20px; border-radius: 4px; }}
            textarea {{ box-sizing: border-box; border: 1px solid #ddd; border-radius: 4px; padding: 10px; margin-top: 8px; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h2>{html.escape(title)} @ {html.escape(comp)}</h2>
            <p class="meta"><b>Fit {html.escape(score_str)}</b> - {html.escape(fit_reason)}</p>
            <p class="meta"><b>Matched Skills:</b> {html.escape(matched_str)}</p>

            <p><b>Targeted ATS Bullets:</b></p>
            <ul>{bullets_html}</ul>
            <div style="margin-top: 20px;">
                <a class="btn btn-primary" href="/stage/{short_id}/pdf?track={track}" download="{html.escape(resume_pdf_filename(comp))}">⬇️ Download Tailored PDF</a>
                <a class="btn btn-secondary" href="{html.escape(apply_link)}" target="_blank">🔗 Open Application Portal</a>
            </div>

            <h3 style="margin-top: 24px;">🎯 Find Someone, Then Message Them</h3>
            <div class="research-links">{research_html}</div>
            <p class="meta" style="margin-top: 12px;">Find a name above, then copy the note below and paste it straight into their LinkedIn connection request - it's already under the 300-character limit.</p>
            <textarea id="linkedin-note" rows="5" style="width: 100%;" readonly>{html.escape(linkedin_note)}</textarea>
            <div style="margin-top: 10px;">
                <button class="btn btn-secondary" onclick="copyField('linkedin-note')" style="border: none; cursor: pointer;">📋 Copy LinkedIn Note</button>
            </div>

            <h3 style="margin-top: 24px;">✉️ Cold Outreach Draft</h3>
            <textarea id="cold-draft" rows="12" style="width: 100%;" readonly>{html.escape(outreach_email)}</textarea>
            <div style="margin-top: 10px;">
                <button class="btn btn-secondary" onclick="copyField('cold-draft')" style="border: none; cursor: pointer;">📋 Copy Cold Draft</button>
            </div>

            <h3 style="margin-top: 24px;">Raw ATS Text (Workday / Taleo fallback)</h3>
            <p style="color: #666; font-size: 0.9em;">Legacy ATS parsers sometimes fail to read the PDF - paste this plain-text version into application forms instead.</p>
            <textarea id="ats-raw-text" rows="15" style="width: 100%; font-family: monospace;" readonly>{html.escape(ats_plaintext)}</textarea>
            <div style="margin-top: 10px;">
                <button class="btn btn-secondary" onclick="copyField('ats-raw-text')" style="border: none; cursor: pointer;">📋 Copy ATS Text</button>
            </div>

            <h3 style="margin-top: 24px;">Tailored PDF Preview</h3>
            <iframe src="/stage/{short_id}/pdf?track={track}"></iframe>
        </div>
        <script>
            function copyField(elementId) {{
                const textarea = document.getElementById(elementId);
                textarea.select();
                textarea.setSelectionRange(0, 99999);
                navigator.clipboard.writeText(textarea.value);
            }}
        </script>
    </body>
    </html>
    """
    return html_page, 200

@app.route("/stage/<short_id>/pdf", methods=["GET"])
def desktop_stage_pdf(short_id):
    """Serves raw PDF bytes for browser preview and download."""
    job = get_job_from_cache(short_id)
    if not job:
        # Same restart-wipe cause as the /stage page above - not a TTL expiry.
        return (
            "<div style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:520px;"
            "margin:80px auto;padding:0 20px;color:#333;'>"
            "<h3 style='margin-bottom:8px;'>This resume's source data is gone.</h3>"
            "<p style='color:#666;line-height:1.5;'>The card predates the last deploy/restart, "
            "which wipes the job cache. Reply <code>/t</code> in Telegram to resurface fresh "
            "cards, then re-run <code>/cv</code>.</p>"
            "</div>"
        ), 404
    track = request.args.get("track") or job.get("track") or "a"
    comp = job.get("employer_name", "Target Firm")
    bullet_indices = job.get("bullet_indices")
    tone_mode = job.get("tone_mode", "conservative")
    pdf_bytes = compile_resume_pdf(comp, track=track, bullet_indices=bullet_indices, tone_mode=tone_mode)
    return Response(pdf_bytes, mimetype="application/pdf")

@app.route("/ingest", methods=["POST"])
def desktop_ingest():
    """Secure endpoint for desktop bookmarklet ingestion of manual job links/text.

    The bookmarklet scrapes the page inside Kevin's logged-in browser, so unlike the Telegram
    /job command it can hand over the full job description that LinkedIn refuses to serve to a
    server-side fetch. Both funnel into ingest_manual_job(), which is what actually writes the
    Tetiana Cold row - this endpoint previously carded without ever writing one.
    """
    ingest_secret = os.environ.get("INGEST_SECRET")
    if ingest_secret:
        incoming_secret = request.headers.get("X-Ingest-Secret") or request.args.get("secret")
        if incoming_secret != ingest_secret:
            return jsonify({"status": "error", "message": "Unauthorized"}), 403

    try:
        data = request.get_json(silent=True) or {}
        raw_text = str(data.get("text") or "").strip()
        url = str(data.get("url") or "").strip()
        title = str(data.get("title") or "").strip()
        company = str(data.get("company") or "").strip()
        if not raw_text and not url:
            return jsonify({"status": "error", "message": "No job text or URL provided"}), 400

        # The bookmarklet often sends the raw document.title verbatim; when it carries LinkedIn's
        # "Company hiring Title in City" shape, recover both halves rather than storing the whole
        # string as the job title.
        if url and not (title and company):
            parsed_title, parsed_company, parsed_desc = parse_job_page_html(raw_text) if raw_text else ("", "", "")
            title = title or parsed_title
            company = company or parsed_company
            if parsed_desc and len(parsed_desc) > len(raw_text or ""):
                raw_text = parsed_desc

        def _process_and_dispatch(u=url, t=title, c=company, d=raw_text):
            try:
                ok, message = ingest_manual_job(
                    url=u, title=t, company=c, description=d,
                    chat_id=TELEGRAM_CHAT_ID, source_label="bookmarklet",
                )
                if not ok and message and TELEGRAM_CHAT_ID:
                    send_telegram_message(TELEGRAM_CHAT_ID, message)
            except Exception as e:
                logging.error(f"Ingest Dispatch Error: {e}", exc_info=True)

        threading.Thread(target=_process_and_dispatch, daemon=True).start()
        return jsonify({"status": "ok", "message": "Ingestion queued"}), 200
    except Exception as e:
        logging.error(f"Ingest Endpoint Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Job outreach engine: Flask server, or a one-shot batch pipeline run.")
    parser.add_argument(
        "--once", action="store_true",
        help="Run one job pipeline pass, then exit (the CI / workflow_dispatch entrypoint). "
             "Background daemons stay off - see _is_oneshot_invocation(). Without this flag the "
             "Flask server starts as before."
    )
    parser.add_argument(
        "--top-n", type=int, default=2,
        help="Cards to dispatch in --once mode (default 2, matching /t's qty default and "
             "run_job_pipeline's own signature)."
    )
    args = parser.parse_args()

    if args.once:
        logging.info(f"[ONCE] One-shot pipeline run starting (top_n={args.top_n}, no background daemons)")
        try:
            dispatched = run_job_pipeline(chat_id=TELEGRAM_CHAT_ID, top_n=args.top_n)
        except Exception as e:
            # Exit non-zero so a failed run shows red in Actions instead of passing silently.
            logging.error(f"[ONCE] Pipeline run failed: {e}", exc_info=True)
            sys.exit(1)
        # A run that dispatches nothing is a valid outcome (no new matches), not a failure.
        logging.info(f"[ONCE] Pipeline run completed: {dispatched} cards dispatched")
        sys.exit(0)

    app.run(host="0.0.0.0", port=5000)
