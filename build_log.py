"""Weekly build-log draft for LinkedIn. Pure: no I/O.

Turns one week of engine counts into a short first-person draft Kevin edits into a post. The
caller (main.py, TASK-13) does the SQLite reads and passes the counts in.

Two rules make the draft safe to post as-is:
- Every digit in it is one of the counts. No dates, rates or "7 days": a derived number is an
  invented one, and a public post is the worst place to invent one. Counts are printed bare
  (str(n), never "1,234"), so a thousands separator cannot split one count into two numbers.
- A count of None means its read failed, and its line is left out. Printing 0 would be the silent
  zero this repo keeps shipping, and naming the gap ("unavailable") is negative language that
  pipeline_utils.negative_language_hits flags. A real 0 is a real number and keeps its line.

The key set is checked, not trusted: a misspelled or missing key would otherwise read exactly like
a failed read and drop its line without a trace (Code.gs/main.py drift, the same shape).

Plain text only: it goes out through Telegram with parse_mode=HTML, so no < > & anywhere.
"""

# (key, singular line, plural line), in funnel order. "{n}" is the only placeholder.
_LINES = (
    ("discovered", "{n} role discovered", "{n} roles discovered"),
    ("screened", "{n} role screened", "{n} roles screened"),
    ("documents", "{n} application document compiled", "{n} application documents compiled"),
    ("drafts_staged", "{n} outreach email drafted", "{n} outreach emails drafted"),
    ("applied", "{n} application sent", "{n} applications sent"),
    ("replies", "{n} new reply", "{n} new replies"),
    ("interviews", "{n} interview set", "{n} interviews set"),
)

BUILD_LOG_KEYS = tuple(key for key, _, _ in _LINES)

# Applications, replies and interviews are Kevin's and other people's doing, so the opening
# credits the week, and the close says which part the engine does.
_OPENING = "Build log: here is how my job search went this week."
_CLOSE = ("The engine finds and screens roles and drafts the documents and emails. "
          "I review and send every one myself.")


def compose_build_log(counts):
    """The week's build-log draft as plain text, or None when every count is None.

    `counts` must have exactly the keys in BUILD_LOG_KEYS, each a non-negative int, or None when
    its read failed. None drops its line; 0 keeps it. A wrong key set raises ValueError and a
    non-int (bool and float included) raises TypeError, so a caller's contract drift fails its
    tests instead of quietly thinning the draft.
    """
    if set(counts) != set(BUILD_LOG_KEYS):
        raise ValueError(
            f"build log counts need exactly {sorted(BUILD_LOG_KEYS)}; got {sorted(counts)}")
    lines = []
    for key, singular, plural in _LINES:
        n = counts[key]
        if n is None:
            continue
        if isinstance(n, bool) or not isinstance(n, int):
            raise TypeError(f"build log count {key!r} must be an int or None, got {n!r}")
        if n < 0:
            raise ValueError(f"build log count {key!r} is negative: {n}")
        lines.append("- " + (singular if n == 1 else plural).format(n=n))
    if not lines:
        return None
    return "\n".join([_OPENING, ""] + lines + ["", _CLOSE])
