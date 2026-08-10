import base64
import html
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
# 1. ENVIRONMENT VARIABLES & INITIALIZATION
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

SENIORITY_EXCLUSIONS = [
    "senior", "lead", "manager", "director", "vp", "executive", "principal", "head of"
]

# 7 Expanded Target Queries (~210 raw jobs scanned per run)
TARGET_QUERIES = [
    "Wealth Operations Detroit MI",
    "Fintech Operations Michigan",
    "Business Operations Analyst Detroit MI",
    "Custodial Operations Schwab Fidelity Michigan",
    "Financial Systems Process Automation Detroit MI",
    "Brokerage Operations Analyst Detroit MI",
    "Trade Operations Specialist Michigan"
]

SYSTEM_PROMPT = """You are a strict technical job screener evaluating roles for an early-career candidate (0-2 years experience).
Target Profile: Non-sales W-2 roles in Tech, FinTech, Auto Tech, or Back-Office Systems/Operations in Metro Detroit.
High Priority Skills: Python, SQL, Salesforce, Excel, Schwab SAC, Fidelity Wealthscape, DocuSign, Process Automation.
Strictly FORBIDDEN: Sales, cold calling, client pitching, commission-based roles, retail bank tellers, CPA tracks, Senior/Lead/Manager roles.

Evaluate the job description and respond ONLY with a JSON object containing:
{
  "score": <integer between 1 and 100 representing fit signal>,
  "reason": "<1-sentence concise explanation of why this role fits or does not fit>"
}"""

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
    encoded = urllib.parse.quote(f'{clean_company} ("VP" OR "Director" OR "Manager") ("Operations" OR "Compliance")')
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
    """Stage 1: Hard deterministic pre-filtering ($0 cost)."""
    title = job.get("job_title", "").lower()
    description = job.get("job_description", "").lower()
    company = job.get("employer_name", "").lower()
    state = str(job.get("job_state", "")).upper()
    city = str(job.get("job_city", "")).lower()

    # Geographical boundary (Metro Detroit / SE Michigan)
    valid_cities = ["farmington", "detroit", "ann arbor", "novi", "troy", "southfield", "auburn hills", "plymouth", "royal oak"]
    is_se_mi = state == "MI" or any(c in city for c in valid_cities)
    if not is_se_mi:
        return False

    # Exclusions & Keyword Auto-Drops
    if any(term in title for term in TITLE_EXCLUSIONS):
        return False
    if any(comp in company for comp in COMPANY_EXCLUSIONS):
        return False
    if any(trigger in description for trigger in HARD_BAN_KEYWORDS):
        return False

    # Seniority Ceiling (0-2 years max)
    if any(sen in title for sen in SENIORITY_EXCLUSIONS):
        return False

    return True

def evaluate_job_with_gemini(job):
    """Stage 2: AI Scoring (0-100 scale, threshold >= 70)."""
    if not GEMINI_API_KEY:
        return True, 75, "Gemini key missing; fallback pass."

    prompt = f"Job Title: {job.get('job_title')}\nCompany: {job.get('employer_name')}\nDescription:\n{job.get('job_description', '')[:2500]}"
    raw_text = call_gemini_api(prompt, SYSTEM_PROMPT)

    if raw_text:
        try:
            res_data = json.loads(raw_text.strip())
            score = int(res_data.get("score", 0))
            reason = res_data.get("reason", "No reason provided")
            return (score >= 70), score, reason
        except Exception:
            pass
            
    return True, 70, "Fallback pass on parse error"

def log_to_sheets_crm(payload):
    """Dispatches webhook payloads directly to Google Apps Script CRM."""
    if not CRM_WEBHOOK_URL:
        return
    try:
        requests.post(CRM_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"CRM Log Error: {e}")

def create_gmail_draft(to_email, company_name, job_title):
    """Refreshes OAuth token and injects a draft email into Gmail."""
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
        message["Subject"] = f"Operations & Systems Alignment - {job_title} @ {company_name}"
        
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

def process_manual_email(chat_id, company_name, found_email, job_title="Operations Role"):
    """Background worker for drafting Gmail and updating Google Sheets CRM."""
    draft_success = create_gmail_draft(found_email, company_name, job_title)
    log_to_sheets_crm({"action": "update_email", "email": found_email})

    msg_text = (
        f"✅ <b>Draft created in Gmail</b> for <code>{found_email}</code>!\n"
        f"📊 Updated CRM Sheet for <b>{html.escape(company_name)}</b>."
        if draft_success else
        f"⚠️ Updated CRM Sheet with <code>{found_email}</code>, but Gmail draft failed. Check OAuth credentials."
    )

    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": msg_text, "parse_mode": "HTML"}
    )

def fetch_jobs(query):
    """Fetches jobs pulling 2 pages per query (~30 jobs per term)."""
    headers = {"x-api-key": API_KEY}
    params = {"query": query, "page": "1", "num_pages": "2", "date_posted": "month"}
    try:
        res = requests.get(JSEARCH_URL, headers=headers, params=params, timeout=20)
        if res.status_code == 200:
            return res.json().get("data", [])
    except Exception as e:
        print(f"Error fetching jobs: {e}")
    return []

