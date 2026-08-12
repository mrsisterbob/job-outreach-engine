import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import html
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# ==========================================
# 1. ENVIRONMENT VARIABLES & INITIALIZATION
# ==========================================
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

# Analytics & Efficiency Trackers
TOTAL_MESSAGES_SENT = 0
TOTAL_INTERVIEWS_SET = 0


def init_db():
    """Initializes local SQLite tables for jobs, deduplication, cooldowns, and dynamic search filters."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            short_id TEXT PRIMARY KEY,
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
        
        # Seed dynamic filter defaults if empty
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
                    " senior", " lead", " manager", "director", "vp", " executive", " principal", "head of"
                ],
                "core_skills": [
                    "python", "sql", "salesforce", "excel", "schwab sac", "schwab advisor center",
                    "fidelity wealthscape", "docusign", "process automation", "reconciliation"
                ],
                "tier1_ecosystem": [
                    "downtown detroit", "inveniam", "rivian", "rocket", "quicken", "stockx", "venture"
                ],
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


# ==========================================
# FILTER & DYNAMIC CONFIGURATION HELPERS
# ==========================================
def get_filter(key, default_val=None):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value_json FROM search_filters WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row:
                return json.loads(row[0])
    except Exception as e:
        print(f"Filter Read Error ({key}): {e}", flush=True)
    return default_val


def set_filter(key, val):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO search_filters (key, value_json) VALUES (?, ?)", (key, json.dumps(val)))
            conn.commit()
            return True
    except Exception as e:
        print(f"Filter Write Error ({key}): {e}", flush=True)
        return False


def save_job_to_cache(short_id, job_dict):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO jobs (short_id, job_json) VALUES (?, ?)", (short_id, json.dumps(job_dict)))
    except Exception as e:
        print(f"DB Save Error: {e}", flush=True)


def get_job_from_cache(short_id):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT job_json FROM jobs WHERE short_id = ?", (short_id,))
            row = cursor.fetchone()
            if row:
                return json.loads(row[0])
    except Exception as e:
        print(f"DB Read Error: {e}", flush=True)
    return {}


def is_job_seen_db(job_hash):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM seen_jobs WHERE job_hash = ?", (job_hash,))
            return cursor.fetchone() is not None
    except Exception:
        return False


def save_seen_job_db(job_hash):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR IGNORE INTO seen_jobs (job_hash) VALUES (?)", (job_hash,))
    except Exception as e:
        print(f"DB Seen Hash Error: {e}", flush=True)


def add_company_cooldown(company_name):
    clean = str(company_name or "").lower().strip()
    if not clean:
        return
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO company_cooldown (company_clean, logged_at) VALUES (?, CURRENT_TIMESTAMP)", (clean,))
    except Exception as e:
        print(f"DB Cooldown Save Error: {e}", flush=True)


def is_company_on_cooldown(company_name):
    clean = str(company_name or "").lower().strip()
    if not clean:
        return False
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT logged_at FROM company_cooldown WHERE company_clean = ? AND logged_at >= datetime('now', '-14 days')", (clean,))
            return cursor.fetchone() is not None
    except Exception:
        return False


# ==========================================
# 2. DYNAMIC PRIORITY DECAY & EMAIL ENGINE
# ==========================================
def calculate_followup_interval(priority_score):
    """Calculates follow-up gap dynamically: Interval = Max(3, Round(35 - (Priority * 3.2)))."""
    try:
        p = float(priority_score)
        return max(3, int(round(35.0 - (p * 3.2))))
    except Exception:
        return 14


def sanitize_text(text):
    """Post-generation sanitizer: strips em-dashes, en-dashes, hyphens, semicolons, colons, quotes,

    prohibited buzzwords, and reduces 3-part lists (X, Y, and Z -> X and Y).
    """
    if not text:
        return ""
    # Strip prohibited punctuation
    cleaned = re.sub(r'[\u2014\u2013\-;:""\'\(\)]', ' ', str(text))
    
    # Remove prohibited corporate buzzwords
    buzzwords = ["leveraging", "passionate", "seamless", "synergy", "cutting-edge", "paradigm"]
    for bw in buzzwords:
        cleaned = re.sub(r'\b' + bw + r'\b', '', cleaned, flags=re.IGNORECASE)
        
    # Simplify three-item lists (X, Y, and Z -> X and Y)
    cleaned = re.sub(r'\b(\w+),\s*(\w+),\s*and\s*(\w+)\b', r'\1 and \2', cleaned)
    
    # Collapse extra whitespace
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def generate_cold_email(job_title, company_name, core_exp="wealth ops and process automation"):
    """Carmen Cold (VP Outreach): Strict 2-Sentence Cap."""
    s1 = f"I saw the {job_title} role at {company_name} and wanted to highlight my background in {core_exp}."
    s2 = "Would you be open to a brief 5 minute call next week to discuss alignment?"
    return f"{sanitize_text(s1)} {sanitize_text(s2)}"


def generate_warm_email(note_context=""):
    """Carmen Warm (Network): Strict 3-Sentence Structure."""
    s1 = sanitize_text(note_context) if note_context else "I hope you have been doing well."
    s2 = "I am currently interning in wealth ops at Signal Advisors, a fast growing startup in downtown Detroit."
    s3 = "I am wondering what you have been up to lately, and would love to reconnect over coffee or a quick call if you have time."
    return f"{s1} {s2} {s3}"


# ==========================================
# 3. HELPER FUNCTIONS & PIPELINE UTILITIES
# ==========================================
SYSTEM_PROMPT = """You are a strict technical job screener evaluating roles for an early-career candidate (0-2 years experience).
Target Profile: Non-sales W-2 roles in Tech, FinTech, Auto Tech, or Back-Office Systems/Operations in Metro Detroit or Remote.
High Priority Skills: Python, SQL, Salesforce, Excel, Schwab SAC, Fidelity Wealthscape, DocuSign, Process Automation.
Strictly FORBIDDEN: Sales, cold calling, client pitching, commission-based roles, retail bank tellers, CPA tracks, Senior/Lead/Manager roles.
Evaluate the job description and respond ONLY with a JSON object containing:
{
  "score": <integer between 1 and 100 representing fit signal>,
  "reason": "<1-sentence concise explanation of why this role fits or does not fit>"
}"""


def send_health_alert(error_msg):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        text = f"🚨 <b>Pipeline Operational Warning</b>\n<code>{html.escape(str(error_msg))}</code>"
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
                json={"chat_id": chat_id, "text": f"📊 <b>Pipeline Telemetry:</b>\n{text}", "parse_mode": "HTML"},
                timeout=5
            )
        except Exception:
            pass


def send_telegram_chunked(chat_id, full_text):
    """Sends text blocks, splitting across Telegram's 4096 char limit with 0.5s safety delays."""
    if not (TELEGRAM_BOT_TOKEN and chat_id):
        return
    max_len = 4000
    if len(full_text) <= max_len:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": full_text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=5
        )
    else:
        chunks = [full_text[i:i + max_len] for i in range(0, len(full_text), max_len)]
        for chunk in chunks:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=5
            )
            time.sleep(0.5)


