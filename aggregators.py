"""College aggregator helpers — CollegeDunia profile lookup + parsing.

CollegeDunia profile pages expose a structured JSON-LD block of type
"CollegeOrUniversity" with email, telephone, address and official URL. We
locate the profile URL via a DuckDuckGo `site:collegedunia.com ...` search,
then fetch and parse it.

This module intentionally has no dependency on main.py so it can be reused
by other scripts.
"""
import html
import json
import re
import time
import warnings

import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

# Many Indian college/university sites have expired or self-signed SSL certs,
# or use Indian CAs not in the default trust store. We disable verification
# rather than dropping those sites entirely — these are public sites and we
# only read public data. urllib3 emits a warning each time; mute it.
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

warnings.filterwarnings('ignore', category=RuntimeWarning)

try:
    from ddgs import DDGS  # renamed package
except ImportError:  # fall back to the old name if user has the old install
    from duckduckgo_search import DDGS

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://collegedunia.com/',
}

# CollegeDunia uses several profile-path prefixes: /college/, /university/, /institute/
_PROFILE_RE = re.compile(
    r'collegedunia\.com/(?:college|university|institute)/\d+-[a-z0-9\-]+',
    re.I,
)


def find_collegedunia_url(name, state, max_results=8):
    """Use DDG to find this college's CollegeDunia profile URL. Returns None if not found."""
    if not name:
        return None
    queries = [
        f'site:collegedunia.com {name} {state}',
        f'site:collegedunia.com {name}',
        f'collegedunia.com {name} {state}',
    ]
    for q in queries:
        try:
            with DDGS() as ddgs:
                for r in ddgs.text(q, max_results=max_results):
                    href = r.get('href', '')
                    m = _PROFILE_RE.search(href)
                    if m:
                        return 'https://' + m.group(0).lower()
        except Exception:
            continue
    return None


# Match "Principal Dr. B.N. Barve", "Director Prof. R. K. Sharma", etc.
# The honorific (Dr./Prof./Mr./Mrs./Ms./Shri/Smt.) is REQUIRED — otherwise we
# would match noise like "Directorate of Technical Education..." or "Director
# General of Police...". Word boundaries on the role keyword prevent matching
# inside longer words like "Directorate".
# Words that often appear right after a name (e.g. "Dr. S.C. Sharma HOD, Computer Sci.")
# — stop the name capture before them so we don't pull in the role/department.
_POC_STOPWORDS = (
    # Honorifics — stops a name capture from absorbing the *next* doctor's
    # honorific when two are listed in a row ("...Banerjee Dr. Sanjay..."):
    'Dr|Prof|Mr|Mrs|Ms|Shri|Smt|'
    # Roles / titles that follow a name and aren't part of it.
    'HOD|Department|Dept|Faculty|Professor|Principal|Director|Dean|'
    'Vice|Sr|Jr|Head|Member|Founder|Chairman|Manager|Officer|Registrar|'
    'Secretary|Coordinator|Convenor|MD|CEO|CTO|Adviser|Assistant|'
    'Associate|Lecturer|Reader|Senior|Junior|Visiting|Adjunct|Emeritus|'
    # Month abbreviations near a "joined on Aug 2024" sentence.
    'Aug|Jul|Jun|May|Apr|Mar|Feb|Jan|Sep|Oct|Nov|Dec'
)
_POC_PATTERNS = [
    re.compile(
        r'\b(?:Principal|Director|Vice[\s-]?Chancellor|'
        r'Head\s+of\s+Institution|Dean|Chairperson|Chairman)\s*[:\-,]?\s+'
        r'(?:Dr\.?|Prof\.?|Mr\.?|Mrs\.?|Ms\.?|Shri\.?|Smt\.?)\s+'
        # A name token is either an initial with a MANDATORY period
        # (so "S" at the start of "Sharma" doesn't get treated as an initial),
        # or a word of length >= 2 starting with a capital. Up to 5 tokens
        # total, with a negative lookahead to stop at role/department words.
        rf"((?:[A-Z]\.|[A-Z][A-Za-z']+)"
        rf"(?:\s+(?!(?:{_POC_STOPWORDS})\b)(?:[A-Z]\.|[A-Z][A-Za-z']+)){{0,4}})",
        re.I,
    ),
]

_STRENGTH_PATTERNS = [
    re.compile(r"(?:Student'?s?\s+)?Strength\s*[:\-]?\s*(\d{2,6})", re.I),
    re.compile(r"Total\s+Students?\s*[:\-]?\s*(\d{2,6})", re.I),
    re.compile(r"(?:Total\s+)?Enroll?ment\s*[:\-]?\s*(\d{2,6})", re.I),
    re.compile(r"Students?\s+Enrolled\s*[:\-]?\s*(\d{2,6})", re.I),
    re.compile(r"(\d{2,6})\s+(?:students|enrolled)", re.I),
]


def _extract_strength(text):
    """First plausible strength number from the text. Caps at 500,000 to
    reject mismatched stats like phone digits or graduation years."""
    for pat in _STRENGTH_PATTERNS:
        for m in pat.finditer(text):
            try:
                n = int(m.group(1))
            except (ValueError, TypeError):
                continue
            if 10 <= n <= 500_000:
                return str(n)
    return None


def infer_district_from_location(location_text, known_districts=None):
    """Pick a district/city name out of a free-form address.

    If `known_districts` is provided (a set of lowercase district names, e.g.
    built from AICTE data), we look for any of those names appearing as a
    token in the address — far more reliable than picking the last comma-
    separated chunk, which often catches landmarks like "Near IDBI Bank ATM".

    Without a vocabulary, we fall back to the last meaningful chunk before
    "India" (and reject obvious landmark phrases).
    """
    if not location_text:
        return None
    s = re.sub(r'\s+', ' ', location_text).strip()
    s = re.sub(r'\bIndia\b\.?\s*$', '', s, flags=re.I).strip(' ,.')
    if not s:
        return None

    if known_districts:
        # Tokenise the address and look for a single-word district match.
        tokens = re.findall(r"[A-Za-z][A-Za-z'-]+", s)
        # Walk right-to-left because the district is usually near the end.
        for tok in reversed(tokens):
            if tok.lower() in known_districts:
                return tok.title()
        # Two-word districts (e.g. "Jammu Kashmir", "New Delhi").
        for i in range(len(tokens) - 1, 0, -1):
            bigram = f'{tokens[i - 1].lower()} {tokens[i].lower()}'
            if bigram in known_districts:
                return f'{tokens[i - 1].title()} {tokens[i].title()}'

    # Heuristic fallback: last comma chunk before "India".
    parts = [p.strip(' .,-') for p in s.split(',') if p.strip(' .,-')]
    if not parts:
        return None
    last = parts[-1]
    if re.fullmatch(r'\d{3,8}', last) and len(parts) >= 2:
        last = parts[-2]
    last = re.sub(r'\s*\b\d{6}\b\s*$', '', last).strip(' .,-')
    # Reject "Near X" / "Opp Y" / etc. landmark phrases.
    if re.match(r'^(near|opp|opposite|behind|next\s+to)\b', last, re.I):
        return None
    if len(last) < 2 or last.lower() in {'india', 'road', 'highway', 'campus', 'street'}:
        return None
    return last.title()


