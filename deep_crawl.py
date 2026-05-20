import os
import re
import time
import socket
import io
import pandas as pd
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from collections import deque
import urllib3
import logging

# --- External Intelligence Libraries ---
from googlesearch import search
import smtplib
import dns.resolver
import fitz  # PyMuPDF

# Suppress insecure request warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- CONFIGURATION ---
INPUT_FILE = 'north_india_deep_contacts.xlsx'
SHEET_NAME = 'Colleges'
MAX_PAGES_TO_CRAWL = 25 

# --- LOGGING SETUP ---
LOG_LEVEL = logging.INFO 
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# --- PATTERNS & FILTERS ---
EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
PHONE_REGEX = re.compile(r'(?:(?:\+|0{0,2})91[\s-]?)?[6-9]\d{9}|0\d{2,4}[-\s]?\d{6,8}') 
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
IGNORE_EXTENSIONS = ('.pdf', '.jpg', '.jpeg', '.png', '.gif', '.zip', '.doc', '.docx', '.mp4', '.xls', '.xlsx')

GENERIC_EMAIL_KEYWORDS = [
    'info@', 'admission', 'contact', 'admin@', 'enquiry', 'query', 
    'helpdesk', 'support', 'career', 'hello@', 'mail@', 'nodal'
]

VALID_PERSONAL_DOMAINS = ['gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com']

# Global Circuit Breaker for Google
google_cooldown_until = 0  

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def get_base_domain(url):
    return urlparse(url).netloc.replace('www.', '')

def is_valuable_email(email):
    if '@' not in email: return False 
    if len(email) < 5: return False    
    email_lower = email.lower()
    if email_lower.endswith(IGNORE_EXTENSIONS): return False
    if any(keyword in email_lower for keyword in GENERIC_EMAIL_KEYWORDS): return False
    return True

def is_domain_match(email, base_domain):
    email_domain = email.split('@')[-1].lower()
    if base_domain in email_domain: return True
    if email_domain in VALID_PERSONAL_DOMAINS: return True
    return False

def filter_existing_cell(raw_email_string):
    if not raw_email_string or pd.isna(raw_email_string) or str(raw_email_string).lower() == 'nan':
        return []
    found_emails = EMAIL_REGEX.findall(str(raw_email_string))
    return [e for e in found_emails if is_valuable_email(e)]

# ==========================================
# METHOD 1: DEEP CRAWLING (Smart Spider)
# ==========================================
def extract_data_and_links(html, current_url, base_domain):
    # Using lxml parser for speed
    soup = BeautifulSoup(html, 'lxml')
    emails, phones, internal_links = set(), set(), set()
    
    for a in soup.find_all('a', href=True):
        href = a.get('href', '').lower()
        if href.startswith('mailto:'): emails.add(href.replace('mailto:', '').split('?')[0].strip())
        elif href.startswith('tel:'): phones.add(href.replace('tel:', '').strip())

    for script in soup(["script", "style"]): script.extract() 
    text = soup.get_text(separator=' ')
    
    emails.update(EMAIL_REGEX.findall(text))
    phones.update(PHONE_REGEX.findall(text))
    
    clean_emails = [e for e in emails if is_valuable_email(e)]
    
    for a in soup.find_all('a', href=True):
        raw_link = a['href'].strip()
        if raw_link.startswith(('#', 'mailto:', 'tel:', 'javascript:')): continue
        
        absolute_url = urljoin(current_url, raw_link).split('#')[0]
        if get_base_domain(absolute_url) == base_domain and not absolute_url.lower().endswith(IGNORE_EXTENSIONS):
            internal_links.add(absolute_url)
            
    return {'emails': list(clean_emails), 'phones': list(phones)}, list(internal_links)

