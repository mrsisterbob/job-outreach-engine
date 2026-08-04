import base64
import json
import os
import re
from email.message import EmailMessage

import dns.resolver
import google.generativeai as genai
import requests

# ---------------------------------------------------------------------------
# Environment Variables & Secrets
# ---------------------------------------------------------------------------
# Fallback logic to accept either OPENWEBNINJA_KEY or RAPIDAPI_KEY
API_KEY = os.environ.get("OPENWEBNINJA_KEY") or os.environ.get("RAPIDAPI_KEY")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN")
GMAIL_USER = os.environ.get("GMAIL_USER")
CRM_WEBHOOK_URL = os.environ.get("CRM_WEBHOOK_URL")

# OpenWeb Ninja Native Direct Endpoint
JSEARCH_URL = "https://api.openwebninja.com/jsearch/search"

# Initialize Gemini Client
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# ---------------------------------------------------------------------------
# Filter Arrays & Target Search Queries
# ---------------------------------------------------------------------------
TITLE_EXCLUSIONS = [
    "sales", "account executive", "business development", "bdr", "sdr",
    "advisor", "wealth advisor", "financial planner", "client relationship manager",
    "relationship manager", "agent", "wholesaler", "producer", "insurance agent",
    "teller", "branch", "personal banker", "loan officer", "mortgage",
    "director", "vice president", "vp", "head of", "lead manager",
    "intern", "internship", "customer service representative", "call center"
]

COMPANY_EXCLUSIONS = [
    "cybercoders", "robert half", "kforce", "jobot", "actalent", "insight global"
]

TARGET_QUERIES = [
    "Wealth Operations OR Brokerage Operations Detroit, MI",
    "Compliance Analyst OR Fintech Operations Detroit, MI",
    "Business Operations Analyst OR RevOps Analyst Remote",
    "Schwab OR Fidelity OR Custodial Operations Remote",
    "Financial Systems Analyst OR Process Automation Detroit, MI"
]

SYSTEM_PROMPT = """
You are a strict technical job screener. Evaluate the candidate's alignment with the job posting.

Candidate Profile Summary:
- Background: Wealth Operations Specialist at a venture-backed fintech startup (Signal Advisors).
- Experience: RIA onboarding, custodian workflows (Schwab/Fidelity), DocuSign, Salesforce, SLA management, annuity compliance, Series 65 candidate, Python/SQL automation.
- Target: Back-office operations, middle-office finance, fintech compliance, operational automation.
- Strictly FORBIDDEN: Sales, client pitching, commission-based roles, retail bank tellers, cold calling.

Respond ONLY with JSON matching this structure: {"pass": true/false, "reason": "Short string explanation"}
Set "pass" to false IMMEDIATELY if the role requires generating new client leads, hitting sales quotas, or selling financial products.
"""


# ---------------------------------------------------------------------------
# Lead Enrichment & MX Lookup
# ---------------------------------------------------------------------------
def resolve_target_email(company_domain, contact_name=None):
    """
    1. Verifies if the domain has active MX records via DNS.
    2. Generates probabilistic corporate email patterns.
    3. Falls back to deterministic department aliases if no person is named.
    """
    domain = company_domain.lower().replace("https://", "").replace("http://", "").replace("www.", "").strip("/")
    
    try:
        mx_records = dns.resolver.resolve(domain, 'MX')
        if not mx_records:
            return f"careers@{domain}"
    except Exception:
        return f"contact@{domain}"

    if not contact_name or contact_name.lower() in ["unknown", "n/a", "none"]:
        return f"talent@{domain}"

    name_parts = re.sub(r'[^a-zA-Z\s]', '', contact_name).lower().split()
    if len(name_parts) < 2:
        first = name_parts[0] if name_parts else "recruiting"
        return f"{first}@{domain}"
    
    first, last = name_parts[0], name_parts[-1]

    patterns = [
        f"{first}.{last}@{domain}",
        f"{first}@{domain}",
        f"{first[0]}{last}@{domain}"
    ]

    return patterns[0]


# ---------------------------------------------------------------------------
# Core Integration Functions
# ---------------------------------------------------------------------------
def passes_strict_filter(job):
    """Deterministic filter against title, company, and description."""
    title = job.get("job_title", "").lower()
    description = job.get("job_description", "").lower()
    company = job.get("employer_name", "").lower()

    if any(term in title for term in TITLE_EXCLUSIONS):
        return False
    if any(comp in company for comp in COMPANY_EXCLUSIONS):
        return False

    sales_triggers = ["quota", "cold call", "commission", "business development", "prospecting"]
    if any(trigger in description for trigger in sales_triggers):
        return False

    return True


