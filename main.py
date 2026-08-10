import os
import re
import json
import urllib.parse
import requests
from flask import Flask, request, jsonify

# ==========================================
# 1. ENVIRONMENT VARIABLES & FLASK INITIALIZATION
# ==========================================
app = Flask(__name__)

API_KEY = os.environ.get("OPENWEBNINJA_KEY") or os.environ.get("RAPIDAPI_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
CRM_WEBHOOK_URL = os.environ.get("CRM_WEBHOOK_URL")

JSEARCH_URL = "https://api.openwebninja.com/jsearch/search"

# ==========================================
# 2. FILTER ARRAYS & SEARCH QUERIES
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
    "uncapped potential", "commission", "hustle", "grind", "door-to-door",
    "phone jockey", "call jockey", "cold calling"
]

TARGET_QUERIES = [
    "Wealth Operations Detroit MI",
    "Fintech Operations Michigan",
    "Business Operations Analyst Detroit MI",
    "Custodial Operations Schwab Fidelity Michigan",
    "Financial Systems Process Automation Detroit MI"
]

SYSTEM_PROMPT = """You are a strict technical job screener evaluating roles for an early-career candidate (0-2 years experience).
Target Profile: Non-sales W-2 roles in Tech, FinTech, Auto Tech, or Back-Office Systems/Operations in Metro Detroit.
Strictly FORBIDDEN: Sales, cold calling, client pitching, commission-based roles, retail bank tellers, CPA tracks, Senior/Lead/Manager roles requiring >3 years experience.

Respond ONLY with JSON matching this structure: {"pass": true/false, "reason": "Short explanation"}"""

# ==========================================
# 3. HELPER FUNCTIONS
# ==========================================
def call_gemini_api(prompt, system_prompt=None):
    if not GEMINI_API_KEY:
        return None
    full_prompt = f"{system_prompt}\n\n{prompt}".strip() if system_prompt else prompt
    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"}
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-lite-latest:generateContent?key={GEMINI_API_KEY}"
    try:
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            return res.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print(f"Gemini API Error: {e}")
    return None

def build_apollo_url(company_name):
    clean_company = re.sub(r'[^a-zA-Z0-9\s]', '', company_name).strip()
    encoded = urllib.parse.quote(f"{clean_company} Operations")
    return f"https://app.apollo.io/#/people?qKeywords={encoded}"

def build_linkedin_url(company_name):
    clean_company = re.sub(r'[^a-zA-Z0-9\s]', '', company_name).strip()
    encoded = urllib.parse.quote(f'"{clean_company}" ("VP" OR "Director" OR "Manager") ("Operations" OR "Compliance")')
    return f"https://www.linkedin.com/search/results/people/?keywords={encoded}"

def passes_strict_filter(job):
    title = job.get("job_title", "").lower()
    description = job.get("job_description", "").lower()
    company = job.get("employer_name", "").lower()
    state = str(job.get("job_state", "")).upper()
    city = str(job.get("job_city", "")).lower()

    # Metro Detroit / SE Michigan Boundary
    valid_cities = ["farmington", "detroit", "ann arbor", "novi", "troy", "southfield", "auburn hills", "plymouth", "royal oak"]
    is_se_mi = state == "MI" or any(c in city for c in valid_cities)
    if not is_se_mi:
        return False

    if any(term in title for term in TITLE_EXCLUSIONS):
        return False
    if any(comp in company for comp in COMPANY_EXCLUSIONS):
        return False
    if any(trigger in description for trigger in HARD_BAN_KEYWORDS):
        return False

    # Seniority Ceiling (0-2 years max)
    if any(sen in title for sen in ["senior", "lead", "manager", "director", "vp", "executive"]):
        return False

    return True

