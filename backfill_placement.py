import os
import re
import time
import pandas as pd
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright
from ddgs import DDGS
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- CONFIGURATION ---
INPUT_FILE = 'north_india_deep_contacts.xlsx'
SHEET_NAME = 'Colleges'

# --- PATTERNS & PATHS ---
EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
PHONE_REGEX = re.compile(r'(?:(?:\+|0{0,2})91[\s-]?)?[6-9]\d{9}|0\d{2,4}[-\s]?\d{6,8}') 

PLACEMENT_KEYWORDS = [
    'placement', 'tpo', 'career', 'recruit', 'training', 'cdc', 
    'corporate', 'alumni', 'job', 'vacancy', 'opportunity', 'internship'
]

COMMON_PATHS = [
    '/training-and-placement', '/placement', '/placements', 
    '/tpo', '/career', '/careers', '/recruitments', 
    '/training', '/placement-cell', '/about-placement', 
    '/crc', '/corporate-resource-center', '/cdc', '/contact-us/placement', '/contact/placement', '/contactus/placement', '/contact-placement', '/contact/placement-officer', '/contactus/placement-officer', '/contact-placement-officer', '/placement-officer', '/tpo', '/training-and-placement', '/training-placement', '/trainingandplacement', '/training-placement-cell', '/training-and-placement-cell', '/trainingplacementcell', '/training-placement-office', '/training-and-placement-office', '/trainingplacementoffice'
]
_ILLEGAL_XLSX_CHARS_RE = re.compile(r'[\x00-\x08\x0B\x0C\x0E-\x1F]')

def _sanitize_for_excel(v):
    if isinstance(v, str): return _ILLEGAL_XLSX_CHARS_RE.sub('', v)
    return v

def _sanitize_dataframe(df):
    if df is None or df.empty: return df
    return df.apply(lambda col: col.map(_sanitize_for_excel))

