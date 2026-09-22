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
from flask import Flask, jsonify, request, Response, redirect
from apscheduler.schedulers.background import BackgroundScheduler
from resume_engine import compile_resume_pdf, compile_cover_letter_pdf, filter_ats_bullets, TRACK_BULLET_POOL_KEYS
from response_schema import GeminiJobScreenerResponse
from pipeline_utils import (
    build_apollo_url, build_linkedin_url, build_linkedin_company_posts_url, build_hiring_manager_dork, build_recruiter_dork,
    build_alumni_dork, normalize_priority_value, calculate_followup_interval,
    resolve_smart_target_tab, enforce_sentence_limit, get_fit_score_indicator,
    generate_dedup_hash, normalize_dedup_key, generate_short_key, parse_posted_hours, get_age_badge,
    extract_salary, extract_work_style, compute_description_simhash, resolve_email_waterfall,
    derive_job_source, is_unverified_email, status_rank, STATUS_VOCAB,
    followup_action, followup_anchor, is_followup_unscheduled,
    lint_outreach_template, advise_outreach_template,
    is_probable_company_name, ats_slug_guess, build_sent_contact,
    is_guessed_contact_email, resolve_sent_email_backfill,
    is_role_mailbox, is_automated_sender, company_domain_of, name_from_email_local_part, parse_email_recipient,
    match_email_to_crm_company,
    plan_carmen_ladder, carmen_reply_anchor, CARMEN_LADDER_DAYS,
    CARMEN_LADDER_DAYS_COLD, CARMEN_LADDER_DAYS_ENGAGED,
    carmen_status_marker, carmen_marker_cell,
    INBOUND_REPLY_NOTE_MARKER, LADDER_RESTART_NOTE_MARKER, MAX_AUTO_KILLS_PER_RUN,
    is_expired_matched_row, MATCHED_EXPIRY_DAYS,
    classify_job_link, may_auto_retire, is_opaque_job_host,
    parse_job_command, parse_job_page_html, build_ingest_job_dict, extract_jd_terms,
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
# Telegram's hard sendMessage limit. A message over it is rejected outright, not trimmed, so
# anything that can grow without bound splits into a second message instead of being truncated.
TELEGRAM_MAX_MESSAGE_CHARS = 4096
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
JSEARCH_TIMEOUT_SECONDS = 40  # see JSEARCH_SEMAPHORE - queued requests spend most of this waiting
JSEARCH_MAX_RETRIES = 1  # additional attempts beyond the first, on timeout/429/5xx
# Every run fetches pages 1..JSEARCH_PAGES_PER_RUN. There is no rolling offset, deliberately.
#
# JSearch orders by relevance, so page 1 holds a metro-scoped query's best matches and the tail is
# noise. A rolling pointer meant that on alternating runs a query SKIPPED page 1 to fetch page 3
# alone - trading its best results for its worst - and because the pointer advanced 3 pages per run
# and only wrapped at 20, queries drifted into pages 4+ where every request timed out. 99 of 110
# queries were stranded there, returning zero listings, and the runs looked like a filter problem.
#
# Re-fetching page 1 every run costs nothing now: in-run dedup collapses repeats, and cross-run
# suppression fires only on roles already in Tetiana Cold/Warm, so a rediscovered listing Kevin
# never acted on still produces a card. Measured at 2 pages: 149 raw -> 23 passed and 179 raw -> 40
# passed, 18 cards across two slices.
JSEARCH_PAGES_PER_RUN = 2
# 2, not 4. The timeout clock starts when the REQUEST is issued, not when the semaphore admits it,
# so a queued request burns its window waiting for a slot. Ten queries firing at once against 4
# slots produced nine timeouts in a single run - including on page 1, the fastest possible request.
# Less parallelism finishes more requests here, because the bottleneck is the API's tolerance for
# bursts, not this instance's ability to wait.
JSEARCH_SEMAPHORE = threading.Semaphore(2)
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

def interpolate_template(template, name="", company="", job_title="", their_desk=""):
    """Deterministically fills {name}/{company}/{job_title}/{their_desk} placeholders via
    str.format() - the only place candidate-facing outreach/LinkedIn copy is ever assembled.
    Never calls Gemini.

    {their_desk} is the one concrete detail about the RECIPIENT's desk, read off their LinkedIn
    About/Experience during the manual pass-2 screen (see memory/outreach-screener.md). It renders
    as a leading subordinate clause with no trailing comma - the template supplies that - so a
    caller passes "Since employee care runs on third-party administrators and HRIS records" and
    gets that clause in front of the ask. Callers that have not done pass 2 pass nothing, and it
    degrades to the generic "Given how much of this sits under you", which is what every cold_ops
    template said before the slot existed. Never leave it unfilled without that fallback: format()
    raises KeyError on an unknown key and the except below returns the RAW template, which would
    put literal braces in a candidate-facing email.

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
            their_desk=str(their_desk or "").strip().rstrip(",") or "Given how much of this sits under you",
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

# How often the Gmail poller runs, in hours. Was a hardcoded 15 minutes, which cost more than it
# returned: each cycle takes SQLite write locks (BEGIN IMMEDIATE, 5s busy_timeout) for inbound
# replies, sent-mail capture and email back-fill, so a cycle landing mid-/t contends with the
# pipeline's own writes across 20 concurrently scored jobs. Once a day is the default; set
# EMAIL_POLL_HOURS to tune it, or EMAIL_POLL_ENABLED=false to turn scheduled polling off
# entirely. /poll always runs a cycle on demand regardless of either setting.
#
# Defined HERE, far above start_gmail_poller(), because EMAIL_MAX_AGE_SECONDS below derives its
# default from it. Module-level constants are evaluated top to bottom at import, so the cadence
# has to be known before the age gate that depends on it.
EMAIL_POLL_HOURS = float(os.environ.get("EMAIL_POLL_HOURS", "1"))

# Attach the resume PDF to the OUTBOUND Gmail draft.
#
# KEVIN SET THIS TO true IN RENDER ON 2026-09-20 AND THAT IS DELIBERATE. Attach on every draft;
# he deletes the attachment by hand on the peer sends. His reasoning, which is sound: it is
# easier to delete an attachment than to add one, and adding by hand is a tax he pays on every
# recruiter email. On 2026-09-20 three recruiter drafts (Albers at thyssenkrupp, Edmonds and
# Ahumada at Ford) shipped with the sentence "my resume is attached" while this flag was false
# and nothing was attached. An email that breaks its own promise to the one person most likely
# to act on it is a worse failure than the filter risk below, which is unmeasured.
#
# The arguments for the old off-by-default, kept because they still govern WHICH sends Kevin
# strips the attachment from:
#
#   RECRUITER - Kevin has already applied by the time he emails one, so his resume is already in
#   their ATS against that req. The attachment duplicates a file they can pull up. It is still
#   worth attaching: it removes a step, and the recruiter-track copy references it directly.
#   PEER - the screener rules say a peer discovery email carries NO resume. A CV attached to
#   "how does your desk actually work?" reads as an application in disguise, which is the thing
#   most likely to stop a peer replying. THIS IS THE SEND TO STRIP.
#
# The cost side is small but real and lands hardest here: ~85% of malicious mail carries a
# PDF/DOC/ZIP, filters weight that, and this sender is on a SPF SOFTFAIL domain with less trust
# margin than most. Measured, the size argument does NOT apply - the resume is 45.8 KB raw /
# 61.1 KB base64, well under the ~110 KB where deliverability starts to degrade - so size is not
# the reason; provenance is. Note that figure is a measurement; the filter penalty is not, and
# nothing here has been tested against reply data.
#
# The Telegram copy is UNAFFECTED and still posted on every /e: that is the file Kevin uploads to
# the ATS portal right after drafting, which is its actual job.
#
# Set RESUME_ATTACH_TO_EMAIL=false to go back to text-only drafts.
RESUME_ATTACH_TO_EMAIL = os.environ.get("RESUME_ATTACH_TO_EMAIL", "false").strip().lower() in ("true", "1", "yes", "on")

# Hard ceiling on how old an inbound message may be and still produce a Telegram alert. Nothing
# outranks it - not a calendar invite, not an offer, not Tier 1.
#
# Everything that alerts is already in Telegram, which stores it indefinitely, so re-sending
# something older than this cannot recover anything: it is either already on Kevin's phone or was
# deliberately skipped. Old mail arriving as a fresh notification is pure noise.
#
# Safe at 24h ONLY because the poller runs hourly (EMAIL_POLL_HOURS=1): the ceiling is 24x the
# cadence, so a message gets ~24 chances to be seen. Raising EMAIL_POLL_HOURS without raising this
# narrows that margin - at a 24h cadence a message could arrive minutes after a poll and be 24h
# old at the next one. The max() below enforces that relationship rather than trusting it.
INBOUND_ALERT_MAX_AGE_SECONDS = max(
    int(float(os.environ.get("INBOUND_ALERT_MAX_AGE_HOURS", "24")) * 3600),
    int(EMAIL_POLL_HOURS * 3600 * 2),
)
EMAIL_POLL_ENABLED = os.environ.get("EMAIL_POLL_ENABLED", "true").strip().lower() not in ("false", "0", "no", "off")


def default_email_max_age_seconds(poll_hours):
    """The age gate's default, derived from the poll cadence instead of being a fixed number.

    The gate exists to drop a stale backlog after an outage, not to drop live mail - but the old
    hardcoded 300s did exactly that: a message had to land inside a 5-minute window that a poller
    running every EMAIL_POLL_HOURS hours hits almost never, so the poller discarded essentially
    everything it was built to catch. Two real interview emails were lost this way.

    Two poll intervals of headroom absorbs a skipped cycle, a Render spin-down or a Gmail 5xx, and
    the floor means a short EMAIL_POLL_HOURS cannot quietly reintroduce a narrow window. Any
    tie-breaking still happens downstream - Gmail's own is:unread already stops a message being
    alerted twice, so a wide window costs nothing but a longer catch-up sweep.

    The floor is 96h, not 24h, because the derived value moves the WRONG WAY when the cadence is
    tightened: going to EMAIL_POLL_HOURS=1 for fresher alerts silently shrank the window from 48h
    to the old 24h floor. A recruiter replying Friday evening, against a weekend Render spin-down
    or a deploy gap, is 62h old by Monday - dropped, and marked read, with no alert. Tier 1 skips
    this gate so interviews were safe, but "every real person who replies" is the actual goal and
    an ordinary human reply is exactly what was being lost. Four days covers a long weekend plus a
    holiday Monday, which is the realistic worst case for an unattended container.
    """
    return max(int(float(poll_hours) * 3600 * 2), 345600)


# Inbound Email Anti-Spam Gatekeeper: pre-filter shield parameters (raw CSV/string env values,
# parsed lazily in passes_email_prefilter() to avoid depending on helpers defined later in the file)
EMAIL_ALLOW_DOMAINS = os.environ.get("EMAIL_ALLOW_DOMAINS", "")
EMAIL_BLOCK_DOMAINS = os.environ.get("EMAIL_BLOCK_DOMAINS", "quora.com,anytimefitness.com")
# Empty by default, and deliberately so. This gate demanded one of interview/schedule/offer/
# opportunity/reply appear in the subject or snippet, which blocked 8 of 10 messages in a real
# production poll - including a recruiter confirming an interview, because Gmail's snippet had not
# reached the word yet. It is redundant as spam defence: the poll query is `label:INBOX`, and
# Gmail files spam under a separate label, so nothing reaching this code was called spam by
# Google. Bulk-vs-human is now decided by the List-Unsubscribe header instead (gate 5 below).
# Setting EMAIL_REQUIRED_KEYWORDS in Render re-enables the old behaviour verbatim.
EMAIL_REQUIRED_KEYWORDS = os.environ.get("EMAIL_REQUIRED_KEYWORDS", "")
# Empty by default, and for the same reason EMAIL_REQUIRED_KEYWORDS is: vocabulary is the wrong
# tool for bulk-vs-human, and this list was actively hostile to the system's actual goal - every
# real person who replies should alert.
#
# It was a substring test over subject+snippet, so "just a quick alert that the role is still open"
# died on "alert", and a recruiter writing "I'll unsubscribe you from the list but wanted to reply
# personally" died on "unsubscribe". Meanwhile it blocked no junk that survives the other gates:
# newsletters carry List-Unsubscribe (gate 5) and robot mailboxes are caught by the sender
# blacklist, both of which are structural rather than guesses about wording. Set it in Render to
# restore the old behaviour.
EMAIL_EXCLUDED_KEYWORDS = os.environ.get("EMAIL_EXCLUDED_KEYWORDS", "")
# Robot mailboxes, matched as a substring of the ADDRESS (not the domain), so a real person at a
# company whose marketing goes out from no-reply@ is unaffected.
#
# "noreply@" and "no-reply@" alone missed the whole hyphenated-suffix family:
# noreply-location-sharing@google.com contains "noreply-", never "noreply@", so Google's location
# notices alerted Kevin as an Unverified Reply. The bare-prefix entries below close that, and the
# transactional senders (service@paypal.com, notifications@, receipts@) are the other half of the
# same problem: they carry no List-Unsubscribe and are filed Updates rather than Promotions, so
# neither the bulk gate nor the category exclusions ever see them.
#
# Added 2026-09-21 after five consumer notifications alerted as Unverified Replies overnight
# (Venmo, CVS, Jooble, Hevy, Lensa). Each one slipped a different way, which is why the list grows
# by prefix rather than by domain:
#   venmo@venmo.com          - brand name as the mailbox; no robot word anywhere
#   cvs@mynotifications...   - "notifications" is in the SUBDOMAIN, and matching is on the address
#   subscribe@jooble.org     - subscription mail, not a reply
#   mail@update.hevyapp.com  - generic "mail@" from a product update domain
#   benjamin.gardner@lensa.com - a job board using a HUMAN NAME, which no prefix rule can catch
# The last one is the real lesson: a fake-human sender is unblockable by mailbox name, so the
# domain list below carries it instead.
EMAIL_SENDER_BLACKLIST = os.environ.get(
    "EMAIL_SENDER_BLACKLIST",
    "no-reply@,noreply@,noreply-,no-reply-,donotreply@,do-not-reply@,"
    "service@paypal.com,notifications@,notification@,receipts@,receipt@,billing@,"
    "mailer-daemon@,postmaster@,bounce@,bounces@,"
    "subscribe@,unsubscribe@,newsletter@,marketing@,updates@,update@,alerts@,alert@,"
    "mail@,email@,hello@,news@,digest@,noreply.,venmo@")

# Domains that only ever send Kevin consumer or job-board bulk mail. Matched like ATS_ROBOT_DOMAINS
# (exact or subdomain) but with the opposite effect: nothing from here is ever a reply worth an
# alert, whatever the mailbox is called. This is the only gate that stops a sender using a
# plausible human name, such as Lensa's "benjamin.gardner@lensa.com".
#
# Job boards go here and NOT in the ATS list above: Workday or Greenhouse carries a real interview
# invitation for a job Kevin applied to, while Lensa, Jooble and ZipRecruiter send alerts about
# jobs he has not. Keep that distinction when adding to either list.
# Keep this list SHORT and only for domains where no human would ever write to Kevin. A domain ban
# is a blunt instrument: test_a_real_person_at_a_robot_domain_still_gets_through() exists because
# banning paypal.com would block a recruiter who happens to work at PayPal, and the same is true of
# indeed.com, ziprecruiter.com and every other large employer. Those belong in the prefix list
# above, never here. Only notification subdomains and scraper job boards qualify.
EMAIL_BULK_SENDER_DOMAINS = tuple(d.strip().lower() for d in os.environ.get(
    "EMAIL_BULK_DOMAINS",
    "mynotifications.cvs.com,update.hevyapp.com,lensa.com,jooble.org,bandana.com"
).split(",") if d.strip())
# Applicant tracking systems that send real interview invitations and scheduling links from
# noreply@ mailboxes. Matched against the sender's DOMAIN (exact or subdomain) to exempt it from
# the blacklist above - see passes_email_sender_blocks(). Everything else about the message is
# still judged normally; this only stops the mailbox NAME being fatal on its own.
ATS_ROBOT_DOMAINS = tuple(d.strip().lower() for d in os.environ.get(
    "EMAIL_ATS_DOMAINS",
    "myworkday.com,workday.com,greenhouse.io,greenhouse-mail.io,lever.co,hire.lever.co,"
    "icims.com,taleo.net,successfactors.com,smartrecruiters.com,jobvite.com,ashbyhq.com,"
    "bamboohr.com,criteriacorp.com,hirevue.com,calendly.com"
).split(",") if d.strip())
EMAIL_SUBJECT_REGEX_FILTER = os.environ.get("EMAIL_SUBJECT_REGEX_FILTER", "")
try:
    EMAIL_MAX_AGE_SECONDS = int(os.environ.get("EMAIL_MAX_AGE_SECONDS") or default_email_max_age_seconds(EMAIL_POLL_HOURS))
except (TypeError, ValueError):
    EMAIL_MAX_AGE_SECONDS = default_email_max_age_seconds(EMAIL_POLL_HOURS)
EMAIL_REQUIRE_DIRECT_REPLY = os.environ.get("EMAIL_REQUIRE_DIRECT_REPLY", "False").strip().lower() in ("1", "true", "yes")
# 50 chars dropped "Hi Kevin, got a sec?" and "Can we talk tomorrow at 2?" - the shortest replies
# are often the warmest, because a busy human writing back types one line. The gate exists to skip
# empty auto-acknowledgements, not brevity, and bulk is already handled structurally by the sender
# blacklist and List-Unsubscribe. 12 still drops a truly empty body while keeping a one-line reply.
try:
    EMAIL_MIN_BODY_LENGTH = int(os.environ.get("EMAIL_MIN_BODY_LENGTH", "12"))
except (TypeError, ValueError):
    EMAIL_MIN_BODY_LENGTH = 12
EMAIL_LABEL_TARGET_INBOX = os.environ.get("EMAIL_LABEL_TARGET_INBOX", "INBOX")

# Gmail-side exclusions, applied in the list query itself rather than in Python. This is the only
# filter layer that runs BEFORE the per-cycle message budget is spent, which is what makes it
# different in kind from every gate in passes_email_prefilter(): those gates reject a message that
# has already consumed one of EMAIL_POLL_MAX_RESULTS slots, so a burst of newsletters can starve a
# real interview email out of the window entirely. Gmail's own category classifier is very good at
# exactly the mail Kevin gets most of - job-board blasts, retail, social - and costs nothing.
#
# Deliberately NOT a spam filter: spam is a separate label Gmail already diverts, and Promotions is
# not spam. A recruiter using Mailchimp can land in Promotions, which is why the Spam sweep's Tier 1
# net exists and why this is one env var away from being switched off.
EMAIL_QUERY_EXCLUSIONS = os.environ.get(
    "EMAIL_QUERY_EXCLUSIONS", "-category:promotions -category:social -category:forums")
# Messages examined per cycle. Gmail bills messages.list at 5 quota units regardless of maxResults,
# and messages.get at 5 units each, against a 1.2M unit/day ceiling - so 50 is not meaningfully more
# expensive than 10, and 10 was far below one day's real inbound volume.
try:
    EMAIL_POLL_MAX_RESULTS = int(os.environ.get("EMAIL_POLL_MAX_RESULTS", "50"))
except (TypeError, ValueError):
    EMAIL_POLL_MAX_RESULTS = 50

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
    # Matched as raw substrings of the whole description, so each term must name the sales role
    # itself, not a task an ops role also does: bare "commission" rejected "commission calculations
    # and reporting", "pipeline development" rejected data-pipeline work, and "client acquisition"
    # rejected roles that merely support advisor onboarding.
    "hard_ban_keywords": [
        "lead generation", "upselling", "quota-driven",
        "hunter mentality", "sales pipeline", "uncapped earnings",
        "cold outreach", "deal closing", "solution pitching",
        "uncapped potential", "commission-only", "hustle", "grind", "door-to-door",
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
    # Search phrases, scanned as a rolling 10-query slice per /t run. Each must read like a job
    # TITLE someone would actually post, not a skills summary: JSearch reads the Google-for-Jobs
    # surface, whose own guidance is to keep titles broad and put skills in keywords. Two former
    # entries broke that rule and were replaced - "Financial Systems Process Automation" (four
    # abstract nouns, not a title any employer writes) and "Custodial Operations Schwab Fidelity"
    # (four terms ANDed, and "custodial" means janitorial outside finance, so it surfaced building
    # maintenance rather than securities custody). /queries reports lifetime yield per phrase.
    # Keyword-driven, not city-driven. radius_miles (45) already covers the whole metro from any
    # anchor, so "Wealth Operations Dearborn MI" and "Wealth Operations Troy MI" search overlapping
    # circles for the SAME phrase - eleven near-duplicate queries where one would do. The old bank
    # spent its 110 slots on 8 role phrases x 11 cities, which meant a whole slice could be one
    # city's worth of near-identical searches and a bad day for that city produced nothing.
    #
    # Now: 58 distinct role phrases against Detroit (the metro anchor), with the 27 highest-value
    # phrases repeated against Farmington Hills and Troy for local density. Every slice mixes roles,
    # so no single run depends on one geography or one phrasing.
    "target_queries": [
        "Wealth Operations Detroit MI",
        "Wealth Management Operations Detroit MI",
        "Investment Operations Analyst Detroit MI",
        "Middle Office Analyst Detroit MI",
        "Portfolio Operations Analyst Detroit MI",
        "Brokerage Operations Analyst Detroit MI",
        "Trust Operations Specialist Detroit MI",
        "Settlements Analyst Detroit MI",
        "Fund Administration Analyst Detroit MI",
        "Reconciliation Analyst Detroit MI",
        "Retirement Plan Administrator Detroit MI",
        "Client Service Associate Detroit MI",
        "Advisory Operations Specialist Detroit MI",
        "Custody Operations Analyst Detroit MI",
        "Securities Operations Analyst Detroit MI",
        "Financial Operations Analyst Detroit MI",
        "Treasury Operations Analyst Detroit MI",
        "Trade Operations Analyst Detroit MI",
        "Financial Systems Analyst Detroit MI",
        "Fund Accounting Analyst Detroit MI",
        "Collateral Operations Analyst Detroit MI",
        "Business Operations Analyst Detroit MI",
        "Business Systems Analyst Detroit MI",
        "Business Process Analyst Detroit MI",
        "Operations Specialist Detroit MI",
        "Operations Analyst Detroit MI",
        "Process Improvement Analyst Detroit MI",
        "Business Intelligence Analyst Detroit MI",
        "ERP Systems Analyst Detroit MI",
        "Systems Analyst Detroit MI",
        "Salesforce Administrator Detroit MI",
        "Salesforce Analyst Detroit MI",
        "CRM Operations Analyst Detroit MI",
        "Data Operations Analyst Detroit MI",
        "Reporting Analyst Detroit MI",
        "Automation Analyst Detroit MI",
        "Client Operations Associate Detroit MI",
        "Client Onboarding Specialist Detroit MI",
        "Onboarding Specialist Detroit MI",
        "Implementation Specialist Detroit MI",
        "Client Success Operations Detroit MI",
        "Compliance Operations Specialist Detroit MI",
        "Risk Operations Analyst Detroit MI",
        "Regulatory Operations Analyst Detroit MI",
        "Fintech Operations Detroit MI",
        "Fintech Systems Analyst Detroit MI",
        "Revenue Operations Analyst Detroit MI",
        "Healthcare Operations Analyst Detroit MI",
        "Claims Operations Analyst Detroit MI",
        "Supply Chain Operations Analyst Detroit MI",
        "Manufacturing Operations Analyst Detroit MI",
        "Logistics Operations Analyst Detroit MI",
        "Procurement Operations Analyst Detroit MI",
        "Cloud Operations Analyst Detroit MI",
        "Insurance Operations Analyst Detroit MI",
        "Loan Operations Specialist Detroit MI",
        "Payment Operations Analyst Detroit MI",
        "Billing Operations Analyst Detroit MI",
        "Wealth Operations Farmington Hills MI",
        "Wealth Management Operations Farmington Hills MI",
        "Investment Operations Analyst Farmington Hills MI",
        "Middle Office Analyst Farmington Hills MI",
        "Portfolio Operations Analyst Farmington Hills MI",
        "Brokerage Operations Analyst Farmington Hills MI",
        "Trust Operations Specialist Farmington Hills MI",
        "Settlements Analyst Farmington Hills MI",
        "Fund Administration Analyst Farmington Hills MI",
        "Reconciliation Analyst Farmington Hills MI",
        "Retirement Plan Administrator Farmington Hills MI",
        "Client Service Associate Farmington Hills MI",
        "Advisory Operations Specialist Farmington Hills MI",
        "Custody Operations Analyst Farmington Hills MI",
        "Securities Operations Analyst Farmington Hills MI",
        "Financial Operations Analyst Farmington Hills MI",
        "Treasury Operations Analyst Farmington Hills MI",
        "Trade Operations Analyst Farmington Hills MI",
        "Financial Systems Analyst Farmington Hills MI",
        "Fund Accounting Analyst Farmington Hills MI",
        "Collateral Operations Analyst Farmington Hills MI",
        "Business Operations Analyst Farmington Hills MI",
        "Business Systems Analyst Farmington Hills MI",
        "Business Process Analyst Farmington Hills MI",
        "Operations Specialist Farmington Hills MI",
        "Operations Analyst Farmington Hills MI",
        "Process Improvement Analyst Farmington Hills MI",
        "Wealth Operations Troy MI",
        "Wealth Management Operations Troy MI",
        "Investment Operations Analyst Troy MI",
        "Middle Office Analyst Troy MI",
        "Portfolio Operations Analyst Troy MI",
        "Brokerage Operations Analyst Troy MI",
        "Trust Operations Specialist Troy MI",
        "Settlements Analyst Troy MI",
        "Fund Administration Analyst Troy MI",
        "Reconciliation Analyst Troy MI",
        "Retirement Plan Administrator Troy MI",
        "Client Service Associate Troy MI",
        "Advisory Operations Specialist Troy MI",
        "Custody Operations Analyst Troy MI",
        "Securities Operations Analyst Troy MI",
        "Financial Operations Analyst Troy MI",
        "Treasury Operations Analyst Troy MI",
        "Trade Operations Analyst Troy MI",
        "Financial Systems Analyst Troy MI",
        "Fund Accounting Analyst Troy MI",
        "Collateral Operations Analyst Troy MI",
        "Business Operations Analyst Troy MI",
        "Business Systems Analyst Troy MI",
        "Business Process Analyst Troy MI",
        "Operations Specialist Troy MI"
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
        # Lifetime yield per search phrase. A single run says almost nothing about a query - the
        # rolling slice means each one fires roughly every 11 runs, and any one firing can be
        # unlucky. Accumulated across runs it separates a phrase that is structurally broken
        # (never returns anything, or only listings every gate rejects) from one that is merely
        # quiet this week, which is the only honest basis for pruning the 110-query bank.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS query_yield (
            query_text TEXT PRIMARY KEY,
            runs INTEGER DEFAULT 0,
            raw_total INTEGER DEFAULT 0,
            passed_total INTEGER DEFAULT 0,
            last_run TIMESTAMP
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
        # posted_hours arrived after application_outcomes was already carrying rows in production,
        # so it has to be an ALTER, not a widened CREATE TABLE - Render's DB is never recreated and
        # CREATE TABLE IF NOT EXISTS is a no-op against an existing table. PRAGMA-guarded because
        # SQLite has no ADD COLUMN IF NOT EXISTS and init_db runs on every boot.
        outcome_columns = {row[1] for row in conn.execute("PRAGMA table_info(application_outcomes)")}
        if "posted_hours" not in outcome_columns:
            conn.execute("ALTER TABLE application_outcomes ADD COLUMN posted_hours INTEGER")
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
        # One row per sequencer run: the full result GET /followups renders. See
        # save_followup_queue_snapshot for why the page reads this instead of recomputing.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS followup_queue_snapshot (
            run_date TEXT PRIMARY KEY,
            payload_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        # The inbound tray: one row per Gmail THREAD, not per message.
        #
        # This is the ledger the notification path never had. Before it, Gmail's own UNREAD flag
        # was the only state, which made three things impossible: a failed Telegram send was
        # unrecoverable, reading mail on a phone silently cancelled the alert, and five replies on
        # one thread produced five alerts. A durable row per thread fixes all three, and turns
        # "did I get a notification?" into a question with an answer that survives a restart.
        #
        # Keyed on thread_id because a conversation is the unit Kevin acts on - he replies to a
        # person, not to a message. state: 'open' (needs a look) | 'done' (dealt with).
        conn.execute("""
        CREATE TABLE IF NOT EXISTS inbound_threads (
            thread_id TEXT PRIMARY KEY,
            sender_email TEXT,
            sender_name TEXT,
            company TEXT,
            subject TEXT,
            snippet TEXT,
            status_label TEXT,
            match_reason TEXT,
            sheet_uuid TEXT,
            is_tier1 INTEGER DEFAULT 0,
            alerted INTEGER DEFAULT 0,
            state TEXT DEFAULT 'open',
            message_count INTEGER DEFAULT 1,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inbound_threads_state ON inbound_threads(state, last_seen)")
        # Rolling vocabulary of every JD the pipeline has scored. calculate_keyword_overlap() only
        # ever asked "do Kevin's 10 words appear here", so the language the market actually uses
        # was computed and thrown away on every run - a 98/100 surety role read "Skills 0%" because
        # the posting says "bordereaux" and the bank says "reconciliation".
        #
        # docs/fit_sum (not an average column) so a term's mean fit stays correct under concurrent
        # upserts: two workers incrementing a stored average would race, whereas summing is
        # associative and SQLite's UPSERT makes each += atomic.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS jd_term_yield (
            term TEXT PRIMARY KEY,
            docs INTEGER DEFAULT 0,
            fit_sum INTEGER DEFAULT 0,
            hi_fit_docs INTEGER DEFAULT 0,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jd_term_yield_docs ON jd_term_yield(hi_fit_docs DESC, docs DESC)")
        # Result of the nightly job-link liveness check, one row per sheet_uuid. Kept in SQLite
        # rather than a file because Render's disk is ephemeral - a container restart would lose a
        # file, and this has to survive to be read by the morning digest and /dead.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS job_link_status (
            sheet_uuid TEXT PRIMARY KEY,
            company TEXT,
            role TEXT,
            job_link TEXT,
            status TEXT,
            verdict TEXT,
            reason TEXT,
            retired INTEGER DEFAULT 0,
            notified INTEGER DEFAULT 0,
            first_dead_at TIMESTAMP,
            checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_job_link_status_verdict ON job_link_status(verdict, notified)")
        # One row per command invocation. Stored as individual events rather than a running
        # counter so any window (week, month, since-a-date) can be asked for after the fact -
        # a counter would fix the window at write time and could never answer "last 7 days".
        conn.execute("""
        CREATE TABLE IF NOT EXISTS command_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            command TEXT NOT NULL,
            used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_command_usage_cmd ON command_usage(command, used_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_command_usage_time ON command_usage(used_at)")

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

def remap_cached_job_uuid(short_id, new_sheet_uuid):
    """Re-point a cached job at a different sheet row.

    Used when Code.gs suppressed a row as a duplicate: the card is re-pointed at the live row, and
    the local cache has to follow or /stage and swipe recovery keep resolving the dead uuid that
    was generated for the suppressed write.
    """
    if not (short_id and new_sheet_uuid):
        return False
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE jobs SET sheet_uuid = ? WHERE short_id = ?", (new_sheet_uuid, short_id))
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"UUID remap error ({short_id}): {e}")
        return False


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

def record_application_outcome(sheet_uuid, status, company=None, role=None, source=None, outreach_path=None, posted_hours=None):
    """Append an application_outcomes row (event-sourced, one row per transition) so /outcomes and
    the Tuesday hub can compute evidence-based reply/interview rates and time-to-response, instead
    of relying on gut-feel. status is one of: applied, interview, rejection, offer, withdrawn,
    dead_link.

    posted_hours is the posting's age when it was carded (see get_posted_hours_at_card), stored on
    the row rather than recomputed at read time: the listing keeps aging after the swipe, and
    /decoys needs the age Kevin actually acted on, not the age today.
    """
    if not sheet_uuid:
        return False
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO application_outcomes (sheet_uuid, company, role, source, outreach_path, status, posted_hours) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sheet_uuid, company, role, source, outreach_path, status, posted_hours)
            )
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"Application Outcome Record Error ({sheet_uuid}, {status}): {e}")
        return False

def get_posted_hours_at_card(sheet_uuid):
    """The posting's age in hours at the moment its card was sent, or None if it cannot be derived.

    Reconstructed from the cached row - jobs.created_at IS the carding timestamp - rather than read
    off a field stamped at card-send time, so it works retroactively against every job already in
    the cache instead of only cards sent after this shipped.

    Two deliberate Nones: a job carrying no job_posted_at_datetime_utc, and a job no longer in the
    cache. Either could be filled with parse_posted_hours' fail-open 48, and either would then put
    a number nothing measured into the /decoys median. An absent age is honest; an invented one
    would make the report confidently wrong.
    """
    if not sheet_uuid:
        return None
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT job_json, created_at FROM jobs WHERE sheet_uuid = ?", (sheet_uuid,))
            row = cursor.fetchone()
    except Exception as e:
        logging.error(f"DB Read Error (posted hours at card, {sheet_uuid}): {e}")
        return None
    if not row:
        return None
    try:
        job = json.loads(row[0]) if row[0] else {}
        posted_raw = job.get("job_posted_at_datetime_utc")
        if not posted_raw:
            return None
        posted_dt = datetime.fromisoformat(str(posted_raw).replace("Z", "+00:00"))
        # SQLite's CURRENT_TIMESTAMP is UTC but writes no offset, so it parses naive - attach UTC
        # explicitly rather than letting the subtraction blow up on mixed awareness.
        carded_dt = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
        if carded_dt.tzinfo is None:
            carded_dt = carded_dt.replace(tzinfo=timezone.utc)
        # Clamped at 0: a board that stamps a posting slightly in the future should read "brand
        # new", not as a negative age dragging the median below anything that can exist.
        return max(0, int((carded_dt - posted_dt).total_seconds() / 3600))
    except Exception as e:
        logging.error(f"Posted-hours-at-card parse error ({sheet_uuid}): {e}")
        return None

def _median_or_none(values):
    """Median of a numeric list, or None when it is empty - no data is not a median of zero."""
    ordered = sorted(values)
    if not ordered:
        return None
    mid = len(ordered) // 2
    return float(ordered[mid]) if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2

def get_decoy_metrics():
    """Aggregate application_outcomes into per-source decoy rates for /decoys.

    A decoy is a posting that was already dead when Kevin opened the card - aggregators resell
    expired inventory, and the only way to learn which feed does it worst (and so which one to drop
    from the Sheet config) is to count. The rate is dead_link rows over ALL rows carrying that
    source, so 1-of-2 is not ranked beside 1-of-40 as though they were the same evidence.

    The paired medians answer the follow-on question: if dead cards are consistently far older at
    carding than surviving ones, the fix is an age gate, not dropping a source.
    """
    by_source = {}
    dead_ages, live_ages = [], []
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT source, status, posted_hours FROM application_outcomes")
            rows = cursor.fetchall()
    except Exception as e:
        logging.error(f"Decoy Metrics Read Error: {e}")
        rows = []

    for source, status, posted_hours in rows:
        source = source or "unknown"
        is_dead = status == "dead_link"
        stats = by_source.setdefault(source, {"total": 0, "dead": 0, "decoy_rate": 0.0})
        stats["total"] += 1
        if is_dead:
            stats["dead"] += 1
        if posted_hours is not None:
            (dead_ages if is_dead else live_ages).append(posted_hours)

    for stats in by_source.values():
        stats["decoy_rate"] = (stats["dead"] / stats["total"]) * 100 if stats["total"] else 0.0

    return {
        "total_rows": sum(s["total"] for s in by_source.values()),
        "total_dead": sum(s["dead"] for s in by_source.values()),
        "by_source": by_source,
        "median_dead_posted_hours": _median_or_none(dead_ages),
        "median_live_posted_hours": _median_or_none(live_ages),
    }

def format_decoy_metrics_message():
    """Render get_decoy_metrics() into one HTML Telegram message for /decoys.

    Deliberately reads as instructions on an empty table, because that is the first thing Kevin
    will see: a report printing 0.0% everywhere before a single /dead exists looks like a measured
    result, and not mistaking an absent measurement for a good one is the whole point of the
    command.
    """
    metrics = get_decoy_metrics()
    lines = ["💀 <b>Decoy Rate by Source</b>\n"]

    if not metrics["total_rows"]:
        lines.append("No outcome rows recorded yet - nothing to measure.")
        lines.append("")
        lines.append("Swipe-reply <code>/dead</code> on any card whose posting has already expired. "
                     "Each one records its source and how old the posting was when it was carded; "
                     "come back once a handful have landed.")
        return "\n".join(lines)

    lines.append(f"🧮 <b>Dead links:</b> {metrics['total_dead']} of {metrics['total_rows']} outcome rows")
    lines.append("")

    if metrics["total_dead"]:
        # Worst offender first: this list exists to pick a source to drop, so the answer belongs on
        # line one. Ties break on volume, since the larger sample is the more actionable one.
        lines.append("<b>By Source (dead / total):</b>")
        ranked = sorted(metrics["by_source"].items(), key=lambda kv: (-kv[1]["decoy_rate"], -kv[1]["total"], kv[0]))
        for source, stats in ranked:
            lines.append(f"• {html.escape(source)}: {stats['dead']}/{stats['total']} ({stats['decoy_rate']:.1f}%)")
    else:
        lines.append("<b>By Source:</b> no <code>/dead</code> marks yet across "
                     f"{len(metrics['by_source'])} source(s) - nothing has been reported as a decoy.")

    lines.append("")
    lines.append("<b>Posting age when carded (median):</b>")
    dead_median = metrics["median_dead_posted_hours"]
    live_median = metrics["median_live_posted_hours"]
    lines.append(f"• Dead: {dead_median:.0f}h" if dead_median is not None else "• Dead: no aged rows yet")
    lines.append(f"• Not dead: {live_median:.0f}h" if live_median is not None else "• Not dead: no aged rows yet")

    return "\n".join(lines)

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
# MARKET SUPPLY REPORT (READ-ONLY)
# ==============================================================================
# How many roles worth applying to the market actually produces, measured instead of guessed.
# Two independent sources, deliberately: pipeline_metrics.listing_discovered counts every posting
# the query bank surfaced (an event log, never pruned), while the jobs table counts the ones that
# survived Gemini scoring. A job only reaches the jobs table if it passed, so `qualified` is the
# real supply line - the number of genuinely good roles that opened in the window.
#
# The ratio matters more than either count: at a consumption rate above the replenishment rate,
# the funnel is being drained faster than the market refills it, and the weekly application count
# will fall for reasons that have nothing to do with how fast Kevin works.
SUPPLY_QUALIFIED_SCORE = 80  # fit_score at or above this counts as a role worth applying to

def get_market_supply(days=30, min_score=SUPPLY_QUALIFIED_SCORE):
    """READ-ONLY. Market supply over the trailing `days` window. Never writes.

    Returns {"days", "discovered", "qualified", "per_week", "consumed", "weeks_of_supply"}:
      discovered      - listings the query bank surfaced (pipeline_metrics event count)
      qualified       - cached jobs scoring >= min_score, i.e. roles worth applying to
      per_week        - qualified normalized to a 7-day rate, the replenishment number
      consumed        - 'applied' events in the same window, normalized to a weekly rate
      weeks_of_supply - per_week / consumed, or None when nothing was consumed (no rate to
                        compare against - reporting a division-by-zero as "infinite supply"
                        would read as good news when it actually means no applications went out)

    A job whose cached JSON predates fit_score persistence has json_extract -> NULL, which fails
    the >= comparison rather than counting as qualified. Undercounting old rows is the safe
    direction: this number is used to decide whether the market is running dry.
    """
    result = {"days": days, "discovered": 0, "qualified": 0, "per_week": 0.0,
              "consumed": 0.0, "weeks_of_supply": None}
    window = f"-{days} days"
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM pipeline_metrics "
                "WHERE event_type = 'listing_discovered' AND timestamp >= datetime('now', ?)",
                (window,)
            )
            result["discovered"] = cursor.fetchone()[0] or 0
            cursor.execute(
                "SELECT COUNT(*) FROM jobs "
                "WHERE created_at >= datetime('now', ?) "
                "  AND CAST(json_extract(job_json, '$.fit_score') AS REAL) >= ?",
                (window, min_score)
            )
            result["qualified"] = cursor.fetchone()[0] or 0
            cursor.execute(
                "SELECT COUNT(*) FROM pipeline_metrics "
                "WHERE event_type = 'applied' AND timestamp >= datetime('now', ?)",
                (window,)
            )
            applied = cursor.fetchone()[0] or 0
    except Exception as e:
        logging.error(f"Market Supply Read Error ({days}d): {e}")
        return result

    weeks = max(days / 7.0, 1e-9)
    result["per_week"] = result["qualified"] / weeks
    result["consumed"] = applied / weeks
    if result["consumed"] > 0:
        result["weeks_of_supply"] = result["per_week"] / result["consumed"]
    return result

def format_market_supply_message(supply):
    """Render get_market_supply() as the /funnel card's supply section. Pure - no I/O."""
    days = supply.get("days", 30)
    discovered = supply.get("discovered", 0)
    qualified = supply.get("qualified", 0)
    per_week = supply.get("per_week", 0.0)
    consumed = supply.get("consumed", 0.0)
    ratio = supply.get("weeks_of_supply")

    lines = [
        f"\n🛒 <b>MARKET SUPPLY</b> <i>(trailing {days}d)</i>",
        f"Discovered {discovered} · Scored {SUPPLY_QUALIFIED_SCORE}+ <b>{qualified}</b>",
        f"<b>≈ {per_week:.1f} good roles/week</b> · applying {consumed:.1f}/week",
    ]
    if not qualified:
        # Distinguish "the market is dry" from "the pipeline has not run here yet" - on a fresh
        # DB both read as zero, and only one of them is a market signal.
        lines.append("<i>No scored roles in this window yet — run /t a few times before reading this.</i>")
    elif ratio is None:
        lines.append("<i>Nothing applied to in this window, so there's no consumption rate to compare.</i>")
    elif ratio >= 1.5:
        lines.append("🟢 <i>Supply outpaces you — room to raise volume.</i>")
    elif ratio >= 1.0:
        lines.append("🟡 <i>Roughly balanced with what the market opens.</i>")
    else:
        lines.append(
            f"🔴 <i>Consuming {1 / ratio:.1f}x faster than the market refills. "
            f"Expect the weekly count to fall — widen the query bank or the geography.</i>"
        )
    return "\n".join(lines)

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
# Separate cache from _APPLIED_CRM_CACHE: that one suppresses whole COMPANIES already applied to
# (Tetiana Warm), while this one is keyed company+title and answers a different question - "is this
# exact role already a row in a job tab?" A company can have one role in Tetiana Cold and another
# worth surfacing, so the two must not share a store.
_TRACKED_ROLE_CACHE = {"data": set(), "fetched_at": 0.0}
_TRACKED_ROLE_CACHE_TTL_SECONDS = 300
# How long a cached suppression set may keep blocking after its last SUCCESSFUL refresh.
#
# The TTL above only decides when to re-fetch. When that re-fetch fails, fetched_at is left
# untouched and the stale set keeps answering - so a warm cache plus an unreachable Sheets
# permanently suppressed roles whose rows Kevin had already deleted, with /job insisting the role
# was "already in the pipeline" against an empty tab. Past this bound the set is discarded and the
# gate errs OPEN (a duplicate card is recoverable; an un-ingestable role is not).
_TRACKED_ROLE_CACHE_MAX_STALE_SECONDS = 900

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

def _record_query_yield(per_query):
    """Accumulate this run's per-query raw/passed counts into the query_yield table.

    Never raises: this is telemetry, and a bookkeeping failure must not take down a /t run.
    """
    if not per_query:
        return
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for query, stats in per_query.items():
                conn.execute("""
                    INSERT INTO query_yield (query_text, runs, raw_total, passed_total, last_run)
                    VALUES (?, 1, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(query_text) DO UPDATE SET
                        runs = runs + 1,
                        raw_total = raw_total + excluded.raw_total,
                        passed_total = passed_total + excluded.passed_total,
                        last_run = CURRENT_TIMESTAMP
                """, (query, stats["raw"], stats["passed"]))
            conn.commit()
    except Exception as e:
        logging.error(f"Query yield persist error: {e}")


def probe_crm_read():
    """One-line diagnosis of what the CRM webhook actually answers on a read.

    Exists because a rejected read and an empty tab look identical downstream, and guessing at
    the cause sent Kevin to check a shared secret that was already correct. This reports the real
    response - status code, body status, message, row count - so the next step follows from
    evidence rather than from the most likely story.

    The secret itself is never printed, only whether one was attached.
    """
    if not CRM_WEBHOOK_URL:
        return "CRM_WEBHOOK_URL is unset - the bot has no CRM to read."
    res = crm_post({"action": "get_followups", "tab": "TC"})
    if not res:
        return "No response at all (network error or timeout reaching the webhook URL)."
    bits = [f"HTTP {res.status_code}"]
    bits.append("secret sent: yes" if CRM_SHARED_SECRET else "secret sent: NO (env var unset)")
    try:
        body = res.json()
    except Exception:
        snippet = re.sub(r"\s+", " ", res.text or "")[:160]
        bits.append(f"non-JSON body: {snippet}")
        return " | ".join(bits)
    if isinstance(body, dict):
        status = str(body.get("status", "?"))
        bits.append(f"status: {status}")
        msg = str(body.get("message", "")).strip()
        if msg:
            bits.append(f"message: {msg[:120]}")
        rows = body.get("followups")
        if isinstance(rows, list):
            bits.append(f"rows returned: {len(rows)}")
    else:
        bits.append(f"unexpected body type: {type(body).__name__}")
    return " | ".join(bits)


def normalize_command_name(text):
    """The bare command from a raw Telegram message, or "" when it is not a command.

    Arguments are stripped so "/f 7" and "/f 14" aggregate as /f - the question is which
    commands Kevin reaches for, not which values he passes. A trailing "!" is kept, because
    /job! is a genuinely different action from /job and counting them together would hide how
    often the AI screener gets overridden.
    """
    raw = str(text or "").strip()
    if not raw.startswith("/"):
        return ""
    token = raw.split()[0].lower()
    token = re.sub(r"@\w+$", "", token)          # /help@mybot -> /help
    if not re.fullmatch(r"/[a-z0-9_]+!?", token):
        return ""
    return token


def record_command_usage(text):
    """Log one command invocation. Telemetry only - never raises, never blocks the command."""
    cmd = normalize_command_name(text)
    if not cmd:
        return ""
    try:
        with get_db_conn() as conn:
            conn.execute("INSERT INTO command_usage (command) VALUES (?)", (cmd,))
            conn.commit()
    except Exception as e:
        logging.error(f"Command usage record error: {e}")
    return cmd


def get_command_usage(days=30, limit=60):
    """(command, count, last_used) over the last `days`, most used first.

    days=None counts all of history.
    """
    try:
        with get_db_conn() as conn:
            if days is None:
                return conn.execute("""
                    SELECT command, COUNT(*) AS n, MAX(used_at)
                    FROM command_usage
                    GROUP BY command ORDER BY n DESC, command ASC LIMIT ?
                """, (limit,)).fetchall()
            return conn.execute("""
                SELECT command, COUNT(*) AS n, MAX(used_at)
                FROM command_usage
                WHERE used_at >= datetime('now', ?)
                GROUP BY command ORDER BY n DESC, command ASC LIMIT ?
            """, (f"-{int(days)} days", limit)).fetchall()
    except Exception as e:
        logging.error(f"Command usage read error: {e}")
        return []


def get_command_usage_totals(days=30):
    """(total_invocations, distinct_commands, first_seen) for the window."""
    try:
        with get_db_conn() as conn:
            if days is None:
                row = conn.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT command), MIN(used_at) FROM command_usage"
                ).fetchone()
            else:
                row = conn.execute("""
                    SELECT COUNT(*), COUNT(DISTINCT command), MIN(used_at) FROM command_usage
                    WHERE used_at >= datetime('now', ?)
                """, (f"-{int(days)} days",)).fetchone()
        return row or (0, 0, None)
    except Exception as e:
        logging.error(f"Command usage totals error: {e}")
        return (0, 0, None)


def get_query_yield_rows(limit=25):
    """Lifetime query yield, worst first (fewest passed, then most wasted raw)."""
    try:
        with get_db_conn() as conn:
            return conn.execute("""
                SELECT query_text, runs, raw_total, passed_total
                FROM query_yield
                ORDER BY passed_total ASC, raw_total DESC
                LIMIT ?
            """, (limit,)).fetchall()
    except Exception as e:
        logging.error(f"Query yield read error: {e}")
        return []


def get_tracked_job_keys():
    """Every role already sitting in a live JOB tab (Tetiana Cold + Warm + Clavicular), as hashes.

    This is the ONLY thing allowed to suppress a rediscovered listing. The seen_jobs ledger answers
    "has the pipeline ever looked at this?", which is the wrong question: a job glanced at during a
    run that produced no card is not a job Kevin has seen, and blocking it there is what made
    repeat runs return 114-of-121 "already seen" while dispatching almost nothing. What he actually
    wants suppressed is a role he is ALREADY TRACKING - one that has a CRM row he could open.

    Keyed on the same generate_dedup_hash(company, title) the discovery path uses, so a company can
    keep surfacing new roles while the specific role already in a tab stays out.

    Errs OPEN, but only within a bound. A Sheets failure returns the last good set rather than an
    empty one, so a transient blip does not re-card everything already tracked. That was described
    here as erring open because a COLD process has an empty cache - but a WARM one does the
    opposite: fetched_any stays False on failure, fetched_at is never advanced, and the stale set
    answers forever. A role whose row Kevin had deleted stayed suppressed indefinitely, with /job
    reporting "already in the pipeline" against a tab that no longer held it.

    _TRACKED_ROLE_CACHE_MAX_STALE_SECONDS caps that: past it the set is dropped and suppression
    stops until Sheets answers again.

    Reads every tab dispatch_tier1_matches can WRITE to. It previously read only TC+TW while rows
    also land in Clavicular (target_code "CL"), so a warm-referral role was tracked in the sheet but
    invisible here: the ingest gate in ingest_manual_job() saw "not tracked" and let the card
    through, while Code.gs's batch_add_rows dedup guard saw the existing row and suppressed the
    write. That mismatch is what dispatched a card whose sheet_uuid had no row behind it, leaving
    every later /warm and /apply on it rejected as "No record found".

    Died is deliberately NOT read: an archived role is one Kevin killed, and re-surfacing it if it
    is reposted is the intended behavior - the tab is a graveyard, not a live tracking state.
    """
    now = time.time()
    if now - _TRACKED_ROLE_CACHE["fetched_at"] < _TRACKED_ROLE_CACHE_TTL_SECONDS:
        return _TRACKED_ROLE_CACHE["data"]
    tracked = set()
    fetched_any = False
    for target_code in ("TC", "TW", "CL"):
        res = crm_post({"action": "get_followups", "tab": target_code})
        if not res:
            continue
        try:
            if res.status_code != 200:
                continue
            data = res.json()
            if data.get("status") != "success":
                continue
            fetched_any = True
            for row in data.get("followups", []):
                company = str(row.get("company") or "").strip()
                title = str(row.get("job_title") or row.get("title") or "").strip()
                if company and title:
                    # Two keys per row, because two different dedup algorithms decide this job's
                    # fate and they do not agree. generate_dedup_hash() (strips legal suffixes,
                    # keeps punctuation) is what the discovery path hashes against. Code.gs's
                    # batch_add_rows guard instead keys on normalizeDedupKey() (strips punctuation
                    # and stop tokens), mirrored here by normalize_dedup_key(). Storing only the
                    # first let a row the Apps Script guard WOULD suppress read as untracked, so
                    # the card shipped and the write silently did not. Tracking both makes the
                    # local gate a superset of the remote guard: anything Sheets would refuse to
                    # write is now refused here first, before a card is ever dispatched.
                    tracked.add(generate_dedup_hash(company, title))
                    tracked.add(normalize_dedup_key(company, title))
        except Exception as e:
            logging.error(f"get_tracked_job_keys Error ({target_code}): {e}")
    if fetched_any:
        _TRACKED_ROLE_CACHE["data"] = tracked
        _TRACKED_ROLE_CACHE["fetched_at"] = now
        logging.info(
            f"Tracked-role suppression set refreshed: {len(tracked)} keys "
            f"across Tetiana Cold + Warm + Clavicular"
        )
    elif _TRACKED_ROLE_CACHE["data"]:
        # The refresh failed. Keep serving the last good set only while it is plausibly still
        # true; beyond that, suppressing against data this old blocks roles Kevin has since
        # deleted, and he cannot ingest them at all.
        age = now - _TRACKED_ROLE_CACHE["fetched_at"]
        if age > _TRACKED_ROLE_CACHE_MAX_STALE_SECONDS:
            logging.error(
                f"Tracked-role suppression set is {int(age)}s stale and Sheets is not answering - "
                f"dropping {len(_TRACKED_ROLE_CACHE['data'])} keys and erring open so ingest works"
            )
            send_health_alert(
                "Tracked-role suppression is running blind: Sheets has not answered for "
                f"{int(age // 60)} minutes, so duplicate-role checking is OFF until it recovers. "
                "Cards may repeat for roles already in a tab."
            )
            _TRACKED_ROLE_CACHE["data"] = set()
    return _TRACKED_ROLE_CACHE["data"]


def locate_tracked_role(company, title):
    """Find the LIVE sheet row that makes this role count as tracked, or None.

    Deliberately bypasses _TRACKED_ROLE_CACHE and re-reads the tabs, because the whole point is to
    tell a genuine duplicate apart from a cached ghost. None here means the suppression set is
    stale - the role is blocked by data that is no longer in the sheet.

    Returns {tab, row_label, status, sheet_uuid} for the first match, newest tab first.
    """
    want_hash = generate_dedup_hash(company, title)
    want_key = normalize_dedup_key(company, title)
    for target_code, tab_name in (("TC", "Tetiana Cold"), ("TW", "Tetiana Warm"), ("CL", "Clavicular")):
        try:
            res = crm_post({"action": "get_followups", "tab": target_code})
            if not res or res.status_code != 200:
                continue
            data = res.json()
            if data.get("status") != "success":
                continue
            for idx, row in enumerate(data.get("followups", [])):
                row_company = str(row.get("company") or "").strip()
                row_title = str(row.get("job_title") or row.get("title") or "").strip()
                if not (row_company and row_title):
                    continue
                if (generate_dedup_hash(row_company, row_title) == want_hash
                        or normalize_dedup_key(row_company, row_title) == want_key):
                    return {
                        "tab": tab_name,
                        # get_followups walks the sheet bottom-up, so this is a position within the
                        # returned list, not a spreadsheet row number - labelled loosely on purpose.
                        "row_label": f"#{idx + 1}",
                        "status": str(row.get("status") or ""),
                        "sheet_uuid": str(row.get("sheet_uuid") or ""),
                    }
        except Exception as e:
            logging.error(f"locate_tracked_role error ({target_code}): {e}")
    return None


def invalidate_tracked_role_cache():
    """Force the next tracked-role check to re-read Sheets.

    Deleting a row by hand is invisible to this process, so without a way to clear the cache the
    gate keeps blocking a role for up to the TTL after Kevin has removed it.
    """
    _TRACKED_ROLE_CACHE["data"] = set()
    _TRACKED_ROLE_CACHE["fetched_at"] = 0.0
    return True


def is_role_tracked(company, title):
    """True when this company+role already has a live CRM row, under EITHER dedup algorithm.

    Callers must use this rather than testing `generate_dedup_hash(...) in get_tracked_job_keys()`,
    which only ever checks one of the two keys the set now holds.
    """
    tracked = get_tracked_job_keys()
    return (
        generate_dedup_hash(company, title) in tracked
        or normalize_dedup_key(company, title) in tracked
    )


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


def backfill_job_contacts_from_carmen_cold(dry_run=True):
    """Fill a job row's Contact Email from the real person already emailed at that company.

    A job row lands in Tetiana Warm carrying whatever resolve_target_email() guessed, which is
    often Kevin's own address or a role mailbox - useless as a record of who was actually
    contacted. Meanwhile Carmen Cold holds the real human (awarner@crain.com) captured from Sent
    mail or typed into /e. Both tabs are keyed by company, so the join already exists; nothing
    was reading across it.

    Matching is on normalize_company_for_match(), the same key the warm/Clavicular routing uses,
    so "Intact Services USA LLC" on the job row meets "Intact Services USA" on the contact row.
    That normalizer strips only trailing legal suffixes, so "Crain" and "Crain Communications"
    stay DISTINCT - deliberately. They are different Carmen Cold rows with different people, and
    collapsing them would write one company's contact onto another company's job.

    Three guards, each of which exists because of a row in the current sheet:
      - company_domain_of() rejects consumer mail, so the Slate Auto row whose "contact" is
        kjmiller406@gmail.com is never copied onto a job. Kevin's own address is not a contact.
      - is_role_mailbox() rejects operations@/careers@, which carry no more information than the
        guess already sitting in the cell.
      - A job row whose existing email is already a real person's is left alone. Only a blank,
        a role mailbox or a consumer address gets overwritten.

    When several people share a company, the FIRST by Carmen Cold row order wins and the rest are
    reported, so a two-contact company like Crain is visible rather than silently truncated.

    Returns (updates, skipped) where updates is a list of dicts describing each write. dry_run
    leaves the sheet untouched, which is how /fillcontacts previews before committing.
    """
    res = crm_post({"action": "get_followups", "tab": "CC"})
    if not res or res.status_code != 200:
        logging.warning("[FILLCONTACTS] Carmen Cold unavailable")
        return [], []
    try:
        cold_rows = res.json().get("followups", []) or []
    except Exception as e:
        logging.error(f"[FILLCONTACTS] Carmen Cold parse error: {e}")
        return [], []

    # company -> [contacts]. get_followups re-sorts by next-followup date, and every fresh Carmen
    # Cold row is stamped with the same first-rung interval, so several people messaged at one
    # company on the same day arrive TIED and in no meaningful order. Sorting by date_added
    # (Column A, Last Contact Date) makes the pick deterministic: the first person contacted at a
    # company is the one promoted onto the job row, and it stays that way across re-runs instead
    # of flipping between contacts as the sheet is re-read. Blank dates sort last, not first, so a
    # row missing Column A can never displace a real dated contact.
    by_company = {}
    for row in cold_rows:
        email = str(row.get("email") or "").strip().lower()
        company = str(row.get("company") or "").strip()
        if not email or not company:
            continue
        if is_role_mailbox(email) or not company_domain_of(email):
            continue
        by_company.setdefault(normalize_company_for_match(company), []).append({
            "email": email,
            "name": str(row.get("name") or "").strip() or name_from_email_local_part(email),
            "raw_company": company,
            "date_added": str(row.get("date_added") or "").strip(),
        })
    for contacts in by_company.values():
        contacts.sort(key=lambda c: (c["date_added"] == "", c["date_added"], c["email"]))

    updates, skipped = [], []
    for tab_code in ("TW", "TC"):
        jres = crm_post({"action": "get_followups", "tab": tab_code})
        if not jres or jres.status_code != 200:
            continue
        try:
            job_rows = jres.json().get("followups", []) or []
        except Exception:
            continue
        for job_row in job_rows:
            sheet_uuid = str(job_row.get("sheet_uuid") or "").strip()
            company = str(job_row.get("company") or "").strip()
            current = str(job_row.get("email") or "").strip().lower()
            if not sheet_uuid or not company:
                continue
            # An address that is already a real person at a real company is the best record there
            # is. Never overwrite it with a different contact at the same firm.
            if current and not is_role_mailbox(current) and company_domain_of(current):
                continue
            candidates = by_company.get(normalize_company_for_match(company))
            if not candidates:
                continue
            chosen = candidates[0]
            if chosen["email"] == current:
                continue
            updates.append({
                "sheet_uuid": sheet_uuid,
                "company": company,
                "title": str(job_row.get("title") or "").strip(),
                "old_email": current,
                "new_email": chosen["email"],
                "contact_name": chosen["name"],
                "tab": tab_code,
                "alternates": [c["email"] for c in candidates[1:]],
            })
            if len(candidates) > 1:
                skipped.append({"company": company, "alternates": [c["email"] for c in candidates[1:]]})

    if not dry_run:
        for u in updates:
            enqueue_crm_payload(build_crm_payload(
                "update_contact_email", sheet_uuid=u["sheet_uuid"], email=u["new_email"]
            ))
            update_job_target_email(u["sheet_uuid"], u["new_email"])
            enqueue_crm_payload(build_crm_payload(
                "append_note", sheet_uuid=u["sheet_uuid"],
                note=f"Contact filled from Carmen Cold: {u['contact_name']} <{u['new_email']}>"
            ))
        logging.info(f"[FILLCONTACTS] Wrote {len(updates)} job contact email(s)")

    return updates, skipped


def auto_fill_job_contacts_from_carmen_cold():
    """Scheduled wrapper: commit the Carmen Cold -> job row contact fill and report what changed.

    Runs unattended on the email poll cadence, so unlike /fillcontacts there is no preview step.
    That is safe because the underlying function only ever overwrites a blank, a role mailbox or
    a consumer address, and never replaces one real person with another - the destructive case
    does not exist. Kevin still gets a Telegram summary, because a silent write to a sheet he
    curates by hand is how a wrong address survives unnoticed for a week.

    Idempotent by construction: once a row carries a real contact it no longer qualifies, so a
    re-run is a no-op rather than a repeated write. Returns the number of rows updated.
    """
    updates, _ = backfill_job_contacts_from_carmen_cold(dry_run=False)
    if not updates:
        return 0

    logging.info(f"[FILLCONTACTS] Auto-filled {len(updates)} job contact email(s)")
    if TELEGRAM_CHAT_ID:
        lines = [
            f"• <b>{html.escape(u['company'])}</b>"
            + (f" - {html.escape(u['title'])}" if u["title"] else "")
            + f"\n   <code>{html.escape(u['old_email'] or '(blank)')}</code> → "
            f"<code>{html.escape(u['new_email'])}</code>"
            for u in updates[:10]
        ]
        more = f"\n\n<i>+{len(updates) - 10} more</i>" if len(updates) > 10 else ""
        send_telegram_message(
            TELEGRAM_CHAT_ID,
            f"🔗 <b>Job Contacts Auto-Filled ({len(updates)})</b>\n"
            f"<i>matched from Carmen Cold</i>\n\n" + "\n".join(lines) + more
        )
    return len(updates)


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

# Legal-entity tokens stripped off the END of a company name by clean_company_for_copy().
# Order is irrelevant (the strip loops), but anchoring is not - see the docstring.
_LEGAL_SUFFIX_PATTERN = re.compile(
    r'[\s,]*\b(inc|llc|llp|pllc|ltd|limited|corp|corporation|co|plc|gmbh|pty|nv|ag)\b\.?\s*$',
    re.IGNORECASE
)

def clean_company_for_copy(company_name):
    """Drops legal-entity suffixes so outreach copy reads 'Atwell', not 'Atwell Group, Inc.'.

    The strip is ANCHORED TO THE END of the name and applied repeatedly. It used to be an
    unanchored \\b(inc|co|group|...)\\b sweep, which removed those words wherever they appeared:
    'Group 1 Automotive' became '1 Automotive', 'Co-Diagnostics' became '-Diagnostics', and
    'The Corporation for Public Broadcasting' became 'The for Public Broadcasting'. A legal suffix
    is by definition trailing, so anchoring fixes every one of those without weakening the actual
    suffix removal ('Atwell Group, Inc.' still loops down to 'Atwell').

    'group', 'holdings' and 'companies' are deliberately NOT in the suffix list. They read as
    legal noise in 'Atwell Group' but they are the actual name in 'Boston Consulting Group',
    'Rocket Companies' and 'Alliance Group Holdings', and there is no way to tell those apart from
    the string. Addressing 'Rocket Companies' as 'Rocket' is a worse error than leaving 'Group' on
    'Atwell Group', because the first one looks like a mail merge that failed.

    Looping matters for stacked suffixes ('Atwell, Inc. Ltd'); the fallback matters because some
    real firms ARE a bare legal word. Falls back to the raw value whenever stripping empties it.
    """
    raw = str(company_name or "").strip()
    clean = raw
    # Loop so stacked suffixes ("Atwell Group, Inc.") come off one token at a time. Bounded by
    # the fact that each pass must shorten the string or break.
    while True:
        stripped = _LEGAL_SUFFIX_PATTERN.sub('', clean).strip().rstrip(',').strip()
        if stripped == clean or not stripped:
            break
        clean = stripped
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
    # LLP/PLLC used to be stripped again here, because the shared suffix list had llc/plc but not
    # llp. The list now covers both, so the filename and the outreach copy strip identically.
    clean = clean_company_for_copy(raw)
    # "&" joins words rather than separating them: "AT&T" -> "ATT", not "AT_T".
    clean = clean.replace("&", "")
    # Collapse each remaining run of non-alphanumerics to one underscore, so "A.B. Smith" -> "A_B_Smith".
    slug = re.sub(r'[^A-Za-z0-9]+', '_', clean).strip('_')
    # 64 chars keeps the whole filename comfortably clear of the 255-byte limit some ATS
    # upload forms and Windows paths enforce, without truncating any realistic company name.
    slug = slug[:64].rstrip('_')
    return f"Kevin_Miller_Resume_{slug}.pdf" if slug else "Kevin_Miller_Resume.pdf"

def render_outreach_email(pool_key, template_id=0, name="", company="", job_title="", their_desk=""):
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
        template, name=name, company=clean_company_for_copy(company), job_title=job_title,
        their_desk=their_desk,
    ))

