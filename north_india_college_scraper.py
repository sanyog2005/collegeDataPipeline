import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS


STATE_QUERIES = [
    "Delhi",
    "Haryana",
    "Punjab",
    "Himachal Pradesh",
    "Jammu and Kashmir",
    "Ladakh",
    "Rajasthan",
    "Uttarakhand",
    "Uttar Pradesh",
    "Chandigarh",
]

NORTH_INDIA_HINTS = set(STATE_QUERIES)

DISCOVERY_QUERIES = [
    "college",
    "engineering college",
    "university college",
    "management college",
    "medical college",
    "law college",
    "pharmacy college",
    "teacher training college",
    "polytechnic college",
    "arts and science college",
]

DISCOVERY_BATCH_SIZE = 10

BLOCKED_DOMAINS = {
    "wikipedia.org",
    "facebook.com",
    "fb.com",
    "instagram.com",
    "linkedin.com",
    "x.com",
    "twitter.com",
    "youtube.com",
    "youtu.be",
    "shiksha.com",
    "collegedunia.com",
    "careers360.com",
    "justdial.com",
    "indiamart.com",
    "mapquest.com",
    "google.com",
    "goo.gl",
    "sites.google.com",
    "blogspot.com",
    "wordpress.com",
    "medium.com",
    "quora.com",
    "yelp.com",
    "sulekha.com",
}

CONTACT_HINTS = {
    "contact",
    "reach",
    "office",
    "about",
    "faculty",
    "team",
    "staff",
    "people",
    "placement",
    "training",
    "career",
    "tpo",
    "cell",
    "department",
    "hod",
    "principal",
}

FACULTY_HINTS = {"faculty", "staff", "team", "people", "department", "hod", "professor", "lecturer"}
PLACEMENT_HINTS = {"placement", "training", "career", "tpo", "job", "industry"}

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?91[-\s]?)?(?:\(?0?\d{2,5}\)?[-\s]?)?\d{6,10}")


@dataclass
class CollegeRecord:
    college_name: str
    search_url: str
    website_url: str
    website_domain: str
    general_emails: str
    general_phones: str
    contact_page_url: str
    faculty_page_url: str
    faculty_contacts_summary: str
    placement_page_url: str
    placement_emails: str
    placement_phones: str


