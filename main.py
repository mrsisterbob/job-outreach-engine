import dns.resolver
import re

def resolve_target_email(company_domain, contact_name=None):
    """
    1. Verifies if the domain has active MX records.
    2. Generates probabilistic corporate email patterns.
    3. Falls back to deterministic department aliases if no person is named.
    """
    # Clean domain input
    domain = company_domain.lower().replace("https://", "").replace("http://", "").replace("www.", "").strip("/")
    
    # Check for valid MX records via DNS
    try:
        mx_records = dns.resolver.resolve(domain, 'MX')
        if not mx_records:
            return f"careers@{domain}" # Fallback if DNS query fails
    except Exception:
        # Domain has no valid mail server
        return f"contact@{domain}"

    # If no contact name was extracted by Gemini, use target roles
    if not contact_name or contact_name.lower() in ["unknown", "n/a", "none"]:
        return f"talent@{domain}"

    # Clean name input
    name_parts = re.sub(r'[^a-zA-Z\s]', '', contact_name).lower().split()
    if len(name_parts) < 2:
        first = name_parts[0] if name_parts else "recruiting"
        return f"{first}@{domain}"
    
    first, last = name_parts[0], name_parts[-1]

    # Standard corporate pattern order:
    # 1. first.last@domain.com (Most common corporate format)
    # 2. first@domain.com
    # 3. f.last@domain.com
    patterns = [
        f"{first}.{last}@{domain}",
        f"{first}@{domain}",
        f"{first[0]}{last}@{domain}"
    ]

    # Return top statistical pattern (since MX is validated)
    return patterns[0]import base64
import json
import os
from email.message import EmailMessage
import google.generativeai as genai
import requests

# ---------------------------------------------------------------------------
# Environment Variables & Secrets
# ---------------------------------------------------------------------------
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN")
GMAIL_USER = os.environ.get("GMAIL_USER")
CRM_WEBHOOK_URL = os.environ.get("CRM_WEBHOOK_URL")

JSEARCH_URL = "https://jsearch.p.rapidapi.com/search"

# Initialize Gemini Client
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# ---------------------------------------------------------------------------
# Filter Arrays & Target Search Queries
# ---------------------------------------------------------------------------
TITLE_EXCLUSIONS = [
    "sales",
    "account executive",
    "business development",
    "bdr",
    "sdr",
    "advisor",
    "wealth advisor",
    "financial planner",
    "client relationship manager",
    "relationship manager",
    "agent",
    "wholesaler",
    "producer",
    "insurance agent",
    "teller",
    "branch",
    "personal banker",
    "loan officer",
    "mortgage",
    "director",
    "vice president",
    "vp",
    "head of",
    "lead manager",
    "intern",
    "internship",
    "customer service representative",
    "call center",
]

COMPANY_EXCLUSIONS = [
    "cybercoders",
    "robert half",
    "kforce",
    "jobot",
    "actalent",
    "insight global",
]