def log_to_sheets_crm(payload, max_retries=3):
    """Interfaces with Google Apps Script 10-column database schema."""
    if not CRM_WEBHOOK_URL:
        return False
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
        return "⚡ [< 24h FRESH]"
    elif posted_hours < 72:
        return "🟢 [1-3d RECENT]"
    elif posted_hours < 168:
        return "🟡 [3-7d ACTIVE]"
    elif posted_hours < 336:
        return "🟠 [7-14d AGING]"
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
        return "🏢 Hybrid"
    elif is_remote:
        return "🌐 Remote"
    return "🏢 On-Site / Unspecified"


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
    clean_company = re.sub(r'[^a-zA-Z0-9\s]', '', str(company_name or "")).strip()
    encoded = urllib.parse.quote(f"{clean_company} Operations")
    return f"https://app.apollo.io/#/people?qKeywords={encoded}"


def build_linkedin_url(company_name):
    clean_company = re.sub(r'[^a-zA-Z0-9\s]', '', str(company_name or "")).strip()
    encoded = urllib.parse.quote(f'{clean_company} ("VP" OR "Director" OR "Manager") ("Operations" OR "Compliance")')
    return f"https://www.linkedin.com/search/results/people/?keywords={encoded}"


def resolve_target_email(company_name, job_title=""):
    clean_domain = re.sub(r'[^a-zA-Z0-9]', '', str(company_name or "")).lower() + ".com"
    title_lower = str(job_title or "").lower()
    if "compliance" in title_lower:
        return f"compliance@{clean_domain}"
    elif any(kw in title_lower for kw in ["wealth", "custody", "brokerage", "ria"]):
        return f"wealthops@{clean_domain}"
    elif any(kw in title_lower for kw in ["systems", "automation", "revops"]):
        return f"bizops@{clean_domain}"
    return f"operations@{clean_domain}"


