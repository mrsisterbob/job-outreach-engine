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


# ==============================================================================
# LINES 800+ : MOBILE COMMAND ROUTER, SEARCH FILTERS & INLINE KEYBOARDS
# ==============================================================================

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
    "q": "target_queries"
}

def get_filter(key_name):
    """Retrieve JSON-decoded or raw scalar value for a given filter key from SQLite."""
    conn = get_db_connection()
    res = conn.execute("SELECT value FROM search_filters WHERE key = ?", (key_name,)).fetchone()
    conn.close()
    if not res:
        return None
    val = res["value"]
    try:
        return json.loads(val)
    except Exception:
        return val

def update_filter_param(raw_key, raw_val_str):
    """Parses short aliases, scalar offsets (+/-), and array mutation operators (+ / -)."""
    key = ALIAS_MAP.get(raw_key.lower().strip(), raw_key.lower().strip())
    conn = get_db_connection()
    current_val = get_filter(key)
    
    if current_val is None:
        conn.close()
        return f"❌ Unknown filter parameter: <code>{raw_key}</code>"

    # Array parameter handling (e.g., ban + sales, city - canton)
    if isinstance(current_val, list):
        clean_val = raw_val_str.strip()
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
            current_val = [x.strip() for x in clean_val.split(",")]

        new_db_val = json.dumps(current_val)
    else:
        # Scalar numeric handling (e.g., pay + 5000, min = 60000)
        clean_val = raw_val_str.strip()
        if clean_val.startswith("+"):
            new_db_val = str(int(current_val) + int(clean_val[1:].strip()))
        elif clean_val.startswith("-"):
            new_db_val = str(int(current_val) - int(clean_val[1:].strip()))
        else:
            new_db_val = str(int(clean_val))

    conn.execute("UPDATE search_filters SET value = ? WHERE key = ?", (new_db_val, key))
    conn.commit()
    conn.close()
    return f"✅ Updated <code>{key}</code> to: <code>{new_db_val}</code>"

def format_email_block(email_text):
    """Wraps anti-fluff email drafts in monospaced blocks for single-tap mobile copying."""
    sanitized = sanitize_email_text(email_text)
    return f"<code>{sanitized}</code>"

