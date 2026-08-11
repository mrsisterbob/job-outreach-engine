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

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
MY_PHONE_NUMBER = os.environ.get("MY_PHONE_NUMBER")

JSEARCH_URL = "https://api.openwebninja.com/jsearch/search"
DB_PATH = "jobs_cache.db"

def init_db():
    """Initializes local SQLite tables for callbacks, deduplication, and 14-day company cooldowns."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                short_id TEXT PRIMARY KEY,
                job_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_jobs (
                job_hash TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS company_cooldown (
                company_clean TEXT PRIMARY KEY,
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    conn.close()

init_db()

def save_job_to_cache(short_id, job_dict):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jobs (short_id, job_json) VALUES (?, ?)",
                (short_id, json.dumps(job_dict))
            )
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
            cursor.execute("""
                SELECT logged_at FROM company_cooldown 
                WHERE company_clean = ? AND logged_at >= datetime('now', '-14 days')
            """, (clean,))
            return cursor.fetchone() is not None
    except Exception:
        return False

# ==========================================
# 2. FILTER RULES & SCORING MODIFIERS
# ==========================================
TITLE_EXCLUSIONS = [
    "sales", "account executive", "bdr", "sdr", "financial advisor", "financial planner",
    "client relationship manager", "agent", "wholesaler", "producer", "insurance agent",
    "teller", "branch", "personal banker", "loan officer", "mortgage", "cpa",
    "customer service representative", "call center", "door to door", "cold call"
]

COMPANY_EXCLUSIONS = [
    "cybercoders", "robert half", "kforce", "jobot", "actalent", "insight global"
]

HARD_BAN_KEYWORDS = [
    "lead generation", "upselling", "quota-driven", "client acquisition",
    "hunter mentality", "pipeline development", "uncapped earnings",
    "cold outreach", "deal closing", "solution pitching",
    "uncapped potential", "commission", "hustle", "grind", "door-to-door",
    "phone jockey", "call jockey", "cold calling"
]

SENIORITY_EXCLUSIONS = [
    " senior", " lead", " manager", "director", "vp", " executive", " principal", "head of"
]

CORE_SKILLS = [
    "python", "sql", "salesforce", "excel", "schwab sac", "schwab advisor center",
    "fidelity wealthscape", "docusign", "process automation", "reconciliation"
]

TIER1_ECOSYSTEM = [
    "downtown detroit", "inveniam", "rivian", "rocket", "quicken", "stockx", "venture"
]

TARGET_QUERIES = [
    "Wealth Operations Detroit MI",
    "Fintech Operations Michigan",
    "Business Operations Analyst Detroit MI",
    "Custodial Operations Schwab Fidelity Michigan",
    "Financial Systems Process Automation Detroit MI",
    "Operations Specialist Detroit MI",
    "Financial Operations Analyst Remote"
]

PRIORITY_TIERS = {
    "CW": 1, "CARMEN WARM": 1, "CARMEN_WARM": 1,
    "TW": 2, "TETIANA WARM": 2, "TETIANA_WARM": 2,
    "CC": 3, "CARMEN COLD": 3, "CARMEN_COLD": 3,
    "TC": 4, "TETIANA COLD": 4, "TETIANA_COLD": 4
}

SYSTEM_PROMPT = """You are a strict technical job screener evaluating roles for an early-career candidate (0-2 years experience).
Target Profile: Non-sales W-2 roles in Tech, FinTech, Auto Tech, or Back-Office Systems/Operations in Metro Detroit or Remote.
High Priority Skills: Python, SQL, Salesforce, Excel, Schwab SAC, Fidelity Wealthscape, DocuSign, Process Automation.
Strictly FORBIDDEN: Sales, cold calling, client pitching, commission-based roles, retail bank tellers, CPA tracks, Senior/Lead/Manager roles.