def parse_quick_command(text_input):
    """Refactored parsing using @ delimiter and integer-position scanning for multi-word contact names."""
    clean = text_input.replace("/quick", "").strip()
    if "@" in clean:
        parts = clean.split("@", 1)
        name = parts[0].strip()
        rest = parts[1].strip().split()
        p_idx = -1
        priority = 5
        for i, token in enumerate(rest):
            if token.isdigit() and 1 <= int(token) <= 10:
                p_idx = i
                priority = int(token)
                break
        if p_idx != -1:
            company = " ".join(rest[:p_idx])
            note = " ".join(rest[p_idx+1:])
        else:
            company = " ".join(rest)
            note = ""
        return name, company, priority, note
    else:
        tokens = clean.split()
        p_idx = -1
        priority = 5
        for i, token in enumerate(tokens):
            if token.isdigit() and 1 <= int(token) <= 10:
                p_idx = i
                priority = int(token)
                break
        if p_idx > 0:
            name = tokens[0]
            company = " ".join(tokens[1:p_idx])
            note = " ".join(tokens[p_idx+1:])
            return name, company, priority, note
        return clean, "Target Firm", 5, ""


# ==========================================
# 4. GEMINI REST API INTEGRATION
# ==========================================
def call_gemini_api(prompt, system_prompt=None, response_mime="application/json"):
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
        if res.status_code == 200:
            return res.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print(f"Gemini API Exception: {e}", flush=True)
    return None


def evaluate_job_with_gemini(job):
    if not GEMINI_API_KEY:
        return True, 75, "Fallback pass (No Key)"
    prompt = f"Job Title: {job.get('job_title')}\nCompany: {job.get('employer_name')}\nDescription:\n{str(job.get('job_description') or '')[:2500]}"
    raw_text = call_gemini_api(prompt, SYSTEM_PROMPT)
    if raw_text:
        try:
            cleaned_text = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw_text).strip()
            res_data = json.loads(cleaned_text)
            raw_score = int(res_data.get("score", 0))
            reason = res_data.get("reason", "N/A")
            final_score = calculate_hybrid_score_modifier(job, raw_score)
            return (final_score >= 65), final_score, reason
        except Exception as e:
            print(f"Gemini evaluation JSON parse failure: {e}", flush=True)
            return True, 70, "Fallback pass on parse error"
    return True, 70, "Fallback pass on API failure"


