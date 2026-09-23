# Working in this repo

## Before writing any new helper, search the function index

`main.py` grew 9,311 → 13,318 lines in five days. Nobody can hold that namespace in their head,
and the failure mode is not that the file is hard to read — it is that you cannot see what
already exists, so you write it again. **A duplicate never fails a test.**

This happened on 2026-09-23: a `/linkedin` command was built with a hand-rolled scan over four
CRM tabs, while `Code.gs` already exposed a server-side `find_contact_by_email` that searched
every tab. The hand-rolled version silently could not see contacts in Carmen Warm, Carmen Hot or
Killed. It passed every test.

So, before adding a function, a command, or a CRM read/write:

```bash
grep -i email FUNCTION_INDEX.md
grep -i contact FUNCTION_INDEX.md
```

`FUNCTION_INDEX.md` lists every function in every tracked Python file plus every CRM action
`Code.gs` handles, each with its one-line summary. Regenerate it after adding a subsystem:

```bash
python build_function_index.py
```

It is deliberately not wired into a hook or CI (this repo has no CI-backed remote). Re-running is
always safe — the file is rewritten from source.

## CRM actions live in two places

Python talks to the Google Sheets CRM through `crm_post({"action": ...})` / `crm_get(...)`, and
the handler is in `Code.gs`. A server-side action searches **every tab**; a Python-side scan
usually walks a subset. Check the `Code.gs` section of the index before scanning tabs by hand.

There is no shared schema between the two sides — a field the Python side stops sending fails
silently as a clean zero rather than an error.

## New subsystems go in new modules

`pipeline_utils.py` (~2,000 lines) is the proof this works. Pure logic, no I/O, easy to test.
Prefer it over adding to `main.py`. `main.py` is already 280 top-level functions.

## Tests

```bash
python -m pytest test_main_integration.py test_pipeline_utils.py -q
```

Both suites should be green before anything is considered done.

**Test the write path, not the return value.** The recurring failure here is a spec that passes
on what a function returns while the next run reads back something different. When a change
affects what the sequencer or the CRM reads on its *next* pass, write the test that drives the
real entry point — not one that hand-builds the dict the function under test expects.
