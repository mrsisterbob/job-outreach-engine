import base64
import hashlib
import html
import json
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from email.message import EmailMessage
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# ==========================================
# 1. ENVIRONMENT VARIABLES & INITIALIZATION
# ==========================================
API_KEY = os.environ.get("OPENWEBNINJA_KEY") or os.environ.get("RAPIDAPI_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
CRM_WEBHOOK_URL = os.environ.get("CRM_WEBHOOK_URL")
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN")
GMAIL_USER = os.environ.get("GMAIL_USER")

JSEARCH_URL = "https://api.openwebninja.com/jsearch/search"

# ==========================================
# 2. FILTER RULES & SKILLS
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
    " senior", " lead", " manager", " director", " vp", " executive", " principal", "head of"
]

CORE_SKILLS = [
    "python", "sql", "salesforce", "excel", "schwab sac", "schwab advisor center",
    "fidelity wealthscape", "docusign", "process automation", "reconciliation"
]

TARGET_QUERIES = [
    "Wealth Operations Detroit MI",
    "Fintech Operations Michigan",
    "Business Operations Analyst Detroit MI",
    "Custodial Operations Schwab Fidelity Michigan",
    "Financial Systems Process Automation Detroit MI"
]

SYSTEM_PROMPT = """You are a strict technical job screener evaluating roles for an early-career candidate (0-2 years experience).
Target Profile: Non-sales W-2 roles in Tech, FinTech, Auto Tech, or Back-Office Systems/Operations in Metro Detroit or Remote.
High Priority Skills: Python, SQL, Salesforce, Excel, Schwab SAC, Fidelity Wealthscape, DocuSign, Process Automation.
Strictly FORBIDDEN: Sales, cold calling, client pitching, commission-based roles, retail bank tellers, CPA tracks, Senior/Lead/Manager roles.
Evaluate the job description and respond ONLY with a JSON object containing:
{
  "score": <integer between 1 and 100 representing fit signal>,
  "reason": "<1-sentence concise explanation of why this role fits or does not fit>"
}"""

JOB_CACHE = {}

# ==========================================
# 3. HELPER FUNCTIONS
# ==========================================
def send_health_alert(error_msg):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        text = f"⚠️ <b>Pipeline Operational Warning</b>\n<code>{html.escape(str(error_msg))}</code>"
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=5
            )
        except Exception:
            pass

def log_to_sheets_crm(payload, max_retries=3):
    if not CRM_WEBHOOK_URL:
        return False
    delay = 1.0
    for attempt in range(max_retries):
        try:
            res = requests.post(CRM_WEBHOOK_URL, json=payload, timeout=10)
            if res.status_code == 200:
                return True
        except Exception as e:
            print(f"CRM Webhook Attempt {attempt+1} Failed: {e}", flush=True)
            time.sleep(delay)
            delay *= 2.0
    send_health_alert(f"Failed to log payload to Google Sheets after {max_retries} attempts.")
    return False

def generate_dedup_hash(company, title):
    clean_company = str(company or "").lower().strip()
    clean_title = str(title or "").lower().strip()
    clean_str = f"{clean_company}_{clean_title}"
    return hashlib.md5(clean_str.encode()).hexdigest()