def generate_cold_email(job_title, company_name, template_id=0, contact_name="", their_desk=""):
    """Cold email body from the cold_ops bank. `template_id` is the Gemini-routed
    outreach_template_id persisted on the cached job, so /draft re-renders the same entry the
    card showed instead of always falling back to cold_ops[0].

    `their_desk` is optional and empty on the automated path; it is filled only when Kevin has
    done the manual pass-2 profile read. Unfilled, interpolate_template() supplies the generic
    clause, so every existing caller renders exactly as it did before the slot existed."""
    return render_outreach_email("cold_ops", template_id, name=contact_name, company=company_name, job_title=job_title, their_desk=their_desk)

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


# A JD scoring at/above this is one Kevin would take, so its vocabulary is what he should be
# writing. Terms below it still get counted (they are the contrast set that stops /gaps from
# recommending language common to EVERY posting), but only hi_fit_docs drives the ranking.
HI_FIT_THRESHOLD = 85


def record_jd_terms(job_desc, fit_score):
    """Fold one scored JD's vocabulary into jd_term_yield. Observation only - never changes how a
    job is scored, filtered or carded, so it is safe to call on every evaluated posting.

    Silent on failure by design: this is telemetry, and a locked DB must never cost Kevin a card.
    """
    try:
        score = safe_int(fit_score, 0)
        terms = extract_jd_terms(job_desc)
        if not terms:
            return 0
        hi = 1 if score >= HI_FIT_THRESHOLD else 0
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany("""
                INSERT INTO jd_term_yield (term, docs, fit_sum, hi_fit_docs)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(term) DO UPDATE SET
                    docs = docs + 1,
                    fit_sum = fit_sum + excluded.fit_sum,
                    hi_fit_docs = hi_fit_docs + excluded.hi_fit_docs,
                    last_seen = CURRENT_TIMESTAMP
            """, [(t, score, hi) for t in terms])
            conn.commit()
        return len(terms)
    except Exception as e:
        logging.error(f"JD term record error: {e}")
        return 0


