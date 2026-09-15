---
name: cover-letter-voice
description: Write, audit, or extend Kevin's cover letter copy in templates/cover_letter_templates.json. Use when adding or editing banked letter paragraphs, adding a new track pool, reviewing whether a letter sounds human, or wiring the optional one-sentence AI slot. Triggers on "cover letter", "/letter", "cover_letter_templates.json", "letter voice", "letter sounds robotic".
---

# Cover letter voice

You maintain the banked copy behind `/letter`. The letters are pasted by hand into
Workday/Greenhouse "cover letter" textareas for $50-70k operations roles in metro Detroit.
One human reads each one for maybe fifteen seconds.

## The reference letter

This was written by hand, was the best letter the system has produced, and is the target.
Everything in this skill exists to reproduce its register.

> Dear Crain Communications Hiring Team,
>
> I am writing to express my interest in the Billing Operations Analyst position. Working
> within the operations side of an Investment Advisory firm, I sit on the same floor as our
> billing department. Over time, I've made it a point to shadow their team, giving me a clear
> look at how daily order intake and billing cycles run.
>
> Because much of my day-to-day already involves auditing contracts and resolving discrepancies
> in Salesforce, the workflows outlined in your job description feel very familiar. I understand
> the mechanics behind reconciling accounts and ensuring data parity before invoices go out, and
> I know how critical that accuracy is to the wider business.
>
> I am looking to bring this technical background and context directly into a dedicated billing
> role. I would welcome the opportunity to support Crain's team and ensure your order-to-cash
> process runs smoothly.
>
> Thank you for your time and consideration.
>
> Best regards,
> Kevin Miller

Why it works, in priority order:

1. **It leads with adjacency, not expertise.** "I sit on the same floor as our billing
   department" concedes he has not run billing. That concession is what makes the rest
   credible. Ninety percent of the value is here.
2. **One claim per sentence.** No sentence carries three tools and a metric.
3. **It explains rather than asserts.** "I understand the mechanics behind reconciling accounts"
   beats "Expert in account reconciliation."
4. **It is slightly under-qualified in tone.** He is asking to move into this work, not
   announcing he has mastered it. For a 1-3 years posting that reads as honest.

## The 20 rules

Rules 1-8 are what make the reference letter sound like a person. Rules 9-15 are the failure
modes that make copy sound generated. Rules 16-20 are mechanical constraints this codebase
imposes. When they conflict, the lower number wins.

**Structure and stance**

1. **Concede before you claim.** Lead with what you have *not* done, then what you have seen from
   next to it. "I sit on the same floor as our billing department" earns every sentence after it.
   A letter that opens by asserting expertise has nothing left to prove and reads like everyone
   else's.
2. **Explain the mechanism, don't assert the skill.** "I understand the mechanics behind
   reconciling accounts and ensuring data parity before invoices go out" shows the knowledge.
   "Expert in reconciliation" only claims it. Anyone can claim.
3. **Say why you want it, not just that you can do it.** "I am looking to bring this background
   directly into a dedicated billing role" tells them this is a deliberate move. Ambition that
   names its direction is more credible than enthusiasm.
4. **Write slightly under your level.** These are 1-3 year postings. Sounding like you have
   already mastered the job reads as either a lie or as someone who will leave in six months.
5. **One concrete anchor per paragraph, and make it physical.** A floor, a desk, a queue, a
   morning. "I sit on the same floor" beats "I have exposure to billing operations" because a
   reader can picture it.

**Rhythm**

6. **Average about 20 words per sentence, and vary hard.** The reference letter runs 13, 20, 25,
   24, 27, 16, 16. Uniform sentence length is the single loudest tell of generated text, in
   either direction: all-long reads as bureaucratic, all-short reads as clipped and robotic.
   Never write three consecutive sentences within two words of each other.
7. **Avoid the sub-10-word sentence in body paragraphs.** "My day is data integrity work." is
   punchy in isolation and mechanical in a letter. The reference letter has none.
8. **Use contractions where speech would.** "I've made it a point", "it doesn't match". An
   all-formal letter reads stiff. Do not contract everything either; "I am writing to express my
   interest" is correct as-is because that opener is a convention.

**Things that make copy sound generated**

9. **No throat-clearing opener.** Never begin with a clause about the industry or the state of
   the market ("Given the analytical demands of modern publishing"). Start at the point.
10. **No tricolon.** "Clean billing schedules, cross-department handoffs, and rapid dispute
    resolution" is the most recognizable LLM cadence there is. Two items, or restructure.
11. **No sentence that would be true of forty other applicants.** "I am detail-oriented and
    thrive in fast-paced environments" carries zero information. Cut or replace with the specific
    thing you actually did.
12. **No stacked metrics.** One number per paragraph at most. Three numbers in a sentence reads
    as a resume that wandered into the wrong field.
13. **No adjective pile-up.** "Comprehensive, detail-oriented operational support" is three words
    doing the work of none. Prefer a verb.
14. **Do not name the company more than twice**, and never in consecutive sentences. Repeating it
    is what a mail-merge does.
15. **No closing flourish.** "I would be thrilled for the opportunity to contribute to your
    continued success" is filler. End on a concrete offer or a plain request.

