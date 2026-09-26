"""
track_registry.py
=================
The single source of truth for resume/cover-letter persona tracks.

Before this module the taxonomy was duplicated in six places (resume_engine.TRACK_BULLET_POOL_KEYS,
resume_engine.TRACKS, resume_bullets_bank.json, templates/cover_letter_templates.json,
response_schema's Literal, and the prompt text in main.build_system_prompt). Going from five
tracks to eight through six files is exactly where a silent mismatch gets introduced: a key that
does not resolve falls back to track a rather than raising, which is how a logistics job
description ended up with a wealth-operations summary.

Deliberately dependency-free (stdlib only, no typst, no pydantic). response_schema.py imports it,
and response_schema must stay importable even on a machine where the Typst binary is broken -
otherwise a rendering problem takes out Gemini response parsing too.
"""

import re

# Persona framing only. Factual content (jobs, dates, metrics) lives in evidence_bank.json; every
# system named in a `skills` footer must already appear in evidence_bank's technical_skills, or be
# a vetted capability phrase listed in CAPABILITY_TERMS below.
TRACK_REGISTRY = {
    "a": {
        "pool_key": "track_a_wealth_ops",
        "fallback_bullet": "Reconciled high-volume data variances and mapped ownership structures to establish risk escalation logic.",
        "aliases": ("wealth_ops", "wealth", "wealth operations"),
        "prompt_label": "wealth operations",
        "subtitle": "Financial Systems & Operations",
        "keywords": ("Wealth Operations", "Process Automation", "Python", "SQL", "Salesforce", "Reconciliation"),
        "summary": "I reconcile custodial accounts across 500+ client files and automate onboarding paperwork with Python and Salesforce.",
        "skills": [
            ("Core Operations", "Custodial Cashiering & Reconciliations, Ticketing Queue Management, RIA Audits, Automation."),
            ("Systems & Tools", "Salesforce, Schwab Advisor Center, Fidelity Wealthscape, DocuSign, Python, SQL, Excel.")
        ],
    },
    "b": {
        "pool_key": "track_b_engineering",
        "fallback_bullet": "Designed and scripted ETL pipelines and schema validation logic to automate high-volume data reconciliation.",
        "aliases": ("engineering", "data_systems", "data systems"),
        "prompt_label": "data/systems engineering",
        "subtitle": "Data & Systems Engineering",
        "keywords": ("Python", "SQL", "REST APIs", "ETL", "Schema Architecture", "Process Automation"),
        "summary": "I build Python and SQL tools that cut manual reporting work, including a pipeline that cleaned 1,500+ legacy account records.",
        "skills": [
            ("Engineering & Data", "Python, SQL, REST APIs, Webhook Integrations, SQLite WAL, Data Reconciliation."),
            ("Platforms & Stack", "Salesforce, HubSpot CRM, Flask, Typst, Schwab Advisor Center, Fidelity Wealthscape.")
        ],
    },
    "c": {
        "pool_key": "track_c_risk_compliance",
        "fallback_bullet": "Audited compliance documentation to enforce regulatory standards prior to execution.",
        "aliases": ("risk_compliance", "compliance", "risk"),
        "prompt_label": "risk & regulatory compliance",
        "subtitle": "Risk & Regulatory Compliance",
        "keywords": ("Regulatory Compliance", "SEC/FinCEN Filings", "Risk Management", "DocuSign", "Salesforce", "Audit Controls"),
        "summary": "I audit onboarding files across 500+ accounts and draft SEC Form D filings to catch compliance risks before execution.",
        "skills": [
            ("Compliance & Risk", "SEC & FinCEN Filings, Suitability Reviews, Custodial Exception Audits, Form D."),
            ("Systems & Controls", "Salesforce Queue Routing, DocuSign API, Schwab Advisor Center, Fidelity Wealthscape, Excel.")
        ],
    },
    "d": {
        "pool_key": "track_d_business_intelligence",
        "fallback_bullet": "Built reporting pipelines to translate raw operational data into executive insights.",
        "aliases": ("business_intelligence", "bi", "analytics"),
        "prompt_label": "business intelligence & analytics",
        "subtitle": "Business Intelligence & Analytics",
        "keywords": ("Power BI", "SQL", "Data Analytics", "Variance Analysis", "Reporting", "Excel"),
        "summary": "I write SQL and build Power BI dashboards that resolved $250k in ledger variances across institutional custody accounts.",
        "skills": [
            ("Analytics & Modeling", "SQL Aggregations, Variance Analysis, Power BI Dashboards, Advanced Excel Modeling."),
            ("Systems & Data", "Salesforce Reports, bSwift, Schwab Advisor Center, Fidelity Wealthscape, Python (pandas).")
        ],
    },
    "e": {
        "pool_key": "track_e_bizops",
        "fallback_bullet": "Automated routine data extraction and workflow tasks to reduce manual administrative overhead.",
        "aliases": ("bizops", "business_operations", "business operations", "crm"),
        "prompt_label": "business operations & CRM systems",
        "subtitle": "Business Operations & CRM Systems",
        "keywords": ("Business Operations", "Salesforce", "HubSpot CRM", "Process Automation", "Ticket Routing", "Python"),
        "summary": "I design Salesforce queues and DocuSign workflows that cut advisor packet review from 60 minutes to 20.",
        "skills": [
            ("Operations & Workflow", "Queue Routing Optimization, SLA Escalation Controls, CRM Pipeline Management, Process Design."),
            ("Systems & Tools", "Salesforce, HubSpot CRM, DocuSign, Schwab Advisor Center, Fidelity Wealthscape, Python.")
        ],
    },
    # --- Non-finance tracks. Their whole reason for existing is that a logistics, manufacturing or
    # internal-tools posting used to route to track a and get a custodial-accounts summary. They
    # claim queue/SLA/exception discipline, multi-site data controls and self-built automation -
    # never carrier, freight, warehouse, inventory or procurement work, none of which Kevin has.
    "f": {
        "pool_key": "track_f_operations_logistics",
        "fallback_bullet": "Worked intake queues against a 1 to 2 hour service window, clearing the documentation exceptions that stalled a handoff.",
        "aliases": ("operations_logistics", "logistics", "operations", "ops"),
        "prompt_label": "operations & logistics",
        "subtitle": "Operations & Service Delivery",
        "keywords": ("Operations", "SLA Management", "Exception Handling", "Salesforce", "Process Automation", "Excel"),
        "summary": "I work intake queues on a 1-to-2-hour turnaround, clearing exceptions before stalled handoffs leave anyone waiting.",
        "skills": [
            ("Operations & Service", "Queue & SLA Management, Exception Handling, Escalation Paths, Process Documentation."),
            ("Systems & Tools", "Salesforce, DocuSign, Excel, SQL, Python, HubSpot CRM.")
        ],
    },
    "g": {
        "pool_key": "track_g_supply_chain",
        "fallback_bullet": "Built audit checklists to cross-reference multi-site operational data, catching recurring errors across 70+ facilities.",
        "aliases": ("supply_chain", "supply chain", "multi_site", "multi-site"),
        "prompt_label": "supply chain & multi-site ops",
        "subtitle": "Multi-Site Operations & Data Controls",
        "keywords": ("Multi-Site Reconciliation", "Audit Controls", "Excel", "SQL", "Variance Analysis", "Recurring Reporting"),
        "summary": "I reconcile data across 70+ manufacturing sites and build the audit checklists that catch the errors nobody was flagging, so recurring reporting comes out right the first time.",
        "skills": [
            ("Controls & Reconciliation", "Multi-Site Data Reconciliation, Audit Checklist Design, Variance Analysis, Recurring Reporting."),
            ("Systems & Tools", "Excel, SQL, Power BI, Salesforce, Python, bSwift.")
        ],
    },
    "h": {
        "pool_key": "track_h_technical_systems",
        "fallback_bullet": "Automated recurring reconciliation and document assembly in Python, replacing work that had been done by hand.",
        "aliases": ("technical_systems", "technical", "automation", "systems"),
        "prompt_label": "technical systems & automation",
        "subtitle": "Operations Automation & Systems",
        # Deliberately not an engineering claim: main.build_system_prompt scores engineer-titled
        # reqs 1-24 on purpose, and the evidence says "operations person who automates his own
        # work", not "software engineer".
        "summary": "I automate the operations work I used to do by hand, building Python and SQLite tools that reconcile records and compile documents instead of assembling them one at a time.",
        "keywords": ("Process Automation", "Python", "SQL", "REST APIs", "Data Reconciliation", "SQLite"),
        "skills": [
            ("Automation & Data", "Process Automation, Data Reconciliation, Schema Validation, Webhook Integrations."),
            ("Systems & Tools", "Python, SQL, SQLite, Flask, Typst, REST APIs, Salesforce.")
        ],
    },
}

