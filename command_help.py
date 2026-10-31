"""Per-command help: `/e/` explains /e instead of running it.

Why a trailing slash. The bot has 71 commands and four slot-code schemes (L/C/W/B/R for the
template banks, TA-TE for resume bullets), and /help prints all of it as one 60-line wall that
you have to find your own line in. A trailing slash is the one suffix that cannot collide with a
real command - Telegram will not autocomplete it, no handler matches it, and it reads as "what is
this" rather than "do this".

Pure data plus two lookups, no I/O, so it is trivially testable. main.py calls
lookup_command_help() before any command dispatch; anything not in HELP falls through untouched
and behaves exactly as it did before.

Every entry is (what it does in a sentence or two, a concrete example). Keep the prose plain -
this exists because the reference got confusing, so an entry that needs its own explanation has
failed. The one-liners in main.py's /help menu stay as they are; this is the long form.
"""

# (summary, example). The summary is 1-2 sentences of plain prose. The example is a literal
# command line with a short "->" note on what comes back, so the answer is readable on a phone
# without a follow-up tap.
HELP = {
    # ---- The two commands Kevin actually applies with ----
    "/e": (
        "Locks a contact's email address onto the job card you are replying to, then re-drafts "
        "the outreach against that person. Use it when you already know the address.",
        "/e dana.reed@atwell.com\n-> locks the address, redraft uses her name",
    ),
    "/eh": (
        "Same idea as /e but it LOOKS UP the address for you through the Apollo API, so it "
        "spends a credit. Give it a name when the card does not already carry one.",
        "/eh Dana Reed\n-> finds the address, locks it, re-drafts",
    ),
    "/draft": (
        "Creates the Gmail draft for the card you are replying to. Nothing sends - it lands in "
        "Drafts for you to read and send by hand.",
        "reply /draft to a job card\n-> Gmail draft created",
    ),
    "/letter": (
        "Writes the cover letter for the card, on the same resume track the PDF uses, so the "
        "letter and the resume argue the same case.",
        "reply /letter to a job card",
    ),
    "/cv": (
        "Compiles the tailored resume PDF for this card. /resume does the same thing.",
        "reply /cv to a job card\n-> PDF with the track's bullets",
    ),
    "/track": (
        "Switches the resume track on a job card when Gemini routed it wrong, so /cv, /letter and /e "
        "all rebuild on the new track. Rewrites the card's compass tag too, so the change survives a "
        "deploy. Add bullet numbers to pick which bullets the resume uses.",
        "reply /track to a job card\n-> current track and the list\n"
        "reply /track h\n-> track H, with its numbered bullet pool\n"
        "reply /track h 9,3,4,0\n-> track H with those four bullets",
    ),
    "/refs": (
        "Prints your saved references with every field tap-to-copy, for the References section "
        "of an application form. Kept in the private CRM sheet, never in the code.",
        "/refs\n/refs add Dana Reed | dana@acme.com | 313-555-0100 | Work | 2 | Acme | Detroit, Michigan\n"
        "/refs remove 2",
    ),

    # ---- Pulling work in ----
    "/t": (
        "Pulls fresh job cards from every source and scores them. This is the main intake.",
        "/t",
    ),
    "/w": (
        "Warm radar. Checks ONLY the companies where you already have a Carmen Warm contact and "
        "reports new postings there. No AI scoring, near-instant, and it is the fastest way to "
        "find a reason to write to someone on the bench.",
        "/w\n-> new roles at warm-contact companies, each tied to its contact",
    ),
    "/job": (
        "Adds a job you found yourself from its URL, then scores and files it exactly like /t "
        "would. /j is the short form.",
        "/job https://boards.greenhouse.io/acme/jobs/12345",
    ),
    "/c": (
        "Pulls networking cards - the people, not the jobs. /cw pulls only the Warm bench and "
        "/cc only the Cold sprint.",
        "/c",
    ),

    # ---- Moving contacts through the pipeline ----
    "/promote": (
        "Moves a contact who actually replied up to Carmen Hot. Always a manual tap, never "
        "automatic, because a human who talked to you should not be moved by a script.",
        "/promote dana.reed@atwell.com",
    ),
    "/demote": (
        "Parks a Carmen Cold or Hot contact back on the Warm bench. Reversible - the row stays.",
        "reply /demote to a contact card",
    ),
    "/x": (
        "Archives the lead you are replying to into the Died or Killed tab. Reversible.",
        "reply /x to a card",
    ),
    "/linkedin": (
        "Logs that you connected or DMed someone on LinkedIn, so the automated email bump does "
        "not fire at a person you are already mid-conversation with. /li is the short form.",
        "/linkedin dana.reed@atwell.com",
    ),
    "/n": (
        "Appends a timestamped note to the record you are replying to.",
        "/n left a voicemail, she is back Monday",
    ),
    "/f": (
        "Snoozes this record's follow-up by a number of days.",
        "/f 7\n-> next follow-up moves out a week",
    ),

    "/refer": (
        "Lists the referral asks /w has written for warm contacts, with the text ready to copy, 5 per "
        "page and oldest first. 'sent' hands the contact to the Carmen Cold nudge ladder; 'dismiss' "
        "drops an ask that can never be sent (the contact moved to Killed or was deleted) and writes "
        "nothing to the CRM.",
        "/refer\n-> page 1 of the pending asks\n/refer 2\n-> page 2\n"
        "/refer sent 14\n-> ask 14 marked sent, contact moves to Carmen Cold\n"
        "/refer dismiss 14\n-> ask 14 leaves the list",
    ),
    "/ghost": (
        "The switch behind the nightly 'ghosted' burial of Applied rows with no movement. ON moves "
        "them to Died; OFF leaves every application where it is. With no argument it shows the "
        "state and the rows past the cutoff, oldest first, one page at a time.",
        "/ghost\n-> state plus page 1 of the rows past the cutoff\n/ghost 2\n-> page 2\n"
        "/ghost on\n/ghost off",
    ),

    # ---- The template bank editor, which is the confusing one ----
    "/edit": (
        "Edits one banked template in place by its slot code. The code is a letter for the bank "
        "plus a number for the position in it, and the new text is linted before it saves.\n\n"
        "<b>C0-C7</b> cold outreach · <b>W0-W5</b> warm (hand-written scaffolds)\n"
        "<b>B0-B1</b> follow-up bumps · <b>R0-R3</b> reactivation (dormant warm contacts)\n"
        "<b>L0-L9</b> LinkedIn notes · <b>TA0-TH14</b> resume bullets by track",
        "/edit R2 Hi{name}, ...\n-> replaces reactivation slot 2",
    ),

    # ---- Batch and review ----
    "/sendall": (
        "The Tuesday batch. Drafts every due follow-up bump and pushes eligible overdue records "
        "out to +14 days. Drafts only - nothing sends itself.",
        "/sendall",
    ),
    "/queue": (
        "Shows what the nightly follow-up sequencer WOULD do tonight, without doing any of it. "
        "Read-only, so it is the safe way to check the ladder before it runs.",
        "/queue",
    ),
    "/inbox": (
        "Open conversations that still need a reply from you.",
        "/inbox",
    ),
    "/linksx": (
        "Archives every dead-link row you APPLIED to straight to Died, in one go. The nightly "
        "sweep will not do this on its own - a posting coming down on a job you applied to is "
        "not a rejection, so those rows wait for you. This is that decision, taken for all of "
        "them at once. Each role is also recorded locally as buried, so /t can never re-source "
        "it. Rows already auto-retired are untouched.",
        "/linksx",
    ),
    "/trace": (
        "Answers 'did my system actually see this reply?' for one address. Shows whether the "
        "poller logged the thread, whether Telegram alerted, and - the part that bites - whether "
        "the CRM note carries a reply anchor. No anchor means the sequencer still thinks they "
        "never wrote back, and will bump them.",
        "/trace dpatnaik@aaalife.com",
    ),
    "/brief": (
        "One page covering the whole pipeline - intake, outcomes, board coverage and live "
        "conversations.",
        "/brief",
    ),
    "/hot": (
        "Just the live conversations in Carmen Hot - calls, interviews, referrals.",
        "/hot",
    ),

    # ---- Numbers ----
    "/outcomes": (
        "Reply and interview rates by source and by outreach path, from recorded results rather "
        "than estimates.",
        "/outcomes",
    ),
    "/treplies": (
        "Reply rate grouped by which template id sent the email, so a bank entry that never "
        "earns a reply is visible. Read-only.",
        "/treplies",
    ),
    "/usage": (
        "How often you actually use each command, over a week, month, 90 days or all time.",
        "/usage month",
    ),
    "/wstages": (
        "Shows the last /w run's sample titles at each stage: pulled, title filter, US/English, "
        "Remote, Michigan screen, AI screen and card. /wstages 2 shows the run before, and the "
        "number can be 1-5. Read-only.",
        "/wstages 2",
    ),
    "/vlink": (
        "Makes a personal link to montelattice.com for one contact (https://montelattice.com/?r=...). "
        "When they open it you get an alert that says who opened it. Asking again for the same name gives "
        "the same link. /vlink alone lists the links made and how many times each was opened.",
        "/vlink Jane Doe",
    ),
    "/monte": (
        "Shows where visits to montelattice.com came from (direct, LinkedIn, search, email and chat, other sites, bots) "
        "and which pages each group viewed, day by day, with what the numbers cannot know. Read-only.",
        "/monte  -> the last 7 days\n/monte 30  -> the last 30 days (1-90)",
    ),
    "/wrun": (
        "Shows a saved /w run company by company: which source answered, the name that was sent and the names that "
        "came back. /wrun Haworth shows one company, /wrun full gives the whole closing text, and /wrun 2 picks the "
        "run before. Read-only.",
        "/wrun Haworth  -> sent, returned and dropped names for Haworth\n/wrun  -> every company in the newest run\n/wrun full  -> the full closing text",
    ),
    "/streak": (
        "The daily outreach scorecard - what you sent today and whether the streak held. "
        "/daily is the same thing.",
        "/streak",
    ),
    "/eco": (
        "Shows the ATS boards being tracked. Add one with 'add' and it gets scanned from then "
        "on. /ecosystem is the long form.",
        "/eco add atwell\n-> Atwell's board joins the scan",
    ),
    "/health": (
        "System telemetry - schedulers, queue depth, and anything currently erroring.",
        "/health",
    ),
}