def parse_posted_hours(posted_utc_str):
    if not posted_utc_str:
        return 48
    try:
        dt = datetime.fromisoformat(str(posted_utc_str).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return int((now - dt).total_seconds() / 3600)
    except Exception:
        return 48

def get_age_badge(posted_hours):
    if posted_hours < 24:
        return "🔥 [< 24h FRESH]"
    elif posted_hours < 72:
        return "⚡ [1-3d RECENT]"
    elif posted_hours < 168:
        return "📌 [3-7d ACTIVE]"
    elif posted_hours < 336:
        return "⏳ [7-14d AGING]"
    else:
        return "💤 [14-30d STALE]"

def extract_salary(job):
    try:
        min_sal = float(job.get("job_min_salary") or 0)
        max_sal = float(job.get("job_max_salary") or 0)
        curr = str(job.get("job_salary_currency") or "USD")
        period = str(job.get("job_salary_period") or "year")
        if min_sal and max_sal:
            return f"${min_sal:,.0f} - ${max_sal:,.0f} {curr}/{period}"
        elif min_sal or max_sal:
            val = min_sal or max_sal
            return f"${val:,.0f} {curr}/{period}"
    except Exception:
        pass
    return "Salary Unlisted"

def extract_work_style(job):
    desc = str(job.get("job_description") or "").lower()
    is_remote = job.get("job_is_remote", False) or "remote" in desc[:300] or "work from home" in desc
    if "hybrid" in desc:
        return "🏫 Hybrid"
    elif is_remote:
        return "🏠 Remote"
    elif "on-site" in desc or "onsite" in desc or "in-office" in desc:
        return "🏢 On-Site"
    return "🏢 On-Site / Unspecified"

def calculate_keyword_overlap(job_desc):
    desc = str(job_desc or "").lower()
    matches = [skill for skill in CORE_SKILLS if skill in desc]
    overlap_pct = int((len(matches) / len(CORE_SKILLS)) * 100)
    return overlap_pct, matches

def build_apollo_url(company_name):
    clean_company = re.sub(r'[^a-zA-Z0-9\s]', '', str(company_name or "")).strip()
    encoded = urllib.parse.quote(f"{clean_company} Operations")
    return f"https://app.apollo.io/#/people?qKeywords={encoded}"

def build_linkedin_url(company_name):
    clean_company = re.sub(r'[^a-zA-Z0-9\s]', '', str(company_name or "")).strip()
    encoded = urllib.parse.quote(f'{clean_company} ("VP" OR "Director" OR "Manager") ("Operations" OR "Compliance")')
    return f"https://www.linkedin.com/search/results/people/?keywords={encoded}"

def build_linkedin_recruiter_url(company_name):
    clean_company = re.sub(r'[^a-zA-Z0-9\s]', '', str(company_name or "")).strip()
    encoded = urllib.parse.quote(f'{clean_company} ("Recruiter" OR "Talent Acquisition" OR "Recruiting")')
    return f"https://www.linkedin.com/search/results/people/?keywords={encoded}"

def resolve_target_email(company_name, job_title=""):
    clean_domain = re.sub(r'[^a-zA-Z0-9]', '', str(company_name or "")).lower() + ".com"
    title_lower = str(job_title or "").lower()
    if "compliance" in title_lower:
        return f"compliance@{clean_domain}"
    elif any(kw in title_lower for kw in ["wealth", "custody", "brokerage", "ria"]):
        return f"wealthops@{clean_domain}"
    elif any(kw in title_lower for kw in ["systems", "automation", "revops"]):
        return f"bizops@{clean_domain}"
    return f"operations@{clean_domain}"

# ==========================================
# 4. GEMINI REST API INTEGRATION
# ==========================================
def call_gemini_api(prompt, system_prompt=None, response_mime="application/json"):
    if not GEMINI_API_KEY:
        return None
    full_prompt = f"{system_prompt}\n\n{prompt}".strip() if system_prompt else prompt
    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {"response_mime_type": response_mime}
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    try:
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            return res.json()["candidates"][0]["content"]["parts"][0]["text"]
        print(f"Gemini API Error ({res.status_code}): {res.text}", flush=True)
    except Exception as e:
        print(f"Gemini API Exception: {e}", flush=True)
    return None

def evaluate_job_with_gemini(job):
    if not GEMINI_API_KEY:
        return True, 75, "Fallback pass (No Key)"
    prompt = f"Job Title: {job.get('job_title')}\nCompany: {job.get('employer_name')}\nDescription:\n{str(job.get('job_description') or '')[:2500]}"
    raw_text = call_gemini_api(prompt, SYSTEM_PROMPT)
    if raw_text:
        try:
            cleaned_text = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw_text).strip()
            res_data = json.loads(cleaned_text)
            return (int(res_data.get("score", 0)) >= 70), int(res_data.get("score", 0)), res_data.get("reason", "N/A")
        except Exception as e:
            print(f"Gemini evaluation JSON parse failure: {e}", flush=True)
            return True, 70, "Fallback pass on parse error"
    return True, 70, "Fallback pass on API failure"

def generate_tailored_intro(job_description):
    prompt = f"Write 2 concise sentences explaining why a Wealth Operations candidate with Python, SQL, Salesforce, and Schwab SAC experience aligns with this job description:\n{str(job_description or '')[:1500]}"
    res = call_gemini_api(prompt, response_mime="text/plain")
    return res.strip() if res else "My background centers on wealth operations, custodial workflows, and process automation."

