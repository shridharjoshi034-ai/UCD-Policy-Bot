import re
import json
import time
import hashlib
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import pdfplumber
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}

GOVERNANCE_URL = "https://hub.ucd.ie/usis/W_HU_REPORTING.P_DISPLAY_QUERY?p_query=GD110-1"

OUT_DIR = Path(".")
PDF_DIR = OUT_DIR / "pdfs"
MD_DIR = OUT_DIR / "markdown"
INDEX_FILE = OUT_DIR / "index.json"

DELAY = 1
PDF_TIMEOUT = 30
HTTP_TIMEOUT = 20
MAX_RETRIES = 3

log = logging.getLogger("ucd_scraper")
log.setLevel(logging.INFO)
log.addHandler(logging.StreamHandler())


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text[:80]


def http_get(url: str, stream=False, retries=MAX_RETRIES):
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(
                url,
                headers=BASE_HEADERS,
                timeout=PDF_TIMEOUT if stream else HTTP_TIMEOUT,
                stream=stream,
                allow_redirects=True,
                verify=False,
            )
            r.raise_for_status()
            time.sleep(DELAY)
            return r
        except requests.RequestException as e:
            log.warning(f"Attempt {attempt}/{retries} failed for {url}: {e}")
            time.sleep(DELAY * attempt)
    log.error(f"All retries exhausted: {url}")
    return None


def get_remote_size(url: str) -> int | None:
    try:
        r = requests.head(
            url, headers=BASE_HEADERS, timeout=HTTP_TIMEOUT,
            allow_redirects=True, verify=False,
        )
        cl = r.headers.get("Content-Length")
        if cl:
            return int(cl)

        r2 = requests.get(
            url, headers=BASE_HEADERS, timeout=HTTP_TIMEOUT,
            stream=True, allow_redirects=True, verify=False,
        )
        cl = r2.headers.get("Content-Length")
        r2.close()
        if cl:
            return int(cl)

        log.info(f"    Size check: downloading full file to measure ({url[-60:]})")
        r3 = requests.get(
            url, headers=BASE_HEADERS, timeout=PDF_TIMEOUT,
            stream=True, allow_redirects=True, verify=False,
        )
        size = sum(len(chunk) for chunk in r3.iter_content(chunk_size=8192))
        return size if size > 0 else None

    except Exception as e:
        log.warning(f"    Size check failed for {url}: {e}")
        return None


def pdf_to_markdown(pdf_path: Path, source_url: str, label: str) -> str:
    pages_md = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)
            for page_num, page in enumerate(pdf.pages, start=1):
                tables = page.extract_tables()
                table_texts = []
                if tables:
                    for table in tables:
                        if not table:
                            continue
                        md_rows = []
                        for i, row in enumerate(table):
                            cells = [str(c).strip() if c else "" for c in row]
                            md_rows.append("| " + " | ".join(cells) + " |")
                            if i == 0:
                                md_rows.append("| " + " | ".join(["---"] * len(cells)) + " |")
                        table_texts.append("\n".join(md_rows))

                raw = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
                raw = re.sub(r"-\n([a-z])", r"\1", raw)
                raw = re.sub(r"\n{3,}", "\n\n", raw)
                raw = "\n".join(l.rstrip() for l in raw.splitlines())

                promoted = []
                for line in raw.splitlines():
                    s = line.strip()
                    if not s:
                        promoted.append("")
                    elif re.match(r"^\d+(\.\d+)?\s+[A-Z]", s) and len(s) < 100:
                        depth = s.count(".")
                        promoted.append(f"{'##' if depth == 0 else '###'} {s}")
                    elif s.isupper() and 3 < len(s) < 80:
                        promoted.append(f"## {s.title()}")
                    else:
                        promoted.append(line)

                page_text = "\n".join(promoted).strip()
                page_block = f"###### Page {page_num} of {total_pages}\n\n{page_text}"
                if table_texts:
                    page_block += "\n\n" + "\n\n".join(table_texts)
                pages_md.append(page_block)

    except Exception as e:
        log.error(f"pdfplumber failed on {pdf_path}: {e}")
        return ""

    body = "\n\n---\n\n".join(pages_md)
    front_matter = (
        f"---\n"
        f"title: {label}\n"
        f"source_url: {source_url}\n"
        f"file: {pdf_path.name}\n"
        f"total_pages: {total_pages}\n"
        f"doc_type: governance_pdf\n"
        f"---\n\n"
        f"# {label}\n\n"
        f"> **Source:** [{source_url}]({source_url})  \n"
        f"> **File:** `{pdf_path.name}`  \n"
        f"> **Pages:** {total_pages}\n\n"
        f"---\n\n"
    )
    return front_matter + body


