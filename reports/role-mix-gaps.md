# Role-mix gaps from the live job tabs

Source: live CRM get_followups (TC, TW, CL, DD), 2026-10-23, 402 rows, 402 titles

Tabs read: TC 16 rows / 16 titles, TW 83 rows / 83 titles, CL 56 rows / 56 titles, DD 247 rows / 247 titles. Carmen Warm, Carmen Cold and Carmen Hot are PEOPLE tabs with no job title and were not read.

Exact repeats of company + title (case-insensitive, within the 402 titles): 3

Method: each title goes through `track_registry.override_track_for_title` (probe track `a`, no employer, no tone_mode). A title that comes back as `a` fired no rule and is counted `none`. No employer was passed, so the employer-is-finance guard is not applied; these counts are the title rules alone.

## Titles by track

Titles read: 402

| track | count |
|---|---|
| d | 31 |
| f | 20 |
| g | 44 |
| h | 6 |
| none | 301 |

Examples for d: Business Intelligence/Data Analyst (Hybrid); Data Analyst; Business Intelligence Data Analyst; Hybrid Renewal Desk Data Analyst - Detroit | Excel & Salesforce; Business Intelligence Intern

Examples for f: Logistics Analyst - Network Performance & Insights; Logistics Analyst — Network Performance & Operations; Logistics Analyst; Supply Chain Analyst: Data-Driven Logistics & Improvement; Hybrid Vehicle Logistics Solutions Analyst

Examples for g: Analyst, Procurement; Supply Chain Business Analyst - 4 days per week on-site in Warren, MI; Supply Chain Analyst | Coordinator; Supply Chain Analyst; Procurement Operations Specialist – Coupa & SAP

Examples for h: Management Berater:in - Post Merger Integration und Value Creation (m/w/d); Business Technology Analyst – Data, Automation and AI; Strategic IT Systems Analyst: ERP/CRM & Process Automation; RPA Analyst; ERP & Business Systems Administrator - Dynamics GP Expert

Examples for none: AI-Driven Process Improvement BA (Remote); Treasury Operations Intern; ERP Systems Specialist; IT Administrator - Hybrid (Cloud & Salesforce); Compliance Specialist

Tracks a, b, c, e received 0 titles from title rules by design (no rule selects them); they are reachable only from Gemini reading the description.

## Families with no title rule

Counted among the 301 `none` titles. Families overlap (a title can sit in two), so these do not sum. The rows' `note` field records Gemini's fit reasoning, not a track letter: 1 of 402 notes contain the word "track", so the track Gemini assigned is not recoverable from these tabs.

- business analyst: 18
- systems analyst: 39
- IT business analyst: 10
- operations specialist: 35
- operations analyst: 25
- IT / administrator / help desk: 25
- ERP / CRM / Salesforce / ServiceNow: 18
- finance / accounting / audit: 24
- sales / marketing: 17
- intern / co-op: 13
- data (not analytics/data analyst): 12
- customer / client service: 10
- compliance: 7
- inventory / material: 6
- treasury / investment: 5
- coordinator: 5
- retirement: 6
- process improvement: 6
- pension: 2

Whether a rule looks unambiguous:
- business analyst: Looks unambiguous as a family but not as a track: the work spans IT, supply chain and insurance, so a single title rule would misroute some; needs a decision.
- systems analyst: Ambiguous: Business, IT, ERP, lending and HR systems analysts want different tracks (h for ERP/automation, a/d for finance).
- IT business analyst: Fairly unambiguous (IT-side requirements work); the natural landing is h, but only 2 titles.
- operations specialist / operations analyst: ambiguous, the word says nothing about the industry (freight, finance and plant all use it).
- retirement / pension: unambiguous as a family (plan administration at benefits or wealth employers), but no track summary names it, see below.
- IT / ERP / CRM groups: mixed; ERP titles at manufacturers are already caught by h/g patterns, the rest are not.

## Families with no track or capability phrase

Counted across all 402 titles. Checked against every `TRACK_REGISTRY` summary, summary_plain, keywords, skills, prompt_label and subtitle, and `CAPABILITY_TERMS`.

- retirement: 6
- pension: 2
- benefits administration: 1
- accounts receivable: 1
- process improvement: 9
- business analyst: 16
- systems analyst: 42
- inventory / material: 8
- customer / client service: 13
- coordinator: 6

Unambiguous?  retirement and pension: yes (plan administration); benefits administration: yes; accounts receivable: yes but finance-framed, would sit with a/d; process improvement: ambiguous, it is a modifier on analyst titles across IT, operations and BA roles.

Vocabulary that high-fit postings use and the résumé lacks is already reported by `/gaps` (main.py:13210) from the live cache on Render; this report does not repeat it.