def generate_resume_cheat_sheet(job_description):
    prompt = f"Analyze this job description and provide 3 high-impact bullet points specifying exact skills/terms to emphasize on a resume:\n{str(job_description or '')[:2000]}"
    res = call_gemini_api(prompt, response_mime="text/plain")
    return res.strip() if res else "• Emphasize Python/SQL automation\n• Highlight Schwab SAC reconciliation\n• Accentuate Salesforce CRM management"

# ==========================================
# 5. GMAIL API DRAFTING
# ==========================================
def create_gmail_draft(to_email, company_name, job_title, job_description=""):
    missing_vars = [v for v in ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_USER"] if not os.environ.get(v)]
    if missing_vars:
        return False, f"Missing Env Vars: {', '.join(missing_vars)}"
    
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
            return False, "OAuth Token Refused"
        
        tailored_intro = generate_tailored_intro(job_description) if job_description else "My background centers on wealth operations, custodial workflows, and process automation."
        
        message = EmailMessage()
        message["To"] = to_email
        message["From"] = GMAIL_USER
        message["Subject"] = f"Operations & Systems Alignment - {job_title} @ {company_name}"
        body = (
            f"Hi Hiring Team,\n\n"
            f"I recently came across the {job_title} opening at {company_name} and wanted to reach out directly. "
            f"{tailored_intro}\n\n"
            f"I have attached my resume (PDF) to this message for your review and would welcome the opportunity to connect.\n\n"
            f"Best regards,\n"
            f"Kevin Miller"
        )
        message.set_content(body)
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        
        draft_url = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        res = requests.post(draft_url, headers=headers, json={"message": {"raw": raw_message}}, timeout=10)
        return (True, "Success") if res.status_code in [200, 201] else (False, f"Gmail Error {res.status_code}")
    except Exception as e:
        return False, str(e)

# ==========================================
# 6. STAGE 1 FILTER & PIPELINE EXECUTION
# ==========================================
def passes_strict_filter(job):
    title = str(job.get("job_title") or "").lower()
    description = str(job.get("job_description") or "").lower()
    company = str(job.get("employer_name") or "").lower()
    state = str(job.get("job_state") or "").upper()
    city = str(job.get("job_city") or "").lower()
    
    valid_cities = [
        "farmington", "detroit", "ann arbor", "novi", "troy", "southfield",
        "auburn hills", "plymouth", "royal oak", "livonia", "dearborn",
        "birmingham", "bloomfield", "warren", "sterling heights", "canton",
        "rochester", "wixom", "madison heights"
    ]
    
    is_mi = (state == "MI") or "michigan" in city or any(c in city for c in valid_cities)
    is_remote = job.get("job_is_remote", False) or "remote" in description[:300] or "work from home" in description[:300]
    
    # Location Gate: Must be in Michigan or Remote
    if not (is_mi or is_remote):
        return False

    if any(term in title for term in TITLE_EXCLUSIONS):
        return False
    if any(comp in company for comp in COMPANY_EXCLUSIONS):
        return False
    if any(trigger in description for trigger in HARD_BAN_KEYWORDS):
        return False
    if any(sen in title for sen in SENIORITY_EXCLUSIONS):
        return False

    return True

def send_telegram_card(job, score, reason, target_email, age_badge, salary_str, work_style, overlap_pct, matched_skills):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return

    company = html.escape(str(job.get("employer_name") or "N/A"))
    title = html.escape(str(job.get("job_title") or "N/A"))
    apply_link = job.get("job_apply_link") or "#"
    job_id = str(job.get("job_id") or "0")

    apollo_url = build_apollo_url(company)
    linkedin_url = build_linkedin_url(company)
    recruiter_url = build_linkedin_recruiter_url(company)

    matched_str = ", ".join(matched_skills[:4]).title() if matched_skills else "General Ops"

    card_text = (
        f"<b>{title}</b>\n"
        f"<b>Company:</b> {company}\n"
        f"<b>Posting Recency:</b> {age_badge}\n"
        f"<b>Work Style & Pay:</b> {work_style} | {salary_str}\n"
        f"<b>Fit Score:</b> {score}/100 | <b>Skill Match:</b> {overlap_pct}%\n"
        f"<b>Key Overlap:</b> <code>{html.escape(matched_str)}</code>\n"
        f"<b>Default Target:</b> <code>{html.escape(target_email)}</code>\n\n"
        f"<b>Fit Reason:</b> {html.escape(reason)}\n\n"
        f"<a href='{apply_link}'>1. Apply Direct</a>\n"
        f"<a href='{apollo_url}'>2. Open Leads in Apollo</a>\n"
        f"<a href='{linkedin_url}'>3. Open Leadership on LinkedIn</a>\n"
        f"<a href='{recruiter_url}'>4. Open Recruiters on LinkedIn</a>"
    )

    safe_company = re.sub(r'[^a-zA-Z0-9\s]', '', str(job.get("employer_name") or "Company"))[:15].strip()
    safe_email = str(target_email)[:30]

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "📌 Mark Applied", "callback_data": f"apply_tc:{job_id}"},
                {"text": "⚡ Draft Email", "callback_data": f"approve:{safe_company}:{safe_email}"}
            ],
            [
                {"text": "🎯 Tailor Resume", "callback_data": f"resume:{job_id}"},
                {"text": "❌ Mark Dead", "callback_data": f"dead_tc:{job_id}"}
            ]
        ]
    }

    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": card_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": reply_markup
        },
        timeout=10
    )