def load_index() -> dict:
    if not INDEX_FILE.exists():
        return {}
    try:
        raw = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return {Path(e["pdf_file"]).stem: e for e in raw if "pdf_file" in e}
        return raw
    except Exception:
        return {}


def save_index(index: dict):
    INDEX_FILE.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


def discover_pdf_items() -> list[dict]:
    log.info(f"Discovering PDFs from: {GOVERNANCE_URL}")
    r = http_get(GOVERNANCE_URL)
    if not r:
        log.error("Cannot reach governance URL.")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    base_domain = f"{urlparse(GOVERNANCE_URL).scheme}://{urlparse(GOVERNANCE_URL).netloc}"
    detail_pages = []
    seen_detail = {GOVERNANCE_URL}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("#") or "mailto:" in href:
            continue
        full = urljoin(GOVERNANCE_URL, href)
        if full.startswith(base_domain) and full not in seen_detail and not href.lower().endswith(".pdf"):
            seen_detail.add(full)
            detail_pages.append({"url": full, "label": a.get_text(strip=True) or full})

    pdf_items = []
    seen_pdf = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().endswith(".pdf"):
            full = urljoin(GOVERNANCE_URL, href)
            if full not in seen_pdf:
                seen_pdf.add(full)
                label = a.get_text(strip=True) or Path(urlparse(full).path).stem
                slug = slugify(label) or hashlib.md5(full.encode()).hexdigest()[:12]
                pdf_items.append({
                    "label": label,
                    "pdf_url": full,
                    "detail_url": GOVERNANCE_URL,
                    "slug": slug,
                })

    log.info(f"Found {len(detail_pages)} detail pages, {len(pdf_items)} direct PDFs on index")

    for dp in detail_pages:
        dp_url = dp["url"]
        log.info(f"  Detail: {dp['label'][:60]}")
        dp_r = http_get(dp_url)
        if not dp_r:
            continue

        dp_soup = BeautifulSoup(dp_r.text, "html.parser")
        pdf_url = None

        for a in dp_soup.find_all("a", href=True):
            text = a.get_text(strip=True).lower()
            href = a["href"].strip()
            if "download" in text and ("document" in text or href.lower().endswith(".pdf")):
                pdf_url = urljoin(dp_url, href)
                break

        if not pdf_url:
            for a in dp_soup.find_all("a", href=True):
                if a["href"].strip().lower().endswith(".pdf"):
                    pdf_url = urljoin(dp_url, a["href"].strip())
                    break

        if not pdf_url:
            for a in dp_soup.find_all("a", href=True):
                href = a["href"].strip().lower()
                if any(x in href for x in ["download", "getfile", "document", "policy"]):
                    pdf_url = urljoin(dp_url, a["href"].strip())
                    break

        if not pdf_url:
            log.warning(f"    No PDF found: {dp_url}")
            continue

        if pdf_url in seen_pdf:
            log.info(f"    [duplicate] {pdf_url}")
            continue
        seen_pdf.add(pdf_url)

        title = dp["label"]
        for row in dp_soup.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) >= 2:
                key = cells[0].get_text(strip=True).lower()
                val = cells[1].get_text(strip=True)
                if "title" in key and val:
                    title = val
                    break

        slug = slugify(title) or hashlib.md5(pdf_url.encode()).hexdigest()[:12]
        log.info(f"    Found PDF: {title[:50]}")
        pdf_items.append({
            "label": title,
            "pdf_url": pdf_url,
            "detail_url": dp_url,
            "slug": slug,
        })

    log.info(f"Total PDFs discovered: {len(pdf_items)}")
    return pdf_items


