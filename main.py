import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import html
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
import requests
from flask import Flask, jsonify, request, Response
from apscheduler.schedulers.background import BackgroundScheduler
from resume_engine import compile_resume_pdf, filter_ats_bullets, TRACK_BULLET_POOL_KEYS
from pipeline_utils import (
    build_apollo_url, build_linkedin_url, build_hiring_manager_dork, build_recruiter_dork,
    build_alumni_dork, normalize_priority_value, calculate_followup_interval,
    resolve_smart_target_tab, enforce_sentence_limit, get_fit_score_indicator,
    generate_dedup_hash, generate_short_key, parse_posted_hours, get_age_badge,
    extract_salary, extract_work_style, compute_description_simhash, resolve_email_waterfall,
    derive_job_source, is_unverified_email
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
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN")
GMAIL_USER = os.environ.get("GMAIL_USER")
JSEARCH_URL = "https://api.openwebninja.com/jsearch/search"
JSEARCH_TIMEOUT_SECONDS = 8
JSEARCH_MAX_RETRIES = 2  # additional attempts beyond the first, on timeout/429/5xx

def build_jsearch_request_config():
    """Prioritizes OPENWEBNINJA_KEY over RAPIDAPI_KEY when both are set."""
    openweb_key = os.environ.get("OPENWEBNINJA_KEY")
    rapidapi_key = os.environ.get("RAPIDAPI_KEY")
    if openweb_key:
        return {"x-api-key": openweb_key}, JSEARCH_URL
    if rapidapi_key:
        return {"X-RapidAPI-Key": rapidapi_key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}, "https://jsearch.p.rapidapi.com/search"
    return {}, JSEARCH_URL
DB_PATH = os.environ.get("JOBS_DB_PATH", "jobs_cache.db")  # override lets tests isolate their own SQLite file
EVIDENCE_BANK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence_bank.json")

def crm_get(params, timeout=4):
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

def crm_post(payload, timeout=4):
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

_FALLBACK_OUTREACH_TEMPLATES = {
    "cold_ops": ["Hi {name},\n\nI saw the {job_title} role at {company} and wanted to reach out. Would you be open to a brief call?\n\nBest regards,\nKevin Miller"],
    "warm_alumni": ["Hi {name},\n\nHope you have been doing well. Would love to reconnect.\n\nBest regards,\nKevin Miller"],
    "followup_bumps": ["Hi {name},\n\nBumping this briefly to the top of your inbox.\n\nBest regards,\nKevin Miller"]
}
_FALLBACK_LINKEDIN_TEMPLATES = {
    "linkedin_templates": ["Hi {name}. I saw the {job_title} opening at {company} and wanted to connect."]
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

def resolve_template_text(pool, idx, fallback_text=""):
    """Bounds-checks an integer template index against a template pool, defaulting to index 0
    (or a supplied fallback string) if the pool is empty or the index is missing/out-of-range.
    """
    if not isinstance(pool, list) or not pool:
        return fallback_text
    if not isinstance(idx, int) or idx < 0 or idx >= len(pool):
        idx = 0
    return pool[idx]

def interpolate_template(template, name="there", company="", job_title=""):
    """Deterministically fills {name}/{company}/{job_title} placeholders via str.format() - the
    only place candidate-facing outreach/LinkedIn copy is ever assembled. Never calls Gemini."""
    try:
        return template.format(name=name or "there", company=company or "your team", job_title=job_title or "this role")
    except Exception as e:
        logging.error(f"Template interpolation failed: {e}")
        return template

RESUME_BULLETS_BANK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resume_bullets_bank.json")

EDIT_ID_PATTERN = re.compile(r"^(L|C|W|B|T[A-E])(\d+)$", re.IGNORECASE)

def resolve_edit_target(id_str):
    """Maps a /edit ID to (file_path, list_key, index):
      L0-L5 -> templates/linkedin_templates.json[linkedin_templates]
      C0-C2 -> templates/outreach_templates.json[cold_ops]
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

def update_template_entry(file_path, list_key, idx, new_text):
    """Atomically loads a template JSON bank, overwrites the string at (list_key, idx), and
    writes it back to disk via a temp-file + os.replace swap (crash-safe, no partial writes on
    disk). Returns (ok: bool, message: str) - message is a ready-to-send Telegram HTML string.
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

    return True, (
        f"✅ <b>Template Updated:</b> <code>{html.escape(str(list_key))}[{idx}]</code>\n\n"
        f"<code>{html.escape(new_text)}</code>"
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

def get_db_conn():
    """Returns a SQLite connection tuned for concurrent writers: WAL + NORMAL sync + busy_timeout."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn

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
        # Migration guard: upgrade pre-existing seen_jobs tables missing the new tracking columns
        for col_def in ["first_seen TIMESTAMP", "last_seen TIMESTAMP", "seen_count INTEGER DEFAULT 1"]:
            try:
                conn.execute(f"ALTER TABLE seen_jobs ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass
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
        # Migration guard: pipeline_metrics pre-dates the source-attribution column
        try:
            conn.execute("ALTER TABLE pipeline_metrics ADD COLUMN source TEXT")
        except sqlite3.OperationalError:
            pass
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        # Migration guard: add column if table pre-dates this field
        try:
            conn.execute("ALTER TABLE sheet_row_map ADD COLUMN telegram_message_id INTEGER")
        except sqlite3.OperationalError:
            pass
        # Migration guard: contact_email powers the strict CRM whitelist gatekeeper for inbound mail
        try:
            conn.execute("ALTER TABLE sheet_row_map ADD COLUMN contact_email TEXT")
        except sqlite3.OperationalError:
            pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sheet_row_map_tg_msg ON sheet_row_map(telegram_message_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sheet_row_map_contact_email ON sheet_row_map(contact_email)")
        
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM search_filters")
        if cursor.fetchone()[0] == 0:
            defaults = {
                "min_salary": 50000,
                "experience_salary_floor": 60000,
                "radius_miles": 35,
                "valid_cities": [
                    "farmington", "detroit", "ann arbor", "novi", "troy", "southfield",
                    "auburn hills", "plymouth", "royal oak", "livonia", "dearborn",
                    "birmingham", "bloomfield", "warren", "sterling heights", "canton",
                    "rochester", "wixom", "madison heights"
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
                "target_queries": [
                    "Wealth Operations Farmington MI",
                    "Fintech Operations Farmington MI",
                    "Business Operations Analyst Farmington MI",
                    "Custodial Operations Schwab Fidelity Farmington MI",
                    "Financial Systems Process Automation Farmington MI",
                    "Operations Specialist Farmington MI",
                    "Financial Operations Analyst Remote",
                    "Business Systems Analyst Farmington MI",
                    "Risk Operations Analyst Remote",
                    "Client Operations Associate Farmington MI",
                    "Business Intelligence Analyst Farmington MI",
                    "Trade Operations Analyst Remote",
                    "Compliance Operations Specialist Farmington MI",
                    "Financial Analyst Operations Farmington MI",
                    "Treasury Operations Analyst Remote",
                    "Salesforce Administrator Farmington MI",
                    "Client Success Operations Remote",
                    "Data Operations Analyst Remote",
                    "Process Improvement Analyst Farmington MI",
                    "Onboarding Specialist Farmington MI"
                ]
            }
            for k, v in defaults.items():
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
        conn.commit()

    hydrate_filters_from_sheets()

def hydrate_filters_from_sheets():
    """On startup, pull load_system_config from Sheets so local filters reflect any manual spreadsheet edits."""
    res = crm_get({"action": "load_system_config"})
    if not res or res.status_code != 200:
        return
    try:
        remote_filters = res.json().get("filters", {})
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for key, val in remote_filters.items():
                conn.execute("INSERT OR REPLACE INTO search_filters (key, value_json) VALUES (?, ?)", (key, json.dumps(val)))
            conn.commit()
        logging.info(f"Hydrated {len(remote_filters)} filters from Google Sheets System_Config")
    except Exception as e:
        logging.error(f"Filter Hydration Error: {e}")

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
    (e.g. "hunter", "anymail"). This is a local approximation for /health visibility only -
    the provider's own dashboard is the authoritative quota source.
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
    """Return {"hunter": n, "anymail": n} local call counts for the current calendar month."""
    month_key = datetime.now().strftime("%Y-%m")
    counts = {"hunter": 0, "anymail": 0}
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

def resolve_reply_mapping(msg, chat_id, command_label):
    """For swipe-reply commands, resolve reply_to_message -> sheet_uuid mapping.
    Sends a Telegram warning and returns None if reply context or mapping is missing.
    """
    reply_msg = msg.get("reply_to_message")
    if not reply_msg:
        send_telegram_message(chat_id, "⚠️ <b>Context Missing:</b> Please swipe-reply directly to a job card or contact card to use this command.")
        return None
    mapping = get_mapping_from_message_id(reply_msg.get("message_id"))
    if not mapping:
        send_telegram_message(chat_id, f"⚠️ <b>Record Not Found:</b> No CRM record is mapped to this card for <code>{html.escape(command_label)}</code>. Please retry with /t or /c to regenerate it.")
        return None
    return mapping

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
    """Returns (core_exp_phrase, full_sentence) for the most recent Evidence Bank experience entry,
    so outreach copy never drifts from the same facts the resume renders. Hot-reloads the bank on
    every call.
    """
    experience = load_evidence_bank().get("experience", [])
    if not experience:
        return "wealth ops and process automation", "I am currently working in wealth operations and process automation."
    current = experience[0]
    title = current.get("title", "")
    company = current.get("company", "")
    location = current.get("location", "")
    core_exp = f"{title.lower()} and process automation" if title else "wealth ops and process automation"
    sentence = f"I am currently working as a {title} at {company}" + (f" in {location}" if location else "") + "."
    return core_exp, sentence

def generate_cold_email(job_title, company_name, core_exp=None):
    """Full cold email: greeting, strict 2-sentence body, sign-off as separate paragraphs."""
    if not core_exp:
        core_exp, _ = get_current_role_blurb()
    s1 = f"I saw the {job_title} role at {company_name} and wanted to highlight my background in {core_exp}."
    s2 = "Would you be open to a brief 5 minute call next week to discuss alignment?"
    body = enforce_sentence_limit(f"{sanitize_text(s1)} {sanitize_text(s2)}", 2)
    return f"Hi,\n\n{body}\n\nBest regards,\nKevin Miller"

def generate_warm_email(note_context=""):
    """Full warm email: greeting, strict 3-sentence body, sign-off as separate paragraphs."""
    _, current_role_sentence = get_current_role_blurb()
    s1 = sanitize_text(note_context) if note_context else "I hope you have been doing well."
    s2 = sanitize_text(current_role_sentence)
    s3 = sanitize_text("I am wondering what you have been up to lately, and would love to reconnect over coffee or a quick call if you have time.")
    body = enforce_sentence_limit(f"{s1} {s2} {s3}", 3)
    return f"Hi,\n\n{body}\n\nBest regards,\nKevin Miller"

def generate_bump_email(contact_name=""):
    """Short follow-up nudge for threads that went unanswered."""
    name_str = f" {contact_name}" if contact_name else ""
    return f"Hi{name_str},\n\nBumping this briefly to the top of your inbox in case it got buried. Would love to connect if you have 5 minutes this week to discuss alignment.\n\nBest regards,\nKevin Miller"

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

EVIDENCE BANK (the only source of truth for this candidate's real background):
{evidence_block}

NEGATIVE CONSTRAINTS: You must strictly use facts from the Evidence Bank above. Never invent skills, employers, or experiences not listed there. You are STRICTLY a classifier/router - NEVER generate prose, sentences, resume bullets, email bodies, or LinkedIn notes yourself. Only return integer indices selecting from pre-approved local template banks; all actual text is interpolated deterministically in Python from those banks.

Evaluate the job description and respond ONLY with a JSON object containing:
{{
"score": <integer between 1 and 100 representing fit signal>,
"reason": "<1-sentence concise explanation of why this role fits or does not fit>",
"track": "<one letter a|b|c|d|e selecting the resume bullet pool that best matches this role: a=wealth operations, b=data/systems engineering, c=risk & regulatory compliance, d=business intelligence & analytics, e=business operations & CRM systems>",
"bullet_indices": [<int>, <int>, <int>],
"linkedin_template_id": <integer 0-5 selecting a LinkedIn connection note template>,
"outreach_template_id": <integer 0-2 selecting a cold outreach email template>
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

def calculate_hybrid_score_modifier(job, base_ai_score):
    score = base_ai_score
    desc = str(job.get("job_description") or "").lower()
    title = str(job.get("job_title") or "").lower()
    company = str(job.get("employer_name") or "").lower()
    salary_str, max_sal = extract_salary(job)
    tier1_ecosystem = get_filter("tier1_ecosystem", [])
    if any(k in desc or k in company for k in tier1_ecosystem):
        score += 10
    if max_sal >= 60000:
        score += 5
    if any(k in desc for k in ["fintech", "payments", "autotech", "automotive", "saas", "bizops"]):
        score += 10
    if any(k in desc for k in ["python", "sql", "salesforce", "automation", "api"]):
        score += 5
    if any(k in title for k in ["data entry", "admin coordinator", "administrative assistant"]) and max_sal < 60000:
        score -= 15
    if "wealth" in desc and not any(k in desc for k in ["python", "sql", "automation", "systems"]):
        score -= 15
    is_remote = job.get("job_is_remote", False) or "remote" in desc[:300] or "work from home" in desc[:300]
    if is_remote:
        score = min(score, 90)
    return max(1, min(100, score))

def resolve_live_alumni_at_company(company_name, school="Hope College"):
    """JIT alumni resolution: live-queries DuckDuckGo HTML search for a LinkedIn profile at
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
            res = requests.post(url, json=payload, timeout=6)
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
    Returns (pass_bool, score, reason, track, bullet_indices, linkedin_template_id, outreach_template_id).
    """
    if not GEMINI_API_KEY:
        return True, 75, "Fallback pass (No Key)", "a", [0, 1, 2], 0, 0

    try:
        desc_truncated = str(job.get("job_description") or "")[:1800]
        prompt = f"Job Title: {job.get('job_title')}\nCompany: {job.get('employer_name')}\nDescription:\n{desc_truncated}"
        
        # Call API with timeout handling
        raw_text = call_gemini_api(prompt, build_system_prompt())
        
        if raw_text:
            try:
                cleaned_text = re.sub(r'^```(?:json)?\s*|\s*```$', "", raw_text).strip()
                res_data = json.loads(cleaned_text)
                raw_score = int(res_data.get("score", 0))
                reason = res_data.get("reason", "N/A")

                track = str(res_data.get("track", "a") or "a").strip().lower()
                if track not in ("a", "b", "c", "d", "e"):
                    track = "a"

                bullet_indices = res_data.get("bullet_indices", [0, 1, 2])
                if not isinstance(bullet_indices, list) or not all(isinstance(i, int) for i in bullet_indices):
                    bullet_indices = [0, 1, 2]

                linkedin_template_id = res_data.get("linkedin_template_id", 0)
                if not isinstance(linkedin_template_id, int):
                    linkedin_template_id = 0

                outreach_template_id = res_data.get("outreach_template_id", 0)
                if not isinstance(outreach_template_id, int):
                    outreach_template_id = 0

                final_score = calculate_hybrid_score_modifier(job, raw_score)
                return (final_score >= 65), final_score, reason, track, bullet_indices, linkedin_template_id, outreach_template_id
            except Exception as e:
                logging.error(f"Gemini evaluation JSON parse failure: {e}")
                # On parse error, return 0 score with Evaluation Pending status
                return False, 0, "Evaluation Pending", "a", [0, 1, 2], 0, 0
        
        # On API failure/timeout, set score to 0 and status to "Evaluation Pending" (NO fake scores)
        return False, 0, "Evaluation Pending", "a", [0, 1, 2], 0, 0
    
    except Exception as e:
        logging.error(f"Gemini evaluation exception: {e}")
        return False, 0, "Evaluation Pending", "a", [0, 1, 2], 0, 0

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

def generate_cover_letter(company, job_title, job_description=""):
    """Tailored 3-paragraph plain-text cover letter; safe static fallback if Gemini is unavailable."""
    fallback = (
        f"Dear Hiring Manager,\n\n"
        f"I'm writing to express my interest in the {job_title or 'operations'} role at {company or 'your organization'}. "
        "My background in wealth operations, process automation, and reconciliation gives me a strong foundation for this kind of work, "
        "and I've built Python and SQL tools that meaningfully cut down manual processing time in similar environments.\n\n"
        f"I'm particularly drawn to {company or 'your team'} because of the operational rigor the role demands, and I believe my "
        "combination of technical fluency and financial operations experience would let me contribute quickly.\n\n"
        "I'd welcome the opportunity to discuss how I can support your team. Thank you for your consideration.\n\nBest regards,\nKevin Miller"
    )
    if not GEMINI_API_KEY:
        return fallback
    desc_truncated = str(job_description or "")[:800]
    prompt = (
        f"Company: {company or 'N/A'}\nRole: {job_title or 'N/A'}\nDescription:\n{desc_truncated}\n\n"
        "Write a tailored 3-paragraph plain-text cover letter for an early-career candidate with a Python/SQL/Salesforce/"
        "process-automation/wealth-ops background. Paragraph 1: intro + role interest. Paragraph 2: relevant experience "
        "tied to this specific role. Paragraph 3: closing + call to action. No markdown formatting. "
        'Respond ONLY with JSON: {"letter": "<full 3-paragraph plain-text letter>"}'
    )
    raw_text = call_gemini_api(prompt)
    if raw_text:
        try:
            cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', "", raw_text).strip()
            data = json.loads(cleaned)
            letter = data.get("letter", "")
            if letter:
                return str(letter)
        except Exception as e:
            logging.error(f"Cover letter parse failure: {e}")
    return fallback

# ==============================================================================
# 6. STAGE 1 STRICT FILTER & SINGLE CANDIDATE EVALUATION
# ==============================================================================
def passes_strict_filter(job):
    title = str(job.get("job_title") or "").lower()
    description = str(job.get("job_description") or "").lower()
    company = str(job.get("employer_name") or "").lower()
    city = str(job.get("job_city") or "").lower()
    salary_str, max_sal = extract_salary(job)

    if is_company_on_cooldown(company):
        return False
    applied_companies = get_applied_crm_companies()
    clean_company = normalize_company_for_match(company)
    if company in applied_companies or clean_company in applied_companies:
        logging.info(f"[EXCLUDED] {company} is already in Tetiana Warm (applied).")
        return False
    
    min_sal_floor = safe_int(get_filter("min_salary"), 50000)
    if max_sal > 0 and max_sal < min_sal_floor:
        return False

    valid_cities = get_filter("valid_cities", [])
    # Metro-area allowlist only (~35mi of Farmington MI via radius_miles) - state=="MI" alone is NOT
    # sufficient, since that would also admit Grand Rapids/Lansing/Traverse City etc. outside the radius.
    is_in_metro_area = any(c in city for c in valid_cities)
    is_remote = job.get("job_is_remote", False) or "remote" in description[:300] or "work from home" in description[:300]
    if not (is_in_metro_area or is_remote):
        return False

    exp_floor = safe_int(get_filter("experience_salary_floor"), 60000)
    if any(k in description for k in ["3+ years", "3-5 years", "4+ years"]) and (0 < max_sal < exp_floor):
        return False

    if any(re.search(rf"\b{re.escape(term)}\b", title) for term in get_filter("title_exclusions", [])):
        return False
    if any(comp in company for comp in get_filter("company_exclusions", [])):
        return False
    if any(trigger in description for trigger in get_filter("hard_ban_keywords", [])):
        return False
    if any(sen in title for sen in get_filter("seniority_exclusions", [])):
        return False

    # Gate wealth/finance roles: require at least one systems, automation, or tooling anchor
    if any(term in title or term in description for term in ["wealth", "financial", "advisor", "branch", "banking"]):
        core_systems_keywords = [
            "python", "sql", "salesforce", "automation", "schwab", "fidelity",
            "docusign", "reconciliation", "excel", "hubspot", "api", "etl"
        ]
        if not any(k in description for k in core_systems_keywords):
            return False

    return True

def process_single_candidate(job):
    log_metric_event("ai_screened", source=derive_job_source(job.get("job_id")))
    ai_pass, score, reason, track, bullet_indices, linkedin_template_id, outreach_template_id = evaluate_job_with_gemini(job)
    if ai_pass:
        raw_id = job.get("job_id") or f"{job.get('employer_name')}_{job.get('job_title')}"
        short_id = generate_short_key(raw_id, fallback=time.time())
        job_title = job.get("job_title") or "this role"
        company_name = job.get("employer_name") or "your team"

        # Strict Deterministic Template Engine: Gemini only routed a track + integer indices -
        # Python resolves/bounds-checks the actual bullet text and interpolates the actual
        # LinkedIn/outreach copy from local JSON banks. Gemini never authors this text directly.
        ats_bullets = filter_ats_bullets(track, bullet_indices)
        linkedin_pool = load_linkedin_templates().get("linkedin_templates", [])
        linkedin_template = resolve_template_text(linkedin_pool, linkedin_template_id)
        linkedin_note = sanitize_text(interpolate_template(linkedin_template, name="there", company=company_name, job_title=job_title))[:300]
        cold_pool = load_outreach_templates().get("cold_ops", [])
        cold_template = resolve_template_text(cold_pool, outreach_template_id)
        outreach_email = sanitize_text(interpolate_template(cold_template, name="there", company=company_name, job_title=job_title))

        # Persist routing keys on the cached job so /cv, /stage, and ATS plaintext all resolve
        # the exact same bullets later (bounds-checked again by resume_engine.filter_ats_bullets).
        job["track"] = track
        job["bullet_indices"] = bullet_indices
        sheet_uuid = save_job_to_cache(short_id, job)
        target_email = resolve_target_email(job.get("employer_name"), job.get("job_title"), job.get("employer_website"))
        age_badge = get_age_badge(parse_posted_hours(job.get("job_posted_at_datetime_utc")))
        salary_str, _ = extract_salary(job)
        work_style = extract_work_style(job)
        overlap_pct, matched_skills = calculate_keyword_overlap(job.get("job_description"))

        # Ghost Listing Penalty: dock score + badge for reposted/evergreen listings (>3 sightings across >45 days)
        job_hash = generate_dedup_hash(job.get("employer_name"), job.get("job_title"))
        penalty, ghost_badge = get_ghost_listing_penalty(job_hash)
        if penalty:
            score = max(1, score + penalty)
            age_badge = f"{age_badge}{ghost_badge}"

        # JIT Hope College Alumni Resolution: live public-search lookup, score boost, auto-log Carmen Warm contact
        alumni_line = ""
        alum = resolve_live_alumni_at_company(job.get("employer_name"))
        if alum:
            score = min(100, score + 20)
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

        # Dynamic Contact Quality Multiplier: warm CRM contacts scale the boost by priority rank (1-10 * 3, capped +30)
        contact_info = get_warm_crm_contacts().get(normalize_company_for_match(job.get("employer_name")))
        if contact_info:
            priority_score = contact_info.get("priority_score", 5)
            score_boost = min(30, priority_score * 3)
            score = min(100, score + score_boost)
            alumni_line += (
                f"🔥 <b>WARM REFERRAL AVAILABLE (+{score_boost} pts):</b> "
                f"{html.escape(contact_info.get('name', 'Contact'))} "
                f"<i>({html.escape(contact_info.get('raw_company', 'Firm'))} - Priority {priority_score}/10)</i>\n"
                f"📝 <b>Note:</b> {html.escape(contact_info.get('note', 'Active relationship'))}\n"
            )

        return {
            "job": job, "score": score, "reason": reason,
            "linkedin_note": linkedin_note, "ats_bullets": ats_bullets,
            "outreach_email": outreach_email,
            "target_email": target_email, "age_badge": age_badge,
            "salary_str": salary_str, "work_style": work_style,
            "overlap_pct": overlap_pct, "matched_skills": matched_skills,
            "short_id": short_id, "sheet_uuid": sheet_uuid,
            "alumni_line": alumni_line
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

def create_gmail_draft(to_email, company_name, job_title, is_warm=False, custom_note="", custom_body=None, custom_subject=None, pdf_bytes=None, pdf_filename="Kevin_Miller_Resume.pdf"):
    """Create Gmail draft with 24h dedup check and OAuth token expiry handling.
    Returns (success, message, draft_id) - draft_id is populated on success or when a duplicate is found.
    """
    missing_vars = [v for v in ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_USER"] if not os.environ.get(v)]
    if missing_vars:
        return False, f"Missing Env Vars: {', '.join(missing_vars)}", None

    # Strip bracketed confidence tags (e.g. "user@x.com [⚠️ Fallback Email]") before this ever
    # reaches an SMTP header - the tag is a UI-only warning, never part of the real address.
    clean_to_email = str(to_email or "").split(" [")[0].strip()

    if custom_body is not None:
        body_content = custom_body
        subject = custom_subject or f"Following up - {company_name}"
    elif is_warm:
        body_content = generate_warm_email(custom_note)
        subject = f"Reconnecting - {company_name}"
    else:
        body_content = generate_cold_email(job_title, company_name)
        subject = f"Operations & Systems Alignment - {job_title} @ {company_name}"

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

def classify_inbound_ats_email(sender: str, subject: str, snippet: str):
    """
    Classifies ATS email into 'interview', 'rejection', or 'general'.
    Returns (status_label, crm_action)
    """
    text = f"{subject} {snippet}".lower()

    interview_patterns = [
        r"invitation to interview", r"interview request", r"schedule a (?:call|time|screen|chat)",
        r"selected for an interview", r"next steps with", r"speaking with our team",
        r"move forward with your application"
    ]
    if any(re.search(p, text) for p in interview_patterns):
        return "INTERVIEW_SET", "update_interview"

    rejection_patterns = [
        r"unfortunately", r"not moving forward", r"other candidates",
        r"decided to pursue", r"position has been filled", r"impressed with your background, but"
    ]
    if any(re.search(p, text) for p in rejection_patterns):
        return "REJECTION", "update_rejected"

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
            if not crm_match:
                logging.info(f"[BLOCKED] Unverified sender (not found in SQLite/Sheets CRM): {sender}")
                requests.post(modify_url, headers=headers, json={"removeLabelIds": ["UNREAD"]}, timeout=10)
                continue

            logging.info(f"[ALLOWED] Verified CRM sender {sender} matched to {crm_match.get('company')} ({crm_match.get('tab')})")

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
            if status_label == "INTERVIEW_SET":
                log_metric_event("interview_set")
                record_application_outcome(crm_match.get("sheet_uuid"), "interview", company=crm_match.get("company"))
            elif status_label == "REJECTION":
                record_application_outcome(crm_match.get("sheet_uuid"), "rejection", company=crm_match.get("company"))

            alert_msg = (
                f"📬 <b>New Gmail Reply!</b>\n\n"
                f"{status_line}"
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

def scheduled_email_poll_job():
    """APScheduler job target: fires exactly once every 15 minutes, independent of webhook load."""
    logging.info("[POLL] 15-minute email poll cycle triggered")
    try:
        check_inbound_gmail_replies()
    except Exception as e:
        logging.error(f"[POLL] Gmail Poller Cycle Error: {e}")
    logging.info("[POLL] 15-minute email poll cycle completed")

def start_gmail_poller():
    """Register the Gmail reply poller on a strict 15-minute interval trigger (APScheduler),
    replacing the old fixed-sleep thread loop. Ensures polling never runs on webhook requests.
    """
    EMAIL_POLL_SCHEDULER.add_job(
        scheduled_email_poll_job,
        trigger="interval",
        minutes=15,
        id="gmail_inbound_poll",
        next_run_time=datetime.now(),  # fire once immediately on boot, then every 15 minutes
        max_instances=1,
        coalesce=True
    )
    EMAIL_POLL_SCHEDULER.start()
    logging.info("[POLL] Gmail inbound poller scheduled: every 15 minutes")

BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
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
        f"• Hunter.io: {api_usage['hunter']} | Anymail Finder: {api_usage['anymail']} (month-to-date local calls)\n\n"
        f"⚠️ <b>Overdue:</b> {len(overdue)} records\n"
        f"<code>/sendall</code> Draft bumps + set all eligible records to +14d\n"
        f"<code>/snoozeall 7</code> Move all overdue follow-ups by N days"
    )
    send_telegram_message(chat_id, hub)
    send_telegram_message(chat_id, format_outcome_metrics_message())
    if not overdue:
        return

    lines = ["⚠️ <b>All Overdue Records (next follow-up ASC):</b>"]
    for record in overdue:
        tab = html.escape(record.get("sheet_tab") or "Unknown")
        company = html.escape(str(record.get("company") or "N/A"))
        name = html.escape(str(record.get("name") or ""))
        due = html.escape(str(record.get("next_followup") or "N/A"))
        lines.append(f"• <b>{company}</b>{f' - {name}' if name else ''} | {tab} | due {due}")

    chunk = ""
    for line in lines:
        if chunk and len(chunk) + len(line) + 1 > 3900:
            send_telegram_message(chat_id, chunk)
            chunk = ""
        chunk = f"{chunk}\n{line}".strip()
    if chunk:
        send_telegram_message(chat_id, chunk)

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
    """
    if not CRM_WEBHOOK_URL:
        return False
    # Ensure row operation order is DESC for backwards loop searches
    if "rowOperationOrder" not in payload:
        payload["rowOperationOrder"] = "DESC"
    delay = 1.0
    for attempt in range(max_retries):
        try:
            res = crm_post(payload)
            if res and res.status_code == 200:
                return True
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
    """Return every overdue Carmen Warm and Tetiana Cold record sorted by next_followup ASC."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    overdue = []
    for target_code, tab_name in (("CW", "Carmen Warm"), ("TC", "Tetiana Cold")):
        for record in fetch_networking_cards(target_code, qty=None):
            next_followup = str(record.get("next_followup") or "")
            if next_followup and next_followup <= today_str:
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
                job_title=record.get("title") or "Operations Specialist",
                custom_body=generate_bump_email(record.get("name") or ""),
                custom_subject=f"Following up - {record.get('company') or 'Target Firm'}"
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

def send_telegram_card(job, score, reason, target_email, age_badge, salary_str, work_style, overlap_pct, matched_skills, short_id, sheet_uuid=None, linkedin_note="", ats_bullets=None, alumni_line="", outreach_email=""):
    """Send an executive-scannable job card as pure text - no inline keyboards, swipe-reply only.
    Captures the telegram_message_id and maps it to sheet_uuid for later swipe-reply resolution.
    """
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return None
    ats_bullets = ats_bullets or []
    company = html.escape(str(job.get("employer_name") or "N/A"))
    title = html.escape(str(job.get("job_title") or "N/A"))
    apply_link = html.escape(str(job.get("job_apply_link") or "#"), quote=True)
    apollo_url = html.escape(build_apollo_url(company), quote=True)
    linkedin_url = html.escape(build_linkedin_url(company), quote=True)
    dork_url = html.escape(build_hiring_manager_dork(company, job.get("job_title")), quote=True)
    recruiter_dork_url = html.escape(build_recruiter_dork(company), quote=True)
    alumni_url = html.escape(build_alumni_dork(company), quote=True)
    # Truncate raw dynamic content BEFORE HTML-escaping/tag-wrapping so tags never get cut mid-string
    reason_safe = str(reason or "")[:300]
    matched_str = (", ".join(matched_skills[:4]).title() if matched_skills else "General Ops")[:150]
    bullets_block = ("\n".join(f"• {b}" for b in ats_bullets) if ats_bullets else "N/A")[:500]
    linkedin_note_safe = str(linkedin_note or "")[:300]
    alumni_line_safe = str(alumni_line or "")[:400]
    outreach_email_safe = str(outreach_email or "")[:600]
    fit_dot = get_fit_score_indicator(score)
    card_text = (
        f"💼 <b>{title}</b>\n"
        f"🏢 <b>{company}</b>\n"
        f"────────────────────\n"
        f"{fit_dot} <b>Fit Score:</b> {score}/100  |  <b>Skill Match:</b> {overlap_pct}%\n"
        f"🕐 <b>Recency:</b> {age_badge}\n"
        f"💰 <b>Pay &amp; Style:</b> {work_style} | {salary_str}\n"
        f"{alumni_line_safe}\n"
        f"🧩 <b>Matched Skills:</b> <code>{html.escape(matched_str)}</code>\n\n"
        f"<b>Fit Reason:</b> {html.escape(reason_safe)}\n\n"
        f"🔗 <b>Quick Links:</b>\n"
        f"<a href='{apply_link}'>Direct Apply</a> | "
        f"<a href='{apollo_url}'>Apollo Operations Leads</a> | "
        f"<a href='{linkedin_url}'>LinkedIn Leadership Search</a> | "
        f"<a href='{alumni_url}'>🎓 Alumni Connections</a>\n\n"
        f"🎯 <b>Direct Decision Makers:</b>\n"
        f"  👔 <a href='{dork_url}'>Search Director / VP of Ops (Hiring Manager)</a> |\n"
        f"  🤝 <a href='{recruiter_dork_url}'>Search Senior In-House Recruiter</a>\n\n"
        f"🧭 <b>Dual-Path Outreach Strategy:</b>\n"
        f"  👔 <i>To Director/VP:</i> Lead with process automation, efficiency gains, and operational rigor.\n"
        f"  🤝 <i>To Recruiter:</i> Confirm application submission, reference the specific role, request a brief phone screen.\n\n"
        f"📧 <b>Target (tap to copy):</b>\n<code>{html.escape(target_email)}</code>\n\n"
        f"🤝 <b>LinkedIn Connect Note (&lt;300 chars):</b>\n<code>{html.escape(linkedin_note_safe) if linkedin_note_safe else 'N/A'}</code>\n\n"
        f"📄 <b>Tailored ATS Resume Bullets:</b>\n<code>{html.escape(bullets_block)}</code>\n\n"
        f"✉️ <b>Cold Outreach Draft (tap to copy):</b>\n<code>{html.escape(outreach_email_safe) if outreach_email_safe else 'N/A'}</code>\n\n"
        f"⚡ <b>Swipe Actions (reply to this card):</b>\n"
        f"  <code>/apply</code> Mark Applied   <code>/draft</code> Gmail Draft\n"
        f"  <code>/warm</code> Move Warm   <code>/cold</code> Move Cold   <code>/x</code> Dead\n"
        f"  <code>/f &lt;days&gt;</code> Snooze   <code>/n &lt;note&gt;</code> Log Note"
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
                save_message_mapping(telegram_message_id, sheet_uuid, "Pipeline_Candidates", company, "", target_email)
            return telegram_message_id
    except Exception as e:
        logging.error(f"Failed to post card to Telegram: {e}")
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
            if attempt == JSEARCH_MAX_RETRIES:
                logging.error(f"JSearch timeout on page {page} ({query}) - retries exhausted")
                return [], True
            logging.warning(f"JSearch timeout on page {page} ({query}), attempt {attempt+1}/{JSEARCH_MAX_RETRIES+1} - retrying in {delay}s")
            time.sleep(delay)
            delay *= 2.0
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
    radius_miles = safe_int(get_filter("radius_miles"), 35)
    start_page = get_query_start_page(query)
    all_jobs = []
    for offset in range(3):
        page = start_page + offset
        params = {"query": query, "page": str(page), "num_pages": "1", "date_posted": "month"}
        if not is_remote_query and radius_miles:
            params["radius"] = str(radius_miles)
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
    """Pull unauthenticated postings from a Greenhouse job board for a company slug."""
    try:
        res = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=10)
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

def fetch_ats_jobs(company_slugs):
    """Pull unauthenticated Greenhouse + Lever + Ashby postings for a list of company slugs, in parallel."""
    if not company_slugs:
        return []
    all_jobs = []
    with ThreadPoolExecutor(max_workers=min(len(company_slugs) * 3, 18) or 1) as executor:
        futures = []
        for slug in company_slugs:
            futures.append(executor.submit(fetch_greenhouse_jobs, slug))
            futures.append(executor.submit(fetch_lever_jobs, slug))
            futures.append(executor.submit(fetch_ashby_jobs, slug))
        for future in futures:
            try:
                all_jobs.extend(future.result(timeout=15))
            except Exception as e:
                logging.error(f"ATS Fetch Future Error: {e}")
    return all_jobs

def auto_expand_ats_slug(company_name):
    """Silent Auto-ATS Expansion: best-effort guess of a company's Greenhouse/Lever/Ashby board slug from its
    name; if any board actually resolves, appends the slug to the ats_company_slugs filter so future
    /t runs source directly from it. Meant to run on a background daemon thread - silent on no match.
    Distinct from expand_ecosystem_filter() (the Gemini-powered /ecosystem add command), which also
    discovers keyword aliases and returns a Telegram report string - this one is fire-and-forget.
    """
    slug_guess = re.sub(r'[^a-z0-9]', '', str(company_name or '').lower())
    if not slug_guess:
        return
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
    logging.info(f"[ATS EXPANSION] No ATS board match found for '{company_name}' (guessed slug '{slug_guess}')")

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
    if chat_id:
        send_status_update(chat_id, "Stage 1: Fetching raw listings from JSearch (3 pages/query, ~240/batch) in parallel...")

    seen_hashes = set()
    candidate_pool = []
    raw_discovered_count = 0
    today_str = datetime.now().strftime("%Y-%m-%d")
    followup_date = (datetime.now() + timedelta(days=calculate_followup_interval(5))).strftime("%Y-%m-%d")

    def _add_candidate(job):
        nonlocal raw_discovered_count
        raw_discovered_count += 1
        company = job.get("employer_name") or ""
        title = job.get("job_title") or ""
        job_hash = generate_dedup_hash(company, title)
        if job_hash in seen_hashes or is_job_seen_db(job_hash):
            return
        seen_hashes.add(job_hash)
        save_seen_job_db(job_hash)

        # Fuzzy content dedup: catches identical postings cross-posted under reworded titles/companies
        content_hash = compute_description_simhash(job.get("job_description"))
        if is_content_seen(content_hash):
            return
        save_content_hash(content_hash)

        log_metric_event("listing_discovered", source=derive_job_source(job.get("job_id")))
        if passes_strict_filter(job):
            candidate_pool.append(job)
    
    # Stage 1: Parallel JSearch fetching (5 pages/query, ~400 listings/batch) + strict filtering
    headers, api_url = build_jsearch_request_config()
    
    target_queries = get_filter("target_queries", [])
    query_tasks = [(q, api_url, headers) for q in target_queries]
    
    with ThreadPoolExecutor(max_workers=min(len(target_queries), 8) or 4) as executor:
        query_results = executor.map(fetch_single_query_jobs, query_tasks)
        for jobs in query_results:
            for job in jobs:
                _add_candidate(job)

    # Stage 1b: High-volume ATS direct-source expansion (unauthenticated Greenhouse/Lever boards)
    ats_slugs = get_filter("ats_company_slugs", [])
    if ats_slugs:
        if chat_id:
            send_status_update(chat_id, f"Stage 1b: Sourcing direct ATS postings from {len(ats_slugs)} companies...")
        for job in fetch_ats_jobs(ats_slugs):
            _add_candidate(job)
    
    logging.info(f"Stage 1 Complete: {raw_discovered_count} raw listings pulled, {len(candidate_pool)} candidates passed strict filter.")
    if raw_discovered_count == 0:
        send_health_alert(
            "JSearch/ATS sourcing returned 0 raw listings this run. Check RAPIDAPI_KEY/OPENWEBNINJA_KEY "
            "validity and the target_queries filter - this usually means the API key expired or every "
            "query is misconfigured, and it will silently produce zero candidates every run until fixed."
        )
    if chat_id:
        send_status_update(
            chat_id,
            f"📊 <b>Batch Ingested:</b> {raw_discovered_count} raw listings pulled.\n"
            f"🎯 <b>Filtered:</b> {len(candidate_pool)} passed strict criteria.\n"
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
                result = future.result(timeout=20)  # 20s timeout per candidate
                if result:
                    top_matches.append(result)
            except Exception as e:
                logging.error(f"Candidate evaluation failed (timeout or error): {e}")
                # On timeout/error: score=0, status='Evaluation Pending' is handled in evaluate_job_with_gemini
    
    # Sort by score descending, then split into Tier-1 (cards + CRM) and Tier-2 (digest only, capped at 5)
    top_matches.sort(key=lambda x: x["score"], reverse=True)
    tier1_matches = [m for m in top_matches if m["score"] >= 80][:5]
    tier2_matches = [m for m in top_matches if 65 <= m["score"] < 80][:5]

    # Dispatch Tier-1 matches as full interactive cards & stage in CRM
    batch_rows = []
    for item in tier1_matches:
        job = item["job"]
        send_telegram_card(
            job, item["score"], item["reason"], item["target_email"],
            item["age_badge"], item["salary_str"], item["work_style"],
            item["overlap_pct"], item["matched_skills"], item["short_id"],
            sheet_uuid=item.get("sheet_uuid"),
            linkedin_note=item.get("linkedin_note", ""),
            ats_bullets=item.get("ats_bullets"),
            alumni_line=item.get("alumni_line", ""),
            outreach_email=item.get("outreach_email", "")
        )
        batch_rows.append({
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
                f"Matched via Pipeline | {item['reason']}"
            ]
        })
        time.sleep(1.1)

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

    # Write ONLY Tier-1 rows to CRM under a single execution lock
    if batch_rows:
        enqueue_crm_payload(build_crm_payload("batch_add_rows", target_code="TC", rows=batch_rows))
    
    logging.info(f"Stage 2 Complete: {len(tier1_matches)} Tier-1 cards + {len(tier2_matches)} Tier-2 digest entries dispatched.")
    if chat_id:
        send_status_update(chat_id, f"Pipeline Complete: {len(tier1_matches)} Tier-1 cards + {len(tier2_matches)} Tier-2 digest entries dispatched.")
    
    return len(tier1_matches) + len(tier2_matches)

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

        # 2. Pipeline Run Trigger (/t [qty])
        if re.match(r"^/t(?:\s+(\d+))?$", text):
            m = re.match(r"^/t(?:\s+(\d+))?$", text)
            qty = safe_int(m.group(1), 2)
            send_telegram_message(chat_id, f"🚀 Triggering Job Search Pipeline (Top {qty})...")
            count = run_job_pipeline(chat_id, top_n=qty)
            send_telegram_message(chat_id, f"🏁 Pipeline Completed. {count} cards dispatched.")
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
                draft_text = generate_warm_email(c.get("note", "")) if is_warm else generate_cold_email(c.get("title", "Operations Specialist"), c.get("company", "Target Firm"))
                monospaced_draft = format_email_block(draft_text)
                card_msg = (
                    f"👤 <b>{c.get('name', 'Contact')}</b> | {c.get('company', 'Company')}\n"
                    f"<b>Priority Tier:</b> {c.get('priority', 5)}/10\n"
                    f"<b>Last Note:</b> <i>{c.get('note', 'N/A')}</i>\n\n"
                    f"<b>Tap-to-Copy Email Draft:</b>\n{monospaced_draft}"
                )
                sent_msg_id = send_telegram_message(chat_id, card_msg)
                contact_sheet_uuid = c.get("sheet_uuid")
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
        if text == "/health":
            send_telegram_message(chat_id, "🟢 <b>System Health:</b> Operational | SQLite WAL persistent | Webhooks Active")
            return
        if text == "/efficiency":
            messages_sent = get_metric_count("message_sent")
            interviews_set = get_metric_count("interview_set")
            ratio = (interviews_set / messages_sent * 100) if messages_sent > 0 else 0.0
            send_telegram_message(chat_id, f"📈 <b>Golden Ratio:</b> {ratio:.1f}% ({interviews_set} interviews / {messages_sent} sent)")
            return
        if text == "/funnel":
            discovered = get_metric_count("listing_discovered")
            screened = get_metric_count("ai_screened")
            drafts_staged = get_metric_count("gmail_draft_staged")
            applied = get_metric_count("applied")
            interviews_set = get_metric_count("interview_set")
            screen_rate = (interviews_set / screened * 100) if screened > 0 else 0.0

            funnel_ascii = render_ascii_funnel([
                ("Discovered Listings", discovered),
                ("AI Screened", screened),
                ("Gmail Drafts Staged", drafts_staged),
                ("Applied Roles", applied)
            ])
            funnel_msg = (
                "📊 <b>Pipeline Conversion Funnel</b>\n"
                f"<pre>{html.escape(funnel_ascii)}</pre>\n"
                f"🎯 <b>Screen / Interview Rate:</b> {screen_rate:.1f}% ({interviews_set} interviews / {screened} screened)"
            )
            send_telegram_message(chat_id, funnel_msg)
            return

        if text == "/outcomes":
            send_telegram_message(chat_id, format_outcome_metrics_message())
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
                target = resolve_target_email(comp, title, job.get("employer_website")) if job else "Unknown"
            track = job.get("track", "a")
            bullet_indices = job.get("bullet_indices")
            pdf_bytes = None
            clean_comp = re.sub(r'[^a-zA-Z0-9]', '', comp)
            pdf_filename = f"Kevin_Miller_Resume_{clean_comp}_Track{str(track).upper()}.pdf"
            try:
                pdf_bytes = compile_resume_pdf(comp, track=track, bullet_indices=bullet_indices)
            except Exception as e:
                logging.error(f"/draft resume compilation failed for {comp}: {e}")
            logging.info(f"/draft command: staging Gmail draft for {comp} <{target}> (chat_id={chat_id})")
            ok, gmail_msg, draft_id = create_gmail_draft(
                to_email=target, company_name=comp, job_title=title, is_warm=is_warm,
                pdf_bytes=pdf_bytes, pdf_filename=pdf_filename
            )
            raw_email_text = generate_warm_email(mapping.get("contact_name", "")) if is_warm else generate_cold_email(title, comp)
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
            comp = job.get("employer_name") or mapping.get("contact_company") or "Target Firm"
            title = job.get("job_title") or "Operations Specialist"
            is_warm = mapping.get("sheet_tab") in ("Carmen Warm", "Carmen Cold")
            update_job_target_email(mapping["sheet_uuid"], new_email)

            # Compile the same tailored resume PDF /draft attaches, so /e never regresses to a bare-text draft
            track = job.get("track", "a")
            bullet_indices = job.get("bullet_indices")
            pdf_bytes = None
            clean_comp = re.sub(r'[^a-zA-Z0-9]', '', comp)
            pdf_filename = f"Kevin_Miller_Resume_{clean_comp}_Track{str(track).upper()}.pdf"
            try:
                pdf_bytes = compile_resume_pdf(comp, track=track, bullet_indices=bullet_indices)
            except Exception as e:
                logging.error(f"/e resume compilation failed for {comp}: {e}")

            ok, gmail_msg, draft_id = create_gmail_draft(
                to_email=new_email, company_name=comp, job_title=title, is_warm=is_warm,
                pdf_bytes=pdf_bytes, pdf_filename=pdf_filename
            )
            raw_email_text = generate_warm_email(mapping.get("contact_name", "")) if is_warm else generate_cold_email(title, comp)
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
            return

        if text == "/prep":
            mapping = resolve_reply_mapping(msg, chat_id, "/prep")
            if not mapping:
                return
            job = get_job_by_sheet_uuid(mapping["sheet_uuid"])
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
            comp = job.get("employer_name") or mapping.get("contact_company") or "Target Firm"
            job_title = job.get("job_title") or "this role"
            pitch = generate_elevator_pitch(comp, job_title)
            pitch_msg = (
                f"🎤 <b>30-Second Elevator Pitch - {html.escape(comp)}</b>\n\n"
                f"<code>{html.escape(pitch)}</code>"
            )
            send_telegram_message(chat_id, pitch_msg)
            return

        if text == "/letter":
            mapping = resolve_reply_mapping(msg, chat_id, "/letter")
            if not mapping:
                return
            job = get_job_by_sheet_uuid(mapping["sheet_uuid"])
            comp = job.get("employer_name") or mapping.get("contact_company") or "Target Firm"
            job_title = job.get("job_title") or "this role"
            letter = generate_cover_letter(comp, job_title, job.get("job_description", ""))
            letter_msg = (
                f"✉️ <b>Cover Letter - {html.escape(comp)}</b>\n\n"
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

            # Fallback to the networking-record mapping (e.g. /quick contacts with no cached job) instead of blocking
            comp = job.get("employer_name") or mapping.get("contact_company") or "Target Company"
            track = requested_track or job.get("track") or "a"
            bullet_indices = job.get("bullet_indices")
            short_id = job.get("short_id") or generate_short_key(job.get("job_id") or mapping["sheet_uuid"], fallback=time.time())

            try:
                pdf_bytes = compile_resume_pdf(comp, track=track, bullet_indices=bullet_indices)
                clean_comp = re.sub(r'[^a-zA-Z0-9]', '', comp)
                filename = f"Kevin_Miller_Resume_{clean_comp}_Track{track.upper()}.pdf"

                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
                files = {"document": (filename, io.BytesIO(pdf_bytes), "application/pdf")}
                caption_text = (
                    f"📄 <b>Tailored Resume ({track.upper()}): {html.escape(comp)}</b>\n\n"
                    f"🖥️ <b>Desktop Staging Link:</b>\n"
                    f"<code>http://localhost:5000/stage/{short_id}?track={track}</code>"
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
            enqueue_crm_payload(build_crm_payload("update_status", sheet_uuid=sheet_uuid, new_tab="Tetiana Warm"))
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
            # Optimistic UI: confirm to Telegram first, dispatch the Sheets write in the background
            send_telegram_message(chat_id, confirm_text)
            # Auto-ATS Expansion: Carmen-family contacts (Cold or Warm) get monitored for future /t job runs
            if new_tab.startswith("Carmen") and mapping.get("contact_company"):
                threading.Thread(target=auto_expand_ats_slug, args=(mapping["contact_company"],), daemon=True).start()
            enqueue_crm_payload(build_crm_payload("update_status", sheet_uuid=sheet_uuid, new_tab=new_tab))
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

        # 10. Catch-All Fallback: unrecognized slash commands get a formatted help menu instead of silence
        if text.startswith("/"):
            send_telegram_message(
                chat_id,
                "⚠️ <b>Command Unrecognized</b>\n\n"
                "<b>CORE COMMANDS:</b>\n"
                "/t - Pull fresh job cards\n"
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
                "/apply - Mark Applied & move to Tetiana Warm\n"
                "/offer - Log an offer for this record\n"
                "/withdraw - Log a withdrawn application\n"
                "/warm - Smart-route lead to its Warm tab\n"
                "/cold - Smart-route lead to its Cold tab\n"
                "/x - Archive lead to Died/Killed tab\n"
                "/n - Append timestamped note\n"
                "/f - Snooze follow-up by [days]\n"
                "/e, /email - Override the target email & re-draft\n"
                "/draft - Generate Gmail draft\n"
                "/cv, /resume - Compile tailored resume PDF\n"
                "/prep - Interview talking points & reverse questions\n"
                "/pitch - 30-second elevator pitch\n"
                "/letter - Generate cover letter\n\n"
                "<b>TUESDAY BATCH HUB:</b>\n"
                "/sendall - Draft bumps + queue eligible overdue records to +14 days\n"
                "/snoozeall [days] - Move every overdue follow-up by 7 days (or the specified number)\n\n"
                "<b>TELEMETRY:</b>\n"
                "/health - View system telemetry and status\n"
                "/efficiency - View Input to Interview Golden Ratio\n"
                "/funnel - View pipeline conversion funnel\n"
                "/outcomes - View evidence-based reply/interview rates by source & path\n"
                "/streak, /daily - View daily outreach scorecard"
            )
            return

    except Exception as e:
        logging.error(f"Async Webhook Processing Error: {e}")

if not os.environ.get("PYTEST_CURRENT_TEST"):  # keep background daemons out of the test process
    start_gmail_poller()
    start_crm_outbox_worker()
    start_morning_digest()
    start_backup_scheduler()

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
        elapsed_ms = (time.time() - start_time) * 1000
        return jsonify({"status": "ok", "mode": mode, "elapsed_ms": round(elapsed_ms, 2)}), 200
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

def handle_fast_path_command(chat_id, text, msg):
    """Directly executes /t, /search, /health, /efficiency, /streak, /daily, /draft with a guaranteed
    synchronous requests.post reply to the Telegram Bot API - fires immediately instead of waiting on
    a background thread so these core commands always produce a visible, instant response.
    Returns True if the command was handled.
    """
    if text == "/sendall":
        sent_id = send_telegram_message(chat_id, "⏳ <b>Send-All Started:</b> creating bump drafts and queueing +14-day follow-ups...")
        logging.info(f"/sendall command acknowledged (chat_id={chat_id}, ack_message_id={sent_id})")
        threading.Thread(target=_run_overdue_batch_and_notify, args=(chat_id, "sendall", 14), daemon=True).start()
        return True

    snooze_match = re.match(r"^/snoozeall(?:\s+(\d+))?$", text)
    if snooze_match:
        snooze_days = safe_int(snooze_match.group(1), 7)
        if snooze_days < 1 or snooze_days > 365:
            send_telegram_message(chat_id, "❌ <b>Usage:</b> <code>/snoozeall &lt;days 1-365&gt;</code>")
            return True
        sent_id = send_telegram_message(chat_id, f"⏳ <b>Snooze-All Started:</b> queueing overdue follow-ups to +{snooze_days} days...")
        logging.info(f"/snoozeall command acknowledged (chat_id={chat_id}, days={snooze_days}, ack_message_id={sent_id})")
        threading.Thread(target=_run_overdue_batch_and_notify, args=(chat_id, "snoozeall", snooze_days), daemon=True).start()
        return True

    t_match = re.match(r"^/t(?:\s+(\d+))?$", text)
    if t_match:
        qty = safe_int(t_match.group(1), 2)
        target_queries = get_filter("target_queries", [])
        ats_slugs = get_filter("ats_company_slugs", [])
        sent_id = send_telegram_message(
            chat_id,
            f"🚀 <b>Triggering Job Search Pipeline (Top {qty})</b>\n"
            f"🔍 Scanning {len(target_queries)} target rules & {len(ats_slugs)} ATS boards (with live Hope Alumni resolution)..."
        )
        logging.info(f"/t command handled directly (chat_id={chat_id}, qty={qty}, ack_message_id={sent_id})")
        threading.Thread(target=_run_pipeline_and_notify, args=(chat_id, qty), daemon=True).start()
        return True

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
        sent_id = send_telegram_message(chat_id, card_text)
        logging.info(f"/search command handled directly (chat_id={chat_id}, ack_message_id={sent_id})")
        return True

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
        sent_id = send_telegram_message(
            chat_id,
            f"🟢 <b>System Health:</b> Operational\n"
            f"💾 <b>SQLite Mode:</b> {html.escape(str(wal_mode)).upper()} ({db_elapsed_ms}ms)\n"
            f"⏱️ <b>Uptime:</b> {uptime_str}\n"
            f"📇 <b>Email Waterfall Usage ({month_label}, local count):</b>\n"
            f"  Hunter.io: {api_usage['hunter']} | Anymail Finder: {api_usage['anymail']}"
        )
        logging.info(f"/health command handled directly (chat_id={chat_id}, ack_message_id={sent_id})")
        return True

    if text == "/efficiency":
        messages_sent = get_metric_count("message_sent")
        interviews_set = get_metric_count("interview_set")
        ratio = (interviews_set / messages_sent * 100) if messages_sent > 0 else 0.0
        sent_id = send_telegram_message(chat_id, f"📈 <b>Golden Ratio:</b> {ratio:.1f}% ({interviews_set} interviews / {messages_sent} sent)")
        logging.info(f"/efficiency command handled directly (chat_id={chat_id}, ack_message_id={sent_id})")
        return True

    if text in ("/streak", "/daily"):
        today_activity = get_daily_activity(datetime.now().strftime("%Y-%m-%d"))
        lifetime = get_lifetime_activity_totals()
        streak_days = calculate_active_day_streak()
        goal_target = 5
        sent_id = send_telegram_message(
            chat_id,
            "🏆 <b>Daily Outreach Scorecard</b>\n\n"
            f"🎯 <b>Today's Goal:</b> {today_activity['drafts_staged']} / {goal_target} Staged Drafts\n"
            f"🔥 <b>Current Streak:</b> {streak_days} Active Days\n"
            f"📊 <b>Lifetime Totals:</b> Staged: {lifetime['drafts_staged']} | Applied: {lifetime['applied_count']} | Notes: {lifetime['notes_logged']}"
        )
        logging.info(f"{text} command handled directly (chat_id={chat_id}, ack_message_id={sent_id})")
        return True

    if text == "/draft":
        mapping = resolve_reply_mapping(msg, chat_id, "/draft")
        if not mapping:
            return True
        job = get_job_by_sheet_uuid(mapping["sheet_uuid"])
        comp = job.get("employer_name") or mapping.get("contact_company") or "Target Firm"
        title = job.get("job_title") or "Operations Specialist"
        is_warm = mapping.get("sheet_tab") in ("Carmen Warm", "Carmen Cold")
        domain_hint = extract_domain_from_website(job.get("employer_website")) if job else None
        if mapping.get("contact_name"):
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
                return True
        else:
            target = resolve_target_email(comp, title, job.get("employer_website")) if job else "Unknown"
        track = job.get("track", "a")
        bullet_indices = job.get("bullet_indices")
        pdf_bytes = None
        clean_comp = re.sub(r'[^a-zA-Z0-9]', '', comp)
        pdf_filename = f"Kevin_Miller_Resume_{clean_comp}_Track{str(track).upper()}.pdf"
        try:
            pdf_bytes = compile_resume_pdf(comp, track=track, bullet_indices=bullet_indices)
        except Exception as e:
            logging.error(f"/draft resume compilation failed for {comp}: {e}")
        logging.info(f"/draft command handled directly: staging Gmail draft for {comp} <{target}> (chat_id={chat_id})")
        ok, gmail_msg, draft_id = create_gmail_draft(
            to_email=target, company_name=comp, job_title=title, is_warm=is_warm,
            pdf_bytes=pdf_bytes, pdf_filename=pdf_filename
        )
        raw_email_text = generate_warm_email(mapping.get("contact_name", "")) if is_warm else generate_cold_email(title, comp)
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
        sent_id = send_telegram_message(chat_id, draft_msg)
        logging.info(f"/draft command reply dispatched (chat_id={chat_id}, ack_message_id={sent_id})")
        return True

    return False

@app.route("/telegram", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    """
    Instant non-blocking execution (<0.05s return).
    Validates Telegram's secret token header. Core commands (/t, /search, /health, /efficiency,
    /streak, /daily, /draft) are handled synchronously right here with a guaranteed requests.post
    reply. Everything else spawns an isolated daemon thread immediately (no bounded queue/worker
    pool - unbounded thread-per-update, matching the rest of the app's fire-and-forget dispatch pattern).
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

        if update_kind == "message":
            try:
                msg = data["message"]
                chat_id = msg["chat"]["id"]
                raw_text = msg.get("text", "").strip()
                text = re.sub(r"@\w+bot", "", raw_text, flags=re.IGNORECASE).strip()
                logging.info(f"Telegram Webhook: parsed chat_id={chat_id} text='{text}'")
                if handle_fast_path_command(chat_id, text, msg):
                    return jsonify({"status": "ok"}), 200
            except Exception as e:
                logging.error(f"Telegram Webhook fast-path error: {e} - falling back to async thread")

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
    validated_bullets = filter_ats_bullets(track, job.get("bullet_indices"))
    for b in validated_bullets:
        lines.append(f"- {b}")

    return "\n".join(lines).strip()

@app.route("/stage/<short_id>", methods=["GET"])
def desktop_stage_view(short_id):
    """Desktop review page showing tailored bullets, apply portal link, and PDF preview."""
    job = get_job_from_cache(short_id)
    if not job:
        return "<h3>Job not found or cache expired.</h3>", 404

    track = request.args.get("track") or job.get("track") or "a"
    comp = job.get("employer_name", "Target Firm")
    title = job.get("job_title", "Role")
    apply_link = job.get("job_apply_link", "#")
    bullets = filter_ats_bullets(track, job.get("bullet_indices"))
    bullets_html = "".join([f"<li>{html.escape(str(b))}</li>" for b in bullets])
    ats_plaintext = format_ats_plaintext(job, track)

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
            .btn-secondary {{ background: #e9ecef; color: #333; }}
            ul {{ line-height: 1.6; }}
            iframe {{ width: 100%; height: 500px; border: 1px solid #ddd; margin-top: 20px; border-radius: 4px; }}
            textarea {{ box-sizing: border-box; border: 1px solid #ddd; border-radius: 4px; padding: 10px; margin-top: 8px; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h2>{html.escape(title)} @ {html.escape(comp)}</h2>
            <p><b>Targeted ATS Bullets:</b></p>
            <ul>{bullets_html}</ul>
            <div style="margin-top: 20px;">
                <a class="btn btn-primary" href="/stage/{short_id}/pdf?track={track}" download="Kevin_Miller_Resume_{re.sub(r'[^a-zA-Z0-9]', '', comp)}.pdf">⬇️ Download Tailored PDF</a>
                <a class="btn btn-secondary" href="{html.escape(apply_link)}" target="_blank">🔗 Open Application Portal</a>
            </div>
            <iframe src="/stage/{short_id}/pdf?track={track}"></iframe>

            <h3 style="margin-top: 24px;">Raw ATS Text (Workday / Taleo fallback)</h3>
            <p style="color: #666; font-size: 0.9em;">Legacy ATS parsers sometimes fail to read the PDF - paste this plain-text version into application forms instead.</p>
            <textarea id="ats-raw-text" rows="15" style="width: 100%; font-family: monospace;" readonly>{html.escape(ats_plaintext)}</textarea>
            <div style="margin-top: 10px;">
                <button class="btn btn-secondary" onclick="copyAtsText()" style="border: none; cursor: pointer;">📋 Copy ATS Text</button>
            </div>
        </div>
        <script>
            function copyAtsText() {{
                const textarea = document.getElementById('ats-raw-text');
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
        return "Job cache expired", 404
    track = request.args.get("track") or job.get("track") or "a"
    comp = job.get("employer_name", "Target Firm")
    bullet_indices = job.get("bullet_indices")
    pdf_bytes = compile_resume_pdf(comp, track=track, bullet_indices=bullet_indices)
    return Response(pdf_bytes, mimetype="application/pdf")

@app.route("/ingest", methods=["POST"])
def desktop_ingest():
    """Secure endpoint for desktop bookmarklet ingestion of manual job links/text."""
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
        if not raw_text and not url:
            return jsonify({"status": "error", "message": "No job text or URL provided"}), 400

        job = {
            "job_id": f"ingest_{hashlib.md5((url or raw_text).encode()).hexdigest()[:12]}",
            "employer_name": data.get("company") or "Manual Ingest",
            "job_title": title or "Manually Ingested Role",
            "job_description": raw_text or title,
            "job_apply_link": url,
            "job_city": "",
            "job_state": "",
            "job_is_remote": False,
            "job_posted_at_datetime_utc": datetime.now(timezone.utc).isoformat()
        }

        def _process_and_dispatch():
            result = process_single_candidate(job)
            if result:
                send_telegram_card(
                    result["job"], result["score"], result["reason"], result["target_email"],
                    result["age_badge"], result["salary_str"], result["work_style"],
                    result["overlap_pct"], result["matched_skills"], result["short_id"],
                    sheet_uuid=result.get("sheet_uuid"),
                    linkedin_note=result.get("linkedin_note", ""),
                    ats_bullets=result.get("ats_bullets"),
                    outreach_email=result.get("outreach_email", "")
                )
            elif TELEGRAM_CHAT_ID:
                send_telegram_message(TELEGRAM_CHAT_ID, f"⚠️ Ingested job did not pass AI screening: {html.escape(job['job_title'])}")

        threading.Thread(target=_process_and_dispatch, daemon=True).start()
        return jsonify({"status": "ok", "message": "Ingestion queued"}), 200
    except Exception as e:
        logging.error(f"Ingest Endpoint Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