@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    data = request.get_json()
    if not data:
        return jsonify({"status": "ignored"}), 200

    # 1. Handle Interactive Inline Keyboard Callbacks
    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        cb_data = cb.get("data", "")

        if cb_data.startswith("adj_pay_"):
            delta = cb_data.replace("adj_pay_", "")
            res = update_filter_param("min_salary", delta)
            send_telegram_message(chat_id, f"<b>Salary Floor Updated:</b>\n{res}")
        elif cb_data == "add_city_novi":
            res = update_filter_param("valid_cities", "+ novi")
            send_telegram_message(chat_id, f"<b>Location Filter Updated:</b>\n{res}")
        elif cb_data == "reset_filters":
            init_db()
            send_telegram_message(chat_id, "✅ <b>Search filters reset to default parameters.</b>")

        return jsonify({"status": "ok"}), 200

    if "message" not in data:
        return jsonify({"status": "ignored"}), 200

    msg = data["message"]
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 2. Pre-filled /quick Monospaced Tap-to-Copy Template
    if text == "/quick":
        template_msg = (
            "Tap the code block below to copy, adjust details, and send:\n\n"
            "<code>/quick Jane Van Der Bilt @ Acme Corp 9 Spoke at event; interested in back-office systems</code>"
        )
        send_telegram_message(chat_id, template_msg)
        return jsonify({"status": "ok"}), 200

    # 3. Multi-Word Name Parser using @ Delimiter Regex
    if text.startswith("/quick "):
        raw_cmd = text[7:].strip()
        match = re.match(r"^([^@]+)@([^\d]+)\s+(\d{1,2})\s+(.+)$", raw_cmd)
        if match:
            name = match.group(1).strip()
            company = match.group(2).strip()
            priority = int(match.group(3).strip())
            note = match.group(4).strip()
            next_followup = calculate_next_followup(priority)
            
            payload = {
                "action": "quick_add",
                "first_contact": today_str,
                "last_contact": today_str,
                "name": name,
                "company": company,
                "priority": priority,
                "next_followup": next_followup,
                "note": f"[{today_str}] {note}"
            }
            requests.post(APPS_SCRIPT_URL, json=payload)
            
            resp = (
                f"✅ <b>Contact Created</b>\n"
                f"<b>Name:</b> {name}\n"
                f"<b>Company:</b> {company}\n"
                f"<b>Priority:</b> {priority}\n"
                f"<b>Next Follow-up:</b> {next_followup}"
            )
            send_telegram_message(chat_id, resp)
        else:
            err = "❌ <b>Syntax Error.</b> Use format:\n<code>/quick <Name> @<Company> <Priority 1-10> <Note></code>"
            send_telegram_message(chat_id, err)
        return jsonify({"status": "ok"}), 200

    # 4. Single-Message Output Priority Batcher (/p 1 - 10)
    if re.match(r"^/p\s+(\d+)$", text):
        priority_lvl = int(re.match(r"^/p\s+(\d+)$", text).group(1))
        resp = requests.get(f"{APPS_SCRIPT_URL}?action=get_priority&level={priority_lvl}").json()
        contacts = resp.get("contacts", [])
        
        if not contacts:
            send_telegram_message(chat_id, f"No active contacts at Priority Tier {priority_lvl}.")
            return jsonify({"status": "ok"}), 200
            
        out_msg = f"<b>PRIORITY {priority_lvl} CONTACTS ({len(contacts)} Total)</b>\n\n"
        for idx, c in enumerate(contacts, 1):
            out_msg += f"{idx}. <b>{c.get('name')}</b> | {c.get('company')}\n"
            out_msg += f"Last Contact: {c.get('last_contact')} | Next: {c.get('next_followup')}\n"
            out_msg += f"Note: <i>{c.get('latest_note', 'No notes logged')}</i>\n\n"
            
        send_telegram_message(chat_id, out_msg)
        return jsonify({"status": "ok"}), 200

    # 5. /search Status Card + Tap-to-Copy Bubbles + Interactive Inline Buttons
    if text == "/search":
        min_sal = get_filter("min_salary")
        exp_sal = get_filter("experience_salary_floor")
        bans = get_filter("title_exclusions")
        cities = get_filter("valid_cities")
        
        card_text = (
            "⚙️ <b>ACTIVE SEARCH FILTER CONFIGURATION</b>\n\n"
            f"<b>Base Salary Floor (min/pay):</b> ${int(min_sal):,}\n"
            f"<b>3+ Yr Exp Floor (exp/floor):</b> ${int(exp_sal):,}\n"
            f"<b>Banned Titles (ban):</b> {', '.join(bans[:4])}... ({len(bans)} total)\n"
            f"<b>Target Cities (city/loc):</b> {', '.join(cities[:4])}... ({len(cities)} total)\n\n"
            "<b>Tap-to-Copy Quick Adjustment Bubbles:</b>\n"
            "<code>min = 60000</code>\n"
            "<code>ban + sales</code>\n"
            "<code>ban - manager</code>\n"
            "<code>city + canton</code>"
        )
        
        inline_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "⬆️ Pay +$5k", "callback_data": "adj_pay_+5000"},
                    {"text": "⬇️ Pay -$5k", "callback_data": "adj_pay_-5000"}
                ],
                [
                    {"text": "📍 Add Novi", "callback_data": "add_city_novi"},
                    {"text": "❌ Reset Filters", "callback_data": "reset_filters"}
                ]
            ]
        }
        
        send_telegram_message(chat_id, card_text, reply_markup=inline_keyboard)
        return jsonify({"status": "ok"}), 200

    # 6. Swipe-Reply Key/Alias Mutation Parser (e.g., pay = 60000, ban + sales, city - canton)
    if any(op in text for op in ["=", "+", "-"]):
        match = re.match(r"^([a-zA-Z_]+)\s*(=|\+|-)\s*(.+)$", text)
        if match:
            raw_key = match.group(1).strip()
            op = match.group(2).strip()
            val_str = match.group(3).strip()
            
            val_arg = f"{op} {val_str}" if op in ["+", "-"] else val_str
            update_res = update_filter_param(raw_key, val_arg)
            send_telegram_message(chat_id, update_res)
            return jsonify({"status": "ok"}), 200

    return jsonify({"status": "ignored"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
