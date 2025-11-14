#!/usr/bin/env python3
"""
Wayback Machine Content Exporter — Advanced (Trafilatura + Readability + Boilerplate removal)

Features:
- Query Wayback CDX API for snapshots of a given domain/path
- Optional temporal filtering (user inputs dates in DD/MM/YYYY)
- Option to collect all snapshots or a user-specified number
- Progressive download with tqdm progress bar
- SSL "soft" fallback: try normal verify, then retry with verify=False on SSL errors
- Intermediate chunked saves every CHUNK_SIZE records (CSV, XLSX, JSON) with RAW content
- Final save that unifies and CLEANS all collected records into final CSV/XLSX/JSON
- Timestamps in output files formatted as DD/MM/YYYY
- Advanced cleaning:
    * primary extraction with trafilatura
    * fallback with readability-lxml
    * fallback with BeautifulSoup
    * post-processing: remove repeated boilerplate lines across pages, date-only lines,
      navigation words (Δείτε, αναλυτικά), very short lines, footer/contact blocks
- Graceful handling of KeyboardInterrupt

Usage:
    python wayback_collector_advanced.py

Requires:
    pip install requests pandas beautifulsoup4 tqdm openpyxl trafilatura readability-lxml lxml
"""

import requests
import os
import json
import re
from bs4 import BeautifulSoup
from tqdm import tqdm
from datetime import datetime
import pandas as pd
import urllib3
import trafilatura
from readability import Document
from collections import defaultdict

# suppress insecure request warnings when verify=False used
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# -------------------- Configuration --------------------
OUTPUT_PREFIX = "wayback_export"
CHUNK_SIZE = 500  # change if you want smaller/larger chunks
CDX_BASE = "http://web.archive.org/cdx/search/cdx"

# Boilerplate detection thresholds
BOILERPLATE_MIN_PAGES = 3        # minimal distinct pages a line must appear in to be candidate
BOILERPLATE_RATIO = 0.15        # or appear on >= 15% of pages -> considered boilerplate

# Heuristic thresholds
MIN_LINE_LENGTH = 20            # lines shorter than this (chars) often navigation/junk
MIN_WORDS_LINE = 3              # lines with fewer words than this are often navigation

# Regex for date-only lines (e.g., 26/07/04 or 2004-07-26)
RE_DATE_LIKE = re.compile(r'^(?:\d{1,2}[\/\-\.\s]\d{1,2}[\/\-\.\s]\d{2,4}|\d{4}[\/\-\.\s]\d{1,2}[\/\-\.\s]\d{1,2})$')
RE_EMAIL = re.compile(r'\b[\w\.-]+@[\w\.-]+\.\w+\b')
RE_PHONE = re.compile(r'(\+?\d[\d\-\s\(\)]{5,}\d)')
RE_COPYRIGHT = re.compile(r'©|copyright|Δήλωση|Προστασία|Τηλ:|Fax:|Fax|Τηλέφωνο', re.I)
NAV_WORDS = set(['Δείτε', 'αναλυτικά', 'αναλυτικα', 'Αρχική', 'Περισσότερα', 'Read', 'More', '»', '‹', '›', '...'])

# -------------------- Helper functions --------------------

def normalize_domain_input(domain_raw: str) -> str:
    """Normalize the user's domain/path input into a form usable in CDX queries."""
    if not domain_raw:
        return ""
    s = domain_raw.strip()
    if s.startswith("http://"):
        s = s[len("http://"):]
    elif s.startswith("https://"):
        s = s[len("https://"):]
    return s.rstrip('/')


def build_cdx_query(domain_path: str, from_ts: str = None, to_ts: str = None):
    params = {
        'url': f"{domain_path}/*",
        'output': 'json',
        'fl': 'timestamp,original',
        'filter': 'statuscode:200'
    }
    query_parts = [f"url={params['url']}", f"output={params['output']}", f"fl={params['fl']}", f"filter={params['filter']}"]
    if from_ts:
        query_parts.append(f"from={from_ts}")
    if to_ts:
        query_parts.append(f"to={to_ts}")
    query = CDX_BASE + "?" + "&".join(query_parts)
    return query


