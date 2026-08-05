import base64
import json
import os
import re
import time
import urllib.parse
from email.message import EmailMessage
import dns.resolver
import requests

# ===========================================================================
# ENVIRONMENT VARIABLES & SECRETS
# ===========================================================================
API_KEY = os.environ.get("OPENWEBNINJA_KEY") or os.environ.get("RAPIDAPI_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN")
GMAIL_USER = os.environ.get("GMAIL_USER")
CRM_WEBHOOK_URL = os.environ.get("CRM_WEBHOOK_URL")

# OpenWeb Ninja Direct Endpoint
JSEARCH_URL = "https://api.openwebninja.com/jsearch/search"

# ===========================================================================
# FILTER ARRAYS & TARGET SEARCH QUERIES
# ===========================================================================
TITLE_EXCLUSIONS = [
    "sales", "account executive", "bdr", "sdr",
    "financial planner", "client relationship manager",
    "agent", "wholesaler", "producer", "insurance agent",
    "teller", "branch", "personal banker", "loan officer", "mortgage",
    "intern", "internship", "customer service representative", "call center"
]

COMPANY_EXCLUSIONS = [
    "cybercoders", "robert half", "kforce", "jobot", "actalent", "insight global"
]

TARGET_QUERIES = [
    "Wealth Operations OR Brokerage Operations Detroit, MI",
    "Compliance Analyst OR Fintech Operations Michigan",
    "Business Operations Analyst OR RevOps Analyst Remote Michigan",
    "Schwab OR Fidelity OR Custodial Operations Michigan",
    "Financial Systems Analyst OR Process Automation Detroit, MI"
]

SYSTEM_PROMPT = """You are a strict technical job screener. Evaluate the candidate's alignment with the job posting.
Candidate Profile Summary:
- Background: Wealth Operations Specialist at a venture-backed fintech startup (Signal Advisors).
- Experience: RIA onboarding, custodian workflows (Schwab/Fidelity), DocuSign, Salesforce, SLA management, annuity compliance, Series 65 candidate, Python/SQL automation.
Target: Back-office operations, middle-office finance, fintech compliance, operational automation.
Strictly FORBIDDEN: Sales, client pitching, commission-based roles, retail bank tellers, cold calling.

Respond ONLY with JSON matching this structure: {"pass": true/false, "reason": "Short string explanation"}
Set "pass" to false IMMEDIATELY if the role requires generating new client leads, hitting sales quotas, or selling financial products.
"""