DEFAULT_TRACK = "a"

TRACK_LETTERS = tuple(TRACK_REGISTRY)

# ==============================================================================
# WHICH cold_ops EMAIL EACH TRACK IS ALLOWED TO SEND
#
# Gemini picks `track` and `outreach_template_id` INDEPENDENTLY, and nothing used to make them
# agree. Live sends on 2026-09-24: Rivian (Carrier Operations Analyst) shipped a track E resume
# under an email describing Kevin as a custodial-reconciliation person; so did Trinity Health
# (Strategic Sourcing) and GPAC (Treasury). Six of six sends that day described the wrong
# background for the role, whatever resume was attached.
#
# The email's own self-description is the constraint. cold_ops entries by the background they
# claim, in the order the pool ships:
#   [0] broker dealers and fiduciary services          finance
#   [1] custodial reconciliation                       finance   (the one that was over-selected)
#   [2] client intake and onboarding paperwork         finance
#   [3] operations and reporting, SQL                  neutral
#   [4] account transfers, documentation exceptions    finance
#   [5] brokerage and fiduciary services               finance
#   [6] operations, intake queues with a service window  NEUTRAL
#   [7] records and reporting, cleaning up data        NEUTRAL
#
# So f/g/h - the non-finance tracks - may only use 6 and 7, which are the only two entries that
# name no financial-services context. This is deterministic Python on purpose: a prompt
# instruction is exactly what already failed here.
#
# Index 0 of each list is the SNAP TARGET, used when Gemini picks something outside the set. It is
# the best email for that track, not merely a legal one.
ALLOWED_EMAIL_IDS_BY_TRACK = {
    "a": (0, 1, 5, 4),   # wealth ops - the custodial/fiduciary copy is genuinely his desk
    "b": (3, 7),         # data/systems - SQL and reporting, nothing client-facing
    "c": (4, 0, 1),      # risk & compliance - documentation exceptions lead
    "d": (3, 7),         # BI & analytics - reporting and data cleanup
    "e": (2, 3, 0),      # bizops & CRM - intake and onboarding paperwork IS the work
    "f": (6, 7),         # operations & logistics - queue/service-window copy only
    "g": (7, 6),         # supply chain & multi-site - records and reporting leads
    "h": (3, 7),         # technical systems - SQL and reporting
}

