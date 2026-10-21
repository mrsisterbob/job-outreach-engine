"""Region and language gate for /w (warm radar). Pure logic, no I/O.

/w used to apply no location rule at all ("a warm contact at a company is a reason to look at a
role anywhere that company hires"), which was safe at ~45 companies and not at 1,100 contacts: one
firm's whole worldwide board arrived as 56 Telegram cards. This module is the one exception to
"no location gate": it removes roles that are CLEARLY outside the US or in a language Kevin does not
work in, and keeps everything else, including every role whose location it cannot read.

The ATS fetchers put the free-text location in job["job_city"] ("Germany", "Japan; Tokyo",
"Chicago; Dallas; Detroit; New York", "Washington, DC") and set job["job_is_remote"] when the text
says remote. job_state is empty for all three boards, so nothing here reads it.

Public surface:
  job_region_verdict(job)        -> ("keep" | "foreign" | "unknown", reason)
  title_looks_non_english(title) -> bool
  role_region_gate(job)          -> ("keep" | "foreign" | "unknown" | "non_english", reason)
  role_rank_key(...), company_order_key(...)  deterministic ordering used by the /w send loop
"""
import re
import unicodedata

KEEP = "keep"
FOREIGN = "foreign"
UNKNOWN = "unknown"
NON_ENGLISH = "non_english"

# A multi-location string is split on these into parts; a part is judged on its own.
LOCATION_SPLIT_RE = re.compile(r"[;|/]")

US_STATE_CODES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
    "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split())

# Checked FIRST, before any foreign name, so "Paris, TX", "London, KY" and "Dublin, OH" are US.
_US_WORD_RE = re.compile(r"\bunited states\b|\busa\b|\bU\.S\.", re.IGNORECASE)
_US_UPPER_ONLY_RE = re.compile(r"\bUS\b")   # case-sensitive: a lower-case "us" is just a word
_US_STATE_SUFFIX_RE = re.compile(r",\s*([A-Z]{2})\b")
# State codes that are ALSO ISO country codes of places in FOREIGN_NAMES below: DE Germany/Delaware,
# IN India/Indiana, CO Colombia/Colorado, CA Canada/California, IL Israel, ID Indonesia, MA Morocco,
# PA Panama, AR Argentina (AL: Albania). For these the foreign-name check wins when the text before
# the comma is a foreign city or country ("Hamburg, DE", "Toronto, CA"); every other state code stays
# US-first, so "Paris, TX" and "London, KY" are US, as are "Denver, CO" and "Wilmington, DE".
AMBIGUOUS_STATE_CODES = frozenset("DE IN CO CA IL ID MA PA AR AL".split())
# Full state names after the last comma ("Paris, Texas"). Georgia is left out: Tbilisi, Georgia.
US_STATE_NAMES = frozenset("""
alabama,alaska,arizona,arkansas,california,colorado,connecticut,delaware,florida,hawaii,idaho,
illinois,indiana,iowa,kansas,kentucky,louisiana,maine,maryland,massachusetts,michigan,minnesota,
mississippi,missouri,montana,nebraska,nevada,new hampshire,new jersey,new mexico,new york,
north carolina,north dakota,ohio,oklahoma,oregon,pennsylvania,rhode island,south carolina,
south dakota,tennessee,texas,utah,vermont,virginia,washington,west virginia,wisconsin,wyoming,
district of columbia
""".replace("\n", "").split(","))
_US_DC_RE = re.compile(r",\s*D\.C\b", re.IGNORECASE)

# Bare US city names (the live boards often give "Detroit" or "New York" with no state). Checked
# after the foreign set, so a city in both would be foreign: keep the two sets disjoint.
US_CITIES = frozenset("""
new york,chicago,dallas,detroit,boston,los angeles,san francisco,houston,atlanta,seattle,austin,
denver,miami,philadelphia,washington,san diego,san jose,phoenix,minneapolis,charlotte,pittsburgh,
cleveland,nashville,portland,baltimore,st. louis,saint louis,salt lake city,columbus,cincinnati,
kansas city,indianapolis,las vegas,orlando,tampa,raleigh,stamford,brooklyn,oakland,sacramento,
milwaukee,richmond,omaha,louisville,memphis,new orleans,jacksonville,san antonio,fort worth,
boulder,palo alto,mountain view,menlo park,irvine,cambridge,southfield,troy,ann arbor,
farmington hills,grand rapids,lansing,princeton,newark,hartford,providence,greenwich
""".replace("\n", "").split(","))

