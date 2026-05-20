import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import concurrent.futures
import threading
import argparse
import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


import pandas as pd
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
FILE_LOCK = threading.Lock()
# --- CONFIGURATION ---
STATE_QUERIES = [
    # North India - States
    "Haryana",
    "Himachal Pradesh",
    "Punjab",
    "Rajasthan",
    "Uttar Pradesh",
    "Uttarakhand",
    
    # North India - Union Territories
    "Chandigarh",
    "Delhi",
    "Jammu and Kashmir",
    "Ladakh"
]

BLOCKED_DOMAINS = {
    "wikipedia.org", "facebook.com", "instagram.com", "linkedin.com", 
    "x.com", "twitter.com", "youtube.com", "shiksha.com", "collegedunia.com", 
    "careers360.com", "justdial.com", "indiamart.com", "collegedekho.com",
    "eduvow.com", "vedantu.com", "getmyuni.com", "zollege.in", "targetstudy.com",
    "sarvgyan.com", "jagranjosh.com", "university.youth4work.com", "mapsofindia.com",
    "studymode.com", "colleges-india.com", "ndl.gov.in", "studyindia.com",
    "archinect.com",

    "agnirva.com", "filertionline.in", "collegesearch.in", "indcareer.com", 
    "icbse.com", "mdsuajmer.ac.in","validcollege.com", "collegelist.in", "collegedunia.com", "collegedekho.com",
    "edufever.com", "rmgoe.org", "oakton.edu",
}

FACULTY_HINTS = {"faculty", "staff", "team", "people", "department", "hod", "professor", "academic", "school"}
PLACEMENT_HINTS = {"placement", "tpo", "tnp", "career", "recruit", "corporate", "crc", "training",}
CONTACT_HINTS = {"contact", "reach", "address", "office", "directory", "about", "enquiry", "helpdesk", "admissions"}
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?91[-\s]?)?(?:\(?0?\d{2,5}\)?[-\s]?)?\d{6,10}")
EXCEL_ILLEGAL_CHARS_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")

# --- DATA MODELS ---
@dataclass
class CollegeRecord:
    aishe_code: str
    college_name: str
    state: str
    website_url: str
    general_emails: str
    general_phones: str
    faculty_page_url: str
    placement_page_url: str
    placement_emails: str
    placement_phones: str

@dataclass
class FacultyRecord:
    aishe_code: str
    college_name: str
    faculty_name: str
    designation: str
    email: str
    phone: str
    source_url: str

def extract_contacts_from_soup(soup: BeautifulSoup) -> Tuple[List[str], List[str]]:
    """Extracts emails/phones from visible text AND hidden HTML attributes."""
    if not soup:
        return [], []
        
    # 1. Grab visible text
    text = soup.get_text(" ", strip=True)
    emails = EMAIL_RE.findall(text)
    phones = []
    for match in PHONE_RE.findall(text):
        digits = re.sub(r"\D", "", match)
        if len(digits) >= 10:
            phones.append(digits[-10:])

    # 2. Hunt for hidden HTML links
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip().lower()
        
        # Catch hidden emails
        if href.startswith("mailto:"):
            # Clean off subjects (e.g., mailto:info@x.com?subject=Help)
            clean_email = href.replace("mailto:", "").split("?")[0].strip()
            if clean_email:
                emails.append(clean_email)
                
        # Catch hidden phone links
        elif href.startswith("tel:"):
            digits = re.sub(r"\D", "", href)
            if len(digits) >= 10:
                phones.append(digits[-10:])

    return dedupe(emails), dedupe(phones)    

# --- HELPER FUNCTIONS ---
def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    # Reconstruct the base URL
    base = f"{parsed.scheme or 'https'}://{parsed.netloc}{parsed.path or '/'}"
    
    # If the URL uses a PHP or HTML query string (e.g., ?page=contact), KEEP IT!
    if parsed.query:
        return f"{base}?{parsed.query}"
        
    # Otherwise, just return the clean base URL
    return base.rstrip("/")

def clean_college_name(raw_name: str) -> str:
    if not isinstance(raw_name, str): return ""
    return re.sub(r'^\d+\s*-\s*', '', raw_name).strip()

def dedupe(values: List[str]) -> List[str]:
    seen = set()
    return [x for x in values if not (x in seen or seen.add(x))]