# response_schema caps outreach_template_id at le=7 and cold_ops ships 8 entries. Kept as the
# fallback for an unrecognized track so a new track added to the registry without an entry above
# degrades to "any email" rather than to track a's finance copy - the failure being fixed here.
_EVERY_COLD_OPS_ID = (0, 1, 2, 3, 4, 5, 6, 7)


def allowed_outreach_template_ids(track) -> tuple:
    """The cold_ops indices `track` may send, snap target first. Never empty."""
    return ALLOWED_EMAIL_IDS_BY_TRACK.get(normalize_track(track), _EVERY_COLD_OPS_ID)


def coerce_outreach_template_id(track, template_id) -> int:
    """Gemini's routed cold_ops id if this track is allowed to send it, else the track's default.

    Honors Gemini's pick whenever it is legal - it sees the job description and this does not, so
    its choice among a track's allowed entries is better than a fixed one. Only an illegal pick is
    overridden, which is the case that shipped a custodial-reconciliation email for a freight role.
    Total: any non-integer, out-of-range or unknown value lands on the track's default rather than
    raising, because this sits on the screening path where an exception costs the whole card.
    """
    allowed = allowed_outreach_template_ids(track)
    if isinstance(template_id, bool) or not isinstance(template_id, int):
        return allowed[0]
    return template_id if template_id in allowed else allowed[0]

# Maps track letters to resume_bullets_bank.json / cover_letter_templates.json pool names.
TRACK_BULLET_POOL_KEYS = {k: v["pool_key"] for k, v in TRACK_REGISTRY.items()}

# One safe bullet per pool, used only when resume_bullets_bank.json is missing or corrupt. Built
# from the registry so a new track cannot be forgotten here: a fallback bank missing f/g/h would
# resolve those tracks to track a's pool, which is the wealth-ops-resume-for-a-logistics-role bug
# the whole expansion exists to fix, reappearing only on the one path nobody tests by hand.
FALLBACK_BULLETS_BY_POOL = {v["pool_key"]: [v["fallback_bullet"]] for v in TRACK_REGISTRY.values()}

