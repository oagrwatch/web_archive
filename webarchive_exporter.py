#!/usr/bin/env python3
"""
Wayback Machine Content Exporter (full)

Features:
- Query Wayback CDX API for snapshots of a given domain/path
- Optional temporal filtering (user inputs dates in DD/MM/YYYY)
- Option to collect all snapshots or a user-specified number
- Progressive download with tqdm progress bar
- SSL "soft" fallback: try normal verify, then retry with verify=False on SSL errors
- Intermediate chunked saves every CHUNK_SIZE records (CSV, XLSX, JSON)
- Final save that unifies all collected records into final CSV/XLSX/JSON
- Timestamps in output files formatted as DD/MM/YYYY
- Graceful handling of KeyboardInterrupt

Usage:
    python wayback_collector_full.py

Requires:
    pip install requests pandas beautifulsoup4 tqdm openpyxl

Note: trafilatura/newspaper not used here (this script fetches archived HTML and extracts text via BeautifulSoup).
"""

import requests
import os
import json
from bs4 import BeautifulSoup
from tqdm import tqdm
from datetime import datetime
import pandas as pd
import urllib3

# suppress insecure request warnings when verify=False used
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# -------------------- Configuration --------------------
OUTPUT_PREFIX = "wayback_export"
CHUNK_SIZE = 500  # change if you want smaller/larger chunks
CDX_BASE = "http://web.archive.org/cdx/search/cdx"

# -------------------- Helper functions --------------------

def normalize_domain_input(domain_raw: str) -> str:
    """Normalize the user's domain/path input into a form usable in CDX queries.

    Accepts inputs like:
      example.com
      www.example.com
      example.com/path
      https://example.com
    Returns string without trailing slash and without protocol for CDX usage.
    """
    if not domain_raw:
        return ""
    s = domain_raw.strip()
    # strip protocol if present
    if s.startswith("http://"):
        s = s[len("http://"):]
    elif s.startswith("https://"):
        s = s[len("https://"):]
    # remove trailing slash
    s = s.rstrip('/')
    return s


def build_cdx_query(domain_path: str, from_ts: str = None, to_ts: str = None):
    """Build CDX API URL for given domain/path and optional from/to timestamps.

    The CDX parameters used:
      url={domain_path}/*
      output=json
      fl=timestamp,original
      filter=statuscode:200
      from=YYYYMMDDhhmmss (optional)
      to=YYYYMMDDhhmmss (optional)

    Returns the full URL string.
    """
    params = {
        'url': f"{domain_path}/*",
        'output': 'json',
        'fl': 'timestamp,original',
        'filter': 'statuscode:200'
    }

    # Build base query string
    query_parts = [f"url={params['url']}", f"output={params['output']}", f"fl={params['fl']}", f"filter={params['filter']}"]
    if from_ts:
        query_parts.append(f"from={from_ts}")
    if to_ts:
        query_parts.append(f"to={to_ts}")

    query = CDX_BASE + "?" + "&".join(query_parts)
    return query


def parse_date_input_ddmmyyyy(inp: str) -> datetime:
    """Parse a date string in DD/MM/YYYY and return a datetime.date object (at midnight).

    Raises ValueError on invalid input.
    """
    return datetime.strptime(inp.strip(), "%d/%m/%Y")


def ts_to_readable_date(ts: str) -> str:
    """Convert Wayback timestamp YYYYMMDDhhmmss to DD/MM/YYYY string.
    If conversion fails, return original string.
    """
    try:
        dt = datetime.strptime(ts[:14], "%Y%m%d%H%M%S")
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return ts


def extract_text_from_html(html: str) -> (str, str):
    """Extract title and cleaned text from HTML using BeautifulSoup.

    Returns (title, text). Empty strings if nothing found.
    """
    if not html:
        return "", ""
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    try:
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
    except Exception:
        title = ""

    # remove script/style/noscript elements
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    # collapse and strip
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    cleaned = "\n".join(lines)
    return title, cleaned


