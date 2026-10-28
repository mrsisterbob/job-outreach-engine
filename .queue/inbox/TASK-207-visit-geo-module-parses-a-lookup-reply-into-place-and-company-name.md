---
id: TASK-207
scope: visit_geo.py, test_visit_geo.py, visit_geo_sample.json
verify: python -m pytest test_visit_geo.py -q -k "visit_geo" 2>&1 | tail -n 1 | grep -E "^([6-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
A new pure module `visit_geo.py` picks the lookup provider, builds its request, and turns its reply into a coarse place (city, region) and a company name (only when the address belongs to a company network, not a home ISP or a hosting provider). Any failure becomes "unknown", never a place. No wiring into `main.py` here.

## Proof it is needed
`python -m pytest test_visit_geo.py -q -k visit_geo` fails because `test_visit_geo.py` does not exist (observed 2026-10-28: `no tests ran in 0.00s`, exit 1). The pass-regex is proven able to pass: `test_visit_alert.py -k visit` ends `14 passed`.

## Notes
- Run `grep -i geo FUNCTION_INDEX.md` and `grep -i ip FUNCTION_INDEX.md` first. Nothing in the repo looks up an IP today.
- **Pick the provider by measuring, then write what you measured in the Log.** Requirements, all from Kevin: needs no paid key if one exists; coarse city/region only; the organisation name when the IP is a company network; the raw IP is never stored or logged. Hard rules from the planner: HTTPS only (reject any provider whose free tier is plaintext HTTP: that sends every visitor's IP in the clear; the planner's unverified reading is that ip-api.com's free tier is HTTP only, 45 requests/minute, non-commercial, so check it, do not trust it); no API key or token in the repo, a task file, a log or a fixture (if the best option needs a free token, it is an env var read at call time and an unset var means "unknown", and say so in the Log); a request timeout of 3 seconds or less.
- **Record in the Log, with the URL you read each from:** the provider's price, its rate limit, and whether commercial/personal-site use is allowed on the free tier. Mark each "provider docs, not billed or load-tested by us". The planner searched and could not confirm any provider's free limits, so every number here is yours to verify.
- **Measure one real reply.** From this machine make ONE request for `8.8.8.8` (a public address, never a visitor's), save the raw JSON as `visit_geo_sample.json` (strip nothing but any key/token echo), and write the parser's field names from THAT file. The recurring failure in this repo is a parser tested against hand-built replies that use guessed key names (see apify_feed.py `ROW_KEYS`), so the parse tests must load the saved sample, and each other case is a one-field edit of it.
- API (names fixed, the planner's tasks 209 and 210 call them):
  - `lookup_url(ip)` / request builder: returns what the caller passes to `requests.get`; raises nothing; returns None for an address that is not globally routable (use `ipaddress`: private, loopback, link-local, reserved, multicast, malformed, empty). The IP appears only in the URL it returns.
  - `parse_reply(status_code, body)` -> `Geo(place, org)` where `place` is `"City, Region"` (or just the region when no city, or `""`) and `org` is `""` or the company name. Non-2xx, non-dict body, the provider's own failure flag, a missing city AND region, or rate-limit text all give `Geo("", "")`. Never raises. Never returns the provider's error text as a place.
  - `company_org(name)` -> the name, or `""` when it is a consumer/mobile ISP or a hosting/cloud provider. A hosting name means a bot or VPN, not a company. Keep the match list as module data: at least Comcast, AT&T, Verizon, Charter/Spectrum, T-Mobile, Frontier, Cox, CenturyLink/Lumen, Google Fiber, Mediacom, Amazon/AWS, Google Cloud, Microsoft/Azure, DigitalOcean, OVH, Hetzner, Cloudflare, Linode/Akamai. Comment it as a HEURISTIC, not a measurement: a miss shows an ISP name as the company.
  - A pure `format_place(geo)` is NOT needed; the alert text is TASK-208.
- Tests (`-k visit_geo`, at least 6): the saved sample parses to a non-empty org and place; `lookup_url` returns None for `127.0.0.1`, `10.0.0.5`, `192.168.1.9`, `::1`, `""`, `"not an ip"` and a URL for `8.8.8.8`; a 429, a 500, a list body, an empty dict, and the provider's failure shape each give `Geo("", "")`; a reply with a region but no city gives the region; `company_org("Comcast Cable Communications")` is `""`, `company_org("Amazon.com, Inc.")` is `""`, `company_org("Steelcase Inc.")` is the name; and no function in the module writes to disk or logs.

## Log