# Persona framing consumed by resume_engine.render_typst_markup().
TRACKS = {
    k: {field: v[field] for field in ("subtitle", "keywords", "summary", "skills")}
    for k, v in TRACK_REGISTRY.items()
}

# The a=..., b=... line interpolated into main.build_system_prompt()'s "track" instruction, so
# adding a track updates the prompt without a second edit.
TRACK_PROMPT_LINE = ", ".join(f"{k}={v['prompt_label']}" for k, v in TRACK_REGISTRY.items())

# Concrete triggers for the three non-finance tracks, injected into build_system_prompt().
#
# A bare list of eight labels was all the prompt gave, and the labels alone do not tell a model
# that "Carrier Operations Analyst" is f rather than e - "operations" appears in both. Six of six
# live sends on 2026-09-24 routed to a finance track, including a freight role at a truck
# manufacturer. There is a deterministic title override behind this (override_track_for_title), but
# the override only knows the title; the prompt is the only place the job DESCRIPTION gets read, so
# both exist. Kept to one line per track: a long prompt section did not survive last time.
TRACK_TRIGGER_GUIDANCE = """Picking the track: a, c and e are finance-framed and are for employers doing financial-services work. Do NOT default to them for an operations role at a non-finance employer - three tracks exist for that and were going unused:
- f (operations & logistics): carrier, freight, dispatch, fleet, transportation, service delivery, queue or SLA-driven request handling. A carrier/freight operations role at a manufacturer is f, never e.
- g (supply chain & multi-site ops): supply chain, procurement, strategic sourcing, plant or multi-site data, manufacturing reporting, warehouse or facility reconciliation.
- h (technical systems & automation): automation, systems administration, integrations, internal tooling, data pipelines owned by an operations team (NOT a software engineering req, which scores 1-24)."""

# Skills-footer entries that are process capabilities rather than named systems. Everything else in
# a footer must name a system banked in evidence_bank.json's technical_skills - that is the
# invariant test_skills_footers_only_name_banked_systems enforces, and it is what stops a
# non-finance track from quietly claiming TMS, WMS or SAP.
CAPABILITY_TERMS = frozenset({
    "Custodial Cashiering & Reconciliations", "Ticketing Queue Management", "RIA Audits", "Automation",
    "REST APIs", "Webhook Integrations", "Data Reconciliation",
    "SEC & FinCEN Filings", "Suitability Reviews", "Custodial Exception Audits", "Form D",
    "Variance Analysis",
    "Queue Routing Optimization", "SLA Escalation Controls", "CRM Pipeline Management", "Process Design",
    "Queue & SLA Management", "Exception Handling", "Escalation Paths", "Process Documentation",
    "Multi-Site Data Reconciliation", "Audit Checklist Design", "Recurring Reporting",
    "Process Automation", "Schema Validation",
})

# Track letters whose audience should never see crypto/Web3 phrasing regardless of tone_mode: a
# logistics or manufacturing reader reads it as unserious, not as risky. Decoupled from
# tone_mode="conservative", which is about the employer being a financial-services business.
CRYPTO_SCRUB_TRACKS = ("f", "g")


# ==============================================================================
# POST-HOC TRACK OVERRIDE FROM THE JOB TITLE
#
# Nothing validated Gemini's track against the job: it arrived and was persisted. Live evidence
# from 2026-09-24 - Rivian, a truck manufacturer, hiring a Carrier Operations Analyst, routed to
# track e (business operations & CRM). Track f exists for exactly that posting and was not used.
#
# Deliberately NARROW. These are title words that only appear on work Kevin has no finance framing
# for, and the tracks they select are the ones written for that work. A title word not listed here
# leaves Gemini's choice alone - it read the description and this has not.
_TITLE_TRACK_PATTERNS = (
    # Freight and service-delivery operations -> f. "dispatch" and "fleet" are unambiguous;
    # "transportation" appears on transit-agency and logistics reqs alike, both of which want f.
    (r"\b(?:carrier|freight|logistics|transportation|fleet|dispatch|last[ -]?mile)\b", "f"),
    # Multi-site / plant / supply-chain data work -> g. "sourcing" excludes TALENT sourcing, which
    # is recruiting: "Sourcing Specialist" at a staffing firm is a different job entirely, and
    # Kevin's screener forbids recruiting roles anyway.
    (r"\b(?:supply[ -]?chain|procurement|plant|manufacturing|manufactur\w*|warehouse)\b", "g"),
    (r"(?<!talent )(?<!technical )\bstrategic sourcing\b|(?<!talent )\bsourcing\b(?! specialist)", "g"),
    # Automation / internal-systems work -> h, the "operations person who automates his own work"
    # framing. Sits BEFORE d on purpose: "RPA Analyst" and "Automation & Reporting Analyst" are h,
    # not d. Engineer-titled reqs never get here as a win - they are scored 1-24 regardless.
    (r"\b(?:rpa|automation|systems admin\w*|integration\w*|workflow|low[- ]?code|power automate|internal tool\w*)\b", "h"),
    # BI / reporting analysis -> d.
    (r"\b(?:business intelligence|analytics|power bi|reporting analyst|data analyst)\b", "d"),
    # No rule selects b (Data & Systems Engineering). An etl/pipeline/data-engineer pattern would
    # push engineer-titled reqs onto an engineering-framed resume, which is exactly what the 1-24
    # title rule in the screener prompt forbids. b stays reachable only from Gemini reading the JD.
)

