"""
resume_engine.py
================
High-Performance In-Memory Resume Compiler for Kevin Miller.
Translates structured profile data and Gemini ATS bullets into an ATS-compliant PDF via Typst (<30ms).
"""

import io
import re
import typst

def escape_typst(text: str) -> str:
    """
    Escapes Typst markup reserved characters to prevent compilation syntax exceptions.
    Order is critical: backslashes must be escaped before structural syntax symbols.
    """
    if not text:
        return ""
    clean = str(text).replace("\\", "\\\\")
    for char in ["#", "$", "[", "]", "*", "_", "<", ">", "@"]:
        clean = clean.replace(char, f"\\{char}")
    return clean

# Structured dual-persona profile data. Track A emphasizes wealth ops/custody/reconciliation;
# Track B emphasizes Python/SQL/REST APIs/ETL/schema architecture. Job history is factual/shared,
# only the subtitle, default highlight bullets, and skills section differ per track.
TRACKS = {
    "a": {
        "subtitle": "Financial Systems & Operations",
        "keywords": ("Wealth Operations", "Process Automation", "Python", "SQL", "Salesforce", "Reconciliation"),
        "default_bullets": [
            "Reconciled high-volume data variances and mapped ownership structures to establish risk escalation logic.",
            "Automated data extraction and operational compliance workflows using Python and structured API schemas."
        ],
        "skills": [
            ("Operations & Data", "High-Volume Reconciliation, Variance Analysis, Audit Escalation, Power BI (ETL/Modeling), SQL, Advanced Excel."),
            ("Systems & Tools", "HubSpot CRM, Schwab Advisor Center, REST APIs, JSON Intake, LLM Orchestration & Prompt Engineering.")
        ]
    },
    "b": {
        "subtitle": "Data & Systems Engineering",
        "keywords": ("Python", "SQL", "REST APIs", "ETL", "Schema Architecture", "Process Automation"),
        "default_bullets": [
            "Designed and scripted ETL pipelines and schema validation logic to automate high-volume data reconciliation.",
            "Built REST API integrations and structured data intake schemas to replace manual compliance workflows."
        ],
        "skills": [
            ("Engineering & Data", "Python, SQL, REST API Integration, ETL Modeling & Schema Design, Process Scripting, Power BI."),
            ("Systems & Tools", "HubSpot CRM API, Schwab Advisor Center, JSON Schema Intake, LLM Orchestration & Prompt Engineering.")
        ]
    }
}

def render_typst_markup(company_name: str, dynamic_bullets: list, track: str = "a") -> str:
    """Builds Typst markup for the selected persona track with verified baseline history, embedded metadata, and tailored role bullets."""
    track_data = TRACKS.get(str(track or "a").lower(), TRACKS["a"])
    clean_company = escape_typst(company_name or "Target Operations")

    if not dynamic_bullets:
        bullet_lines = [f"  - {b}" for b in track_data["default_bullets"]]
    else:
        bullet_lines = []
        for b in dynamic_bullets[:2]:
            clean_b = re.sub(r'^[•\-\*]\s*', '', str(b).strip())
            bullet_lines.append(f"  - {escape_typst(clean_b)}")
    dynamic_bullets_block = "\n".join(bullet_lines)

    skills_block = "\n".join(f"- *{cat}:* {desc}" for cat, desc in track_data["skills"])
    keywords_tuple = ", ".join(f'"{kw}"' for kw in track_data["keywords"])

    markup = f"""
#set document(
  title: "Kevin Miller - Resume - {clean_company}",
  author: "Kevin Miller",
  date: auto,
  keywords: ({keywords_tuple})
)

#set page(paper: "us-letter", margin: (x: 0.55in, top: 0.45in, bottom: 0.45in))
#set text(font: "Liberation Sans", size: 9.5pt)
#set par(justify: false, leading: 0.52em)

#align(center)[
  #text(size: 15pt, weight: "bold")[Kevin Miller] \\
  #text(size: 10pt, weight: "medium", fill: rgb("#1B2A4A"))[{track_data["subtitle"]}] \\
  #v(2pt)
  kjmiller406\@gmail.com | 248-709-6326 | Detroit, MI | montelattice.com | [linkedin.com/in/kevinmiller](https://linkedin.com/in/kevinmiller)
]

#v(4pt)
#line(length: 100%, stroke: 0.6pt + rgb("#CCCCCC"))
#v(2pt)

== Professional Experience
*Compliance Lead* | 40 Acres App #h(1fr) Detroit, MI (04/2026 -- Present)
- Architect regulatory infrastructure for blockchain-based real estate and real-world asset (RWA) tokenization.
- Draft SEC Form D filings and Regulation Crowdfunding documentation for SEC and FinCEN compliance.
- Design operational plumbing for digital asset ownership and institutional data validation.

*Total Rewards Finance Intern* | ABC Technologies #h(1fr) Southfield, MI (05/2024 -- 08/2024)
- Performed high-volume reconciliation of 500+ retirement accounts, validating ledger accuracy and resolving discrepancies.
- Prepared and maintained supporting schedules for variance analysis and recurring internal audit reports.
- Built and automated Excel models to streamline data intake workflows and cut manual processing cycles.

== Targeted Systems & Project Highlights ({clean_company})
{dynamic_bullets_block}
- *Institutional Data Controls:* Reconciled \\$250k in ledger variances; automated AI extraction and compliance validation for legal workflows.
- *Regulatory Translation:* Built operational onboarding workflows bridging legal compliance with structured due diligence.

== Education & Certifications
*Hope College* | B.A. Business (Finance), B.A. Political Science #h(1fr) (08/2022 -- 05/2026)
- *Licenses & Credentials:* Series 65 Candidate, Securities Industry Essentials (SIE), Schwab Limited Power of Attorney (LPOA), Bloomberg Market Concepts (BMC).

== Technical Systems & Core Skills
{skills_block}
"""
    return markup.strip()

def compile_resume_pdf(company_name: str, dynamic_bullets: list, track: str = "a") -> bytes:
    """Compiles the Typst markup string directly into PDF bytes in memory for the selected persona track."""
    markup = render_typst_markup(company_name, dynamic_bullets, track)
    return typst.compile(markup.encode("utf-8"))