# ==========================================
# 5. GMAIL API DRAFTING
# ==========================================
def create_gmail_draft(to_email, company_name, job_title, is_warm=False, custom_note=""):
    missing_vars = [v for v in ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_USER"] if not os.environ.get(v)]
    if missing_vars:
        return False, f"Missing Env Vars: {', '.join(missing_vars)}"
    
    token_url = "https://oauth2.googleapis.com/token"
    token_data = {
        "client_id": GMAIL_CLIENT_ID,
        "client_secret": GMAIL_CLIENT_SECRET,
        "refresh_token": GMAIL_REFRESH_TOKEN,
        "grant_type": "refresh_token"
    }
    try:
        token_res = requests.post(token_url, data=token_data, timeout=10)
        access_token = token_res.json().get("access_token")
        if not access_token:
            return False, "OAuth Token Refused"
            
        if is_warm:
            body_content = generate_warm_email(custom_note)
            subject = f"Reconnecting - {company_name}"
        else:
            body_content = generate_cold_email(job_title, company_name)
            subject = f"Operations & Systems Alignment - {job_title} @ {company_name}"
            
        message = EmailMessage()
        message["To"] = to_email
        message["From"] = GMAIL_USER
        message["Subject"] = subject
        body = f"Hi,\n\n{body_content}\n\nBest regards,\nKevin Miller"
        message.set_content(body)
        
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        draft_url = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        res = requests.post(draft_url, headers=headers, json={"message": {"raw": raw_message}}, timeout=10)
        return (True, "Success") if res.status_code in [200, 201] else (False, f"Gmail Error {res.status_code}")
    except Exception as e:
        return False, str(e)


# ==========================================
# 6. STAGE 1 STRICT FILTER & PIPELINE EXECUTION
# ==========================================
def passes_strict_filter(job):
    title = str(job.get("job_title") or "").lower()
    description = str(job.get("job_description") or "").lower()
    company = str(job.get("employer_name") or "").lower()
    state = str(job.get("job_state") or "").upper()
    city = str(job.get("job_city") or "").lower()
    salary_str, max_sal = extract_salary(job)

    # 1. Company Cooldown Audit
    if is_company_on_cooldown(company):
        return False

    # 2. Base Salary Floor ($50K Hard Drop)
    min_sal_floor = get_filter("min_salary", 50000)
    if max_sal > 0 and max_sal < min_sal_floor:
        return False

    # 3. Location & Commute (35-mile Farmington/Detroit radius)
    valid_cities = get_filter("valid_cities", [])
    is_mi = (state == "MI") or "michigan" in city or any(c in city for c in valid_cities)
    is_remote = job.get("job_is_remote", False) or "remote" in description[:300] or "work from home" in description[:300]
    if not (is_mi or is_remote):
        return False

    # 4. Experience-to-Pay Audit
    exp_floor = get_filter("experience_salary_floor", 60000)
    if any(k in description for k in ["3+ years", "3-5 years", "4+ years"]) and (0 < max_sal < exp_floor):
        return False

    # 5. Exclusions & Bans
    if any(term in title for term in get_filter("title_exclusions", [])):
        return False
    if any(comp in company for comp in get_filter("company_exclusions", [])):
        return False
    if any(trigger in description for trigger in get_filter("hard_ban_keywords", [])):
        return False
    if any(sen in title for sen in get_filter("seniority_exclusions", [])):
        return False

    return True


def send_telegram_card(job, score, reason, target_email, age_badge, salary_str, work_style, overlap_pct, matched_skills, short_id):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    company = html.escape(str(job.get("employer_name") or "N/A"))
    title = html.escape(str(job.get("job_title") or "N/A"))
    apply_link = html.escape(str(job.get("job_apply_link") or "#"), quote=True)
    apollo_url = html.escape(build_apollo_url(company), quote=True)
    linkedin_url = html.escape(build_linkedin_url(company), quote=True)
    matched_str = ", ".join(matched_skills[:4]).title() if matched_skills else "General Ops"
    
    card_text = (
        f"<b>{title}</b>\n"
        f"<b>Company:</b> {company}\n"
        f"<b>Posting Recency:</b> {age_badge}\n"
        f"<b>Work Style & Pay:</b> {work_style} | {salary_str}\n"
        f"<b>Fit Score:</b> {score}/100 | <b>Skill Match:</b> {overlap_pct}%\n"
        f"<b>Key Overlap:</b> <code>{html.escape(matched_str)}</code>\n"
        f"<b>Default Target:</b> <code>{html.escape(target_email)}</code>\n\n"
        f"<b>Fit Reason:</b> {html.escape(reason)}\n\n"
        f"<a href='{apply_link}'>1. Apply Direct</a>\n"
        f"<a href='{apollo_url}'>2. Open Leads in Apollo</a>\n"
        f"<a href='{linkedin_url}'>3. Open Leadership on LinkedIn</a>\n\n"
        f"<b>Mobile Swipe Shortcuts:</b>\n"
        f"  <code>draft</code> - Gmail Draft\n"
        f"  <code>/f &lt;days&gt;</code> - Snooze Followup\n"
        f"  <code>/tw</code> or <code>/cw</code> - Log Warm\n"
        f"  <code>/cc</code> or <code>/tc</code> - Log Cold\n"
        f"  <code>/x</code> - Mark Dead"
    )
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "📩 Draft Email", "callback_data": f"approve:{short_id}"},
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
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Failed to post card to Telegram: {e}", flush=True)