_TITLE_TRACK_RULES = tuple(
    (re.compile(p, re.IGNORECASE), letter) for p, letter in _TITLE_TRACK_PATTERNS
)

# A title word never outranks the employer's own industry. "Strategic Sourcing Analyst" at a bank
# is procurement AT A BANK, and a multi-site manufacturing resume is the wrong document for it -
# overriding there would trade one misroute for another. Two independent signals, because either
# alone has a live failure mode: tone_mode is Gemini's read and can be wrong, and a name check
# cannot see a financial-services firm whose name says nothing (Signal Advisors aside, "GPAC"
# says nothing either).
_FINANCE_EMPLOYER_RE = re.compile(
    r"\b(?:bank|banking|bancorp|bancshares|credit union|savings|trust|fiduciar\w+|custodian|custody|"
    r"wealth|advisor\w*|advisory|asset management|investment\w*|securities|brokerage|broker[ -]?dealer|"
    r"insurance|assurance|underwrit\w+|reinsurance|mutual|annuit\w+|capital|equity|"
    r"financial|finserv|fintech|mortgage|lending|loans|payments|treasury services)\b",
    re.IGNORECASE,
)

# These tracks all describe Kevin through a financial-services lens, so a non-finance title is
# evidence the router landed on the wrong one. f/g/h are already non-finance: Gemini's choice
# among them is finer-grained than a title regex, so those are never overridden.
_FINANCE_FRAMED_TRACKS = ("a", "b", "c", "d", "e")


def employer_is_financial_services(employer) -> bool:
    """True when the employer's NAME says its own business is financial services.

    Name-based and therefore incomplete by construction - it is one of two guards, not the whole
    test. The asymmetry is deliberate: a false positive here only means "do not override", which
    is today's behavior, while a false negative is caught by the tone_mode check beside it.
    """
    return bool(_FINANCE_EMPLOYER_RE.search(str(employer or "")))


def override_track_for_title(track, job_title, employer=None, tone_mode=None) -> str:
    """`track`, or the track a high-confidence non-finance job TITLE demands instead.

    Returns the input unchanged unless all of these hold: the title matches one of the narrow
    patterns above, the current track is one of the finance-framed ones, and the employer is not
    itself a financial-services business. Callers log every change - see evaluate_job_with_gemini.
    """
    letter = normalize_track(track)
    if letter not in _FINANCE_FRAMED_TRACKS:
        return letter
    if tone_mode == "conservative" or employer_is_financial_services(employer):
        return letter
    title = str(job_title or "")
    for pattern, target in _TITLE_TRACK_RULES:
        if pattern.search(title):
            return target
    return letter


def normalize_track(value) -> str:
    """Any Gemini/Telegram-supplied track value coerced to a letter this registry can resolve.

    Never raises and never returns something pool_key_for() cannot handle: unknown input degrades
    to DEFAULT_TRACK. That is why response_schema stopped using a Literal - with one, Gemini
    returning "f " or "logistics" raised a ValidationError and cost the whole card, rather than
    costing one routing decision.
    """
    text = str(value or "").strip().lower()
    if text in TRACK_REGISTRY:
        return text
    for letter, data in TRACK_REGISTRY.items():
        if text == data["pool_key"] or text in data["aliases"]:
            return letter
    return DEFAULT_TRACK


def pool_key_for(track) -> str:
    """The resume_bullets_bank.json / cover_letter_templates.json pool name for a track value."""
    return TRACK_BULLET_POOL_KEYS[normalize_track(track)]