Evaluate the job description and respond ONLY with a JSON object containing:
{
  "score": <integer between 1 and 100 representing fit signal>,
  "reason": "<1-sentence concise explanation of why this role fits or does not fit>"
}"""

# ==========================================
# 3. HELPER FUNCTIONS & HYBRID SCORER
# ==========================================
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
                json={"chat_id": chat_id, "text": f"📡 <b>Pipeline Telemetry:</b>\n{text}", "parse_mode": "HTML"},
                timeout=5
            )
        except Exception:
            pass

def send_twilio_sms(body_text):
    """Dispatches Twilio SMS for high-priority alerts."""
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, MY_PHONE_NUMBER]):
        return False
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    data = {"From": TWILIO_PHONE_NUMBER, "To": MY_PHONE_NUMBER, "Body": body_text}
    try:
        res = requests.post(url, data=data, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=10)
        return res.status_code in [200, 201]
    except Exception as e:
        print(f"Twilio SMS Exception: {e}", flush=True)
        return False

def log_to_sheets_crm(payload, max_retries=3):
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
        return "🔥 [1-3d RECENT]"
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
        return "🏢 Hybrid"
    elif is_remote:
        return "🏠 Remote"
    elif "on-site" in desc or "onsite" in desc or "in-office" in desc:
        return "📍 On-Site"
    return "📍 On-Site / Unspecified"

def calculate_keyword_overlap(job_desc):
    desc = str(job_desc or "").lower()
    matches = [skill for skill in CORE_SKILLS if skill in desc]
    overlap_pct = int((len(matches) / len(CORE_SKILLS)) * 100)
    return overlap_pct, matches

def calculate_hybrid_score_modifier(job, base_ai_score):
    score = base_ai_score
    desc = str(job.get("job_description") or "").lower()
    title = str(job.get("job_title") or "").lower()
    company = str(job.get("employer_name") or "").lower()
    salary_str, max_sal = extract_salary(job)

    if any(k in desc or k in company for k in TIER1_ECOSYSTEM):
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

def calculate_urgency(due_date_str):
    if not due_date_str:
        return "🔥 HIGH", 0
    try:
        due_dt = datetime.strptime(str(due_date_str).strip(), "%Y-%m-%d")
        days_overdue = (datetime.now() - due_dt).days
        if days_overdue > 3:
            return "🚨 CRITICAL", days_overdue
        return "🔥 HIGH", max(0, days_overdue)
    except Exception:
        return "🔥 HIGH", 0

# ==========================================
# 4. GEMINI API INTEGRATION
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
        print(f"Gemini API Error ({res.status_code}): {res.text}", flush=True)
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

def generate_tailored_intro(job_description):
    prompt = f"Write 2 concise sentences explaining why a Wealth Operations candidate with Python, SQL, Salesforce, and Schwab SAC experience aligns with this job description:\n{str(job_description or '')[:1500]}"
    res = call_gemini_api(prompt, response_mime="text/plain")
    return res.strip() if res else "My background centers on wealth operations, custodial workflows, and process automation."

def generate_resume_cheat_sheet(job_description):
    prompt = f"Analyze this job description and provide 3 high-impact bullet points specifying exact skills/terms to emphasize on a resume:\n{str(job_description or '')[:2000]}"
    res = call_gemini_api(prompt, response_mime="text/plain")
    return res.strip() if res else "• Emphasize Python/SQL automation\n• Highlight Schwab SAC reconciliation\n• Accentuate Salesforce CRM management"

# ==========================================
# 5. GMAIL API DRAFTING
# ==========================================
def create_gmail_draft(to_email, company_name, job_title, job_description=""):
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

        tailored_intro = generate_tailored_intro(job_description) if job_description else "My background centers on wealth operations, custodial workflows, and process automation."
        
        message = EmailMessage()
        message["To"] = to_email
        message["From"] = GMAIL_USER
        message["Subject"] = f"Operations & Systems Alignment - {job_title} @ {company_name}"
        body = (
            f"Hi Hiring Team,\n\n"
            f"I recently came across the {job_title} opening at {company_name} and wanted to reach out directly. "
            f"{tailored_intro}\n\n"
            f"I have attached my resume (PDF) to this message for your review and would welcome the opportunity to connect.\n\n"
            f"Best regards,\n"
            f"Kevin Miller"
        )
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

    # 1. 14-Day Company Cooldown Audit
    if is_company_on_cooldown(company):
        return False

    # 2. Normalized Base Salary Floor ($50K Minimum Hard Drop)
    if max_sal > 0 and max_sal < 50000:
        return False

    # 3. Location & Commute (35 miles from Farmington, MI)
    valid_cities = [
        "farmington", "detroit", "ann arbor", "novi", "troy", "southfield",
        "auburn hills", "plymouth", "royal oak", "livonia", "dearborn",
        "birmingham", "bloomfield", "warren", "sterling heights", "canton",
        "rochester", "wixom", "madison heights"
    ]
    is_mi = (state == "MI") or "michigan" in city or any(c in city for c in valid_cities)
    is_remote = job.get("job_is_remote", False) or "remote" in description[:300] or "work from home" in description[:300]
    if not (is_mi or is_remote):
        return False

    # 4. Experience-to-Pay Audit (Disqualify 3+ yrs requiring under $60K)
    if any(k in description for k in ["3+ years", "3-5 years", "4+ years"]) and (0 < max_sal < 60000):
        return False

    # 5. Sales Buzzword & Seniority Bans
    if any(term in title for term in TITLE_EXCLUSIONS):
        return False
    if any(comp in company for comp in COMPANY_EXCLUSIONS):
        return False
    if any(trigger in description for trigger in HARD_BAN_KEYWORDS):
        return False
    if any(sen in title for sen in SENIORITY_EXCLUSIONS):
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
        f"<b>📱 Quick Reply Commands (Swipe Right):</b>\n"
        f" • <code>draft</code> - Create Draft + Copy Text\n"
        f" • <code>s &lt;days&gt;</code> - Snooze Follow-up\n"
        f" • <code>a</code> - Mark Applied\n"
        f" • <code>tc</code> / <code>tw</code> - Log Tetiana Cold / Warm\n"
        f" • <code>cc</code> / <code>cw</code> - Log Carmen Cold / Warm\n"
        f" • <code>x</code> - Mark Dead"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ Mark Applied", "callback_data": f"apply:{short_id}"},
                {"text": "✉️ Draft Email", "callback_data": f"approve:{short_id}"}
            ],
            [
                {"text": "📋 Tailor Resume", "callback_data": f"resume:{short_id}"},
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
        if res.status_code != 200:
            print(f"Telegram Card Error ({res.status_code}): {res.text}", flush=True)
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

def fetch_and_push_lead_batch(chat_id=None, batch_size=3):
    """Audits sheet via CRM webhook, filters overdue leads by priority tier & due date, and pushes top N."""
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not (CRM_WEBHOOK_URL and TELEGRAM_BOT_TOKEN and target_chat):
        return 0

    try:
        res = requests.post(CRM_WEBHOOK_URL, json={"action": "get_followups"}, timeout=10)
        if res.status_code != 200:
            send_health_alert(f"CRM Webhook returned code {res.status_code} during batch sweep.")
            return 0

        raw_followups = res.json().get("followups", [])
        if not raw_followups:
            send_status_update(target_chat, "<b>Batch Sweep:</b> No overdue follow-ups found in CRM sheet!")
            return 0

        def parse_date(d_str):
            try:
                return datetime.strptime(str(d_str).strip(), "%Y-%m-%d")
            except Exception:
                return datetime.max

        def get_tier(lead):
            cat = str(lead.get("tab") or lead.get("target_code") or "").upper().strip()
            return PRIORITY_TIERS.get(cat, 99)

        sorted_leads = sorted(
            raw_followups,
            key=lambda x: (get_tier(x), parse_date(x.get("due_date") or x.get("next_followup")))
        )

        top_batch = sorted_leads[:batch_size]
        header = f"🚨 <b>Follow-up Batch ({len(top_batch)} Overdue Leads)</b>\n<i>Prioritized: CW → TW → CC → TC (Oldest First)</i>\n"
        send_status_update(target_chat, header)

        sms_summary = "Morning Follow-up Batch:\n"

        for idx, lead in enumerate(top_batch, start=1):
            company = html.escape(str(lead.get("company") or "Target Firm"))
            title = html.escape(str(lead.get("title") or "Operations Role"))
            email = html.escape(str(lead.get("email") or "Unlisted"))
            tier_name = html.escape(str(lead.get("tab") or "Followup"))
            due_date = html.escape(str(lead.get("due_date") or "Overdue"))
            notes = html.escape(str(lead.get("notes") or ""))

            card_text = (
                f"<b>[{idx}/{len(top_batch)}] {company}</b> — {title}\n"
                f"<b>Tab/Tier:</b> <code>{tier_name}</code>\n"
                f"<b>Due Date:</b> <code>{due_date}</code>\n"
                f"<b>Contact:</b> <code>{email}</code>\n"
                f"<b>Notes:</b> {notes}\n\n"
                f"<b>Quick Actions (Swipe Right):</b>\n"
                f"• <code>s 4</code> (Snooze 4 days)\n"
                f"• <code>a</code> (Mark Applied)\n"
                f"• <code>x</code> (Mark Dead)\n"
                f"• <code>draft</code> (Gmail Draft)"
            )

            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": target_chat, "text": card_text, "parse_mode": "HTML"},
                timeout=5
            )

            sms_summary += f"{idx}. {company} ({tier_name}) - Due {due_date}\n"

        # Direct SMS Alert for Morning Batch
        if TWILIO_PHONE_NUMBER:
            send_twilio_sms(sms_summary)

        return len(top_batch)

    except Exception as e:
        print(f"Batch Sweeper Error: {e}", flush=True)
        send_health_alert(f"Batch Sweeper Exception: {e}")
        return 0

def run_job_pipeline(chat_id=None, top_n=5):
    print(">>> Starting Job Search Pipeline...", flush=True)
    if chat_id:
        send_status_update(chat_id, "Fetching raw listings from JSearch across query clusters...")

    seen_hashes = set()
    candidate_pool = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    followup_date_tc = (datetime.now() + timedelta(days=4)).strftime("%Y-%m-%d")

    rapidapi_key = os.environ.get("RAPIDAPI_KEY")
    openweb_key = os.environ.get("OPENWEBNINJA_KEY")

    if rapidapi_key:
        headers = {"X-RapidAPI-Key": rapidapi_key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
        api_url = "https://jsearch.p.rapidapi.com/search"
    else:
        headers = {"x-api-key": openweb_key} if openweb_key else {}
        api_url = JSEARCH_URL

    raw_jobs_count = 0
    for query in TARGET_QUERIES:
        for page in range(1, 4):
            for attempt in range(2):
                try:
                    params = {"query": query, "page": str(page), "num_pages": "1", "date_posted": "month"}
                    res = requests.get(api_url, headers=headers, params=params, timeout=35)
                    if res.status_code != 200:
                        err_msg = f"JSearch API Error ({res.status_code}) on '{query}' page {page}: {res.text[:80]}"
                        print(err_msg, flush=True)
                        if chat_id and page == 1:
                            send_status_update(chat_id, f"⚠️ {err_msg}")
                        time.sleep(1.0)
                        break

                    jobs = res.json().get("data", [])
                    raw_jobs_count += len(jobs)

                    for job in jobs:
                        company = job.get("employer_name") or ""
                        title = job.get("job_title") or ""
                        job_hash = generate_dedup_hash(company, title)

                        if job_hash in seen_hashes or is_job_seen_db(job_hash):
                            continue
                        seen_hashes.add(job_hash)
                        save_seen_job_db(job_hash)

                        posted_hours = parse_posted_hours(job.get("job_posted_at_datetime_utc"))
                        if posted_hours > 720:
                            continue

                        if passes_strict_filter(job):
                            candidate_pool.append(job)
                    time.sleep(0.5)
                    break
                except requests.exceptions.Timeout:
                    time.sleep(2.0)
                except Exception as e:
                    print(f"Fetch Exception: {e}", flush=True)
                    break

    if chat_id:
        send_status_update(chat_id, f"Scanned {raw_jobs_count} total postings. {len(candidate_pool)} roles passed Stage 1 filters. Running parallel Gemini AI evaluation...")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(process_single_candidate, candidate_pool))

    evaluated_matches = [r for r in results if r is not None]
    evaluated_matches.sort(key=lambda x: x["score"], reverse=True)

    top_matches = []
    contract_count = 0
    remote_count = 0

    for match in evaluated_matches:
        job = match["job"]
        desc = str(job.get("job_description") or "").lower()
        is_contract = any(k in desc for k in ["contract", "c2h", "contract-to-hire", "staffing"])
        is_remote = job.get("job_is_remote", False) or "remote" in desc[:300] or "work from home" in desc[:300]

        if is_contract and contract_count >= 1:
            continue
        if is_remote and remote_count >= 1:
            continue

        if is_contract:
            contract_count += 1
        if is_remote:
            remote_count += 1

        top_matches.append(match)
        if len(top_matches) >= top_n:
            break

    if chat_id:
        telemetry_msg = (
            f"AI Evaluation complete. Posting top {len(top_matches)} high-fit role cards below...\n\n"
            f"<b>📱 Command & Swipe-Reply Cheat Sheet</b>\n"
            f"<b>Email & Actions:</b>\n"
            f" • <code>draft</code> - Generate Gmail Draft + Text Copy\n"
            f" • <code>s &lt;days&gt;</code> - Dynamic Snooze (e.g. <code>s 3</code>)\n"
            f" • <code>a</code> - Mark Applied in CRM\n"
            f" • <code>r</code> - Generate Resume Cheat Sheet\n\n"
            f"<b>CRM Tab Routing:</b>\n"
            f" • <code>tc</code> / <code>tw</code> - Log to Tetiana Cold / Warm\n"
            f" • <code>cc</code> / <code>cw</code> - Log to Carmen Cold / Warm\n"
            f" • <code>carmen</code> - Show Ranked Carmen Urgency Queue\n\n"
            f"<b>Direct Standalone Commands:</b>\n"
            f" • <code>/cc user@firm.com Firm Title</code> - Quick Log Carmen Cold\n"
            f" • <code>/tc user@firm.com Firm Title</code> - Quick Log Tetiana Cold"
        )
        send_status_update(chat_id, telemetry_msg)

    for item in top_matches:
        job = item["job"]
        send_telegram_card(
            job, item["score"], item["reason"], item["target_email"],
            item["age_badge"], item["salary_str"], item["work_style"],
            item["overlap_pct"], item["matched_skills"], item["short_id"]
        )
        log_to_sheets_crm({
            "action": "add_row",
            "target_code": "TC",
            "row_data": [
                today_str,
                job.get("employer_name"),
                job.get("job_title"),
                item["target_email"],
                item["score"],
                "Matched",
                followup_date_tc,
                job.get("job_apply_link", ""),
                f"{item['age_badge']} | {item['work_style']} | {item['reason']}"
            ]
        })

    if not top_matches and chat_id:
        send_status_update(chat_id, "Pipeline run finished: 0 roles met the Gemini score threshold (65+) for this batch.")
    return len(top_matches)

# Backwards compatibility alias
run_stale_application_sweeper = fetch_and_push_lead_batch

# ==========================================
# 7. FLASK SERVER & WEBHOOK ROUTES
# ==========================================
@app.route('/', methods=['GET'])
def health_check():
    return "CRM & Job Pipeline Engine Active", 200

@app.route('/cron/morning-batch', methods=['GET', 'POST'])
def morning_batch_trigger():
    """8:00 AM Daily Morning Cron Endpoint."""
    pushed_count = fetch_and_push_lead_batch(batch_size=3)
    return jsonify({"status": "success", "leads_pushed": pushed_count}), 200

@app.route('/webhook', methods=['POST'])
def telegram_webhook():
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
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                    json={"callback_query_id": callback_id, "text": "Processing..."},
                    timeout=3
                )
            except Exception:
                pass

        if callback_data.startswith("approve:"):
            short_id = callback_data.split(":", 1)[1]
            job = get_job_from_cache(short_id)
            company = job.get("employer_name", "Company")
            job_title = job.get("job_title", "Operations Role")
            email = resolve_target_email(company, job_title)
            job_desc = job.get("job_description", "")

            success, err = create_gmail_draft(email, company, job_title, job_desc)
            status_msg = "✉️ <b>Gmail Draft Created!</b>" if success else f"⚠️ <b>Gmail Draft Failed:</b> <code>{html.escape(err)}</code>"
            tailored_intro = generate_tailored_intro(job_desc)

            email_text_block = (
                f"{status_msg}\n\n"
                f"<b>To:</b> <code>{html.escape(email)}</code>\n"
                f"<b>Subject:</b> Operations & Systems Alignment - {html.escape(job_title)} @ {html.escape(company)}\n\n"
                f"Hi Hiring Team,\n\n"
                f"I recently came across the {html.escape(job_title)} opening at {html.escape(company)} and wanted to reach out directly. "
                f"{html.escape(tailored_intro)}\n\n"
                f"I have attached my resume (PDF) to this message for your review and would welcome the opportunity to connect.\n\n"
                f"Best regards,\nKevin Miller"
            )
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": chat_id, "text": email_text_block[:3990], "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

        elif callback_data.startswith("resume:"):
            short_id = callback_data.split(":", 1)[1]
            job = get_job_from_cache(short_id)
            cheat_sheet = generate_resume_cheat_sheet(job.get("job_description", "")) if job else "Focus on Python, SQL, Salesforce, Schwab SAC."
            msg = f"📋 <b>Resume Tips:</b>\n\n{html.escape(cheat_sheet)}"
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": chat_id, "text": msg[:3990], "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

        elif callback_data.startswith("apply:"):
            short_id = callback_data.split(":", 1)[1]
            job = get_job_from_cache(short_id)
            comp = job.get("employer_name", "")
            add_company_cooldown(comp)
            log_to_sheets_crm({"action": "apply_job", "company": comp})
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": chat_id, "text": f"✅ Marked applied for <b>{html.escape(comp)}</b>.", "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

        elif callback_data.startswith("dead:"):
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": chat_id, "text": "❌ Marked role as dead.", "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

    # 2. HANDLE TEXT COMMANDS & SWIPE REPLIES
    if "message" in data:
        msg = data["message"]
        text = msg.get("text", "").strip()
        chat_id = msg.get("chat", {}).get("id")

        if "reply_to_message" in msg:
            reply_text = msg.get("reply_to_message", {}).get("text", "")
            lines = reply_text.split("\n")
            job_title = lines[0].strip() if lines else "Operations Role"
            company = "Target Firm"
            for line in lines:
                if "Company:" in line:
                    company = line.replace("Company:", "").strip()
                    break

        cmd_clean = text.lower().strip()
        today_str = datetime.now().strftime("%Y-%m-%d")
        followup_date = (datetime.now() + timedelta(days=4)).strftime("%Y-%m-%d")

        # Dynamic Relative Snooze Command: "s <days>" (e.g. "s 3" or "/s 5")
        if cmd_clean.startswith("s ") or cmd_clean.startswith("/s "):
            try:
                days_to_add = int(cmd_clean.split()[1])
                new_followup = (datetime.now() + timedelta(days=days_to_add)).strftime("%Y-%m-%d")
                log_to_sheets_crm({
                    "action": "update_followup",
                    "company": company,
                    "next_followup": new_followup
                })
                msg_out = f"⏳ Snoozed <b>{html.escape(company)}</b> for {days_to_add} days (Next: {new_followup})."
            except Exception:
                msg_out = "⚠️ Invalid snooze format. Use <code>s 4</code> or <code>/s 3</code>."

            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": chat_id, "text": msg_out, "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

        # Carmen Urgency Queue Report
        if cmd_clean == "carmen":
            res = requests.post(CRM_WEBHOOK_URL, json={"action": "get_followups"}, timeout=10)
            if res.status_code == 200:
                raw_leads = res.json().get("followups", [])
                carmen_leads = [
                    l for l in raw_leads 
                    if str(l.get("tab", "")).upper() in ["CW", "CARMEN WARM", "CC", "CARMEN COLD"]
                ]
                if not carmen_leads:
                    send_status_update(chat_id, "No pending follow-ups in Carmen Warm or Carmen Cold queues.")
                    return jsonify({"status": "ok"}), 200

                ranked = []
                for lead in carmen_leads:
                    urgency, days_over = calculate_urgency(lead.get("due_date"))
                    ranked.append((urgency, days_over, lead))

                ranked.sort(key=lambda x: x[1], reverse=True)

                msg_body = "<b>Carmen Queue Urgency Report</b>\n\n"
                for urgency, days_over, lead in ranked:
                    comp = html.escape(str(lead.get("company", "Target")))
                    t_title = html.escape(str(lead.get("title", "Ops Role")))
                    tab_code = html.escape(str(lead.get("tab", "CW")))
                    msg_body += f"{urgency} ({days_over}d overdue) | <b>{comp}</b> [{tab_code}]\n   Role: {t_title} | Contact: {lead.get('email', 'N/A')}\n\n"

                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                              json={"chat_id": chat_id, "text": msg_body[:3990], "parse_mode": "HTML"})
                return jsonify({"status": "ok"}), 200

        # Email Drafting Command
        if cmd_clean in ["/draft", "draft"] or text.startswith("/email") or text.startswith("e "):
            parts = text.split(" ", 1)
            target_email = parts[1].strip() if (len(parts) > 1 and "@" in text) else resolve_target_email(company, job_title)
            success, err = create_gmail_draft(target_email, company, job_title, reply_text)
            status_msg = "✉️ <b>Gmail Draft Created!</b>" if success else f"⚠️ <b>Gmail Draft Failed:</b> <code>{html.escape(err)}</code>"
            tailored_intro = generate_tailored_intro(reply_text)

            email_text_block = (
                f"{status_msg}\n\n"
                f"<b>To:</b> <code>{html.escape(target_email)}</code>\n"
                f"<b>Subject:</b> Operations & Systems Alignment - {html.escape(job_title)} @ {html.escape(company)}\n\n"
                f"Hi Hiring Team,\n\n"
                f"I recently came across the {html.escape(job_title)} opening at {html.escape(company)} and wanted to reach out directly. "
                f"{html.escape(tailored_intro)}\n\n"
                f"I have attached my resume (PDF) to this message for your review and would welcome the opportunity to connect.\n\n"
                f"Best regards,\nKevin Miller"
            )
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": chat_id, "text": email_text_block[:3990], "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

        # Sheet Tab Routing Shortcuts
        tab_map = {
            "/tc": ("TC", "Tetiana Cold"), "tc": ("TC", "Tetiana Cold"),
            "/tw": ("TW", "Tetiana Warm"), "tw": ("TW", "Tetiana Warm"),
            "/cc": ("CC", "Carmen Cold"), "cc": ("CC", "Carmen Cold"),
            "/cw": ("CW", "Carmen Warm"), "cw": ("CW", "Carmen Warm"),
            "/d": ("D", "Died"), "d": ("D", "Died"),
            "/k": ("K", "Killed"), "k": ("K", "Killed"),
            "a": ("TC", "Applied"), "applied": ("TC", "Applied"), "x": ("D", "Died")
        }

        if cmd_clean in tab_map:
            code, tab_name = tab_map[cmd_clean]
            target_email = resolve_target_email(company, job_title)
            next_follow = followup_date if code in ["TC", "CC"] else ""
            add_company_cooldown(company)

            if tab_name == "Applied":
                log_to_sheets_crm({"action": "apply_job", "company": company})
                msg_out = f"✅ Marked applied for <b>{html.escape(company)}</b>."
            else:
                log_to_sheets_crm({
                    "action": "add_row",
                    "target_code": code,
                    "row_data": [today_str, company, job_title, target_email, "90", "Logged", next_follow, "", f"Logged via Telegram command '{code}'"]
                })
                msg_out = f"📁 Logged <b>{html.escape(company)}</b> directly to <b>{tab_name} ({code})</b> tab."

            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": chat_id, "text": msg_out, "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

        elif cmd_clean in ["/resume", "r", "resume"]:
            cheat_sheet = generate_resume_cheat_sheet(reply_text)
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": chat_id, "text": f"📋 <b>Resume Tips ({html.escape(company)}):</b>\n\n{html.escape(cheat_sheet)}", "parse_mode": "HTML"})
            return jsonify({"status": "ok"}), 200

        # Standalone Tab Logging
        if any(text.startswith(prefix) for prefix in ["/cc ", "/cw ", "/tc ", "/tw "]):
            parts = text.split(" ", 3)
            cmd = parts[0].replace("/", "").upper()
            email = parts[1] if len(parts) > 1 else ""
            comp = parts[2] if len(parts) > 2 else "Target Firm"
            t_title = parts[3] if len(parts) > 3 else "Executive"
            next_follow = (datetime.now() + timedelta(days=4)).strftime("%Y-%m-%d") if cmd in ["TC", "CC"] else ""
            add_company_cooldown(comp)

            log_to_sheets_crm({
                "action": "add_row",
                "target_code": cmd,
                "row_data": [today_str, comp, t_title, email, "Direct Log", "Contacted", next_follow, "LinkedIn", "Logged via Telegram"]
            })
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": f"📁 Logged <b>{html.escape(comp)}</b> to <b>{cmd} Tab</b>.", "parse_mode": "HTML"}
            )
            return jsonify({"status": "tab_logged"}), 200

        # Pipeline Control Commands
        if text in ["/run", "/start"]:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": chat_id, "text": "🚀 Pipeline initialized. Scanning Metro Detroit & Remote roles..."})
            threading.Thread(target=run_job_pipeline, args=(chat_id,)).start()
            return jsonify({"status": "started"}), 200

        elif text == "/sweep":
            send_status_update(chat_id, "Auditing sheet and fetching next top 3 priority overdue leads...")
            fetch_and_push_lead_batch(chat_id=chat_id, batch_size=3)
            return jsonify({"status": "swept"}), 200

    return jsonify({"status": "ignored"}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