def extract_contacts_from_soup(soup: BeautifulSoup) -> Tuple[List[str], List[str]]:
    """Extracts emails/phones from visible text AND hidden HTML attributes."""
    if not soup:
        return [], []
        
    # 1. Grab visible text (What we were doing before)
    text = soup.get_text(" ", strip=True)
    emails = EMAIL_RE.findall(text)
    phones = []
    for match in PHONE_RE.findall(text):
        digits = re.sub(r"\D", "", match)
        if len(digits) >= 10:
            phones.append(digits[-10:])

    # 2. NEW: Hunt for hidden HTML links
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip().lower()
        
        # Catch hidden emails
        if href.startswith("mailto:"):
            # Clean off subjects (e.g., mailto:info@x.com?subject=Help)
            clean_email = href.replace("mailto:", "").split("?")[0].strip()
            if clean_email:
                emails.append(clean_email)
                
        # Catch hidden phone links
        elif href.startswith("tel:"):
            digits = re.sub(r"\D", "", href)
            if len(digits) >= 10:
                phones.append(digits[-10:])

    return dedupe(emails), dedupe(phones)


def strip_excel_illegal_chars(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set, dict)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    if not isinstance(value, str):
        return value
    if isinstance(value, str):
        cleaned = EXCEL_ILLEGAL_CHARS_RE.sub("", value)
        cleaned = "".join(ch for ch in cleaned if ch.isprintable() or ch in "\t\n\r")
        return cleaned
    return value


def sanitize_dataframe_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    clean_df = df.copy()
    for column in clean_df.columns:
        clean_df[column] = clean_df[column].map(strip_excel_illegal_chars)
    return clean_df


def export_to_excel(output_file: str, college_rows: List[Dict], faculty_rows: List[Dict]) -> None:
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    college_df = pd.DataFrame(college_rows)
    faculty_df = pd.DataFrame(faculty_rows)

    college_df = sanitize_dataframe_for_excel(college_df)
    faculty_df = sanitize_dataframe_for_excel(faculty_df)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        college_df.to_excel(writer, sheet_name="Colleges", index=False)
        faculty_df.to_excel(writer, sheet_name="Faculty", index=False)

# --- CORE LOGIC ---
def load_and_prepare_aishe_data(file_paths: List[str]) -> pd.DataFrame:
    dataframes = []
    for path in file_paths:
        if not os.path.exists(path):
            logging.warning(f"File not found: {path}. Check your spelling!")
            continue
            
        try:
            # 1. Read the first 15 rows to find where the actual headers are
            temp_df = pd.read_excel(path, nrows=15, header=None)
            header_row_index = 0
            
            for index, row in temp_df.iterrows():
                row_text = str(row.values).lower()
                # Broadened check to include "institution" or "college"
                if ('name' in row_text or 'institution' in row_text or 'college' in row_text) and 'state' in row_text:
                    header_row_index = index
                    break
            
            # 2. Read the file properly, skipping the junk rows
            df = pd.read_excel(path, header=header_row_index)
            
            # Broadened column mapping
            name_keywords = ['name', 'institution', 'college']
            name_col = next((c for c in df.columns if any(kw in str(c).lower() for kw in name_keywords) and 'state' not in str(c).lower() and 'district' not in str(c).lower()), None)
            state_col = next((c for c in df.columns if 'state' in str(c).lower()), None)
            id_col = next((c for c in df.columns if 'aishe' in str(c).lower() or 'id' in str(c).lower()), None)

            if name_col and state_col:
                temp_df = pd.DataFrame({
                    'Aishe_Code': df[id_col] if id_col else 'UNKNOWN',
                    'Raw_Name': df[name_col],
                    'State': df[state_col]
                })
                dataframes.append(temp_df)
                logging.info(f"Successfully loaded {len(temp_df)} rows from {path}")
            else:
                logging.warning(f"Could not find 'Name/Institution' or 'State' columns in {path}. Columns found: {list(df.columns)}")
                
        except Exception as e:
            logging.error(f"Error reading {path}: {e}")

    if not dataframes:
        raise ValueError("No objects to concatenate. Ensure your Excel files are valid.")

    master_df = pd.concat(dataframes, ignore_index=True)
    master_df = master_df[master_df['State'].astype(str).str.strip().isin(STATE_QUERIES)]
    master_df['Clean_Name'] = master_df['Raw_Name'].apply(clean_college_name)
    master_df = master_df[master_df['Clean_Name'] != ""]
    return master_df.drop_duplicates(subset=['Aishe_Code'])

