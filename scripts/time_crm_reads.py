"""Read-only: time each CRM tab read the bot makes, through main.crm_post and its own timeout.

Sends only {"action": "get_followups", "tab": X} for CW, CC, TC, TW, CL. Never a write.
Prints per tab: seconds, rows, response bytes, headroom under crm_post's default timeout. A
timed-out or failed read is reported as TIMED OUT / FAILED, never as 0 rows.

Run it by hand against the live Sheet:  python scripts/time_crm_reads.py
"""
import inspect
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TABS = ("CW", "CC", "TC", "TW", "CL")


def _load_main():
    import main  # heavy import, kept lazy so tests never pay for it
    return main


def _default_timeout(crm_post):
    return inspect.signature(crm_post).parameters["timeout"].default


def time_one(crm_post, tab, timeout):
    """Returns (line, ok) for one tab."""
    start = time.monotonic()
    res = crm_post({"action": "get_followups", "tab": tab})
    secs = time.monotonic() - start
    if res is None:
        return f"{tab}: TIMED OUT or no response after {secs:.2f}s (timeout {timeout}s)", False
    status = getattr(res, "status_code", None)
    if status != 200:
        return f"{tab}: FAILED HTTP {status} after {secs:.2f}s", False
    try:
        body = res.json()
    except Exception as e:
        return f"{tab}: FAILED unreadable body after {secs:.2f}s ({e})", False
    if not isinstance(body, dict) or str(body.get("status", "")).lower() != "success":
        msg = body.get("message", "") if isinstance(body, dict) else "non-dict body"
        return f"{tab}: FAILED body status not success after {secs:.2f}s ({msg})", False
    rows = len(body.get("followups", []))
    size = len(getattr(res, "content", b"") or b"")
    headroom = timeout - secs
    return (f"{tab}: {secs:.2f}s, {rows} rows, {size} bytes, "
            f"headroom {headroom:.2f}s of {timeout}s"), True


def main():
    crm_post = _load_main().crm_post
    timeout = _default_timeout(crm_post)
    bad = 0
    for tab in TABS:
        line, ok = time_one(crm_post, tab, timeout)
        print(line)
        bad += 0 if ok else 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
