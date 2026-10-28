---
id: TASK-205
scope: pipeline_utils.py, test_pipeline_utils.py
verify: python -m pytest test_pipeline_utils.py -q -k "visit_probe" 2>&1 | tail -n 1 | grep -E "^([6-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
`visit_bot_flag` and `classify_visit` take an optional `path`, and a path that is not one of montelattice.com's real pages comes back as the kind `"probe"`, so a scanner hitting `/.env` is stored as a bot instead of a person.

## Proof it is needed
`python -m pytest test_pipeline_utils.py -q -k visit_probe` collects nothing, so the verify exits 1 (observed 2026-10-28: `407 deselected in 0.92s`). Today `classify_visit` (pipeline_utils.py:2686) judges only user agent, ip and the ignore list: `/.env`, `/wp-config.php`, `/composer.json`, `/appsettings.json`, `/config/parameters.yml`, `/web.config`, `/.env.js`, `/.env-config.js`, `/settings.php`, `/phpinfo.php`, `/wp-login.php` and `/litellm/config.yml` all pass as people. Pass-regex proven able to pass: `-k help` on the same file ends `6 passed, 401 deselected`.

## Notes
- Run `grep -i visit FUNCTION_INDEX.md` first. `classify_visit` (2686), `visit_bot_flag` (2705), `summarize_visits` (2761) and `format_visitor_line` (2786) exist; extend them, add nothing parallel.
- **The real pages** (read from montelattice-site/main.py on 2026-10-28; every route there is a literal string): exact paths `/`, `/lattice`, `/code`, `/job-engine`, `/docfiler`, `/crypto`, `/budget`, `/budget/login`, `/budget/logout`, `/console`, `/console/login`, `/console/logout`, `/console/demo`, and the prefix `/resume/` (the PDF route is `/resume/<file>`). Put them in one module constant, `SITE_PAGE_PATHS` (frozenset) plus `SITE_PAGE_PREFIXES = ("/resume/",)`, with a comment that the site's routes are the source of truth and this copy drifts when a page is added: the first symptom of drift is a real new page being stored as a bot. Do not import from or read the site repo.
- `is_site_page(path)`: strip whitespace, drop anything from `?` or `#`, compare case-SENSITIVELY (Flask routes are case-sensitive, so `/Lattice` 404s on the site), exact match only; no trailing-slash forgiveness (`/code/` is a 404 on the site). A non-string or empty path counts as `/` (the ingest already defaults a missing path to `/`).
- New signature: `classify_visit(user_agent, ip, is_self, ignore_ips=(), path=None)` and the same on `visit_bot_flag`. `path=None` means "no path check", so every existing positional caller (tests at test_main_integration.py:13609 and :13617) keeps its result. Order: self, preview, health, crawler stay first and keep their labels (a crawler UA on `/.env` is still `crawler`; Kevin's own traffic is never a bot); only when all of those say person AND `path` is not None AND `is_site_page(path)` is False does it return `"probe"`.
- `summarize_visits` and `format_visitor_line` already count every non-None bot kind into "N bot hits filtered", so `"probe"` needs no change there; add a test that proves it (a probe row is not a visitor and adds to the bot count).
- Tests (`-k visit_probe`, at least 6): each of the twelve probe paths from the failure list is a probe; each real page and `/resume/anything.pdf` is None; the query string is ignored (`/lattice?r=abc` is None, `/.env?x=1` is a probe); `/Lattice` and `/code/` are probes; self and ignore-list traffic on a probe path stays `self`; a link-preview UA on a probe path stays `preview`; `path=None` keeps the old result for a probe-looking path.
- The ingest wiring is TASK-206. Do not touch `main.py` here.

## Log

- Attempt 1 (built): added SITE_PAGE_PATHS, SITE_PAGE_PREFIXES, is_site_page; `path=None` param on classify_visit/visit_bot_flag returning "probe" last. Added 8 `visit_probe` tests. Verify: `8 passed, 407 deselected`, exit 0. `pytest test_pipeline_utils.py -q`: `415 passed`, exit 0. main.py untouched. FUNCTION_INDEX.md not regenerated (not in scope).
- Worker re-check: verify exit 0 (8 passed). review skipped: 31 non-test lines. Worktree full-suite gate exit 0.
