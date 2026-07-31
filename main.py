import os
import json
import requests
import google.generativeai as genai

# Load Secrets from Environment
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
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
    
    # Load configuration
    with open("config.json", "r") as f:
        config = json.load(f)
    
    roles = ", ".join(config["job_search_params"]["target_roles"])
    
    # Initialize Gemini AI
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
    
    summary_prompt = f"System online. Monitoring for roles: {roles}. Pipeline active and waiting for incoming data triggers."
    response = model.generate_content(summary_prompt)
    
    # Notify Telegram of successful pipeline run
    message = f"🚀 *Job Outreach Engine Active*\n\n{response.text}"
    send_telegram_message(message)
    print("Execution complete. Telegram alert sent.")

if __name__ == "__main__":
    main()