def run_job_pipeline(top_n=5):
    print(">>> Starting Job Search Pipeline...", flush=True)
    seen_hashes = set()
    candidate_pool = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    headers = {"x-api-key": API_KEY} if API_KEY else {}

    for query in TARGET_QUERIES:
        for page in range(1, 3):  # Fetch pages 1 and 2
            try:
                print(f">>> Querying JSearch for: {query} (Page {page})", flush=True)
                params = {"query": query, "page": str(page), "num_pages": "1", "date_posted": "month"}
                res = requests.get(JSEARCH_URL, headers=headers, params=params, timeout=20)
                if res.status_code != 200:
                    print(f"JSearch API Error {res.status_code}: {res.text[:100]}", flush=True)
                    continue

                jobs = res.json().get("data", [])
                time.sleep(0.5)

                for job in jobs:
                    try:
                        company = job.get("employer_name") or ""
                        title = job.get("job_title") or ""
                        job_hash = generate_dedup_hash(company, title)
                        if job_hash in seen_hashes:
                            continue
                        seen_hashes.add(job_hash)

                        posted_hours = parse_posted_hours(job.get("job_posted_at_datetime_utc"))
                        if posted_hours > 720:  # Ignore > 30 days
                            continue

                        if passes_strict_filter(job):
                            ai_pass, score, reason = evaluate_job_with_gemini(job)
                            time.sleep(1.0)
                            if ai_pass:
                                job_id = str(job.get("job_id") or time.time())
                                JOB_CACHE[job_id] = job
                                target_email = resolve_target_email(company, title)
                                age_badge = get_age_badge(posted_hours)
                                salary_str = extract_salary(job)
                                work_style = extract_work_style(job)
                                overlap_pct, matched_skills = calculate_keyword_overlap(job.get("job_description"))

                                candidate_pool.append({
                                    "job": job, "score": score, "reason": reason,
                                    "target_email": target_email, "age_badge": age_badge,
                                    "salary_str": salary_str, "work_style": work_style,
                                    "overlap_pct": overlap_pct, "matched_skills": matched_skills,
                                    "posted_hours": posted_hours
                                })
                    except Exception as inner_e:
                        print(f"Skipped single corrupted job entry: {inner_e}", flush=True)
                        continue
            except Exception as e:
                send_health_alert(f"Error querying JSearch for '{query}': {e}")

    candidate_pool.sort(key=lambda x: x["score"], reverse=True)
    top_matches = candidate_pool[:top_n]
    print(f">>> Found {len(top_matches)} high-fit candidate matches.", flush=True)

    for item in top_matches:
        job = item["job"]
        send_telegram_card(
            job, item["score"], item["reason"], item["target_email"],
            item["age_badge"], item["salary_str"], item["work_style"],
            item["overlap_pct"], item["matched_skills"]
        )
        log_to_sheets_crm({
            "action": "add_row",
            "target_code": "TC",
            "row_data": [
                today_str,
                job.get("employer_name"),
                job.get("job_title"),
                item["target_email"],
                item["score"],
                "Matched",
                job.get("job_apply_link", ""),
                f"{item['age_badge']} | {item['work_style']} | {item['reason']}"
            ]
        })

    if not top_matches and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": "⚠️ Pipeline run complete: 0 matching roles found across targeted queries."},
            timeout=5
        )

    return len(top_matches)

