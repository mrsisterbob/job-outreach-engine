import os
import requests

# ---------------------------------------------------------------------------
# Environment Variables & Secrets
# ---------------------------------------------------------------------------
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

JSEARCH_URL = "https://jsearch.p.rapidapi.com/search"

# ---------------------------------------------------------------------------
# Filter Arrays & Target Search Queries
# ---------------------------------------------------------------------------
TITLE_EXCLUSIONS = [
    # Sales & Pitch Roles
    "sales", "account executive", "business development", "bdr", "sdr", 
    "advisor", "wealth advisor", "financial planner", "client relationship manager",
    "relationship manager", "agent", "wholesaler", "producer", "insurance agent",
    
    # Retail / Branch Banking
    "teller", "branch", "personal banker", "loan officer", "mortgage", 
    
    # Management / Senior Out-of-Scope
    "director", "vice president", "vp", "head of", "lead manager",
    
    # Low-Level / Unrelated
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

Respond ONLY with JSON: {"pass": true/false, "reason": "Short string explanation"}
Set "pass" to false IMMEDIATELY if the role requires generating new client leads, hitting sales quotas, or selling financial products.
"""

# ---------------------------------------------------------------------------
# Pipeline Functions
# ---------------------------------------------------------------------------
def passes_strict_filter(job):
    """
    Evaluates job metadata against deterministic blocklists to eliminate sales,
    retail banking, senior executive positions, and agency spam.
    """
    title = job.get("job_title", "").lower()
    description = job.get("job_description", "").lower()
    company = job.get("employer_name", "").lower()
    
    # 1. Reject matching excluded titles
    if any(term in title for term in TITLE_EXCLUSIONS):
        return False
        
    # 2. Reject matching excluded companies
    if any(comp in company for comp in COMPANY_EXCLUSIONS):
        return False
        
    # 3. Reject commission / quota flags in job description body
    sales_triggers = ["quota", "cold call", "commission", "business development", "prospecting"]
    if any(trigger in description for trigger in sales_triggers):
        return False
        
    return True

def fetch_jobs(query):
    """
    Queries JSearch endpoint via RapidAPI.
    """
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com"
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
        data = response.json()
        return data.get("data", [])
    except Exception as e:
        print(f"Error fetching jobs for query '{query}': {e}")
        return []

def send_telegram_message(message):
    """
    Dispatches formatted job alert to Telegram chat API.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"Error posting to Telegram: {e}")

def format_job_payload(job):
    """
    Formats parsed job payload into clean Telegram Markdown.
    """
    title = job.get("job_title", "N/A")
    company = job.get("employer_name", "N/A")
    location = job.get("job_city", "") or ("Remote" if job.get("job_is_remote") else "N/A")
    state = job.get("job_state", "")
    full_loc = f"{location}, {state}".strip(", ")
    apply_link = job.get("job_apply_link", "#")
    
    return (
        f"🎯 *New Matched Role*\n"
        f"• *Title:* {title}\n"
        f"• *Company:* {company}\n"
        f"• *Location:* {full_loc}\n"
        f"• *Apply Direct:* [Link]({apply_link})"
    )

# ---------------------------------------------------------------------------
# Main Execution Entrypoint
# ---------------------------------------------------------------------------
def main():
    if not all([RAPIDAPI_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        print("Error: Missing required environment variables.")
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
                message = format_job_payload(job)
                send_telegram_message(message)
                matched_jobs_count += 1

    print(f"Execution finished. Dispatched {matched_jobs_count} non-sales ops roles.")

if __name__ == "__main__":
    main()