TARGET_QUERIES = [
    "Wealth Operations OR Brokerage Operations Detroit, MI",
    "Compliance Analyst OR Fintech Operations Detroit, MI",
    "Business Operations Analyst OR RevOps Analyst Remote",
    "Schwab OR Fidelity OR Custodial Operations Remote",
    "Financial Systems Analyst OR Process Automation Detroit, MI",
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

    sales_triggers = [
        "quota",
        "cold call",
        "commission",
        "business development",
        "prospecting",
    ]
    if any(trigger in description for trigger in sales_triggers):
        return False

    return True


def evaluate_job_with_gemini(job):
    """AI evaluation using Gemini to confirm candidate fit."""
    if not GEMINI_API_KEY:
        return True, "Gemini key not configured; skipping AI evaluation."

    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = f"{SYSTEM_PROMPT}\n\nJob Title: {job.get('job_title')}\nCompany: {job.get('employer_name')}\nDescription:\n{job.get('job_description', '')[:3000]}"
        response = model.generate_content(prompt)

        clean_json = response.text.strip().strip("```json").strip("```")
        res_data = json.loads(clean_json)
        return res_data.get("pass", False), res_data.get(
            "reason", "No reason provided"
        )
    except Exception as e:
        print(f"Gemini Evaluation Error: {e}")
        return True, "Fallback pass on AI error"


def get_gmail_access_token():
    """Refreshes OAuth token for Gmail API access."""
    token_url = "https://oauth2.googleapis.com/token"
    payload = {
        "client_id": GMAIL_CLIENT_ID,
        "client_secret": GMAIL_CLIENT_SECRET,
        "refresh_token": GMAIL_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }
    response = requests.post(token_url, data=payload, timeout=10)
    response.raise_for_status()
    return response.json().get("access_token")


def create_gmail_draft(job):
    """Creates an automated draft in Gmail via REST API."""
    if not all(
        [
            GMAIL_CLIENT_ID,
            GMAIL_CLIENT_SECRET,
            GMAIL_REFRESH_TOKEN,
            GMAIL_USER,
        ]
    ):
        print("Gmail OAuth credentials missing; skipping draft creation.")
        return ""

    try:
        access_token = get_gmail_access_token()
        msg = EmailMessage()
        msg["To"] = GMAIL_USER
        msg["From"] = GMAIL_USER
        msg["Subject"] = (
            f"Outreach Draft: {job.get('job_title')} - {job.get('employer_name')}"
        )

        body = (
            f"Role: {job.get('job_title')}\n"
            f"Company: {job.get('employer_name')}\n"
            f"Apply Link: {job.get('job_apply_link', 'N/A')}\n\n"
            f"Hello Team,\n\nI am writing to express my interest in the {job.get('job_title')} position..."
        )
        msg.set_content(body)

        raw_message = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        url = f"https://gmail.googleapis.com/gmail/v1/users/{GMAIL_USER}/drafts"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        res = requests.post(
            url, headers=headers, json={"message": {"raw": raw_message}}, timeout=10
        )
        res.raise_for_status()
        return res.json().get("id", "")
    except Exception as e:
        print(f"Error creating Gmail draft: {e}")
        return ""


def log_to_sheets_crm(
    company,
    role,
    location="",
    draft_id="",
    status="Draft Created",
    job_url="",
):
    """Logs the candidate contact/job record directly to Google Sheets via Apps Script Webhook."""
    if not CRM_WEBHOOK_URL:
        print("CRM_WEBHOOK_URL missing; skipping CRM log.")
        return

    payload = {
        "company": company,
        "role": role,
        "location": location,
        "draft_id": draft_id,
        "status": status,
        "job_url": job_url,
    }

    try:
        res = requests.post(CRM_WEBHOOK_URL, json=payload, timeout=10)
        print(f"Logged to Sheets CRM: {res.status_code}")
    except Exception as e:
        print(f"Error logging to Sheets CRM: {e}")


def fetch_jobs(query):
    """Retrieves listings from JSearch API."""
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
    }
    params = {
        "query": query,
        "page": "1",
        "num_pages": "1",
        "date_posted": "3days",
    }
    try:
        response = requests.get(
            JSEARCH_URL, headers=headers, params=params, timeout=10
        )
        response.raise_for_status()
        return response.json().get("data", [])
    except Exception as e:
        print(f"Error fetching jobs for query '{query}': {e}")
        return []


def send_telegram_message(message):
    """Sends notification to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Error posting to Telegram: {e}")


def format_job_payload(job, draft_id):
    """Formats Markdown output for Telegram."""
    title = job.get("job_title", "N/A")
    company = job.get("employer_name", "N/A")
    location = job.get("job_city", "") or (
        "Remote" if job.get("job_is_remote") else "N/A"
    )
    state = job.get("job_state", "")
    full_loc = f"{location}, {state}".strip(", ")
    apply_link = job.get("job_apply_link", "#")

    return (
        f"🎯 *New Matched Role*\n"
        f"• *Title:* {title}\n"
        f"• *Company:* {company}\n"
        f"• *Location:* {full_loc}\n"
        f"• *Gmail Draft ID:* `{draft_id or 'N/A'}`\n"
        f"• *Apply Direct:* [Link]({apply_link})"
    )


# ---------------------------------------------------------------------------
# Main Execution Entrypoint
# ---------------------------------------------------------------------------
def main():
    if not all([RAPIDAPI_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        print("Error: Missing required basic environment variables.")
        return

    seen_job_ids = set()
    matched_jobs_count = 0

    for query in TARGET_QUERIES:
        print(f"Searching: {query}")
        jobs = fetch_jobs(query)

        for job in jobs:
            job_id = job.get("job_id")
            if job_id in seen_job_ids:
                continue

            seen_job_ids.add(job_id)

            if passes_strict_filter(job):
                ai_pass, reason = evaluate_job_with_gemini(job)
                if not ai_pass:
                    print(
                        f"Skipped by AI: {job.get('job_title')} @ {job.get('employer_name')} - Reason: {reason}"
                    )
                    continue

                # Execute pipeline steps
                draft_id = create_gmail_draft(job)

                location = job.get("job_city", "") or (
                    "Remote" if job.get("job_is_remote") else "N/A"
                )
                log_to_sheets_crm(
                    company=job.get("employer_name", ""),
                    role=job.get("job_title", ""),
                    location=location,
                    draft_id=draft_id,
                    status="Draft Created",
                    job_url=job.get("job_apply_link", ""),
                )

                message = format_job_payload(job, draft_id)
                send_telegram_message(message)
                matched_jobs_count += 1

    print(f"Finished pipeline execution. Matched {matched_jobs_count} roles.")


if __name__ == "__main__":
    main()