def evaluate_job_with_gemini(job):
    """AI evaluation using Gemini to confirm candidate fit."""
    if not GEMINI_API_KEY:
        return True, "Gemini key not configured; skipping AI evaluation."

    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        prompt = f"{SYSTEM_PROMPT}\n\nJob Title: {job.get('job_title')}\nCompany: {job.get('employer_name')}\nDescription:\n{job.get('job_description', '')[:3000]}"
        response = model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )

        res_data = json.loads(response.text.strip())
        return res_data.get("pass", False), res_data.get("reason", "No reason provided")
    except Exception as e:
        print(f"Gemini Evaluation Error: {e}")
        return True, "Fallback pass on AI error"


def extract_variables_with_gemini(job):
    """Extracts structured fields from job posting for Apps Script & MX Lookup."""
    if not GEMINI_API_KEY:
        return {
            "company_name": job.get("employer_name", "N/A"),
            "company_domain": f"{job.get('employer_name', 'company').lower().replace(' ', '')}.com",
            "job_title": job.get("job_title", "N/A"),
            "primary_responsibility": "Operations Management",
            "core_tool": "Python/SQL",
            "key_qualification": "Workflow Optimization",
            "hiring_manager_name": None
        }

    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        prompt = (
            "Extract these variables from the job posting in JSON: "
            "company_name, company_domain (e.g. acme.com), job_title, primary_responsibility, core_tool, key_qualification, hiring_manager_name (return null if not found).\n\n"
            f"Employer: {job.get('employer_name')}\nTitle: {job.get('job_title')}\nDescription:\n{job.get('job_description', '')[:2500]}"
        )
        response = model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        return json.loads(response.text.strip())
    except Exception as e:
        print(f"Gemini Extraction Error: {e}")
        return {
            "company_name": job.get("employer_name", "N/A"),
            "company_domain": f"{job.get('employer_name', 'company').lower().replace(' ', '')}.com",
            "job_title": job.get("job_title", "N/A"),
            "primary_responsibility": "Operations Management",
            "core_tool": "Python/SQL",
            "key_qualification": "Workflow Optimization",
            "hiring_manager_name": None
        }


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
    headers = {
        "x-api-key": API_KEY
    }
    params = {
        "query": query,
        "page": "1",
        "num_pages": "1",
        "date_posted": "3days"
    }
    try:
        response = requests.get(JSEARCH_URL, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        return response.json().get("data", [])
    except Exception as e:
        print(f"Error fetching jobs for query '{query}': {e}")
        return []


def send_telegram_notification(job_id, extracted_vars, target_email, apply_link):
    """Sends notification with inline Approve button to Telegram."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("Telegram tokens missing; skipping message.")
        return

    text = (
        f"🎯 *New Matched Role*\n"
        f"• *Company:* {extracted_vars.get('company_name')}\n"
        f"• *Title:* {extracted_vars.get('job_title')}\n"
        f"• *Target Email:* `{target_email}`\n"
        f"• *Tool:* {extracted_vars.get('core_tool')}\n"
        f"• *Apply Direct:* [Link]({apply_link})"
    )
    
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "⚡ Approve & Draft Email", "callback_data": f"approve_{job_id}"}
            ]]
        }
    }
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Error posting to Telegram: {e}")


# ---------------------------------------------------------------------------
# Main Execution Entrypoint
# ---------------------------------------------------------------------------
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

                # Stage 1: Extract JSON variables via Gemini
                extracted_vars = extract_variables_with_gemini(job)

                # Stage 2: Lead Enrichment (MX Lookup)
                domain = extracted_vars.get("company_domain") or f"{extracted_vars.get('company_name', 'company').lower().replace(' ', '')}.com"
                hiring_manager = extracted_vars.get("hiring_manager_name")
                target_email = resolve_target_email(domain, hiring_manager)

                # Stage 3: Send Payload to Apps Script / Google Sheets
                payload = {
                    "action": "log_job",
                    "job_id": job_id,
                    "company_name": extracted_vars.get("company_name", job.get("employer_name")),
                    "job_title": extracted_vars.get("job_title", job.get("job_title")),
                    "primary_responsibility": extracted_vars.get("primary_responsibility", "N/A"),
                    "core_tool": extracted_vars.get("core_tool", "N/A"),
                    "key_qualification": extracted_vars.get("key_qualification", "N/A"),
                    "target_email": target_email
                }
                log_to_sheets_crm(payload)

                # Stage 4: Send Telegram Alert with Approval Callback
                apply_link = job.get("job_apply_link", "#")
                send_telegram_notification(job_id, extracted_vars, target_email, apply_link)

                matched_jobs_count += 1

    print(f"Finished pipeline execution. Matched {matched_jobs_count} roles.")


if __name__ == "__main__":
    main()