def safe_request_get(url: str, timeout: int = 15) -> str:
    """Try to GET a URL. On SSL errors, retry with verify=False. Returns response.text or raises.
    """
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.SSLError:
        # retry with SSL verify disabled (soft fallback)
        resp = requests.get(url, timeout=timeout, verify=False)
        resp.raise_for_status()
        return resp.text


def save_chunk(data_records, chunk_index: int):
    """Save a chunk (list of records) to CSV/XLSX/JSON files.

    Each record is a dict with keys: timestamp, original_url, archive_url, title, content
    Timestamps will be converted to DD/MM/YYYY in output.
    """
    if not data_records:
        return
    base = f"{OUTPUT_PREFIX}_chunk_{chunk_index}"
    # convert to DataFrame with readable dates
    rows = []
    for r in data_records:
        rows.append({
            'timestamp': ts_to_readable_date(r.get('timestamp', '')),
            'original_url': r.get('original_url', ''),
            'archive_url': r.get('archive_url', ''),
            'title': r.get('title', ''),
            'content': r.get('content', '')
        })
    df = pd.DataFrame(rows)
    csv_name = base + '.csv'
    xlsx_name = base + '.xlsx'
    json_name = base + '.json'
    df.to_csv(csv_name, index=False, encoding='utf-8')
    df.to_excel(xlsx_name, index=False)
    with open(json_name, 'w', encoding='utf-8') as jf:
        json.dump(rows, jf, ensure_ascii=False, indent=2)
    print(f"\n💾 Ενδιάμεση αποθήκευση chunk #{chunk_index}: {csv_name}, {xlsx_name}, {json_name}")


def save_final(all_records):
    """Save final unified output files with readable dates."""
    if not all_records:
        print("⚠️ Δεν υπάρχουν δεδομένα για τελική αποθήκευση.")
        return
    rows = []
    for r in all_records:
        rows.append({
            'timestamp': ts_to_readable_date(r.get('timestamp', '')),
            'original_url': r.get('original_url', ''),
            'archive_url': r.get('archive_url', ''),
            'title': r.get('title', ''),
            'content': r.get('content', '')
        })
    df = pd.DataFrame(rows)
    csv_name = OUTPUT_PREFIX + '_all.csv'
    xlsx_name = OUTPUT_PREFIX + '_all.xlsx'
    json_name = OUTPUT_PREFIX + '_all.json'
    df.to_csv(csv_name, index=False, encoding='utf-8')
    df.to_excel(xlsx_name, index=False)
    with open(json_name, 'w', encoding='utf-8') as jf:
        json.dump(rows, jf, ensure_ascii=False, indent=2)
    print(f"\n💾 Τελική αποθήκευση: {csv_name}, {xlsx_name}, {json_name}")

# -------------------- Main program --------------------

