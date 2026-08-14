import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import html
import io
import json
import logging
import os
import queue
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
from resume_engine import compile_resume_pdf

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)

# ==============================================================================
# 1. ENVIRONMENT VARIABLES & DATABASE INITIALIZATION (WAL MODE)
# ==============================================================================
API_KEY = os.environ.get("OPENWEBNINJA_KEY") or os.environ.get("RAPIDAPI_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
CRM_WEBHOOK_URL = os.environ.get("CRM_WEBHOOK_URL")
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN")
GMAIL_USER = os.environ.get("GMAIL_USER")
JSEARCH_URL = "https://api.openwebninja.com/jsearch/search"
DB_PATH = "jobs_cache.db"

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

# Bounded webhook queue + fixed daemon worker pool (backpressure instead of unbounded threads)
WEBHOOK_QUEUE = queue.Queue(maxsize=100)
WEBHOOK_WORKER_COUNT = 4

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
        CREATE TABLE IF NOT EXISTS crm_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payload_json TEXT NOT NULL,
            status TEXT DEFAULT 'PENDING',
            retry_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_attempt TIMESTAMP
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
                    "customer service representative", "call center", "door to door", "cold call"
                ],
                "company_exclusions": [
                    "cybercoders", "robert half", "kforce", "jobot", "actalent", "insight global"
                ],
                "hard_ban_keywords": [
                    "lead generation", "upselling", "quota-driven", "client acquisition",
                    "hunter mentality", "pipeline development", "uncapped earnings",
                    "cold outreach", "deal closing", "solution pitching",
                    "uncapped potential", "commission", "hustle", "grind", "door-to-door",
                    "phone jockey", "call jockey", "cold calling"
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
                    "Wealth Operations Detroit MI",
                    "Fintech Operations Michigan",
                    "Business Operations Analyst Detroit MI",
                    "Custodial Operations Schwab Fidelity Michigan",
                    "Financial Systems Process Automation Detroit MI",
                    "Operations Specialist Detroit MI",
                    "Financial Operations Analyst Remote"
                ]
            }
            for k, v in defaults.items():
                conn.execute("INSERT INTO search_filters (key, value_json) VALUES (?, ?)", (k, json.dumps(v)))
            conn.commit()

    hydrate_filters_from_sheets()

def hydrate_filters_from_sheets():
    """On startup, pull load_system_config from Sheets so local filters reflect any manual spreadsheet edits."""
    if not CRM_WEBHOOK_URL:
        return
    try:
        res = requests.get(f"{CRM_WEBHOOK_URL}?action=load_system_config", timeout=10)
        if res.status_code != 200:
            return
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
        if CRM_WEBHOOK_URL:
            try:
                requests.post(CRM_WEBHOOK_URL, json={"action": "update_system_config", "key": key, "value": val}, timeout=5)
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

def compute_description_simhash(text: str) -> str:
    """Computes a normalized SimHash token on the core job description."""
    clean = re.sub(r'[^a-zA-Z0-9\s]', '', str(text or "")[:400].lower())
    tokens = clean.split()
    if not tokens:
        return hashlib.md5(b"").hexdigest()
    # Normalize 3-grams to catch reworded titles with identical bodies
    shingles = [" ".join(tokens[i:i+3]) for i in range(max(1, len(tokens)-2))]
    return hashlib.md5("".join(sorted(shingles)).encode()).hexdigest()

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
    clean = str(company_name or "").lower().strip()
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

def log_metric_event(event_type, sheet_uuid=None):
    """Persist a pipeline metric event (e.g. message_sent, interview_set) to SQLite atomically."""
    try:
        with get_db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO pipeline_metrics (event_type, sheet_uuid, timestamp) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (event_type, sheet_uuid)
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
    clean = str(company_name or "").lower().strip()
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
        send_telegram_message(chat_id, f"⚠️ <code>{html.escape(command_label)}</code> requires a reply context. Please reply to a job/contact card message.")
        return None
    mapping = get_mapping_from_message_id(reply_msg.get("message_id"))
    if not mapping:
        send_telegram_message(chat_id, "⚠️ No CRM record found for this card. Please retry with /t or /c to regenerate it.")
        return None
    return mapping

# ==============================================================================
# 3. DYNAMIC PRIORITY DECAY & ANTI-FLUFF EMAIL ENGINE
# ==============================================================================
def calculate_followup_interval(priority_score):
    try:
        p = float(priority_score)
        return max(3, int(round(35.0 - (p * 3.2))))
    except Exception:
        return 14

