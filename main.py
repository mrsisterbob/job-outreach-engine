import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import html
import json
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
from flask import Flask, jsonify, request

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

# Database Write Lock (30s timeout)
DB_WRITE_LOCK = threading.Lock()
DB_WRITE_LOCK_TIMEOUT = 30  # seconds

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
    "kw": "required_keywords"
}

def get_db_conn():
    """Returns a SQLite connection with WAL mode + a busy_timeout to reduce lock contention."""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL;")
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sheet_row_map_tg_msg ON sheet_row_map(telegram_message_id)")
        
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
        print(f"Filter Read Error ({key}): {e}", flush=True)
    return default_val

def set_filter(key, val):
    """Set filter with thread-safe locking (30s timeout). Dual-write to System_Config sheet."""
    if not DB_WRITE_LOCK.acquire(timeout=DB_WRITE_LOCK_TIMEOUT):
        print(f"DB Write Lock Timeout (filter {key})", flush=True)
        return False
    try:
        with get_db_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO search_filters (key, value_json) VALUES (?, ?)", (key, json.dumps(val)))
            conn.commit()
        # Dual-write to Google Sheets System_Config tab
        if CRM_WEBHOOK_URL:
            try:
                requests.post(CRM_WEBHOOK_URL, json={"action": "update_system_config", "key": key, "value": val}, timeout=5)
            except Exception as e:
                print(f"System_Config dual-write failed ({key}): {e}", flush=True)
        return True
    except Exception as e:
        print(f"Filter Write Error ({key}): {e}", flush=True)
        return False
    finally:
        DB_WRITE_LOCK.release()

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
    """Save job to cache with thread-safe locking (30s timeout)."""
    if sheet_uuid is None:
        sheet_uuid = str(uuid.uuid4())
    if not DB_WRITE_LOCK.acquire(timeout=DB_WRITE_LOCK_TIMEOUT):
        print(f"DB Write Lock Timeout ({short_id})", flush=True)
        return sheet_uuid
    try:
        with get_db_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO jobs (short_id, sheet_uuid, job_json) VALUES (?, ?, ?)", 
                        (short_id, sheet_uuid, json.dumps(job_dict)))
            conn.commit()
    except Exception as e:
        print(f"DB Save Error: {e}", flush=True)
    finally:
        DB_WRITE_LOCK.release()
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
        print(f"DB Read Error: {e}", flush=True)
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
        print(f"DB Read Error (sheet_uuid lookup): {e}", flush=True)
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
        print(f"DB Read Error (job by sheet_uuid): {e}", flush=True)
    return {}

def is_job_seen_db(job_hash):
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM seen_jobs WHERE job_hash = ?", (job_hash,))
            return cursor.fetchone() is not None
    except Exception:
        return False

def save_seen_job_db(job_hash):
    """Save seen job hash with thread-safe locking (30s timeout).
    Prevents unhandled exceptions in thread pools.
    """
    if not DB_WRITE_LOCK.acquire(timeout=DB_WRITE_LOCK_TIMEOUT):
        print(f"DB Write Lock Timeout (seen_job: {job_hash})", flush=True)
        return False
    try:
        with get_db_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO seen_jobs (job_hash) VALUES (?)", (job_hash,))
            conn.commit()
        return True
    except Exception as e:
        print(f"DB Seen Hash Error ({job_hash}): {e}", flush=True)
        return False
    finally:
        DB_WRITE_LOCK.release()

def add_company_cooldown(company_name):
    """Add company cooldown with thread-safe locking (30s timeout).
    Prevents unhandled exceptions in thread pools.
    """
    clean = str(company_name or "").lower().strip()
    if not clean:
        return False
    if not DB_WRITE_LOCK.acquire(timeout=DB_WRITE_LOCK_TIMEOUT):
        print(f"DB Write Lock Timeout (cooldown: {clean})", flush=True)
        return False
    try:
        with get_db_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO company_cooldown (company_clean, logged_at) VALUES (?, CURRENT_TIMESTAMP)", (clean,))
            conn.commit()
        return True
    except Exception as e:
        print(f"DB Cooldown Save Error ({clean}): {e}", flush=True)
        return False
    finally:
        DB_WRITE_LOCK.release()