def run_stale_application_sweeper(chat_id):
    if not CRM_WEBHOOK_URL:
        return
    try:
        res = requests.post(CRM_WEBHOOK_URL, json={"action": "get_followups"}, timeout=10)
        if res.status_code == 200:
            due_list = res.json().get("followups", [])
            if due_list:
                text = f"<b>Stale Application Sweeper</b>\nFound {len(due_list)} pending follow-ups requiring triage."
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
                )
    except Exception as e:
        print(f"Sweeper Error: {e}", flush=True)

# ==========================================
# 7. FLASK SERVER & WEBHOOK ROUTES
# ==========================================
@app.route('/', methods=['GET'])
def health_check():
    return "CRM & Job Pipeline Engine Active", 200

@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    data = request.get_json()
    if not data:
        return jsonify({"status": "ignored"}), 200

    if "callback_query" in data:
        callback = data["callback_query"]
        chat_id = callback["message"]["chat"]["id"]
        callback_data = callback.get("data", "")

        if callback_data.startswith("approve:"):
            parts = callback_data.split(":", 2)
            company = parts[1] if len(parts) > 1 else "Company"
            email = parts[2] if len(parts) > 2 else "operations@company.com"
            job_desc = ""
            for j in JOB_CACHE.values():
                if j.get("employer_name") == company:
                    job_desc = j.get("job_description", "")
                    break
            success, err_detail = create_gmail_draft(email, company, "Operations Role", job_desc)
            msg = f"<b>Draft Created</b> for <code>{html.escape(email)}</code>!" if success else f"<b>Draft Failed:</b> <code>{html.escape(err_detail)}</code>"
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}
            )
            return jsonify({"status": "ok"}), 200

        elif callback_data.startswith("resume:"):
            job_id = callback_data.split(":", 1)[1]
            job = JOB_CACHE.get(job_id)
            if job:
                cheat_sheet = generate_resume_cheat_sheet(job.get("job_description", ""))
                msg = f"🎯 <b>Resume Tailoring Cheat Sheet</b> ({html.escape(job.get('employer_name', ''))}):\n\n{html.escape(cheat_sheet)}"
            else:
                msg = "⚠️ Job description expired from cache. Base resume on: Python, SQL, Salesforce, Schwab SAC."
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}
            )
            return jsonify({"status": "ok"}), 200

        elif callback_data.startswith("apply_tc:"):
            log_to_sheets_crm({"action": "apply_job", "row_index": 2})
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": "📌 Moved to <b>Tetiana Warm</b> (Follow-up set to +5 days).", "parse_mode": "HTML"}
            )
            return jsonify({"status": "ok"}), 200

    if "message" in data:
        message = data["message"]
        text = message.get("text", "").strip()
        chat_id = message.get("chat", {}).get("id")

        if text in ["/run", "/start"]:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": "🚀 Pipeline scanning ~225 Metro Detroit & Remote roles..."}
            )
            threading.Thread(target=run_job_pipeline).start()
            return jsonify({"status": "started"}), 200

        elif text == "/sweep":
            run_stale_application_sweeper(chat_id)
            return jsonify({"status": "swept"}), 200

        elif text.startswith("/vp "):
            parts = text.split(" ", 3)
            email = parts[1] if len(parts) > 1 else ""
            company = parts[2] if len(parts) > 2 else "Target Firm"
            title = parts[3] if len(parts) > 3 else "Executive"
            today_str = datetime.now().strftime("%Y-%m-%d")

            log_to_sheets_crm({
                "action": "add_row",
                "target_code": "CC",
                "row_data": [today_str, company, title, email, "VP Cold", "Contacted", "", "LinkedIn", "Logged via Telegram"]
            })
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": f"👤 Logged <b>{html.escape(company)}</b> to <b>Carmen Cold</b> (Follow-up set to +4 days).", "parse_mode": "HTML"}
            )
            return jsonify({"status": "vp_logged"}), 200

    return jsonify({"status": "ignored"}), 200


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