def crawl_website(seed_url):
    logger.info(f"[Step 1] Booting Smart-Spider for: {seed_url}")
    base_domain = get_base_domain(seed_url)
    visited, queue = set(), deque([seed_url]) 
    all_emails, all_phones = set(), set()
    pages_crawled = 0

    while queue and pages_crawled < MAX_PAGES_TO_CRAWL:
        current_url = queue.popleft()
        if current_url in visited: continue
        visited.add(current_url)
        pages_crawled += 1
        
        try:
            resp = requests.get(current_url, headers=HEADERS, timeout=5, verify=False)
            if resp.status_code != 200 or 'text/html' not in resp.headers.get('Content-Type', ''): continue
            
            contacts, new_links = extract_data_and_links(resp.text, current_url, base_domain)
            all_emails.update(contacts['emails'])
            all_phones.update(contacts['phones'])
            
            if all_emails or all_phones:
                logger.info(f"  [+] Spider hit paydirt on page {pages_crawled}. Stopping crawl.")
                break
            
            # Priority routing for placement pages
            priority_keywords = ['place', 'tpo', 'career', 'recruit', 'alumni','training','development','cdc','crc','placement', 'training-and-placement', 'training-placement', 'training_placement', 'placement_officer', 'tpo', 'training-and-placement-officer', 'training-placement-officer', 'placement-officer', 'training_and_placement_officer','registrar', 'vc', 'vice-chancellor', 'director', 'principal', 'dean']
            for link in new_links:
                if link not in visited and link not in queue:
                    if any(k in link.lower() for k in priority_keywords):
                        queue.appendleft(link)
                    else:
                        queue.append(link)
                    
        except requests.exceptions.RequestException:
            continue

    return {'emails': list(all_emails), 'phones': list(all_phones)}

# ==========================================
# METHOD 2: PDF BROCHURE SCRAPING
# ==========================================
def scrape_domain_pdfs(domain):
    global google_cooldown_until
    
    if time.time() < google_cooldown_until:
        logger.debug(f"  [-] Skipping PDF search (Google is in 15-min cooldown)")
        return {'emails': [], 'phones': []}

    logger.info(f"[Step 2] Hunting for Placement PDFs on {domain} via Google...")
    query = f'site:{domain} filetype:pdf "placement" OR "tpo" OR "brochure"'
    found_emails, found_phones = set(), set()
    
    try:
        # 5 second sleep between searches to mimic humans and stay free
        results = list(search(query, num_results=2, sleep_interval=5))
        
        for pdf_url in results:
            if not pdf_url.lower().endswith('.pdf'): continue
            logger.info(f"  [~] Downloading PDF for analysis: {pdf_url}")
            try:
                pdf_resp = requests.get(pdf_url, headers=HEADERS, timeout=10, verify=False)
                if pdf_resp.status_code == 200 and 'application/pdf' in pdf_resp.headers.get('Content-Type', ''):
                    pdf_stream = io.BytesIO(pdf_resp.content)
                    doc = fitz.open(stream=pdf_stream, filetype="pdf")
                    
                    pdf_text = ""
                    for page_num in range(min(15, doc.page_count)):
                        pdf_text += doc[page_num].get_text()
                        
                    raw_emails = EMAIL_REGEX.findall(pdf_text)
                    clean_emails = [e for e in raw_emails if is_valuable_email(e) and is_domain_match(e, domain)]
                    
                    found_emails.update(clean_emails)
                    found_phones.update(PHONE_REGEX.findall(pdf_text))
                    doc.close()
                    
                    if found_emails or found_phones:
                        logger.info("  [+] Valid contacts found inside PDF.")
                        break 
            except Exception: pass
            
    except Exception as e:
        error_msg = str(e)
        if "429" in error_msg or "Too Many Requests" in error_msg:
            logger.error("  [!!!] GOOGLE IP BAN DETECTED. Engaging 15-minute global cooldown.")
            google_cooldown_until = time.time() + 900
        else:
            logger.error(f"  [-] Google PDF Search failed: {type(e).__name__}")

    return {'emails': list(found_emails), 'phones': list(found_phones)}

# ==========================================
# METHOD 3: LINKEDIN DORKING
# ==========================================
def linkedin_dork(college_name, domain):
    global google_cooldown_until
    
    if time.time() < google_cooldown_until:
        logger.debug(f"  [-] Skipping LinkedIn Dorking (Google is in 15-min cooldown)")
        return []

    logger.info(f"[Step 3] Initiating LinkedIn Dorking for {college_name}...")
    query = f'site:linkedin.com/in "Training and Placement Officer" OR "TPO" "{college_name}"'
    found_emails = set()
    
    try:
        results = list(search(query, num_results=3, sleep_interval=5, advanced=True))
            
        for res in results:
            snippet = res.description if hasattr(res, 'description') else ''
            raw_emails = EMAIL_REGEX.findall(snippet)
            clean_emails = [e for e in raw_emails if is_valuable_email(e) and is_domain_match(e, domain)]
            found_emails.update(clean_emails)
                
        if found_emails:
            logger.info(f"  [+] Found domain-verified emails in LinkedIn snippets.")
        return list(found_emails)
        
    except Exception as e:
        error_msg = str(e)
        if "429" in error_msg or "Too Many Requests" in error_msg:
            logger.error("  [!!!] GOOGLE IP BAN DETECTED. Engaging 15-minute global cooldown.")
            google_cooldown_until = time.time() + 900 
        else:
            logger.error(f"  [-] Google Dorking failed: {type(e).__name__}")
        
    return []

