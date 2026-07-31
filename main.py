import os
import sys
import requests

# ==========================================
# 1. ENVIRONMENT & KEYS
# ==========================================
API_KEY = os.getenv("RAPIDAPI_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Fail fast if secrets are missing in GitHub Actions
if not API_KEY or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("❌ Error: One or more environment secrets are missing.")
    sys.exit(1)

# ==========================================
# 2. CONFIGURATION & SEARCH TARGETS
# ==========================================
# OpenWeb Ninja JSearch Endpoint
JSEARCH_URL = "https://api.openwebninja.com/jsearch/search"

# Target roles across your core locations and domains
SEARCH_QUERIES = [
    "Finance Operations Detroit MI",
    "Compliance Operations Detroit MI",
    "Wealth Operations Remote",
    "Real Estate Tokenization Compliance"
]

# ==========================================
# 3. HELPER FUNCTIONS
# ==========================================
def send_telegram_alert(message: str):
    """Pushes formatted Markdown text to your Telegram chat."""
    telegram_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    try:
        res = requests.post(telegram_url, json=payload, timeout=10)
        res.raise_for_status()
    except Exception as e:
        print(f"Failed to send Telegram alert: {e}")

def fetch_jobs(query_string: str):
    """Queries JSearch for jobs posted within the last 24 hours."""
    headers = {
        "x-api-key": API_KEY,
        "Content-Type": "application/json"
    }
    params = {
        "query": query_string,
        "page": "1",
        "num_pages": "1",
        "date_posted": "today"  # Pulls fresh listings only
    }
    
    try:
        response = requests.get(JSEARCH_URL, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        res_json = response.json()
        
        # Handle varying data payload wrappers (dict vs list)
        data = res_json.get("data", [])
        if isinstance(data, dict):
            return data.get("jobs", [])
        elif isinstance(data, list):
            return data
        return []
        
    except requests.exceptions.RequestException as e:
        print(f"⚠️ API Error for query '{query_string}': {e}")
        return []

# ==========================================
# 4. MAIN PIPELINE EXECUTION
# ==========================================
def main():
    print("🚀 Starting Job Automation Engine...")
    total_found = 0
    
    # Simple set to deduplicate listings across multiple search strings
    seen_job_ids = set()

    for query in SEARCH_QUERIES:
        print(f"🔍 Searching for: '{query}'...")
        jobs = fetch_jobs(query)
        
        for job in jobs:
            job_id = job.get("job_id")
            if not job_id or job_id in seen_job_ids:
                continue
            
            seen_job_ids.add(job_id)
            total_found += 1
            
            # Key JSON fields from OpenWeb Ninja
            title = job.get("job_title", "N/A")
            company = job.get("employer_name", "N/A")
            location = job.get("job_city") or job.get("job_country") or "Location Not Specified"
            apply_link = job.get("job_apply_link", "#")
            is_remote = "Yes" if job.get("job_is_remote") else "No"

            # Construct clean Telegram alert format
            alert_text = (
                f"🎯 *New Job Alert*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"*Role:* {title}\n"
                f"*Company:* {company}\n"
                f"*Location:* {location} (Remote: {is_remote})\n\n"
                f"🔗 [Apply Directly Here]({apply_link})"
            )
            
            send_telegram_alert(alert_text)

    print(f"✅ Pipeline complete. Processed {total_found} fresh listings.")

if __name__ == "__main__":
    main()
# Absolute NO-SALES and Irrelevant Role Blocklist
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

# High-Noise Recruiter Aggregators (Optional, filter if clogging feed)
COMPANY_EXCLUSIONS = [
    "cybercoders", "robert half", "kforce", "jobot", "actalent", "insight global"
]

def passes_strict_filter(job):
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