def find_official_website(college_name: str, state: str) -> Optional[str]:
    # We leave .gov.in in the search query so DuckDuckGo still looks for AIIMS
    query_strict = f'"{college_name}" {state} site:.ac.in OR site:.edu.in OR site:.edu OR site:.org.in OR site:.gov.in'
    query_broad = f'"{college_name}" {state} official website'
    
    # --- NEW: Added Government Portal Keywords ---
    seo_domain_keywords = [
        'study', 'career', 'exam', 'result', 'naukri', 'sarkari', 'shiksha',
        'admission', 'dekho', 'university', 'colleges', 'getmy', 'sarvgyan', 'jagran',
        'blog', 'fever', 'portal', 'guide', 'info',
        'cee', 'dhe', 'nta', 'aicte', 'ugc', 'board', 'counselling' 
    ]
    bad_url_paths = ['/colleges/', '/university/', '/institute/', '/list', '/courses', '.pdf', '/directory', '/blog/']

    def is_valid_homepage(url: str, c_name: str) -> bool:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        path = parsed.path.lower()
        
        # 1. Reject Blacklist
        if any(b in domain for b in BLOCKED_DOMAINS):
            return False
            
        # 2. Reject SEO & Portal Buzzwords
        # REMOVED .gov.in from the safe list so state portals get caught here!
        if not domain.endswith((".ac.in", ".edu.in")):
            if any(seo_word in domain for seo_word in seo_domain_keywords):
                return False
                
        # 3. Reject bad paths
        if any(bad in path for bad in bad_url_paths):
            return False
            
        # 4. Reject suspiciously long paths
        if path.count('/') > 2 and len(path) > 20:
            return False

        # --- LAYER 3 NAME-MATCH HEURISTIC ---
        # REMOVED .gov.in from the safe list! It must prove it matches the college name.
        if not domain.endswith((".ac.in", ".edu.in", ".edu")):
            clean_name = re.sub(r'[^a-zA-Z\s]', '', c_name.lower())
            ignore_words = {'of', 'and', 'the', 'university', 'college', 'institute', 'for', 'science', 'technology', 'medical', 'all', 'india', 'state'}
            name_words = [w for w in clean_name.split() if w not in ignore_words and len(w) > 3]
            
            acronym = "".join([w[0] for w in clean_name.split() if w not in ignore_words])

            match_found = False
            for word in name_words:
                if word in domain:
                    match_found = True
                    break
            
            if acronym and acronym in domain and len(acronym) > 2:
                match_found = True

            if not match_found and len(name_words) > 0:
                return False
                
        return True

    try:
        time.sleep(5)
        with DDGS() as ddgs:
            # ATTEMPT 1: STRICT DOMAINS
            strict_results = list(ddgs.text(query_strict, max_results=5))
            for item in strict_results:
                url = item.get("href", "")
                domain = urlparse(url).netloc.lower()
                
                if domain.endswith((".ac.in", ".edu.in", ".edu", ".org.in", ".ernet.in", ".gov.in")):
                    if is_valid_homepage(url, college_name): 
                        return url

            # ATTEMPT 2: BROAD DOMAINS
            time.sleep(3)
            broad_results = list(ddgs.text(query_broad, max_results=10))
            
            for item in broad_results:
                url = item.get("href", "")
                if is_valid_homepage(url, college_name):
                    return url

            logging.warning(f"No clean/matching homepage found for {college_name}. Leaving blank.")
            return None
            
    except Exception as e:
        logging.warning(f"Search failed for {college_name}: {e}")
        
    return None

def fetch_soup(session: requests.Session, url: str) -> Optional[BeautifulSoup]:
    try:
        # verify=False is the magic key for older college websites
        response = session.get(url, timeout=15, allow_redirects=True, verify=False)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")
    except Exception as e:
        return None
    
def playwright_fallback(page, url: str) -> Tuple[List[str], List[str]]:
    """The Slow-Path: Uses a real browser in STEALTH mode to bypass Cloudflare and read hidden DOM elements."""
    try:
        # Load the page and wait for the network to settle
        page.goto(url, timeout=20000, wait_until="networkidle")
        
        # Give Cloudflare an extra 5 seconds to do its "Checking your browser" spin
        page.wait_for_timeout(5000) 
        
        # Grab the RAW HTML of the loaded page
        html_content = page.content()
        soup = BeautifulSoup(html_content, "html.parser")
        
        # Feed the HTML into our new extractor
        return extract_contacts_from_soup(soup)
    except Exception as e:
        logging.debug(f"Playwright/Cloudflare failed on {url}: {e}")
        return [], []

