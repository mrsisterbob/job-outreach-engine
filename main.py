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