def send_telegram_card(job, score, reason, target_email):
    """Sends a clean HTML-formatted job card displaying score and dual inline buttons."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return

    company = html.escape(job.get("employer_name", "N/A"))
    title = html.escape(job.get("job_title", "N/A"))
    apply_link = job.get("job_apply_link", "#")
    
    is_easy_apply = "linkedin.com" in apply_link.lower()
    tag = "⚡ EASY APPLY (LinkedIn)" if is_easy_apply else "🌐 DIRECT ATS (Company Site)"
    
    apollo_url = build_apollo_url(company)
    linkedin_url = build_linkedin_url(company)

    card_text = (
        f"📌 <b>{title}</b>\n"
        f"🏢 <b>Company:</b> {company}\n"
        f"🏷️ <b>Type:</b> {tag}\n"
        f"🎯 <b>Fit Score:</b> {score}/100\n"
        f"✉️ <b>Default Email:</b> <code>{target_email}</code>\n\n"
        f"💡 <b>Fit Reason:</b> {html.escape(reason)}\n\n"
        f"🔗 <a href='{apply_link}'>1. Apply Direct</a>\n"
        f"⚡ <a href='{apollo_url}'>2. Open Leads in Apollo</a>\n"
        f"🔍 <a href='{linkedin_url}'>3. Open People on LinkedIn</a>"
    )

    safe_company = re.sub(r'[^a-zA-Z0-9\s]', '', job.get("employer_name", "Company"))[:15].strip()
    safe_email = target_email[:30]

    reply_markup = {
        "inline_keyboard": [[
            {"text": "✉️ Approve Default Email", "callback_data": f"approve:{safe_company}:{safe_email}"},
            {"text": "⚡ Add Custom Email", "callback_data": f"prompt:{safe_company}"}
        ]]
    }

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": card_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": reply_markup
    }

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json=payload, timeout=10)

def run_job_pipeline(top_n=5):
    """Scans ~210 raw roles, ranks candidate pool by score, and dispatches ONLY the top N."""
    seen_ids = set()
    candidate_pool = []

    for query in TARGET_QUERIES:
        jobs = fetch_jobs(query)
        for job in jobs:
            job_id = job.get("job_id")
            if not job_id or job_id in seen_ids:
                continue
            seen_ids.add(job_id)

            if passes_strict_filter(job):
                ai_pass, score, reason = evaluate_job_with_gemini(job)
                if ai_pass:
                    target_email = resolve_target_email(
                        job.get("employer_name", "company"), 
                        job.get("job_title", "")
                    )
                    candidate_pool.append({
                        "job": job,
                        "score": score,
                        "reason": reason,
                        "target_email": target_email
                    })

    # "Best Man Wins" Leaderboard Sort (Descending)
    candidate_pool.sort(key=lambda x: x["score"], reverse=True)

    # Slice top N winners
    top_matches = candidate_pool[:top_n]

    # Dispatch and Log ONLY the winning top 5 roles
    for item in top_matches:
        job = item["job"]
        score = item["score"]
        reason = item["reason"]
        target_email = item["target_email"]

        send_telegram_card(job, score, reason, target_email)
        log_to_sheets_crm({
            "action": "log_job",
            "job_id": job.get("job_id"),
            "company": job.get("employer_name"),
            "title": job.get("job_title"),
            "target_email": target_email,
            "job_url": job.get("job_apply_link", "")
        })

    return len(top_matches)

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

    # 1. Handle Inline Button Clicks
    if "callback_query" in data:
        callback = data["callback_query"]
        callback_id = callback.get("id")
        chat_id = callback["message"]["chat"]["id"]
        callback_data = callback.get("data", "")

        if callback_data.startswith("approve:"):
            parts = callback_data.split(":", 2)
            company = parts[1] if len(parts) > 1 else "Company"
            email = parts[2] if len(parts) > 2 else "operations@company.com"

            threading.Thread(target=process_manual_email, args=(chat_id, company, email)).start()
            
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": f"Drafting email for {company}..."}
            )
            return jsonify({"status": "approved"}), 200

        elif callback_data.startswith("prompt:"):
            company = callback_data.split(":", 1)[1] if ":" in callback_data else "Target Company"
            
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": f"📩 Paste the contact email found on Apollo/LinkedIn for <b>{html.escape(company)}</b> below:",
                    "parse_mode": "HTML",
                    "reply_markup": {"force_reply": True, "selective": True}
                }
            )
            return jsonify({"status": "prompted"}), 200

    # 2. Handle Text Commands & Direct Email Replies
    if "message" in data:
        message = data["message"]
        text = message.get("text", "").strip()
        chat_id = message.get("chat", {}).get("id")

        if text in ["/run", "/start"]:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": "🚀 Pipeline started in background. Scanning Metro Detroit..."}
            )
            threading.Thread(target=run_job_pipeline).start()
            return jsonify({"status": "started"}), 200

        # Regex auto-detect any email string inside a message or swipe-reply
        email_match = re.search(r'[a-zA-Z0-9%+\_.-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
        if email_match:
            found_email = email_match.group(0)
            company_name = "Target Company"

            if "reply_to_message" in message:
                original_card = message["reply_to_message"].get("text", "")
                company_match = re.search(r'Company:\s*(.*)', original_card)
                if company_match:
                    company_name = company_match.group(1).split('\n')[0].strip()

            threading.Thread(target=process_manual_email, args=(chat_id, company_name, found_email)).start()
            return jsonify({"status": "processing"}), 200

    return jsonify({"status": "ignored"}), 200

# ==========================================
# 5. EXECUTION ENTRYPOINT (RENDER VS GITHUB)
# ==========================================
if __name__ == '__main__':
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("Running pipeline directly via GitHub Actions...")
        run_job_pipeline()
    else:
        app.run(host='0.0.0.0', port=5000)