def deep_scrape_college(session: requests.Session, playwright_page, base_url: str, name: str, aishe: str) -> Tuple[CollegeRecord, List[FacultyRecord]]:
    home_soup = fetch_soup(session, base_url)
    if not home_soup:
        return CollegeRecord(aishe, name, "", base_url, "", "", "", "", "", ""), []

    # --- LAYER 2 SEO TITLE FINGERPRINTING ---
    title_tag = home_soup.find('title')
    page_title = title_tag.get_text(" ", strip=True).lower() if title_tag else ""
    seo_title_buzzwords = ['fees', 'cutoff', 'placements', 'reviews', 'admissions 20', 'ranking', 'scholarship', 'syllabus']
    
    spam_score = sum(1 for word in seo_title_buzzwords if word in page_title)
    if spam_score >= 2:
        logging.warning(f"SEO Spam Title detected for {name} ({base_url}). Aborting scrape.")
        return CollegeRecord(aishe, name, "", "", "", "", "", "", "", ""), []
    # ---------------------------------------------

    # 1. Scrape Homepage Contacts
    gen_emails, gen_phones = extract_contacts_from_soup(home_soup)

    # 2. Find and Categorize Internal Links (With Subdomain Support)
    base_domain = urlparse(base_url).netloc
    root_domain = base_domain.replace("www.", "") 
    contact_urls, faculty_urls, placement_urls = set(), set(), set()

    for a in home_soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith(('javascript:', '#')):
            continue
            
        full_url = normalize_url(urljoin(base_url, href))
        
        if urlparse(full_url).netloc.endswith(root_domain):
            # Grab normal text AND Image Alt Text
            link_text = a.get_text(" ", strip=True).lower()
            img = a.find('img')
            if img and img.get('alt'):
                link_text += " " + img.get('alt').lower()
                
            url_lower = full_url.lower()

            if any(h in link_text or h in url_lower for h in CONTACT_HINTS):
                contact_urls.add(full_url)
            if any(h in link_text or h in url_lower for h in FACULTY_HINTS):
                faculty_urls.add(full_url)
            if any(h in link_text or h in url_lower for h in PLACEMENT_HINTS):
                placement_urls.add(full_url)

    target_contact_pages = list(contact_urls)[:2]
    target_faculty_pages = list(faculty_urls)[:3]
    target_placement_pages = list(placement_urls)[:2]

    # 3. Deep Scrape Contact Pages
    for curl in target_contact_pages:
        c_soup = fetch_soup(session, curl)
        if c_soup:
            c_emails, c_phones = extract_contacts_from_soup(c_soup)
            gen_emails.extend(c_emails)
            gen_phones.extend(c_phones)

    # 3.5 THE PLAYWRIGHT FALLBACK FOR CONTACTS
    if not gen_emails:
        logging.info(f" -> BS4 blocked/empty. Engaging Playwright fallback for {name}...")
        p_emails, p_phones = playwright_fallback(playwright_page, base_url)
        gen_emails.extend(p_emails)
        gen_phones.extend(p_phones)
        
        if not gen_emails and target_contact_pages:
            p_emails, p_phones = playwright_fallback(playwright_page, target_contact_pages[0])
            gen_emails.extend(p_emails)
            gen_phones.extend(p_phones)

    # ---------------------------------------------------------
    # 4. Deep Scrape Placement Pages (UPGRADED WITH PLAYWRIGHT)
    # ---------------------------------------------------------
    place_emails, place_phones = [], []
    for purl in target_placement_pages:
        p_soup = fetch_soup(session, purl)
        if p_soup:
            p_emails, p_phones = extract_contacts_from_soup(p_soup)
            place_emails.extend(p_emails)
            place_phones.extend(p_phones)
            
        # NEW: If BS4 fails on the placement page, use Playwright!
        if not place_emails:
            logging.info(f" -> Engaging Playwright fallback for Placement page: {purl}")
            p_emails_play, p_phones_play = playwright_fallback(playwright_page, purl)
            place_emails.extend(p_emails_play)
            place_phones.extend(p_phones_play)

    # 5. Deep Scrape Faculty Pages
    faculty_records = []
    for furl in target_faculty_pages:
        fac_soup = fetch_soup(session, furl)
        if fac_soup:
            for block in fac_soup.find_all(["li", "tr", "div", "p"]):
                f_emails, f_phones = extract_contacts_from_soup(block)
                if f_emails or f_phones:
                    block_text = block.get_text(" ", strip=True)
                    words = block_text.split()
                    f_name = " ".join(words[:4]) if len(words) > 0 else ""
                    desig = "Professor" if "prof" in block_text.lower() else ("HOD" if "hod" in block_text.lower() else "Faculty")
                    faculty_records.append(FacultyRecord(
                        aishe, name, f_name, desig, ", ".join(dedupe(f_emails)), ", ".join(dedupe(f_phones)), furl
                    ))

    # ---------------------------------------------------------
    # 6. THE SMART EMAIL SORTER
    # ---------------------------------------------------------
    placement_keywords = ['tpo', 'placement', 'career', 'recruit', 'crc', 'training', 'corporate']
    final_gen_emails = []
    
    for email in dedupe(gen_emails):
        if any(keyword in email.lower() for keyword in placement_keywords):
            place_emails.append(email)
        else:
            final_gen_emails.append(email)

    # ---------------------------------------------------------
    # 7. ASSEMBLE RECORD
    # ---------------------------------------------------------
    college_record = CollegeRecord(
        aishe_code=str(aishe),
        college_name=name,
        state="", 
        website_url=base_url,
        general_emails=", ".join(final_gen_emails), 
        general_phones=", ".join(dedupe(gen_phones)),
        faculty_page_url=", ".join(target_faculty_pages),
        placement_page_url=", ".join(target_placement_pages),
        placement_emails=", ".join(dedupe(place_emails)), 
        placement_phones=", ".join(dedupe(place_phones))
    )

    return college_record, faculty_records

