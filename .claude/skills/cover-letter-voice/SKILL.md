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

## Hard rules for any banked sentence

- **Never claim a function Kevin has not held.** `evidence_bank.json` is the only source of
  truth for what he has done. It contains **no billing bullet** - if a claim about billing,
  invoicing, or AR is not in that file, it must be framed as adjacency ("I sit next to",
  "I have shadowed", "one desk over") or not written.
- **No colons, no semicolons, no em/en dashes.** `sanitize_text()` in `main.py` deletes them.
- **Never write a single-word "X, Y, and Z" triple.** `sanitize_text()` rewrites it to "X and Y"
  and silently drops the third item.
- **Two specifics per paragraph, maximum.** A third turns the paragraph into a resume.
- **No banned words.** They hot-reload from `evidence_bank.json`'s `banned_words`.
- **Short sentences.** If one needs a comma-spliced list of tools, split it in two.
- **Contractions are fine and preferred.** "I've made it a point" reads human; "I have made it
  a point" reads stiff. The reference letter uses them.

Things that make a letter sound machine-written, all of which are banned here: opening with a
throat-clearing clause about the industry ("Given the analytical demands of modern publishing"),
naming the company without saying anything about it, stacking three metrics in one sentence, and
any sentence that would be equally true of forty other applicants.

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