@dataclass
class FacultyRecord:
    college_name: str
    faculty_name: str
    designation: str
    email: str
    phone: str
    page_url: str
    source_url: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape North India college contacts with checkpointed resume support.")
    parser.add_argument("--output", default="north_india_colleges.xlsx", help="Output Excel file path")
    parser.add_argument("--checkpoint", default="north_india_college_checkpoint.json", help="Checkpoint file path")
    parser.add_argument("--results-per-state", type=int, default=15, help="Search results per state query")
    parser.add_argument("--target-colleges", type=int, default=5000, help="Try to collect up to this many college rows")
    parser.add_argument("--max-colleges", type=int, default=0, help="Optional cap on total colleges to process")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout in seconds")
    parser.add_argument("--user-agent", default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    return parser


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc
    path = parsed.path or "/"
    return f"{scheme}://{netloc}{path}".rstrip("/")


def domain_matches(domain: str, blocked_domain: str) -> bool:
    domain = domain.lower().strip()
    blocked_domain = blocked_domain.lower().strip()
    return domain == blocked_domain or domain.endswith("." + blocked_domain)


def is_blocked_url(url: str) -> bool:
    domain = urlparse(url).netloc.lower()
    return any(domain_matches(domain, blocked_domain) for blocked_domain in BLOCKED_DOMAINS)


def dedupe_preserve_order(values: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    result: List[str] = []
    for value in values:
        cleaned = normalize_whitespace(value)
        if not cleaned:
            continue
        lower = cleaned.lower()
        if lower in seen:
            continue
        seen.add(lower)
        result.append(cleaned)
    return result


def extract_emails(text: str) -> List[str]:
    return dedupe_preserve_order(EMAIL_RE.findall(text or ""))


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) > 10:
        digits = digits[-10:]
    return digits


def extract_phones(text: str) -> List[str]:
    values: List[str] = []
    for match in PHONE_RE.findall(text or ""):
        phone = normalize_phone(match)
        if len(phone) == 10:
            values.append(phone)
    return dedupe_preserve_order(values)


def create_session(user_agent: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"})
    return session


import time # Make sure to import time at the top of your script

def search_colleges(results_per_state: int, target_colleges: int) -> List[Dict[str, str]]:
    collected: List[Dict[str, str]] = []
    seen_urls: Set[str] = set()
    
    with DDGS() as ddgs:
        for state in STATE_QUERIES:
            logging.info("Discovering colleges for %s", state)
            for query_seed in DISCOVERY_QUERIES[:DISCOVERY_BATCH_SIZE]:
                # query = f'{state} {query_seed} site:.ac.in OR site:.edu.in OR site:.org'
                # Create a string of domains you want the search engine to ignore
                negatives = "-shiksha -collegedunia -careers360 -justdial -youtube -medium -wikipedia -facebook"

# Add the negatives to your query
                query = f'"{state}" {query_seed} official website {negatives}'
                
                try:
                    # Added a delay to avoid rate-limiting
                    time.sleep(5) 
                    
                    for item in ddgs.text(query, max_results=results_per_state):
                        href = item.get("href") or item.get("url") or ""
                        title = normalize_whitespace(item.get("title") or "")
                        body = normalize_whitespace(item.get("body") or item.get("snippet") or "")
                        
                        if not href or not title:
                            continue
                        normalized = normalize_url(href)
                        if is_blocked_url(normalized):
                            print(f"Skipped Blocked Domain san: {normalized}")
                            continue
                        if normalized in seen_urls:
                            continue
                        
                        haystack = f"{title} {body} {href} {state} {query_seed}".lower()
                        if not any(hint.lower() in haystack for hint in NORTH_INDIA_HINTS):
                            continue
                            
                        seen_urls.add(normalized)
                        collected.append(
                            {
                                "title": title,
                                "href": normalized,
                                "body": body,
                                "state": state,
                            }
                        )
                        
                        if len(collected) >= target_colleges:
                            return collected
                            
                except Exception as e:
                    logging.warning("DDGS error for query '%s': %s. Pausing for 10 seconds...", query, e)
                    # If blocked, pause for a longer time before trying the next query
                    time.sleep(10)
                    continue 
                    
    return collected

def load_checkpoint(path: Path) -> Dict:
    if not path.exists():
        return {
            "results": [],
            "current_index": 0,
            "processed_urls": [],
            "college_rows": [],
            "faculty_rows": [],
        }
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_checkpoint(path: Path, payload: Dict) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def is_internal_link(base_url: str, candidate_url: str) -> bool:
    base_domain = urlparse(base_url).netloc.lower()
    candidate_domain = urlparse(candidate_url).netloc.lower()
    return bool(base_domain) and base_domain == candidate_domain


def get_page(session: requests.Session, url: str, timeout: int) -> Optional[BeautifulSoup]:
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")
    except Exception:
        return None


def page_text(soup: BeautifulSoup) -> str:
    return normalize_whitespace(soup.get_text(" ", strip=True))


def collect_internal_links(base_url: str, soup: BeautifulSoup) -> List[str]:
    links: List[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        label = normalize_whitespace(anchor.get_text(" ", strip=True).lower())
        if not href:
            continue
        absolute = normalize_url(urljoin(base_url, href))
        if not is_internal_link(base_url, absolute):
            continue
        combined = f"{href} {label}"
        if any(hint in combined for hint in CONTACT_HINTS):
            links.append(absolute)
    return dedupe_preserve_order(links)


def collect_contact_details_from_soup(soup: BeautifulSoup) -> Tuple[List[str], List[str]]:
    text = page_text(soup)
    emails = extract_emails(text)
    phones = extract_phones(text)

    mailto_emails = [link.replace("mailto:", "", 1).split("?")[0] for link in [a.get("href", "") for a in soup.find_all("a", href=True)] if link.startswith("mailto:")]
    tel_phones = [normalize_phone(link.replace("tel:", "", 1).split("?")[0]) for link in [a.get("href", "") for a in soup.find_all("a", href=True)] if link.startswith("tel:")]

    emails.extend(mailto_emails)
    phones.extend(tel_phones)
    return dedupe_preserve_order(emails), dedupe_preserve_order(phones)


def choose_best_link(links: List[str], hints: Set[str]) -> str:
    for link in links:
        path = urlparse(link).path.lower()
        if any(hint in path for hint in hints):
            return link
    return links[0] if links else ""


def find_candidate_links(base_url: str, soup: BeautifulSoup) -> List[str]:
    links: List[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        label = normalize_whitespace(anchor.get_text(" ", strip=True).lower())
        if not href:
            continue
        absolute = normalize_url(urljoin(base_url, href))
        if not is_internal_link(base_url, absolute):
            continue
        combined = f"{href} {label}"
        if any(hint in combined for hint in FACULTY_HINTS | PLACEMENT_HINTS | {"contact", "address", "about"}):
            links.append(absolute)
    return dedupe_preserve_order(links)


def extract_name_from_block(block: BeautifulSoup) -> str:
    for selector in ["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"]:
        heading = block.find(selector)
        if heading:
            text = normalize_whitespace(heading.get_text(" ", strip=True))
            if text:
                return text
    text = normalize_whitespace(block.get_text(" ", strip=True))
    words = text.split()
    if 2 <= len(words) <= 6:
        return text
    return ""


def extract_faculty_rows(college_name: str, page_url: str, soup: BeautifulSoup) -> List[FacultyRecord]:
    rows: List[FacultyRecord] = []
    blocks = soup.find_all(["article", "section", "div", "li", "tr"])
    for block in blocks:
        text = normalize_whitespace(block.get_text(" ", strip=True))
        if not text:
            continue
        emails = extract_emails(text)
        phones = extract_phones(text)
        if not emails and not phones:
            continue
        name = extract_name_from_block(block)
        designation = ""
        lower = text.lower()
        for keyword in ["professor", "assistant professor", "associate professor", "lecturer", "hod", "dean", "principal", "director"]:
            if keyword in lower:
                designation = keyword.title()
                break
        rows.append(
            FacultyRecord(
                college_name=college_name,
                faculty_name=name,
                designation=designation,
                email=", ".join(emails),
                phone=", ".join(phones),
                page_url=page_url,
                source_url=page_url,
            )
        )
    if rows:
        return rows

    text = page_text(soup)
    emails = extract_emails(text)
    phones = extract_phones(text)
    if emails or phones:
        rows.append(
            FacultyRecord(
                college_name=college_name,
                faculty_name="",
                designation="",
                email=", ".join(emails),
                phone=", ".join(phones),
                page_url=page_url,
                source_url=page_url,
            )
        )
    return rows


def summarize_contacts(emails: List[str], phones: List[str]) -> str:
    parts: List[str] = []
    if emails:
        parts.append("Emails: " + ", ".join(emails))
    if phones:
        parts.append("Phones: " + ", ".join(phones))
    return " | ".join(parts)


def scrape_college(session: requests.Session, result: Dict[str, str], timeout: int) -> Tuple[CollegeRecord, List[FacultyRecord]]:
    college_name = result["title"]
    search_url = result["href"]
    home_soup = get_page(session, search_url, timeout)
    if home_soup is None:
        return (
            CollegeRecord(
                college_name=college_name,
                search_url=search_url,
                website_url=search_url,
                website_domain=urlparse(search_url).netloc,
                general_emails="",
                general_phones="",
                contact_page_url="",
                faculty_page_url="",
                faculty_contacts_summary="",
                placement_page_url="",
                placement_emails="",
                placement_phones="",
            ),
            [],
        )

    try:
        response = session.get(search_url, timeout=timeout, allow_redirects=True)
        website_url = normalize_url(response.url)
    except Exception as e:
        logging.warning("Could not resolve website for %s: %s", college_name, e)
        return (
            CollegeRecord(
                college_name=college_name,
                search_url=search_url,
                website_url=search_url,
                website_domain=urlparse(search_url).netloc,
                general_emails="",
                general_phones="",
                contact_page_url="",
                faculty_page_url="",
                faculty_contacts_summary="",
                placement_page_url="",
                placement_emails="",
                placement_phones="",
            ),
            [],
        )
    if is_blocked_url(website_url):
        return (
            CollegeRecord(
                college_name=college_name,
                search_url=search_url,
                website_url=website_url,
                website_domain=urlparse(website_url).netloc,
                general_emails="",
                general_phones="",
                contact_page_url="",
                faculty_page_url="",
                faculty_contacts_summary="",
                placement_page_url="",
                placement_emails="",
                placement_phones="",
            ),
            [],
        )
    website_domain = urlparse(website_url).netloc
    if not website_domain.endswith(('.ac.in', '.edu.in', '.org.in')) and website_domain not in {"ac.in", "edu.in", "org.in"}:
        return (
            CollegeRecord(
                college_name=college_name,
                search_url=search_url,
                website_url=website_url,
                website_domain=website_domain,
                general_emails="",
                general_phones="",
                contact_page_url="",
                faculty_page_url="",
                faculty_contacts_summary="",
                placement_page_url="",
                placement_emails="",
                placement_phones="",
            ),
            [],
        )
    all_links = collect_internal_links(website_url, home_soup)
    candidate_links = find_candidate_links(website_url, home_soup)

    contact_page = choose_best_link(candidate_links, {"contact", "reach", "address", "office"})
    faculty_page = choose_best_link(candidate_links, FACULTY_HINTS)
    placement_page = choose_best_link(candidate_links, PLACEMENT_HINTS)

    general_emails, general_phones = collect_contact_details_from_soup(home_soup)
    faculty_rows: List[FacultyRecord] = []
    faculty_summary = ""
    placement_emails: List[str] = []
    placement_phones: List[str] = []

    pages_to_scan = [
        contact_page,
        faculty_page,
        placement_page,
    ]
    for link in all_links:
        if len(pages_to_scan) >= 5:
            break
        if link not in pages_to_scan:
            pages_to_scan.append(link)

    seen_page_urls: Set[str] = set()
    for page_url in pages_to_scan:
        if not page_url or page_url in seen_page_urls:
            continue
        seen_page_urls.add(page_url)
        soup = get_page(session, page_url, timeout)
        if soup is None:
            continue
        emails, phones = collect_contact_details_from_soup(soup)
        page_text_value = page_text(soup).lower()

        if page_url == placement_page or any(hint in page_text_value for hint in PLACEMENT_HINTS):
            placement_emails.extend(emails)
            placement_phones.extend(phones)

        if page_url == faculty_page or any(hint in page_text_value for hint in FACULTY_HINTS):
            faculty_rows.extend(extract_faculty_rows(college_name, page_url, soup))

        if page_url == contact_page and not general_emails and not general_phones:
            general_emails.extend(emails)
            general_phones.extend(phones)

        if page_url == faculty_page:
            faculty_summary = summarize_contacts(emails, phones)

    if not faculty_rows and faculty_page:
        soup = get_page(session, faculty_page, timeout)
        if soup is not None:
            faculty_rows.extend(extract_faculty_rows(college_name, faculty_page, soup))

    college_record = CollegeRecord(
        college_name=college_name,
        search_url=search_url,
        website_url=website_url,
        website_domain=website_domain,
        general_emails=", ".join(dedupe_preserve_order(general_emails)),
        general_phones=", ".join(dedupe_preserve_order(general_phones)),
        contact_page_url=contact_page,
        faculty_page_url=faculty_page,
        faculty_contacts_summary=faculty_summary,
        placement_page_url=placement_page,
        placement_emails=", ".join(dedupe_preserve_order(placement_emails)),
        placement_phones=", ".join(dedupe_preserve_order(placement_phones)),
    )
    return college_record, faculty_rows


def export_excel(output_path: Path, college_rows: List[Dict], faculty_rows: List[Dict]) -> None:
    colleges_df = pd.DataFrame(college_rows)
    faculty_df = pd.DataFrame(faculty_rows)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        colleges_df.to_excel(writer, sheet_name="Colleges", index=False)
        faculty_df.to_excel(writer, sheet_name="Faculty", index=False)


def serialize_records(college_record: CollegeRecord, faculty_rows: List[FacultyRecord]) -> Tuple[Dict, List[Dict]]:
    college_dict = asdict(college_record)
    faculty_dicts = [asdict(row) for row in faculty_rows]
    return college_dict, faculty_dicts


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    checkpoint_path = Path(args.checkpoint)
    output_path = Path(args.output)
    checkpoint = load_checkpoint(checkpoint_path)

    results = checkpoint.get("results") or []
    if not results or len(results) < args.target_colleges:
        logging.info("Running DDGS discovery to build a larger result set.")
        results = search_colleges(args.results_per_state, args.target_colleges)
        checkpoint["results"] = results
        checkpoint["current_index"] = 0
        checkpoint["processed_urls"] = []
        checkpoint["college_rows"] = []
        checkpoint["faculty_rows"] = []
        save_checkpoint(checkpoint_path, checkpoint)

    college_rows: List[Dict] = checkpoint.get("college_rows") or []
    faculty_rows: List[Dict] = checkpoint.get("faculty_rows") or []
    processed_urls: Set[str] = set(checkpoint.get("processed_urls") or [])
    current_index = int(checkpoint.get("current_index") or 0)

    if args.max_colleges and current_index >= args.max_colleges:
        logging.info("Checkpoint already satisfies the requested max-colleges value.")
        export_excel(output_path, college_rows, faculty_rows)
        return 0

    if len(results) < args.target_colleges:
        logging.warning(
            "Discovery produced %s candidate websites, which is below the requested target of %s. The run will continue with what was found.",
            len(results),
            args.target_colleges,
        )

    logging.info("Starting processing with %s discovered candidates.", len(results))

    session = create_session(args.user_agent)
    processed_since_save = 0

    for index in range(current_index, len(results)):
        if args.max_colleges and len(college_rows) >= args.max_colleges:
            break
        if args.target_colleges and len(college_rows) >= args.target_colleges:
            break

        result = results[index]
        url = result["href"]
        if url in processed_urls:
            checkpoint["current_index"] = index + 1
            continue

        logging.info("Processing %s (%s/%s)", result["title"], index + 1, len(results))
        college_record, faculty_records = scrape_college(session, result, args.timeout)
        college_dict, faculty_dicts = serialize_records(college_record, faculty_records)

        college_rows.append(college_dict)
        faculty_rows.extend(faculty_dicts)
        processed_urls.add(url)

        checkpoint["current_index"] = index + 1
        checkpoint["processed_urls"] = sorted(processed_urls)
        checkpoint["college_rows"] = college_rows
        checkpoint["faculty_rows"] = faculty_rows

        processed_since_save += 1
        if processed_since_save >= 10:
            logging.info("Saving checkpoint after %s rows.", processed_since_save)
            save_checkpoint(checkpoint_path, checkpoint)
            export_excel(output_path, college_rows, faculty_rows)
            processed_since_save = 0

    if processed_since_save:
        save_checkpoint(checkpoint_path, checkpoint)
        export_excel(output_path, college_rows, faculty_rows)

    save_checkpoint(checkpoint_path, checkpoint)
    export_excel(output_path, college_rows, faculty_rows)
    logging.info("Done. Wrote %s and %s.", output_path, checkpoint_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