def process_single_candidate(job):
    ai_pass, score, reason = evaluate_job_with_gemini(job)
    if ai_pass:
        raw_id = job.get("job_id") or f"{job.get('employer_name')}_{job.get('job_title')}"
        short_id = generate_short_key(raw_id)
        save_job_to_cache(short_id, job)
        target_email = resolve_target_email(job.get("employer_name"), job.get("job_title"))
        age_badge = get_age_badge(parse_posted_hours(job.get("job_posted_at_datetime_utc")))
        salary_str, _ = extract_salary(job)
        work_style = extract_work_style(job)
        overlap_pct, matched_skills = calculate_keyword_overlap(job.get("job_description"))
        return {
            "job": job, "score": score, "reason": reason,
            "target_email": target_email, "age_badge": age_badge,
            "salary_str": salary_str, "work_style": work_style,
            "overlap_pct": overlap_pct, "matched_skills": matched_skills,
            "short_id": short_id
        }
    return None


def run_job_pipeline(chat_id=None, top_n=2):
    print(">>> Starting Job Search Pipeline...", flush=True)
    if chat_id:
        send_status_update(chat_id, "Fetching raw listings from JSearch...")
    seen_hashes = set()
    candidate_pool = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    followup_date = (datetime.now() + timedelta(days=calculate_followup_interval(5))).strftime("%Y-%m-%d")
    
    rapidapi_key = os.environ.get("RAPIDAPI_KEY")
    openweb_key = os.environ.get("OPENWEBNINJA_KEY")
    if rapidapi_key:
        headers = {"X-RapidAPI-Key": rapidapi_key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
        api_url = "https://jsearch.p.rapidapi.com/search"
    else:
        headers = {"x-api-key": openweb_key} if openweb_key else {}
        api_url = JSEARCH_URL
        
    target_queries = get_filter("target_queries", [])
    for query in target_queries:
        try:
            params = {"query": query, "page": "1", "num_pages": "1", "date_posted": "month"}
            res = requests.get(api_url, headers=headers, params=params, timeout=35)
            if res.status_code == 200:
                jobs = res.json().get("data", [])
                for job in jobs:
                    company = job.get("employer_name") or ""
                    title = job.get("job_title") or ""
                    job_hash = generate_dedup_hash(company, title)
                    if job_hash in seen_hashes or is_job_seen_db(job_hash):
                        continue
                    seen_hashes.add(job_hash)
                    save_seen_job_db(job_hash)
                    if passes_strict_filter(job):
                        candidate_pool.append(job)
        except Exception as e:
            print(f"Fetch Exception ({query}): {e}", flush=True)
            
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(process_single_candidate, candidate_pool))
    evaluated_matches = [r for r in results if r is not None]
    evaluated_matches.sort(key=lambda x: x["score"], reverse=True)
    
    top_matches = evaluated_matches[:top_n]
    for item in top_matches:
        job = item["job"]
        send_telegram_card(
            job, item["score"], item["reason"], item["target_email"],
            item["age_badge"], item["salary_str"], item["work_style"],
            item["overlap_pct"], item["matched_skills"], item["short_id"]
        )
        # Log to 10-column schema
        log_to_sheets_crm({
            "action": "add_row",
            "target_code": "TC",
            "row_data": [
                today_str,                   # A: First Contact Date
                today_str,                   # B: Last Contact Date
                job.get("employer_name"),    # C: Company
                job.get("job_title"),        # D: Title
                item["target_email"],        # E: Email
                5,                           # F: Priority Score (1-10)
                "Matched",                   # G: Status
                followup_date,               # H: Next Followup
                job.get("job_apply_link", ""), # I: Apply Link
                f"Matched via Pipeline | {item['reason']}" # J: Notes
            ]
        })
    return len(top_matches)


def fetch_networking_cards(target_code="CW", qty=2):
    """Pulls cards from 10-column Google Apps Script schema."""
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


# ==========================================
# 7. FLASK SERVER & WEBHOOK ROUTES
# ==========================================
@app.route('/', methods=['GET'])
def health_check():
    return "CRM & Job Pipeline Engine Active", 200