# --- DIAGNOSTIC SCRAPING PIPELINE ---
def get_html(url, use_playwright=False):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'}
    
    if not use_playwright:
        try:
            resp = requests.get(url, headers=headers, timeout=10, verify=False)
            if resp.status_code == 200:
                if len(resp.text) > 500:
                    return resp.text
                else:
                    print(f"      [-] Page loaded, but HTML is too small ({len(resp.text)} bytes). Likely a bot-blocker or blank page.")
                    return None
            else:
                print(f"      [-] Failed: Server returned HTTP Status {resp.status_code}")
                return None
        except requests.exceptions.Timeout:
            print("      [-] Failed: Connection Timed Out after 10 seconds.")
            return None
        except Exception as e:
            print(f"      [-] Failed: Connection Error ({type(e).__name__})")
            return None
            
    try:
        print("      [~] Booting Playwright headless browser...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=True, user_agent=headers['User-Agent'])
            page = context.new_page()
            page.goto(url, timeout=20000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000) 
            html = page.content()
            browser.close()
            return html
    except Exception as e: 
        error_msg = str(e).splitlines()[0]
        print(f"      [-] Playwright Failed: {error_msg}")
        return None

def find_links(base_url, html, keywords):
    if not html: return []
    soup = BeautifulSoup(html, 'html.parser')
    found_links = []
    for a_tag in soup.find_all('a', href=True):
        text = a_tag.get_text().lower()
        href = a_tag['href'].lower()
        title = a_tag.get('title', '').lower()
        if any(k in text for k in keywords) or any(k in href for k in keywords) or any(k in title for k in keywords):
            found_links.append(urljoin(base_url, a_tag['href']))
    return list(set(found_links))

def search_ddgs_fallback(college_name):
    """Uses DuckDuckGo to find the placement page directly by college name."""
    # Wrapped in quotes for exact match, refined keywords
    query = f'"{college_name}" placement OR TPO contact'
    print(f"  [Step 5] Searching web for: {query}")
    
    try:
        # Pause for 2 seconds to avoid DuckDuckGo rate limits
        time.sleep(2) 
        
        with DDGS() as ddgs:
            # backend="html" is less likely to be blocked than the default API
            results = list(ddgs.text(query, max_results=3, backend="html"))
            
            if results:
                for res in results:
                    url = res.get('href', '')
                    if url and "facebook.com" not in url and "instagram.com" not in url:
                        print(f"    [+] Search Engine found fallback link: {url}")
                        return url
                        
    except Exception as e:
        print(f"    [-] DDGS Web search failed: {e}")
    
    print("    [-] Search Engine found no results. (May be rate-limited or no page exists)")
    return None


def extract_contacts(html):
    if not html: return {'emails': [], 'phones': []}
    soup = BeautifulSoup(html, 'html.parser')
    
    emails = set()
    phones = set()
    
    for a in soup.find_all('a', href=True):
        href = a['href'].lower()
        if href.startswith('mailto:'):
            emails.add(href.replace('mailto:', '').split('?')[0].strip())
        elif href.startswith('tel:'):
            phones.add(href.replace('tel:', '').strip())

    for script in soup(["script", "style"]): 
        script.extract() 
    text = soup.get_text(separator=' ')
    
    emails.update(EMAIL_REGEX.findall(text))
    phones.update(PHONE_REGEX.findall(text))
    
    clean_emails = [e for e in emails if not e.endswith(('.png', '.jpg', '.jpeg', '.gif', '.pdf'))]
    return {'emails': list(clean_emails), 'phones': list(phones)}

def process_college(college_name, home_url):
    print(f"\n{'='*60}")
    print(f"[START] {college_name}")
    print(f"[URL]   {home_url}")
    print(f"{'-'*60}")
    
    base_url = home_url.rstrip('/')
    target_url = None
    contacts = None
    
    # STEP 1: Fetch Homepage
    print("  [Step 1] Fetching Homepage...")
    home_html = get_html(base_url)
    
    if home_html:
        # STEP 2: Search for Placement Links
        print("  [Step 2] Scanning homepage for placement keywords...")
        placement_urls = find_links(base_url, home_html, PLACEMENT_KEYWORDS)
        target_url = placement_urls[0] if placement_urls else None
        
        if target_url:
            print(f"    [+] Found explicit link in HTML: {target_url}")
        else:
            print("    [-] No explicit links found in HTML.")
            
        # STEP 3: Brute Force (If needed)
        if not target_url:
            print("  [Step 3] Attempting brute-force URL guessing...")
            domain = urlparse(base_url).netloc.replace('www.', '')
            guesses = [base_url + path for path in COMMON_PATHS] + [
                f"https://placement.{domain}", f"https://tpo.{domain}", f"https://crc.{domain}"
            ]
            
            for guess_url in guesses:
                try:
                    test_resp = requests.get(guess_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3, verify=False)
                    if test_resp.status_code == 200 and len(test_resp.text) > 1000:
                        print(f"    [+] [WIN] Valid portal discovered: {guess_url}")
                        target_url = guess_url
                        break
                except: 
                    pass # Silently skip failed guesses
                    
            if not target_url:
                print("    [-] Brute-force failed. No hidden pages found.")

    else:
        print("    [-] Cannot load homepage. Will proceed directly to web search.")

    # STEP 4: Scrape the Target (if found via Step 2 or 3)
    if target_url:
        print(f"  [Step 4] Extracting contacts from: {target_url}")
        target_html = get_html(target_url, use_playwright=True) 
        if target_html:
            contacts = extract_contacts(target_html)

    # Check if we actually found valid data
    has_valid_contacts = contacts and (contacts.get('emails') or contacts.get('phones'))

    # STEP 5: DDGS Search Fallback
    if not has_valid_contacts:
        search_url = search_ddgs_fallback(college_name)
        if search_url:
            print(f"    [~] Extracting contacts from fallback link...")
            fallback_html = get_html(search_url, use_playwright=True)
            if fallback_html:
                contacts = extract_contacts(fallback_html)

    # FINAL RESULT CHECK
    # We check again because Step 5 might have updated the 'contacts' dictionary
    if contacts and (contacts.get('emails') or contacts.get('phones')):
        print(f"  [RESULT] SUCCESS! Found {len(contacts['emails'])} Emails, {len(contacts['phones'])} Phones.")
        for e in contacts['emails']: print(f"           - {e}")
        return contacts
    else:
        print("  [RESULT] FAILURE. No emails or phones found across all methods. Leaving entry empty.")
        return None
    
# --- MAIN EXCEL RUNNER ---
def main():
    if not os.path.exists(INPUT_FILE):
        print(f"Error: {INPUT_FILE} not found in this folder.")
        return

    print(f"Loading {INPUT_FILE}...")
    df = pd.read_excel(INPUT_FILE, sheet_name=SHEET_NAME, dtype=str)
    
    try: faculty_df = pd.read_excel(INPUT_FILE, sheet_name='Faculty', dtype=str)
    except Exception: faculty_df = None

    if 'placement_emails' not in df.columns: df['placement_emails'] = ''
    if 'placement_phones' not in df.columns: df['placement_phones'] = ''

    total_processed = 0
    for index, row in df.iterrows():
        url = row.get('website_url')
        name = row.get('college_name')

        if pd.isna(url) or str(url).strip() == '': continue
        if not str(url).startswith('http'): url = 'https://' + str(url)

        if not pd.isna(row.get('placement_emails')) and not pd.isna(row.get('placement_phones')):
            if str(row.get('placement_emails')).strip() != '' or str(row.get('placement_phones')).strip() != '':
                continue

        contacts = process_college(name, url)
        
        if contacts:
            if contacts['emails']: df.at[index, 'placement_emails'] = ", ".join(contacts['emails'])
            if contacts['phones']: df.at[index, 'placement_phones'] = ", ".join(contacts['phones'])
                
        total_processed += 1
        
        if total_processed % 5 == 0:
            print("\n  [Saving Checkpoint to Excel...]")
            safe_df = _sanitize_dataframe(df)
            with pd.ExcelWriter(INPUT_FILE, engine='openpyxl') as w:
                safe_df.to_excel(w, sheet_name=SHEET_NAME, index=False)
                if faculty_df is not None: faculty_df.to_excel(w, sheet_name='Faculty', index=False)

    print("\nFinished processing. Saving final data...")
    safe_df = _sanitize_dataframe(df)
    with pd.ExcelWriter(INPUT_FILE, engine='openpyxl') as w:
        safe_df.to_excel(w, sheet_name=SHEET_NAME, index=False)
        if faculty_df is not None: faculty_df.to_excel(w, sheet_name='Faculty', index=False)
    print("Done!")

if __name__ == '__main__':
    main()