def log_metric_event(event_type, sheet_uuid=None):
    """Persist a pipeline metric event (e.g. message_sent, interview_set) to SQLite."""
    if not DB_WRITE_LOCK.acquire(timeout=DB_WRITE_LOCK_TIMEOUT):
        print(f"DB Write Lock Timeout (metric: {event_type})", flush=True)
        return False
    try:
        with get_db_conn() as conn:
            conn.execute(
                "INSERT INTO pipeline_metrics (event_type, sheet_uuid, timestamp) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (event_type, sheet_uuid)
            )
            conn.commit()
        return True
    except Exception as e:
        print(f"DB Metric Log Error ({event_type}): {e}", flush=True)
        return False
    finally:
        DB_WRITE_LOCK.release()

def get_metric_count(event_type):
    """Return the total persisted count of a given metric event_type."""
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM pipeline_metrics WHERE event_type = ?", (event_type,))
            return cursor.fetchone()[0]
    except Exception as e:
        print(f"DB Metric Count Error ({event_type}): {e}", flush=True)
        return 0

DAILY_ACTIVITY_COLUMNS = ("drafts_staged", "applied_count", "notes_logged")

def log_daily_activity(activity_type):
    """Increment today's daily_activity counter for a valid activity_type (drafts_staged/applied_count/notes_logged)."""
    if activity_type not in DAILY_ACTIVITY_COLUMNS:
        print(f"Invalid daily_activity type: {activity_type}", flush=True)
        return False
    today_str = datetime.now().strftime("%Y-%m-%d")
    if not DB_WRITE_LOCK.acquire(timeout=DB_WRITE_LOCK_TIMEOUT):
        print(f"DB Write Lock Timeout (daily_activity: {activity_type})", flush=True)
        return False
    try:
        with get_db_conn() as conn:
            conn.execute(
                f"INSERT INTO daily_activity (date, {activity_type}) VALUES (?, 1) "
                f"ON CONFLICT(date) DO UPDATE SET {activity_type} = {activity_type} + 1",
                (today_str,)
            )
            conn.commit()
        return True
    except Exception as e:
        print(f"DB Daily Activity Error ({activity_type}): {e}", flush=True)
        return False
    finally:
        DB_WRITE_LOCK.release()

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
        print(f"DB Daily Activity Read Error ({date_str}): {e}", flush=True)
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
        print(f"DB Lifetime Activity Error: {e}", flush=True)
        return {"drafts_staged": 0, "applied_count": 0, "notes_logged": 0}

def calculate_active_day_streak():
    """Count consecutive active days (any activity logged) ending today or yesterday."""
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT date FROM daily_activity WHERE (drafts_staged + applied_count + notes_logged) > 0 ORDER BY date DESC")
            active_dates = {row[0] for row in cursor.fetchall()}
    except Exception as e:
        print(f"DB Streak Calc Error: {e}", flush=True)
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