def parse_date_input_ddmmyyyy(inp: str) -> datetime:
    return datetime.strptime(inp.strip(), "%d/%m/%Y")


def ts_to_readable_date(ts: str) -> str:
    try:
        dt = datetime.strptime(ts[:14], "%Y%m%d%H%M%S")
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return ts


def extract_with_trafilatura(url_or_html: str, is_html=False):
    """Attempt extraction with trafilatura.
    If is_html=True, url_or_html is HTML string; else it's a URL string and we fetch it via trafilatura.fetch_url."""
    try:
        if is_html:
            downloaded = url_or_html
            res = trafilatura.extract(downloaded, include_comments=False, include_tables=False, include_formatting=False)
            meta = trafilatura.extract_metadata(downloaded)
        else:
            downloaded = trafilatura.fetch_url(url_or_html)
            if not downloaded:
                return "", ""
            res = trafilatura.extract(downloaded, include_comments=False, include_tables=False, include_formatting=False)
            meta = trafilatura.extract_metadata(downloaded)
        title = ""
        if meta and hasattr(meta, 'get'):
            title = meta.get('title', '') if meta else ''
        if res:
            return title or "", res.strip()
    except Exception:
        pass
    return "", ""


def extract_with_readability(html: str):
    try:
        doc = Document(html)
        title = doc.short_title() or ""
        summary = doc.summary()
        soup = BeautifulSoup(summary, "html.parser")
        text = soup.get_text(separator="\n")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return title, "\n".join(lines)
    except Exception:
        return "", ""


def extract_with_bs4(html: str):
    try:
        soup = BeautifulSoup(html, "html.parser")
        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        text = soup.get_text(separator="\n")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return title, "\n".join(lines)
    except Exception:
        return "", ""


def safe_request_get(url: str, timeout: int = 15) -> str:
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.SSLError:
        resp = requests.get(url, timeout=timeout, verify=False)
        resp.raise_for_status()
        return resp.text


def looks_like_date_line(line: str) -> bool:
    return bool(RE_DATE_LIKE.match(line.strip()))


def is_junk_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    # email or phone
    if RE_EMAIL.search(s) or RE_PHONE.search(s):
        return True
    # copyright/contact
    if RE_COPYRIGHT.search(s):
        return True
    # navigation words alone
    if s in NAV_WORDS:
        return True
    # arrows or short UI strings
    if s in ('»', '«', '›', '‹', '...'):
        return True
    # date-like lines
    if looks_like_date_line(s):
        return True
    # very short or very few words
    if len(s) < MIN_LINE_LENGTH or len(s.split()) < MIN_WORDS_LINE:
        return True
    return False


def save_chunk_raw(records, chunk_index: int):
    """Save chunk with RAW content (before boilerplate removal)."""
    if not records:
        return
    base = f"{OUTPUT_PREFIX}_chunk_raw_{chunk_index}"
    rows = []
    for r in records:
        rows.append({
            'timestamp': ts_to_readable_date(r.get('timestamp', '')),
            'original_url': r.get('original_url', ''),
            'archive_url': r.get('archive_url', ''),
            'title': r.get('title', ''),
            'raw_content': r.get('raw_content', '')
        })
    df = pd.DataFrame(rows)
    csv_name = base + '.csv'
    xlsx_name = base + '.xlsx'
    json_name = base + '.json'
    df.to_csv(csv_name, index=False, encoding='utf-8')
    df.to_excel(xlsx_name, index=False)
    with open(json_name, 'w', encoding='utf-8') as jf:
        json.dump(rows, jf, ensure_ascii=False, indent=2)
    print(f"\n💾 Ενδιάμεση raw αποθήκευση chunk #{chunk_index}: {csv_name}, {xlsx_name}, {json_name}")