def evaluate_job_with_gemini(job):
    if not GEMINI_API_KEY:
        return True, "Gemini key missing; fallback pass."
    prompt = f"Job Title: {job.get('job_title')}\nCompany: {job.get('employer_name')}\nDescription:\n{job.get('job_description', '')[:2500]}"
    raw_text = call_gemini_api(prompt, SYSTEM_PROMPT)
    if raw_text:
        try:
            res_data = json.loads(raw_text.strip())
            return res_data.get("pass", False), res_data.get("reason", "No reason provided")
        except Exception:
            pass
    return True, "Fallback pass on error"

def log_to_sheets_crm(payload):
    if not CRM_WEBHOOK_URL:
        return
    try:
        requests.post(CRM_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"CRM Log Error: {e}")

def fetch_jobs(query):
    headers = {"x-api-key": API_KEY}
    params = {"query": query, "page": "1", "num_pages": "1", "date_posted": "3days"}
    try:
        res = requests.get(JSEARCH_URL, headers=headers, params=params, timeout=20)
        if res.status_code == 200:
            return res.json().get("data", [])
    except Exception as e:
        print(f"Error fetching jobs: {e}")
    return []

def send_telegram_card(job, reason):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    
    company = job.get("employer_name", "N/A")
    title = job.get("job_title", "N/A")
    apply_link = job.get("job_apply_link", "#")
    is_easy_apply = "linkedin.com" in apply_link.lower()
    tag = "⚡ EASY APPLY (LinkedIn)" if is_easy_apply else "🌐 DIRECT ATS (Company Site)"
    
    apollo_url = build_apollo_url(company)
    linkedin_url = build_linkedin_url(company)

    text = (
        f"📌 *{title}*\n"
        f"🏢 *Company:* {company}\n"
        f"🏷️ *Type:* {tag}\n\n"
        f"💡 *Fit Reason:* {reason}\n\n"
        f"🔗 [1. Apply Direct]({apply_link})\n"
        f"⚡ [2. Open Leads in Apollo]({apollo_url})\n"
        f"🔍 [3. Open People on LinkedIn]({linkedin_url})\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 Reply to this message with:\n`/email [found_email]` to log CRM & draft Gmail."
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    requests.post(url, json=payload, timeout=10)

def run_job_pipeline():
    seen_ids = set()
    matches = 0
    for query in TARGET_QUERIES:
        jobs = fetch_jobs(query)
        for job in jobs:
            job_id = job.get("job_id")
            if not job_id or job_id in seen_ids:
                continue
            seen_ids.add(job_id)

            if passes_strict_filter(job):
                ai_pass, reason = evaluate_job_with_gemini(job)
                if ai_pass:
                    send_telegram_card(job, reason)
                    log_to_sheets_crm({
                        "action": "log_job",
                        "job_id": job_id,
                        "company": job.get("employer_name"),
                        "title": job.get("job_title"),
                        "job_url": job.get("job_apply_link", "")
                    })
                    matches += 1
    return matches

# ==========================================
# 4. FLASK SERVER & TELEGRAM WEBHOOK ROUTES
# ==========================================
@app.route('/', methods=['GET'])
def health_check():
    return "Job Outreach Engine Webhook is Live!", 200

@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    data = request.get_json()
    if not data or "message" not in data:
        return jsonify({"status": "ignored"}), 200

    message = data["message"]
    text = message.get("text", "").strip()
    chat_id = message.get("chat", {}).get("id")

    # Command 1: Trigger manual scan via Telegram (/run or /start)
    if text in ["/run", "/start"]:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "🚀 Pipeline execution started. Scanning Metro Detroit roles..."}
        )
        count = run_job_pipeline()
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": f"✅ Execution finished. Found {count} high-signal matches."}
        )

    # Command 2: Reply with found email (/email john.doe@company.com)
    elif text.startswith("/email "):
        found_email = text.replace("/email ", "").strip()
        log_to_sheets_crm({"action": "update_email", "email": found_email})
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": f"📥 Updated CRM Sheet & Draft created for: {found_email}"}
        )

    return jsonify({"status": "success"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