def sanitize_text(text):
    """Strip corporate fluff while preserving apostrophes, hyphens, and paragraph breaks."""
    if not text:
        return ""
    cleaned = str(text)
    cleaned = re.sub(r'[\u2014\u2013]', "", cleaned)  # em-dash / en-dash only
    cleaned = re.sub(r'[;:]', "", cleaned)
    buzzwords = ["leveraging", "passionate", "seamless", "synergy", "cutting-edge", "paradigm"]
    for bw in buzzwords:
        cleaned = re.sub(rf'\b{bw}\b', "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b(\w+),\s*(\w+),\s*and\s*(\w+)\b', r'\1 and \2', cleaned)
    cleaned = re.sub(r'[ \t]+', ' ', cleaned)  # collapse horizontal whitespace only
    cleaned = re.sub(r' *\n *', '\n', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)  # cap excess blank lines, keep \n\n breaks
    return cleaned.strip()

def enforce_sentence_limit(text, max_sentences):
    """Truncate text to at most max_sentences sentences."""
    sentences = [s for s in re.split(r'(?<=[.!?])\s+', text.strip()) if s]
    return ' '.join(sentences[:max_sentences])

def generate_cold_email(job_title, company_name, core_exp="wealth ops and process automation"):
    """Full cold email: greeting, strict 2-sentence body, sign-off as separate paragraphs."""
    s1 = f"I saw the {job_title} role at {company_name} and wanted to highlight my background in {core_exp}."
    s2 = "Would you be open to a brief 5 minute call next week to discuss alignment?"
    body = enforce_sentence_limit(f"{sanitize_text(s1)} {sanitize_text(s2)}", 2)
    return f"Hi,\n\n{body}\n\nBest regards,\nKevin Miller"

def generate_warm_email(note_context=""):
    """Full warm email: greeting, strict 3-sentence body, sign-off as separate paragraphs."""
    s1 = sanitize_text(note_context) if note_context else "I hope you have been doing well."
    s2 = "I am currently interning in wealth ops at Signal Advisors, a fast growing startup in downtown Detroit."
    s3 = "I am wondering what you have been up to lately, and would love to reconnect over coffee or a quick call if you have time."
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
SYSTEM_PROMPT = """You are a strict technical job screener evaluating roles for an early-career candidate (0-2 years experience). Target Profile: Non-sales W-2 roles in Tech, FinTech, Auto Tech, or Back-Office Systems/Operations in Metro Detroit or Remote.
High Priority Skills: Python, SQL, Salesforce, Excel, Schwab SAC, Fidelity Wealthscape, DocuSign, Process Automation.
Strictly FORBIDDEN: Sales, cold calling, client pitching, commission-based roles, retail bank tellers, CPA tracks, Senior/Lead/Manager roles.
Evaluate the job description and respond ONLY with a JSON object containing:
{
"score": <integer between 1 and 100 representing fit signal>,
"reason": "<1-sentence concise explanation of why this role fits or does not fit>",
"linkedin_note": "<a personalized LinkedIn connection note tailored to this specific role and company, strictly under 300 characters, ideally under 280>",
"ats_bullets": ["<high-impact quantified resume bullet #1 tailored to this job description's keywords and operations/systems focus>", "<high-impact quantified resume bullet #2>"]
}"""

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

def generate_dedup_hash(company, title):
    clean_company = str(company or "").lower().strip()
    clean_title = str(title or "").lower().strip()
    return hashlib.md5(f"{clean_company}_{clean_title}".encode()).hexdigest()

def generate_short_key(raw_id):
    return hashlib.md5(str(raw_id or time.time()).encode()).hexdigest()[:12]

def parse_posted_hours(posted_utc_str):
    if not posted_utc_str:
        return 48
    try:
        dt = datetime.fromisoformat(str(posted_utc_str).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return int((now - dt).total_seconds() / 3600)
    except Exception:
        return 48

def get_age_badge(posted_hours):
    if posted_hours < 24:
        return "🔥 [< 24h FRESH]"
    elif posted_hours < 72:
        return "⚡ [1-3d RECENT]"
    elif posted_hours < 168:
        return "🟢 [3-7d ACTIVE]"
    elif posted_hours < 336:
        return "🟡 [7-14d AGING]"
    else:
        return "🔴 [14-30d STALE]"

def extract_salary(job):
    try:
        min_sal = float(job.get("job_min_salary") or 0)
        max_sal = float(job.get("job_max_salary") or 0)
        curr = str(job.get("job_salary_currency") or "USD")
        period = str(job.get("job_salary_period") or "year").lower()
        if "hour" in period or period == "hr":
            min_sal = min_sal * 2080
            max_sal = max_sal * 2080
            period = "year"
        if min_sal and max_sal:
            return f"${min_sal:,.0f} - ${max_sal:,.0f} {curr}/{period}", max_sal
        elif min_sal or max_sal:
            val = min_sal or max_sal
            return f"${val:,.0f} {curr}/{period}", val
    except Exception:
        pass
    return "Salary Unlisted", 0

def extract_work_style(job):
    desc = str(job.get("job_description") or "").lower()
    is_remote = job.get("job_is_remote", False) or "remote" in desc[:300] or "work from home" in desc[:300]
    if "hybrid" in desc:
        return "Hybrid"
    elif is_remote:
        return "Remote"
    return "On-Site / Unspecified"

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

def build_apollo_url(company_name):
    clean_company = re.sub(r'[^a-zA-Z0-9\s]', "", str(company_name or "")).strip()
    encoded = urllib.parse.quote(f"{clean_company} Operations")
    return f"https://app.apollo.io/#/people?qKeywords={encoded}"

def build_linkedin_url(company_name):
    clean_company = re.sub(r'[^a-zA-Z0-9\s]', "", str(company_name or "")).strip()
    encoded = urllib.parse.quote(f'{clean_company} ("VP" OR "Director" OR "Manager") ("Operations" OR "Compliance")')
    return f"https://www.linkedin.com/search/results/people/?keywords={encoded}"

def build_hiring_manager_dork(company_name, job_title=""):
    """Google dork to surface a company's Head/Director/VP of Operations or COO on LinkedIn."""
    clean_comp = re.sub(r'[^a-zA-Z0-9\s]', '', str(company_name or '')).strip()
    query = f'site:linkedin.com/in "{clean_comp}" ("Head of Operations" OR "Director of Operations" OR "Operations Manager" OR "VP of Operations" OR "COO")'
    return f"https://www.google.com/search?q={urllib.parse.quote(query)}"

def build_alumni_dork(company_name, school="Hope College"):
    """Google dork to surface shared-alma-mater employees at a target company on LinkedIn."""
    clean_comp = re.sub(r'[^a-zA-Z0-9\s]', '', str(company_name or '')).strip()
    clean_school = re.sub(r'[^a-zA-Z0-9\s]', '', str(school or '')).strip()
    query = f'site:linkedin.com/in "{clean_comp}" "{clean_school}"'
    return f"https://www.google.com/search?q={urllib.parse.quote(query)}"

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
    Format: /quick Name @ Company [1-10] Note
    Handles company names with numbers and special symbols safely (e.g. 3M, Web3 Labs, 1Password, 7-Eleven).
    """
    clean = text_input.replace("/quick", "").strip()
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
            res = requests.post(url, json=payload, timeout=15)
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
    """Evaluate job with Gemini. On failure/timeout, set score=0 and status 'Evaluation Pending'.
    Thread-safe with timeout handling: DO NOT assign fake scores on failure.
    Returns (pass_bool, score, reason, linkedin_note, ats_bullets).
    """
    if not GEMINI_API_KEY:
        return True, 75, "Fallback pass (No Key)", "", []
    
    try:
        desc_truncated = str(job.get("job_description") or "")[:4000]
        prompt = f"Job Title: {job.get('job_title')}\nCompany: {job.get('employer_name')}\nDescription:\n{desc_truncated}"
        
        # Call API with timeout handling
        raw_text = call_gemini_api(prompt, SYSTEM_PROMPT)
        
        if raw_text:
            try:
                cleaned_text = re.sub(r'^```(?:json)?\s*|\s*```$', "", raw_text).strip()
                res_data = json.loads(cleaned_text)
                raw_score = int(res_data.get("score", 0))
                reason = res_data.get("reason", "N/A")
                linkedin_note = str(res_data.get("linkedin_note", "") or "")[:300]
                ats_bullets = res_data.get("ats_bullets", [])
                if not isinstance(ats_bullets, list):
                    ats_bullets = []
                ats_bullets = [str(b) for b in ats_bullets][:2]
                final_score = calculate_hybrid_score_modifier(job, raw_score)
                return (final_score >= 65), final_score, reason, linkedin_note, ats_bullets
            except Exception as e:
                logging.error(f"Gemini evaluation JSON parse failure: {e}")
                # On parse error, return 0 score with Evaluation Pending status
                return False, 0, "Evaluation Pending", "", []
        
        # On API failure/timeout, set score to 0 and status to "Evaluation Pending" (NO fake scores)
        return False, 0, "Evaluation Pending", "", []
    
    except Exception as e:
        logging.error(f"Gemini evaluation exception: {e}")
        return False, 0, "Evaluation Pending", "", []

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
    fallback = (
        f"Hi, I'm Kevin - I work in wealth ops and process automation, building Python and SQL tools that cut manual reconciliation time. "
        f"I've been following {company or 'your team'} and think my background lines up well with {job_title or 'the operations work'} you're doing. "
        "Would love to grab 15 minutes to see where I could help."
    )
    if not GEMINI_API_KEY:
        return fallback
    prompt = (
        f"Company: {company or 'N/A'}\nRole: {job_title or 'N/A'}\n\n"
        "Write a tight 3-sentence conversational 30-second elevator pitch for an early-career candidate with a "
        "Python/SQL/Salesforce/process-automation/wealth-ops background, tailored to this company and role. "
        'Respond ONLY with JSON: {"pitch": "<3-sentence pitch>"}'
    )
    raw_text = call_gemini_api(prompt)
    if raw_text:
        try:
            cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', "", raw_text).strip()
            data = json.loads(cleaned)
            pitch = data.get("pitch", "")
            if pitch:
                return str(pitch)
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
    state = str(job.get("job_state") or "").upper()
    city = str(job.get("job_city") or "").lower()
    salary_str, max_sal = extract_salary(job)

    if is_company_on_cooldown(company):
        return False
    
    min_sal_floor = safe_int(get_filter("min_salary"), 50000)
    if max_sal > 0 and max_sal < min_sal_floor:
        return False

    valid_cities = get_filter("valid_cities", [])
    is_mi = (state == "MI") or "michigan" in city or any(c in city for c in valid_cities)
    is_remote = job.get("job_is_remote", False) or "remote" in description[:300] or "work from home" in description[:300]
    if not (is_mi or is_remote):
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

    return True

def process_single_candidate(job):
    log_metric_event("ai_screened")
    ai_pass, score, reason, linkedin_note, ats_bullets = evaluate_job_with_gemini(job)
    if ai_pass:
        raw_id = job.get("job_id") or f"{job.get('employer_name')}_{job.get('job_title')}"
        short_id = generate_short_key(raw_id)
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

        return {
            "job": job, "score": score, "reason": reason,
            "linkedin_note": linkedin_note, "ats_bullets": ats_bullets,
            "target_email": target_email, "age_badge": age_badge,
            "salary_str": salary_str, "work_style": work_style,
            "overlap_pct": overlap_pct, "matched_skills": matched_skills,
            "short_id": short_id, "sheet_uuid": sheet_uuid
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

def create_gmail_draft(to_email, company_name, job_title, is_warm=False, custom_note=""):
    """Create Gmail draft with 24h dedup check and OAuth token expiry handling.
    Returns (success, message, draft_id) - draft_id is populated on success or when a duplicate is found.
    """
    missing_vars = [v for v in ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_USER"] if not os.environ.get(v)]
    if missing_vars:
        return False, f"Missing Env Vars: {', '.join(missing_vars)}", None

    if is_warm:
        body_content = generate_warm_email(custom_note)
        subject = f"Reconnecting - {company_name}"
    else:
        body_content = generate_cold_email(job_title, company_name)
        subject = f"Operations & Systems Alignment - {job_title} @ {company_name}"

    existing = check_existing_gmail_draft(to_email, subject)
    if existing:
        if TELEGRAM_CHAT_ID:
            send_telegram_message(
                TELEGRAM_CHAT_ID,
                f"ℹ️ <b>Draft Already Exists</b>\n"
                f"<b>To:</b> <code>{html.escape(to_email)}</code>\n"
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
        message["To"] = to_email
        message["From"] = GMAIL_USER
        message["Subject"] = subject
        message.set_content(body_content)
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        draft_url = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        res = requests.post(draft_url, headers=headers, json={"message": {"raw": raw_message}}, timeout=10)
        if res.status_code in [200, 201]:
            draft_id = res.json().get("id", "")
            save_gmail_draft_record(to_email, subject, draft_id)
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
                return {"name": row[0], "company": row[1], "tab": row[2], "sheet_uuid": row[3]}

            # Fallback: exact target_email match inside cached job_json blobs (auto-generated job outreach targets)
            cursor.execute("SELECT sheet_uuid, job_json FROM jobs WHERE LOWER(job_json) LIKE ?", (f"%{sender_email}%",))
            for sheet_uuid, job_json in cursor.fetchall():
                try:
                    job_dict = json.loads(job_json)
                    cached_target = str(job_dict.get("target_email", "")).split(" [")[0].strip().lower()
                    if cached_target == sender_email:
                        return {
                            "name": "",
                            "company": job_dict.get("employer_name", "Unknown"),
                            "tab": "Pipeline_Candidates",
                            "sheet_uuid": sheet_uuid
                        }
                except (json.JSONDecodeError, TypeError):
                    continue
    except Exception as e:
        logging.error(f"CRM Whitelist Local Lookup Error: {e}")

    # Live authoritative check against the Google Sheets CRM (catches manual edits not yet cached locally)
    if CRM_WEBHOOK_URL:
        try:
            res = requests.get(f"{CRM_WEBHOOK_URL}?action=find_contact_by_email&email={urllib.parse.quote(sender_email)}", timeout=10)
            if res.status_code == 200:
                data = res.json()
                if data.get("found"):
                    return {
                        "name": data.get("name", ""),
                        "company": data.get("company", "Unknown"),
                        "tab": data.get("sheet_tab", "Unknown"),
                        "sheet_uuid": data.get("sheet_uuid", "")
                    }
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
                logging.info(f"Inbound email dropped (pre-filter: {reject_reason}) from {sender}")
                requests.post(modify_url, headers=headers, json={"removeLabelIds": ["UNREAD"]}, timeout=10)
                continue

            # GATE 2: Strict CRM whitelist - zero tolerance for unverified senders
            crm_match = is_verified_crm_contact(sender)
            if not crm_match:
                logging.info(f"Inbound email dropped (unverified sender, not in CRM) from {sender}")
                requests.post(modify_url, headers=headers, json={"removeLabelIds": ["UNREAD"]}, timeout=10)
                continue

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

def check_inbound_replies_loop():
    """Daemon loop: poll Gmail for unread replies every 120 seconds."""
    while True:
        try:
            check_inbound_gmail_replies()
        except Exception as e:
            logging.error(f"Gmail Poller Loop Error: {e}")
        time.sleep(120)

def start_gmail_poller():
    """Spin up the inbound Gmail reply poller as a daemon thread."""
    threading.Thread(target=check_inbound_replies_loop, daemon=True).start()

def morning_digest_loop():
    """Dispatches a standup digest daily at 08:30 AM local time."""
    while True:
        now = datetime.now()
        target_time = now.replace(hour=8, minute=30, second=0, microsecond=0)
        if now >= target_time:
            target_time += timedelta(days=1)

        sleep_seconds = (target_time - now).total_seconds()
        time.sleep(sleep_seconds)

        try:
            if TELEGRAM_CHAT_ID:
                today_str = datetime.now().strftime("%Y-%m-%d")
                activity = get_daily_activity(today_str)
                streak = calculate_active_day_streak()
                cw_cards = fetch_networking_cards("CW", qty=10)
                tc_cards = fetch_networking_cards("TC", qty=10)

                overdue = [
                    c for c in (cw_cards + tc_cards)
                    if c.get("next_followup") and c.get("next_followup") <= today_str
                ]

                digest = (
                    f"🌅 <b>Good Morning! Daily Outreach Brief ({today_str})</b>\n\n"
                    f"🔥 <b>Active Streak:</b> {streak} Days\n"
                    f"🎯 <b>Today's Staged Goal:</b> {activity['drafts_staged']} / 5\n"
                    f"⚠️ <b>Overdue Actions:</b> {len(overdue)} contacts requiring follow-up\n\n"
                    f"Run <code>/s</code> to review overdue contacts or <code>/t</code> to trigger the search pipeline."
                )
                send_telegram_message(TELEGRAM_CHAT_ID, digest)
        except Exception as e:
            logging.error(f"Morning Digest Dispatch Error: {e}")

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
            res = requests.post(CRM_WEBHOOK_URL, json=payload, timeout=10)
            if res.status_code == 200:
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

def crm_outbox_worker_loop():
    """Background daemon processing queued Sheets writes with exponential backoff."""
    while True:
        try:
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
                time.sleep(1.0)
        except Exception as e:
            logging.error(f"CRM Outbox Worker Error: {e}")
        time.sleep(5)

def start_crm_outbox_worker():
    """Spin up the persistent CRM outbox worker as a daemon thread."""
    threading.Thread(target=crm_outbox_worker_loop, daemon=True).start()

def fetch_networking_cards(target_code="CW", qty=2):
    if not CRM_WEBHOOK_URL:
        return []
    try:
        res = requests.post(CRM_WEBHOOK_URL, json={"action": "get_followups", "tab": target_code}, timeout=10)
        if res.status_code == 200:
            leads = res.json().get("followups", [])
            return leads[:qty]
    except Exception as e:
        logging.error(f"Error fetching networking cards: {e}")
    return []

def answer_callback_query(callback_query_id, text=None, show_alert=False):
    """Answer a callback query directly (clears the loading spinner) without sending a new chat message."""
    if not (TELEGRAM_BOT_TOKEN and callback_query_id):
        return
    payload = {"callback_query_id": callback_query_id, "show_alert": show_alert}
    if text:
        payload["text"] = text
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            json=payload,
            timeout=3
        )
    except Exception as e:
        logging.error(f"answerCallbackQuery error: {e}")

def edit_telegram_message(chat_id, message_id, text, reply_markup=None):
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
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        res = requests.post(url, json=payload, timeout=5)
        return res.status_code == 200
    except Exception as e:
        logging.error(f"editMessageText error: {e}")
        return False

def send_telegram_message(chat_id, text, reply_markup=None, callback_query_id=None):
    """Send Telegram message. If callback_query_id provided, answer callback immediately (no spinner).
    Returns the sent message's telegram_message_id, or None on failure.
    """
    if not (TELEGRAM_BOT_TOKEN and chat_id):
        return None
    
    # Answer callback immediately to remove loading spinner
    if callback_query_id:
        answer_callback_query(callback_query_id)
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        res = requests.post(url, json=payload, timeout=5)
        if res.status_code == 429:
            retry_after = res.json().get("parameters", {}).get("retry_after", 1)
            logging.warning(f"Telegram 429 Rate Limit - retrying after {retry_after}s")
            time.sleep(retry_after)
            res = requests.post(url, json=payload, timeout=5)
        if res.status_code == 200:
            log_metric_event("message_sent")
            return res.json().get("result", {}).get("message_id")
    except Exception as e:
        logging.error(f"Telegram Post Error: {e}")
    return None

def get_fit_score_indicator(score):
    """Traffic-light emoji for Fit Score: green >=80, yellow >=65, red otherwise."""
    if score >= 80:
        return "🟢"
    elif score >= 65:
        return "🟡"
    return "🔴"

def send_telegram_card(job, score, reason, target_email, age_badge, salary_str, work_style, overlap_pct, matched_skills, short_id, sheet_uuid=None, linkedin_note="", ats_bullets=None):
    """Send an executive-scannable job card with buttons. Buttons auto-removed on first tap via answerCallbackQuery.
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
    alumni_url = html.escape(build_alumni_dork(company), quote=True)
    # Truncate raw dynamic content BEFORE HTML-escaping/tag-wrapping so tags never get cut mid-string
    reason_safe = str(reason or "")[:300]
    matched_str = (", ".join(matched_skills[:4]).title() if matched_skills else "General Ops")[:150]
    bullets_block = ("\n".join(f"• {b}" for b in ats_bullets) if ats_bullets else "N/A")[:500]
    linkedin_note_safe = str(linkedin_note or "")[:300]
    fit_dot = get_fit_score_indicator(score)
    card_text = (
        f"💼 <b>{title}</b>\n"
        f"🏢 <b>{company}</b>\n"
        f"────────────────────\n"
        f"{fit_dot} <b>Fit Score:</b> {score}/100  |  <b>Skill Match:</b> {overlap_pct}%\n"
        f"🕐 <b>Recency:</b> {age_badge}\n"
        f"💰 <b>Pay &amp; Style:</b> {work_style} | {salary_str}\n\n"
        f"🧩 <b>Matched Skills:</b> <code>{html.escape(matched_str)}</code>\n\n"
        f"<b>Fit Reason:</b> {html.escape(reason_safe)}\n\n"
        f"🔗 <b>Quick Links:</b>\n"
        f"<a href='{apply_link}'>Direct Apply</a> | "
        f"<a href='{apollo_url}'>Apollo Operations Leads</a> | "
        f"<a href='{linkedin_url}'>LinkedIn Leadership Search</a> | "
        f"<a href='{dork_url}'>🎯 Find Direct Hiring Manager (Google Dork)</a> | "
        f"<a href='{alumni_url}'>🎓 Alumni Connections</a>\n\n"
        f"📧 <b>Target (tap to copy):</b>\n<code>{html.escape(target_email)}</code>\n\n"
        f"🤝 <b>LinkedIn Connect Note (&lt;300 chars):</b>\n<code>{html.escape(linkedin_note_safe) if linkedin_note_safe else 'N/A'}</code>\n\n"
        f"📄 <b>Tailored ATS Resume Bullets:</b>\n<code>{html.escape(bullets_block)}</code>\n\n"
        f"⚡ <b>Swipe Actions:</b>\n"
        f"  <code>draft</code> Gmail Draft   <code>/f &lt;days&gt;</code> Snooze\n"
        f"  <code>/tw</code>/<code>/cw</code> Warm   <code>/cc</code>/<code>/tc</code> Cold   <code>/x</code> Dead"
    )
    # Buttons are auto-removed on callback via answerCallbackQuery
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✉️ Draft Email", "callback_data": f"approve:{short_id}"},
                {"text": "✅ Mark Applied", "callback_data": f"apply:{short_id}"}
            ],
            [
                {"text": "🔄 Pivot VP Lead", "callback_data": f"pivot:{short_id}"},
                {"text": "❌ Mark Dead", "callback_data": f"dead:{short_id}"}
            ]
        ]
    }
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": card_text[:3990],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": reply_markup
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
def fetch_single_query_jobs(query_args):
    """Worker function for parallel JSearch API query execution.
    Fetches up to 3 pages sequentially per query for 3x candidate volume; stops early on empty page or 429.
    """
    query, api_url, headers = query_args
    all_jobs = []
    for page in range(1, 4):
        params = {"query": query, "page": str(page), "num_pages": "1", "date_posted": "month"}
        try:
            res = requests.get(api_url, headers=headers, params=params, timeout=10)
            if res.status_code == 429:
                logging.warning(f"JSearch 429 Rate Limit on page {page} ({query}) - stopping pagination")
                break
            if res.status_code != 200:
                break
            page_jobs = res.json().get("data", [])
            if not page_jobs:
                break  # no more results, stop paging early
            all_jobs.extend(page_jobs)
        except Exception as e:
            logging.error(f"Fetch Exception ({query} page {page}): {e}")
            break
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