def sync_and_process(discovered: list[dict], index: dict) -> dict:
    stats = {"new": 0, "changed": 0, "skipped": 0, "failed": 0}

    for item in discovered:
        slug = item["slug"]
        label = item["label"]
        pdf_url = item["pdf_url"]
        detail_url = item["detail_url"]

        pdf_path = PDF_DIR / f"{slug}.pdf"
        md_path = MD_DIR / f"{slug}.md"
        existing = index.get(slug)
        pdf_exists = pdf_path.exists()
        md_exists = md_path.exists()

        if pdf_exists and md_exists and existing:
            remote_size = get_remote_size(pdf_url)
            local_size = pdf_path.stat().st_size
            if remote_size is not None and remote_size != local_size:
                action = "changed"
                log.info(f"  [CHANGED]  {label[:50]}  (local={local_size}B remote={remote_size}B)")
            elif remote_size is None:
                action = "skip"
                log.info(f"  [SKIP]     {label[:50]}  (cannot verify size, files present)")
            else:
                action = "skip"
                log.info(f"  [SKIP]     {label[:50]}  (unchanged)")
        else:
            action = "new"
            log.info(f"  [NEW]      {label[:50]}")

        if action == "skip":
            stats["skipped"] += 1
            if slug not in index:
                index[slug] = {
                    "label": label,
                    "pdf_url": pdf_url,
                    "detail_url": detail_url,
                    "pdf_file": str(pdf_path),
                    "md_file": str(md_path),
                    "size_bytes": pdf_path.stat().st_size if pdf_exists else None,
                    "status": "unchanged",
                }
            continue

        log.info(f"    Downloading: {pdf_url}")
        dl = http_get(pdf_url, stream=True)
        if not dl:
            log.error(f"    FAILED download: {pdf_url}")
            stats["failed"] += 1
            continue

        with open(pdf_path, "wb") as f:
            for chunk in dl.iter_content(chunk_size=8192):
                f.write(chunk)

        actual_size = pdf_path.stat().st_size
        log.info(f"    Saved PDF: {pdf_path.name} ({actual_size:,} bytes)")

        md = pdf_to_markdown(pdf_path, detail_url, label)
        if not md:
            log.warning(f"    SKIP markdown (empty): {pdf_path.name}")
            stats["failed"] += 1
            continue

        md_path.write_text(md, encoding="utf-8")
        log.info(f"    Saved MD:  {md_path.name} ({len(md):,} chars)")

        index[slug] = {
            "label": label,
            "pdf_url": pdf_url,
            "detail_url": detail_url,
            "pdf_file": str(pdf_path),
            "md_file": str(md_path),
            "size_bytes": actual_size,
            "status": action,
        }

        if action == "new":
            stats["new"] += 1
        else:
            stats["changed"] += 1

    return index, stats


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    MD_DIR.mkdir(parents=True, exist_ok=True)
    log.info("=" * 60)
    log.info("UCD PolicyBot Scraper — Governance PDFs")
    log.info("=" * 60)

    index = load_index()
    log.info(f"Existing index: {len(index)} entries")

    discovered = discover_pdf_items()
    if not discovered:
        log.error("No PDFs discovered. Exiting.")
        return

    index, stats = sync_and_process(discovered, index)
    save_index(index)

    log.info("=" * 60)
    log.info("SYNC COMPLETE")
    log.info(f"  New       : {stats['new']}")
    log.info(f"  Changed   : {stats['changed']}")
    log.info(f"  Skipped   : {stats['skipped']}")
    log.info(f"  Failed    : {stats['failed']}")
    log.info(f"  Total PDFs: {len(index)}")
    log.info(f"  PDFs dir  : {PDF_DIR}")
    log.info(f"  MD dir    : {MD_DIR}")
    log.info(f"  Index     : {INDEX_FILE}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()