"""Report on the biggest warm companies (10+ warm contacts): which have a scannable job board.

Pure logic, no I/O. "scanned" means the company is in /w's scanned bucket (a cached ATS board or
Workday triple exists). It does NOT say the board was fetched successfully on this run, or when.
"""
import html

BIG_COMPANY_MIN_CONTACTS = 10
MAX_LINES = 12

_WORDING = {
    "scanned": "board scanned",
    "no_board": "no board found",
    "unchecked": "not looked up yet",
    "not_company": "not a company name",
}


def big_company_report(rows):
    """rows: (display_name, contact_count, status), status in scanned / no_board / unchecked /
    not_company. Returns the closing-message lines, [] when no company has 10+ contacts."""
    big = [(str(n), int(c), s) for n, c, s in rows if int(c) >= BIG_COMPANY_MIN_CONTACTS]
    if not big:
        return []
    big.sort(key=lambda r: (-r[1], r[0].lower()))
    total = sum(c for _, c, _ in big)
    have = sum(c for _, c, s in big if s == "scanned")
    none = sum(c for _, c, s in big if s == "no_board")
    unlooked = sum(c for _, c, s in big if s == "unchecked")
    lines = [f"Biggest warm companies ({BIG_COMPANY_MIN_CONTACTS}+ contacts): {total}. "
             f"{have} have a board, {none} have none, {unlooked} not looked up yet."]
    for name, count, status in big[:MAX_LINES]:
        lines.append(f"• {html.escape(name)} ({count}) - {_WORDING.get(status, status)}")
    if len(big) > MAX_LINES:
        lines.append(f"and {len(big) - MAX_LINES} more")
    return lines