def get_resume_vocabulary():
    """Every term Kevin's resume/outreach copy already claims, normalized for comparison.

    Union of core_skills and the bullet bank. A gap term is one the MARKET uses that this set does
    not, so the bank has to be read as live text rather than assumed - editing a bullet changes
    what counts as a gap.
    """
    vocab = set()

    def _fold(text):
        for term in extract_jd_terms(text, max_terms=400):
            vocab.add(term)

    for skill in get_filter("core_skills", []) or []:
        s = str(skill).strip().lower()
        if s:
            vocab.add(s)
            _fold(s)
    try:
        with open(RESUME_BULLETS_BANK_PATH, "r", encoding="utf-8") as f:
            bank = json.load(f)
        for bullets in (bank or {}).values():
            for bullet in bullets or []:
                _fold(bullet)
    except Exception as e:
        logging.error(f"Resume vocabulary read error: {e}")
    return vocab


def load_resume_bullet_tracks():
    """The bullet bank as {track_key: [bullet, ...]}. Empty dict if unreadable."""
    try:
        with open(RESUME_BULLETS_BANK_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        logging.error(f"Resume bullet bank read error: {e}")
        return {}


def resolve_bullet_track_key(arg, tracks):
    """Accept 'e', 'track_e', 'bizops' or the full key for track_e_bizops."""
    want = str(arg or "").strip().lower().replace("-", "_")
    if not want:
        return None
    if want in tracks:
        return want
    for key in tracks:
        tail = key[len("track_"):] if key.startswith("track_") else key
        letter = tail.split("_", 1)[0]
        name = tail.split("_", 1)[1] if "_" in tail else ""
        if want in (letter, f"track_{letter}", name, tail):
            return key
    return None


def draft_bullets_for_gaps(track_key, existing_bullets, gaps, max_bullets=5):
    """Ask Gemini to phrase resume bullets that use the market's vocabulary.

    NOTHING IS WRITTEN. The output is printed for Kevin to accept, edit or bin, and the caller
    labels it unverified. That gate is the whole design: a bullet is a factual claim about his
    work history, and a model optimizing for keyword coverage will happily assert experience he
    does not have - which is a claim he then has to defend in an interview. So the prompt is
    framed as rephrasing what he already did, the existing bank is passed as the ground truth,
    and the model is told to leave a term alone when his history does not support it.
    """
    terms = [g["term"] for g in (gaps or [])][:12]
    if not terms or not existing_bullets:
        return []
    system_prompt = (
        "You rephrase EXISTING resume bullets to use the vocabulary a job market actually uses. "
        "You are strictly forbidden from inventing experience. Every bullet you return must be a "
        "rewording of a bullet you were given, describing the SAME work, the same systems and the "
        "same metrics. If a target term does not honestly fit any existing bullet, omit that term "
        "- an omitted term is a correct answer, a fabricated one is a failure. Never invent "
        "numbers, employers, tools or dates, and never inflate a metric you were given."
    )
    prompt = (
        "EXISTING BULLETS (the only work history that is true):\n"
        + "\n".join(f"- {b}" for b in existing_bullets)
        + "\n\nTARGET VOCABULARY (terms from high-fit job postings):\n"
        + ", ".join(terms)
        + f"\n\nReturn JSON: {{\"bullets\":[{{\"bullet\":\"...\",\"covers\":[\"term\"],"
          "\"based_on\":\"the existing bullet you rewrote\"}}]}}\n"
        f"At most {max_bullets} bullets. Each under 200 characters, starting with a past-tense "
        "verb. Only include a bullet if it genuinely reflects the source bullet's work."
    )
    raw = call_gemini_api(prompt, system_prompt=system_prompt)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception as e:
        logging.error(f"Bullet draft parse error: {e}")
        return []
    out = []
    for item in (data or {}).get("bullets", [])[:max_bullets]:
        if isinstance(item, dict) and str(item.get("bullet", "")).strip():
            out.append({
                "bullet": str(item["bullet"]).strip()[:300],
                "covers": [str(c) for c in (item.get("covers") or [])][:6],
                "based_on": str(item.get("based_on", ""))[:300],
            })
    return out


def get_jd_term_gaps(limit=25, min_docs=2):
    """Terms the market uses in high-fit roles that Kevin's resume copy never says.

    Ranked by hi_fit_docs: a term in ten 90-scoring postings matters more than one in fifty
    postings that averaged 40. min_docs drops one-off vocabulary from a single weird listing.
    """
    try:
        with get_db_conn() as conn:
            rows = conn.execute("""
                SELECT term, docs, fit_sum, hi_fit_docs
                FROM jd_term_yield
                WHERE docs >= ?
                ORDER BY hi_fit_docs DESC, docs DESC
                LIMIT 400
            """, (min_docs,)).fetchall()
    except Exception as e:
        logging.error(f"JD term gap read error: {e}")
        return []

    owned = get_resume_vocabulary()
    gaps = []
    for term, docs, fit_sum, hi_fit_docs in rows:
        if term in owned:
            continue
        # A bigram whose halves Kevin already claims is not a gap - "process automation" when the
        # bank says both "process" and "automation" adds nothing to go rewrite bullets over.
        parts = term.split()
        if len(parts) > 1 and all(p in owned for p in parts):
            continue
        gaps.append({
            "term": term,
            "docs": docs,
            "hi_fit_docs": hi_fit_docs,
            "avg_fit": int(fit_sum / docs) if docs else 0,
        })
        if len(gaps) >= limit:
            break
    return gaps

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
    # Plain job-family titles, no seniority marker either way. "Operations Analyst", "EHR Clinical
    # Analyst" and "Business Administrator" are the roles actually being targeted, but none of them
    # carry a junior/entry word, and the branch above only matches "analyst i" - so a bare "Analyst"
    # scored the same as a title the filter had never heard of. Smaller than the +6 above because
    # the word alone is weaker evidence than an explicit entry-level marker; the seniority and
    # wrong-family branches still win outright, since this is the last elif in the chain.
    elif re.search(r'\b(analyst|operations|administrator|assistant)\b', title):
        bonus += 4

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
    # Pass the timestamp FIELD, not the job dict: parse_posted_hours fails open to 48 on anything
    # it cannot parse, so handing it the dict silently scored every listing as exactly 48h old -
    # the +8 bonus fired on all of them and the >=720h penalty never once fired.
    posted_hours = parse_posted_hours(job.get("job_posted_at_datetime_utc"))
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

    # A pasted EMAIL ADDRESS is the most likely way to misuse this: "/cold kev@gmail.com" parses
    # cleanly as name="kev", company="gmail.com" and silently creates a contact at a company that
    # does not exist. These commands take a COMPANY NAME, never an address, so a first company
    # token that looks like a bare domain is rejected and the caller shows the real format.
    first_company_token = rest.split()[0] if rest.split() else ""
    if re.fullmatch(r'[a-z0-9-]+\.[a-z]{2,}', first_company_token.lower()):
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
    "dedup_title": "Duplicate within this run",
    "dedup_content": "Duplicate description this run",
    "already_tracked": "Already in Tetiana Cold/Warm",
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
    """Counts why candidates were dropped during one pipeline run.

    Also attributes raw/passed counts back to the SOURCE QUERY when one is set, which answers a
    question the rejection tally cannot: is a given search phrase earning its slot in the 110-query
    bank? A phrase that returns nothing, or only listings that every gate rejects, is costing an
    API call per run for no candidates - and there is no other way to tell it apart from a phrase
    that is merely unlucky this slice.
    """

    def __init__(self):
        self.raw = 0
        self.passed = 0
        self.reasons = {}
        self.current_query = None
        self.per_query = {}

    def set_query(self, query):
        """Attribute subsequent candidates to `query` (None = non-JSearch sources: ATS boards,
        remote feeds, warm sweeps - those have no search phrase to credit)."""
        self.current_query = query or None
        if self.current_query and self.current_query not in self.per_query:
            self.per_query[self.current_query] = {"raw": 0, "passed": 0}

    def _bump(self, field):
        if self.current_query:
            self.per_query[self.current_query][field] += 1

    def note(self, reason):
        """Record one rejection. Unknown reasons are counted under their raw key rather than
        dropped, so a gate added without a label still shows up in the summary."""
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def query_yield_report(self, limit=12):
        """Per-query raw->passed counts, worst first. Empty when no query was ever attributed."""
        if not self.per_query:
            return ""
        ranked = sorted(self.per_query.items(), key=lambda kv: (kv[1]["passed"], -kv[1]["raw"]))
        lines = [f"{stats['raw']:>3} raw -> {stats['passed']} passed  {query}"
                 for query, stats in ranked[:limit]]
        return "\n".join(lines)

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
    # Two ways to clear the geography gate:
    #   1. The city matches the hand-maintained metro allowlist (the precise path).
    #   2. The posting is in Michigan at all, per an explicit state field or a "..., MI" city string.
    #
    # Rule 2 exists because rule 1 alone was rejecting a third of every run. valid_cities is a
    # hand-curated suburb list, not a computed geofence, so a real in-radius job whose city string
    # is merely unfamiliar ("Bingham Farms", "Detroit Metro", "Southeast Michigan") looked exactly
    # like an out-of-area reject and was dropped silently. Every JSearch query is already
    # radius-limited to radius_miles around a metro anchor, so a Michigan result that survived
    # sourcing is overwhelmingly local; letting Grand Rapids through occasionally is far cheaper
    # than dropping a Farmington Hills job because the suburb was never typed into a list.
    is_in_metro_area = any(c in city for c in valid_cities)
    is_michigan = state in ("mi", "michigan") or bool(re.search(r",\s*mi\b", city)) or "michigan" in city
    if not (is_in_metro_area or is_michigan):
        # Logged with the city, not just counted: a real in-radius suburb missing from the list
        # looks identical to a genuine out-of-area reject, and the city name is the only way to
        # tell them apart - or to know what is worth adding.
        if trace is not None:
            logging.info(f"[EXCLUDED] city '{city}' not in valid_cities allowlist (state={state!r})")
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

# Fallbacks for a forced card whose AI rejection left the routing keys unset. Track A is the
# wealth-ops bank, the closest thing to a neutral default for the roles Kevin pastes by hand;
# the indices are bounds-checked by filter_ats_bullets() regardless.
DEFAULT_FORCED_TRACK = "a"
DEFAULT_FORCED_BULLET_INDICES = [1, 4, 7]


def process_single_candidate(job, force=False):
    log_metric_event("ai_screened", source=derive_job_source(job.get("job_id")))
    ai_pass, score, reason, track, tone_mode, bullet_indices, linkedin_template_id, outreach_template_id, layer1_bonus, gemini_base = evaluate_job_with_gemini(job)

    # force=True (from /job!) makes the AI verdict ADVISORY instead of a gate. Kevin pasted this
    # link deliberately, so his judgment outranks the screener's - the score and reason still ride
    # on the card, they just stop deciding whether it exists. Everything downstream needs a track
    # and template ids to resolve copy, and a rejection can leave those unset or out of range, so
    # they fall back to defaults that filter_ats_bullets/resolve_template_text bounds-check anyway.
    forced_override = bool(force) and not ai_pass
    if forced_override:
        logging.warning(
            f"Forced card (/job!) overriding AI rejection for "
            f"{job.get('employer_name')} - {job.get('job_title')} (score {score}): {reason}"
        )
        track = track or DEFAULT_FORCED_TRACK
        tone_mode = tone_mode or "conservative"
        if not bullet_indices:
            bullet_indices = DEFAULT_FORCED_BULLET_INDICES
        # A rejection often carries score 0; a card needs a number that sorts sanely against the
        # rest of the pipeline without pretending this was a strong match.
        score = max(safe_int(score, 0), 1)
        # Mark it on the card. A forced card that looks identical to a scored one is a trap: weeks
        # later the sheet row gives no hint that the screener said no and Kevin overrode it.
        job["forced_override"] = True
        job["forced_override_reason"] = str(reason or "")
        # Carried into the sheet's Notes column by dispatch_tier1_matches, so the row itself records
        # that this was an override and what the screener objected to.
        reason = f"FORCED via /job! (screener said: {reason or 'no reason given'})"

    if ai_pass or forced_override:
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
        # Bank this JD's vocabulary against its fit score. Pure telemetry - see record_jd_terms().
        # This is the data /gaps and /bullets read; without it every posting's language is
        # computed for the Skills % and then discarded.
        record_jd_terms(job.get("job_description"), score)

        # Oddball Wildcard Badge: flags roles matching the rolling query bank's oddball keyword themes
        oddball_text = f"{job_title.lower()} {str(job.get('job_description') or '')[:300].lower()}"
        if any(kw in oddball_text for kw in ODDBALL_KEYWORDS):
            age_badge = f"{age_badge} 🎲 [WILDCARD ROLE]"

        # Forced cards are Kevin's call over the screener's, and the card must say so.
        if forced_override:
            age_badge = f"{age_badge} 🚩 [FORCED - AI SAID NO]"

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

def send_telegram_document(chat_id, file_bytes, filename, caption, command_label):
    """Uploads an in-memory PDF to Telegram as a document. Returns True on success.

    Factored out of the /cv handler, which was the only caller that knew how to do this. A failure
    here is logged and reported but never raised: the tap-to-copy text has already landed on the
    card by the time this runs, so a failed upload degrades the convenience, not the application.
    """
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
        files = {"document": (filename, io.BytesIO(file_bytes), "application/pdf")}
        res = requests.post(
            url,
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files=files, timeout=20
        )
        if res.status_code != 200:
            logging.error(f"{command_label} sendDocument failed ({res.status_code}): {res.text[:200]}")
            return False
        return True
    except Exception as e:
        logging.error(f"{command_label} sendDocument raised for {filename}: {e}")
        return False

def resolve_letter_for_job(job, mapping, comp):
    """THE cover letter every command renders, so /letter, /e and /eh cannot drift apart.

    Mirrors resolve_outreach_body()'s role for the email body: one function reads the routing off
    the cached job, so the letter attached to a draft is the same string the /letter card showed.
    Returns (letter_text, track). Reuses the resume's own track/index routing so the letter and the
    attached PDF argue one case - the reasoning documented in generate_cover_letter().
    """
    job = job or {}
    job_title = job.get("job_title") or "this role"
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
    return letter, track

def cover_letter_pdf_filename(company_name):
    """"Kevin_Miller_Cover_Letter_Atwell.pdf" - same slug rules as resume_pdf_filename()."""
    return resume_pdf_filename(company_name).replace("Kevin_Miller_Resume", "Kevin_Miller_Cover_Letter", 1)

def send_cover_letter_pdf_async(chat_id, letter_text, comp, track, command_label):
    """Compiles and sends the cover letter PDF on a background thread.

    Threaded for the same reason /backfillcontacts is: the Telegram card is already sent by this
    point, and the webhook handler should not hold the request open for a compile. A compile
    failure is logged and surfaced, never raised into the handler.
    """
    def _compile_and_send():
        try:
            pdf_bytes = compile_cover_letter_pdf(letter_text, comp)
            if not pdf_bytes:
                raise ValueError("compile_cover_letter_pdf returned empty bytes")
            caption = f"✉️ <b>Cover Letter: {html.escape(comp)}</b> · Track {html.escape(str(track).upper())}"
            send_telegram_document(
                chat_id, pdf_bytes, cover_letter_pdf_filename(comp), caption, command_label
            )
        except Exception as e:
            logging.error(f"{command_label} cover letter PDF failed for {comp}: {e}")
            send_telegram_message(chat_id, f"⚠️ Cover letter PDF failed: {html.escape(str(e)[:200])}")
    threading.Thread(target=_compile_and_send, daemon=True).start()

def stage_outreach_draft(chat_id, mapping, job, comp, title, is_warm, target, header_line, command_label):
    """THE shared tail of /e and /eh: resume PDF -> email body -> Gmail draft -> Telegram card.

    The two commands differ only in how `target` is obtained - /e takes a hand-typed address for
    free, /eh burns provider credits through resolve_email_waterfall() - and in the header line
    above the card. Everything after the address is identical, which is why it lives here once
    rather than being duplicated line-for-line in each handler.

    Deliberately does NOT send the cover letter. It used to send two extra Telegram messages here
    (the letter text and a compiled letter PDF) on the theory that an application needs both at
    once. In practice that buried the thing /e is for - the draft and its tap-to-copy body - under
    two messages Kevin had not asked for, on every single use. /letter renders the same letter,
    from the same resolve_letter_for_job() routing, when he actually wants it.

    The resume PDF is compiled here as an ATTACHMENT on the Gmail draft, and the same bytes are
    also posted to Telegram after the card. The resume - not the letter - is what Kevin uploads to
    a portal right after running /e, so it is the one file worth putting in the chat by default;
    it is sent AFTER the card and on a thread so the tap-to-copy body still lands first.

    Returns the created draft_id (or None), so callers can keep their own post-draft CRM writes.
    """
    track = job.get("track", "a")
    bullet_indices = job.get("bullet_indices")
    tone_mode = job.get("tone_mode", "conservative")
    pdf_filename = resume_pdf_filename(comp)
    pdf_bytes = compile_resume_pdf_resilient(chat_id, comp, track, bullet_indices, command_label, tone_mode=tone_mode)

    raw_email_text = resolve_outreach_body(job, mapping, title, comp, is_warm)
    # pdf_bytes is still compiled above and still posted to Telegram below - only the OUTBOUND
    # attachment is gated. See RESUME_ATTACH_TO_EMAIL.
    ok, gmail_msg, draft_id = create_gmail_draft(
        to_email=target, company_name=comp, job_title=title, is_warm=is_warm,
        custom_body=raw_email_text,
        pdf_bytes=pdf_bytes if RESUME_ATTACH_TO_EMAIL else None,
        pdf_filename=pdf_filename
    )
    monospaced_body = format_email_block(raw_email_text)
    draft_link_line = ""
    if draft_id:
        draft_url = html.escape(f"https://mail.google.com/mail/u/0/#drafts/{draft_id}", quote=True)
        draft_link_line = f"📱 <a href='{draft_url}'>Open Draft in Gmail</a>\n\n"
    confirm_msg = (
        f"{header_line}\n\n"
        f"{draft_link_line}"
        f"<b>Tap-to-Copy Email Body:</b>\n{monospaced_body}"
    )
    # Optimistic UI: confirm to Telegram first, dispatch the Sheets write in the background
    send_telegram_message(chat_id, confirm_msg)
    if ok:
        log_daily_activity("drafts_staged")

    # The resume that was just attached to the draft, posted to the chat so it can be uploaded to a
    # portal without a second command. Threaded for the same reason /letter's PDF is: the card is
    # already on screen, and the webhook should not be held open for an upload. Reuses the bytes
    # compiled above rather than recompiling - same file the employer receives, by construction.
    if pdf_bytes:
        def _send_resume():
            send_telegram_document(
                chat_id, pdf_bytes, pdf_filename,
                f"📄 <b>Resume: {html.escape(comp)}</b> · Track {html.escape(str(track).upper())}",
                command_label,
            )
        threading.Thread(target=_send_resume, daemon=True).start()

    return draft_id

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


def _gmail_header_value(header_list, name, default=""):
    """One header off a Gmail payload, matched case-insensitively.

    Header names are case-insensitive per RFC 5322 and senders genuinely vary the spelling -
    List-Unsubscribe in particular shows up as List-unsubscribe often enough that an exact-case
    lookup would let those newsletters through as human mail.
    """
    lowered = str(name).lower()
    return next(
        (h.get("value", "") for h in (header_list or []) if str(h.get("name", "")).lower() == lowered),
        default,
    )


def display_name_from_sender(sender_raw):
    """A readable human name for a From: header, for the alert's CRM Match line.

    name_from_email_local_part() expects a bare address, and a From: header usually is not one:
    handed "Andy Stemler <astemler@nextpathcp.com>" it splits on the @ and returns
    "Andy stemler <astemler". That was survivable while only thread participants reached it, but
    every unresolved sender now produces an alert, so it is the name Kevin reads on most of them.
    """
    display, address = parse_email_recipient(sender_raw)
    return display or name_from_email_local_part(address or sender_raw) or "Unknown"


def _decode_gmail_part_data(data):
    """Gmail part bodies are base64url with the padding stripped; restore it before decoding."""
    try:
        padded = str(data or "") + "=" * (-len(str(data or "")) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return ""


CLASSIFIER_BODY_CHARS = 2000


def extract_plain_body(payload, limit=CLASSIFIER_BODY_CHARS):
    """Pull readable text out of a Gmail payload for the classifier. Returns "" when there is none.

    Gmail's `snippet` is capped around 200 characters and cuts mid-sentence, so a recruiter who
    opens with two lines of pleasantries and puts the ask in paragraph three was being classified
    on the pleasantries alone. The message body is already in memory - the fetch is format=full so
    that extract_calendar_invite() can see .ics parts - it was simply being discarded.

    Prefers text/plain over text/html, because the HTML alternative of the same message is mostly
    markup and a tag-stripped version of it is noisier than the plain part. Falls back to stripped
    HTML when a sender provides no plain part at all.

    The quoted tail is cut: a reply to a long thread repeats the whole history, and the classifier
    matching "interview" inside Kevin's OWN earlier message would turn every reply into a false
    interview signal. Truncated to `limit` because only the top of a message carries the intent,
    and an unbounded body makes the vocabulary match slower and noisier, not better.
    """
    plain_parts, html_parts = [], []
    stack = [payload or {}]
    while stack:
        part = stack.pop()
        if not isinstance(part, dict):
            continue
        stack.extend(part.get("parts") or [])
        mime = str(part.get("mimeType") or "").lower()
        # An attachment has a filename; its bytes are not body text (and a .pdf decodes to noise).
        if str(part.get("filename") or "").strip():
            continue
        if mime.startswith("text/plain"):
            plain_parts.append(_decode_gmail_part_data((part.get("body") or {}).get("data")))
        elif mime.startswith("text/html"):
            html_parts.append(_decode_gmail_part_data((part.get("body") or {}).get("data")))

    text = "\n".join(p for p in plain_parts if p).strip()
    if not text and html_parts:
        raw = "\n".join(p for p in html_parts if p)
        raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
        raw = re.sub(r"(?is)<br\s*/?>|</p>", "\n", raw)
        text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))

    # Quoted-reply markers, in the order they appear in the wild. Everything from the first one on
    # is thread history, not what this person just wrote.
    for marker in (r"\r?\n\s*On .{0,120}? wrote:", r"\r?\n\s*-{2,}\s*Original Message",
                   r"\r?\n\s*_{5,}", r"\r?\n\s*From:\s.{0,80}?\r?\nSent:",
                   r"\r?\n\s*>{1,}\s"):
        cut = re.search(marker, text)
        if cut:
            text = text[:cut.start()]
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def _format_ics_dtstart(raw_value, tzid=""):
    """Render an iCalendar DTSTART as readable text, or None if it is not a shape we parse.

    None is a real answer here, not a failure to handle: an alert saying "calendar invite" with no
    time is useful, and an alert showing a time that was guessed at is worse than useless, because
    Kevin would plan around it. Named zones are printed as the zone name rather than converted -
    labelling 2pm as America/New_York is honest, and silently shifting it is how you show up an
    hour late.
    """
    value = str(raw_value or "").strip()
    if not value:
        return None
    zone_note = ""
    if value.endswith("Z"):
        zone_note = " UTC"
        value = value[:-1]
    elif tzid:
        zone_note = f" ({tzid})"
    for fmt, out in (("%Y%m%dT%H%M%S", "%a %b %d, %Y %I:%M %p"), ("%Y%m%d", "%a %b %d, %Y")):
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        rendered = parsed.strftime(out).replace(" 0", " ")
        return f"{rendered}{zone_note}".strip()
    return None


def extract_calendar_invite(payload):
    """Find a calendar invitation inside a Gmail message payload. Returns (is_invite, start_text).

    A meeting invite is the least ambiguous interview signal that exists - nobody sends an .ics to
    a stranger by accident - and it is also the signal the text filters are worst at, because the
    human-written part of an invite is often empty. Kevin's Raymond James interview arrived exactly
    this way and was discarded.

    Detection is the MIME part (text/calendar, or a .ics attachment) or METHOD:REQUEST anywhere in
    a decoded part, per RFC 5546 - a reply or a cancellation carries METHOD:REPLY/CANCEL instead
    and is not a new invitation. start_text is None whenever DTSTART is missing or unparseable;
    see _format_ics_dtstart for why that is deliberate.

    Requires the message to have been fetched with format=full - format=metadata returns headers
    only, with no payload.parts to walk.
    """
    is_invite = False
    ics_text = ""
    stack = [payload or {}]
    while stack:
        part = stack.pop()
        if not isinstance(part, dict):
            continue
        stack.extend(part.get("parts") or [])
        mime_type = str(part.get("mimeType") or "").lower()
        filename = str(part.get("filename") or "").lower()
        is_calendar_part = mime_type.startswith(("text/calendar", "application/ics")) or filename.endswith(".ics")
        decoded = _decode_gmail_part_data((part.get("body") or {}).get("data"))
        if "METHOD:REQUEST" in decoded.upper():
            is_invite = True
        if is_calendar_part:
            is_invite = True
            if decoded:
                ics_text = decoded

    if not is_invite:
        return False, None

    # Unfolded first: iCalendar wraps long lines with a CRLF + single space, which can split a
    # DTSTART across two lines and make the regex below quietly find nothing.
    unfolded = re.sub(r"\r?\n[ \t]", "", ics_text)
    match = re.search(r"^DTSTART([^:\r\n]*):([^\r\n]+)", unfolded, re.MULTILINE)
    if not match:
        return True, None
    tzid_match = re.search(r"TZID=([^;:]+)", match.group(1) or "")
    return True, _format_ics_dtstart(match.group(2), tzid_match.group(1) if tzid_match else "")


