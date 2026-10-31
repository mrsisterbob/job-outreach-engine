---
id: TASK-254
scope: inbound_hold.py, test_inbound_hold.py
verify: python -m pytest test_inbound_hold.py -q -p no:cacheprovider 2>&1 | tail -n 1 | grep -E "^(1[2-9]|[2-9][0-9]|[1-9][0-9]{2,}) passed" | grep -Ev "failed|error"
---

## Target
A pure module `inbound_hold.py` that says whether an email from a sender with no CRM identity should be HELD quietly instead of pinging Kevin (it is neither a reply, nor an interview, offer or rejection signal) and words the one standup line that counts what was held.

## Proof it is needed
`python -m pytest test_inbound_hold.py -q -p no:cacheprovider` on 2026-10-31 (main at 099d737): `ERROR: file or directory not found: test_inbound_hold.py`, exit 4, so the verify exits 1. Pass-regex proven able to pass: `python -m pytest test_apify_feed.py -q -p no:cacheprovider` ends `44 passed` and matches `^(1[2-9]|[2-9][0-9]|[1-9][0-9]{2,}) passed`. Why: in `check_inbound_gmail_replies`, a sender who is on no CRM tab, in no thread Kevin started and on no tracked domain is alerted as `⚠️ Unverified Reply (unknown sender)` whatever the mail is (main.py, the `if not crm_match:` fallback; comment "A stranger writing to you is not a defect"). The sender screen (`inbound_sender_screen_reason`, pipeline_utils.py:1063) only judges machine mailboxes and relay domains, so cold spam from a human-looking address on a normal domain reaches the phone and buries real replies.

## Notes
- Search first: `grep -i -E "direct|reply|tier1|screen" FUNCTION_INDEX.md`. Pure, no I/O, no clock, no import from `main`. Style: `visit_alert.py` / `resolve_plan.py`.
- **The rule, decided (state it in the module docstring):** an unknown sender is held unless the mail is a REAL reply or a hiring signal. Not held, i.e. pings as today: Tier 1 (calendar invite, interview or offer), a REJECTION, and anything that is a direct reply. A direct reply is the same test the pre-filter already uses (main.py `passes_email_prefilter`, gate 9, "Require direct reply"): an `In-Reply-To` header, a `References` header, or a subject starting with `re:` after trimming, case-insensitive. A cold recruiter answering Kevin's outreach ("Re: Kevin Miller") therefore still pings; an unsolicited mail with none of those is held. Do not edit the pre-filter; the helper is separate on purpose (the pre-filter's flag is off by default and drops mail without leaving a trace).
- `is_direct_reply(in_reply_to, references, subject)` -> bool. None and non-string inputs are empty. A subject like `Re` or `Reminder: ...` is NOT a direct reply (the prefix is `re:` with the colon, so "Reminder" cannot match).
- `hold_reason(*, is_tier1, status_label, in_reply_to, references, subject, mode="hold")` -> `""` (do not hold, ping as today) or `"unsolicited, not a reply"`. `mode` is `"hold"` or `"alert"`; anything else, including None, is treated as `"hold"`; `"alert"` always returns `""` (the old behaviour, restored by a dashboard flip). `status_label` of `REJECTION` returns `""` even when nothing else qualifies, because rejection routing (archiving the job row) happens on the alert path and must not be skipped.
- `parse_mode(raw)` -> `"hold"` or `"alert"` from an env string: strips and lowercases, `alert` / `alerts` / `permissive` -> `"alert"`, everything else (blank, None, typo) -> `"hold"`. Constant `DEFAULT_MODE = "hold"`.
- `format_held_line(rows, limit=3)`: `rows` is a list of `(sender_name, sender_email, subject)` newest first, or `None` (the read failed). Returns `""` for an empty list (a quiet day adds no line); a line like `📪 4 unsolicited emails held, not pinged: Dana Reed - "Quick question" · a@b.com - "Hello" · … (+1 more)` for rows, singular `1 unsolicited email held` for one; a missing sender name falls back to the address; subjects and names are cut at 40 characters, HTML-escaped (the standup is sent as HTML) and a newline in either becomes a space; `None` returns `📪 held-mail count unavailable this morning` (an unreadable tray is never a zero). Never prints a snippet or body text.
- Tests (`test_inbound_hold.py`, at least 12): direct-reply truth table (header only, references only, `Re:` and `RE:` and ` re: x`, `Reminder:`, `Re`, empty, None); an unsolicited mail is held; a direct reply is not; Tier 1 is not; REJECTION is not; Tier 1 plus no reply headers is not held; `mode="alert"` never holds; an unknown mode holds; `parse_mode` table; `format_held_line` for 0, 1, 4 rows, a long subject, an HTML-special name (`<b>`), a newline in a subject, and `None`.

## Log


- 2026-10-31 cloud worker: built; verify 37 passed; main suites 1874 passed, 1 skipped.