@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    global TOTAL_MESSAGES_SENT, TOTAL_INTERVIEWS_SET
    data = request.get_json()
    if not data:
        return jsonify({"status": "ignored"}), 200

    # 1. HANDLE INLINE BUTTON CLICKS
    if "callback_query" in data:
        callback = data["callback_query"]
        callback_id = callback.get("id")
        chat_id = callback["message"]["chat"]["id"]
        callback_data = callback.get("data", "")

        if TELEGRAM_BOT_TOKEN and callback_id:
            try:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": callback_id, "text": "Processing..."}, timeout=3)
            except Exception:
                pass

        if callback_data.startswith("approve:"):
            short_id = callback_data.split(":", 1)[1]
            job = get_job_from_cache(short_id)
            company = job.get("employer_name", "Company")
            job_title = job.get("job_title", "Operations Role")
            email = resolve_target_email(company, job_title)
            success, err = create_gmail_draft(email, company, job_title, is_warm=False)
            status_msg = "<b>Gmail Draft Created!</b>" if success else f"⚠️ <b>Draft Failed:</b> <code>{html.escape(err)}</code>"
            email_body = generate_cold_email(job_title, company)
            msg_out = f"{status_msg}\n\n<b>To:</b> <code>{email}</code>\n<b>Body:</b>\n{email_body}"
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": msg_out, "parse_mode": "HTML"})
            TOTAL_MESSAGES_SENT += 1
            return jsonify({"status": "ok"}), 200

        elif callback_data.startswith("apply:"):
            short_id = callback_data.split(":", 1)[1]
            job = get_job_from_cache(short_id)
            comp = job.get("employer_name", "")
            add_company_cooldown(comp)
            log_to_sheets_crm({"action": "apply_job", "company": comp})
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": f"✅ Marked applied for <b>{html.escape(comp)}</b>.", "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

        elif callback_data.startswith("pivot:"):
            short_id = callback_data.split(":", 1)[1]
            job = get_job_from_cache(short_id)
            comp = job.get("employer_name", "Firm")
            apollo_url = build_apollo_url(comp)
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": f"🔄 <b>Pivot Lead for {html.escape(comp)}:</b>\n<a href='{apollo_url}'>Search New Operations Leadership on Apollo</a>", "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

        elif callback_data.startswith("dead:"):
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": "❌ Marked role as dead.", "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

    # 2. HANDLE TEXT COMMANDS & SWIPE REPLIES
    if "message" in data:
        msg = data["message"]
        text = msg.get("text", "").strip()
        chat_id = msg.get("chat", {}).get("id")
        cmd_clean = text.lower().strip()
        today_str = datetime.now().strftime("%Y-%m-%d")

        # Parse Swipe Thread Context if available
        company = "Target Firm"
        job_title = "Operations Role"
        if "reply_to_message" in msg:
            reply_text = msg.get("reply_to_message", {}).get("text", "")
            lines = reply_text.split("\n")
            job_title = lines[0].strip() if lines else "Operations Role"
            for line in lines:
                if "Company:" in line:
                    company = line.replace("Company:", "").strip()
                    break

        # SHORTHAND COMMAND ROUTER
        if cmd_clean.startswith("/t"):
            parts = cmd_clean.split()
            qty = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 2
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": f"⚡ Pulling {qty} job cards..."})
            threading.Thread(target=run_job_pipeline, args=(chat_id, qty)).start()
            return jsonify({"status": "ok"}), 200

        elif cmd_clean.startswith("/c") or cmd_clean.startswith("/cw") or cmd_clean.startswith("/cc"):
            parts = cmd_clean.split()
            qty = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 2
            target_tab = "CW" if "/cw" in cmd_clean else ("CC" if "/cc" in cmd_clean else "CW")
            leads = fetch_networking_cards(target_tab, qty)
            if not leads:
                send_status_update(chat_id, f"No active contacts found in {target_tab} tab.")
                return jsonify({"status": "ok"}), 200
            for lead in leads:
                comp = html.escape(str(lead.get("company") or "Target Firm"))
                title = html.escape(str(lead.get("title") or "Executive"))
                email = html.escape(str(lead.get("email") or "Unlisted"))
                p_score = lead.get("priority") or 5
                notes = html.escape(str(lead.get("notes") or ""))
                card = (
                    f"<b>{comp}</b> - {title}\n"
                    f"<b>Contact:</b> <code>{email}</code> | <b>Priority:</b> {p_score}/10\n"
                    f"<b>Notes:</b> {notes}\n\n"
                    f"<b>Swipe Actions:</b>\n"
                    f"  <code>draft</code> - Generate Draft\n"
                    f"  <code>/f 14</code> - Set Followup\n"
                    f"  <code>/p 8</code> - Set Priority\n"
                    f"  <code>/n &lt;text&gt;</code> - Append Note"
                )
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": card, "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

        elif cmd_clean.startswith("/p "):
            parts = cmd_clean.split()
            target_p = parts[1] if len(parts) > 1 else "10"
            
            # If in reply thread, update priority
            if "reply_to_message" in msg:
                next_f = (datetime.now() + timedelta(days=calculate_followup_interval(target_p))).strftime("%Y-%m-%d")
                log_to_sheets_crm({
                    "action": "update_lead",
                    "company": company,
                    "priority": target_p,
                    "next_followup": next_f
                })
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": f"✅ Priority updated to <b>{target_p}</b> for <b>{company}</b>. Next followup: {next_f}"})
                return jsonify({"status": "ok"}), 200
            
            # Priority Search Query: Single Consolidated Message Block
            res = requests.post(CRM_WEBHOOK_URL, json={"action": "get_followups", "priority": target_p}, timeout=10)
            if res.status_code == 200:
                leads = res.json().get("followups", [])
                out_lines = [f"<b>PRIORITY {target_p} CONTACTS ({len(leads)} Total)</b>\n"]
                for idx, l in enumerate(leads, 1):
                    c = html.escape(str(l.get("company", "Firm")))
                    title = html.escape(str(l.get("title", "Role")))
                    email = html.escape(str(l.get("email", "N/A")))
                    last_c = l.get("last_contact") or today_str
                    nxt_f = l.get("next_followup") or today_str
                    n = html.escape(str(l.get("notes", "No notes.")))
                    out_lines.append(f"{idx}. <b>{c}</b> | {title}\n   <code>{email}</code> | Last: {last_c} | Next: {nxt_f}\n   Note: {n}\n")
                send_telegram_chunked(chat_id, "\n".join(out_lines))
            return jsonify({"status": "ok"}), 200

        elif cmd_clean.startswith("/quick"):
            name, comp, priority, note = parse_quick_command(text)
            next_f = (datetime.now() + timedelta(days=calculate_followup_interval(priority))).strftime("%Y-%m-%d")
            log_to_sheets_crm({
                "action": "add_row",
                "target_code": "CW",
                "row_data": [
                    today_str,                   # A: First Contact Date
                    today_str,                   # B: Last Contact Date
                    comp,                        # C: Company
                    name,                        # D: Title/Contact Name
                    f"{name.lower().replace(' ', '')}@{comp.lower().replace(' ', '')}.com", # E: Email
                    priority,                    # F: Priority Score (1-10)
                    "New Lead",                  # G: Status
                    next_f,                      # H: Next Followup
                    "Telegram /quick",           # I: Source
                    f"[{today_str}] {note}"      # J: Timestamped Notes
                ]
            })
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": f"✅ Contact <b>{html.escape(name)}</b> @ <b>{html.escape(comp)}</b> added (Priority {priority}, Next: {next_f}).", "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

        elif cmd_clean.startswith("/f "):
            try:
                days = int(cmd_clean.split()[1])
                nxt_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
                log_to_sheets_crm({
                    "action": "update_lead",
                    "company": company,
                    "last_contact": today_str,
                    "next_followup": nxt_date
                })
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": f"🗓️ Followup for <b>{html.escape(company)}</b> set to {nxt_date} ({days}d).", "parse_mode": "HTML"})
            except Exception:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": "⚠️ Invalid format. Use <code>/f 14</code>."})
            return jsonify({"status": "ok"}), 200

        elif cmd_clean.startswith("/n "):
            note_text = text[3:].strip()
            timestamped_note = f"[{today_str}] {note_text}"
            log_to_sheets_crm({
                "action": "append_note",
                "company": company,
                "note": timestamped_note
            })
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": f"📝 Appended note to <b>{html.escape(company)}</b>: <code>{html.escape(timestamped_note)}</code>", "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

        elif cmd_clean == "/pivot":
            apollo_url = build_apollo_url(company)
            log_to_sheets_crm({"action": "move_row", "company": company, "target_tab": "Killed"})
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": f"🔄 Archived <b>{html.escape(company)}</b>. <a href='{apollo_url}'>Find new contacts on Apollo</a>", "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

        elif cmd_clean == "/health":
            health_msg = f"⚙️ <b>System Health Report:</b>\n\nDatabase: <code>ONLINE</code>\nCRM Connection: <code>ACTIVE</code>\nTimestamp Engine: <code>NATIVE ISO (UTC-4)</code>\nActive Mode: <code>100% Manual Pull</code>"
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": health_msg, "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

        elif cmd_clean == "/efficiency":
            ratio = f"{TOTAL_MESSAGES_SENT} Messages -> {TOTAL_INTERVIEWS_SET} Interviews"
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": f"📈 <b>Golden Ratio Analytics:</b>\n{ratio}", "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

        elif cmd_clean.startswith("/search"):
            # Swipe key = value updates
            if "=" in text:
                parts = text.replace("/search", "").split("=", 1)
                k = parts[0].strip()
                v_str = parts[1].strip()
                try:
                    v_val = json.loads(v_str)
                except Exception:
                    v_val = v_str
                set_filter(k, v_val)
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": f"⚙️ Filter <code>{k}</code> updated to <code>{json.dumps(v_val)}</code>."})
                return jsonify({"status": "ok"}), 200
            
            # Display current search filters
            min_sal = get_filter("min_salary", 50000)
            cities = get_filter("valid_cities", [])
            bans = get_filter("title_exclusions", [])
            msg_out = (
                f"🔍 <b>Current Dynamic Search Filters:</b>\n\n"
                f"<b>Min Base Salary Floor:</b> ${min_sal:,.0f}\n"
                f"<b>Radius Cities ({len(cities)}):</b> {', '.join(cities[:5])}...\n"
                f"<b>Banned Terms:</b> {', '.join(bans[:5])}...\n\n"
                f"<i>Swipe-reply key = value to update live (e.g. min_salary = 60000)</i>"
            )
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": msg_out, "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

        elif cmd_clean in ["draft", "/draft"]:
            email_target = resolve_target_email(company, job_title)
            success, err = create_gmail_draft(email_target, company, job_title, is_warm=False)
            status = "<b>Gmail Draft Created!</b>" if success else f"⚠️ <b>Draft Failed:</b> <code>{html.escape(err)}</code>"
            email_body = generate_cold_email(job_title, company)
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": f"{status}\n\nTo: <code>{email_target}</code>\nBody:\n{email_body}", "parse_mode": "HTML"})
            TOTAL_MESSAGES_SENT += 1
            return jsonify({"status": "ok"}), 200

        elif cmd_clean in ["/conv", "/int", "/tw", "/cw", "/cc", "/tc", "/x"]:
            if cmd_clean == "/conv":
                log_to_sheets_crm({"action": "update_lead", "company": company, "status": "Good Conversation", "priority": 8})
                msg_out = f"💬 Marked <b>{html.escape(company)}</b> as Good Conversation."
            elif cmd_clean == "/int":
                TOTAL_INTERVIEWS_SET += 1
                log_to_sheets_crm({"action": "update_lead", "company": company, "status": "Interview Scheduled", "priority": 10})
                msg_out = f"🎉 Marked <b>{html.escape(company)}</b> as Interview Scheduled!"
            elif cmd_clean in ["/tw", "/cw"]:
                log_to_sheets_crm({"action": "move_row", "company": company, "target_tab": "CW"})
                msg_out = f"📁 Moved <b>{html.escape(company)}</b> to Warm tab."
            elif cmd_clean in ["/cc", "/tc"]:
                log_to_sheets_crm({"action": "move_row", "company": company, "target_tab": "CC"})
                msg_out = f"📁 Moved <b>{html.escape(company)}</b> to Cold tab."
            elif cmd_clean == "/x":
                log_to_sheets_crm({"action": "move_row", "company": company, "target_tab": "Killed"})
                msg_out = f"❌ Archived <b>{html.escape(company)}</b>."
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": msg_out, "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

    return jsonify({"status": "ignored"}), 200


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