def classify_inbound_ats_email(sender: str, subject: str, snippet: str):
    """
    Classifies ATS email into 'offer', 'interview', 'rejection', or 'general'.
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

    # OFFER is checked before INTERVIEW: the email that extends an offer almost always recaps the
    # interviews that led to it ("the team was impressed by your interviews"), so an offer letter
    # matched INTERVIEW_SET and was announced as an interview signal. Offer is the more advanced
    # stage, and the badge Kevin sees should name the thing that actually happened.
    #
    # Placed AFTER rejection for the same reason interview is: "we regret that we cannot offer you
    # the position" contains "offer", and a decline must never be announced as an offer.
    offer_patterns = [
        r"(?:thrilled|pleased|excited|happy|delighted) to (?:extend|offer)",
        r"extend (?:you )?an offer", r"offer of employment", r"job offer", r"internship offer",
        r"formal offer", r"offer letter", r"we would like to offer",
        # Acceptance mechanics: the paperwork and start-date mail that follows an accepted offer is
        # every bit as time-critical as the offer itself, and it never repeats the word "offer".
        #
        # A bare "welcome (you )?to \w+" used to live here and matched EVERY SaaS signup on earth -
        # "Welcome to Jobright!" was announced as a possible offer. Product onboarding and job
        # onboarding share almost all of their vocabulary ("welcome", "onboarding", "get started"),
        # so each phrase here has to carry something a marketing blast would not say: a team you
        # are joining, a first day, or hiring paperwork by name.
        r"welcome to the team", r"officially welcome you",
        r"welcome (?:you )?aboard", r"excited to have you (?:join|on)",
        r"your first day", r"first day is", r"start date", r"new hire (?:paperwork|onboarding)",
        r"docusign", r"\bi-9\b", r"background check", r"offer letter attached",
    ]
    if any(re.search(p, text) for p in offer_patterns):
        return "OFFER_EXTENDED", "update_offer"

    # Two families. The formal ATS phrasings were all this used to match, but Kevin's outreach is
    # peer-to-peer cold email, and a peer agreeing to talk does not write "invitation to
    # interview" - they write "happy to chat, do you have 15 minutes Thursday?". Those replies
    # scored GENERAL, so the interview metric and the outcome record never fired on exactly the
    # conversations the whole pipeline exists to produce.
    interview_patterns = [
        # formal / ATS
        r"invit(?:ation|e you|ing you) to (?:an? )?interview", r"interview request",
        # "schedule a call" is also every SaaS sales CTA ("Schedule a call, an expert will handle
        # your taxes"), so it now needs a word that means THIS conversation - with you, with the
        # team, about the role - rather than standing alone.
        r"schedule a (?:call|time|screen|chat|meeting)\b.{0,40}?\b(?:with you|with our|with the|about the (?:role|position)|to discuss)",
        r"(?:like|love|want) to schedule a (?:call|time|screen|chat|meeting)",
        r"selected for an interview", r"next steps with", r"speaking with our team",
        r"move forward with your application",
        # "set up" alone matched "your account is set up!" and "Set up advanced security" - it is
        # in practically every onboarding email written. Bind it to the thing being set up.
        r"set (?:up|something up)\b.{0,30}?\b(?:call|time|chat|meeting|interview|conversation|screen)",
        r"(?:let'?s|can we|could we|happy to) set (?:up|something up)",
        # peer-to-peer acceptance
        r"happy to (?:chat|talk|connect|hop on)", r"(?:would|i'?d) love to (?:chat|talk|connect)",
        r"(?:are|r) you (?:free|available)", r"do you have (?:a few|some|\d+)\s*(?:minutes|mins)",
        r"send (?:over|me) some times", r"what(?:'s| is) your availability",
        r"works for me", r"let'?s (?:chat|talk|connect|set)", r"grab (?:15|20|30|a few)",
        r"calendly", r"book a time",
        # The bare word, which every pattern above managed to miss. Two real interview emails
        # classified GENERAL while their subject lines literally read "Interview" - the phrases
        # were all written for formal ATS copy, and a recruiter writing to a human just says
        # "interview". Safe to add only because rejection is matched first above: "we will not be
        # moving forward to interview" is already a REJECTION before this line is reached.
        r"\binterview\b",
        # Meeting mechanics. A calendar invite's own body is the strongest signal there is, and it
        # rarely contains any of the phrasing above - it contains an RSVP prompt and a join link.
        # A bare "RSVP" is every event-marketing blast ("Last chance to RSVP and meet the team
        # at Bloomberg"), so it counts only near something that means an actual conversation.
        # The join links stay unqualified - a Zoom/Teams meeting link is not newsletter content.
        # "meeting request"/"meeting invite" is included because that is what a calendar RSVP
        # actually says; a bare "meet the team at <brand>" event blast still misses, because it
        # carries no "meeting" noun.
        r"\brsvp\b.{0,60}?\b(?:interview|screen|conversation|meeting|your (?:call|time|slot))",
        r"\b(?:interview|screen|conversation|meeting)\b.{0,60}?\brsvp\b",
        r"zoom\.us/j/", r"teams\.microsoft\.com/l/meetup",
        # "Invitation" alone is a newsletter word ("invitation to our webinar"), so it only counts
        # within a short distance of something that means an actual conversation.
        r"\binvitation\b.{0,60}?\b(?:interview|meeting|call|chat|conversation|screen)\b",
        r"\b(?:interview|meeting|call|chat|conversation|screen)\b.{0,60}?\binvitation\b",
    ]
    if any(re.search(p, text) for p in interview_patterns):
        return "INTERVIEW_SET", "update_interview"

    return "GENERAL", None

def passes_email_sender_blocks(sender: str):
    """The sender rules that hold even for a Tier 1 interview signal: the no-reply@ blacklist, the
    blocked-domain list and the allow-list. Returns (passed: bool, rejection_reason: str).

    Split out of passes_email_prefilter() so the Tier 1 bypass in check_inbound_gmail_replies()
    can honour exactly these three and nothing else. The line is who the sender is, not what the
    message says: a calendar invite from a robot mailbox is still a robot, but a calendar invite
    from a stranger is precisely the case the bypass exists for.

    Note the blacklist matches the ADDRESS, not the domain - "no-reply@" is a substring test
    against the full address, so a real person at a company whose marketing mail comes from
    no-reply@ is unaffected. That is intentional and should stay that way.
    """
    email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", sender or "")
    sender_email = email_match.group(0).lower().strip() if email_match else ""
    sender_domain = sender_email.split("@")[-1] if sender_email else ""

    # 1. Sender blacklist (substring match, e.g. "no-reply@", "noreply@")
    blacklist = [s.strip().lower() for s in EMAIL_SENDER_BLACKLIST.split(",") if s.strip()]
    if blacklist and any(b in sender_email for b in blacklist):
        # ATS carve-out. Workday, Greenhouse, Lever and iCIMS send REAL interview invitations and
        # scheduling links from noreply@ mailboxes, and the blacklist is a substring test on the
        # address - so "noreply@myworkday.com" was blocked, ahead of the Tier 1 bypass, and marked
        # read. Kevin's two real interviews came from named humans, which is why this never showed
        # up; the moment an employer runs scheduling through their ATS, it would have.
        #
        # Narrow on purpose: it keys on the DOMAIN, not on the message text, so a robot mailbox at
        # a random domain gains nothing. Blocked domains and the allow-list below still apply, and
        # the caller decides what to do with the result - this only declines to block.
        if sender_domain and any(sender_domain == d or sender_domain.endswith("." + d)
                                 for d in ATS_ROBOT_DOMAINS):
            logging.info(f"[ATS CARVE-OUT] {sender_email} is a blacklisted mailbox at a known ATS "
                         f"domain - allowed through the sender blacklist")
        else:
            return False, f"sender blacklisted ({sender_email})"

    # 1b. Bulk sender domains. Runs AFTER the ATS carve-out on purpose: criteriacorp.com sends
    # pre-hire assessments from DO-NOT-REPLY@ and must survive, while lensa.com sends job alerts
    # from a human-looking name and must not. Subdomain-aware, because CVS sends from
    # mynotifications.cvs.com and Hevy from update.hevyapp.com.
    if sender_domain and any(sender_domain == d or sender_domain.endswith("." + d)
                             for d in EMAIL_BULK_SENDER_DOMAINS):
        return False, f"bulk sender domain ({sender_domain})"

    # 2. Blocked domains
    block_domains = [d.strip().lower() for d in EMAIL_BLOCK_DOMAINS.split(",") if d.strip()]
    if sender_domain and block_domains and sender_domain in block_domains:
        return False, f"domain blocked ({sender_domain})"

    # 3. Allow-list domains (if configured, sender domain MUST be present)
    allow_domains = [d.strip().lower() for d in EMAIL_ALLOW_DOMAINS.split(",") if d.strip()]
    if allow_domains and sender_domain not in allow_domains:
        return False, f"domain not in allow-list ({sender_domain})"

    return True, ""

def passes_email_prefilter(sender: str, subject: str, snippet: str, internal_date_ms=None, in_reply_to="", references="", list_unsubscribe=""):
    """Bulk-vs-human pre-filter shield, enforced BEFORE any CRM whitelist check runs.
    Returns (passed: bool, rejection_reason: str).

    This layer is no longer a spam filter and should not be read as one. The poll query is
    `is:unread -from:me label:INBOX`, and Gmail files spam and trash under separate labels, so
    every message reaching here has already been judged not-spam by Google. Duplicating that
    judgement with a keyword whitelist is how a DKIM-signed recruiter email that Gmail itself
    flagged Important got thrown away. What is left for this layer to decide is bulk versus human.
    """
    subject_l = str(subject or "")
    combined_text = f"{subject} {snippet}".lower()

    # 1-3. Sender blacklist, blocked domains, allow-list. Shared with the Tier 1 bypass, which
    # honours these and skips everything below.
    passed, reason = passes_email_sender_blocks(sender)
    if not passed:
        return False, reason

    # 4. Excluded keywords (subject/body)
    excluded_kws = [k.strip().lower() for k in EMAIL_EXCLUDED_KEYWORDS.split(",") if k.strip()]
    if excluded_kws and any(kw in combined_text for kw in excluded_kws):
        return False, "excluded keyword matched"

    # 5. Bulk mail, identified by the header a bulk sender is legally obliged to set rather than by
    # guessing at vocabulary. This is the single rule that separates Andy Stemler from Lee Jeans,
    # Venmo and Condado Tacos: a recruiter typing an email by hand does not emit List-Unsubscribe,
    # and every newsletter does. It replaces the required-keyword whitelist that used to sit here.
    if str(list_unsubscribe or "").strip():
        # ...unless Kevin already knows this person. A recruiter at a firm that routes ALL outbound
        # mail through a bulk platform (Mailchimp, HubSpot, an ATS marketing suite) emits
        # List-Unsubscribe on a hand-written note, and dropping that is exactly the silent loss
        # this system exists to prevent - it never alerts and never reaches the Spam sweep, which
        # also refuses bulk.
        #
        # The whitelist is the discriminator, not the wording: an exact CRM address match means
        # this is someone Kevin is actually corresponding with. Lee Jeans and Venmo are not in the
        # CRM, so nothing else gets in. Checked here rather than earlier because it costs a lookup,
        # and only bulk-flagged mail needs to pay it.
        if is_verified_crm_contact(sender):
            logging.info(f"[BULK OVERRIDE] {sender} sets List-Unsubscribe but is a known CRM "
                         f"contact - treated as a real reply")
        else:
            return False, "bulk mail (List-Unsubscribe header present)"

    # 5b. Required keywords, off unless EMAIL_REQUIRED_KEYWORDS is explicitly set in Render. Kept
    # so the old behaviour is one env var away, not a redeploy away. See the constant's comment.
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
            # The marker is parsed back by pipeline_utils.carmen_reply_anchor() to restart the
            # Carmen ladder and to tell a responder from a ghost - keep it in the shared constant.
            f"[{today_str}] {INBOUND_REPLY_NOTE_MARKER} (they wrote to Kevin, not a send). "
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

def record_inbound_thread(thread_id, sender_email, sender_name, company, subject, snippet,
                          status_label, match_reason, sheet_uuid, is_tier1):
    """Upsert one conversation into the inbound tray. Returns (is_new_thread, message_count).

    The upsert is what makes a thread the unit instead of a message: a second reply on a thread
    already in the tray bumps last_seen and message_count rather than creating a row, which is how
    one conversation stops costing five notifications.

    Deliberately records EVERY thread that clears the filters, verified or not. The CRM path can
    only hold a sender who resolves to a sheet row, so a recruiter's first email - a stranger by
    definition - had nowhere to live and fell out of the system after its single alert. Here it
    persists with sheet_uuid blank, and stays visible until Kevin marks it done.

    Never raises: the tray is an enhancement to the alert path, and a DB error must not cost an
    alert. On failure it returns (True, 1), which makes the caller treat it as a fresh thread -
    the behaviour the system had before the tray existed.
    """
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT message_count FROM inbound_threads WHERE thread_id = ?", (thread_id,))
            row = cursor.fetchone()
            if row:
                new_count = int(row[0] or 1) + 1
                # A new message on a settled thread reopens it: the conversation moved again, and
                # a thread marked done last week is not done when they write back.
                conn.execute(
                    "UPDATE inbound_threads SET last_seen = CURRENT_TIMESTAMP, message_count = ?, "
                    "subject = ?, snippet = ?, status_label = ?, is_tier1 = ?, state = 'open' "
                    "WHERE thread_id = ?",
                    (new_count, subject, snippet, status_label, 1 if is_tier1 else 0, thread_id))
                conn.commit()
                return False, new_count
            conn.execute(
                "INSERT INTO inbound_threads (thread_id, sender_email, sender_name, company, subject, "
                "snippet, status_label, match_reason, sheet_uuid, is_tier1) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (thread_id, sender_email, sender_name, company, subject, snippet,
                 status_label, match_reason, sheet_uuid, 1 if is_tier1 else 0))
            conn.commit()
            return True, 1
    except Exception as e:
        logging.error(f"Inbound tray write error ({thread_id}): {e}")
        return True, 1


def mark_inbound_thread_alerted(thread_id):
    """Record that Telegram accepted an alert for this thread. Never raises."""
    try:
        with get_db_conn() as conn:
            conn.execute("UPDATE inbound_threads SET alerted = 1 WHERE thread_id = ?", (thread_id,))
            conn.commit()
    except Exception as e:
        logging.error(f"Inbound tray alerted-flag error ({thread_id}): {e}")


def close_inbound_thread(thread_id):
    """Mark a conversation dealt with. Returns True if a row was actually updated."""
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE inbound_threads SET state = 'done' WHERE thread_id = ? AND state != 'done'",
                (thread_id,))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        logging.error(f"Inbound tray close error ({thread_id}): {e}")
        return False


def get_open_inbound_threads(limit=25):
    """Open conversations, most recently active first. Returns a list of dicts; [] on error."""
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT thread_id, sender_email, sender_name, company, subject, status_label, "
                "match_reason, message_count, last_seen, is_tier1 FROM inbound_threads "
                "WHERE state = 'open' ORDER BY is_tier1 DESC, last_seen DESC LIMIT ?", (limit,))
            cols = ("thread_id", "sender_email", "sender_name", "company", "subject",
                    "status_label", "match_reason", "message_count", "last_seen", "is_tier1")
            return [dict(zip(cols, r)) for r in cursor.fetchall()]
    except Exception as e:
        logging.error(f"Inbound tray read error: {e}")
        return []


def format_inbound_tray_message(threads):
    """Render the tray for Telegram. Mirrors the Needs You Today card's shape so /inbox reads like
    the rest of the system rather than like a database dump."""
    if not threads:
        return "📭 <b>Inbox Tray</b>\n\n<i>Nothing open - every conversation is dealt with.</i>"
    lines = [f"📬 <b>Inbox Tray</b> - {len(threads)} open\n"]
    for t in threads:
        badge = {"OFFER_EXTENDED": "🏆", "INTERVIEW_SET": "🎉", "REJECTION": "⚠️"}.get(
            t.get("status_label"), "🟢")
        who = html.escape(str(t.get("sender_name") or t.get("sender_email") or "Unknown"))
        company = html.escape(str(t.get("company") or "Unknown"))
        subject = html.escape(str(t.get("subject") or "(No Subject)")[:70])
        count = int(t.get("message_count") or 1)
        count_str = f" · {count} msgs" if count > 1 else ""
        # The thread_id is the handle /done takes, shown the way every other card shows an ID.
        lines.append(
            f"{badge} <b>{who}</b> @ {company}{count_str}\n"
            f"    <i>{subject}</i>\n"
            f"    🆔 <code>{html.escape(str(t.get('thread_id')))}</code>")
    lines.append("\n<i>Mark one dealt with: /done &lt;id&gt;</i>")
    return "\n".join(lines)


POLLER_FAILURE_ALERT_COOLDOWN_HOURS = 6


def report_poller_failure(stage, detail):
    """Tell Kevin in Telegram when the poller itself breaks, not just the log file.

    Every failure path here used to log and continue. That is correct for one bad message, but the
    failures that matter are total: a dead GMAIL_REFRESH_TOKEN, a revoked scope, a Gmail outage.
    In those cases notifications simply stop, and silence is indistinguishable from a quiet inbox -
    the failure mode is finding out from a missed interview days later.

    Rate-limited per stage so a persistent outage costs one message every six hours rather than one
    per cycle. Uses the DB-backed should_send_alert() rather than a module-level dict: Render
    restarts the container on every deploy, and an in-memory cooldown would reset with it and
    re-alert on each boot. Never raises: a broken alerter must not also break the poll.
    """
    try:
        if not TELEGRAM_CHAT_ID:
            return
        if not should_send_alert(f"poller_failure:{stage}", POLLER_FAILURE_ALERT_COOLDOWN_HOURS):
            return
        send_telegram_message(TELEGRAM_CHAT_ID, (
            "🛑 <b>Email poller failure</b>\n\n"
            f"<b>Stage:</b> {html.escape(str(stage))}\n"
            f"<b>Detail:</b> {html.escape(str(detail))}\n\n"
            "<i>Inbound alerts may be stopped. Check Gmail credentials on Render, "
            "then run /poll to retry.</i>"
        ))
    except Exception as e:
        logging.error(f"Poller failure alert could not be sent: {e}")


def check_inbound_gmail_replies():
    """Poll Gmail for unread inbound replies and alert on the ones a human sent.

    Three tiers, in the order they are decided:

      Tier 1 - a calendar invite or an interview signal. Alerts ALWAYS. It clears the bulk rules,
               the age gate and the CRM whitelist, and respects only the sender blacklist and the
               blocked/allowed domain lists. Kevin's rule: tell me when something looks like an
               interview invite, regardless of whether I know the sender. Two real interviews were
               lost to a filter that outranked this signal, and no filter outranks it now.
      Tier 2 - ordinary human mail that cleared the pre-filter. Alerts, and writes to the CRM only
               when the sender resolves to an actual row.
      Dropped - bulk mail (List-Unsubscribe), blacklisted senders, blocked domains, stale backlog.

    The old behaviour - silently dropping everything without an exact CRM match - is gone. It was
    built as a spam defence, but the poll query is label:INBOX and Gmail files spam elsewhere, so
    what it actually dropped was strangers: which is what a recruiter reaching out for the first
    time is. An unresolved sender never produces a CRM write; it produces an alert that says so.

    Finishes by calling sweep_spam_for_interview_signals(), a second narrow query over label:SPAM
    that applies the Tier 1 test and nothing else - see that function for why it is separate.
    """
    missing_vars = [v for v in ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_USER"] if not os.environ.get(v)]
    if missing_vars or not TELEGRAM_CHAT_ID:
        return
    access_token = get_gmail_access_token()
    if not access_token:
        # The single most dangerous failure: an expired or revoked refresh token stops every
        # inbound alert indefinitely, and nothing downstream ever runs to notice.
        report_poller_failure("Gmail auth", "could not obtain an access token")
        return
    headers = {"Authorization": f"Bearer {access_token}"}
    # A failure here logs and falls through to the Spam sweep rather than returning. The two
    # queries are independent, and the sweep is the safety net for the mail most likely to be lost
    # - letting an INBOX list error suppress it would take the net down exactly when it matters.
    message_ids = []
    try:
        list_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
        query = f"is:unread -from:me label:{EMAIL_LABEL_TARGET_INBOX} {EMAIL_QUERY_EXCLUSIONS}".strip()
        params = {"q": query, "maxResults": EMAIL_POLL_MAX_RESULTS}
        res = requests.get(list_url, headers=headers, params=params, timeout=10)
        if res.status_code != 200:
            logging.error(f"Gmail Poll List Error: {res.status_code}")
            report_poller_failure("INBOX list", f"HTTP {res.status_code}")
        else:
            # Sliced defensively: maxResults is a request, and a cycle that alerted on hundreds of
            # messages because a server ignored it would be worse than one that ran short.
            message_ids = [m["id"] for m in res.json().get("messages", [])][:EMAIL_POLL_MAX_RESULTS]
            logging.info(f"[POLL] Gmail list query returned {len(message_ids)} unread message(s) in label:{EMAIL_LABEL_TARGET_INBOX}")
    except Exception as e:
        logging.error(f"Gmail Poll List Exception: {e}")
        report_poller_failure("INBOX list", str(e))

    for msg_id in message_ids:
        try:
            detail_url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}"
            # format=full, not metadata. A calendar invite is only visible in payload.parts, which
            # metadata does not return, and the invite is the strongest interview signal there is.
            # The cost: messages.get is 5 quota units at either format, so the daily quota is
            # unchanged; what grows is the response body, from roughly 1KB of headers to the whole
            # message - tens of KB. At maxResults=10 per poll and one poll per EMAIL_POLL_HOURS
            # that is a few hundred KB a day and a little more latency per message, which buys the
            # one signal the text filters cannot see.
            # metadataHeaders is ignored by format=full (it returns every header), but is kept
            # accurate so that flipping back to metadata does not silently lose List-Unsubscribe.
            detail_params = {
                "format": "full",
                "metadataHeaders": ["From", "Subject", "In-Reply-To", "References", "List-Unsubscribe"],
            }
            detail_res = requests.get(detail_url, headers=headers, params=detail_params, timeout=10)
            if detail_res.status_code != 200:
                continue
            detail = detail_res.json()
            payload = detail.get("payload", {}) or {}
            header_list = payload.get("headers", [])
            sender = _gmail_header_value(header_list, "From", "Unknown Sender")
            subject = _gmail_header_value(header_list, "Subject", "(No Subject)")
            in_reply_to = _gmail_header_value(header_list, "In-Reply-To")
            references = _gmail_header_value(header_list, "References")
            list_unsubscribe = _gmail_header_value(header_list, "List-Unsubscribe")
            snippet = detail.get("snippet", "")
            internal_date_ms = detail.get("internalDate")
            thread_id = detail.get("threadId", msg_id)
            modify_url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}/modify"

            # HARD AGE CEILING. Placed above Tier 1 on purpose: this is the ONE gate Tier 1 may
            # not outrank. Everything that alerts is already stored in Telegram, so a message
            # older than the ceiling is either already on Kevin's phone or was deliberately
            # skipped - re-sending it is noise, never recovery.
            #
            # This is a different thing from EMAIL_MAX_AGE_SECONDS inside passes_email_prefilter.
            # That one is a stale-BACKLOG guard with a 96h floor, and it lives behind the Tier 1
            # bypass, so an interview-shaped message from last month still alerted. This ceiling
            # is unconditional.
            #
            # THE TRADE, stated plainly because it reverses an earlier decision: this DOES drop the
            # weekend-reply case that default_email_max_age_seconds()'s 96h floor was added to
            # protect (a Friday reply, a Render spin-down, 62h old by Monday). That floor still
            # governs the prefilter; this ceiling sits above it and wins.
            #
            # Kevin's call, made with the loss understood: every alert now carries the message's
            # own date, so anything that does arrive is findable, and re-notifying about old mail
            # is noise he will not read. A notification does not need to come through twice.
            # Revert by setting INBOUND_ALERT_MAX_AGE_HOURS higher - 96 restores the old behavior.
            if internal_date_ms is not None:
                try:
                    inbound_age_s = time.time() - (int(internal_date_ms) / 1000.0)
                    # +60s grace so the boundary is inclusive: a message that is exactly 24h old
                    # is inside a 24h window, and without this the seconds spent fetching it push
                    # it over. The grace is far smaller than the poll interval, so it cannot let
                    # a second day's mail through.
                    if inbound_age_s > INBOUND_ALERT_MAX_AGE_SECONDS + 60:
                        logging.info(
                            f"[AGE CEILING] Skipping {sender} - message is "
                            f"{inbound_age_s / 3600:.1f}h old (> {INBOUND_ALERT_MAX_AGE_SECONDS / 3600:.0f}h)"
                        )
                        # Marked read so the next poll does not re-examine it forever. Safe
                        # because nothing was alerted and nothing was written.
                        try:
                            requests.post(modify_url, headers=headers,
                                          json={"removeLabelIds": ["UNREAD"]}, timeout=10)
                        except Exception:
                            pass
                        continue
                except (TypeError, ValueError):
                    pass

            # TIER 1 detection runs BEFORE any gate, because the whole point is that no gate may
            # outrank it. classify_inbound_ats_email checks rejection patterns first, so a decline
            # that mentions interviewing cannot buy itself a bypass.
            # Classify on the real body, not on Gmail's ~200-char snippet. The alert still SHOWS
            # the snippet - the short preview is deliberate and Kevin likes it - but what the
            # classifier reads is up to CLASSIFIER_BODY_CHARS of the actual message, so an ask that
            # sits in paragraph three is no longer invisible. Falls back to the snippet when there
            # is no decodable body.
            body_text = extract_plain_body(payload) or snippet
            status_label, _crm_action = classify_inbound_ats_email(sender, subject, body_text)
            has_calendar_invite, invite_start = extract_calendar_invite(payload)
            is_tier1 = has_calendar_invite or status_label in ("INTERVIEW_SET", "OFFER_EXTENDED")

            # A bulk sender cannot buy a Tier 1 bypass. "Application status update - YOUR INTERVIEW
            # REQUEST AWAITING YOUR CONFIRMATION" is a real job-board blast from Kevin's inbox, and
            # it matches \binterview\b, so it used to clear every gate the bypass skips. The header
            # is the same structural bulk test gate 5 applies, and a recruiter typing by hand never
            # sets it - so honouring it here costs no real interview while closing the hole that
            # job-board volume would otherwise drive straight through.
            if is_tier1 and str(list_unsubscribe or "").strip():
                is_tier1 = False
                logging.info(
                    f"[TIER1 DENIED] Interview-shaped bulk mail from {sender} "
                    f"(List-Unsubscribe present) - demoted to the normal pre-filter")

            # ...and neither can an automated sender. List-Unsubscribe only covers marketing mail:
            # TRANSACTIONAL blasts (account setup, security notices, terms updates) are not
            # obliged to set it and frequently do not, so they cleared the gate above and arrived
            # as "Interview Signal Detected". Real examples off Kevin's phone: Experian "your
            # account is set up!", Azure "Set up advanced security", TurboTax "Schedule a call".
            #
            # The check is on the SENDER, not the wording, which is why it closes the whole class
            # rather than one phrase at a time. careers@/recruiting@/talent@ are excluded from
            # is_automated_sender() precisely so a real invitation keeps its bypass.
            if is_tier1 and is_automated_sender(sender):
                is_tier1 = False
                logging.info(
                    f"[TIER1 DENIED] Interview-shaped automated mail from {sender} "
                    f"- demoted to the normal pre-filter")

            # GATE 1: Pre-filter shield. Tier 1 skips it, except for the sender rules - a robot
            # mailbox blasting calendar spam is still a robot, and a blocked domain stays blocked.
            if is_tier1:
                passed, reject_reason = passes_email_sender_blocks(sender)
                if passed:
                    logging.info(
                        f"[TIER1] Interview signal from {sender} "
                        f"(calendar_invite={has_calendar_invite}, classifier={status_label}) - pre-filter bypassed"
                    )
            else:
                # body_text, not snippet: the length gate should judge what the person actually
                # wrote, and a short snippet on a long message was never a reason to drop it.
                passed, reject_reason = passes_email_prefilter(
                    sender, subject, body_text, internal_date_ms, in_reply_to, references, list_unsubscribe
                )
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
                    "name": display_name_from_sender(sender),
                    "company": "Unknown",
                    "tab": "Thread participant",
                    "sheet_uuid": "",
                }
                is_unverified = True
                match_reason = "thread participant"
            elif is_unverified:
                match_reason = "domain match"
            if not crm_match:
                # No CRM identity at all. This used to be a silent drop, and it is how a recruiter
                # Kevin had never emailed - confirming a real interview, DKIM-signed, marked
                # Important by Gmail - was thrown away without a trace. A stranger writing to you
                # is not a defect; it is the outcome the outreach exists to produce. So it alerts,
                # and it alerts with an empty sheet_uuid so every CRM branch below skips itself:
                # guessing which row a stranger belongs to would be worse than the original bug.
                is_unverified = True
                match_reason = "interview signal" if is_tier1 else "unknown sender"
                crm_match = {
                    "name": display_name_from_sender(sender),
                    "company": "Unknown",
                    "tab": "Not in CRM",
                    "sheet_uuid": "",
                }

            logging.info(f"[ALLOWED] {'Unverified (' + match_reason + ')' if is_unverified else 'Verified CRM'} sender {sender} matched to {crm_match.get('company')} ({crm_match.get('tab')})")

            thread_link = html.escape(f"https://mail.google.com/mail/u/0/#inbox/{thread_id}", quote=True)
            match_name = html.escape(str(crm_match.get("name") or "Unknown"))
            match_company = html.escape(str(crm_match.get("company") or "Unknown"))
            match_tab = html.escape(str(crm_match.get("tab") or "Unknown"))
            crm_line = f"<b>CRM Match:</b> {match_name} @ {match_company} <i>({match_tab})</i>\n"

            # status_label was already computed above for the Tier 1 decision - reusing it keeps
            # the badge and the bypass from ever disagreeing about the same message.
            status_badges = {
                "OFFER_EXTENDED": "🏆 <b>OFFER / ONBOARDING — act on this first</b>\n",
                "INTERVIEW_SET": "🎉 <b>Interview Signal Detected!</b>\n",
                "REJECTION": "⚠️ <b>Rejection Detected</b>\n"
            }
            status_line = status_badges.get(status_label, "")
            # The time is shown only when DTSTART actually parsed. extract_calendar_invite returns
            # None rather than a guess, and an invented time is the one error Kevin would act on.
            invite_line = ""
            if has_calendar_invite:
                invite_line = (
                    f"📅 <b>Calendar invite:</b> {html.escape(invite_start)}\n" if invite_start
                    else "📅 <b>Calendar invite attached</b> <i>(start time not parsed)</i>\n"
                )
            # Outcome metrics and CRM routing are skipped for every unresolved sender - domain
            # match, thread participant or outright stranger. There is no sheet_uuid to attach
            # them to, and a guess about WHO replied must never move a stage or book an interview
            # against the wrong row. Kevin gets the alert and decides.
            if is_unverified:
                logging.info(f"[UNVERIFIED] Skipping CRM writes for {match_reason} sender {sender}")
            elif status_label == "OFFER_EXTENDED":
                # "offer" is already in record_application_outcome's vocabulary (see its docstring)
                # and is what /offer writes by hand, so an inbound offer lands in the same column
                # the funnel's Interviewing->Offer rate already reads.
                record_application_outcome(crm_match.get("sheet_uuid"), "offer", company=crm_match.get("company"))
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

            # A Tier 1 stranger gets its own header: "Unverified Reply" reads like something to
            # deal with later, which is the wrong instruction for an interview invitation.
            if not is_unverified:
                header_line = "📬 <b>New Gmail Reply!</b>"
            elif match_reason == "interview signal":
                header_line = (
                    "🚨 <b>Possible OFFER - Unknown Sender</b>" if status_label == "OFFER_EXTENDED"
                    else "🚨 <b>Possible Interview - Unknown Sender</b>"
                )
            else:
                header_line = f"⚠️ <b>Unverified Reply ({html.escape(match_reason)})</b>"

            # Day stamp on every alert. Telegram stores these indefinitely, so the alert IS the
            # archive - and an archive with no date is hard to search months later. The date is the
            # MESSAGE's own (internalDate), not now(): a message that sat unread for 20 hours
            # should be findable under the day it was sent, not the day the poller happened to see
            # it. Falls back to today only when internalDate is missing or unparseable.
            alert_day = datetime.now()
            if internal_date_ms is not None:
                try:
                    alert_day = datetime.fromtimestamp(int(internal_date_ms) / 1000.0)
                except (TypeError, ValueError, OSError):
                    pass
            header_line = f"{header_line}\n🗓 <i>{alert_day.strftime('%a %b %d, %Y · %I:%M %p').replace(' 0', ' ')}</i>"
            unverified_notes = {
                "domain match": "<i>Not a CRM contact - matched by company domain. No CRM changes were made.</i>\n",
                "thread participant": "<i>New person in a thread you started - possibly an introduction. No CRM changes were made.</i>\n",
                "interview signal": "<i>Not a known contact - surfaced because it looks like an interview. No CRM changes were made.</i>\n",
                "unknown sender": "<i>Not a known contact - no CRM changes were made.</i>\n",
            }
            unverified_note = "" if not is_unverified else unverified_notes.get(match_reason, "")
            # The tray is written BEFORE the alert, so a conversation is durably recorded even if
            # Telegram never accepts the message. This is what makes a failed send recoverable:
            # the row is open, and /inbox and the daily card will both still show it.
            sender_address = (re.search(r"[\w\.-]+@[\w\.-]+\.\w+", sender or "") or [None])
            sender_address = sender_address.group(0).lower() if hasattr(sender_address, "group") else ""
            is_new_thread, thread_msg_count = record_inbound_thread(
                thread_id, sender_address, display_name_from_sender(sender),
                str(crm_match.get("company") or "Unknown"), subject, snippet,
                status_label, match_reason, str(crm_match.get("sheet_uuid") or ""), is_tier1)

            # Thread-level dedup. A follow-up message on a conversation already in the tray does
            # not earn its own notification - it updates the row, and the tray shows the new count.
            # Tier 1 is exempt: an interview or offer landing on an existing thread is exactly the
            # development worth interrupting for, whatever came before it.
            if not is_new_thread and not is_tier1:
                logging.info(
                    f"[TRAY] Thread {thread_id} already open ({thread_msg_count} msgs) - "
                    f"updated without a duplicate alert")
                requests.post(modify_url, headers=headers, json={"removeLabelIds": ["UNREAD"]}, timeout=10)
                continue

            # A REJECTION archives the job row it refers to. Runs for UNVERIFIED senders too, which
            # is the whole point: an ATS decline arrives from trinityhealth@myworkday.com, whose
            # domain resolves to no company at all, so it can never be a CRM contact and every
            # CRM branch above skips it. The company is read from the message instead, and the
            # ROW is matched on that - one live row is archived, several raise a pick card.
            rejection_line = ""
            if status_label == "REJECTION":
                try:
                    reject_company = (
                        str(crm_match.get("company") or "").strip()
                        if str(crm_match.get("company") or "").strip() not in ("", "Unknown")
                        else extract_company_from_rejection(subject, body_text)
                    )
                    rejection_line = route_rejection_to_died(reject_company, subject, snippet)
                except Exception as e:
                    logging.error(f"[REJECTION ROUTING] Failed for {sender}: {e}")

            alert_msg = (
                f"{header_line}\n\n"
                f"{status_line}"
                f"{invite_line}"
                f"{unverified_note}"
                f"<b>From:</b> {html.escape(sender)}\n"
                f"{crm_line}"
                f"<b>Subject:</b> {html.escape(subject)}\n"
                f"<b>Preview:</b> <i>{html.escape(snippet)}</i>\n"
                f"{rejection_line}\n"
                f"<a href='{thread_link}'>Open Thread in Gmail</a>"
            )
            # Telegram hard-rejects over 4096 chars. Everything above the preview is what makes the
            # alert actionable, so an oversized snippet is split into its own follow-up message
            # rather than truncating the alert and losing the Gmail link off the end.
            if len(alert_msg) > TELEGRAM_MAX_MESSAGE_CHARS:
                # The header message is the one that carries the link and the verdict, so IT is
                # what delivery is judged on. A dropped preview is a cosmetic loss; a dropped
                # header is the whole alert.
                delivered = send_telegram_message(TELEGRAM_CHAT_ID, (
                    f"{header_line}\n\n{status_line}{invite_line}{unverified_note}"
                    f"<b>From:</b> {html.escape(sender)}\n{crm_line}"
                    f"<b>Subject:</b> {html.escape(subject)}\n\n"
                    f"<a href='{thread_link}'>Open Thread in Gmail</a>"
                ))
                preview = f"<b>Preview:</b> <i>{html.escape(snippet)}</i>"
                send_telegram_message(TELEGRAM_CHAT_ID, preview[:TELEGRAM_MAX_MESSAGE_CHARS])
            else:
                delivered = send_telegram_message(TELEGRAM_CHAT_ID, alert_msg)

            # Mark read ONLY on confirmed delivery. send_telegram_message returns the message_id on
            # success and None on failure - it never raises - so the old unconditional mark-read
            # turned every Telegram failure into permanent silent loss: the alert was never seen,
            # the message was no longer unread, and the next poll's is:unread query could never
            # find it again. A 5s timeout or a second 429 was enough to lose an interview.
            #
            # Leaving it UNREAD is the entire retry mechanism: the next cycle re-lists it and tries
            # again. Duplicate alerts are possible if Telegram delivered but the response was lost,
            # and that is the correct trade - a duplicate interview alert costs a glance, a dropped
            # one costs the interview.
            if delivered:
                mark_inbound_thread_alerted(thread_id)
                requests.post(modify_url, headers=headers, json={"removeLabelIds": ["UNREAD"]}, timeout=10)
            else:
                logging.error(
                    f"[DELIVERY FAILED] Alert for {sender} was not delivered - leaving UNREAD to "
                    f"retry on the next cycle (msg_id={msg_id})")
                report_poller_failure("Telegram delivery", f"alert for {sender} not delivered")
        except Exception as e:
            logging.error(f"Gmail Poll Message Processing Error ({msg_id}): {e}")

    # Second, narrower query. Runs after the INBOX pass and shares its access token.
    sweep_spam_for_interview_signals(headers)


SPAM_SWEEP_MAX_RESULTS = 10


def sweep_spam_for_interview_signals(request_headers):
    """Surface Tier 1 interview signals that Gmail filed as spam. Nothing else from Spam is ever
    surfaced, and nothing from Spam ever touches the CRM.

    This exists because Gmail's spam classifier is wrong in a specific, costly direction: a
    calendar invitation from a company Kevin has never corresponded with, sent by a system he has
    never replied to, looks exactly like bulk mail. His recruiter warned outright that the Raymond
    James invite might land there. A false positive in Spam is unrecoverable in practice - nobody
    reads that folder - so the one signal worth paying attention for is checked there too.

    Deliberately a separate function rather than a second label in the main loop, because the
    guarantee should be structural and not a matter of reading the branches correctly: this
    function calls no CRM lookup, no CRM write, no outcome recorder and no metric event. It cannot
    corrupt a row, because it has no code path that reaches one. The only gates it applies are the
    Tier 1 test and passes_email_sender_blocks - a no-reply@ robot's calendar spam is still spam,
    and a blocked domain stays blocked whichever folder it lands in.

    Known limit: Gmail returns newest first and this reads at most SPAM_SWEEP_MAX_RESULTS, so an
    invite sitting behind more than ten newer unread spam messages is not seen. A fresh invite is
    at the top by construction, and raising the cap trades that edge for a slower cycle on a
    folder that is mostly junk by definition.
    """
    try:
        list_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
        params = {"q": "is:unread -from:me label:SPAM", "maxResults": SPAM_SWEEP_MAX_RESULTS}
        res = requests.get(list_url, headers=request_headers, params=params, timeout=10)
        if res.status_code != 200:
            logging.error(f"Gmail Spam Sweep List Error: {res.status_code}")
            return
        spam_ids = [msg["id"] for msg in res.json().get("messages", [])][:SPAM_SWEEP_MAX_RESULTS]
        logging.info(f"[SPAM SWEEP] {len(spam_ids)} unread message(s) in label:SPAM to test for interview signals")
    except Exception as e:
        logging.error(f"Gmail Spam Sweep List Exception: {e}")
        return

    for msg_id in spam_ids:
        try:
            detail_url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}"
            detail_res = requests.get(
                detail_url, headers=request_headers,
                params={"format": "full",
                        "metadataHeaders": ["From", "Subject", "List-Unsubscribe"]}, timeout=10)
            if detail_res.status_code != 200:
                continue
            detail = detail_res.json()
            payload = detail.get("payload", {}) or {}
            header_list = payload.get("headers", [])
            sender = _gmail_header_value(header_list, "From", "Unknown Sender")
            subject = _gmail_header_value(header_list, "Subject", "(No Subject)")
            list_unsubscribe = _gmail_header_value(header_list, "List-Unsubscribe")
            snippet = detail.get("snippet", "")
            thread_id = detail.get("threadId", msg_id)

            # Bulk mail gets no rescue from Spam. This sweep's whole justification is that Gmail is
            # wrong in one costly direction - a real invite from an unknown company looking like
            # bulk - and a message that SETS List-Unsubscribe is the case where Gmail was right.
            # Without this, every "YOUR INTERVIEW REQUEST" blast Gmail correctly caught gets
            # resurrected into Telegram, which is the opposite of what the sweep is for.
            if str(list_unsubscribe or "").strip():
                continue

            # Same hard age ceiling as the inbox path, and for the same reason: a month-old
            # interview-shaped message in Spam is not a rescue, it is noise. Left untouched
            # (not marked read) - Gmail owns this folder and the sweep has no opinion on old mail.
            spam_internal_date = detail.get("internalDate")
            if spam_internal_date is not None:
                try:
                    if (time.time() - int(spam_internal_date) / 1000.0) > INBOUND_ALERT_MAX_AGE_SECONDS:
                        continue
                except (TypeError, ValueError):
                    pass

            # And an automated sender gets no rescue either - see is_automated_sender(). Transactional
            # blasts do not set List-Unsubscribe, so the gate above misses them entirely.
            if is_automated_sender(sender):
                continue

            status_label, _crm_action = classify_inbound_ats_email(
                sender, subject, extract_plain_body(payload) or snippet)
            has_calendar_invite, invite_start = extract_calendar_invite(payload)
            if not (has_calendar_invite or status_label in ("INTERVIEW_SET", "OFFER_EXTENDED")):
                # Left completely untouched - not alerted, not marked read. Gmail put it here and
                # this sweep has no opinion about anything that is not an interview signal.
                continue
            passed, reject_reason = passes_email_sender_blocks(sender)
            if not passed:
                logging.info(f"[SPAM SWEEP] Interview-shaped spam from {sender} still blocked - {reject_reason}")
                continue

            logging.info(
                f"[SPAM SWEEP] Tier 1 signal rescued from Spam: {sender} "
                f"(calendar_invite={has_calendar_invite}, classifier={status_label})"
            )
            invite_line = ""
            if has_calendar_invite:
                invite_line = (
                    f"📅 <b>Calendar invite:</b> {html.escape(invite_start)}\n" if invite_start
                    else "📅 <b>Calendar invite attached</b> <i>(start time not parsed)</i>\n"
                )
            thread_link = html.escape(f"https://mail.google.com/mail/u/0/#spam/{thread_id}", quote=True)
            is_offer = status_label == "OFFER_EXTENDED"
            alert_msg = (
                f"🚨 <b>Possible {'OFFER' if is_offer else 'Interview'} - Found in SPAM</b>\n\n"
                f"{invite_line}"
                f"<i>Gmail filed this as spam. Surfaced because it looks like "
                f"{'an offer' if is_offer else 'an interview'} - "
                "verify the sender before acting. No CRM changes were made.</i>\n"
                f"<b>From:</b> {html.escape(sender)} <i>({html.escape(display_name_from_sender(sender))})</i>\n"
                f"<b>Subject:</b> {html.escape(subject)}\n"
                f"<b>Preview:</b> <i>{html.escape(snippet)}</i>\n\n"
                f"<a href='{thread_link}'>Open in Gmail Spam</a>"
            )
            if len(alert_msg) > TELEGRAM_MAX_MESSAGE_CHARS:
                delivered = send_telegram_message(TELEGRAM_CHAT_ID, (
                    "🚨 <b>Possible Interview - Found in SPAM</b>\n\n"
                    f"{invite_line}"
                    "<i>Gmail filed this as spam. Verify the sender before acting. "
                    "No CRM changes were made.</i>\n"
                    f"<b>From:</b> {html.escape(sender)}\n"
                    f"<b>Subject:</b> {html.escape(subject)}\n\n"
                    f"<a href='{thread_link}'>Open in Gmail Spam</a>"
                ))
                send_telegram_message(
                    TELEGRAM_CHAT_ID,
                    f"<b>Preview:</b> <i>{html.escape(snippet)}</i>"[:TELEGRAM_MAX_MESSAGE_CHARS])
            else:
                delivered = send_telegram_message(TELEGRAM_CHAT_ID, alert_msg)

            # Marked read, and ONLY marked read - the message stays in Spam. Removing UNREAD is
            # what stops the same invite alerting on every cycle; moving it out of Spam would be
            # this code overruling Gmail's classification on the strength of a regex, which is a
            # judgement that belongs to Kevin after he has looked at it.
            #
            # Conditional on delivery for the same reason the INBOX loop is, and it matters more
            # here: nobody browses the Spam folder, so an undelivered alert that was marked read is
            # the one case where the message is unreachable by every path at once.
            if delivered:
                requests.post(
                    f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}/modify",
                    headers=request_headers, json={"removeLabelIds": ["UNREAD"]}, timeout=10)
            else:
                logging.error(
                    f"[SPAM SWEEP] Delivery failed for {sender} - leaving UNREAD to retry")
        except Exception as e:
            logging.error(f"Gmail Spam Sweep Processing Error ({msg_id}): {e}")

# Dedicated scheduler instance: Gmail polling runs strictly once every 15 minutes,
# decoupled from Telegram webhook traffic (never triggered by incoming webhook pings).
# Render containers run UTC; pin the zone so cron jobs fire on Michigan time.
EMAIL_POLL_SCHEDULER = BackgroundScheduler(daemon=True, timezone="America/Detroit")

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


# Every PEOPLE-schema tab a contact can already live in. One list, because "is this person already
# logged?" is asked in several places and a tab missing from any one of them silently duplicates a
# real contact - adding Carmen Hot without updating the capture gate below would have written a
# freshly promoted contact back into Carmen Cold on the next sent-mail scan, restarting the ladder
# on someone already graduated. Mirrors the PEOPLE entries in Code.gs's TAB_MAP.
PEOPLE_TABS = ("Carmen Cold", "Carmen Hot", "Carmen Warm", "Killed")

# The subset that means "Kevin knows this person", used to pick warm copy over cold. The bench
# (Carmen Warm) counts: those are real contacts he has met, so a cold-stranger template would be
# wrong even before they re-enter the ladder. Only Killed is excluded - a contact archived after
# three unanswered nudges has no established relationship to write to.
WARM_TONE_TABS = ("Carmen Cold", "Carmen Hot", "Carmen Warm")

def is_logged_person_contact(email):
    """True when this address is already a row in a PEOPLE tab (see PEOPLE_TABS).

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
            placeholders = ",".join("?" for _ in PEOPLE_TABS)
            cursor.execute(
                f"SELECT 1 FROM sheet_row_map WHERE LOWER(contact_email) = ? "
                f"AND sheet_tab IN ({placeholders}) LIMIT 1",
                (clean, *PEOPLE_TABS)
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
                # A near-miss is worth a log line: a real person at a company the matcher did not
                # recognize is exactly the ford.com / ncms.org class of silent drop, and without
                # this the only symptom is a contact that never appears in Carmen Cold. Role
                # mailboxes and consumer domains are deliberate skips and stay quiet.
                _name, _addr = parse_email_recipient(to_header)
                if _addr and not is_role_mailbox(_addr) and company_domain_of(_addr):
                    logging.info(
                        f"[SENT] no CRM company matched {_addr} (domain "
                        f"{company_domain_of(_addr)}) - not captured"
                    )
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
    # Runs LAST, and deliberately after capture_contacts_from_sent_mail(): that step files the
    # newly-emailed person into Carmen Cold, and this one reads Carmen Cold. Same cycle, so a
    # person emailed today lands on their job row today rather than waiting a full extra poll.
    #
    # This does NOT duplicate backfill_contact_emails_from_sent_mail() above. That one reads
    # Gmail Sent directly and only within SENT_CAPTURE_LOOKBACK_HOURS (72h), so a contact emailed
    # last week is permanently out of its reach; it also treats any non-role address as real, so
    # a job row carrying Kevin's own kjmiller406@gmail.com is invisible to it. This one reads
    # Carmen Cold, which has no expiry, and rejects consumer addresses - which is what actually
    # repairs the Affirm-style row. Together they cover recent mail and the standing contact list.
    try:
        auto_fill_job_contacts_from_carmen_cold()
    except Exception as e:
        logging.error(f"[POLL] Carmen Cold contact fill Error: {e}")
    logging.info("[POLL] Email poll cycle completed")

# EMAIL_POLL_HOURS and EMAIL_POLL_ENABLED are defined with the other EMAIL_* constants near the
# top of the file, not here: EMAIL_MAX_AGE_SECONDS derives its default from the cadence, and a
# module-level constant cannot read one defined 4,500 lines further down.

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
    """Register the weekly SQLite backup on the existing background scheduler (Sunday 3 AM local).

    "Local" means America/Detroit, pinned on EMAIL_POLL_SCHEDULER. Render runs UTC, so before
    the pin this fired at 03:00 UTC.
    """
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
    # Job links that died since the last digest. Reported once each (notified flag), so a posting
    # that stays dead does not repeat every morning.
    fresh_dead = get_dead_job_links(include_notified=False, limit=10)
    if fresh_dead:
        digest += f"\n\n🔗 <b>Job links gone dead ({len(fresh_dead)}):</b>"
        for uuid_v, company, role, _link, status, _reason, retired, _first in fresh_dead:
            tag = "⚰️ retired" if retired else f"⚠️ {html.escape(str(status or '?'))}"
            digest += f"\n• {html.escape(str(company or '?'))} - {html.escape(str(role or '?'))} ({tag})"
        digest += "\n<i>⚰️ auto-moved to Died. ⚠️ you applied, so it was left alone.</i> <code>/links</code>"
        mark_dead_links_notified([r[0] for r in fresh_dead])

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

class PermanentCRMRejection(Exception):
    """A CRM write Apps Script refused for a reason that cannot change on retry.

    Raised by log_to_sheets_crm so the outbox can tell "the sheet is briefly unreachable" (requeue
    and try again) apart from "this write is impossible" (drop it). Without the distinction a row
    whose sheet_uuid does not exist is re-dispatched every 5s until retry_count hits 10, firing a
    health alert every pass - the Telegram alert storm this class exists to prevent.
    """
    def __init__(self, action, message):
        self.action = action
        self.message = message
        super().__init__(f"CRM '{action}' permanently rejected: {message}")


# Per-row outcomes from the most recent batch_add_rows, keyed by the uuid that was SENT.
#
# log_to_sheets_crm() returns a single bool for a whole batch, and nine callers depend on that
# contract, so the per-row detail rides alongside it here instead of changing the return type.
# Thread-local because run_job_pipeline dispatches concurrently: a shared dict would let one
# thread's batch overwrite another's and re-point a card at an unrelated row.
_BATCH_DISPOSITIONS = threading.local()


def _stash_batch_dispositions(payload, dispositions):
    """Record Code.gs's per-row verdict for the batch just sent."""
    table = {}
    for d in (dispositions or []):
        if isinstance(d, dict) and d.get("sent_uuid"):
            table[str(d["sent_uuid"])] = {
                "status": str(d.get("status") or ""),
                "existing_uuid": str(d.get("existing_uuid") or ""),
            }
    _BATCH_DISPOSITIONS.table = table


def get_batch_disposition(sent_uuid):
    """The verdict for one sent uuid, or None when Apps Script reported nothing.

    None means an OLD Code.gs deployment that predates the dispositions field - callers must fall
    back to their previous behavior rather than treating silence as "suppressed".
    """
    table = getattr(_BATCH_DISPOSITIONS, "table", None) or {}
    return table.get(str(sent_uuid))


# Deterministic Apps Script rejections: a missing row, a malformed payload or an unroutable tab
# answers identically on every attempt. Matched as substrings against the response's `message`,
# which is the only failure detail doPost returns. "Lock timeout - server busy" is deliberately
# absent - it is the one rejection that is genuinely transient and must stay retryable.
PERMANENT_CRM_REJECTIONS = (
    "no record found",
    "invalid action type",
    "unknown target_code",
    "unsupported get request",
    "requires sheet_uuid",
)


def is_permanent_crm_rejection(message):
    """True when an Apps Script rejection message cannot resolve itself on a retry."""
    lowered = str(message or "").lower()
    return any(marker in lowered for marker in PERMANENT_CRM_REJECTIONS)


def crm_failure_alert_text(payload, attempts, reason=""):
    """One line identifying WHICH write failed and why.

    The old text was a bare attempt count, so several failing payloads produced several identical
    Telegram warnings that could not be told apart - and the Apps Script `message` explaining the
    failure only ever reached logging.error, which is not visible from the phone this alert lands on.
    """
    payload = payload or {}
    action = payload.get("action", "unknown")
    bits = [f"CRM write '{action}' failed after {attempts} attempt(s)."]
    uuid = payload.get("sheet_uuid")
    if uuid:
        bits.append(f"uuid={uuid}")
    tab = payload.get("tab") or payload.get("target_code")
    if tab:
        bits.append(f"tab={tab}")
    rows = payload.get("rows")
    if rows:
        bits.append(f"rows={len(rows)}")
    if reason:
        bits.append(f"Last error: {str(reason)[:200]}")
    return " ".join(bits)


def log_to_sheets_crm(payload, max_retries=3, raise_on_permanent=False, alert_on_exhaustion=True):
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

    The same is true of any deterministic rejection (see PERMANENT_CRM_REJECTIONS): a missing
    sheet_uuid or a malformed payload answers identically forever. Those stop retrying at once.
    Callers that only branch on success get the usual False; the outbox passes
    raise_on_permanent=True to get a PermanentCRMRejection instead, which is its signal to delete
    the queued row rather than requeue it for ten more alert-firing passes.

    alert_on_exhaustion is for callers that are themselves the retry mechanism. Exhausting the
    attempts here is only newsworthy when this call was the last word on the payload. The outbox
    passes max_retries=1 and re-dispatches every 5s until retry_count hits 10, so a failed attempt
    is an ordinary step in its backoff, not a delivery failure - alerting per attempt turned one
    stuck /warm write into four identical "Failed to log payload after 1 attempts" warnings inside
    two minutes, with no action, uuid or reason to tell them apart. The outbox alerts once, on its
    own terms, when the row is actually abandoned.
    """
    if not CRM_WEBHOOK_URL:
        return False
    # Ensure row operation order is DESC for backwards loop searches
    if "rowOperationOrder" not in payload:
        payload["rowOperationOrder"] = "DESC"
    action = payload.get("action", "unknown")
    expected_rows = len(payload.get("rows") or []) if action == "batch_add_rows" else None
    last_failure = ""
    delay = 1.0
    for attempt in range(max_retries):
        try:
            res = crm_post(payload)
            if not res:
                last_failure = "no response from the CRM webhook"
            elif res.status_code != 200:
                last_failure = f"HTTP {res.status_code}"
            if res and res.status_code == 200:
                try:
                    body = res.json()
                except Exception:
                    # A non-JSON 200 is the Apps Script HTML error/login page, not a written row.
                    logging.error(f"CRM '{action}': non-JSON 200 response: {res.text[:200]}")
                    last_failure = "non-JSON 200 (Apps Script error or login page)"
                    body = None

                if isinstance(body, dict):
                    status = str(body.get("status", "")).lower()
                    message = str(body.get("message", ""))
                    if status == "success":
                        if expected_rows is not None:
                            written = safe_int(body.get("count"), 0)
                            # [] when an older Code.gs deployment reports no per-row detail, which
                            # must never be read as "every row was a duplicate".
                            dispositions_reported = [
                                d for d in (body.get("dispositions") or []) if isinstance(d, dict)
                            ]
                            _stash_batch_dispositions(payload, dispositions_reported)
                            if written < expected_rows:
                                # A SHORT COUNT IS NOT A FAILED WRITE. Code.gs's in-append dedup
                                # guard (findLiveJobsDuplicate) skips a row whose normalized
                                # Company+Role already exists as a live row, and reports
                                # status:"success" with the smaller count. That is the guard doing
                                # its job, and the rows that were not duplicates DID reach the
                                # sheet.
                                #
                                # Returning False here told run_job_pipeline() the whole batch had
                                # failed, so it withheld EVERY card - on 2026-09-21 one duplicate
                                # out of five suppressed all five Tier-1 cards, including a
                                # 100-score role, and the alert claimed none of the rows were in
                                # the sheet when four of them were.
                                #
                                # Zero written is different: nothing landed, and the caller must
                                # still withhold rather than dispatch cards for rows that do not
                                # exist.
                                #
                                # UNLESS every row was dropped as a duplicate, which the
                                # dispositions now make visible. An all-duplicate batch is not a
                                # failure - each of those jobs IS tracked, on a live row whose uuid
                                # Code.gs just reported - so returning False here withheld cards for
                                # rows that exist, and did it on exactly the re-paste and rerun
                                # cases where a batch is most likely to be entirely duplicates.
                                # dispatch_tier1_matches re-points each card at its live row.
                                if written <= 0:
                                    all_dupes = bool(dispositions_reported) and all(
                                        d.get("status") == "duplicate_suppressed"
                                        for d in dispositions_reported
                                    )
                                    if not all_dupes:
                                        logging.error(
                                            f"CRM batch_add_rows wrote 0/{expected_rows} rows: {message}"
                                        )
                                        send_health_alert(
                                            f"CRM batch wrote NO rows of {expected_rows} sent. {message}"
                                        )
                                        return False
                                    logging.warning(
                                        f"CRM batch_add_rows wrote 0/{expected_rows} rows - every row "
                                        f"was already tracked on a live row; cards will be re-pointed "
                                        f"at the existing rows: {message}"
                                    )
                                logging.warning(
                                    f"CRM batch_add_rows wrote {written}/{expected_rows} rows; "
                                    f"{expected_rows - written} suppressed as duplicate(s) by the "
                                    f"Apps Script dedup guard: {message}"
                                )
                        return True

                    logging.error(f"CRM '{action}' rejected by Apps Script: {message}")
                    last_failure = message or "rejected with no message"
                    if "unauthorized" in message.lower():
                        send_health_alert(
                            "CRM webhook is rejecting every write as Unauthorized - rows are NOT "
                            "reaching the sheet. Set the CRM_SHARED_SECRET Script Property in the "
                            "Apps Script project to match Render's CRM_SHARED_SECRET, then redeploy "
                            "the web app (Deploy > Manage deployments > New version)."
                        )
                        return False
                    # A deterministic rejection will answer identically forever, so retrying it
                    # only burns the backoff and - via the outbox, which re-dispatches every 5s
                    # until retry_count hits 10 - fires a health alert on every pass. Raising
                    # PermanentCRMRejection instead of returning False lets the outbox delete the
                    # row rather than requeue it. "Lock timeout" is the one rejection Apps Script
                    # emits that IS transient (another execution held the script lock), so it
                    # alone falls through to the normal retry path.
                    if is_permanent_crm_rejection(message):
                        if raise_on_permanent:
                            raise PermanentCRMRejection(action, message)
                        # Default bool contract, for the callers that only branch on success:
                        # still stop retrying, since the answer cannot change.
                        return False
        except PermanentCRMRejection:
            raise  # never retried, and never swallowed by the transient handler below
        except Exception as e:
            logging.error(f"CRM Webhook Attempt {attempt+1} Failed: {e}")
            last_failure = f"{type(e).__name__}: {e}"
        time.sleep(delay)
        delay *= 2.0
    if alert_on_exhaustion:
        send_health_alert(crm_failure_alert_text(payload, max_retries, last_failure))
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
        permanent = None
        try:
            success = log_to_sheets_crm(
                payload, max_retries=1, raise_on_permanent=True, alert_on_exhaustion=False
            )
        except PermanentCRMRejection as e:
            # Impossible to satisfy by retrying (e.g. the row's sheet_uuid is not in any tab), so
            # drop it instead of requeueing. Alert ONCE here rather than on all 10 retry passes.
            success = False
            permanent = e
            logging.error(
                f"CRM Outbox dropping permanently-rejected payload #{job_id} "
                f"('{e.action}', sheet_uuid={payload.get('sheet_uuid')}): {e.message}"
            )
            send_health_alert(
                f"CRM write '{e.action}' dropped - {e.message}. The row is NOT in the sheet, so this "
                "write can never land; it was discarded instead of retried. Anything it carried "
                "(status move, follow-up date, note) must be set by hand."
            )

        with get_db_conn() as conn:
            if success or permanent:
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

        # The outbox owns the retry budget, so it also owns the "this will never land" alert - fired
        # once, at the pass that gives up, instead of on every pass that merely backs off.
        if not (success or permanent) and retries + 1 >= 10:
            logging.error(f"CRM Outbox abandoning payload #{job_id} after 10 attempts")
            send_health_alert(
                crm_failure_alert_text(payload, 10)
                + " Giving up after 10 retries - it is NOT in the sheet. "
                "Anything it carried (status move, follow-up date, note) must be set by hand."
            )
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
    """Rows from one CRM tab, or [] when the read failed.

    HTTP 200 IS NOT SUCCESS. An Apps Script web app answers 200 for everything it handles,
    including its own {"status":"error"} bodies - an unset or mismatched CRM_SHARED_SECRET makes
    doPost reject every request as "Unauthorized" behind a 200. Reading .get("followups", [])
    off that body yields [], which is indistinguishable from a genuinely empty tab. On
    2026-09-21 that made /links check report "Checked 0 links" against a sheet holding dozens of
    rows, reading as a clean result when the CRM was entirely unreachable.

    So a rejection is logged loudly and, once per process, alerted - silence here means callers
    (the link sweep, the sequencer, the tracked-role gate) quietly act on an empty world.
    """
    res = crm_post({"action": "get_followups", "tab": target_code})
    if not res:
        logging.error(f"[CRM] get_followups({target_code}): no response from the webhook")
        return []
    try:
        if res.status_code != 200:
            logging.error(f"[CRM] get_followups({target_code}): HTTP {res.status_code}")
            return []
        body = res.json()
        if not isinstance(body, dict):
            logging.error(f"[CRM] get_followups({target_code}): non-dict body")
            return []
        status = str(body.get("status", "")).lower()
        if status and status != "success":
            message = str(body.get("message", ""))
            logging.error(f"[CRM] get_followups({target_code}) REJECTED: {message}")
            _alert_crm_read_rejection(message)
            return []
        leads = body.get("followups", [])
        return leads if qty is None else leads[:qty]
    except Exception as e:
        logging.error(f"Error fetching networking cards: {e}")
    return []


# One alert per process for a rejected CRM read. Every caller of fetch_networking_cards would
# otherwise fire its own, and the sweep alone calls it three times per pass.
_CRM_READ_REJECTION_ALERTED = threading.Event()


def _alert_crm_read_rejection(message):
    if _CRM_READ_REJECTION_ALERTED.is_set():
        return
    _CRM_READ_REJECTION_ALERTED.set()
    hint = ""
    if "unauthorized" in str(message).lower():
        # Apps Script returns a bare "Unauthorized" for several distinct causes and does not say
        # which: a mismatched secret, a BLANK Script Property (isRequestAuthorized fails closed on
        # an unset one), a CRM_SHARED_SECRET missing from Render so no secret is sent at all, or a
        # deployment serving an older version of the script. Naming only the first sent Kevin to
        # re-check a secret that already matched, so list what it actually could be.
        hint = (
            " 'Unauthorized' means the secret Apps Script received did not equal the one in its "
            "Script Properties. Check, in order: (1) CRM_SHARED_SECRET is set on RENDER - if it is "
            "missing the bot sends no secret at all; (2) the Script Property exists and is not "
            "blank; (3) both sides have no trailing whitespace; (4) the deployment was republished "
            "after the property was set (Deploy > Manage deployments > New version) - an edited "
            "property does not reach the live web app until then."
        )
    send_health_alert(
        f"CRM READS are being rejected - every tab is coming back EMPTY, so the link sweep, the "
        f"sequencer and the duplicate gate are all seeing a blank sheet. {message}{hint}"
    )

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

# Where a Carmen Cold contact goes after three nudges and a grace week with no reply. Kevin's call:
# Killed, the existing PEOPLE archive - it is reversible (the row still exists) and skipped by
# quick_add's duplicate check, so the person can be re-added later. Set to "Carmen Warm" to return
# ghosts to the bench instead. A contact who DID reply is never moved automatically.
CARMEN_GHOST_TAB = "Killed"

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

def find_carmen_contacts(token):
    """Resolve a /promote or /demote id to [(sheet_uuid, tab, record)] among Carmen Cold and
    Carmen Hot rows. Accepts what the morning card prints as 🆔: a jobs-cache short_id, or - for
    the many contacts with no cached job - the first 8+ characters of the sheet_uuid, which
    get_sheet_uuid_by_short_id() alone cannot resolve. Matching only these two tabs also keeps a
    short_id that belongs to a JOBS row from being moved into a PEOPLE tab.
    """
    token = str(token or "").strip()
    if not token:
        return []
    exact = str(get_sheet_uuid_by_short_id(token) or "").lower()
    prefix = token.lower() if len(token) >= 8 else ""
    # EMAIL or NAME, because the UUID is the one identifier Kevin cannot see: Column J is hidden
    # by formatSheet(), and a Carmen contact captured by /e or the sent-mail sweep has no job card
    # and therefore no 🆔 in Telegram at all. Promoting Beth Young by UUID meant unhiding a column
    # and copying 8 characters by hand; "beth.young@altarum.org" is on screen and unambiguous.
    # Name is matched case-insensitively and in full - a bare "beth" would be a coin flip the day
    # a second Beth is captured, and find_carmen_contacts()'s contract is that 2+ hits refuse.
    needle = token.lower()
    hits = {}
    for code, tab_name in (("CC", "Carmen Cold"), ("CH", "Carmen Hot")):
        for rec in fetch_networking_cards(code, qty=None) or []:
            uuid_val = str(rec.get("sheet_uuid") or "")
            low = uuid_val.lower()
            matched = bool(uuid_val) and ((exact and low == exact) or (prefix and low.startswith(prefix)))
            if not matched:
                rec_email = str(rec.get("email") or "").strip().lower()
                rec_name = str(rec.get("name") or "").strip().lower()
                matched = bool(needle) and needle in (rec_email, rec_name)
            if matched and uuid_val:
                hits[uuid_val] = (uuid_val, tab_name, rec)
    return list(hits.values())

# Rejections resolved to exactly one live job row are archived without asking; 2+ raise a pick
# card instead. A rejection names ONE req, so killing every row at that company would archive
# roles Kevin is still live on - Trinity Health alone carries several.
PENDING_REJECTION_KILLS = {}
_PENDING_KILL_LOCK = threading.Lock()


def extract_company_from_rejection(subject, body_text):
    """The employer named inside an ATS rejection, or "".

    Needed because the SENDER cannot answer it. trinityhealth@myworkday.com is a role mailbox on
    an ATS domain, so company_domain_of() returns "" and no CRM match is possible - the alert
    shows "@ Unknown (Not in CRM)". But the body says it plainly: "your interest in the EHR
    Clinical Analyst - Onsite in Southeast Michigan position at IHA Medical Group".

    Patterns only, no LLM: a rejection is a form letter, and the phrasings are fixed. Returns ""
    rather than a guess when nothing matches - route_rejection_to_died() then does nothing, which
    is the correct outcome for an employer this cannot identify.
    """
    text = " ".join(str(body_text or "")[:2000].split())
    patterns = (
        r"position at\s+([A-Z][\w&.,'\- ]{2,60}?)\s*[.\n]",
        r"role at\s+([A-Z][\w&.,'\- ]{2,60}?)\s*[.\n]",
        r"opportunity at\s+([A-Z][\w&.,'\- ]{2,60}?)\s*[.\n]",
        r"application (?:to|with)\s+([A-Z][\w&.,'\- ]{2,60}?)\s*[.\n]",
        r"interest in\s+(?:joining\s+)?([A-Z][\w&.,'\- ]{2,60}?)\s*[.\n]",
        r"careers? at\s+([A-Z][\w&.,'\- ]{2,60}?)\s*[.\n]",
    )
    for pat in patterns:
        found = re.search(pat, text)
        if found:
            company = found.group(1).strip(" .,")
            # "the next phase of our recruiting process" style tails are not company names.
            if company and len(company) >= 3 and not company.lower().startswith(("our ", "the ", "your ")):
                return company
    return ""


def find_live_job_rows_for_company(company):
    """Live JOB rows at `company`, newest tab first: [{sheet_uuid, tab, title, status}].

    Reads the same three tabs locate_tracked_role() does. Died is deliberately excluded - a row
    already archived is not a row to archive again - and the Carmen PEOPLE tabs are excluded
    because a rejection is a JOB outcome; the recruiter who sent it stays a live contact.
    """
    want = normalize_company_for_match(company)
    if not want:
        return []
    rows = []
    for target_code, tab_name in (("TC", "Tetiana Cold"), ("TW", "Tetiana Warm"), ("CL", "Clavicular")):
        for rec in fetch_networking_cards(target_code, qty=None) or []:
            if normalize_company_for_match(rec.get("company")) != want:
                continue
            sheet_uuid = str(rec.get("sheet_uuid") or "").strip()
            if not sheet_uuid:
                continue
            rows.append({
                "sheet_uuid": sheet_uuid,
                "tab": tab_name,
                "title": str(rec.get("job_title") or rec.get("title") or "").strip() or "(untitled role)",
                "status": str(rec.get("status") or "").strip(),
            })
    return rows


def route_rejection_to_died(company, subject="", snippet=""):
    """A classified REJECTION -> archive the job row it refers to. Returns a Telegram-ready
    summary line, or "" when there is nothing to say.

    One live row at the company: archived to Died immediately, because there is no ambiguity to
    resolve and a rejection Kevin has to hand-file is a rejection that sits in the tray for a week.

    Two or more: NOTHING is written. The rows are parked in PENDING_REJECTION_KILLS and listed as
    a numbered pick card - "/kill 2" - because the ATS names one req and the company may hold
    several. Auto-killing all of them would archive live applications silently, which is the one
    outcome worse than filing by hand.

    The pick card carries NO 🆔 marker on purpose: _parse_sheet_uuid_from_card_text() takes the
    FIRST uuid in a message, so a swipe-reply on a multi-entry card would always act on entry #1.
    """
    company = str(company or "").strip()
    if not company:
        return ""
    try:
        rows = find_live_job_rows_for_company(company)
    except Exception as e:
        logging.error(f"[REJECTION ROUTING] Row lookup failed for {company}: {e}")
        return ""
    if not rows:
        logging.info(f"[REJECTION ROUTING] No live job row at '{company}' - nothing to archive")
        return ""

    today_str = datetime.now().strftime("%Y-%m-%d")
    if len(rows) == 1:
        row = rows[0]
        new_tab = resolve_smart_target_tab(row["tab"], "kill")
        enqueue_crm_payload(build_crm_payload(
            "update_status", sheet_uuid=row["sheet_uuid"], new_tab=new_tab))
        enqueue_crm_payload(build_crm_payload(
            "append_note", sheet_uuid=row["sheet_uuid"],
            note=f"[{today_str}] Rejected - auto-archived to {new_tab} from the inbound rejection."))
        record_application_outcome(row["sheet_uuid"], "rejection", company=company)
        logging.info(f"[REJECTION ROUTING] Archived {company} / {row['title']} to {new_tab}")
        return (f"\n💀 <b>Auto-archived to {new_tab}:</b> {html.escape(row['title'])}\n"
                f"<i>The only live row at {html.escape(company)}.</i>\n")

    # Ambiguous. Park the candidates against the company key and ask.
    with _PENDING_KILL_LOCK:
        PENDING_REJECTION_KILLS[normalize_company_for_match(company)] = {
            "company": company, "rows": rows, "at": today_str,
        }
    lines = [f"\n⚠️ <b>{len(rows)} live roles at {html.escape(company)}</b> - "
             f"<i>nothing archived, pick one:</i>"]
    for i, row in enumerate(rows, 1):
        status = f" · {html.escape(row['status'])}" if row["status"] else ""
        lines.append(f"  <b>{i}.</b> {html.escape(row['title'])} <i>({html.escape(row['tab'])}{status})</i>")
    lines.append(f"<code>/kill {html.escape(company)} 1</code> · "
                 f"<code>/kill {html.escape(company)} all</code> · ignore to keep them all")
    return "\n".join(lines) + "\n"


def resolve_pending_kill(company_token, choice):
    """`/kill <company> <n|all>` -> archive the picked row(s). Returns a Telegram-ready reply."""
    key = normalize_company_for_match(company_token)
    with _PENDING_KILL_LOCK:
        pending = PENDING_REJECTION_KILLS.get(key)
    if not pending:
        return (f"⚠️ <b>Nothing pending</b> for <code>{html.escape(company_token)}</code>. "
                f"A pick expires when the bot restarts - archive it with <code>/x</code> on the card instead.")
    rows = pending["rows"]
    choice = str(choice or "").strip().lower()
    if choice == "all":
        picked = list(rows)
    else:
        try:
            idx = int(choice)
        except ValueError:
            return f"⚠️ <b>Pick a number</b> 1-{len(rows)}, or <code>all</code>."
        if not 1 <= idx <= len(rows):
            return f"⚠️ <b>Out of range:</b> pick 1-{len(rows)}, or <code>all</code>."
        picked = [rows[idx - 1]]

    today_str = datetime.now().strftime("%Y-%m-%d")
    done = []
    for row in picked:
        new_tab = resolve_smart_target_tab(row["tab"], "kill")
        enqueue_crm_payload(build_crm_payload(
            "update_status", sheet_uuid=row["sheet_uuid"], new_tab=new_tab))
        enqueue_crm_payload(build_crm_payload(
            "append_note", sheet_uuid=row["sheet_uuid"],
            note=f"[{today_str}] Rejected - archived to {new_tab} via /kill."))
        record_application_outcome(row["sheet_uuid"], "rejection", company=pending["company"])
        done.append(f"• {html.escape(row['title'])} → {new_tab}")
    # Cleared either way: a second /kill on the same card would re-archive rows already moved.
    with _PENDING_KILL_LOCK:
        PENDING_REJECTION_KILLS.pop(key, None)
    return f"💀 <b>Archived {len(done)} role(s)</b> at {html.escape(pending['company'])}\n" + "\n".join(done)


def promote_job_card_contact(chat_id, token, extra):
    """`/promote <job_id> Name name@company.com` -> a new Carmen Hot row. True when handled.

    The job row is READ, never moved: its company is the only field borrowed, so the role stays in
    its pipeline tab with its Status, Fit Score and Golden Ratio contribution intact. A JOBS->PEOPLE
    tab move would instead blank the name column (JOBS has no `name` field), demote the title to a
    "[Former Role: ...]" note, and delete the source row - losing the application to gain a nameless
    contact.

    Returns False without sending anything when `extra` carries no email, so the caller falls
    through to its own "not a contact" message and the normal two-token /promote is untouched.
    """
    email_match = re.search(r"[\w\.\-\+]+@[\w\.\-]+\.\w+", extra or "")
    if not email_match:
        return False
    email = email_match.group(0).strip().lower()
    # Whatever is left once the address is removed is the person's name. Falls back to the
    # address's local part, the same way log_addressed_contact_to_carmen_cold() does.
    name = re.sub(re.escape(email_match.group(0)), "", extra, flags=re.IGNORECASE).strip(" ,<>-").strip()

    job = get_job_from_cache(token) or get_job_by_sheet_uuid(token) or {}
    company = str(job.get("employer_name") or "").strip()
    title = str(job.get("job_title") or "").strip()
    if not job:
        send_telegram_message(
            chat_id,
            f"⚠️ <b>Not added:</b> <code>{html.escape(token)}</code> is not a job 🆔 in the cache. "
            f"Use the 🆔 exactly as the card shows it."
        )
        return True

    if is_logged_person_contact(email):
        send_telegram_message(chat_id, f"ℹ️ <code>{html.escape(email)}</code> is already a CRM contact.")
        return True
    # Role mailboxes and consumer domains are refused for the same reason the /e capture gate
    # refuses them: bizops@ is an inbox, not the person who replied.
    if is_role_mailbox(email) or not company_domain_of(email):
        send_telegram_message(
            chat_id,
            f"⚠️ <b>Not added:</b> <code>{html.escape(email)}</code> is a role mailbox or a "
            f"consumer address - Carmen rows are for named people."
        )
        return True

    today_str = datetime.now().strftime("%Y-%m-%d")
    sheet_uuid = str(uuid.uuid4())
    contact_name = name or name_from_email_local_part(email)
    contact_company = company or (company_domain_of(email) or "").split(".")[0].title()
    note = f"[{today_str}] Replied re: {title} - promoted from the job card." if title else \
           f"[{today_str}] Promoted from the job card."
    # Straight into Carmen Hot: this path exists because the person ALREADY replied, which is the
    # exact condition /promote's Cold->Hot move is for. Routing via Carmen Cold would need a
    # second command to undo.
    payload = build_crm_payload(
        "quick_add",
        target_code="CH",
        sheet_uuid=sheet_uuid,
        first_contact=today_str,
        last_contact=today_str,
        name=contact_name,
        company=contact_company,
        email=email,
        priority=9,
        status="Replied",
        next_followup=(datetime.now() + timedelta(days=CARMEN_LADDER_DAYS[0])).strftime("%Y-%m-%d"),
        source="Promoted from job card",
        note=note,
    )
    if not log_to_sheets_crm(payload):
        send_telegram_message(chat_id, f"⚠️ <b>CRM write failed</b> for {html.escape(contact_name)} - not added.")
        return True
    record_captured_contact(
        sheet_uuid=sheet_uuid,
        sheet_tab="Carmen Hot",
        contact_name=contact_name,
        contact_company=contact_company,
        contact_email=email,
    )
    logging.info(f"[/promote] Created Carmen Hot contact {email} ({contact_company}) from job {token}")
    role_line = f"\n<i>Re: {html.escape(title)}</i>" if title else ""
    send_telegram_message(
        chat_id,
        f"🔥 <b>Added {html.escape(contact_name)}</b> to Carmen Hot\n"
        f"📧 <code>{html.escape(email)}</code> @ {html.escape(contact_company)}{role_line}\n"
        f"<i>The job row was not moved - use /interview {html.escape(token)} to advance it.</i>"
    )
    return True


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

def _sequencer_draft_recipient(record):
    """The row's real address, or "" when there is nothing safe to draft to. Same guard as the
    /sendall path: a bracketed value is a UI confidence tag, not an address."""
    email = str(record.get("email") or "").strip()
    return "" if (not email or "[" in email) else email

def _sequencer_draft_subject(record):
    """The follow-up bump's subject, mirroring process_overdue_batch()'s sendall bump: "Re:" so it
    threads, with the company-only form for a roleless PEOPLE row. create_gmail_draft() dedups on
    this, so it must be computed identically everywhere it is checked."""
    company = record.get("company") or "Target Firm"
    if str(record.get("title") or "").strip():
        return f"Re: {record.get('title')} @ {company}"
    return f"Re: {company}"

def _stage_sequencer_draft(record, draft_text):
    """Stage one follow-up as a Gmail draft - never sends. Returns (draft_id, created, message).

    draft_text is passed verbatim so Gmail holds exactly what the /followups page shows. draft_id
    is also returned for a same-subject draft already in Gmail (create_gmail_draft answers that
    with ok=False and the real id), so callers must branch on draft_id, not on `created`. Never
    raises; on failure draft_id is None and message says why.
    """
    email = _sequencer_draft_recipient(record)
    try:
        ok, message, draft_id = create_gmail_draft(
            to_email=email,
            company_name=record.get("company") or "Target Firm",
            job_title=record.get("title") or "",
            custom_body=draft_text,
            custom_subject=_sequencer_draft_subject(record),
        )
    except Exception as e:
        logging.error(f"[FOLLOWUPS] Gmail draft error ({record.get('sheet_uuid')}): {e}")
        return None, False, f"Gmail request failed: {e}"
    if not ok and not draft_id:
        logging.error(f"[FOLLOWUPS] Gmail draft not created ({record.get('sheet_uuid')}): {message}")
        return None, False, str(message or "Gmail did not create the draft")
    return (draft_id or None), bool(ok), str(message or "")

def run_followup_sequencer(today=None, dry_run=False):
    """Scan Tetiana Cold/Warm + Clavicular via get_followups, run followup_action() on every row,
    and return a structured plan: followups_ready / applications_quiet / going_cold / buried /
    top_matched / counts.

    Due follow-ups split by tab: PEOPLE rows (Carmen Cold) land in followups_ready with bump text
    and a Gmail draft; JOBS rows land in applications_quiet as status only - no text, no draft -
    but are still snoozed and logged so the +4/+9/+16 clock (and the +16 bury) keeps advancing.

    Unless dry_run: queues a +window snooze via update_snooze for each drafted follow-up, buries
    each ghosted row (append_note '[reason: ghosted]' + update_status -> Died), and records each
    actioned row in followup_sequencer_log. dry_run=True (the /queue path) performs ZERO writes.

    Idempotent: (a) same-day - followup_sequencer_log skips a row already actioned today;
    (b) across days - a queued follow-up pushes Next Followup Date to the next window boundary
    (so followup_action()'s future gate returns "none" until then) and a bury moves the row off
    the scanned tabs entirely.

    Buries are capped at MAX_AUTO_BURIES_PER_RUN per pass (see counts["buries_suppressed"]).

    Carmen Cold runs its own 4/11/21 ladder (plan_carmen_ladder). A stale row is revived - a dated
    restart note plus a fresh first nudge, listed in `revived`. When the ladder is exhausted, a
    contact whose notes carry a reply goes to `ready_to_promote` (nothing written); a silent one
    is moved to CARMEN_GHOST_TAB and listed in `killed`, capped at MAX_AUTO_KILLS_PER_RUN
    (see counts["kills_suppressed"]).

    No Gmail drafts are created here. Each followups_ready entry carries its draft_text and the
    recipient fields, and GET /followups/draft/<sheet_uuid> creates the draft from the saved
    snapshot only when Kevin clicks it.
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

    result = {"run_date": run_date, "followups_ready": [], "ready_to_promote": [], "revived": [],
              "applications_quiet": [], "going_cold": [], "buried": [], "killed": [],
              "top_matched": [], "counts": {}}
    buries_written = 0
    buries_suppressed = 0
    kills_written = 0
    kills_suppressed = 0

    for rec in records:
        # Carmen Cold runs the 4/11/21 people ladder instead of the JOBS +4/+9/+16 windows: these
        # are networking contacts, so the cadence is tighter and the sequence ends quietly rather
        # than burying. Rung is read from the row's own dates, so a contact dragged in by hand
        # joins the ladder on this pass with nothing to configure.
        if rec.get("sheet_tab") in SEQUENCER_PEOPLE_SCHEMA_TABS:
            note_text = rec.get("note") or ""
            plan = plan_carmen_ladder(rec.get("date_added"), rec.get("next_followup"), today, note=note_text)
            ladder_action, ladder_next = plan.action, plan.next_date
            sheet_uuid = rec.get("sheet_uuid")

            def _write_marker(action_name):
                """Stamp the Carmen ladder marker into Column E. Column E, not Status: a decorated
                Status reads as status_rank() == -1, which makes followup_action() return "none"
                and silently stops sequencing the row.

                Skipped on dry_run (the /queue path performs zero writes) and when the cell is
                already correct, so a re-run does not churn the sheet."""
                if dry_run or not sheet_uuid:
                    return
                marker = carmen_status_marker(action_name, plan.replied, plan.ladder)
                if not marker:
                    return
                current = rec.get("raw_priority") or ""
                merged = carmen_marker_cell(current, marker)
                if merged == str(current).strip():
                    return
                enqueue_crm_payload(build_crm_payload(
                    "set_context", sheet_uuid=sheet_uuid, context=merged,
                ))

            if ladder_action in ("none", "hold"):
                # "hold" is a hand-set future date the sequencer is deliberately respecting rather
                # than laddering. Mark it so the sheet distinguishes "waiting on purpose" from "the
                # sequencer forgot this row". Plain "none" is the ordinary quiet between rungs and
                # must NOT be marked: it fires on every day a row is simply waiting, and would
                # overwrite the real rung marker the next morning.
                if ladder_action == "hold":
                    _write_marker(ladder_action)
                continue
            person = {
                "company": rec.get("company") or "N/A",
                "role": rec.get("title") or "",
                "name": rec.get("name") or "",
                "short_id": get_short_id_by_sheet_uuid(sheet_uuid) if sheet_uuid else None,
                "sheet_uuid": sheet_uuid,
                "sheet_tab": rec.get("sheet_tab"),
            }
            if ladder_action == "exhausted":
                # Three nudges and a grace week are done. Triage on the reply marker the inbound
                # router writes: a person who talked to Kevin is never moved automatically.
                replied_on = carmen_reply_anchor(note_text)
                if replied_on is not None:
                    # Nothing written, so the row reappears every morning until Kevin runs
                    # /promote or /demote - that daily reminder is intended. The marker is the one
                    # exception: it makes the waiting row findable by sorting Column E.
                    _write_marker(ladder_action)
                    result["ready_to_promote"].append({**person, "replied_on": replied_on.strftime("%Y-%m-%d")})
                    continue
                result["killed"].append(person)
                if dry_run or not sheet_uuid or _sequencer_already_actioned(sheet_uuid, run_date):
                    continue
                if kills_written >= MAX_AUTO_KILLS_PER_RUN:
                    # Same deferral as a capped bury: reported, not written, not logged, so the
                    # row stays eligible and drains on a later run.
                    kills_suppressed += 1
                    continue
                enqueue_crm_payload(build_crm_payload(
                    "append_note", sheet_uuid=sheet_uuid,
                    note=f"[reason: no reply after {len(plan.ladder)} nudges]",
                ))
                enqueue_crm_payload(build_crm_payload("update_status", sheet_uuid=sheet_uuid, new_tab=CARMEN_GHOST_TAB))
                _record_sequencer_action(sheet_uuid, run_date, "kill_ghosted")
                kills_written += 1
                continue
            if ladder_action == "schedule":
                first_nudge = ladder_next.strftime("%Y-%m-%d")
                if plan.revived:
                    result["revived"].append({**person, "first_nudge": first_nudge})
                if dry_run or not sheet_uuid:
                    continue
                if plan.revived:
                    if _sequencer_already_actioned(sheet_uuid, run_date):
                        continue
                    # Date Added is real history and is never rewritten, so the restart is recorded
                    # as a dated note - plan_carmen_ladder() reads it back as the new anchor.
                    # Without it the next pass would revive again and the row would never climb.
                    cadence = "/".join(str(d) for d in plan.ladder)
                    enqueue_crm_payload(build_crm_payload(
                        "append_note", sheet_uuid=sheet_uuid,
                        note=f"[{run_date}] {LADDER_RESTART_NOTE_MARKER} - stale anchor, {cadence} restarted "
                             f"from today. First nudge {first_nudge}.",
                    ))
                enqueue_crm_payload(build_crm_payload(
                    "update_snooze", sheet_uuid=sheet_uuid, next_followup=first_nudge,
                ))
                _write_marker(ladder_action)
                if plan.revived:
                    _record_sequencer_action(sheet_uuid, run_date, "revive")
                continue

            attempt = int(ladder_action.rsplit("_", 1)[1])
            entry = {
                "company": rec.get("company") or "N/A",
                "role": rec.get("title") or "",
                "short_id": get_short_id_by_sheet_uuid(sheet_uuid) if sheet_uuid else None,
                "sheet_uuid": sheet_uuid,
                "attempt": attempt,
                "draft_text": build_followup_bump_draft(rec, attempt),
                "sheet_tab": rec.get("sheet_tab"),
                # Read from the row's OWN ladder, not the module constant: a cold row walks
                # (4, 11), so indexing the engaged tuple would report the wrong day and never
                # flag its real final rung.
                "ladder_day": plan.ladder[attempt - 1] if attempt <= len(plan.ladder) else plan.ladder[-1],
                "final_rung": attempt == len(plan.ladder),
                "track": "engaged" if plan.replied else "cold",
                "name": rec.get("name") or "",
                # Recipient fields for the on-demand draft route (raw company, not the "N/A" label).
                "email": rec.get("email") or "",
                "company_raw": rec.get("company") or "",
                "next_followup": rec.get("next_followup") or "",
                "new_next_followup": ladder_next.strftime("%Y-%m-%d") if ladder_next else None,
            }
            result["followups_ready"].append(entry)
            if dry_run or not sheet_uuid or _sequencer_already_actioned(sheet_uuid, run_date):
                continue
            if ladder_next is not None:
                enqueue_crm_payload(build_crm_payload(
                    "update_snooze", sheet_uuid=sheet_uuid,
                    next_followup=ladder_next.strftime("%Y-%m-%d"),
                ))
            _write_marker(ladder_action)
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
            base = anchor or today
            push_days = FOLLOWUP_2_DAYS if attempt == 1 else FOLLOWUP_BURY_DAYS
            new_nf = (base + timedelta(days=push_days)).strftime("%Y-%m-%d")
            if rec.get("sheet_tab") not in SEQUENCER_PEOPLE_SCHEMA_TABS:
                # A job application is watched, not messaged: its Contact Email is often Kevin's own
                # address or a contact already tracked in Carmen Cold, so no bump text and no Gmail
                # draft. The snooze still advances, or the +16 bury would never be reached.
                result["applications_quiet"].append({
                    "company": company, "role": role, "short_id": short_id, "sheet_uuid": sheet_uuid,
                    "sheet_tab": rec.get("sheet_tab"), "attempt": attempt,
                    "date_added": rec.get("date_added") or "",
                    "next_followup": rec.get("next_followup") or "",
                    "new_next_followup": new_nf,
                    "days_silent": days_since,
                    "buries_on": (anchor + timedelta(days=FOLLOWUP_BURY_DAYS)).strftime("%Y-%m-%d") if anchor else None,
                })
                if dry_run or already or not sheet_uuid:
                    continue
                enqueue_crm_payload(build_crm_payload("update_snooze", sheet_uuid=sheet_uuid, next_followup=new_nf))
                _record_sequencer_action(sheet_uuid, run_date, action)
                continue
            entry = {
                "company": company, "role": role, "short_id": short_id, "sheet_uuid": sheet_uuid,
                "attempt": attempt, "draft_text": build_followup_bump_draft(rec, attempt),
                "sheet_tab": rec.get("sheet_tab"),
                "name": rec.get("name") or "",
                "next_followup": rec.get("next_followup") or "", "new_next_followup": new_nf,
                "email": rec.get("email") or "", "company_raw": rec.get("company") or "",
            }
            result["followups_ready"].append(entry)
            if dry_run or already or not sheet_uuid:
                continue
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

    result["counts"] = {k: len(result[k]) for k in ("followups_ready", "ready_to_promote", "revived",
                                                     "applications_quiet", "going_cold", "buried",
                                                     "killed", "top_matched")}
    # Not a section length like the four above: how many of result["buried"] were reported but
    # left unwritten by the cap. Never nonzero on its own (it implies buried > 0), so the card's
    # all-empty early return stays correct.
    result["counts"]["buries_suppressed"] = buries_suppressed
    # And a subset of killed: ghosts listed but left in Carmen Cold by MAX_AUTO_KILLS_PER_RUN.
    result["counts"]["kills_suppressed"] = kills_suppressed
    return result

def _seq_id_tag(entry):
    """short_id for /replied /interview, falling back to a sheet_uuid stub, or an em dash."""
    return entry.get("short_id") or (str(entry.get("sheet_uuid") or "")[:8]) or "—"

def _followup_date_label(value, blank="—"):
    """A CRM date for display: the 1970-01-01 "unscheduled" sentinel and blanks read as `blank`."""
    text = str(value or "").strip()
    return blank if (not text or is_followup_unscheduled(text)) else text

def _next_step_label(entry):
    """What follows this nudge: the next rung's date, or - after the last rung - the triage date
    on which a still-silent contact is moved to CARMEN_GHOST_TAB."""
    nxt = _followup_date_label(entry.get("new_next_followup"), blank="last nudge")
    if entry.get("final_rung") and entry.get("new_next_followup"):
        return f"{CARMEN_GHOST_TAB.lower()} {nxt} if silent"
    return f"next {nxt}"

def _silence_dot(days):
    """Severity dot for an application's silence, on the job card's fit-dot idiom. The bands
    track the 4/9/16 JOBS ladder: red means the +16 bury is at most a day away."""
    if not isinstance(days, int):
        return "⚪"
    if days <= 4:
        return "🟢"
    if days <= 9:
        return "🟡"
    if days <= 14:
        return "🟠"
    return "🔴"

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
        lines.append(f"\n▶ <b>Nudge these people ({len(ready)})</b>")
        for e in ready:
            who = html.escape(str(e.get("role") or e.get("name") or "—"))
            company = html.escape(str(e.get("company") or "—"))
            due = html.escape(_followup_date_label(e.get("next_followup")))
            step = html.escape(_next_step_label(e))
            lines.append(
                f"💼 <b>{who}</b> — {company} · #{html.escape(str(e.get('attempt', 1)))} · due {due} → {step}"
                f" · 🆔 <code>{html.escape(_seq_id_tag(e))}</code>"
            )
        # Draft text and the on-demand Gmail links live on /followups, not here - this card stays a
        # scannable list, and no draft exists until Kevin clicks one there.
        queue_url = html.escape(f"{BASE_URL}/followups", quote=True)
        lines.append(f"📋 <a href='{queue_url}'>Open Follow-up Queue</a>")
        # No full-sheet_uuid 🆔 line and no swipe legend: this card holds N entries in one message,
        # and _parse_sheet_uuid_from_card_text takes the first UUID it finds, so a swipe-reply would
        # silently act on entry #1. Swipes here fail cleanly instead; actions carry their own id.
        lines.append("<i>Swipe-replies don't work on this card - act via the 📋 links, or "
                     "<code>/replied &lt;id&gt;</code> · <code>/interview &lt;id&gt; [YYYY-MM-DD]</code> "
                     "with the 🆔 above.</i>")

    promote = result.get("ready_to_promote", [])
    if promote:
        lines.append(f"\n▶ <b>Ready to promote ({len(promote)})</b> <i>— replied, ladder finished</i>")
        for e in promote:
            company = html.escape(str(e.get("company") or "—"))
            who = html.escape(str(e.get("name") or e.get("role") or "—"))
            tag = html.escape(_seq_id_tag(e))
            lines.append(
                f"• <b>{company}</b> — {who} · replied {html.escape(_followup_date_label(e.get('replied_on')))}"
                f" · 🆔 <code>{tag}</code> · <code>/promote {tag}</code>"
            )

    revived = result.get("revived", [])
    if revived:
        lines.append(f"\n▶ <b>Back on the ladder ({len(revived)})</b> <i>— stale date, restarted from today</i>")
        for e in revived:
            company = html.escape(str(e.get("company") or "—"))
            who = html.escape(str(e.get("name") or e.get("role") or "—"))
            lines.append(
                f"• <b>{company}</b> — {who} · revived — ladder restarted today"
                f" · first nudge {html.escape(_followup_date_label(e.get('first_nudge')))}"
                f" · 🆔 <code>{html.escape(_seq_id_tag(e))}</code>"
            )

    quiet = result.get("applications_quiet", [])
    if quiet:
        lines.append(f"\n▶ <b>Applications going quiet ({len(quiet)})</b> <i>— watch only, no drafts</i>")
        for e in quiet:
            company = html.escape(str(e.get("company") or "—"))
            role = html.escape(str(e.get("role") or "—"))
            days = e.get("days_silent")
            days_str = f"{days}d silent" if isinstance(days, int) else "? silent"
            line = (
                f"{_silence_dot(days)} <b>{company}</b> — {role}"
                f" · applied {html.escape(_followup_date_label(e.get('date_added')))} · {days_str}"
                f" · buries {html.escape(_followup_date_label(e.get('buries_on')))}"
            )
            if e.get("short_id"):
                stage_url = html.escape(f"{BASE_URL}/stage/{e['short_id']}", quote=True)
                line += f" · 📋 <a href='{stage_url}'>Full Card</a>"
            lines.append(line)

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

    killed = result.get("killed", [])
    if killed:
        lines.append(
            f"\n▶ <b>Killed overnight ({len(killed)})</b> "
            f"<i>— no reply after 3 nudges, moved to {html.escape(CARMEN_GHOST_TAB)}</i>"
        )
        for e in killed:
            company = html.escape(str(e.get("company") or "—"))
            who = html.escape(str(e.get("name") or e.get("role") or "—"))
            lines.append(f"• <b>{company}</b> — {who} · <code>{html.escape(_seq_id_tag(e))}</code>")
        kills_capped = counts.get("kills_suppressed", 0)
        if kills_capped:
            # Same honesty rule as the bury cap: the list mixes moved and withheld rows.
            lines.append(
                f"🛑 <i>{kills_capped} of these were withheld by the safety cap "
                f"(max {MAX_AUTO_KILLS_PER_RUN}/run) and not moved — re-run to process the rest.</i>"
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
        f"{counts.get('applications_quiet', 0)} applications quiet · "
        f"{counts.get('going_cold', 0)} going cold · {counts.get('buried', 0)} buried · "
        f"{counts.get('top_matched', 0)} top matches"
    )
    if counts.get("buries_suppressed", 0):
        summary += f" · {counts['buries_suppressed']} buries capped"
    for key, label in (("ready_to_promote", "to promote"), ("revived", "revived"), ("killed", "killed")):
        if counts.get(key, 0):
            summary += f" · {counts[key]} {label}"
    if counts.get("kills_suppressed", 0):
        summary += f" · {counts['kills_suppressed']} kills capped"
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

FOLLOWUP_SNAPSHOT_RETENTION_DAYS = 14

def save_followup_queue_snapshot(run_date, result):
    """Persist one run's full result under its run_date for GET /followups, and prune snapshots
    older than FOLLOWUP_SNAPSHOT_RETENTION_DAYS so the table stays bounded.

    The page cannot recompute: this run's own snoozes push every listed row's Next Followup Date
    into the future within seconds, so a later dry_run finds nothing due. The saved entries are
    also what GET /followups/draft/<sheet_uuid> drafts from. Never raises; a failed save must not
    block the card.
    """
    try:
        cutoff = (datetime.strptime(run_date, "%Y-%m-%d")
                  - timedelta(days=FOLLOWUP_SNAPSHOT_RETENTION_DAYS)).strftime("%Y-%m-%d")
        with get_db_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO followup_queue_snapshot (run_date, payload_json, created_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (run_date, json.dumps(result, default=str))
            )
            conn.execute("DELETE FROM followup_queue_snapshot WHERE run_date < ?", (cutoff,))
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"[SEQUENCER] Snapshot save failed ({run_date}): {e}")
        return False

def load_followup_queue_snapshot(run_date):
    """The saved result for run_date, or None when there is none (or it cannot be read)."""
    try:
        with get_db_conn() as conn:
            row = conn.execute(
                "SELECT payload_json FROM followup_queue_snapshot WHERE run_date = ?", (run_date,)
            ).fetchone()
        return json.loads(row[0]) if row else None
    except Exception as e:
        logging.error(f"[SEQUENCER] Snapshot load failed ({run_date}): {e}")
        return None

# Pacing for the nightly link sweep. The sequencer and the digest share this window, so the
# sweep is capped rather than allowed to run until it finishes - a slow host must never delay
# the 08:30 digest.
LINK_CHECK_MAX_ROWS = 40
LINK_CHECK_TIMEOUT = 12
LINK_CHECK_SLEEP = 0.8
# A dead link may retire at most this many rows per pass, matching MAX_AUTO_KILLS_PER_RUN's
# reasoning: overflow is reported, not written, and drains on the next run.
MAX_AUTO_RETIRE_PER_RUN = 10


def fetch_job_link_state(url):
    """GET a job posting and return (status_code, final_url, text, error). Never raises."""
    try:
        res = requests.get(
            url, headers=_JOB_SCRAPE_HEADERS, timeout=LINK_CHECK_TIMEOUT, allow_redirects=True
        )
        return (res.status_code, res.url, res.text[:400000], None)
    except Exception as e:
        return (None, None, "", e)


def check_job_links(limit=LINK_CHECK_MAX_ROWS, auto_retire=True, sleep_between=LINK_CHECK_SLEEP):
    """Sweep live JOBS rows, classify each Job Link, record the verdict and retire the safe ones.

    Only a 'dead' verdict on a row whose Status permits it (see may_auto_retire) is moved to Died.
    An APPLIED row with a dead posting is recorded and surfaced instead: the posting coming down
    means the employer stopped sourcing, not that Kevin was rejected, and burying it would lose a
    live thread.

    Returns a dict the digest and /dead render.
    """
    checked, dead, retired, unknown = 0, [], [], 0
    today_str = datetime.now().strftime("%Y-%m-%d")

    for code, tab_name in (("TC", "Tetiana Cold"), ("TW", "Tetiana Warm"), ("CL", "Clavicular")):
        for rec in fetch_networking_cards(code, qty=None) or []:
            if checked >= limit:
                break
            link = str(rec.get("job_link") or "").strip()
            sheet_uuid = str(rec.get("sheet_uuid") or "").strip()
            if not (link.startswith("http") and sheet_uuid):
                continue

            checked += 1
            status_code, final_url, text, err = fetch_job_link_state(link)
            verdict, reason = classify_job_link(link, status_code, final_url, text, fetch_error=err)
            row_status = rec.get("status")
            company = rec.get("company") or ""
            role = rec.get("job_title") or rec.get("title") or ""

            if verdict == "unknown":
                unknown += 1

            try:
                with get_db_conn() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("""
                        INSERT INTO job_link_status
                            (sheet_uuid, company, role, job_link, status, verdict, reason,
                             first_dead_at, checked_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 'dead' THEN CURRENT_TIMESTAMP END,
                                CURRENT_TIMESTAMP)
                        ON CONFLICT(sheet_uuid) DO UPDATE SET
                            company = excluded.company, role = excluded.role,
                            job_link = excluded.job_link, status = excluded.status,
                            verdict = excluded.verdict, reason = excluded.reason,
                            checked_at = CURRENT_TIMESTAMP,
                            -- keep the ORIGINAL first_dead_at so "dead since" stays true
                            first_dead_at = CASE
                                WHEN excluded.verdict = 'dead'
                                THEN COALESCE(job_link_status.first_dead_at, CURRENT_TIMESTAMP)
                                ELSE NULL END,
                            -- a link that came back alive is newly notifiable if it dies again
                            notified = CASE WHEN excluded.verdict = 'dead' THEN job_link_status.notified ELSE 0 END
                    """, (sheet_uuid, company, role, link, row_status, verdict, reason, verdict))
                    conn.commit()
            except Exception as e:
                logging.error(f"[LINKCHECK] record error ({sheet_uuid}): {e}")

            if verdict == "dead":
                item = {"sheet_uuid": sheet_uuid, "company": company, "role": role,
                        "status": row_status, "tab": tab_name, "reason": reason, "link": link}
                dead.append(item)
                if auto_retire and may_auto_retire(row_status) and len(retired) < MAX_AUTO_RETIRE_PER_RUN:
                    # Same two-step the sequencer's bury uses: note the reason on the row while it
                    # is still in its source tab, then move it.
                    enqueue_crm_payload(build_crm_payload(
                        "append_note", sheet_uuid=sheet_uuid,
                        note=f"[{today_str}] Auto-retired: job link dead ({reason})"
                    ))
                    enqueue_crm_payload(build_crm_payload(
                        "update_status", sheet_uuid=sheet_uuid, new_tab="Died"
                    ))
                    try:
                        with get_db_conn() as conn:
                            conn.execute("UPDATE job_link_status SET retired = 1 WHERE sheet_uuid = ?", (sheet_uuid,))
                            conn.commit()
                    except Exception as e:
                        logging.error(f"[LINKCHECK] retire flag error: {e}")
                    item["retired"] = True
                    retired.append(item)

            if sleep_between:
                time.sleep(sleep_between)

    logging.info(
        f"[LINKCHECK] checked={checked} dead={len(dead)} retired={len(retired)} unknown={unknown}"
    )
    return {"checked": checked, "dead": dead, "retired": retired, "unknown": unknown}


def get_dead_job_links(include_notified=True, limit=40):
    """Dead links recorded by the sweep, newest first."""
    try:
        with get_db_conn() as conn:
            sql = """SELECT sheet_uuid, company, role, job_link, status, reason, retired,
                            first_dead_at
                     FROM job_link_status WHERE verdict = 'dead'"""
            if not include_notified:
                sql += " AND notified = 0"
            sql += " ORDER BY first_dead_at DESC LIMIT ?"
            return conn.execute(sql, (limit,)).fetchall()
    except Exception as e:
        logging.error(f"[LINKCHECK] read error: {e}")
        return []


def mark_dead_links_notified(uuids):
    """Flag these as already surfaced so the digest reports each death once."""
    if not uuids:
        return 0
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "UPDATE job_link_status SET notified = 1 WHERE sheet_uuid = ?",
                [(u,) for u in uuids]
            )
            conn.commit()
        return len(uuids)
    except Exception as e:
        logging.error(f"[LINKCHECK] notify flag error: {e}")
        return 0