def process_single_college(row, checkpoint, checkpoint_file, output_file):
    """The task that each parallel worker will execute."""
    aishe = str(row['Aishe_Code'])
    name = row['Clean_Name']
    state = row['State']

    # 1. Skip if already processed
    if aishe in checkpoint["processed_codes"]:
        return

    logging.info(f"Thread started for: {name} ({state})")
    
    # 2. Search for the URL
    website_url = find_official_website(name, state)
    
    if website_url:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--window-size=1920,1080"
                ]
            )
            
            # --- THE SSL BYPASS FIX IS APPLIED HERE ---
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True 
            )
            playwright_page = context.new_page()
            
            # --- MANUALLY INJECT STEALTH JS ---
            playwright_page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = { runtime: {} };
            """)

            # 4. Scrape the data
            c_record, f_records = deep_scrape_college(session, playwright_page, website_url, name, aishe)
            c_record.state = state
            
            # 5. LOCK THE THREAD TO SAVE DATA SAFELY
            with FILE_LOCK:
                checkpoint["colleges"].append(asdict(c_record))
                checkpoint["faculty"].extend([asdict(f) for f in f_records])
                checkpoint["processed_codes"].append(aishe)
                
                # Save to disk
                with open(checkpoint_file, "w") as f:
                    json.dump(checkpoint, f)
                export_to_excel(output_file, checkpoint["colleges"], checkpoint["faculty"])
                logging.info(f"✅ SAVED TO EXCEL! ({len(checkpoint['processed_codes'])} total)")
    else:
        # Even if we didn't find a website, we mark it as processed so we don't try again
        with FILE_LOCK:
            checkpoint["processed_codes"].append(aishe)
            with open(checkpoint_file, "w") as f:
                json.dump(checkpoint, f)
                
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    
    excel_files = ["universities.xlsx", "colleges.xlsx", "standalone.xlsx"]
    output_file = "north_india_deep_contacts.xlsx"
    checkpoint_file = "scraper_checkpoint.json"

    # Load Checkpoint safely
    checkpoint = {"processed_codes": [], "colleges": [], "faculty": []}
    if os.path.exists(checkpoint_file):
        with open(checkpoint_file, "r") as f:
            checkpoint = json.load(f)
        logging.info(f"Loaded checkpoint. Resuming with {len(checkpoint['processed_codes'])} already processed.")

    df = load_and_prepare_aishe_data(excel_files)

    # --- THE PARALLEL THREAD POOL ---
    # MAX_WORKERS = 3. Do not set this higher than 5 unless you want to get IP banned.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        # Submit all the rows to the thread pool
        futures = []
        for index, row in df.iterrows():
            futures.append(
                executor.submit(process_single_college, row, checkpoint, checkpoint_file, output_file)
            )
            
        # Wait for all threads to finish
        concurrent.futures.wait(futures)

        for future in futures:
            try:
                future.result()
            except Exception:
                logging.exception("A worker failed while scraping a college.")

        export_to_excel(output_file, checkpoint["colleges"], checkpoint["faculty"])

    logging.info("Scraping Complete!")


if __name__ == "__main__":
    main()