def parse_collegedunia_profile(url, session=None, timeout=20):
    """Fetch the profile and pull whatever fields it has. Returns a dict (possibly partial)."""
    s = session or requests
    try:
        r = s.get(url, headers=_HEADERS, timeout=timeout)
    except requests.RequestException as e:
        return {'_error': str(e)}
    if r.status_code != 200:
        return {'_error': f'HTTP {r.status_code}'}

    soup = BeautifulSoup(r.text, 'html.parser')
    out = {}

    # JSON-LD CollegeOrUniversity block (email/phone/address/official URL)
    for blk in soup.find_all('script', type='application/ld+json'):
        raw = blk.string or ''
        try:
            data = json.loads(raw)
        except Exception:
            continue
        # Some pages wrap the LD object in a list
        if isinstance(data, list):
            objs = data
        else:
            objs = [data]
        for obj in objs:
            if not isinstance(obj, dict):
                continue
            if obj.get('@type') == 'CollegeOrUniversity':
                if obj.get('email'):
                    out['Email ID'] = obj['email']
                if obj.get('telephone'):
                    out['Phone Number'] = obj['telephone']
                if obj.get('url'):
                    out['Official URL'] = obj['url']
                addr = obj.get('address') or {}
                if isinstance(addr, dict):
                    street = addr.get('streetAddress')
                    if street:
                        out['Location'] = street
                    # CollegeDunia stores city in addressLocality
                    if addr.get('addressLocality'):
                        out.setdefault('District', addr['addressLocality'])
                if obj.get('name'):
                    out['_matched_name'] = obj['name']

    # Principal / Director from the page text
    text = soup.get_text(separator=' ', strip=True)
    for pat in _POC_PATTERNS:
        m = pat.search(text)
        if m:
            out['POC Name'] = m.group(1).strip()
            break

    # Student strength
    strength = _extract_strength(text)
    if strength:
        out['Approx Strength'] = strength

    return out


# ---------------------------------------------------------------------------
# Administrative contact extraction (role + name + email triplet).
# ---------------------------------------------------------------------------

# Roles in descending priority — the first plausible (name, email) pair we
# find for the highest-priority role wins.
_ADMIN_ROLES = [
    ('Director',          re.compile(r'\bDirector(?!ate|\s+of\s+(?:Technical|Higher))', re.I)),
    ('Vice Chancellor',   re.compile(r'\bVice[\s-]?Chancellor\b', re.I)),
    ('Principal',         re.compile(r'\bPrincipal(?!s\b)', re.I)),
    ('Head of Institution', re.compile(r'\bHead\s+of\s+(?:the\s+)?Institution\b', re.I)),
    ('Dean',              re.compile(r'\bDean(?!s\b)\b', re.I)),
    ('Registrar',         re.compile(r'\bRegistrar\b', re.I)),
    ('HOD',               re.compile(r'\b(?:HOD|Head\s+of\s+(?:the\s+)?Department)\b', re.I)),
]

_EMAIL_RE_GLOBAL = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

# Name with mandatory honorific; max 5 name tokens; same stoplist as POC patterns.
_NAME_AFTER_HONORIFIC_RE = re.compile(
    rf"(?:Dr\.?|Prof\.?|Mr\.?|Mrs\.?|Ms\.?|Shri\.?|Smt\.?)\s+"
    rf"((?:[A-Z]\.|[A-Z][A-Za-z']+)"
    rf"(?:\s+(?!(?:{_POC_STOPWORDS})\b)(?:[A-Z]\.|[A-Z][A-Za-z']+)){{0,4}})",
)

# English words that look name-like but aren't (often UI labels caught next to
# a role keyword on poorly structured pages).
_NAME_BLOCKLIST_WORDS = {
    'why', 'to', 'join', 'welcome', 'about', 'review', 'message', 'apply',
    'now', 'click', 'here', 'home', 'page', 'view', 'read', 'more', 'know',
    'our', 'team', 'us', 'meet', 'staff', 'admission', 'admissions',
    'overview', 'introduction', 'profile', 'photo', 'students', 'student',
    'connect', 'login', 'logout', 'register', 'sign', 'submit', 'next',
    'previous', 'back', 'continue', 'download', 'upload', 'browse', 'search',
    'menu', 'toggle', 'close', 'open', 'expand', 'collapse', 'enabled',
}

# Email "local parts" that signal a generic/role inbox rather than a person's
# email. We deprioritise these — only use as a fallback if no better match.
_GENERIC_EMAIL_PREFIXES = {
    'website', 'webmaster', 'admin', 'info', 'contact', 'enquiry', 'enquiries',
    'support', 'admissions', 'admission', 'office', 'help', 'no-reply',
    'noreply',
}


def _is_specific_email(email):
    """True if email's local part doesn't look like a generic role inbox."""
    if not email or '@' not in email:
        return False
    local = email.split('@', 1)[0].lower()
    return local not in _GENERIC_EMAIL_PREFIXES

# Junk-email substrings — exclude aggregator/no-reply/CDN-style addresses.
_EMAIL_BLOCKLIST = (
    'example.com', 'noreply', 'no-reply', 'donotreply', '@sentry',
    '@wixpress', '@cloudflare', 'indiacollegeshub', 'indiacollegesearch',
    'shiksha.com', 'collegedunia.com', 'collegedekho.com', 'careers360.com',
    'getmyuni', 'sarvgyan', 'jagranjosh',
)


# Words that signal we captured an institutional name, not a person.
_INSTITUTIONAL_TOKENS = {
    'university', 'institute', 'college', 'school', 'academy', 'foundation',
    'trust', 'society', 'corporation', 'company', 'limited', 'ltd', 'pvt',
    'private', 'public', 'government', 'national', 'group', 'campus',
    'linkedin', 'facebook', 'twitter', 'instagram', 'youtube', 'profile',
}


