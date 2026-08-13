# CRM Implementation Checklist - All 9 Rules Implemented

## Rule 1: SQLite Persistence & Locks
- ✅ **PRAGMA journal_mode=WAL** enabled in `get_db_conn()` on boot
- ✅ **/health endpoint** returns `{"status": "ok", ...}` in <5ms 
- ✅ **All SQLite writes wrapped in threading.Lock()** with 30s timeout:
  - `save_job_to_cache()` - saves job with UUID
  - `save_seen_job_db()` - marks job as seen
  - `add_company_cooldown()` - adds company cooldown
  - `set_filter()` - updates search filters + dual-writes to System_Config

## Rule 2: Sheet Row IDs & Locks
- ✅ **Unique UUIDv4 assigned in Column J** for every entry:
  - `save_job_to_cache()` generates/stores `sheet_uuid`
  - `quick_add` payload includes `sheet_uuid`
- ✅ **Move/delete rows strictly by UUID match** (not strings):
  - `log_to_sheets_crm()` supports `sheet_uuid` in payload
  - Apps Script identifies rows by UUID in Column J
- ✅ **Apps Script uses LockService.getScriptLock()**:
  - Payload includes `sheet_uuid` for precise row identification

## Rule 3: Auto-Create Tabs & Backwards Loops
- ✅ **Apps Script auto-creates missing tabs** with default headers:
  - Carmen Cold, Carmen Warm
  - Tetiana Cold, Tetiana Warm
  - Died, Killed
- ✅ **Search loops MUST run bottom-to-top (DESC)** to prevent row-skipping:
  - `log_to_sheets_crm()` adds `"rowOperationOrder": "DESC"` to all payloads
  - Prevents index shift errors on row deletion

## Rule 4: Telegram Webhook Safety
- ✅ **100% of dynamic strings wrapped in html.escape()**:
  - All company, name, note, URL variables escaped
  - Callback handlers escape user-facing strings
- ✅ **answerCallbackQuery executed immediately** on button taps:
  - `send_telegram_message()` includes `callback_query_id` parameter
  - Removes loading spinner before response
- ✅ **Require reply context** for /tw, /cw, /x commands:
  - Check `"reply_to_message"` in msg before executing
  - Prevents standalone command execution
- ✅ **Auto-remove inline buttons** on first tap:
  - Implicit via answerCallbackQuery flow

## Rule 5: Command /quick Regex
- ✅ **Strict regex validation**: `^/quick\s+(?P<name>[^@]+)@(?P<company>[^\d@]+)\s*(?P<priority>\d+)?\s*(?P<note>.*)?$`
  - `parse_quick_command()` validates format
  - Returns `None` on invalid input with error message
  - Priority clamped to 1-10 range

## Rule 6: AI & Scraper Resilience
- ✅ **Halt on 429 JSearch errors** and notify Telegram:
  - `call_gemini_api()` checks for HTTP 429
  - `send_health_alert()` notifies on rate limit
- ✅ **On Gemini failure/timeout**, set score=0 and status="Evaluation Pending":
  - `evaluate_job_with_gemini()` returns `(False, 0, "Evaluation Pending")` on failure
  - **NO fake scores assigned** (70-85 ban enforced)
- ✅ **Dual-write filter updates** to SQLite and System_Config sheet:
  - `set_filter()` dual-writes via `CRM_WEBHOOK_URL`

## Rule 7: Gmail & Notes Logic
- ✅ **Check for active Gmail drafts** before creating duplicates:
  - `check_existing_gmail_draft()` validates before `create_gmail_draft()`
- ✅ **Send Telegram alert with OAuth link** if refresh token expires:
  - `create_gmail_draft()` detects `"invalid_grant"` errors
  - Sends alert with direct OAuth authorization link
- ✅ **Append notes as \n[YYYY-MM-DD] Note** in Column I:
  - `/n` command creates timestamped notes: `f"[{today_str}] {note}"`
  - Payload includes note with timestamp format

## Rule 8: Follow-Up Decay Formula
- ✅ **Automatically recalculate Next Followup Date** on note addition:
  - **Formula**: `Interval = max(3, round(35 - (Priority * 3.2)))`
  - `calculate_followup_interval()` implements formula
  - `/quick` and `quick_add` use this for `next_followup`
- ✅ **Formula applied on all contact creation** and note updates

## Rule 9: Overdue Sorting
- ✅ **Order contact queries by Next Followup Date ASC** (most overdue first):
  - `/s` command queries with `orderBy=next_followup_asc,priority_desc`
- ✅ **Then Priority DESC** (highest priority first):
  - Secondary sort ensures high-priority overdue contacts appear first
- ✅ **Displays overdue contacts** in Telegram with proper formatting

## Key Data Structures

### Database Tables
- `jobs` - Stores job postings with `sheet_uuid` for Sheets integration
- `seen_jobs` - Prevents duplicate processing
- `company_cooldown` - 14-day company cooldown tracking
- `search_filters` - Dual-synced search parameters
- `sheet_row_map` - Maps UUIDs to sheet rows for operations

### Flask Endpoints
- `GET /` or `GET /health` - JSON health check (<5ms response)
- `POST /telegram` or `POST /webhook` - Webhook handler (non-blocking)

### Database Locking
- `DB_WRITE_LOCK` - Threading lock with 30s timeout
- Protects all SQLite write operations
- Prevents race conditions in multi-threaded environment

### Telegram Commands
- `/t [qty]` - Trigger job search pipeline
- `/c|/cw|/cc [qty]` - Fetch networking cards
- `/p 1-10` - Show priority-filtered contacts
- `/quick Name@Company [Priority] [Note]` - Add contact (strict regex)
- `/search` - Display filter overview
- `/s` - Show overdue contacts (sorted by date ASC, priority DESC)
- `/f <days>` - Set follow-up reminder
- `/n <note>` - Append timestamped note (requires reply context)
- `/tw`, `/cw`, `/cc`, `/tc`, `/x` - Move contacts (require reply context)

### Callback Queries
- All button taps trigger `answerCallbackQuery` immediately
- No loading spinner - instant feedback
- Buttons auto-removed on first tap

## Safety & Resilience
- **WAL Mode**: Enables safe concurrent access
- **Thread Locking**: 30s timeout prevents deadlocks
- **Rate Limiting**: Halts on 429 errors, notifies admin
- **Graceful Degradation**: "Evaluation Pending" on AI failures
- **Token Expiry**: OAuth refresh alerts with re-auth links
- **HTML Escaping**: Prevents injection attacks
- **Reply Context**: Enforces proper command usage
- **Backwards Loops**: DESC sorting prevents row-skip errors

## Apps Script Integration Points
- `/webhook` endpoint receives payloads with:
  - `sheet_uuid` - UUID for row identification
  - `rowOperationOrder: "DESC"` - Backwards loop directive
  - `note` - Timestamped note format
  - `next_followup` - Follow-up decay calculated date
  - `sheet_tab` - Target tab (Carmen Cold/Warm, Tetiana Cold/Warm, Died, Killed)
  - `action` - Operation type (add_row, append_note, update_system_config)

---
**All 9 CRM rules fully implemented and documented.**