def run_job_pipeline(chat_id=None, top_n=2):
    """Job search pipeline with two-stage architecture:
    Stage 1: Pre-filter candidates (JSearch multi-page + ATS direct-source, strict filters)
    Stage 2: Concurrent Gemini AI evaluation (uncapped, ThreadPoolExecutor max_workers=20)
    Tiered delivery: Tier-1 (score>=80, top 5) get full interactive cards; Tier-2 (65-79) get a bundled digest.
    """
    logging.info(">>> Starting Job Search Pipeline...")
    if chat_id:
        send_status_update(chat_id, "Stage 1: Fetching raw listings from JSearch (3 pages/query) in parallel...")
    
    seen_hashes = set()
    candidate_pool = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    followup_date = (datetime.now() + timedelta(days=calculate_followup_interval(5))).strftime("%Y-%m-%d")

    def _add_candidate(job):
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

        log_metric_event("listing_discovered")
        if passes_strict_filter(job):
            candidate_pool.append(job)
    
    # Stage 1: Parallel JSearch fetching (3 pages/query) + strict filtering
    rapidapi_key = os.environ.get("RAPIDAPI_KEY")
    openweb_key = os.environ.get("OPENWEBNINJA_KEY")
    if rapidapi_key:
        headers = {"X-RapidAPI-Key": rapidapi_key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
        api_url = "https://jsearch.p.rapidapi.com/search"
    else:
        headers = {"x-api-key": openweb_key} if openweb_key else {}
        api_url = JSEARCH_URL
    
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
    
    logging.info(f"Stage 1 Complete: {len(candidate_pool)} candidates passed strict filter.")
    if chat_id:
        send_status_update(chat_id, f"Stage 1 Complete: {len(candidate_pool)} candidates passed strict filter.\nStage 2: Running AI evaluations (uncapped, Tier-1 capacity)...")
    
    # Stage 2: Evaluate ALL strict-filtered candidates concurrently (uncapped Tier-1 capacity)
    eval_candidates = candidate_pool
    logging.info(f"Stage 2: Evaluating {len(eval_candidates)} candidates with Gemini AI (uncapped)...")
    
    top_matches = []
    with ThreadPoolExecutor(max_workers=4) as eval_executor:
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
    
    # Sort by score descending, then split into Tier-1 (full cards) and Tier-2 (bundled digest)
    top_matches.sort(key=lambda x: x["score"], reverse=True)
    tier1_matches = [m for m in top_matches if m["score"] >= 80][:5]
    tier2_matches = [m for m in top_matches if 65 <= m["score"] < 80]

    # Dispatch Tier-1 matches as full interactive cards, paced to avoid Telegram 429s
    batch_rows = []
    for item in tier1_matches:
        job = item["job"]
        send_telegram_card(
            job, item["score"], item["reason"], item["target_email"],
            item["age_badge"], item["salary_str"], item["work_style"],
            item["overlap_pct"], item["matched_skills"], item["short_id"],
            sheet_uuid=item.get("sheet_uuid"),
            linkedin_note=item.get("linkedin_note", ""),
            ats_bullets=item.get("ats_bullets")
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
        time.sleep(1.1)  # Telegram rate-limit pacing between outbound cards

    # Dispatch Tier-2 (secondary qualified) matches as bundled digests, chunked to stay under Telegram's payload limit
    if tier2_matches:
        digest_lines = []
        for item in tier2_matches:
            job = item["job"]
            comp = str(job.get("employer_name") or "N/A")[:28]
            title = str(job.get("job_title") or "N/A")[:40]
            digest_lines.append(f"{item['score']:>3}  {comp} - {title}")
            batch_rows.append({
                "sheet_uuid": item.get("sheet_uuid"),
                "row_data": [
                    today_str,
                    job.get("employer_name"),
                    job.get("job_title"),
                    item["target_email"],
                    item["score"],
                    "Watchlist",
                    followup_date,
                    job.get("job_apply_link", ""),
                    f"Secondary Match via Pipeline | {item['reason']}"
                ]
            })

        # Max 20 roles/message to keep well under Telegram's 4,096 char payload limit
        chunk_size = 20
        line_chunks = [digest_lines[i:i + chunk_size] for i in range(0, len(digest_lines), chunk_size)]
        total_chunks = len(line_chunks)
        for idx, chunk in enumerate(line_chunks, 1):
            digest_ascii = "\n".join(chunk)
            page_label = f" (Page {idx}/{total_chunks})" if total_chunks > 1 else ""
            digest_msg = (
                f"📋 <b>Secondary Match Leaderboard ({len(tier2_matches)} roles, score 65-79){page_label}</b>\n"
                f"<pre>{html.escape(digest_ascii)}</pre>"
            )
            send_telegram_message(TELEGRAM_CHAT_ID, digest_msg)
            time.sleep(1.1)

    # Single batched CRM write for all Tier-1 + Tier-2 rows under one lock/execution in Code.gs
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
    """Executes heavy workloads in background worker threads so HTTP return is instant."""
    try:
        # 1. Interactive Inline Keyboard Callbacks - Execute answerCallbackQuery immediately
        if "callback_query" in data:
            cb = data["callback_query"]
            callback_query_id = cb.get("id")  # For answerCallbackQuery
            chat_id = cb["message"]["chat"]["id"]
            cb_data = cb.get("data", "") or ""
            cb_parts = cb_data.split(":")
            cb_action = cb_parts[0] if cb_parts and cb_parts[0] else ""
            cb_arg = cb_parts[1] if len(cb_parts) > 1 and cb_parts[1] else None

            if cb_action == "approve":
                if not cb_arg:
                    send_telegram_message(chat_id, "⚠️ Malformed button data. Please re-run /t to regenerate cards.", callback_query_id=callback_query_id)
                    return
                short_id = cb_arg
                message_id = cb["message"].get("message_id")
                # Mutate the inline keyboard immediately to prevent a double-tap race from staging two drafts
                edit_telegram_message(
                    chat_id, message_id,
                    cb["message"].get("text", ""),
                    reply_markup={"inline_keyboard": [[{"text": "⏳ Staging Draft...", "callback_data": "noop"}]]}
                )
                job = get_job_from_cache(short_id)
                if job:
                    target = resolve_target_email(job.get("employer_name"), job.get("job_title"), job.get("employer_website"))
                    comp = job.get("employer_name", "Target Firm")
                    title = job.get("job_title", "Operations Specialist")
                    
                    # 1. Create clean Gmail draft in background
                    ok, msg, draft_id = create_gmail_draft(
                        to_email=target, 
                        company_name=comp, 
                        job_title=title, 
                        is_warm=False
                    )
                    
                    # 2. Build sanitized monospaced body for instant mobile tap-copy
                    raw_email_text = generate_cold_email(title, comp)
                    monospaced_body = format_email_block(raw_email_text)
                    subject_line = f"Operations & Systems Alignment - {title} @ {comp}"

                    if ok:
                        status_hdr = "✉️ <b>Gmail Draft Created & Ready!</b>"
                        log_daily_activity("drafts_staged")
                    else:
                        status_hdr = f"⚠️ <b>Gmail API Alert ({html.escape(msg)})</b> - Manual Copy Below:"

                    # Deep-link straight into the Gmail mobile web draft when we have an id
                    draft_link_line = ""
                    if draft_id:
                        draft_url = html.escape(f"https://mail.google.com/mail/u/0/#drafts/{draft_id}", quote=True)
                        draft_link_line = f"📱 <a href='{draft_url}'>Open Draft in Gmail</a>\n\n"

                    # Send rich Telegram message with autofilled tap-to-copy block
                    card_response = (
                        f"{status_hdr}\n"
                        f"{draft_link_line}"
                        f"<b>To:</b> <code>{html.escape(target)}</code>\n"
                        f"<b>Subject:</b> <code>{html.escape(subject_line)}</code>\n\n"
                        f"<b>Tap-to-Copy Email Body:</b>\n"
                        f"{monospaced_body}"
                    )
                    send_telegram_message(chat_id, card_response, callback_query_id=callback_query_id)
                else:
                    send_telegram_message(chat_id, "⚠️ Job cache expired. Please re-run pipeline with /t.", callback_query_id=callback_query_id)
                return
            elif cb_action == "apply":
                if not cb_arg:
                    answer_callback_query(callback_query_id, "⚠️ Malformed button data.", show_alert=True)
                    return
                message_id = cb["message"].get("message_id")
                original_text = html.escape(cb["message"].get("text", ""))
                today_str = datetime.now().strftime("%Y-%m-%d")
                updated_text = f"{original_text}\n\n✅ <b>Applied - {today_str}</b>"
                edit_telegram_message(chat_id, message_id, updated_text, reply_markup={"inline_keyboard": []})
                log_metric_event("applied", get_sheet_uuid_by_short_id(cb_arg))
                log_daily_activity("applied_count")
                answer_callback_query(callback_query_id, "✅ Marked as Applied")
            elif cb_action == "pivot":
                if not cb_arg:
                    answer_callback_query(callback_query_id, "⚠️ Malformed button data.", show_alert=True)
                    return
                short_id = cb_arg
                job = get_job_from_cache(short_id)
                comp = job.get("employer_name", "Target Firm") if job else "Target Firm"
                message_id = cb["message"].get("message_id")
                original_text = html.escape(cb["message"].get("text", ""))
                apollo_url = html.escape(build_apollo_url(comp), quote=True)
                updated_text = f"{original_text}\n\n🔄 <b>Pivoted</b> - <a href='{apollo_url}'>Apollo Leads</a>"
                edit_telegram_message(chat_id, message_id, updated_text)
                answer_callback_query(callback_query_id, f"🔄 Pivoted for {comp}")
            elif cb_action == "dead":
                message_id = cb["message"].get("message_id")
                original_text = html.escape(cb["message"].get("text", ""))
                updated_text = f"{original_text}\n\n❌ <b>Archived to Dead</b>"
                edit_telegram_message(chat_id, message_id, updated_text, reply_markup={"inline_keyboard": []})
                answer_callback_query(callback_query_id, "❌ Archived to Dead")
            elif cb_action == "bump":
                if not cb_arg:
                    answer_callback_query(callback_query_id, "⚠️ Malformed button data.", show_alert=True)
                    return
                contact = get_contact_by_sheet_uuid(cb_arg)
                if not contact:
                    answer_callback_query(callback_query_id, "⚠️ Contact not found.", show_alert=True)
                    return
                contact_name = contact.get("name") or ""
                contact_company = contact.get("company") or "Target Firm"
                to_email = resolve_target_email(contact_company).split(" [")[0]  # strip fallback-warning suffix
                bump_body = generate_bump_email(contact_name)
                try:
                    access_token = get_gmail_access_token()
                    if access_token:
                        message = EmailMessage()
                        message["To"] = to_email
                        message["From"] = GMAIL_USER
                        message["Subject"] = f"Following Up - {contact_company}"
                        message.set_content(bump_body)
                        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
                        draft_url = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
                        gmail_headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
                        res = requests.post(draft_url, headers=gmail_headers, json={"message": {"raw": raw_message}}, timeout=10)
                        if res.status_code in [200, 201]:
                            log_daily_activity("drafts_staged")
                            answer_callback_query(callback_query_id, f"📨 Bump draft staged for {contact_name or contact_company}")
                        else:
                            answer_callback_query(callback_query_id, "⚠️ Gmail draft failed.", show_alert=True)
                    else:
                        answer_callback_query(callback_query_id, "⚠️ Gmail auth unavailable.", show_alert=True)
                except Exception as e:
                    logging.error(f"Bump Draft Error: {e}")
                    answer_callback_query(callback_query_id, "❌ Error staging bump draft.", show_alert=True)
            elif cb_data.startswith("adj_pay_"):
                delta = cb_data.replace("adj_pay_", "")
                res = update_filter_param("min_salary", delta)
                send_telegram_message(chat_id, res, callback_query_id=callback_query_id)
            elif cb_data == "add_city_novi":
                res = update_filter_param("valid_cities", "+ novi")
                send_telegram_message(chat_id, res, callback_query_id=callback_query_id)
            elif cb_data == "reset_filters":
                init_db()
                send_telegram_message(chat_id, "<b>Search filters reset to default parameters.</b>", callback_query_id=callback_query_id)
            elif cb_data == "noop":
                # Placeholder button shown while a draft/action is staging - just clear the spinner
                answer_callback_query(callback_query_id, "⏳ Already in progress...")
            else:
                send_telegram_message(chat_id, "⚠️ Unrecognized button action.", callback_query_id=callback_query_id)
            return

        if "message" not in data:
            return

        msg = data["message"]
        chat_id = msg["chat"]["id"]
        raw_text = msg.get("text", "").strip()
        text = re.sub(r"@\w+bot", "", raw_text, flags=re.IGNORECASE).strip()
        today_str = datetime.now().strftime("%Y-%m-%d")

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
            qty = safe_int(m.group(2), 2)
            target_code = "CW" if cmd_type in ["c", "cw"] else "TC"
            cards = fetch_networking_cards(target_code, qty)
            if not cards:
                send_telegram_message(chat_id, f"No active networking cards found for <code>/{cmd_type}</code>.")
                return
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
            contacts = []
            if CRM_WEBHOOK_URL:
                try:
                    resp = requests.get(f"{CRM_WEBHOOK_URL}?action=get_priority&level={priority_lvl}", timeout=10).json()
                    contacts = resp.get("contacts", [])
                except Exception:
                    contacts = []
            if not contacts:
                send_telegram_message(chat_id, f"No active contacts found at Priority Tier {priority_lvl}.")
                return
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
            inline_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "➕ Pay +$5k", "callback_data": "adj_pay_+5000"},
                        {"text": "➖ Pay -$5k", "callback_data": "adj_pay_-5000"}
                    ],
                    [
                        {"text": "📍 Add Novi", "callback_data": "add_city_novi"},
                        {"text": "🔄 Reset Filters", "callback_data": "reset_filters"}
                    ]
                ]
            }
            send_telegram_message(chat_id, card_text, reply_markup=inline_keyboard)
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
            bump_buttons = []
            for item in overdue[:3]:
                comp = html.escape(str(item.get("company") or "N/A"))
                name = html.escape(str(item.get("name") or "N/A"))
                lines.append(f"• <b>{comp}</b> - {name} | {item['days_overdue']}d overdue | <code>/f 7</code>")
                if item["days_overdue"] >= 5 and item.get("sheet_uuid"):
                    raw_label = str(item.get("name") or item.get("company") or "Contact")[:20]
                    bump_buttons.append([{"text": f"📨 Bump {raw_label}", "callback_data": f"bump:{item['sheet_uuid']}"}])
            reply_markup = {"inline_keyboard": bump_buttons} if bump_buttons else None
            send_telegram_message(chat_id, "\n".join(lines), reply_markup=reply_markup)
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

        # 9. Swipe-Reply CRM Actions (/f, /n, /pivot, /tw, /cw, /cc, /tc, /x, /e) - require reply context
        if text.startswith("/f ") or text == "/f":
            mapping = resolve_reply_mapping(msg, chat_id, "/f")
            if not mapping:
                return
            parts = text.split()
            days = safe_int(parts[1], 7) if len(parts) > 1 else 7
            next_followup = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
            label = html.escape(mapping.get("contact_company") or mapping.get("contact_name") or "record")
            # Optimistic UI: confirm to Telegram first, dispatch the Sheets write in the background
            send_telegram_message(chat_id, f"📅 Follow-up for {label} snoozed to {next_followup}.")
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
            # Optimistic UI: confirm to Telegram first, dispatch the Sheets write in the background
            send_telegram_message(chat_id, f"📝 Note logged: <code>{timestamped_note}</code>")
            enqueue_crm_payload(build_crm_payload("append_note", sheet_uuid=mapping["sheet_uuid"], note=timestamped_note))
            log_daily_activity("notes_logged")
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

            ok, gmail_msg, draft_id = create_gmail_draft(to_email=new_email, company_name=comp, job_title=title, is_warm=is_warm)
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

        cv_match = re.match(r"^/(cv|resume)(?:\s+([ab]))?$", text, re.IGNORECASE)
        if cv_match:
            track = (cv_match.group(2) or "a").lower()
            mapping = resolve_reply_mapping(msg, chat_id, cv_match.group(0).split()[0])
            if not mapping:
                return
            job = get_job_by_sheet_uuid(mapping["sheet_uuid"])

            # Fallback to the networking-record mapping (e.g. /quick contacts with no cached job) instead of blocking
            comp = job.get("employer_name") or mapping.get("contact_company") or "Target Company"
            bullets = job.get("ats_bullets", [])
            short_id = job.get("short_id") or generate_short_key(job.get("job_id") or mapping["sheet_uuid"])

            try:
                pdf_bytes = compile_resume_pdf(comp, bullets, track=track)
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

        if text in ["/pivot", "/tw", "/cw", "/cc", "/tc", "/x"]:
            mapping = resolve_reply_mapping(msg, chat_id, text)
            if not mapping:
                return
            sheet_uuid = mapping["sheet_uuid"]

            if text == "/pivot":
                comp = mapping.get("contact_company") or "Target Firm"
                send_telegram_message(chat_id, f"🔄 Lead pivoted for {html.escape(comp)}.\nApollo: {html.escape(build_apollo_url(comp), quote=True)}")
                return

            if text == "/x":
                # Archive to the pipeline-appropriate tab: Carmen leads -> Killed, Tetiana leads -> Died
                source_tab = mapping.get("sheet_tab") or ""
                new_tab = "Killed" if source_tab in ("Carmen Warm", "Carmen Cold") else "Died"
                # Optimistic UI: confirm to Telegram first, dispatch the Sheets write in the background
                send_telegram_message(chat_id, f"⚡ ❌ Archived lead to {new_tab}.")
                enqueue_crm_payload(build_crm_payload("update_status", sheet_uuid=sheet_uuid, new_tab=new_tab))
                return

            new_tab_map = {
                "/tw": "Carmen Warm",
                "/cw": "Carmen Warm",
                "/cc": "Tetiana Cold",
                "/tc": "Tetiana Cold"
            }
            new_tab = new_tab_map[text]

            confirm_map = {
                "/tw": "🔄 Moved lead to Carmen Warm.",
                "/cw": "🔄 Moved lead to Carmen Warm.",
                "/cc": "🔄 Logged lead to Tetiana Cold.",
                "/tc": "🔄 Logged lead to Tetiana Cold."
            }
            # Optimistic UI: confirm to Telegram first, dispatch the Sheets write in the background
            send_telegram_message(chat_id, f"⚡ {confirm_map[text]}")
            enqueue_crm_payload(build_crm_payload("update_status", sheet_uuid=sheet_uuid, new_tab=new_tab))
            return

    except Exception as e:
        logging.error(f"Async Webhook Processing Error: {e}")