# ==========================================
# MAIN EXECUTION PIPELINE
# ==========================================
def process_empty_row(college_name, website_url):
    print(f"\n{'='*80}")
    logger.info(f"TARGET: {college_name}")
    print(f"{'-'*80}")
    
    if not str(website_url).startswith('http'): 
        website_url = 'https://' + str(website_url)
        
    domain = get_base_domain(website_url)
    
    # STEP 1: Smart Crawl
    contacts = crawl_website(website_url)
    
    # STEP 2: PDF Parsing
    if not contacts['emails']:
        pdf_contacts = scrape_domain_pdfs(domain)
        if pdf_contacts['emails'] or pdf_contacts['phones']:
            contacts['emails'].extend(pdf_contacts['emails'])
            contacts['phones'].extend(pdf_contacts['phones'])
            
    # STEP 3: LinkedIn Dorking
    if not contacts['emails']:
        dorked_emails = linkedin_dork(college_name, domain)
        if dorked_emails:
            contacts['emails'].extend(dorked_emails)

    # Clean duplicates
    contacts['emails'] = list(set(contacts['emails']))
    contacts['phones'] = list(set(contacts['phones']))
    
    if contacts['emails'] or contacts['phones']:
        logger.info(f"FINAL RESULT: Extracted {len(contacts['emails'])} Emails, {len(contacts['phones'])} Phones.")
        for e in contacts['emails']: logger.info(f"  --> Email: {e}")
        for p in contacts['phones']: logger.info(f"  --> Phone: {p}")
        return contacts
    else:
        logger.warning(f"FINAL RESULT: All methods exhausted. Data unavailable.")
        return None

# ==========================================
# SEQUENTIAL MAIN LOOP
# ==========================================
def main():
    if not os.path.exists(INPUT_FILE):
        logger.critical(f"Error: {INPUT_FILE} not found.")
        return

    logger.info("Loading Excel data...")
    df = pd.read_excel(INPUT_FILE, sheet_name=SHEET_NAME, dtype=str)
    
    if 'placement_emails' not in df.columns: df['placement_emails'] = ''
    if 'placement_phones' not in df.columns: df['placement_phones'] = ''

    updates_made = 0

    # Sequential iteration (one college at a time)
    for index, row in df.iterrows():
        name = row.get('college_name')
        url = row.get('website_url')
        raw_email_val = str(row.get('placement_emails')).strip()
        phone_val = str(row.get('placement_phones')).strip()

        if pd.isna(url) or url == 'nan' or url == '': continue
        
        # --- CELL WIPING LOGIC ---
        surviving_emails = filter_existing_cell(raw_email_val)
        
        if raw_email_val != '' and raw_email_val.lower() != 'nan' and len(surviving_emails) == 0:
            logger.warning(f"  [X] Wiping completely generic cell for {name}: '{raw_email_val}'")
            df.at[index, 'placement_emails'] = '' 
            is_email_empty = True
            updates_made += 1 
        elif len(surviving_emails) > 0:
            cleaned_string = ", ".join(surviving_emails)
            if cleaned_string != raw_email_val:
                logger.info(f"  [~] Cleaned mixed cell for {name}: Removed generic clutter.")
                df.at[index, 'placement_emails'] = cleaned_string
                updates_made += 1
            is_email_empty = False
        else:
            is_email_empty = True
            
        is_phone_empty = (phone_val == '' or phone_val.lower() == 'nan')

        # --- PROCESS IF DATA IS MISSING ---
        if is_email_empty or is_phone_empty:
            contacts = process_empty_row(name, url)
            
            if contacts:
                if contacts['emails']: 
                    all_emails = set(surviving_emails + contacts['emails'])
                    df.at[index, 'placement_emails'] = ", ".join(all_emails)
                    
                existing_phones = [p.strip() for p in phone_val.split(',')] if not is_phone_empty else []
                if contacts['phones']: 
                    all_phones = set(existing_phones + contacts['phones'])
                    df.at[index, 'placement_phones'] = ", ".join(all_phones)
                
                updates_made += 1
                
        # Save every 3 modifications
        if updates_made > 0 and updates_made % 3 == 0:
            df.to_excel(INPUT_FILE, sheet_name=SHEET_NAME, index=False)

    print(f"\n{'='*80}")
    logger.info("Script execution finished. Saving final data payload...")
    df.to_excel(INPUT_FILE, sheet_name=SHEET_NAME, index=False)
    logger.info("Process complete! Safe to exit.")

if __name__ == '__main__':
    main()