# Countries, regions and big foreign cities. Ambiguous names that are also US places (Georgia,
# Birmingham, Manchester, Jordan) are left out on purpose: an unreadable location fails OPEN.
FOREIGN_NAMES = tuple(sorted({
    # countries
    "germany", "france", "italy", "spain", "portugal", "netherlands", "belgium", "switzerland",
    "austria", "sweden", "norway", "denmark", "finland", "ireland", "united kingdom", "uk",
    "england", "scotland", "wales", "poland", "czech republic", "czechia", "hungary", "romania",
    "bulgaria", "greece", "turkey", "ukraine", "russia", "serbia", "croatia", "slovakia",
    "slovenia", "lithuania", "latvia", "estonia", "luxembourg", "israel", "saudi arabia",
    "united arab emirates", "uae", "qatar", "kuwait", "bahrain", "oman", "egypt", "south africa",
    "nigeria", "kenya", "morocco", "india", "china", "japan", "south korea", "korea", "taiwan",
    "hong kong", "singapore", "malaysia", "thailand", "vietnam", "indonesia", "philippines",
    "pakistan", "bangladesh", "australia", "new zealand", "canada", "mexico", "brazil",
    "argentina", "chile", "colombia", "peru", "uruguay", "costa rica", "panama",
    # regions
    "emea", "apac", "latam", "europe", "asia", "middle east", "mena", "dach", "anz", "africa",
    "latin america", "asia pacific", "southeast asia",
    # cities
    "london", "paris", "berlin", "munich", "frankfurt", "dusseldorf", "hamburg", "cologne",
    "stuttgart", "milan", "rome", "madrid", "barcelona", "lisbon", "amsterdam", "brussels",
    "zurich", "geneva", "vienna", "stockholm", "oslo", "copenhagen", "helsinki", "dublin",
    "warsaw", "prague", "budapest", "bucharest", "athens", "istanbul", "tel aviv", "dubai",
    "abu dhabi", "riyadh", "doha", "cairo", "johannesburg", "cape town", "lagos", "nairobi",
    "mumbai", "delhi", "new delhi", "bangalore", "bengaluru", "hyderabad", "chennai", "pune",
    "gurgaon", "gurugram", "shanghai", "beijing", "shenzhen", "guangzhou", "tokyo", "osaka",
    "seoul", "taipei", "kuala lumpur", "bangkok", "jakarta", "manila", "ho chi minh", "hanoi",
    "sydney", "melbourne", "brisbane", "perth", "auckland", "toronto", "vancouver", "montreal",
    "ottawa", "calgary", "mexico city", "monterrey", "guadalajara", "sao paulo",
    "rio de janeiro", "buenos aires", "santiago", "bogota", "lima", "medellin", "munchen",
    "edinburgh", "glasgow", "lyon", "marseille", "rotterdam", "antwerp", "basel",
}, key=lambda s: (-len(s), s)))
_FOREIGN_RE = re.compile(
    r"(?<!new )\b(?:" + "|".join(re.escape(n) for n in FOREIGN_NAMES) + r")\b")
_REMOTE_RE = re.compile(r"\bremote\b", re.IGNORECASE)

# Non-English title cues. A cue alone marks the role non-English whatever the location says.
_GENDER_MARKER_RE = re.compile(
    # not when another "/<letter>" follows: "(M/F/D/V)" is a US EEO suffix, not "(m/w/d)"
    r"\(?\s*\b[mwfd]\s*/\s*[mwfd]\s*/\s*[mwfd]\b(?!\s*/\s*[a-wyz0-9])\s*\)?|\b\w+:in(?:nen)?\b|\b\w+\*in\b",
    re.IGNORECASE)
NON_ENGLISH_TITLE_WORDS = frozenset("""
werkstudent,werkstudentin,praktikum,praktikant,praktikantin,berater,beraterin,mitarbeiter,
mitarbeiterin,ingenieur,ingenieurin,sachbearbeiter,sachbearbeiterin,referent,referentin,
projektleiter,projektleiterin,ausbildung,abschlussarbeit,stagiaire,alternance,alternant,
apprenti,responsable,gestionnaire,conseiller,conseillere,directeur,directrice,chargé,
pasante,practicante,gerente,analista,consultor,coordinador
""".replace("\n", "").split(","))
_TITLE_WORD_RE = re.compile(r"\b(" + "|".join(sorted(NON_ENGLISH_TITLE_WORDS, key=lambda s: -len(s))) + r")\b",
                            re.IGNORECASE)
NON_ASCII_LETTERS_FOR_NON_ENGLISH = 3