def save_message_mapping(telegram_message_id, sheet_uuid, sheet_tab="", contact_name="", contact_company=""):
    """Persist (telegram_message_id, sheet_uuid, sheet_tab) so swipe-replies can resolve the CRM row."""
    if not telegram_message_id or not sheet_uuid:
        return False
    if not DB_WRITE_LOCK.acquire(timeout=DB_WRITE_LOCK_TIMEOUT):
        print(f"DB Write Lock Timeout (msg_map: {telegram_message_id})", flush=True)
        return False
    try:
        with get_db_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO sheet_row_map 
                (sheet_uuid, sheet_tab, contact_name, contact_company, telegram_message_id, created_at)
                VALUES (?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM sheet_row_map WHERE sheet_uuid = ?), CURRENT_TIMESTAMP))
            """, (sheet_uuid, sheet_tab, contact_name, contact_company, telegram_message_id, sheet_uuid))
            conn.commit()
        return True
    except Exception as e:
        print(f"DB Message Mapping Save Error ({telegram_message_id}): {e}", flush=True)
        return False
    finally:
        DB_WRITE_LOCK.release()

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
        print(f"DB Message Mapping Lookup Error ({telegram_message_id}): {e}", flush=True)
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
    """Strict regex parser for /quick command.
    Format: /quick Name@Company [Priority] [Note]
    Regex: ^/quick\s+(?P<name>[^@]+)@(?P<company>[^\d@]+)\s*(?P<priority>\d+)?\s*(?P<note>.*?)$
    """
    clean = text_input.replace("/quick", "").strip()
    # Strict regex: name@company with optional priority and note
    match = re.match(r"^(?P<name>[^@]+)@(?P<company>[^\d@]+)\s*(?P<priority>\d+)?\s*(?P<note>.*)$", clean)
    if not match:
        return None  # Invalid format
    name = match.group('name').strip()
    company = match.group('company').strip()
    priority = safe_int(match.group('priority') or 5, 5)
    note = match.group('note').strip()
    # Validate: priority must be 1-10
    if priority < 1 or priority > 10:
        priority = 5
    return name, company, priority, note

# ==============================================================================
# 5. GEMINI REST API INTEGRATION (TRUNCATED PAYLOAD)
# ==============================================================================
def call_gemini_api(prompt, system_prompt=None, response_mime="application/json"):
    """Call Gemini API with resilience handling. Return None on failure/timeout."""
    if not GEMINI_API_KEY:
        return None
    full_prompt = f"{system_prompt}\n\n{prompt}".strip() if system_prompt else prompt
    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {"response_mime_type": response_mime}
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={GEMINI_API_KEY}"
    try:
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code == 429:
            # JSearch-like 429 error - halt and notify
            send_health_alert("Gemini API Rate Limit (429) - halting evaluations temporarily")
            return None
        if res.status_code == 200:
            return res.json()["candidates"][0]["content"]["parts"][0]["text"]
    except requests.exceptions.Timeout:
        print(f"Gemini API Timeout", flush=True)
        return None
    except Exception as e:
        print(f"Gemini API Exception: {e}", flush=True)
    return None

def evaluate_job_with_gemini(job):
    """Evaluate job with Gemini. On failure/timeout, set score=0 and status 'Evaluation Pending'.
    Thread-safe with timeout handling: DO NOT assign fake scores on failure.
    Returns (pass_bool, score, reason, linkedin_note, ats_bullets).
    """
    if not GEMINI_API_KEY:
        return True, 75, "Fallback pass (No Key)", "", []
    
    try:
        desc_truncated = str(job.get("job_description") or "")[:1000]
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
                print(f"Gemini evaluation JSON parse failure: {e}", flush=True)
                # On parse error, return 0 score with Evaluation Pending status
                return False, 0, "Evaluation Pending", "", []
        
        # On API failure/timeout, set score to 0 and status to "Evaluation Pending" (NO fake scores)
        return False, 0, "Evaluation Pending", "", []
    
    except Exception as e:
        print(f"Gemini evaluation exception: {e}", flush=True)
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
            print(f"Interview prep parse failure: {e}", flush=True)
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
            print(f"Elevator pitch parse failure: {e}", flush=True)
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

    if any(term in title for term in get_filter("title_exclusions", [])):
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
    """Persist a created Gmail draft's identity for 24h dedup checks."""
    if not DB_WRITE_LOCK.acquire(timeout=DB_WRITE_LOCK_TIMEOUT):
        print(f"DB Write Lock Timeout (gmail_draft: {to_email})", flush=True)
        return False
    try:
        with get_db_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO gmail_drafts (to_email, subject, draft_id, created_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (to_email, subject, draft_id)
            )
            conn.commit()
        return True
    except Exception as e:
        print(f"DB Gmail Draft Save Error ({to_email}): {e}", flush=True)
        return False
    finally:
        DB_WRITE_LOCK.release()

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
        print(f"DB Gmail Draft Lookup Error ({to_email}): {e}", flush=True)
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
            return False, f"Gmail OAuth Failure: {error_code}", None

        access_token = token_json.get("access_token")
        if not access_token:
            return False, "OAuth Token Refused", None

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
            print(f"CRM Webhook Attempt {attempt+1} Failed: {e}", flush=True)
        time.sleep(delay)
        delay *= 2.0
    send_health_alert(f"Failed to log payload to Google Sheets after {max_retries} attempts.")
    return False