def _is_plausible_name(name):
    if not name or not (4 <= len(name) <= 50):
        return False
    words = name.split()
    if len(words) < 2:
        return False
    lower_words = [w.lower() for w in words]
    if any(w in _NAME_BLOCKLIST_WORDS for w in lower_words):
        return False
    # Reject if any token suggests this is the name of an institution, not
    # a person (LinkedIn snippets frequently leak "RKDF University" etc.).
    if any(w in _INSTITUTIONAL_TOKENS for w in lower_words):
        return False
    # Reject all-caps names of length >= 3 — these are usually acronyms or
    # institution names ("RKDF UNIVERSITY", "VICE CHANCELLOR").
    if all(w.isupper() and len(w) >= 3 for w in words):
        return False
    return True


def _is_junk_email(email):
    e = (email or '').lower()
    return any(bad in e for bad in _EMAIL_BLOCKLIST)


def _domain_from_url(url):
    """Extract the registrable domain (drop subdomains like www., admissions.)."""
    from urllib.parse import urlparse
    if not url:
        return ''
    host = (urlparse(url).hostname or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    # Keep last 2 labels for .com / .org, last 3 for .ac.in / .co.in / .edu.in
    parts = host.split('.')
    if len(parts) >= 3 and parts[-2] in ('ac', 'co', 'edu', 'gov', 'org', 'net'):
        return '.'.join(parts[-3:])
    return '.'.join(parts[-2:]) if len(parts) >= 2 else host


def _email_belongs_to_domain(email, domain):
    if not email or not domain:
        return False
    return email.lower().endswith('@' + domain) or email.lower().endswith('.' + domain)


def _normalize_contact_text(text):
    """Decode common email obfuscation patterns before running regexes.

    Handles bracketed/entity forms such as example[dot]name[at]domain[dot]com,
    plus normal HTML entity variants like &#64; and &#46;.
    """
    if not text:
        return ''
    s = html.unescape(str(text))
    s = s.replace('\u00a0', ' ')
    s = re.sub(r'\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}', '@', s, flags=re.I)
    s = re.sub(r'\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}', '.', s, flags=re.I)
    s = re.sub(r'\[\s*underscore\s*\]|\(\s*underscore\s*\)|\{\s*underscore\s*\}', '_', s, flags=re.I)
    s = re.sub(r'\[\s*hyphen\s*\]|\(\s*hyphen\s*\)|\{\s*hyphen\s*\}', '-', s, flags=re.I)
    s = re.sub(r'\s*@\s*', '@', s)
    s = re.sub(r'(?<=\w)\s*\.\s*(?=\w)', '.', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def _collect_link_contacts(soup):
    """Collect mailto/tel values from a page and return them as plain text."""
    if not soup:
        return ''
    parts = []
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if href.lower().startswith('mailto:'):
            parts.append(href[7:])
        elif href.lower().startswith('tel:'):
            parts.append(href[4:])
    return ' '.join(parts)


def extract_admin_contact(text, homepage_url=None):
    """Find the single best (name, role, email) triplet on the page text.

    Name-centric strategy (works for both "ROLE\\nName\\nEmail" and
    "Name, Role ... Email" layouts):
      1. Scan every name preceded by an honorific (Dr./Prof./...) on the page.
      2. For each name, check whether any leadership role keyword appears
         within +/- 300 chars (in either direction).
      3. Find the closest domain-matching email within +/- 800 chars.
         If the homepage domain is known, ONLY accept emails on that domain
         so we never pair an admin name with a footer/web-developer email.
      4. Best triplet wins: highest role priority, then smallest name-email
         distance.
    """
    if not text:
        return None
    text = re.sub(r'\s+', ' ', text)
    domain = _domain_from_url(homepage_url) if homepage_url else ''
    role_priority = {label: i for i, (label, _) in enumerate(_ADMIN_ROLES)}

    best = None  # (role_priority, email_distance, name, role, email)
    for nm in _NAME_AFTER_HONORIFIC_RE.finditer(text):
        name = nm.group(1).strip()
        if not _is_plausible_name(name):
            continue

        # Find the role keyword *closest* to the name in +/- 300 chars. We
        # take closest-by-position rather than highest-priority because the
        # canonical "ROLE: Title Name" pattern always has the actual role
        # right next to the name, while less-relevant role words (e.g. a
        # mention of a previous position the person held) sit farther away.
        win_start = max(0, nm.start() - 300)
        win_end = min(len(text), nm.end() + 300)
        role_found = None
        best_role_score = None  # (distance, priority) — smaller is better
        for pri, (label, pat) in enumerate(_ADMIN_ROLES):
            for rm in pat.finditer(text[win_start:win_end]):
                abs_pos = win_start + rm.start()
                # Distance to nearest end of name (start or end edge).
                dist = min(abs(abs_pos - nm.start()), abs(abs_pos - nm.end()))
                score = (dist, pri)
                if best_role_score is None or score < best_role_score:
                    best_role_score = score
                    role_found = label
        if role_found is None:
            continue

        # Best domain-matching email anywhere on the combined page text. We
        # rank specific emails (`director@`, `vc@`, `firstname@`) ahead of
        # generic inboxes (`info@`, `webmaster@`) and break ties by proximity
        # to the name. Searching the full text matters because the name often
        # lives on the leadership page while the email lives on the contacts
        # page, both of which we crawl and concatenate.
        email = None
        best_score = None  # (is_generic, distance) — smaller is better
        for em in _EMAIL_RE_GLOBAL.finditer(text):
            cand = em.group(0)
            if _is_junk_email(cand):
                continue
            if domain and not _email_belongs_to_domain(cand, domain):
                continue
            d = abs(em.start() - nm.end())
            score = (0 if _is_specific_email(cand) else 1, d)
            if best_score is None or score < best_score:
                best_score = score
                email = cand
        if not email:
            continue
        best_email_dist = best_score[1]

        candidate = (role_priority[role_found], best_email_dist,
                     name, role_found, email)
        if best is None or (candidate[0], candidate[1]) < (best[0], best[1]):
            best = candidate

    if best is None:
        return None
    return {'name': best[2], 'role': best[3], 'email': best[4]}


def _parse_next_data(html):
    """Pull and JSON-decode the __NEXT_DATA__ blob from a CollegeDunia page."""
    soup = BeautifulSoup(html, 'html.parser')
    nd = soup.find('script', id='__NEXT_DATA__')
    if not nd or not nd.string:
        return None
    try:
        return json.loads(nd.string)
    except Exception:
        return None


def fetch_collegedunia_people(profile_url):
    """Fetch a CollegeDunia profile page and pull leadership + faculty roster
    from its __NEXT_DATA__ JSON blob.

    Returns a dict:
        {
          'contact':  {'name', 'role', 'email', 'phone'}  — the leadership row
                       used to populate the main sheet's Contact columns, or
                       None if nothing usable was found,
          'faculty':  [ {'name', 'designation', 'department', 'email', 'phone',
                          'qualification'}, ... ]   — the full faculty roster,
                       possibly empty.
        }

    CollegeDunia stores this under:
        data.faculty.head_faculty_details  -> {head_faculty, designation}
        data.faculty.faculty_list          -> [{faculty_name, landline, email,
                                                designation, desig, department,
                                                qualification}, ...]
    """
    empty = {'contact': None, 'faculty': []}
    if not profile_url:
        return empty
    try:
        r = requests.get(profile_url, headers=_HEADERS, timeout=20)
    except requests.RequestException:
        return empty
    if r.status_code != 200:
        return empty
    data = _parse_next_data(r.text)
    if not data:
        return empty
    try:
        fac = data['props']['initialProps']['pageProps']['data']['faculty']
    except (KeyError, TypeError):
        return empty

    head = fac.get('head_faculty_details') or {}
    head_name = (head.get('head_faculty') or '').strip()
    head_role = (head.get('designation') or '').strip()
    faculty_list = fac.get('faculty_list') or []

    # Build the flat faculty roster first (used by both the main sheet's
    # Contact columns and the per-faculty Faculty sheet).
    faculty_rows = []
    for f in faculty_list:
        if not isinstance(f, dict):
            continue
        faculty_rows.append({
            'name':          _strip_honorific(f.get('faculty_name') or ''),
            'designation':   (f.get('desig') or '').strip(),
            'department':    (f.get('department') or '').strip(),
            'email':         (f.get('email') or '').strip(),
            'phone':         (f.get('landline') or '').strip(),
            'qualification': (f.get('qualification') or '').strip(),
        })

    # Pick the best leadership contact for the main-sheet Contact columns.
    contact = None

    # 1. Director-in-faculty-list — match on distinctive last name.
    if head_name and faculty_rows:
        head_tokens = {t.lower() for t in re.findall(r"[A-Za-z]{3,}", head_name)
                       if t.lower() not in {'prof', 'professor', 'dr', 'mr', 'mrs', 'ms'}}
        for f in faculty_rows:
            f_tokens = {t.lower() for t in re.findall(r"[A-Za-z]{3,}", f['name'])}
            if head_tokens and head_tokens.issubset(f_tokens) and f['email']:
                contact = {
                    'name':  f['name'],
                    'role':  head_role or 'Director',
                    'email': f['email'],
                    'phone': f['phone'],
                }
                break

    # 2. Director name only — no email available.
    if contact is None and head_name:
        contact = {
            'name':  _strip_honorific(head_name),
            'role':  head_role or 'Director',
            'email': '',
            'phone': '',
        }

    # 3. First full Professor with an email.
    if contact is None:
        for f in faculty_rows:
            if f['designation'] == 'Professor' and f['email']:
                contact = {
                    'name':  f['name'],
                    'role':  'Professor',
                    'email': f['email'],
                    'phone': f['phone'],
                }
                break

    # 4. Any faculty entry with an email.
    if contact is None:
        for f in faculty_rows:
            if f['email']:
                contact = {
                    'name':  f['name'],
                    'role':  f['designation'] or 'Faculty',
                    'email': f['email'],
                    'phone': f['phone'],
                }
                break

    return {'contact': contact, 'faculty': faculty_rows}


def _strip_honorific(name):
    """Remove leading 'Prof.', 'Dr.', 'DR.', 'Professor', etc. from a display name."""
    return re.sub(
        r'^\s*(?:Professor|Prof\.?|Dr\.?|Mr\.?|Mrs\.?|Ms\.?|Shri\.?|Smt\.?)\s+',
        '', name, flags=re.I).strip()


# ---------------------------------------------------------------------------
# NIRF "Data PDF" extraction — fills Approx Strength + Faculty Count + Intake
# for the ~800 colleges that participate in NIRF rankings.
# ---------------------------------------------------------------------------

# Cache parsed PDFs to disk so re-runs are instant
_NIRF_CACHE_DIR = 'nirf_cache'


def _nirf_cache_path(institute_id):
    import os
    os.makedirs(_NIRF_CACHE_DIR, exist_ok=True)
    return os.path.join(_NIRF_CACHE_DIR, f'{institute_id}.pdf')


def fetch_nirf_pdf(institute_id, pdf_url):
    """Download a NIRF Data PDF and return its raw bytes. Cached on disk."""
    import os
    path = _nirf_cache_path(institute_id)
    if os.path.exists(path) and os.path.getsize(path) > 100:
        with open(path, 'rb') as f:
            return f.read()
    try:
        r = requests.get(pdf_url, headers=_HEADERS, timeout=30)
    except requests.RequestException:
        return None
    if r.status_code != 200 or not r.content.startswith(b'%PDF'):
        return None
    with open(path, 'wb') as f:
        f.write(r.content)
    return r.content


def parse_nirf_pdf(pdf_bytes):
    """Parse a NIRF Data PDF and return structured fields.

    Returns a dict with whatever could be extracted:
        institute_name, institute_id, total_strength,
        intake_latest, faculty_count_full_time, faculty_count_part_time,
        phd_students_full_time, phd_students_part_time

    NIRF PDFs are tabular and broadly consistent across categories. The
    "Total Actual Student Strength" table has columns Male / Female / Total
    per program (UG 4yr, UG 5yr, PG 2yr, PG 3yr, etc.) — we sum the Total
    column to get the institute-wide enrolment.
    """
    import io
    try:
        import pdfplumber
    except ImportError:
        return None
    if not pdf_bytes:
        return None
    out = {}
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            full_text = ''
            tables = []
            for p in pdf.pages:
                full_text += '\n' + (p.extract_text() or '')
                for t in (p.extract_tables() or []):
                    tables.append(t)
    except Exception:
        return None

    # 1. Institute name + ID from the top-of-document header
    m = re.search(r'Institute Name:\s*(.+?)\s*\[(IR-[A-Z]-[A-Z]-\d+)\]', full_text)
    if m:
        out['institute_name'] = m.group(1).strip()
        out['institute_id'] = m.group(2)

    # 2. Total Actual Student Strength — sum the Total column of the program
    # strength table. The header row contains "No. of Male Students",
    # "No. of Female Students", "Total Students". Subsequent rows are per
    # program. We sum the "Total Students" column (index 3 in this table).
    total_strength = 0
    for tbl in tables:
        if not tbl or len(tbl) < 2:
            continue
        # Look for the header row that contains all 3 markers.
        header = ' '.join(str(c or '') for c in tbl[0]).lower()
        if 'male' in header and 'female' in header and 'total students' in header:
            for row in tbl[1:]:
                if not row or len(row) < 4:
                    continue
                val = (row[3] or '').strip() if isinstance(row[3], str) else str(row[3] or '')
                if val.isdigit():
                    total_strength += int(val)
            break
    if total_strength:
        out['total_strength'] = total_strength

    # 3. Latest year's sanctioned intake. The Intake table's header row is
    # "Academic Year | 2022-23 | 2021-22 | ...". The latest year column has
    # the most recent intake number; sum across program rows.
    intake_latest = 0
    for tbl in tables:
        if not tbl or len(tbl) < 2:
            continue
        header_row = [str(c or '').strip() for c in tbl[0]]
        if not header_row or 'Academic Year' not in header_row[0]:
            continue
        # The next column after "Academic Year" is the most recent year
        if len(header_row) < 2 or not re.match(r'20\d{2}-\d{2}', header_row[1]):
            continue
        for row in tbl[1:]:
            if not row or len(row) < 2:
                continue
            label = (row[0] or '').strip()
            if 'Program' in label:
                v = (row[1] or '').strip() if isinstance(row[1], str) else str(row[1] or '')
                if v.isdigit():
                    intake_latest += int(v)
        if intake_latest:
            break
    if intake_latest:
        out['intake_latest'] = intake_latest

    # 4. Faculty numbers — must be from the "Faculty Details" section.
    # NIRF lists Ph.D students BEFORE faculty in the doc, both with
    # "Full Time / Part Time" labels, so we anchor to "Faculty Details"
    # explicitly. Without anchoring, we'd grab the Ph.D numbers.
    fac_anchor = re.search(r'Faculty\s+Details', full_text, re.I)
    if fac_anchor:
        # Search a window starting at the anchor; faculty numbers are usually
        # within the next ~800 chars.
        window = full_text[fac_anchor.end():fac_anchor.end() + 1500]
        m = re.search(r'Full\s*Time\s+(\d{1,5})\b', window)
        if m:
            out['faculty_full_time'] = int(m.group(1))
        m = re.search(r'Part\s*Time\s+(\d{1,5})\b', window)
        if m:
            out['faculty_part_time'] = int(m.group(1))

    return out or None


# ---------------------------------------------------------------------------
# Placement Officer extraction — find Name / Phone / Email from each college's
# placement / training-placement / TPO / CDC page.
# ---------------------------------------------------------------------------

# Common placement-page paths to try off the homepage. The subdomain
# `placement.<bare-domain>` is also tried separately.
_PLACEMENT_PATHS = [
    '/placement', '/placements', '/placement-cell', '/placement_cell',
    '/training-placement', '/training-and-placement', '/training_and_placement',
    '/training-placement-cell', '/training_and_placement_cell',
    '/tpo', '/t-p-o', '/tnp', '/tnp-cell', '/cdc',
    '/career-cell', '/career_cell', '/career-development-cell', '/careers',
    '/placement-overview', '/placement/contact', '/placements/contact',
    '/internship', '/industry-interface', '/corporate-relations',
]

# Email local-parts that strongly suggest this is the placement-cell address.
# Score these higher than the generic admin@ / info@ that appears on every page.
_PLACEMENT_LOCAL_PARTS = (
    'placementofficer', 'placement_officer', 'placement-officer',
    'placement', 'placements', 'tpo', 'training_placement',
    'trainingplacement', 'careers', 'career', 'cdc', 'placementcell',
    'placement_cell', 'jobs', 'recruitment', 'pld',
)

# Regex to pull a person's name immediately following a placement-role keyword.
# Honorific is required so we don't grab random capitalised words.
_PLACEMENT_OFFICER_RE = re.compile(
    r'\b(?:Training\s*(?:and|&|&)?\s*Placement\s*Officer|'
    r'Placement\s*Officer|TPO|'
    r'Head\s*(?:of)?\s*Placement|Placement\s*Head|'
    r'Placement\s*(?:Cell\s*)?Coordinator|Placement\s*Coordinator|'
    rf'Chairman\s*[,-]?\s*(?:Career|Placement))\s*[:\-,]?\s+'
    rf"(?:Dr\.?|Prof\.?|Mr\.?|Mrs\.?|Ms\.?|Shri\.?|Smt\.?)\s+"
    rf"((?:[A-Z]\.|[A-Z][A-Za-z']+)"
    rf"(?:\s+(?!(?:{_POC_STOPWORDS})\b)(?:[A-Z]\.|[A-Z][A-Za-z']+)){{0,4}})",
    re.I,
)

# Fallback pattern for sites that omit honorifics.
_PLACEMENT_OFFICER_FALLBACK_RE = re.compile(
    r'\b(?:Training\s*(?:and|&)\s*Placement\s*Officer|'
    r'Placement\s*Officer|TPO|'
    r'Head\s*(?:of)?\s*Placement|Placement\s*Head|'
    r'Placement\s*(?:Cell\s*)?Coordinator|Placement\s*Coordinator)\s*[:\-,]?\s+'
    r'((?:[A-Z][A-Za-z\']{1,})(?:\s+[A-Z][A-Za-z\']{1,}){1,4})',
    re.I,
)

_PLACEMENT_KEYWORDS_RE = re.compile(
    r'(placement|training|tpo|tnp|cdc|recruit|career|internship|corporate)',
    re.I,
)

# External hosts that are commonly linked from footers/nav but are not useful
# for extracting on-site placement-cell contacts.
_PLACEMENT_EXTERNAL_HOST_BLOCKLIST = (
    'facebook.com', 'instagram.com', 'twitter.com', 'x.com', 'linkedin.com',
    'youtube.com', 'wa.me', 'whatsapp.com', 't.me', 'telegram.me',
    'maps.google.', 'google.com/maps', 'goo.gl',
)


def _placement_subdomain_url(homepage_url):
    """Given e.g. https://www.iiti.ac.in/, return https://placement.iiti.ac.in/."""
    from urllib.parse import urlparse
    if not homepage_url:
        return None
    try:
        u = urlparse(homepage_url)
        host = (u.hostname or '').lower()
        if not host:
            return None
        # Drop www. then prepend placement.
        if host.startswith('www.'):
            host = host[4:]
        scheme = u.scheme or 'https'
        return f'{scheme}://placement.{host}/'
    except Exception:
        return None


def _score_placement_email(email):
    """Lower scores rank earlier. Placement-specific local parts win; generic
    admin/info emails lose; gmail/yahoo only used as last resort."""
    if not email or '@' not in email:
        return 100
    local = email.split('@', 1)[0].lower()
    if any(p in local for p in _PLACEMENT_LOCAL_PARTS):
        return 0  # best: e.g. placementofficer@, tpo@
    if any(g in email.lower() for g in ('@gmail.', '@yahoo.', '@hotmail.', '@outlook.')):
        return 3
    if local in _GENERIC_EMAIL_PREFIXES:
        return 2  # generic admin@, info@
    return 1      # any other domain email


def find_placement_contact(homepage_url, max_pages=6, timeout=10):
    """Return {name, phone, email} for the placement officer of a college, by
    visiting the placement subdomain + common placement page paths off the
    homepage. Any field can be empty if not found. Returns None if no
    placement page was reachable at all.
    """
    if not homepage_url:
        return None
    from urllib.parse import urljoin, urlparse

    # Build the list of URLs to try, in order of likelihood.
    tries = []
    sub = _placement_subdomain_url(homepage_url)
    if sub:
        tries.append(sub)
    for path in _PLACEMENT_PATHS:
        tries.append(urljoin(homepage_url, path))

    # Also discover candidate links from the homepage nav/footer itself.
    hr = _fetch(homepage_url, timeout=timeout)
    if hr is not None and hr.status_code == 200 and 'html' in hr.headers.get('content-type', '').lower():
        try:
            hsoup = BeautifulSoup(hr.content, 'html.parser')
            base_host = (urlparse(homepage_url).hostname or '').lower().lstrip('www.')
            for a in hsoup.find_all('a', href=True):
                href = (a.get('href') or '').strip()
                if not href or href.startswith(('mailto:', 'tel:', 'javascript:')):
                    continue
                label = (a.get_text(separator=' ', strip=True) or '').lower()
                hay = (label + ' ' + href.lower())
                if not _PLACEMENT_KEYWORDS_RE.search(hay):
                    continue
                full = urljoin(homepage_url, href)
                host = (urlparse(full).hostname or '').lower().lstrip('www.')
                if host and any(bad in host for bad in _PLACEMENT_EXTERNAL_HOST_BLOCKLIST):
                    continue
                tries.append(full)
        except Exception:
            pass
    # Dedup while preserving order
    seen = set()
    ordered = []
    for u in tries:
        if u not in seen:
            seen.add(u)
            ordered.append(u)

    combined_text = ''
    visited = 0
    for u in ordered:
        if visited >= max_pages:
            break
        r = _fetch(u, timeout=timeout)
        if r is None or r.status_code != 200:
            continue
        ct = r.headers.get('content-type', '').lower()
        if 'html' not in ct:
            continue
        try:
            soup = BeautifulSoup(r.content, 'html.parser')
        except Exception:
            continue
        # If the page redirects to the homepage / is too short, it isn't a real
        # placement page — skip without counting against the budget.
        text = soup.get_text(separator=' ', strip=True)
        if len(text) < 120:
            continue
        text_lower = text.lower()
        if not _PLACEMENT_KEYWORDS_RE.search(text_lower):
            continue
        combined_text += '\n\n' + text
        visited += 1

    if not combined_text:
        return None

    out = {'name': '', 'phone': '', 'email': ''}
    text = re.sub(r'\s+', ' ', combined_text)

    # Placement-officer name
    m = _PLACEMENT_OFFICER_RE.search(text)
    if m:
        candidate = m.group(1).strip()
        if _is_plausible_name(candidate):
            out['name'] = candidate
    if not out['name']:
        m2 = _PLACEMENT_OFFICER_FALLBACK_RE.search(text)
        if m2:
            candidate = m2.group(1).strip()
            if _is_plausible_name(candidate):
                out['name'] = candidate

    # Best email candidate — rank by placement-related local part
    emails = sorted(set(_EMAIL_RE_GLOBAL.findall(text)))
    emails = [e for e in emails if not _is_junk_email(e)]
    if emails:
        emails.sort(key=_score_placement_email)
        if _score_placement_email(emails[0]) <= 2:
            out['email'] = ', '.join(emails)

    # Best phone candidate — Indian mobile or landline near a placement keyword.
    phone_pat = re.compile(
        r'(?:\+91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}\b|\(?0\d{2,4}\)?[\s-]?\d{6,8}\b'
    )
    # Restrict to phone numbers found within 300 chars of a placement keyword.
    candidates = []
    for kw_match in re.finditer(r'\b(?:placement|training|tpo|cdc|recruit)',
                                text, re.I):
        win_start = max(0, kw_match.start() - 50)
        win_end = min(len(text), kw_match.end() + 300)
        for pm in phone_pat.finditer(text[win_start:win_end]):
            candidates.append(pm.group(0))
    if not candidates:
        for pm in phone_pat.finditer(text):
            candidates.append(pm.group(0))
    if candidates:
        # Strip dup whitespace / dashes
        cleaned = []
        seen_p = set()
        for p in candidates:
            p = re.sub(r'\s+', ' ', p).strip()
            if p not in seen_p:
                seen_p.add(p)
                cleaned.append(p)
        out['phone'] = ', '.join(cleaned)

    # Only return the dict if we got at least one non-empty field.
    if any(out.values()):
        return out
    return None


def name_similarity(query_name, matched_name):
    """Return a 0-100 token-sort similarity score between two college names."""
    if not query_name or not matched_name:
        return 0
    return fuzz.token_sort_ratio(query_name.lower(), matched_name.lower())


# ---------------------------------------------------------------------------
# Multi-page crawl of a college's official website.
# ---------------------------------------------------------------------------

# Weighted sub-page keywords. Higher weights win when ranking the candidates,
# so we always pick the Director/Principal/Administration pages over generic
# About / Vision pages — those are where name+email pairs actually live.
_SUBPAGE_WEIGHTS = {
    # Highest: pages dedicated to a specific admin role
    'director':          10,
    'principal':         10,
    'vice-chancellor':   10,
    'vice_chancellor':   10,
    'vc-message':         9,
    'vcmessage':          9,
    # Strong: directories of administrative people
    'administration':     8,
    'admin/':             8,
    'office-bearers':     8,
    'office_bearers':     8,
    'people':             7,
    'leadership':         7,
    'governance':         6,
    'authorities':        6,
    'staff':              6,
    'faculty':            5,
    # Useful: contact + reach pages
    'contact-us':         5,
    'contact_us':         5,
    'contact':            4,
    'reach-us':           4,
    'reach_us':           4,
    # Weakest: generic about / overview pages
    'about-us':           2,
    'about':              1,
    'overview':           1,
    'admission':          1,
    'admissions':         1,
}


def _same_host(base_url, candidate):
    """True if candidate URL is on the same host as base_url."""
    from urllib.parse import urlparse
    try:
        bh = urlparse(base_url).hostname or ''
        ch = urlparse(candidate).hostname or ''
        return bh.lower().lstrip('www.') == ch.lower().lstrip('www.') and bool(ch)
    except Exception:
        return False


def _find_subpages(base_url, soup, max_pages=4):
    """Rank and return up to max_pages internal sub-page URLs that are most
    likely to contain admin contact info. Each link is scored by the highest
    _SUBPAGE_WEIGHTS keyword found in its href OR anchor text; we sort by
    score (desc) so /director and /administration beat /about-us / /overview.
    """
    from urllib.parse import urljoin
    candidates = {}  # full_url -> (score, anchor_text)
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue
        full = urljoin(base_url, href)
        if full in candidates:
            continue
        if not _same_host(base_url, full):
            continue
        text = (a.get_text(separator=' ', strip=True) or '').lower()
        hay = (text + ' ' + href).lower()
        score = 0
        for kw, w in _SUBPAGE_WEIGHTS.items():
            if kw in hay and w > score:
                score = w
        if score > 0:
            candidates[full] = (score, text)
    # Highest score first; stable so insertion order breaks ties.
    ranked = sorted(candidates.items(), key=lambda kv: -kv[1][0])
    return [url for url, _ in ranked[:max_pages]]


def _fetch(url, timeout=8):
    """GET a URL, tolerating SSL errors and trying https if http fails.

    SSL verification is OFF by default because many Indian college/govt
    sites have self-signed, expired, or Indian-CA-signed certs that the
    default trust store doesn't accept. Verifying first then retrying
    on failure roughly doubled per-row time, so we skip the verify=True
    attempt for these public read-only fetches.
    """
    attempts = [url]
    # If user gave http://, prepare an https:// fallback to try on failure.
    if url.startswith('http://'):
        attempts.append('https://' + url[len('http://'):])
    elif url.startswith('https://'):
        attempts.append('http://' + url[len('https://'):])
    for attempt in attempts:
        try:
            return requests.get(attempt, headers=_HEADERS,
                                timeout=timeout, verify=False,
                                allow_redirects=True)
        except requests.RequestException:
            continue
    return None


def crawl_official_site(url, max_pages=6, timeout=8, delay=0.2):
    """Fetch the homepage + a few contact-ish sub-pages and return the combined
    page text along with the list of URLs we successfully visited.
    """
    pages_visited = []
    combined_text = ''
    if not url:
        return combined_text, pages_visited
    r = _fetch(url, timeout=timeout)
    if r is None or r.status_code != 200:
        return combined_text, pages_visited
    if 'html' not in r.headers.get('content-type', '').lower():
        return combined_text, pages_visited
    # Parse homepage text (use .text to let requests handle encoding)
    try:
        soup = BeautifulSoup(r.text, 'html.parser')
    except Exception:
        # Fall back to raw bytes if text decoding/parsing fails
        try:
            soup = BeautifulSoup(r.content, 'html.parser')
        except Exception:
            return combined_text, pages_visited
    combined_text = _normalize_contact_text(
        soup.get_text(separator=' ', strip=True) + ' ' + _collect_link_contacts(soup)
    )
    pages_visited.append(r.url)

    for sub_url in _find_subpages(r.url, soup, max_pages=max_pages):
        time.sleep(delay)
        sr = _fetch(sub_url, timeout=timeout)
        if sr is None or sr.status_code != 200:
            continue
        # Skip non-HTML responses (PDFs, images, downloads, etc.)
        ctype = (sr.headers.get('content-type') or '').lower()
        if 'html' not in ctype:
            continue
        # Parse safely, preferring decoded text
        try:
            ssoup = BeautifulSoup(sr.text, 'html.parser')
        except Exception:
            try:
                ssoup = BeautifulSoup(sr.content, 'html.parser')
            except Exception:
                continue
        combined_text += '\n\n' + _normalize_contact_text(
            ssoup.get_text(separator=' ', strip=True) + ' ' + _collect_link_contacts(ssoup)
        )
        pages_visited.append(sr.url)

    return combined_text, pages_visited


def extract_contacts_from_text(text):
    """Run the same email/phone/POC/strength extractors used for CollegeDunia
    on arbitrary page text. Returns a dict of whatever it could find.
    """
    out = {}
    if not text:
        return out
    text = _normalize_contact_text(text)
    email_re = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    phone_re = r'(?:\+91[- ]?|0)?[6-9]\d{9}\b|\(?\d{2,4}\)?[- ]?\d{6,8}\b'
    emails = sorted(set(re.findall(email_re, text)))
    # Drop obvious non-college emails (CDNs, tracking, no-reply). Keep gmail
    # / yahoo / hotmail — many smaller Indian colleges legitimately use them.
    emails = [e for e in emails if not any(bad in e.lower() for bad in (
        'example.com', '@sentry.', '@wixpress.com', '@cloudflare.com',
        'noreply', 'no-reply', 'donotreply', '@sentry-next.',
    ))]
    # Prefer institutional / .edu / .ac.in / .in / college-domain addresses.
    def _email_rank(e):
        e = e.lower()
        if '.edu' in e or '.ac.in' in e:
            return 0
        if e.endswith('.in'):
            return 1
        if any(g in e for g in ('@gmail.com', '@yahoo.', '@hotmail.', '@outlook.')):
            return 3
        return 2
    emails.sort(key=_email_rank)
    phones = sorted(set(re.findall(phone_re, text)))
    if emails:
        out['Email ID'] = ', '.join(emails)
    if phones:
        out['Phone Number'] = ', '.join(phones)

    for pat in _POC_PATTERNS:
        m = pat.search(text)
        if m:
            out['POC Name'] = m.group(1).strip()
            break

    strength = _extract_strength(text)
    if strength:
        out['Approx Strength'] = strength
    return out


# ---------------------------------------------------------------------------
# LinkedIn x-ray search: find the admin's name via DDG, then pattern-match
# their email against the already-crawled college website text.
# ---------------------------------------------------------------------------

_LINKEDIN_ROLE_QUERIES = [
    ('Vice Chancellor', 'Vice Chancellor'),
    ('Director',        'Director'),
    ('Principal',       'Principal'),
    ('Registrar',       'Registrar'),
    ('Head of Department', 'HOD'),
]

# Patterns to pull a name + role from a search snippet. Each must capture the
# name in group 1. We deliberately allow short snippets — DDG search bodies are
# typically <200 chars.
_SNIPPET_NAME_PATTERNS = [
    # "SN Singh, Director of ABV-IIITM" / "R. K. Sharma, Vice Chancellor of..."
    re.compile(
        r'\b((?:[A-Z]\.?\s+){0,2}[A-Z][A-Za-z\']+(?:\s+[A-Z][A-Za-z\']+){0,3})'
        r'\s*,\s*(?:Prof\.?\s+|Dr\.?\s+)?'
        r'(?:Director|Principal|Vice[\s-]?Chancellor|HOD|Registrar|'
        r'Head\s+of\s+(?:the\s+)?(?:Department|Institution))\b',
    ),
    # "Prof. Suhas Joshi (Director)" / "Dr. R.K. Sharma, Director"
    re.compile(
        r'(?:Prof\.?|Dr\.?)\s+'
        r'((?:[A-Z]\.?\s*){0,2}[A-Z][A-Za-z\']+(?:\s+[A-Z][A-Za-z\']+){0,3})'
        r'\s*[(,]?\s*(?:Director|Principal|Vice[\s-]?Chancellor|HOD|Registrar)',
        re.I,
    ),
    # "Director: Prof. Name" / "Vice Chancellor - Dr. Name"
    re.compile(
        r'(?:Director|Principal|Vice[\s-]?Chancellor|HOD|Registrar|'
        r'Head\s+of\s+(?:Department|Institution))\s*[:\-,]\s*'
        r'(?:Prof\.?|Dr\.?|Shri\.?|Smt\.?)?\s*'
        r'((?:[A-Z]\.?\s*){0,2}[A-Z][A-Za-z\']+(?:\s+[A-Z][A-Za-z\']+){0,3})',
        re.I,
    ),
]


def _names_from_snippets(snippets, college_name):
    """Pull (name, role) candidates from a list of search-result snippet texts.

    We bias toward snippets that mention the college, since a generic snippet
    about 'directors in India' shouldn't contribute a name for this college.
    """
    out = []  # list of (name, role)
    college_tokens = {t.lower() for t in re.findall(r"[A-Za-z]+", college_name)
                      if len(t) >= 4}
    for snippet in snippets:
        if not snippet:
            continue
        snippet_lower = snippet.lower()
        # Require at least one distinctive college token to appear, to avoid
        # pulling random directors from unrelated colleges.
        if college_tokens and not any(t in snippet_lower for t in college_tokens):
            continue
        for pat in _SNIPPET_NAME_PATTERNS:
            for m in pat.finditer(snippet):
                name = m.group(1).strip()
                if _is_plausible_name(name):
                    # Look back in match to infer role
                    role = 'Director'  # default; the patterns mostly match Director
                    around = snippet[max(0, m.start() - 40):m.end() + 40].lower()
                    if 'vice chancellor' in around or 'vice-chancellor' in around:
                        role = 'Vice Chancellor'
                    elif 'principal' in around:
                        role = 'Principal'
                    elif 'registrar' in around:
                        role = 'Registrar'
                    elif 'hod' in around or 'head of department' in around:
                        role = 'HOD'
                    out.append((name, role))
    return out


def _email_candidates_for_name(name, domain):
    """Generate likely email local-parts for a person at a given domain.

    Indian institutional emails commonly use these patterns:
        first@domain          (most common at IITs/NITs)
        firstlast@domain
        first.last@domain
        flast@domain
        firstl@domain
    Plus role inboxes the same person might use (director@, vc@) which we add
    as low-priority fallbacks.
    """
    parts = re.findall(r"[A-Za-z]+", name.lower())
    parts = [p for p in parts if len(p) >= 2]  # drop single-letter initials
    if not parts:
        return []
    first = parts[0]
    last = parts[-1] if len(parts) > 1 else ''
    locals_ = []
    if first:
        locals_.append(first)
    if last:
        locals_.extend([
            last,
            first + last,
            first + '.' + last,
            first[0] + last,
            first + last[0],
            first + '_' + last,
            first[0] + '.' + last,
        ])
    return [f'{lp}@{domain}' for lp in locals_ if lp]


def _find_email_in_text(candidates, text):
    """Return the first candidate email that appears as a substring of text."""
    if not text or not candidates:
        return None
    t = text.lower()
    for c in candidates:
        if c.lower() in t:
            return c
    return None


def find_contact_via_linkedin_xray(college_name, domain, crawled_text=None,
                                   max_results=5, max_role_queries=3):
    """LinkedIn x-ray pipeline.

    1. For up to `max_role_queries` leadership roles, run a DDG search for
       `site:linkedin.com/in "Role" "College"`.
    2. Pull (name, role) candidates from the search-result snippets — these
       often appear in OTHER profiles' bios even when the role-holder isn't
       on LinkedIn directly.
    3. For each candidate, generate likely email local-parts at `domain`.
    4. Pick the first candidate whose generated email is found in `crawled_text`.

    Returns dict {name, role, email} or None.
    """
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    snippets = []
    queries_run = 0
    for role_query, _role_label in _LINKEDIN_ROLE_QUERIES:
        if queries_run >= max_role_queries:
            break
        q = f'site:linkedin.com/in "{role_query}" "{college_name}"'
        try:
            with DDGS() as ddgs:
                for r in ddgs.text(q, max_results=max_results):
                    body = r.get('body') or ''
                    title = r.get('title') or ''
                    snippets.append(title + ' ' + body)
        except Exception:
            pass
        queries_run += 1
        time.sleep(0.6)

    candidates = _names_from_snippets(snippets, college_name)
    if not candidates:
        return None

    # Deduplicate by normalised name, preserving order (first hit per name).
    seen = set()
    deduped = []
    for name, role in candidates:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append((name, role))

    # Only return a contact if we can actually verify an email pattern in the
    # crawled site text. Snippet-extracted names are too noisy on their own
    # (LinkedIn snippets mix multiple people/colleges) — requiring an email
    # match in the official site is the precision filter that makes this
    # approach worth using as a fallback.
    if not domain or not crawled_text:
        return None
    for name, role in deduped:
        emails = _email_candidates_for_name(name, domain)
        found = _find_email_in_text(emails, crawled_text)
        if found:
            return {'name': name, 'role': role, 'email': found}
    return None


if __name__ == '__main__':
    # Quick self-test
    tests = [
        ('Acropolis Institute of Technology & Research', 'Madhya pradesh'),
        ('VIT Bhopal University', 'Madhya pradesh'),
    ]
    for name, state in tests:
        print(f'\n=== {name} ({state}) ===')
        url = find_collegedunia_url(name, state)
        print('url:', url)
        if url:
            info = parse_collegedunia_profile(url)
            for k, v in info.items():
                print(f'  {k}: {v}')
        time.sleep(2)