def _fold(text):
    """Lower-case with accents removed ("Düsseldorf" -> "dusseldorf")."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).lower()


def _classify_part(part):
    """('us' | 'foreign' | 'remote' | 'unknown', reason) for ONE location part."""
    raw = part.strip()
    if not raw:
        return "unknown", "empty part"
    # 1. US markers first.
    if _US_WORD_RE.search(raw) or _US_UPPER_ONLY_RE.search(raw):
        return "us", f"US marker in {raw!r}"
    for suffix in _US_STATE_SUFFIX_RE.finditer(raw):
        code = suffix.group(1)
        if code not in US_STATE_CODES:
            continue
        if code in AMBIGUOUS_STATE_CODES:
            before = _FOREIGN_RE.search(_fold(raw[:suffix.start()]))
            if before:
                return "foreign", f"{before.group(0)} (country code {code})"
        return "us", f"state code {code}"
    if _US_DC_RE.search(raw):
        return "us", "state code DC"
    if "," in raw and _fold(raw.rsplit(",", 1)[1]).strip(" .") in US_STATE_NAMES:
        return "us", f"state name {raw.rsplit(',', 1)[1].strip()!r}"
    folded = _fold(raw)
    # 2. A named foreign place ("Remote - Germany" is foreign: a foreign place is named).
    hit = _FOREIGN_RE.search(folded)
    if hit:
        return "foreign", hit.group(0)
    # 3. Remote with no foreign place named.
    if _REMOTE_RE.search(raw):
        return "remote", "remote"
    # 4. A bare US city.
    stripped = folded.strip(" .-")
    if stripped in US_CITIES or any(stripped.startswith(c + " ") or stripped.startswith(c + ",") for c in US_CITIES):
        return "us", f"US city {stripped!r}"
    return "unknown", f"unrecognised {raw!r}"


def job_region_verdict(job):
    """('keep' | 'foreign' | 'unknown', reason) from job["job_city"] and job["job_is_remote"].

    'foreign' only when EVERY part of the location is a foreign place. Any US, remote or unknown
    part keeps the role; 'keep' means a US or remote part was positively read, 'unknown' means
    nothing could be read (empty, or only unrecognised/foreign-mixed text). Unknown is KEPT by the
    caller (fail open) and counted separately.
    """
    job = job or {}
    location = str(job.get("job_city") or "").strip()
    state = str(job.get("job_state") or "").strip()
    if state and state.upper() in US_STATE_CODES and location:
        return KEEP, f"state {state.upper()}"
    if not location:
        if job.get("job_is_remote"):
            return KEEP, "remote flag, no location text"
        return UNKNOWN, "no location"
    parts = [p for p in LOCATION_SPLIT_RE.split(location) if p.strip()]
    if not parts:
        return UNKNOWN, "no location"
    kinds = [_classify_part(p) for p in parts]
    if all(k == "foreign" for k, _ in kinds):
        return FOREIGN, "; ".join(r for _, r in kinds)
    for kind, reason in kinds:
        if kind in ("us", "remote"):
            return KEEP, reason
    return UNKNOWN, kinds[0][1]


def title_looks_non_english(title):
    """True when the title carries a German/French/Spanish cue: a gender marker such as (m/w/d) or
    the ':in' suffix, a word from NON_ENGLISH_TITLE_WORDS, or 3+ non-ASCII letters. One accent in
    an otherwise English title (a name, a place) is not enough."""
    title = str(title or "")
    if not title:
        return False
    if _GENDER_MARKER_RE.search(title) or _TITLE_WORD_RE.search(title):
        return True
    return sum(1 for c in title if ord(c) > 127 and c.isalpha()) >= NON_ASCII_LETTERS_FOR_NON_ENGLISH


def role_region_gate(job):
    """The /w gate in one call. A non-English title wins even when the location is unknown.
    Returns ('non_english' | 'foreign' | 'unknown' | 'keep', reason)."""
    job = job or {}
    if title_looks_non_english(job.get("job_title")):
        return NON_ENGLISH, "non-English title"
    return job_region_verdict(job)


def role_rank_key(track_hit, verdict, board_index):
    """Sort key inside one company: roles a track rule fires on first, then a positively read US or
    remote location over an unknown one, then the board's own order."""
    return (0 if track_hit else 1, 0 if verdict == KEEP else 1, board_index)


def company_order_key(priority_score, role_count, name):
    """Sort key for which companies get a message when the run cap bites: contact priority
    descending, then more surviving roles, then name."""
    try:
        priority = float(priority_score)
    except (TypeError, ValueError):
        priority = 0.0
    return (-priority, -int(role_count), str(name or "").lower())