def scheduled_job_link_check():
    """APScheduler target: nightly link sweep, after the sequencer and before the digest."""
    logging.info("[LINKCHECK] Nightly job-link sweep triggered")
    try:
        check_job_links()
    except Exception as e:
        logging.error(f"[LINKCHECK] sweep error: {e}", exc_info=True)


def scheduled_followup_sequencer_job():
    """APScheduler target: nightly follow-up sequencer pass (07:30 local, before the digest).
    Applies the automatic bury, queues follow-up drafts, saves the result for GET /followups,
    and posts the single 'needs you today' card. This is the only morning message at this hour -
    the standup digest posts at 08:30.
    """
    logging.info("[SEQUENCER] Nightly follow-up sequencer cycle triggered")
    try:
        result = run_followup_sequencer()
        save_followup_queue_snapshot(result["run_date"], result)
        c = result["counts"]
        logging.info(
            f"[SEQUENCER] followups_ready={c['followups_ready']} "
            f"applications_quiet={c['applications_quiet']} going_cold={c['going_cold']} "
            f"buried={c['buried']} top_matched={c['top_matched']}"
        )
        if TELEGRAM_CHAT_ID:
            _send_telegram_card_chunked(TELEGRAM_CHAT_ID, render_followup_needs_card(result))
    except Exception as e:
        logging.error(f"[SEQUENCER] Nightly cycle error: {e}")
    logging.info("[SEQUENCER] Nightly follow-up sequencer cycle completed")

