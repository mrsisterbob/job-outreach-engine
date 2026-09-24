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
        "summary": "I work intake queues with a service window attached, resolving requests inside 1 to 2 hours and clearing the exceptions that stall a handoff before the person on the other end is waiting.",
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