def webhook_worker_loop():
    """Daemon worker: pulls payloads off the bounded queue and processes them serially per-thread."""
    while True:
        data = WEBHOOK_QUEUE.get()
        try:
            process_webhook_payload_async(data)
        except Exception as e:
            logging.error(f"Webhook Worker Error: {e}")
        finally:
            WEBHOOK_QUEUE.task_done()

def start_webhook_workers():
    """Spin up a fixed pool of daemon threads instead of an unbounded thread-per-request model."""
    for _ in range(WEBHOOK_WORKER_COUNT):
        threading.Thread(target=webhook_worker_loop, daemon=True).start()

start_webhook_workers()
start_gmail_poller()
start_crm_outbox_worker()
start_morning_digest()

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

@app.route("/telegram", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    """
    Instant non-blocking execution (<0.05s return).
    Validates Telegram's secret token header, then enqueues onto a bounded queue
    processed by a fixed daemon worker pool instead of spawning a thread per request.
    """
    webhook_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if webhook_secret:
        incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if incoming_secret != webhook_secret:
            logging.warning("Telegram Webhook Rejected: invalid secret token")
            return jsonify({"status": "error", "message": "Unauthorized"}), 403

    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "ignored"}), 200
        try:
            WEBHOOK_QUEUE.put_nowait(data)
        except queue.Full:
            logging.warning("Webhook Queue Full: dropping payload")
            return jsonify({"status": "error", "message": "Server busy, queue full"}), 503
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logging.error(f"Telegram Webhook Dispatch Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 200

@app.route("/stage/<short_id>", methods=["GET"])
def desktop_stage_view(short_id):
    """Desktop review page showing tailored bullets, apply portal link, and PDF preview."""
    job = get_job_from_cache(short_id)
    if not job:
        return "<h3>Job not found or cache expired.</h3>", 404

    track = request.args.get("track", "a")
    comp = job.get("employer_name", "Target Firm")
    title = job.get("job_title", "Role")
    apply_link = job.get("job_apply_link", "#")
    bullets = job.get("ats_bullets", [])
    bullets_html = "".join([f"<li>{html.escape(str(b))}</li>" for b in bullets])

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
        </div>
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
    track = request.args.get("track", "a")
    comp = job.get("employer_name", "Target Firm")
    bullets = job.get("ats_bullets", [])
    pdf_bytes = compile_resume_pdf(comp, bullets, track=track)
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
                    ats_bullets=result.get("ats_bullets")
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
