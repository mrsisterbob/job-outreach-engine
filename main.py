import base64
import json
import os
import re
import threading
import time
import urllib.parse
from email.message import EmailMessage
import requests
from flask import Flask, jsonify, request

# ==========================================
# 1. ENVIRONMENT VARIABLES & FLASK INITIALIZATION
# ==========================================
app = Flask(__name__)

API_KEY = os.environ.get("OPENWEBNINJA_KEY") or os.environ.get("RAPIDAPI_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
CRM_WEBHOOK_URL = os.environ.get("CRM_WEBHOOK_URL")

# Gmail OAuth Credentials
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN")
GMAIL_USER = os.environ.get("GMAIL_USER")

JSEARCH_URL = "https://api.openwebninja.com/jsearch/search"

# ==========================================
# 2. FILTER RULES & EXCLUSIONS
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
# 3. HELPER FUNCTIONS & INTEGRATIONS
# ==========================================
def call_gemini_api(prompt, system_prompt=None):
    """Executes REST request to Gemini Flash-Lite endpoint."""
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

def resolve_target_email(company_name, job_title=""):
    """Generates a clean domain-based email routing fallback."""
    clean_domain = re.sub(r'[^a-zA-Z0-9]', '', company_name).lower() + ".com"
    title_lower = job_title.lower()
    if "compliance" in title_lower:
        return f"compliance@{clean_domain}"
    elif any(kw in title_lower for kw in ["wealth", "custody", "brokerage", "ria"]):
        return f"wealthops@{clean_domain}"
    elif any(kw in title_lower for kw in ["systems", "automation", "revops"]):
        return f"bizops@{clean_domain}"
    return f"operations@{clean_domain}"

def passes_strict_filter(job):
    """Filters title, company, sales triggers, and enforces Metro Detroit location."""
    title = job.get("job_title", "").lower()
    description = job.get("job_description", "").lower()
    company = job.get("employer_name", "").lower()
    state = str(job.get("job_state", "")).upper()
    city = str(job.get("job_city", "")).lower()

    # Metro Detroit / SE Michigan Geographical Boundary
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
    """Dispatches webhook payloads directly to Google Apps Script CRM."""
    if not CRM_WEBHOOK_URL:
        return
    try:
        requests.post(CRM_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"CRM Log Error: {e}")

def create_gmail_draft(to_email, company_name, job_title):
    """Refreshes OAuth token and injects a draft email into your Gmail account."""
    if not all([GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN, GMAIL_USER]):
        print("Gmail OAuth credentials missing.")
        return False

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
            print("Failed to acquire Gmail access token.")
            return False

        message = EmailMessage()
        message["To"] = to_email
        message["From"] = GMAIL_USER
        message["Subject"] = f"Operations & Systems Alignment — {job_title} @ {company_name}"
        
        body = (
            f"Hi Hiring Team,\n\n"
            f"I recently came across the {job_title} opening at {company_name} and wanted to reach out directly. "
            f"My background centers on wealth operations, custodial workflows, and process automation (Python/SQL).\n\n"
            f"I've attached my resume for reference and would welcome the opportunity to connect.\n\n"
            f"Best regards,\n"
            f"Kevin Miller"
        )
        message.set_content(body)

        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        draft_url = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        draft_payload = {"message": {"raw": raw_message}}

        res = requests.post(draft_url, headers=headers, json=draft_payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        print(f"Error creating Gmail draft: {e}")
        return False

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

def send_telegram_card(job, reason, target_email):
    """Sends structured job card to Telegram with actionable links and working inline draft button."""
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
        f"✉️ *Target Email:* `{target_email}`\n"
        f"🏷️ *Type:* {tag}\n\n"
        f"💡 *Fit Reason:* {reason}\n\n"
        f"🔗 [1. Apply Direct]({apply_link})\n"
        f"⚡ [2. Open Leads in Apollo]({apollo_url})\n"
        f"🔍 [3. Open People on LinkedIn]({linkedin_url})\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 Reply to this card with:\n`/email [found_email]` to log CRM & draft Gmail."
    )

    # Encapsulate payload under 64 bytes for Telegram callback data security
    safe_callback = f"app|{target_email[:25]}|{company[:15]}|{title[:15]}"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✉️ Approve & Draft Email", "callback_data": safe_callback}
            ]]
        }
    }
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json=payload, timeout=10)

def run_job_pipeline():
    """Main execution loop for scanning, filtering, logging, and dispatching roles."""
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
                    target_email = resolve_target_email(job.get("employer_name", "company"), job.get("job_title", ""))
                    send_telegram_card(job, reason, target_email)
                    log_to_sheets_crm({
                        "action": "log_job",
                        "job_id": job_id,
                        "company": job.get("employer_name"),
                        "title": job.get("job_title"),
                        "target_email": target_email,
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
    if not data:
        return jsonify({"status": "ignored"}), 200

    # 1. Handle Inline Button Clicks ("Approve & Draft Email")
    if "callback_query" in data:
        callback = data["callback_query"]
        callback_id = callback.get("id")
        callback_data = callback.get("data", "")

        if callback_data.startswith("app|"):
            parts = callback_data.split("|")
            to_email = parts[1] if len(parts) > 1 else "contact@company.com"
            company = parts[2] if len(parts) > 2 else "Company"
            title = parts[3] if len(parts) > 3 else "Operations Role"

            # Execute Gmail Draft API call & Update CRM Sheet
            draft_success = create_gmail_draft(to_email, company, title)
            log_to_sheets_crm({"action": "update_email", "email": to_email})

            alert_text = f"✅ Draft created in Gmail for {to_email}!" if draft_success else "❌ Draft failed. Check OAuth credentials."
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": alert_text, "show_alert": True}
            )
        return jsonify({"status": "success"}), 200

    # 2. Handle Text Commands (/run, /start, /email)
    if "message" in data:
        message = data["message"]
        text = message.get("text", "").strip()
        chat_id = message.get("chat", {}).get("id")

        if text in ["/run", "/start"]:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": "🚀 Pipeline started in background. Scanning Metro Detroit..."}
            )
            # Threaded non-blocking execution prevents Gunicorn timeouts on Render
            thread = threading.Thread(target=run_job_pipeline)
            thread.start()

        elif text.startswith("/email "):
            found_email = text.replace("/email ", "").strip()
            log_to_sheets_crm({"action": "update_email", "email": found_email})
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": f"📥 Updated CRM Sheet with: {found_email}"}
            )

    return jsonify({"status": "success"}), 200

# ==========================================
# 5. EXECUTION ENTRYPOINT (RENDER VS GITHUB)
# ==========================================
if __name__ == '__main__':
    # Check if running in GitHub Actions CI/CD environment
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("Running pipeline directly via GitHub Actions...")
        run_job_pipeline()
    else:
        # Running locally or fallback
        app.run(host='0.0.0.0', port=5000)