# Commands that are the same handler under a different name. Asking about either shows the
# canonical entry, so /resume/ does not dead-end just because the dict is keyed on /cv.
ALIASES = {
    "/j": "/job",
    "/resume": "/cv",
    "/email": "/e",
    "/li": "/linkedin",
    "/cw": "/c",
    "/cc": "/c",
    "/ecosystem": "/eco",
    "/daily": "/streak",
}


def parse_help_request(text):
    """Return the command a trailing-slash request is asking about, or None.

    `/e/` -> `/e`. Anything else, including a bare `/` or a command with arguments, returns None
    so the caller leaves it alone. Case-insensitive, because phone keyboards capitalize.
    """
    stripped = str(text or "").strip()
    if len(stripped) < 3 or not stripped.startswith("/") or not stripped.endswith("/"):
        return None
    # A space means arguments were passed, which is a real invocation, not a question.
    if " " in stripped:
        return None
    return stripped[:-1].lower()


def lookup_command_help(text):
    """Return formatted help for a `/cmd/` request, or None if this is not one.

    None means "not a help request, carry on dispatching" - so an unknown command with a trailing
    slash still returns a message rather than silently falling through to the full /help wall.
    """
    command = parse_help_request(text)
    if command is None:
        return None

    canonical = ALIASES.get(command, command)
    entry = HELP.get(canonical)
    if not entry:
        return (f"❓ No per-command help for <code>{command}</code> yet.\n\n"
                f"Send <code>/help</code> for the full reference.")

    summary, example = entry
    alias_note = f"\n\n<i>{command} is the same as {canonical}.</i>" if canonical != command else ""
    return (f"📖 <b>{canonical}</b>\n\n{summary}{alias_note}\n\n"
            f"<b>Example</b>\n<code>{example}</code>")