def main():
    print("=== Wayback Machine Content Exporter (with date filter & chunks) ===\n")

    user_input = input("🔗 Πληκτρολόγησε τη διεύθυνση (π.χ. example.com ή www.example.com/path): ").strip()
    if not user_input:
        print("❌ Δεν δόθηκε διεύθυνση. Έξοδος.")
        return
    domain_path = normalize_domain_input(user_input)

    # ask about date filtering
    print("\nΘες να περιορίσεις την αναζήτηση σε συγκεκριμένο χρονικό διάστημα;")
    print("1. Όχι — όλα τα snapshots")
    print("2. Ναι — θα δώσω ημερομηνίες (DD/MM/YYYY)")
    date_choice = input("👉 Επίλεξε (1 ή 2): ").strip()

    from_ts = None
    to_ts = None
    if date_choice == '2':
        # loop for valid start date
        while True:
            s = input("🔹 Ημερομηνία έναρξης (DD/MM/YYYY): ").strip()
            try:
                dt_s = parse_date_input_ddmmyyyy(s)
                # CDX expects YYYYMMDDhhmmss
                from_ts = dt_s.strftime('%Y%m%d') + '000000'
                break
            except Exception:
                print("⚠️ Μη έγκυρη ημερομηνία. Δοκίμασε π.χ. 01/01/1999")
        # loop for valid end date
        while True:
            s = input("🔹 Ημερομηνία λήξης (DD/MM/YYYY): ").strip()
            try:
                dt_e = parse_date_input_ddmmyyyy(s)
                to_ts = dt_e.strftime('%Y%m%d') + '235959'
                # ensure from <= to
                if from_ts and int(from_ts) > int(to_ts):
                    print("⚠️ Η ημερομηνία λήξης πρέπει να είναι μετά την ημερομηνία έναρξης.")
                    continue
                break
            except Exception:
                print("⚠️ Μη έγκυρη ημερομηνία. Δοκίμασε π.χ. 31/12/2015")

    # ask how many snapshots
    print("\nΠόσα snapshots θες να συλλεχθούν;")
    print("1. Όλα")
    print("2. Συγκεκριμένος αριθμός")
    how_many = input("👉 Επίλεξε (1 ή 2): ").strip()
    max_snapshots = None
    if how_many == '2':
        while True:
            val = input("🔢 Πληκτρολόγησε πόσα snapshots θέλεις (π.χ. 50): ").strip()
            try:
                n = int(val)
                if n > 0:
                    max_snapshots = n
                    break
            except Exception:
                pass
            print("⚠️ Γράψε έναν θετικό ακέραιο αριθμό.")

    # build CDX query
    cdx_url = build_cdx_query(domain_path, from_ts=from_ts, to_ts=to_ts)
    print(f"\n🔍 Ερώτημα στο Wayback CDX API...\n   {cdx_url}\n")

    try:
        resp = requests.get(cdx_url, timeout=20)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        print(f"❌ Σφάλμα στο αίτημα CDX API: {e}")
        return

    if len(raw) <= 1:
        print("⚠️ Το CDX API δεν επέστρεψε snapshots για τα κριτήρια αυτά.")
        return

    rows = raw[1:]
    # optionally limit by user-specified max_snapshots
    if max_snapshots is not None:
        rows = rows[:max_snapshots]

    total = len(rows)
    print(f"✅ Βρέθηκαν {total} snapshots (θα επιχειρηθούν λήψεις).\n")

    all_records = []
    chunk_records = []
    chunk_index = 1

    try:
        for item in tqdm(rows, desc='Λήψη snapshot σελίδων', unit='snap'):
            try:
                timestamp, original = item
            except Exception:
                tqdm.write("⚠️ Παράλειψη μη αναμενόμενου αρχείου CDX entry")
                continue

            archive_url = f"https://web.archive.org/web/{timestamp}/{original}"

            try:
                html = safe_request_get(archive_url, timeout=15)
                title, content = extract_text_from_html(html)
                if not content.strip():
                    raise ValueError("Κενό περιεχόμενο μετά το parsing")

                rec = {
                    'timestamp': timestamp,
                    'original_url': original,
                    'archive_url': archive_url,
                    'title': title,
                    'content': content
                }
                all_records.append(rec)
                chunk_records.append(rec)

                # save chunk when reached CHUNK_SIZE
                if len(chunk_records) >= CHUNK_SIZE:
                    save_chunk(chunk_records, chunk_index)
                    chunk_index += 1
                    chunk_records = []

            except Exception as e:
                tqdm.write(f"⚠️ Παράλειψη {archive_url} ({e})")
                continue

    except KeyboardInterrupt:
        print("\n⏹️ Εκτέλεση διακόπηκε από τον χρήστη. Αποθηκεύονται όσα συγκεντρώθηκαν...")

    finally:
        # save any remaining chunk
        if chunk_records:
            save_chunk(chunk_records, chunk_index)
        # save final unified files
        save_final(all_records)
        print(f"\nΣυνολικά αρχεία που σώθηκαν: {len(all_records)}")


if __name__ == '__main__':
    main()