def start_followup_sequencer():
    """Register the nightly sequencer on the shared background scheduler (07:30 local, one hour
    ahead of the 08:30 morning digest). Bury-to-Died is its only automatic write.

    "Local" means America/Detroit, pinned on EMAIL_POLL_SCHEDULER. Render runs UTC, so before
    the pin this fired at 07:00 UTC (03:00 Detroit).
    """
    EMAIL_POLL_SCHEDULER.add_job(
        scheduled_followup_sequencer_job,
        trigger="cron",
        hour=7,
        minute=30,
        id="followup_sequencer",
        max_instances=1,
        coalesce=True,
    )
    logging.info("[SEQUENCER] Nightly follow-up sequencer scheduled: 07:30 local")


def start_job_link_checker():
    """Register the nightly job-link sweep at 07:45 local - after the sequencer's 07:30 pass so
    the two never overlap on the CRM, and before the 08:30 digest so its results are ready to
    report. Same America/Detroit pin as the rest of the schedule.
    """
    EMAIL_POLL_SCHEDULER.add_job(
        scheduled_job_link_check,
        trigger="cron",
        hour=7,
        minute=45,
        id="job_link_check",
        max_instances=1,
        coalesce=True,
    )
    logging.info("[LINKCHECK] Nightly job-link sweep scheduled: 07:45 local")

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
        f"<code>/x</code> <code>/dead</code> <code>/f</code> <code>/n</code> <code>/e</code> <code>/eh</code> · <code>/help</code>"
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
    the role, the warm contact it maps to, and Apply. Swipe-replies (/apply, /dead, /x, /n, /f) resolve
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
        f"<code>/x</code> <code>/dead</code> <code>/f</code> <code>/n</code> <code>/e</code> <code>/eh</code> · <code>/help</code>"
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
    Always fetches pages 1..JSEARCH_PAGES_PER_RUN - no rolling offset, so no query can drift onto a
    deep page that times out, and none ever skips page 1 (its best matches) to fetch a worse one.
    Stops early on empty page, 429, or exhausted retries (see _fetch_jsearch_page_with_retry).
    Non-"Remote" queries are radius-limited (radius_miles filter, anchored to the location text in
    the query itself); "Remote" queries are capped to at most 1 result so nationwide remote postings
    don't crowd out the local metro-area focus.
    """
    query, api_url, headers = query_args
    is_remote_query = "remote" in query.lower()
    radius_miles = safe_int(get_filter("radius_miles"), 45)
    all_jobs = []
    for page in range(1, JSEARCH_PAGES_PER_RUN + 1):
        params = {
            "query": query,
            "page": str(page),
            "num_pages": "1",
            "date_posted": "month",
            # Without this JSearch answers a "... Auburn Hills MI" query with postings in Singapore,
            # Dubai, Warsaw and Gibraltar. They are all rejected downstream, but they consume the
            # page budget that local listings should have filled.
            "country": "us",
        }
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

    Withholding is per BATCH, not per row, and that is safe for the one case where a batch is
    partially written: Code.gs suppresses a row only when a LIVE row with the same normalized
    Company+Role already exists, so the job is already tracked and the card still resolves. Any
    other short write would need per-row withholding, which this cannot do - log_to_sheets_crm()
    returns a single bool for the batch. A batch that writes NOTHING still returns False and
    withholds everything.

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

        # A batch can succeed overall while THIS row was dropped as a duplicate. The batch-level
        # bool cannot express that, so the card used to ship carrying the uuid this run generated -
        # a uuid Code.gs never wrote to any tab. Every later /warm, /apply and /n on that card then
        # failed with "No record found" forever, because the row it points at does not exist.
        #
        # Code.gs now reports each row's fate, so a suppressed row's card is re-pointed at the
        # LIVE row that caused the suppression - the job really is tracked, and the card should
        # drive the row that is actually there.
        card_uuid = item.get("sheet_uuid")
        disposition = get_batch_disposition(card_uuid)
        if disposition and disposition["status"] == "duplicate_suppressed":
            existing = disposition.get("existing_uuid") or ""
            if existing:
                logging.warning(
                    f"Re-pointing card for {job.get('employer_name')} - {job.get('job_title')}: "
                    f"row was suppressed as a duplicate, card now targets the live row {existing}"
                )
                card_uuid = existing
                remap_cached_job_uuid(item.get("short_id"), existing)
            else:
                # Suppressed by a live row that itself has no uuid (a hand-added sheet row). There
                # is nothing for a swipe command to resolve against, so withhold rather than ship a
                # card whose every action will fail.
                logging.error(
                    f"Withholding card for {job.get('employer_name')} - {job.get('job_title')}: "
                    "suppressed as a duplicate and the existing row carries no uuid"
                )
                send_health_alert(
                    f"Card withheld for {job.get('employer_name')} - {job.get('job_title')}: the "
                    "role is already in a job tab but that row has no UUID, so swipe commands "
                    "could never resolve it. Add the row's UUID in column J to make it actionable."
                )
                continue

        send_telegram_card(
            job, item["score"], item["target_email"],
            item["age_badge"], item["salary_str"], item["work_style"],
            item["overlap_pct"], item["short_id"],
            sheet_uuid=card_uuid,
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


def ingest_manual_job(url="", title="", company="", description="", chat_id=None, source_label="/job", force=False):
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

    # Dedup against roles already TRACKED in a job tab, not against everything /t has ever
    # glanced at - re-pasting a link for a role with no CRM row should still produce a card.
    job_hash = generate_dedup_hash(job["employer_name"], job["job_title"])
    if force:
        logging.warning(
            f"Forced ingest (/job!) bypassing the tracked-role gate for "
            f"{job['employer_name']} - {job['job_title']}"
        )
    if not force and is_role_tracked(job["employer_name"], job["job_title"]):
        # Name the tab and row that caused the block. "Already in the pipeline" with nothing to
        # open is a dead end when the row has been deleted by hand: the suppression set is cached,
        # so it keeps answering from data that no longer matches the sheet, and Kevin has no way to
        # tell a real duplicate from a stale one.
        where = locate_tracked_role(job["employer_name"], job["job_title"])
        if where:
            detail = f"It is row {where['row_label']} of <b>{html.escape(where['tab'])}</b>"
            if where.get("status"):
                detail += f" (Status: {html.escape(where['status'])})"
            detail += "."
        else:
            detail = (
                "⚠️ But no matching row is in the sheet right now - the suppression list is stale "
                "(deleted by hand, or Sheets was unreachable at the last refresh)."
            )
        return (False, (
            f"♻️ <b>Already in the pipeline:</b> {html.escape(final_title)} @ {html.escape(final_company)}.\n"
            f"{detail}\n\n"
            "Re-ingest it anyway with <code>/job!</code> + the link, or refresh the list with "
            "<code>/resync</code>."
        ))

    log_metric_event("listing_discovered", source="manual_ingest")
    result = process_single_candidate(job, force=force)
    if not result:
        # Screening rejected it AND this was not a forced ingest, so there is no score, sheet_uuid
        # or resolved copy to write. Report the rejection rather than fabricating a partial record.
        # A forced ingest overrides the verdict inside process_single_candidate, so reaching here
        # with force=True means the evaluator itself failed (no Gemini key, API down), not that the
        # role was judged a poor fit.
        save_seen_job_db(job_hash)
        if force:
            return (False, (
                f"❌ <b>Could not score</b> {html.escape(final_title)} @ {html.escape(final_company)}.\n"
                "The forced ingest bypassed the fit check, but the evaluator itself failed "
                "(Gemini unreachable or no API key), so there is no card to build. Try again, or "
                f"add it with <code>/quick</code>."
            ))
        return (False, (
            f"⚠️ <b>Did not pass AI screening:</b> {html.escape(final_title)} @ {html.escape(final_company)}.\n"
            "No row written. Force a card anyway with <code>/job!</code> + the link, or add it "
            f"with <code>/quick</code>."
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
    seen_content_hashes_this_run = set()
    candidate_pool = []
    raw_discovered_count = 0
    funnel = FunnelTrace()
    # Fetched once per run, not per candidate: this is the cross-run suppression set (roles already
    # in Tetiana Cold/Warm) and it is the only memory allowed to block a rediscovered listing.
    tracked_job_keys = get_tracked_job_keys()

    def _add_candidate(job):
        nonlocal raw_discovered_count
        raw_discovered_count += 1
        funnel.raw += 1
        funnel._bump("raw")
        company = job.get("employer_name") or ""
        title = job.get("job_title") or ""
        job_hash = generate_dedup_hash(company, title)
        # Within ONE run, collapse the same role arriving from several queries/boards. This is pure
        # duplicate suppression and has no memory past the run.
        if job_hash in seen_hashes:
            funnel.note("dedup_title")
            return
        seen_hashes.add(job_hash)

        # ACROSS runs, the only thing that suppresses a role is already having a CRM row for it.
        # Deliberately NOT is_job_seen_db(): the seen_jobs ledger records every listing the pipeline
        # ever looked at, so a role glanced at during a run that dispatched no card was blocked
        # forever afterwards - 114 of 121 listings on a typical run, with almost nothing dispatched.
        # A rediscovered role Kevin never acted on is exactly the role he wants a card for.
        # Checked under both dedup algorithms - see get_tracked_job_keys() - so this gate cannot
        # pass a role that Code.gs's batch_add_rows guard would then refuse to write.
        if job_hash in tracked_job_keys or normalize_dedup_key(company, title) in tracked_job_keys:
            funnel.note("already_tracked")
            return

        # Fuzzy content dedup, in-run only for the same reason: catches one posting cross-listed
        # under reworded titles without remembering it past today. "" means the description was
        # missing or too short to identify a job - never dedup on that, or the first
        # description-less posting buries every later one (see compute_description_simhash).
        content_hash = compute_description_simhash(job.get("job_description"))
        if content_hash and content_hash in seen_content_hashes_this_run:
            funnel.note("dedup_content")
            return
        if content_hash:
            seen_content_hashes_this_run.add(content_hash)

        log_metric_event("listing_discovered", source=derive_job_source(job.get("job_id")))
        if not passes_strict_filter(job, trace=funnel):
            # Deliberately NOT marked seen. A rejected job is not a job Kevin has considered - it
            # failed today's filters, in today's posted state. Recording it here is what buried
            # hundreds of roles: a posting rejected once for a missing salary or a city not yet on
            # the allowlist could never be reconsidered, even after the filters changed or the
            # employer reposted it with better data. Only jobs that actually reach the candidate
            # pool are remembered, so the ledger means "seen and judged", not "glanced at once".
            return

        # seen_jobs is still written - the repost/evergreen penalty in score_job_layer1 reads
        # first_seen/seen_count to spot a listing that has been recycled for months. It is a
        # SIGNAL now, not a gate: nothing above consults it to block a candidate.
        save_seen_job_db(job_hash)
        funnel.passed += 1
        funnel._bump("passed")
        candidate_pool.append(job)
    
    # Stage 1: Parallel JSearch fetching (rolling 10-query slice) + strict filtering
    headers, api_url = build_jsearch_request_config()
    
    query_tasks = [(q, api_url, headers) for q in active_queries]
    
    with ThreadPoolExecutor(max_workers=min(len(active_queries), 8) or 4) as executor:
        # executor.map preserves input order, so zipping results back against active_queries
        # attributes each listing to the phrase that found it. Ingestion stays single-threaded
        # here, so setting funnel.current_query around each batch is safe.
        query_results = executor.map(fetch_single_query_jobs, query_tasks)
        for query, jobs in zip(active_queries, query_results):
            funnel.set_query(query)
            for job in jobs:
                _add_candidate(job)
        funnel.set_query(None)  # later stages (ATS, remote feeds) have no search phrase to credit

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
    # Logged rather than pushed to Telegram: this is a slow-moving question about the query bank,
    # not something to act on mid-run, and one line per query would drown the run card.
    yield_report = funnel.query_yield_report(limit=len(active_queries) or 10)
    if yield_report:
        logging.info("[QUERY YIELD] worst-performing first:\n" + yield_report)
        _record_query_yield(funnel.per_query)
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
        # Mirror _add_candidate: collapse duplicates within this run, and suppress across runs only
        # when the role is already tracked in a job tab - never merely because /t once saw it.
        if job_hash in seen_hashes or is_role_tracked(company, title):
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
        # Every command passes through here, so this is the one place usage can be counted
        # without touching 70 handlers. Pure telemetry - see record_command_usage().
        record_command_usage(text)

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
            # "/job!" overrides the tracked-role gate, for the case the gate gets wrong: the row was
            # deleted by hand, or Sheets was unreachable when the suppression set was last built.
            ing_force = bool(re.match(r"^/(job|j)!", text, re.IGNORECASE))
            send_telegram_message(
                chat_id,
                ("⏳ <b>Ingesting job (forced)...</b> skipping the duplicate check and the AI "
                 "screener - you get a card either way."
                 if ing_force else
                 "⏳ <b>Ingesting job...</b> scoring it through the same pipeline as /t "
                 "(Gemini fit, alumni lookup, warm routing).")
            )

            def _ingest_and_report(u=ing_url, t=ing_title, c=ing_company, cid=chat_id, force=ing_force):
                try:
                    ok, message = ingest_manual_job(url=u, title=t or "", company=c or "", chat_id=cid, source_label="/job", force=force)
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
                send_telegram_message(
                    chat_id,
                    f"❌ Invalid {cmd_token} format. Use: <code>{cmd_token} Name@Company [Priority 1-10] [Note]</code>\n"
                    f"<i>Company NAME, not an email address</i> - e.g. "
                    f"<code>{cmd_token} Dana Reed@Signal Advisors 7 ops lead</code>.\n"
                    f"To log someone you are emailing, use <code>/e</code> instead."
                )
                return
            name, company, priority, note = result
            sheet_uuid = str(uuid.uuid4())
            # Carmen tabs run the 4/11 (cold) or 4/11/21 (engaged) ladder, whose first rung is +4
            # days. calculate_followup_interval() is the JOBS priority-decayed model and waits 19
            # days at the default priority - a gap the ladder never writes, so plan_carmen_ladder()
            # reads the row as stalled and revives it instead of treating it as rung 1.
            next_followup = (datetime.now() + timedelta(days=CARMEN_LADDER_DAYS_COLD[0])).strftime("%Y-%m-%d")
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
        # NOT "/dead" - that is the swipe-reply that kills the replied-to row (see below).
        if text == "/usage" or text.startswith("/usage "):
            arg = text[len("/usage"):].strip().lower()
            windows = {"week": 7, "7": 7, "month": 30, "30": 30, "90": 90,
                       "quarter": 90, "all": None, "": 30}
            if arg not in windows:
                send_telegram_message(
                    chat_id,
                    "📊 <b>Command Usage</b>\n\nUsage: <code>/usage</code> (30d), "
                    "<code>/usage week</code>, <code>/usage 90</code>, <code>/usage all</code>"
                )
                return
            days = windows[arg]
            rows = get_command_usage(days=days)
            total, distinct, first_seen = get_command_usage_totals(days=days)
            label = "all time" if days is None else f"last {days} days"
            if not rows:
                send_telegram_message(
                    chat_id,
                    f"📊 <b>Command Usage</b> ({label})\n\nNothing recorded yet. Counting starts "
                    "from the deploy that added this, so history before then is not here."
                )
                return
            peak = rows[0][1] or 1
            lines = [f"📊 <b>Command Usage</b> ({label})\n",
                     f"<b>{total}</b> commands · <b>{distinct}</b> distinct\n"]
            for cmd, n, _last in rows:
                # Bar is proportional to the most-used command, so the shape reads at a glance.
                bar = "█" * max(1, round((n / peak) * 12))
                lines.append(f"<code>{n:>4}</code> {bar} {html.escape(cmd)}")
            if first_seen:
                lines.append(f"\n<i>Counting since {html.escape(str(first_seen)[:10])}.</i>")
            send_telegram_message(chat_id, "\n".join(lines))
            return

        if text == "/links" or text.startswith("/links "):
            # Same commit semantics as /unbury: "check" is a DRY RUN that writes nothing, "go"
            # is the one that actually moves rows. A sweep that retires rows the moment Kevin
            # types it gives him no way to see what it would do first.
            arg = text[len("/links"):].strip().lower()
            if arg in ("check", "dry", "preview", "go"):
                commit = (arg == "go")
                send_telegram_message(
                    chat_id,
                    ("🔗 Checking job links and RETIRING dead Matched rows..."
                     if commit else
                     "🔗 <b>Dry run</b> - checking job links, writing nothing...")
                )
                result = check_job_links(auto_retire=commit)
                would = [d for d in result["dead"] if may_auto_retire(d.get("status"))]
                if result["checked"] == 0:
                    # Zero checked is almost never "no jobs" - it means the CRM read came back
                    # empty, which a rejected webhook does silently. Say so instead of
                    # reporting it as a clean sweep.
                    probe = probe_crm_read()
                    send_telegram_message(
                        chat_id,
                        "⚠️ <b>Checked 0 links.</b> No rows came back from Tetiana Cold, Tetiana "
                        "Warm or Clavicular.\n\n<b>What the CRM actually answered:</b>\n"
                        f"<code>{html.escape(probe)}</code>"
                    )
                    return
                summary = (
                    f"🔗 Checked {result['checked']} links: <b>{len(result['dead'])}</b> dead, "
                    f"{result['unknown']} unknown.\n"
                )
                if commit:
                    summary += f"⚰️ Retired {len(result['retired'])} row(s) to Died."
                else:
                    summary += (
                        f"<b>Nothing was written.</b> A real run would retire "
                        f"<b>{len(would)}</b> row(s):\n"
                        + ("\n".join(
                            f"  • {html.escape(str(d['company']))} - {html.escape(str(d['role']))}"
                            for d in would[:10]) or "  (none)")
                        + "\n\nRun <code>/links go</code> to apply."
                    )
                send_telegram_message(chat_id, summary)
            rows = get_dead_job_links()
            if not rows:
                send_telegram_message(
                    chat_id,
                    "🔗 <b>Dead Job Links</b>\n\nNone recorded. The sweep runs nightly at 07:45; "
                    "preview one now with <code>/links check</code> (writes nothing)."
                )
                return
            lines = ["🔗 <b>Dead Job Links</b>\n"]
            for uuid_v, company, role, link, status, reason, retired, first_dead in rows:
                mark = "⚰️ retired" if retired else f"⚠️ still in {html.escape(str(status or '?'))}"
                lines.append(
                    f"<b>{html.escape(str(company or '?'))}</b> - {html.escape(str(role or '?'))}\n"
                    f"  {mark} · <i>{html.escape(str(reason or ''))}</i>\n"
                    f"  🆔 <code>{html.escape(str(uuid_v))}</code>"
                )
            lines.append(
                "\n⚰️ = auto-moved to Died (was still 'Matched').\n"
                "⚠️ = you applied, so nothing was moved. The posting is down, which means they "
                "stopped sourcing - not that you were rejected. Reply <code>/dead</code> to that "
                "job's card if you want it retired."
            )
            send_telegram_message(chat_id, "\n".join(lines))
            return

        if text == "/resync":
            invalidate_tracked_role_cache()
            keys = get_tracked_job_keys()
            if keys:
                send_telegram_message(
                    chat_id,
                    "🔄 <b>Suppression list refreshed</b>\n\n"
                    f"Re-read Tetiana Cold + Warm + Clavicular: <b>{len(keys) // 2}</b> role(s) "
                    "are currently tracked.\n\nA role you deleted by hand will now ingest normally."
                )
            else:
                send_telegram_message(
                    chat_id,
                    "🔄 <b>Suppression list cleared</b>\n\n"
                    "No tracked roles came back. Either the job tabs are empty, or Sheets did not "
                    "answer - either way nothing is being suppressed right now, so "
                    "<code>/job</code> will accept anything."
                )
            return

        if text == "/gaps" or text.startswith("/gaps "):
            gaps = get_jd_term_gaps(limit=20)
            if not gaps:
                with get_db_conn() as _c:
                    banked = _c.execute("SELECT COUNT(*) FROM jd_term_yield").fetchone()[0]
                send_telegram_message(
                    chat_id,
                    "🕳️ <b>Vocabulary Gaps</b>\n\n"
                    f"Nothing to report yet ({banked} terms banked). Every scored job adds its "
                    "vocabulary; a term needs to appear in 2+ postings before it shows here, so "
                    "this fills up over the next few <code>/t</code> runs."
                )
                return
            lines = ["🕳️ <b>Vocabulary Gaps</b>\n",
                     "Language high-fit postings use that your resume copy never says:\n"]
            for g in gaps:
                flag = " 🔥" if g["hi_fit_docs"] >= 3 else ""
                lines.append(
                    f"<code>{g['docs']:>3} JDs · {g['hi_fit_docs']:>2} hi-fit · avg {g['avg_fit']:>3}</code>{flag}\n"
                    f"  {html.escape(str(g['term']))}"
                )
            lines.append(
                f"\n🔥 = in 3+ postings scoring {HI_FIT_THRESHOLD}+.\n"
                "<code>/bullets &lt;track&gt;</code> drafts resume bullets from these."
            )
            send_telegram_message(chat_id, "\n".join(lines))
            return

        if text == "/bullets" or text.startswith("/bullets "):
            arg = text[len("/bullets"):].strip().lower()
            tracks = load_resume_bullet_tracks()
            if not arg:
                send_telegram_message(
                    chat_id,
                    "✍️ <b>Draft Resume Bullets</b>\n\nUsage: <code>/bullets &lt;track&gt;</code>\n\n"
                    "Tracks: " + ", ".join(f"<code>{t}</code>" for t in sorted(tracks)) +
                    "\n\nDrafts candidate bullets from the vocabulary gaps in <code>/gaps</code>. "
                    "Nothing is saved - you copy what is true and edit the rest."
                )
                return
            track_key = resolve_bullet_track_key(arg, tracks)
            if not track_key:
                send_telegram_message(
                    chat_id,
                    f"❌ Unknown track <code>{html.escape(arg)}</code>.\n\nTracks: "
                    + ", ".join(f"<code>{t}</code>" for t in sorted(tracks))
                )
                return
            gaps = get_jd_term_gaps(limit=12)
            if not gaps:
                send_telegram_message(
                    chat_id,
                    "🕳️ No vocabulary gaps banked yet - run a few <code>/t</code> cycles first, "
                    "then <code>/gaps</code> to see what the market is asking for."
                )
                return
            send_telegram_message(chat_id, f"✍️ Drafting bullets for <code>{track_key}</code>...")
            drafted = draft_bullets_for_gaps(track_key, tracks.get(track_key, []), gaps)
            if not drafted:
                send_telegram_message(
                    chat_id,
                    "❌ Draft failed (Gemini unavailable or returned nothing usable). "
                    "The gap list from <code>/gaps</code> is still the useful part - write from it by hand."
                )
                return
            lines = [f"✍️ <b>Draft Bullets - {html.escape(track_key)}</b>\n",
                     "⚠️ <b>Unverified.</b> These are phrasing suggestions built from market "
                     "vocabulary, NOT claims about your history. Keep only what you actually did.\n"]
            for i, b in enumerate(drafted, 1):
                lines.append(f"<b>{i}.</b> {html.escape(str(b.get('bullet', '')))}")
                covers = b.get("covers") or []
                if covers:
                    lines.append(f"   <i>covers: {html.escape(', '.join(str(c) for c in covers))}</i>")
            lines.append("\nNothing was saved. Edit what is true into <code>resume_bullets_bank.json</code>.")
            send_telegram_message(chat_id, "\n".join(lines))
            return

        if text == "/queries":
            rows = get_query_yield_rows(limit=25)
            if not rows:
                send_telegram_message(
                    chat_id,
                    "📊 <b>Query Yield</b>\n\nNo data yet. Each <code>/t</code> records the raw and "
                    "passed counts per search phrase; a phrase needs a few runs before its numbers "
                    "mean anything (the rolling slice fires each one roughly every 11 runs)."
                )
                return
            lines = ["📊 <b>Query Yield</b> (lifetime, worst first)\n"]
            for query_text, runs, raw_total, passed_total in rows:
                flag = " ⚠️" if runs >= 3 and passed_total == 0 else ""
                lines.append(
                    f"<code>{raw_total:>4} raw → {passed_total:>2} passed</code> "
                    f"({runs}r){flag}\n  {html.escape(str(query_text))}"
                )
            lines.append("\n⚠️ = 3+ runs, never produced a candidate.")
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

        # Outbox panic button. A payload whose row does not exist in the sheet can never succeed,
        # but process_crm_outbox_batch treats every failure as transient and re-dispatches it every
        # 5s until retry_count hits 10 - each pass firing its own "Failed to log payload" health
        # alert. /crazy is the manual stop: it drains the queue so the alert storm ends immediately,
        # and reports what it dropped so the underlying rows can be dealt with by hand.
        crazy_match = re.match(r"^/crazy(?:\s+(\S+))?$", text)
        if crazy_match:
            arg = (crazy_match.group(1) or "").strip().lower()
            try:
                with get_db_conn() as conn:
                    rows = conn.execute(
                        "SELECT id, payload_json, status, retry_count FROM crm_outbox ORDER BY id"
                    ).fetchall()
            except Exception as e:
                send_telegram_message(chat_id, f"⚠️ <b>/crazy failed to read the outbox:</b> {html.escape(str(e))}")
                return

            if not rows:
                send_telegram_message(chat_id, "✅ <b>Outbox is already empty.</b> Nothing to stop.")
                return

            # Summarize by action + sheet_uuid so a storm of identical retries reads as one line.
            tally = {}
            for _id, payload_str, _status, retries in rows:
                try:
                    payload = json.loads(payload_str)
                    key = (payload.get("action", "unknown"), str(payload.get("sheet_uuid") or "-")[:8])
                except Exception:
                    key = ("unparseable", "-")
                entry = tally.setdefault(key, {"n": 0, "max_retry": 0})
                entry["n"] += 1
                entry["max_retry"] = max(entry["max_retry"], safe_int(retries, 0))

            summary = "\n".join(
                f"· <code>{html.escape(action)}</code> {html.escape(uuid8)} "
                f"- {v['n']} queued, {v['max_retry']} retries"
                for (action, uuid8), v in sorted(tally.items())
            )

            # Dry run by default: deleting queued CRM writes is not reversible, so the destructive
            # form is opt-in via an explicit argument, exactly like /unbury go.
            if arg not in ("go", "force"):
                send_telegram_message(chat_id, (
                    f"🛑 <b>/crazy - {len(rows)} payload(s) stuck in the outbox</b>\n\n"
                    f"{summary}\n\n"
                    "These are being retried every 5s, and each failed pass fires a health alert.\n"
                    "Nothing was deleted. Run <code>/crazy go</code> to clear the queue and stop the alerts."
                ))
                return

            try:
                with get_db_conn() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    deleted = conn.execute("DELETE FROM crm_outbox").rowcount
                    conn.commit()
            except Exception as e:
                send_telegram_message(chat_id, f"⚠️ <b>/crazy could not clear the outbox:</b> {html.escape(str(e))}")
                return

            logging.warning(f"[/crazy] Operator cleared {deleted} queued CRM payload(s):\n{summary}")
            send_telegram_message(chat_id, (
                f"🛑 <b>Outbox cleared - {deleted} payload(s) dropped.</b>\n\n"
                f"{summary}\n\n"
                "The retry storm has stopped. These writes did NOT reach the sheet, so any status "
                "move or follow-up date they carried must be set by hand."
            ))
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
            # Supply comes from the local SQLite metrics, not the CRM payload above, so it renders
            # even when Sheets is the thing that is slow.
            funnel_lines.append(format_market_supply_message(get_market_supply()))
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

        if text == "/decoys":
            send_telegram_message(chat_id, format_decoy_metrics_message())
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

        # 9. Swipe-Reply CRM Actions (/f, /n, /apply, /dead, /warm, /cold, /x, /e) - require reply context
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
            is_warm = mapping.get("sheet_tab") in WARM_TONE_TABS
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
            # Same gate as stage_outreach_draft: the PDF still reaches Telegram, not the email.
            ok, gmail_msg, draft_id = create_gmail_draft(
                to_email=target, company_name=comp, job_title=title, is_warm=is_warm,
                custom_body=raw_email_text,
                pdf_bytes=pdf_bytes if RESUME_ATTACH_TO_EMAIL else None,
                pdf_filename=pdf_filename
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
            is_warm = mapping.get("sheet_tab") in WARM_TONE_TABS
            domain_hint = extract_domain_from_website(job.get("employer_website")) if job else None
            contact_name = custom_name or "Operations Lead"

            target = resolve_email_waterfall(contact_name, comp, domain_hint=domain_hint, on_provider_attempt=increment_api_usage_counter)
            confidence = "unverified" if is_unverified_email(target) else "verified"
            log_email_enrichment_attempt(mapping["sheet_uuid"], "waterfall", target, confidence)
            update_job_target_email(mapping["sheet_uuid"], target)
            enqueue_crm_payload(build_crm_payload("update_contact_email", sheet_uuid=mapping["sheet_uuid"], email=target))
            # Same reasoning as /e: an address resolved and drafted to here is one Kevin is
            # actively working, so it belongs in Carmen Cold regardless of company tracking.
            #
            # NOTE: unlike bare /e, this DOES persist an unverified waterfall guess - /eh spends
            # provider credits on a real lookup, so even a low-confidence hit is evidence rather
            # than a name-mangled guess, and the [⚠️ Unverified] tag rides along with it.
            log_addressed_contact_to_carmen_cold(
                target, company=comp, name=mapping.get("contact_name", ""),
                note=f"[{datetime.now().strftime('%Y-%m-%d')}] Emailed: {title}"
            )

            # Resume PDF, email body, Gmail draft, card and cover letter - shared with /e
            confidence_badge = "⚠️ Unverified guess" if confidence == "unverified" else "✅ Verified"
            stage_outreach_draft(
                chat_id, mapping, job, comp, title, is_warm, target,
                f"🔍 <b>API Lookup Resolved ({confidence_badge}):</b> <code>{html.escape(target)}</code>", "/eh"
            )
            return

        if text in ("/e", "/email") or text.startswith("/e ") or text.startswith("/email "):
            raw_email = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
            email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
            # The address is OPTIONAL. Bare /e resolves one the way /draft does, so the common case
            # - draft this, I do not have a name - is one keystroke instead of a lookup first. Only
            # a MALFORMED argument is an error: silently resolving a fallback after Kevin typed an
            # address would hide his typo behind a plausible-looking draft.
            typed_email = bool(raw_email)
            if typed_email and not re.match(email_pattern, raw_email):
                send_telegram_message(
                    chat_id,
                    "❌ Invalid email format. Use <code>/e name@company.com</code>, "
                    "or bare <code>/e</code> to resolve one automatically."
                )
                return
            mapping = resolve_reply_mapping(msg, chat_id, "/e")
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
            is_warm = mapping.get("sheet_tab") in WARM_TONE_TABS
            # Bare /e resolves the same way /draft does - no provider credits are spent (that is
            # /eh); resolve_target_email falls back to a role mailbox or a flagged best guess.
            new_email = raw_email if typed_email else resolve_target_email(
                comp, title, job.get("employer_website")
            )
            # A GUESSED address is not a contact. resolve_target_email() always returns something -
            # when it has no real domain it invents one from the company name and tags it
            # [⚠️ Fallback Email] - and writing that to the sheet filled the Contact Email column
            # with addresses nobody had verified, indistinguishable at a glance from ones Kevin
            # actually confirmed. A bare /e that resolves a guess now leaves the column BLANK; the
            # draft still goes out to the guess, because a draft needs a recipient and Kevin reads
            # it before sending.
            persist_email = typed_email or not is_unverified_email(new_email)
            if persist_email:
                update_job_target_email(mapping["sheet_uuid"], new_email)

            if typed_email:
                header = f"🎯 <b>Apollo Email Locked:</b> <code>{html.escape(new_email)}</code>"
            elif persist_email:
                header = f"✉️ <b>Drafted to:</b> <code>{html.escape(new_email)}</code> <i>(auto-resolved)</i>"
            else:
                header = (
                    f"✉️ <b>Drafted to:</b> <code>{html.escape(new_email)}</code>\n"
                    "<i>Guessed from the company name - NOT saved to the CRM. Send "
                    "<code>/e name@company.com</code> once you have a real address.</i>"
                )
            # Resume PDF, email body, Gmail draft and card - shared with /eh
            stage_outreach_draft(chat_id, mapping, job, comp, title, is_warm, new_email, header, "/e")
            if persist_email:
                enqueue_crm_payload(build_crm_payload("update_contact_email", sheet_uuid=mapping["sheet_uuid"], email=new_email))
            # Typing the address IS the intent to track this person, so log them to Carmen Cold
            # without the company gate the passive sweep uses - that gate drops agency recruiters
            # at untracked firms, which is most of who /e gets used on. A RESOLVED address carries
            # no such intent: it is a guess, often a role mailbox, and writing those into Carmen
            # Cold would fill the contact list with addresses Kevin never chose.
            if typed_email and log_addressed_contact_to_carmen_cold(
                new_email, company=comp, note=f"[{datetime.now().strftime('%Y-%m-%d')}] Emailed: {title}"
            ):
                send_telegram_message(chat_id, f"👤 Logged <code>{html.escape(new_email)}</code> to Carmen Cold.")
            return

        if text == "/fillcontacts" or text == "/fillcontacts go":
            # Join Carmen Cold's real people onto the job rows that still carry a resolved guess.
            # Previews by default for the same reason /backfillcontacts does: it writes to tabs
            # Kevin curates by hand, so the exact changes are shown before anything is committed.
            commit = text.endswith(" go")
            send_telegram_message(chat_id, "🔗 Matching Carmen Cold contacts to job rows...")

            def _fill_and_notify():
                try:
                    updates, skipped = backfill_job_contacts_from_carmen_cold(dry_run=not commit)
                    if not updates:
                        send_telegram_message(
                            chat_id,
                            "✅ <b>Nothing to fill.</b> Every job row either already has a real "
                            "contact or has no matching person in Carmen Cold."
                        )
                        return
                    lines = []
                    for u in updates[:20]:
                        old = u["old_email"] or "(blank)"
                        lines.append(
                            f"• <b>{html.escape(u['company'])}</b>"
                            + (f" - {html.escape(u['title'])}" if u["title"] else "")
                            + f"\n   <code>{html.escape(old)}</code> → "
                            f"<code>{html.escape(u['new_email'])}</code>"
                        )
                    alt_note = ""
                    multi = [u for u in updates if u["alternates"]]
                    if multi:
                        alt_note = "\n\n<i>Multiple contacts at: " + ", ".join(
                            html.escape(u["company"]) for u in multi[:5]
                        ) + " - took the first, the rest stay in Carmen Cold.</i>"
                    hdr = (f"✅ <b>Filled {len(updates)} job contact(s).</b>" if commit
                           else f"🔍 <b>Preview: {len(updates)} row(s) would change.</b>")
                    footer = "" if commit else "\n\nReply <code>/fillcontacts go</code> to write them."
                    send_telegram_message(chat_id, f"{hdr}\n\n" + "\n".join(lines) + alt_note + footer)
                except Exception as e:
                    logging.error(f"/fillcontacts Error: {e}")
                    send_telegram_message(chat_id, f"❌ Fill failed: {html.escape(str(e))}")

            threading.Thread(target=_fill_and_notify, daemon=True).start()
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

        if text == "/inbox":
            # The tray on demand. The counterpart to the daily card: the card tells Kevin what
            # changed, /inbox tells him what is still open.
            send_telegram_message(chat_id, format_inbound_tray_message(get_open_inbound_threads()))
            return

        if text.startswith("/done"):
            # Takes the thread id printed on the tray. Deliberately NOT a swipe-reply command:
            # the tray is a multi-entry list card, and swipe recovery takes the first id on the
            # card, which would close the wrong conversation.
            parts = text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                send_telegram_message(chat_id, (
                    "Usage: <code>/done &lt;thread id&gt;</code>\n"
                    "<i>The id is printed under each entry on /inbox.</i>"))
                return
            thread_id = parts[1].strip()
            if close_inbound_thread(thread_id):
                remaining = len(get_open_inbound_threads())
                send_telegram_message(chat_id, (
                    f"✅ Marked dealt with.\n<i>{remaining} conversation(s) still open.</i>"))
            else:
                send_telegram_message(chat_id, (
                    "🤷 <i>No open conversation with that id.</i>\n"
                    "Run /inbox for the current list."))
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
            # Shared with /e and /eh so the three commands cannot render different letters.
            letter, track = resolve_letter_for_job(job, mapping, comp)
            letter_msg = (
                f"✉️ <b>Cover Letter - {html.escape(comp)}</b> · Track {html.escape(str(track).upper())}\n\n"
                f"<code>{html.escape(letter)}</code>"
            )
            # Text lands instantly; the PDF compile follows on a thread so the webhook is not held open.
            send_telegram_message(chat_id, letter_msg)
            send_cover_letter_pdf_async(chat_id, letter, comp, track, "/letter")
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

                caption_text = (
                    f"📄 <b>Tailored Resume ({track.upper()}): {html.escape(comp)}</b>\n\n"
                    f"🖥️ <b>Desktop Staging Link:</b>\n"
                    f"<code>{html.escape(f'{BASE_URL}/stage/{short_id}?track={track}')}</code>"
                )
                send_telegram_document(chat_id, pdf_bytes, filename, caption_text, "/cv")
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

        if text == "/dead":
            # Decoy report: the posting was already gone when Kevin opened the card. Aggregators
            # resell expired inventory, so this exists to MEASURE which source does it (see
            # /decoys) - live URL-checking was tried and rejected, since Indeed answers 403 for a
            # dead posting and for its own home page alike and cannot tell them apart.
            mapping = resolve_reply_mapping(msg, chat_id, "/dead")
            if not mapping:
                return
            sheet_uuid = mapping["sheet_uuid"]
            job = get_job_by_sheet_uuid(sheet_uuid)
            company = mapping.get("contact_company") or job.get("employer_name")
            # Resolved before the write so a cache miss shows up as a missing age on the row rather
            # than as a silently absent column later.
            posted_hours = get_posted_hours_at_card(sheet_uuid)
            marked_date = datetime.now().strftime("%Y-%m-%d")
            reply_card = msg.get("reply_to_message") or {}
            if reply_card.get("message_id"):
                original_text = html.escape(reply_card.get("text", ""))
                edit_telegram_message(chat_id, reply_card["message_id"], f"{original_text}\n\n💀 <b>Dead link - {marked_date}</b>")
            # derive_job_source() defaults to "jsearch" for any id it does not recognise, which is
            # correct for a real jsearch posting and wrong for a card whose cache entry a Render
            # restart wiped - that would quietly inflate jsearch's decoy count with rows nothing
            # measured. No job_id, no source: the report buckets those as "unknown" instead.
            source = derive_job_source(job.get("job_id")) if job.get("job_id") else None
            age_note = f" · {posted_hours}h old when carded" if posted_hours is not None else " · age unknown"
            # Measurement only - no CRM write. /dead answers "was this listing real", which is a
            # different question from what Kevin wants the row to become; /x still kills it.
            send_telegram_message(chat_id, f"💀 <b>Marked dead</b> - {marked_date}{html.escape(age_note)}")
            record_application_outcome(
                sheet_uuid, "dead_link",
                company=company, role=job.get("job_title"),
                source=source, posted_hours=posted_hours
            )
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

        # /kill <company> <n|all> - resolve the pick card a multi-row rejection raised. A typed
        # command rather than a swipe because the card lists several rows and
        # _parse_sheet_uuid_from_card_text() takes the FIRST uuid it finds, so a swipe there would
        # always archive entry #1 whatever Kevin meant.
        kill_cmd_match = re.match(r"^/kill\s+(.+?)\s+(\d+|all)$", text, re.IGNORECASE)
        if kill_cmd_match:
            send_telegram_message(chat_id, resolve_pending_kill(
                kill_cmd_match.group(1).strip(), kill_cmd_match.group(2)))
            return
        if text.startswith("/kill"):
            with _PENDING_KILL_LOCK:
                open_picks = [v["company"] for v in PENDING_REJECTION_KILLS.values()]
            hint = ("\n<i>Waiting on:</i> " + ", ".join(html.escape(c) for c in open_picks)) if open_picks else \
                   "\n<i>Nothing is waiting on a pick right now.</i>"
            send_telegram_message(
                chat_id,
                f"⚠️ <b>Usage:</b> <code>/kill &lt;company&gt; &lt;number|all&gt;</code>{hint}")
            return

        # Canonical Status advance by short_id (no reply context): /replied <id>, /interview <id>.
        # Resolves the short_id to a sheet_uuid the same way callbacks do (get_sheet_uuid_by_short_id)
        # and writes only the Status field - never a tab move.
        #
        # /interview also takes the interview DATE: "/interview <id> 2026-09-25". Status alone
        # leaves Next Followup Date wherever the outreach ladder last set it, so a role Kevin is
        # actively interviewing for keeps its old anchor and the sequencer either nags mid-process
        # or - once the anchor is stale - says nothing at all. Passing the date re-anchors the row
        # to the day AFTER the call, which is when a thank-you or a status chase is actually due.
        status_cmd_match = re.match(r"^/(replied|interview)(?:\s+(\S+))?(?:\s+(\S+))?$", text)
        if status_cmd_match:
            cmd, short_id = status_cmd_match.group(1), (status_cmd_match.group(2) or "").strip()
            date_arg = (status_cmd_match.group(3) or "").strip()
            new_status = "Replied" if cmd == "replied" else "Interviewing"
            if not short_id:
                usage = (f"⚠️ <b>Usage:</b> <code>/{cmd} &lt;short_id&gt;</code>"
                         + (" <code>[YYYY-MM-DD]</code>" if cmd == "interview" else ""))
                send_telegram_message(chat_id, usage)
                return
            # The date is optional, so a bad one must never be swallowed: writing today's anchor
            # when Kevin typed "9/25" would silently schedule the wrong follow-up.
            interview_date = None
            if date_arg:
                if cmd != "interview":
                    send_telegram_message(chat_id, f"⚠️ <code>/{cmd}</code> takes no date. Use <code>/interview &lt;id&gt; YYYY-MM-DD</code>.")
                    return
                try:
                    interview_date = datetime.strptime(date_arg, "%Y-%m-%d")
                except ValueError:
                    send_telegram_message(
                        chat_id,
                        f"⚠️ <b>Bad date:</b> <code>{html.escape(date_arg)}</code> - use "
                        f"<code>YYYY-MM-DD</code>, e.g. <code>/interview {html.escape(short_id)} "
                        f"{(datetime.now() + timedelta(days=3)).strftime('%Y-%m-%d')}</code>."
                    )
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
            confirm = f"✅ {new_status} - {datetime.now().strftime('%Y-%m-%d')}"
            if interview_date:
                # +1 day: a follow-up due the morning OF the interview is noise, and one due that
                # evening is what Kevin actually wants to act on.
                follow_up = (interview_date + timedelta(days=1)).strftime("%Y-%m-%d")
                confirm += (f"\n📅 Interview: <b>{interview_date.strftime('%a %b %d, %Y').replace(' 0', ' ')}</b>"
                            f"\n🔔 Follow-up anchored to {follow_up}")
            send_telegram_message(chat_id, confirm)
            enqueue_crm_payload(build_crm_payload("set_status", sheet_uuid=sheet_uuid, status=new_status))
            if interview_date:
                enqueue_crm_payload(build_crm_payload(
                    "update_snooze", sheet_uuid=sheet_uuid,
                    next_followup=(interview_date + timedelta(days=1)).strftime("%Y-%m-%d")))
                enqueue_crm_payload(build_crm_payload(
                    "append_note", sheet_uuid=sheet_uuid,
                    note=f"[{datetime.now().strftime('%Y-%m-%d')}] Interview scheduled for "
                         f"{interview_date.strftime('%Y-%m-%d')} via /interview."))
            return

        # Carmen contact lifecycle (no reply context): /promote <id> moves a contact who replied from
        # Carmen Cold to Carmen Hot; /demote <id> parks a Cold or Hot contact on the Carmen Warm bench.
        #
        # The usual form is an EMAIL or a NAME - both visible in the sheet and in the reply Kevin
        # just read, unlike the UUID, which formatSheet() hides in Column J:
        #   /promote beth.young@altarum.org
        #   /promote Beth Young
        #
        # /promote also accepts a JOB 🆔 plus a name and email, for someone with no contact row yet:
        #   /promote <job_id> Beth Young beth.young@altarum.org
        # That form creates the contact straight into Carmen Hot and leaves the job row ALONE -
        # never a tab move, because transposeRowValues() would blank the name column and delete
        # the job from its pipeline tab.
        people_cmd_match = re.match(r"^/(promote|demote)(?:\s+(\S+))?(?:\s+(.+))?$", text)
        if people_cmd_match:
            cmd, token = people_cmd_match.group(1), (people_cmd_match.group(2) or "").strip()
            extra = (people_cmd_match.group(3) or "").strip()
            if not token:
                send_telegram_message(chat_id, f"⚠️ <b>Usage:</b> <code>/{cmd} &lt;id or name or email&gt;</code>")
                return
            # "Beth Young" arrives split across token and extra, so the WHOLE argument is tried as
            # a contact first. Checked before the single token so a full name beats a stray prefix
            # match, and before the job-card form so an existing contact is always moved, never
            # duplicated into a second row.
            full = f"{token} {extra}".strip() if extra else token
            matches = find_carmen_contacts(full) if extra else []
            if not matches:
                matches = find_carmen_contacts(token)
            if cmd == "promote" and not matches and extra:
                handled = promote_job_card_contact(chat_id, token, extra)
                if handled:
                    return
            if len(matches) != 1:
                shown = full if extra else token
                problem = "matches more than one contact" if matches else "is not a Carmen Cold or Carmen Hot contact"
                hint = ""
                # The id resolves to a JOB. Say so and name both commands, rather than letting
                # Kevin re-read a "not a contact" message that is true but unactionable.
                if not matches and (get_job_from_cache(token) or get_job_by_sheet_uuid(token)):
                    hint = (f"\n\n<i>That 🆔 is a JOB, not a contact.</i>\n"
                            f"• <code>/interview {html.escape(token)}</code> - mark the role Interviewing\n"
                            f"• <code>/promote {html.escape(token)} Name name@company.com</code> - add the person who replied")
                send_telegram_message(
                    chat_id, f"⚠️ <b>Not moved:</b> <code>{html.escape(shown)}</code> {problem}. "
                             f"Try the contact's email address, their full name, or the 🆔.{hint}"
                )
                return
            sheet_uuid, source_tab, record = matches[0]
            target_tab, allowed = (("Carmen Hot", ("Carmen Cold",)) if cmd == "promote"
                                   else ("Carmen Warm", ("Carmen Cold", "Carmen Hot")))
            who = html.escape(str(record.get("name") or record.get("company") or token))
            if source_tab == target_tab:
                send_telegram_message(chat_id, f"ℹ️ {who} is already in {target_tab}.")
                return
            if source_tab not in allowed:
                send_telegram_message(chat_id, f"⚠️ <b>Not moved:</b> /{cmd} works on {' or '.join(allowed)} contacts; {who} is in {html.escape(source_tab)}.")
                return
            today_str = datetime.now().strftime("%Y-%m-%d")
            verb = "Promoted" if cmd == "promote" else "Demoted"
            send_telegram_message(chat_id, f"{'🔥' if cmd == 'promote' else '🪑'} {verb} {who} to {target_tab}.")
            enqueue_crm_payload(build_crm_payload("update_status", sheet_uuid=sheet_uuid, new_tab=target_tab))
            enqueue_crm_payload(build_crm_payload(
                "append_note", sheet_uuid=sheet_uuid,
                note=f"[{today_str}] {verb} from {source_tab} to {target_tab} via /{cmd}.",
            ))
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
                "/interview &lt;id&gt; [YYYY-MM-DD] - Set Status to Interviewing (date anchors the follow-up)\n"
                "/promote &lt;email | name | id&gt; - Move a contact who replied to Carmen Hot\n"
                "/promote &lt;job id&gt; Name email - Add the person who replied, from a job card\n"
                "/demote &lt;id&gt; - Park a Carmen Cold/Hot contact on the Warm bench\n"
                "/offer - Log an offer for this record\n"
                "/withdraw - Log a withdrawn application\n"
                "/warm - Smart-route lead to its Warm tab\n"
                "/cold - Smart-route lead to its Cold tab\n"
                "/x - Archive lead to Died/Killed tab\n"
                "/kill &lt;company&gt; &lt;n|all&gt; - Archive the role a rejection named (pick card)\n"
                "/dead - Mark the posting itself expired (decoy) - feeds /decoys\n"
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
                "/inbox - Open conversations that still need a reply\n"
                "/done &lt;id&gt; - Mark a conversation dealt with (id is printed on /inbox)\n"
                "/backfillcontacts - Preview a full Sent-history contact sweep (add 'go' to write)\n\n"
                "<b>TUESDAY BATCH HUB:</b>\n"
                "/sendall - Draft bumps + queue eligible overdue records to +14 days\n"
                "/snoozeall [days] - Move every overdue follow-up by 7 days (or the specified number)\n"
                "/overdue - Full overdue list (the morning digest shows only the 10 most overdue)\n\n"
                "<b>TELEMETRY:</b>\n"
                "/health - View system telemetry and status\n"
                "/efficiency - View Input to Interview Golden Ratio\n"
                "/funnel - View pipeline conversion funnel\n"
                "/unbury - Preview buried-listing cleanup (add 'go' to clear)\n"
                "/crazy - Stop a CRM retry/alert storm (add 'go' to clear the outbox)\n"
                "/queries - Per-query yield: which search phrases earn their slot\n"
                "/links - Dead job postings · <code>/links check</code> dry run · <code>/links go</code> retires\n"
                "/usage - How often you use each command (week/month/90/all)\n"
                "/resync - Re-read the job tabs after deleting rows by hand\n"
                "/job! &lt;url&gt; - Force a card: skips the duplicate check AND the AI screener\n"
                "/gaps - Vocabulary high-fit postings use that your resume doesn't\n"
                "/bullets &lt;track&gt; - Draft resume bullets from those gaps (saves nothing)\n"
                "/queue - Preview what the nightly follow-up sequencer would do (read-only)\n"
                "/outcomes - View evidence-based reply/interview rates by source & path\n"
                "/decoys - Dead-link (expired posting) rate per source\n"
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
    start_job_link_checker()

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

def _followups_page(title, body_html):
    """Wrap /followups content in the /stage page's shell: same CSS, same copyField() script."""
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{html.escape(title)}</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 40px; background: #f8f9fa; color: #212529; }}
            .card {{ background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.08); max-width: 750px; margin: auto; }}
            h2 {{ color: #1B2A4A; margin-top: 0; }}
            .btn {{ display: inline-block; padding: 10px 18px; margin-right: 10px; border-radius: 6px; text-decoration: none; font-weight: bold; }}
            .btn-primary {{ background: #1B2A4A; color: white; }}
            .btn-secondary {{ background: #e9ecef; color: #333; margin-bottom: 8px; }}
            .meta {{ color: #444; line-height: 1.5; }}
            textarea {{ box-sizing: border-box; border: 1px solid #ddd; border-radius: 4px; padding: 10px; margin-top: 8px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
            th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #e9ecef; vertical-align: top; }}
        </style>
    </head>
    <body>
        <div class="card">
            {body_html}
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

@app.route("/followups", methods=["GET"])
def followup_queue_view():
    """The morning card's "Open Follow-up Queue" target: full draft text, Copy buttons and Gmail
    links for each person to nudge, plus the applications going quiet.

    Renders today's saved sequencer result and never recomputes: the 7:30 run's snoozes have
    already pushed these rows into the future, so a recompute would find nothing due. This page
    writes nothing; its "Open in Gmail" links go to followup_draft_on_demand(), which does.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    title = f"Follow-up Queue · {today_str}"
    result = load_followup_queue_snapshot(today_str)
    if result is None:
        return _followups_page(title, (
            f"<h2>{html.escape(title)}</h2>"
            "<p class='meta'>No queue for today yet — the sequencer runs at 7:30.</p>"
        )), 200

    ready = result.get("followups_ready") or []
    quiet = result.get("applications_quiet") or []
    if not ready and not quiet:
        return _followups_page(title, (
            f"<h2>{html.escape(title)}</h2>"
            "<p class='meta'>✅ Queue is clear — nobody to nudge and no applications going quiet.</p>"
        )), 200

    parts = [f"<h2>{html.escape(title)}</h2>"]
    if ready:
        parts.append(f"<h3 style='margin-top: 24px;'>✉️ Nudge These People ({len(ready)})</h3>")
        for i, e in enumerate(ready):
            who = str(e.get("role") or e.get("name") or "—")
            company = str(e.get("company") or "—")
            due = _followup_date_label(e.get("next_followup"))
            step = _next_step_label(e)
            field_id = f"draft-{i}"
            links = (
                f'<button class="btn btn-secondary" onclick="copyField(\'{field_id}\')" '
                f'style="border: none; cursor: pointer;">📋 Copy Draft</button>'
            )
            no_address_note = ""
            if e.get("sheet_uuid") and _sequencer_draft_recipient(e):
                open_url = html.escape(f"/followups/draft/{urllib.parse.quote(str(e['sheet_uuid']), safe='')}", quote=True)
                links += f'<a class="btn btn-primary" href="{open_url}" target="_blank">✉️ Open in Gmail</a>'
            else:
                no_address_note = "<p class='meta'><i>No verified address on file - copy the draft and send it by hand.</i></p>"
            parts.append(
                f"<h3 style='margin-top: 24px;'>{html.escape(who)} — {html.escape(company)}</h3>"
                f"<p class='meta'>Follow-up #{html.escape(str(e.get('attempt', 1)))} · "
                f"due {html.escape(due)} → {html.escape(step)} · "
                f"🆔 <code>{html.escape(_seq_id_tag(e))}</code></p>"
                f'<textarea id="{field_id}" rows="10" style="width: 100%;" readonly>'
                f"{html.escape(str(e.get('draft_text') or ''))}</textarea>"
                f'<div style="margin-top: 10px;">{links}</div>'
                f"{no_address_note}"
            )

    if quiet:
        rows_html = []
        for e in quiet:
            days = e.get("days_silent")
            stage = ""
            if e.get("short_id"):
                stage_url = html.escape(f"/stage/{e['short_id']}", quote=True)
                stage = f'<a href="{stage_url}" target="_blank">📋 Full Card</a>'
            rows_html.append(
                "<tr>"
                f"<td>{html.escape(str(e.get('company') or '—'))}</td>"
                f"<td>{html.escape(str(e.get('role') or '—'))}</td>"
                f"<td>{html.escape(_followup_date_label(e.get('date_added')))}</td>"
                f"<td>{_silence_dot(days)} {html.escape(str(days) if isinstance(days, int) else '?')}d</td>"
                f"<td>{html.escape(_followup_date_label(e.get('buries_on')))}</td>"
                f"<td>{stage}</td>"
                "</tr>"
            )
        parts.append(
            f"<h3 style='margin-top: 32px;'>👀 Applications Going Quiet ({len(quiet)})</h3>"
            "<p class='meta'>Watch only - no drafts. Each is auto-buried to Died on its buries-on date "
            "unless its status moves.</p>"
            "<table><tr><th>Company</th><th>Role</th><th>Applied</th><th>Silent</th>"
            "<th>Buries on</th><th></th></tr>"
            + "".join(rows_html) + "</table>"
        )
    return _followups_page(title, "".join(parts)), 200

def _followup_copy_page(title, reason, draft_text, status):
    """The fallback for an on-demand draft that could not be created: the reason, and the text to
    copy by hand. Always a real page, never a bare error."""
    return _followups_page(title, (
        f"<h2>{html.escape(title)}</h2>"
        f"<p class='meta'>⚠️ {html.escape(reason)}</p>"
        f'<textarea id="draft-0" rows="12" style="width: 100%;" readonly>{html.escape(draft_text)}</textarea>'
        '<div style="margin-top: 10px;"><button class="btn btn-secondary" onclick="copyField(\'draft-0\')" '
        'style="border: none; cursor: pointer;">📋 Copy Draft</button>'
        '<a class="btn btn-secondary" href="/followups">← Back to queue</a></div>'
    )), status

@app.route("/followups/draft/<sheet_uuid>", methods=["GET"])
def followup_draft_on_demand(sheet_uuid):
    """Create one follow-up's Gmail draft when Kevin clicks "Open in Gmail", then redirect to it.

    A GET that writes, deliberately, so it works as a plain link. It is bounded three ways: it only
    drafts an entry in TODAY's saved snapshot (never recomputes), a blank or bracketed address is
    refused before anything reaches Gmail, and a repeat click finds the draft the first click made
    and redirects to it rather than creating another. It never sends.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    result = load_followup_queue_snapshot(today_str) or {}
    entry = next((e for e in result.get("followups_ready") or []
                  if e.get("sheet_uuid") and e.get("sheet_uuid") == sheet_uuid), None)
    if entry is None:
        return _followups_page("Follow-up not found", (
            "<h2>Follow-up not found</h2>"
            "<p class='meta'>That follow-up is not in today's queue. Links only work on the day "
            "the 7:30 sequencer listed them.</p>"
            '<a class="btn btn-secondary" href="/followups">← Back to queue</a>'
        )), 404

    who = str(entry.get("name") or entry.get("role") or entry.get("company") or "this follow-up")
    title = f"Follow-up draft · {who}"
    draft_text = str(entry.get("draft_text") or "")
    record = {
        "sheet_uuid": sheet_uuid,
        "email": entry.get("email"),
        "company": entry.get("company_raw", entry.get("company")),
        "title": entry.get("role") or "",
    }
    email = _sequencer_draft_recipient(record)
    if not email:
        return _followup_copy_page(title, "No verified address on file - copy the draft and send it by hand.",
                                   draft_text, 200)

    # A repeat click is answered here, before create_gmail_draft(): its own duplicate path would
    # also return the id, but it pings Telegram with "Draft Already Exists", which is noise from a
    # browser click.
    existing = check_existing_gmail_draft(email, _sequencer_draft_subject(record))
    draft_id = existing["draft_id"] if existing and existing.get("draft_id") else None
    reason = ""
    if not draft_id:
        # Branch on draft_id, not on success: a duplicate comes back as ok=False with a real id.
        draft_id, _created, reason = _stage_sequencer_draft(record, draft_text)
    if draft_id:
        return redirect(f"https://mail.google.com/mail/u/0/#drafts/{urllib.parse.quote(str(draft_id), safe='')}", 302)
    return _followup_copy_page(title, f"Gmail draft not created: {reason}. Copy the text below instead.",
                               draft_text, 502)

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

# ==============================================================================
# 11. PUBLIC AGGREGATE STATS (read-only, counts only)
# ==============================================================================
# The portfolio site cites real pipeline numbers; this endpoint is what makes them
# checkable by a stranger. It serves aggregate integers only - no row, no company, no
# role, no person, no email address - so it is safe to leave unauthenticated.
#
# Source of truth is the Sheets CRM (the funnel_stats GET action), not local SQLite:
# the Sheet is where a status actually changes, and on a host restart SQLite can be
# behind it. Results are cached for PUBLIC_STATS_TTL_SECONDS so a page refresh does not
# spend an Apps Script call.
#
# It never invents a number. If the CRM is unreachable and nothing is cached it returns
# 503, not zeros: a page rendering "0 applications" from a failed fetch is
# indistinguishable from one that made the number up, which is the failure this whole
# endpoint exists to prevent.

# The day the engine started logging outreach (first commit). days_running counts from here.
PUBLIC_STATS_START_DATE = "2026-07-31"
PUBLIC_STATS_TTL_SECONDS = int(os.environ.get("PUBLIC_STATS_TTL_SECONDS", "900"))
_public_stats_cache = {"fetched_at": 0.0, "payload": None}
_public_stats_lock = threading.Lock()


def _public_stats_days_running(now=None):
    """Whole days from PUBLIC_STATS_START_DATE to today, floored at 0."""
    now = now or datetime.now(timezone.utc)
    start = datetime.strptime(PUBLIC_STATS_START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return max(0, (now - start).days)


def build_public_stats(now=None):
    """Aggregate the CRM funnel into public counts, or return None if the CRM is unreachable.

    Bucket semantics matter here, because funnel_stats reports each row's CURRENT status, not
    its history. A row sitting in Interviewing was necessarily applied to and necessarily
    replied to, so each count rolls up every stage at or past it:

      applications_logged - every row past Matched (Matched means sourced but not yet applied)
      replies             - rows that drew a human response that moved them forward
      interviews          - Screening, Interviewing and Offer (a recruiter screen is an interview)

    Rejected is reported on its own line rather than folded into replies. A rejection is
    terminal, and the bucket cannot tell an auto-reject from a post-interview no - counting
    those as "replies" would inflate the reply rate in exactly the direction that flatters.
    """
    res = crm_get({"action": "funnel_stats"}, timeout=15)
    if res is None or res.status_code != 200:
        return None
    try:
        data = res.json()
    except Exception as e:
        logging.error(f"Public stats: funnel_stats returned non-JSON: {e}")
        return None
    if data.get("status") != "success":
        logging.error(f"Public stats: funnel_stats error: {data.get('message')}")
        return None

    overall = data.get("overall") or {}
    if not isinstance(overall, dict) or not overall:
        return None

    def n(key):
        try:
            return int(overall.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    matched = n("Matched")
    applied = n("Applied")
    replied = n("Replied")
    screening = n("Screening")
    interviewing = n("Interviewing")
    offer = n("Offer")
    rejected = n("Rejected")

    interviews = screening + interviewing + offer
    replies = replied + interviews
    applications_logged = applied + replies + rejected

    return {
        "applications_logged": applications_logged,
        "replies": replies,
        "interviews": interviews,
        "rejections": rejected,
        "still_sourcing": matched,
        "days_running": _public_stats_days_running(now),
        "start_date": PUBLIC_STATS_START_DATE,
        "as_of": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d"),
    }


@app.route("/public/stats", methods=["GET"])
def public_stats():
    """Public, unauthenticated, counts-only pipeline aggregate. See section 11's header."""
    now = time.time()
    with _public_stats_lock:
        cached = _public_stats_cache["payload"]
        fresh = cached is not None and (now - _public_stats_cache["fetched_at"]) < PUBLIC_STATS_TTL_SECONDS
    if fresh:
        return jsonify(dict(cached, status="ok", cached=True)), 200

    payload = build_public_stats()
    if payload is None:
        # Serve a stale cache before serving nothing, but say that it is stale.
        with _public_stats_lock:
            cached = _public_stats_cache["payload"]
        if cached is not None:
            return jsonify(dict(cached, status="ok", cached=True, stale=True)), 200
        return jsonify({"status": "unavailable", "message": "CRM unreachable; no counts to report"}), 503

    with _public_stats_lock:
        _public_stats_cache["payload"] = payload
        _public_stats_cache["fetched_at"] = now
    return jsonify(dict(payload, status="ok", cached=False)), 200


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
