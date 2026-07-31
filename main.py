import os
import json
import requests

# Load Secrets from Environment Variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_message(message):
    """Sends evaluated results directly to Telegram and prints response."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ CRITICAL ERROR: Telegram credentials missing in environment variables.")
        return None
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(url, json=payload)
        res_json = response.json()
        return res_json
    except Exception as e:
        print(f"❌ HTTP Request Exception: {e}")
        return None

def main():
    print("Starting Job Outreach Engine Debug Run...")
    
    # Load configuration parameters
    with open("config.json", "r") as f:
        config = json.load(f)
    
    roles = ", ".join(config["job_search_params"]["target_roles"])
    locations = ", ".join(config["job_search_params"]["locations"])
    
    message = (
        f"🚀 *Job Outreach Engine Active*\n\n"
        f"**Pipeline Status:** Operational\n"
        f"**Target Roles:** {roles}\n"
        f"**Target Locations:** {locations}"
    )
    
    print("Sending payload to Telegram API...")
    res = send_telegram_message(message)
    
    print("--------------------------------------------------")
    print(f"TELEGRAM API RESPONSE: {res}")
    print("--------------------------------------------------")

if __name__ == "__main__":
    main()
