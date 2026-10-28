---
id: TASK-222
scope: source_scoring.py, scripts/score_source.py, test_source_scoring.py
verify: python -m pytest test_source_scoring.py -q 2>&1 | tail -n 1 | grep -E "^([9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
One command scores any source's answers (a CSV from the free resolver, the classifier, or pasted from an actor run) against Kevin's hand-labelled truth file for the 60-company sample, and prints hits, false hits and misses per source and per size tier, plus dollars per correct board and wall-clock only when Kevin supplies a cost and a time.

## Proof it is needed
`python -m pytest test_source_scoring.py -q` finds no file, so the verify exits 1 (re-run before starting and paste the line). Pass-regex proven able to pass: `python -m pytest test_warm_export_reader.py -q` ends `N passed` with N >= 9. Nothing compares sources today: `scripts/measure_board_systems.py` reports a bucket per company but has no truth to score it against, so "which source is best" has no number behind it.

## Notes
- Run `grep -i score FUNCTION_INDEX.md` and `grep -i board FUNCTION_INDEX.md` first (the scoring hits there are job-fit scores, not this). Reuse `board_system_classifier.classify_board_system` and `workday_board.board_triple_from_url` to turn a board URL into a system and token; `warm_sample.read_sample` (TASK-221) for the tiers. Depends on TASK-221.
- **Three CSV shapes, all one header row.**
  - SAMPLE: TASK-221's file.
  - TRUTH (Kevin hand-labels about 20 of the 60): `company_key,system,token`. `system` is a value from `board_system_classifier.SYSTEMS` or the word `none` (Kevin checked and the company has no public board). `token` is the slug, or the Workday `tenant/wdN/site` triple, or blank for `none`. A blank `system` means "not labelled yet": that row is ignored and counted in a `not labelled` figure.
  - RESULT (any source): `company_key,system,token`, one row per company the source answered, absent rows mean "no answer". Write the format down in the module docstring.
- **Actor CSVs will not have those column names, and their real column names are NOT verified anywhere in this repo.** So the CLI takes `--company-col`, `--domain-col`, `--system-col`, `--token-col`, `--url-col` (any subset). Join rule: by `company_key` when `--company-col` holds a name, normalised the way TASK-221 does; else by `domain` against the sample's domain column. A `--url-col` is turned into system and token with the classifier and `board_triple_from_url` (a Workday URL gives a triple; a greenhouse/lever/ashby/smartrecruiters/workable/bamboohr URL gives the first path segment or subdomain as the token; state the rule per system in the docstring and test it). When a named column is not in the file's header the command exits non-zero and prints the header it found; it never scores a mismatched column as "no answers".
- **Verdict per labelled company, per source** (state the table in a comment): truth has a board and the result has the same system and token (case-insensitive, trimmed) = HIT; the result names a board and truth is `none`, or a different system/token = FALSE HIT; truth has a board and the result has no answer = MISS; truth `none` and no answer = CORRECT NEGATIVE (not a miss, not a hit). A result row for a company with no truth label is reported once as `unlabelled answers: N` and never scored. Rates are over labelled companies only.
- **Report.** Per source: HIT, FALSE HIT, MISS, CORRECT NEGATIVE as counts, overall and per tier (top, mid, tail). Always print `n` beside any rate (about 7 labelled companies per tier is the expected size; never a percentage without its n). `--cost USD` adds `dollars per correct board = cost / HITS`, printed as `n/a (0 hits)` when HITS is 0, never infinity and never 0. `--seconds S` adds wall-clock and seconds per correct board the same way. With neither flag, those two lines are absent (never zero, never estimated). Multiple `--result name=path` args print the sources side by side in one table.
- `--init-truth OUT.csv` writes the labelling file from the sample (`company_key,name,tier,domain,system,token` with the last two blank), refusing to overwrite and refusing a path inside the repo root, so Kevin can fill it in Excel. Reading the truth file accepts the extra columns.
- Pure scoring in `source_scoring.py`; `scripts/score_source.py` is the CLI. No network, no CRM, nothing written except `--init-truth`.
- Tests (`test_source_scoring.py`, at least 9, made-up companies): one of each verdict; false hit on a wrong token with a right system; `none` truth plus no answer is a correct negative; unlabelled answers counted not scored; per-tier counts with n; `--cost` with 2 hits divides by 2 and with 0 hits prints `n/a`; no cost flag means no dollar line; a missing mapped column exits non-zero and names the header; a URL column resolves a Workday URL to its triple and a lever URL to its slug; the output has a row per source when two results are given.

## Log


- Built source_scoring.py, scripts/score_source.py, test_source_scoring.py (no existing file touched). Reused classify_board_system, board_triple_from_url, warm_sample.read_sample/_inside, warm_export_reader._key (TASK-221 key rule). grep of FUNCTION_INDEX found no existing board-scoring helper.
- Attempt 1: first pytest run 1 failed, 11 passed: my own test expected the unjoinable "ghost" row inside score()["unlabelled"], but it is counted as `unjoined` by read_result and added in the CLI. Fixed the new test (no existing test edited).
- Attempt 2: verify exit=0; test_source_scoring.py "12 passed in 0.09s", exit=0.
- Design notes: bare --cost/--seconds number only with exactly one --result, else name=value; column flags apply to all --result files; truth keys absent from the sample are ignored; real column names of actor CSVs remain unverified (mapped by flags only). Not run against real sample/truth files (absent here).

- Review fix: (1) mapping column flags with no --company-col/--domain-col now keeps company_key as the join column; a file lacking it exits 2 naming its header (was: all rows silently unjoined, rc=0). (2) greenhouse embed URL uses the for= value (blank if absent); docstring updated. Added 2 tests. Verify exit=0; test_source_scoring.py "14 passed in 0.08s". `unlabelled answers` left as-is (joined-unlabelled distinct companies + unjoined rows).
- Worker re-check: verify exit 0 (14 passed). Review (319 non-test lines): 2 real findings fixed (silent join-column drop -> exit 2; greenhouse embed ?for= token); minor unlabelled-count consistency noted. No real sample/truth files in container: fake data only. Worktree full-suite gate exit 0.