def save_final_clean(records):
    """After cleaning, save final CSV/XLSX/JSON with cleaned 'content' field and readable timestamps."""
    if not records:
        print("⚠️ Δεν υπάρχουν δεδομένα για τελική αποθήκευση.")
        return
    rows = []
    for r in records:
        rows.append({
            'timestamp': ts_to_readable_date(r.get('timestamp', '')),
            'original_url': r.get('original_url', ''),
            'archive_url': r.get('archive_url', ''),
            'title': r.get('title', ''),
            'content': r.get('content', '')
        })
    df = pd.DataFrame(rows)
    csv_name = OUTPUT_PREFIX + '_all_clean.csv'
    xlsx_name = OUTPUT_PREFIX + '_all_clean.xlsx'
    json_name = OUTPUT_PREFIX + '_all_clean.json'
    df.to_csv(csv_name, index=False, encoding='utf-8')
    df.to_excel(xlsx_name, index=False)
    with open(json_name, 'w', encoding='utf-8') as jf:
        json.dump(rows, jf, ensure_ascii=False, indent=2)
    print(f"\n💾 Τελική καθαρή αποθήκευση: {csv_name}, {xlsx_name}, {json_name}")


# -------------------- Main program --------------------

def main():
    print("=== Wayback Machine Content Exporter (Advanced) ===\n")
    user_input = input("🔗 Πληκτρολόγησε τη διεύθυνση (π.χ. example.com ή www.example.com/path): ").strip()
    if not user_input:
        print("❌ Δεν δόθηκε διεύθυνση. Έξοδος.")
        return
    domain_path = normalize_domain_input(user_input)

    # date filter
    print("\nΘες να περιορίσεις την αναζήτηση σε συγκεκριμένο χρονικό διάστημα;")
    print("1. Όχι — όλα τα snapshots")
    print("2. Ναι — θα δώσω ημερομηνίες (DD/MM/YYYY)")
    date_choice = input("👉 Επίλεξε (1 ή 2): ").strip()

    from_ts = None
    to_ts = None
    if date_choice == '2':
        while True:
            s = input("🔹 Ημερομηνία έναρξης (DD/MM/YYYY): ").strip()
            try:
                dt_s = parse_date_input_ddmmyyyy(s)
                from_ts = dt_s.strftime('%Y%m%d') + '000000'
                break
            except Exception:
                print("⚠️ Μη έγκυρη ημερομηνία. Δοκίμασε π.χ. 01/01/1999")
        while True:
            s = input("🔹 Ημερομηνία λήξης (DD/MM/YYYY): ").strip()
            try:
                dt_e = parse_date_input_ddmmyyyy(s)
                to_ts = dt_e.strftime('%Y%m%d') + '235959'
                if from_ts and int(from_ts) > int(to_ts):
                    print("⚠️ Η ημερομηνία λήξης πρέπει να είναι μετά την ημερομηνία έναρξης.")
                    continue
                break
            except Exception:
                print("⚠️ Μη έγκυρη ημερομηνία. Δοκίμασε π.χ. 31/12/2015")

    # how many snapshots
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
    if max_snapshots is not None:
        rows = rows[:max_snapshots]

    total = len(rows)
    print(f"✅ Βρέθηκαν {total} snapshots (θα επιχειρηθούν λήψεις).\n")

    all_records = []      # list of dicts with timestamp, original_url, archive_url, title, raw_content, content (cleaned later)
    chunk_buffer = []
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
            except Exception as e:
                tqdm.write(f"⚠️ Παράλειψη (λ.λ. αίτημα) {archive_url} ({e})")
                continue

            # Attempt extraction: trafilatura -> readability -> bs4 fallback
            title, main_text = "", ""
            try:
                # try trafilatura on HTML string
                title, main_text = extract_with_trafilatura(html, is_html=True)
            except Exception:
                title, main_text = "", ""

            if not main_text.strip():
                # try readability
                try:
                    title2, main_text2 = extract_with_readability(html)
                    if main_text2 and len(main_text2) > len(main_text):
                        title = title2 or title
                        main_text = main_text2
                except Exception:
                    pass

            if not main_text.strip():
                # bs4 fallback
                try:
                    title3, main_text3 = extract_with_bs4(html)
                    if main_text3:
                        title = title3 or title
                        main_text = main_text3
                except Exception:
                    pass

            if not main_text.strip():
                tqdm.write(f"⚠️ Παράλειψη (κενό περιεχόμενο) {archive_url}")
                continue

            # store raw content (pre-clean)
            rec = {
                'timestamp': timestamp,
                'original_url': original,
                'archive_url': archive_url,
                'title': title or "",
                'raw_content': main_text,
                'content': ""   # placeholder for cleaned
            }
            all_records.append(rec)
            chunk_buffer.append(rec)

            # chunk raw save
            if len(chunk_buffer) >= CHUNK_SIZE:
                save_chunk_raw(chunk_buffer, chunk_index)
                chunk_index += 1
                chunk_buffer = []

    except KeyboardInterrupt:
        print("\n⏹️ Εκτέλεση διακόπηκε από τον χρήστη. Θα γίνει αποθήκευση των δεδομένων που έχουν συλλεχθεί...")

    finally:
        # save remaining raw chunk
        if chunk_buffer:
            save_chunk_raw(chunk_buffer, chunk_index)

    # If no records collected
    if not all_records:
        print("⚠️ Δεν συλλέχθηκαν εγγραφές. Τέλος.")
        return

    # -------------------- Advanced post-processing / boilerplate detection --------------------
    print("\n🔎 Εκτέλεση προηγμένης ανίχνευσης boilerplate και καθαρισμού...")

    # Build index: line -> set(page_indices)
    line_pages = defaultdict(set)
    page_lines = []  # list of lists (per page)
    for idx, rec in enumerate(all_records):
        lines = [ln.strip() for ln in rec['raw_content'].splitlines() if ln.strip()]
        page_lines.append(lines)
        unique_lines = set(lines)
        for ln in unique_lines:
            if len(ln) < 3:
                continue
            # normalize some whitespace and punctuation for detection
            nl = re.sub(r'\s+', ' ', ln).strip()
            line_pages[nl].add(idx)

    num_pages = len(all_records)
    # detect boilerplate candidates
    boilerplate_lines = set()
    for ln, pageset in line_pages.items():
        count = len(pageset)
        if count >= BOILERPLATE_MIN_PAGES or (count / num_pages) >= BOILERPLATE_RATIO:
            # also filter out lines that are short but repeat often (menus)
            boilerplate_lines.add(ln)

    # Expand boilerplate patterns by heuristics: small variations (lower/strip punctuation)
    expanded_boilerplate = set(boilerplate_lines)
    for ln in list(boilerplate_lines):
        lnl = ln.lower()
        # also consider stripped punctuation version
        s = re.sub(r'[^\w\s]', '', lnl).strip()
        if s and s != lnl:
            expanded_boilerplate.add(s)

    # Now clean each page: remove boilerplate lines and junk lines
    cleaned_count = 0
    for idx, rec in enumerate(all_records):
        raw = rec['raw_content']
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        cleaned_lines = []
        for ln in lines:
            norm = re.sub(r'\s+', ' ', ln).strip()
            norm_low = norm.lower()
            short_norm = re.sub(r'[^\w\s]', '', norm_low).strip()
            # skip if matches boilerplate (exact or normalized)
            if norm in boilerplate_lines or norm_low in boilerplate_lines or short_norm in expanded_boilerplate:
                continue
            # skip if junk heuristics
            if is_junk_line(norm):
                continue
            cleaned_lines.append(norm)
        # post-processing: merge consecutive short lines if they form sentences?
        # simple join
        final_text = "\n".join(cleaned_lines).strip()
        # if after cleaning the text is very small, fall back to raw but filtered minimal junk removal
        if len(final_text) < 100:
            # try lighter cleaning: remove pure junk lines only
            lite = [ln for ln in lines if not is_junk_line(ln)]
            final_text = "\n".join(lite).strip()
        rec['content'] = final_text
        if final_text:
            cleaned_count += 1

    print(f"✅ Καθαρίστηκαν κείμενα για {cleaned_count}/{num_pages} σελίδες.")

    # -------------------- Save final cleaned outputs --------------------
    save_final_clean(all_records)
    print(f"\nΟλοκληρώθηκε — συνολικά σελίδες που σώθηκαν: {len(all_records)}")


if __name__ == '__main__':
    main()