# ===========================================================================
# HELPER FUNCTIONS
# ===========================================================================
def call_gemini_api(prompt, system_prompt=None):
    """Executes direct REST request against the stable auto-updating Flash-Lite endpoint."""
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
        print(f"Gemini API Error ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"Gemini API Exception: {e}")

    return None

def build_linkedin_exec_url(company_name):
    """Generates a pre-filtered LinkedIn search URL for target VPs and Directors."""
    clean_company = re.sub(r'[^a-zA-Z0-9\s]', '', company_name).strip()
    query = f'"{clean_company}" ("VP" OR "Director" OR "Head") ("Operations" OR "Compliance")'
    encoded = urllib.parse.quote(query)
    return f"https://www.linkedin.com/search/results/people/?keywords={encoded}"

def resolve_target_email(company_domain, contact_name=None, job_title=""):
    """Generates named email pattern or department-specific operational fallbacks."""
    domain = company_domain.lower().replace("https://", "").replace("http://", "").replace("www.", "").strip("/")
    
    # Check MX Records
    try:
        mx_records = dns.resolver.resolve(domain, 'MX')
        if not mx_records:
            return f"careers@{domain}"
    except Exception:
        return f"contact@{domain}"

    # Priority 1: Named Contact Pattern
    if contact_name and str(contact_name).lower() not in ["unknown", "n/a", "none", "null"]:
        name_parts = re.sub(r'[^a-zA-Z\s]', '', contact_name).lower().split()
        if len(name_parts) >= 2:
            return f"{name_parts[0]}.{name_parts[-1]}@{domain}"
        elif len(name_parts) == 1:
            return f"{name_parts[0]}@{domain}"

    # Priority 2: Department-Specific Operational Routing
    title_lower = job_title.lower()
    if "compliance" in title_lower:
        return f"compliance@{domain}"
    elif any(kw in title_lower for kw in ["wealth", "custody", "brokerage", "ria"]):
        return f"wealthops@{domain}"
    elif any(kw in title_lower for kw in ["systems", "automation", "revops"]):
        return f"bizops@{domain}"

    return f"operations@{domain}"

def passes_strict_filter(job):
    """Deterministic filter against title, company, sales triggers, and Michigan location."""
    title = job.get("job_title", "").lower()
    description = job.get("job_description", "").lower()
    company = job.get("employer_name", "").lower()
    state = str(job.get("job_state", "")).upper()
    country = str(job.get("job_country", "")).upper()
    city = str(job.get("job_city", "")).lower()

    # Location Lock: Michigan residents only
    is_mi = state == "MI" or "michigan" in city or "detroit" in city or "mi" in state
    is_remote = job.get("job_is_remote", False) or "remote" in description[:300]
    
    if not (is_mi or is_remote) and country == "US":
        return False

    # Exclusions
    if any(term in title for term in TITLE_EXCLUSIONS):
        return False
    if any(comp in company for comp in COMPANY_EXCLUSIONS):
        return False

    sales_triggers = ["cold call", "commission", "prospecting", "lead generation quota"]
    if any(trigger in description for trigger in sales_triggers):
        return False

    return True

def evaluate_job_with_gemini(job):
    """AI evaluation using dynamic Gemini REST call."""
    if not GEMINI_API_KEY:
        return True, "Gemini key not configured; skipping AI evaluation."

    prompt = f"Job Title: {job.get('job_title')}\nCompany: {job.get('employer_name')}\nDescription:\n{job.get('job_description', '')[:3000]}"
    raw_text = call_gemini_api(prompt, SYSTEM_PROMPT)

    if raw_text:
        try:
            res_data = json.loads(raw_text.strip())
            return res_data.get("pass", False), res_data.get("reason", "No reason provided")
        except Exception as e:
            print(f"Gemini Evaluation JSON Parsing Error: {e}")
            return True, "Fallback pass on AI error"

    return True, "Fallback pass on API failure"

def extract_variables_with_gemini(job):
    """Extracts structured fields using Gemini REST call."""
    default_payload = {
        "company_name": job.get("employer_name", "N/A"),
        "company_domain": f"{job.get('employer_name', 'company').lower().replace(' ', '')}.com",
        "job_title": job.get("job_title", "N/A"),
        "primary_responsibility": "Operations Management",
        "core_tool": "Python/SQL",
        "key_qualification": "Workflow Optimization",
        "hiring_manager_name": None
    }

    if not GEMINI_API_KEY:
        return default_payload

    prompt = (
        "Extract these variables from the job posting in JSON: "
        "company_name, company_domain (e.g. acme.com), job_title, primary_responsibility, "
        "core_tool, key_qualification, hiring_manager_name (return null if not found).\n\n"
        f"Employer: {job.get('employer_name')}\nTitle: {job.get('job_title')}\nDescription:\n{job.get('job_description', '')[:2500]}"
    )

    raw_text = call_gemini_api(prompt)
    if raw_text:
        try:
            return json.loads(raw_text.strip())
        except Exception as e:
            print(f"Gemini Extraction JSON Parsing Error: {e}")
            return default_payload

    return default_payload

def log_to_sheets_crm(payload):
    """Logs job record directly to Google Sheets via Apps Script Webhook."""
    if not CRM_WEBHOOK_URL:
        print("CRM_WEBHOOK_URL missing; skipping CRM log.")
        return
    try:
        res = requests.post(CRM_WEBHOOK_URL, json=payload, timeout=10)
        print(f"Logged to Sheets CRM: {res.status_code}")
    except Exception as e:
        print(f"Error logging to Sheets CRM: {e}")

def fetch_jobs(query):
    """Retrieves listings directly from OpenWeb Ninja JSearch API."""
    headers = {"x-api-key": API_KEY}
    params = {
        "query": query,
        "page": "1",
        "num_pages": "1",
        "date_posted": "3days"
    }

    try:
        response = requests.get(JSEARCH_URL, headers=headers, params=params, timeout=20)
        response.raise_for_status()
        return response.json().get("data", [])
    except Exception as e:
        print(f"Error fetching jobs for query '{query}': {e}")
        return []

def send_telegram_notification(job_id, extracted_vars, target_email, apply_link):
    """Sends notification with inline Approve button and LinkedIn Exec Search to Telegram."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("Telegram tokens missing; skipping message.")
        return

    company = str(extracted_vars.get('company_name', 'N/A')).replace('*', '').replace('_', '').replace('[', '').replace(']', '')
    title = str(extracted_vars.get('job_title', 'N/A')).replace('*', '').replace('_', '').replace('[', '').replace(']', '')
    tool = str(extracted_vars.get('core_tool', 'N/A')).replace('*', '').replace('_', '').replace('[', '').replace(']', '')
    email = str(target_email).replace('*', '').replace('_', '')
    link = str(apply_link)
    linkedin_url = build_linkedin_exec_url(company)

    text = (
        f"🎯 *New Matched Role*\n"
        f"🏢 *Company:* {company}\n"
        f"💼 *Title:* {title}\n"
        f"📧 *Target Email:* {email}\n"
        f"🛠 *Tool:* {tool}\n"
        f"🔗 *Apply Direct:* [Job Link]({link})\n"
        f"👤 *Find Decision-Maker:* [LinkedIn Exec Search]({linkedin_url})"
    )

    safe_callback = f"approve_{str(job_id)[:50]}"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "Approve & Draft Email", "callback_data": safe_callback}
            ]]
        }
    }

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            print(f"Successfully posted job {job_id} to Telegram.")
        else:
            print(f"Telegram API Error ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"Error posting to Telegram: {e}")

# ===========================================================================
# MAIN EXECUTION ENTRYPOINT
# ===========================================================================
def main():
    if not all([API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, CRM_WEBHOOK_URL]):
        print("Error: Missing required basic environment variables.")
        return

    seen_job_ids = set()
    matched_jobs_count = 0

    for query in TARGET_QUERIES:
        print(f"Searching: {query}")
        jobs = fetch_jobs(query)
        for job in jobs:
            job_id = job.get("job_id")
            if not job_id or job_id in seen_job_ids:
                continue
            seen_job_ids.add(job_id)

            if passes_strict_filter(job):
                ai_pass, reason = evaluate_job_with_gemini(job)
                if not ai_pass:
                    print(f"Skipped by AI: {job.get('job_title')} @ {job.get('employer_name')} - Reason: {reason}")
                    continue

                extracted_vars = extract_variables_with_gemini(job)
                domain = extracted_vars.get("company_domain") or f"{extracted_vars.get('company_name', 'company').lower().replace(' ', '')}.com"
                hiring_manager = extracted_vars.get("hiring_manager_name")
                job_title = extracted_vars.get("job_title", job.get("job_title", ""))
                
                target_email = resolve_target_email(domain, hiring_manager, job_title)

                payload = {
                    "action": "log_job",
                    "job_id": job_id,
                    "company_name": extracted_vars.get("company_name", job.get("employer_name")),
                    "job_title": job_title,
                    "primary_responsibility": extracted_vars.get("primary_responsibility", "N/A"),
                    "core_tool": extracted_vars.get("core_tool", "N/A"),
                    "key_qualification": extracted_vars.get("key_qualification", "N/A"),
                    "target_email": target_email
                }

                log_to_sheets_crm(payload)
                apply_link = job.get("job_apply_link", "#")
                send_telegram_notification(job_id, extracted_vars, target_email, apply_link)
                matched_jobs_count += 1

    print(f"Finished pipeline execution. Matched {matched_jobs_count} roles.")

if __name__ == "__main__":
    main()