**Mechanical constraints of this codebase**

16. **Never claim a function that is not in `evidence_bank.json`.** It has no billing bullet. Any
    billing/invoicing/AR claim must be framed as adjacency or not written. This is the one rule
    that gets a letter thrown out if broken.
17. **No colons, semicolons, em dashes or en dashes.** `sanitize_text()` deletes them, mid-sentence,
    silently.
18. **No single-word "X, Y, and Z" triple.** `sanitize_text()` rewrites it to "X and Y" and drops
    the third item. This overlaps rule 10 and is enforced by a test.
19. **No banned words.** They hot-reload from `evidence_bank.json`'s `banned_words`.
20. **Nothing role-specific in a shared pool.** `openers`, `bridges_*`, `closers` and `signoffs`
    are used by every track. A word like "billing" or "invoice" belongs in a track pool or behind
    the title gate in `generate_cover_letter()`.

Rules 6, 7, 10, 17, 18, 19 and 20 are enforced by tests. The rest are judgment, which is why
step 4 of the workflow below is reading the letter aloud.

## How the file is structured

`templates/cover_letter_templates.json` has these pools. `generate_cover_letter()` in `main.py`
picks one entry from each and joins them with blank lines.

| Pool | What it is | Selected by |
|---|---|---|
| `openers` | "I am writing to express my interest in the {job_title} position at {company}." | routed index |
| `track_a_wealth_ops` ... `track_e_bizops` | Paragraph 1 body, the adjacency claim | `track` letter from Gemini |
| `bridges_conservative` / `bridges_tech` | Paragraph 2 | `tone_mode` from Gemini |
| `closers` | Paragraph 3 | routed index |
| `signoffs` | "Thank you for your time and consideration." | routed index |

Track letters map through `TRACK_BULLET_POOL_KEYS` in `resume_engine.py`, the same map the resume
PDF uses, so the letter and the attached resume always argue one case. Never introduce a second
naming scheme.

Index 0 of `bridges_*` and `closers` is deliberately billing-flavored. `generate_cover_letter()`
skips past it unless the job title matches billing/invoice/revenue/AR. If you add more
role-specific copy, extend that regex rather than letting the copy leak into unrelated roles.

Only `{company}` and `{job_title}` interpolate. `{name}` is not supported.

## Workflow when you change copy

1. Read `evidence_bank.json` first and confirm every factual claim you are about to write
   appears there. If it does not, reframe as adjacency or drop it.
2. Edit `templates/cover_letter_templates.json`.
3. Run `python -m pytest test_main_integration.py -k cover_letter -q`. These tests enforce the
   mechanical rules above and will catch a colon, a dropped triple, a repeated six-word run
   across paragraphs, and billing copy leaking into a non-billing role. **A failure here is a
   real defect in the copy, not a flaky test.** Fix the sentence.
4. Print a real letter and read it aloud before you call it done:
   ```
   python -c "import main; print(main.generate_cover_letter('Crain Communications','Billing Operations Analyst','e',0,'','tech'))"
   ```
   If any sentence would embarrass a person to say out loud in an interview, rewrite it.
5. Run the full suite (`python -m pytest -q`), then `python update_readme_stats.py`.

## Rating the current bank

When asked how good the letters are, score against the reference letter above, not against
"is this grammatical". Report a number out of 10 with the specific sentence that costs the most
points. As of the last audit the bank sits at **8/10**: the adjacency framing, sentence length,
and honesty are right; the remaining gap is that no letter can say anything specific about the
company it is addressed to, because nothing in the deterministic path reads the job description.

## The last 10 percent: the AI slot

Kevin has approved a small AI contribution to close that gap. The rule is that the model
supplies **at most one sentence per letter**, and never touches the rest.

If you implement it, implement it exactly this way:

- Add a `"jd_hook"` slot rendered at the **end of paragraph 2**, after the banked bridge text.
- Gemini's task is narrow: given the job description, write **one sentence, 25 words maximum**,
  naming something concrete from the posting and connecting it to work already in
  `evidence_bank.json`. It writes nothing else.
- Send it `build_evidence_context_block(mode="eval")` so the banned words and verified
  experience travel with the prompt.
- **Validate before use, and drop it on any failure.** Reject the sentence if it: contains a
  banned word, exceeds 25 words or contains more than one sentence, contains a digit not present
  in the job description, or names a tool absent from `technical_skills`. A dropped hook means
  the letter renders exactly as it does today. Never let a validation failure produce a
  degraded letter, and never retry into a weaker check.
- Run it through `sanitize_text()` with the rest of the letter.
- Log every rejection with the reason so the failure rate is visible.

Do not expand the slot beyond one sentence, and do not let the model rewrite banked copy. The
whole system's value is that every other word is traceable to a file Kevin edited.

## Note on `voice_and_tone`

`build_evidence_context_block()` in `main.py` reads `evidence_bank.json["voice_and_tone"]`, but
that key does not currently exist, so every Gemini prompt ships an empty VOICE & TONE section. If
you add the AI slot, add that key too - the guidance in "Hard rules" above is what belongs in it.