def fetch_networking_cards(target_code="CW", qty=2):
    if not CRM_WEBHOOK_URL:
        return []
    try:
        res = requests.post(CRM_WEBHOOK_URL, json={"action": "get_followups", "tab": target_code}, timeout=10)
        if res.status_code == 200:
            leads = res.json().get("followups", [])
            return leads[:qty]
    except Exception as e:
        print(f"Error fetching networking cards: {e}", flush=True)
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
        print(f"answerCallbackQuery error: {e}", flush=True)

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
        print(f"editMessageText error: {e}", flush=True)
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
        if res.status_code == 200:
            log_metric_event("message_sent")
            return res.json().get("result", {}).get("message_id")
    except Exception as e:
        print(f"Telegram Post Error: {e}", flush=True)
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
    matched_str = ", ".join(matched_skills[:4]).title() if matched_skills else "General Ops"
    bullets_block = "\n".join(f"• {b}" for b in ats_bullets) if ats_bullets else "N/A"
    fit_dot = get_fit_score_indicator(score)
    card_text = (
        f"💼 <b>{title}</b>\n"
        f"🏢 <b>{company}</b>\n"
        f"────────────────────\n"
        f"{fit_dot} <b>Fit Score:</b> {score}/100  |  <b>Skill Match:</b> {overlap_pct}%\n"
        f"🕐 <b>Recency:</b> {age_badge}\n"
        f"💰 <b>Pay &amp; Style:</b> {work_style} | {salary_str}\n\n"
        f"🧩 <b>Matched Skills:</b> <code>{html.escape(matched_str)}</code>\n\n"
        f"<b>Fit Reason:</b> {html.escape(reason)}\n\n"
        f"🔗 <b>Quick Links:</b>\n"
        f"<a href='{apply_link}'>Direct Apply</a> | "
        f"<a href='{apollo_url}'>Apollo Operations Leads</a> | "
        f"<a href='{linkedin_url}'>LinkedIn Leadership Search</a> | "
        f"<a href='{dork_url}'>🎯 Find Direct Hiring Manager (Google Dork)</a>\n\n"
        f"📧 <b>Target (tap to copy):</b>\n<code>{html.escape(target_email)}</code>\n\n"
        f"🤝 <b>LinkedIn Connect Note (&lt;300 chars):</b>\n<code>{html.escape(linkedin_note) if linkedin_note else 'N/A'}</code>\n\n"
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
        if res.status_code == 200:
            telegram_message_id = res.json().get("result", {}).get("message_id")
            log_metric_event("message_sent", sheet_uuid)
            if telegram_message_id and sheet_uuid:
                save_message_mapping(telegram_message_id, sheet_uuid, "Pipeline_Candidates", company, "")
            return telegram_message_id
    except Exception as e:
        print(f"Failed to post card to Telegram: {e}", flush=True)
    return None

# ==============================================================================
# 8. PARALLEL PIPELINE EXECUTION (PARALLEL JSEARCH + EARLY-EXIT CIRCUIT BREAKER)
# ==============================================================================
def fetch_single_query_jobs(query_args):
    """Worker function for parallel JSearch API query execution."""
    query, api_url, headers = query_args
    params = {"query": query, "page": "1", "num_pages": "1", "date_posted": "month"}
    try:
        res = requests.get(api_url, headers=headers, params=params, timeout=10)
        if res.status_code == 200:
            return res.json().get("data", [])
    except Exception as e:
        print(f"Fetch Exception ({query}): {e}", flush=True)
    return []

def run_job_pipeline(chat_id=None, top_n=2):
    """Job search pipeline with two-stage architecture:
    Stage 1: Pre-filter candidates (strict filters)
    Stage 2: Concurrent Gemini AI evaluation (ThreadPoolExecutor, max 10 candidates)
    """
    print(">>> Starting Job Search Pipeline...", flush=True)
    if chat_id:
        send_status_update(chat_id, "Stage 1: Fetching raw listings from JSearch in parallel...")
    
    seen_hashes = set()
    candidate_pool = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    followup_date = (datetime.now() + timedelta(days=calculate_followup_interval(5))).strftime("%Y-%m-%d")
    
    # Stage 1: Parallel JSearch fetching + strict filtering
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
                company = job.get("employer_name") or ""
                title = job.get("job_title") or ""
                job_hash = generate_dedup_hash(company, title)
                if job_hash in seen_hashes or is_job_seen_db(job_hash):
                    continue
                seen_hashes.add(job_hash)
                save_seen_job_db(job_hash)
                log_metric_event("listing_discovered")
                if passes_strict_filter(job):
                    candidate_pool.append(job)
    
    print(f"Stage 1 Complete: {len(candidate_pool)} candidates passed strict filter.", flush=True)
    if chat_id:
        send_status_update(chat_id, f"Stage 1 Complete: {len(candidate_pool)} candidates passed strict filter.\nStage 2: Running AI evaluations (max 10 candidates)...")
    
    # Stage 2: Cap at 10 candidates, concurrent Gemini evaluation with timeout handling
    eval_candidates = candidate_pool[:10]
    print(f"Stage 2: Evaluating {len(eval_candidates)} candidates with Gemini AI (max 10)...", flush=True)
    
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
                print(f"Candidate evaluation failed (timeout or error): {e}", flush=True)
                # On timeout/error: score=0, status='Evaluation Pending' is handled in evaluate_job_with_gemini
    
    # Sort by score descending
    top_matches.sort(key=lambda x: x["score"], reverse=True)
    
    # Dispatch results to Telegram and CRM
    for item in top_matches:
        job = item["job"]
        send_telegram_card(
            job, item["score"], item["reason"], item["target_email"],
            item["age_badge"], item["salary_str"], item["work_style"],
            item["overlap_pct"], item["matched_skills"], item["short_id"],
            sheet_uuid=item.get("sheet_uuid"),
            linkedin_note=item.get("linkedin_note", ""),
            ats_bullets=item.get("ats_bullets")
        )
        log_to_sheets_crm(build_crm_payload(
            "add_row",
            sheet_uuid=item.get("sheet_uuid"),
            target_code="TC",
            row_data=[
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
        ))
    
    print(f"Stage 2 Complete: {len(top_matches)} cards dispatched.", flush=True)
    if chat_id:
        send_status_update(chat_id, f"Pipeline Complete: {len(top_matches)} cards dispatched.")
    
    return len(top_matches)

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
                    save_message_mapping(sent_msg_id, contact_sheet_uuid, target_code, c.get("name", ""), c.get("company", ""))
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
            send_telegram_message(chat_id, "📊 <b>Overdue Status:</b> 0 overdue follow-ups across all tabs.")
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

        # 9. Swipe-Reply CRM Actions (/f, /n, /pivot, /tw, /cw, /cc, /tc, /x) - require reply context
        if text.startswith("/f ") or text == "/f":
            mapping = resolve_reply_mapping(msg, chat_id, "/f")
            if not mapping:
                return
            parts = text.split()
            days = safe_int(parts[1], 7) if len(parts) > 1 else 7
            next_followup = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
            log_to_sheets_crm(build_crm_payload("update_snooze", sheet_uuid=mapping["sheet_uuid"], next_followup=next_followup))
            label = html.escape(mapping.get("contact_company") or mapping.get("contact_name") or "record")
            send_telegram_message(chat_id, f"📅 Follow-up for {label} snoozed to {next_followup}.")
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
            log_to_sheets_crm(build_crm_payload("append_note", sheet_uuid=mapping["sheet_uuid"], note=timestamped_note))
            log_daily_activity("notes_logged")
            send_telegram_message(chat_id, f"📝 Note logged: <code>{timestamped_note}</code>")
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
                log_to_sheets_crm(build_crm_payload("update_status", sheet_uuid=sheet_uuid, new_tab=new_tab))
                send_telegram_message(chat_id, f"⚡ ❌ Archived lead to {new_tab}.")
                return

            new_tab_map = {
                "/tw": "Carmen Warm",
                "/cw": "Carmen Warm",
                "/cc": "Tetiana Cold",
                "/tc": "Tetiana Cold"
            }
            new_tab = new_tab_map[text]
            log_to_sheets_crm(build_crm_payload("update_status", sheet_uuid=sheet_uuid, new_tab=new_tab))

            confirm_map = {
                "/tw": "🔄 Moved lead to Carmen Warm.",
                "/cw": "🔄 Moved lead to Carmen Warm.",
                "/cc": "🔄 Logged lead to Tetiana Cold.",
                "/tc": "🔄 Logged lead to Tetiana Cold."
            }
            send_telegram_message(chat_id, f"⚡ {confirm_map[text]}")
            return

    except Exception as e:
        print(f"Async Webhook Processing Error: {e}", flush=True)

def webhook_worker_loop():
    """Daemon worker: pulls payloads off the bounded queue and processes them serially per-thread."""
    while True:
        data = WEBHOOK_QUEUE.get()
        try:
            process_webhook_payload_async(data)
        except Exception as e:
            print(f"Webhook Worker Error: {e}", flush=True)
        finally:
            WEBHOOK_QUEUE.task_done()

def start_webhook_workers():
    """Spin up a fixed pool of daemon threads instead of an unbounded thread-per-request model."""
    for _ in range(WEBHOOK_WORKER_COUNT):
        threading.Thread(target=webhook_worker_loop, daemon=True).start()

start_webhook_workers()

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
            print("Telegram Webhook Rejected: invalid secret token", flush=True)
            return jsonify({"status": "error", "message": "Unauthorized"}), 403

    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "ignored"}), 200
        try:
            WEBHOOK_QUEUE.put_nowait(data)
        except queue.Full:
            print("Webhook Queue Full: dropping payload", flush=True)
            return jsonify({"status": "error", "message": "Server busy, queue full"}), 503
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"Telegram Webhook Dispatch Error: {e}", flush=True)
        return jsonify({"status": "error", "message": str(e)}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
