import os
import json
import requests

# Load Secrets from Environment Variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_message(message):
    """Sends evaluated results directly to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing.")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    response = requests.post(url, json=payload)
    return response.json()

def main():
    print("Starting Job Outreach Engine...")
    
    # Load configuration parameters
    with open("config.json", "r") as f:
        config = json.load(f)
    
    roles = ", ".join(config["job_search_params"]["target_roles"])
    locations = ", ".join(config["job_search_params"]["locations"])
    
    # Static test message verifying environment and config loading
    message = (
        f"🚀 *Job Outreach Engine Active*\n\n"
        f"**Pipeline Status:** Operational\n"
        f"**Target Roles:** {roles}\n"
        f"**Target Locations:** {locations}"
    )
    
    # Send confirmation alert to Telegram
    res = send_telegram_message(message)
    print("Execution complete. Telegram response:", res)

if __name__ == "__main__":
    main()
