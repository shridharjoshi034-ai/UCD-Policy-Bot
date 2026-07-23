"""
UCD PolicyBot Scraper (Supabase Storage version)
=================================================

Source 1 — Governance PDFs:
https://hub.ucd.ie/usis/W_HU_REPORTING.P_DISPLAY_QUERY?p_query=GD110-1

Two-level crawl:
  Level 1 — index page → collect all detail page links
  Level 2 — each detail page → find "Download Document" → PDF URL

Source 2 — Student Guides (HTML → Markdown):
https://www.ucd.ie/studentadvisers/studentguides/

Two-level crawl:
  Level 1 — index page → collect all guide category links + slugs
  Level 2 — each guide page → scrape all section content → write one .md per guide

Sync logic (governance PDFs):
  - SHA-256 hash of remote file vs hash stored in index.json (in Supabase)
  - New file     → download PDF (in-memory) + convert to Markdown + upload both to Supabase
  - Changed file → re-download + re-convert + overwrite both in Supabase
  - Unchanged    → skip (no network download of the PDF body, no upload)

Sync logic (student guides):
  - SHA-256 hash of scraped text vs hash stored in index.json (in Supabase)
  - New/changed  → overwrite <slug>.md in the Supabase `markdown/` folder
  - Unchanged    → skip (no upload)

Storage layout (Supabase Storage bucket, no local files written for scraped content):
  <SUPABASE_BUCKET>/
    pdfs/          ← all downloaded PDFs (governance only)
    markdown/      ← all converted/scraped Markdowns (both sources)
    index.json     ← sync-state file stored in Supabase (NOT on local disk)

PDF → Markdown conversion uses pymupdf4llm (needs a real file path, so each
PDF is written to a temporary file only for the duration of the conversion,
then deleted — it is never persisted under a project folder).
HTML → Markdown conversion uses BeautifulSoup + markdownify.

All Supabase operations go through the shared app.services.storage_service.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pymupdf4llm
import requests
import urllib3
from bs4 import BeautifulSoup
from app.services import storage_service

try:
    from markdownify import markdownify as md_convert
except ImportError:
    md_convert = None

try:
    import fitz  # type: ignore
except Exception:
    fitz = None

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── Config ────────────────────────────────────────────────────────────────────
BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}

GOVERNANCE_URL = "https://hub.ucd.ie/usis/W_HU_REPORTING.P_DISPLAY_QUERY?p_query=GD110-1"
STUDENT_GUIDES_URL = "https://www.ucd.ie/studentadvisers/studentguides/"

BUCKET_PDF_FOLDER = "pdfs"
BUCKET_MD_FOLDER = "markdown"


def validate_credentials() -> None:
    """
    Fail fast, with a clear message, if Supabase is unreachable.
    Also ensures the bucket exists (creates it if needed).
    """
    try:
        bucket = storage_service.ensure_bucket_exists()
        log.info(f"Supabase credentials OK — bucket '{bucket}' is reachable.")
    except Exception as e:
        raise RuntimeError(
            f"Supabase rejected the credentials/bucket during a startup check: {e}\n"
            "Check that:\n"
            "  - SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY are set in .env\n"
            "  - SUPABASE_SERVICE_ROLE_KEY is the service_role key (not anon), copied in full\n"
            "  - SUPABASE_URL matches Settings → API → Project URL exactly\n"
        ) from e


def upload_to_supabase(bucket_folder: str, filename: str, data: bytes, content_type: str) -> str:
    """Upload raw bytes to <bucket_folder>/<filename> in the Supabase bucket (upsert). Returns storage path."""
    dest_path = f"{bucket_folder}/{filename}"
    storage_service.upload_file(dest_path, data, content_type, upsert=True)
    return dest_path


def sha256_of_text(text: str) -> str:
    """SHA-256 hash of a string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


DELAY = 1.0
PDF_TIMEOUT = 30
HTTP_TIMEOUT = 20
MAX_RETRIES = 3
MAX_WORKERS = 3
UPLOAD_SEMAPHORE = threading.BoundedSemaphore(2)  # max 2 concurrent Supabase uploads


# ── Logger ────────────────────────────────────────────────────────────────────
log = logging.getLogger("ucd_scraper")
log.setLevel(logging.INFO)
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)


# ── Helpers ───────────────────────────────────────────────────────────────────
def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text.strip("_")[:80]


def yaml_quote(value: object) -> str:
    """Return a simple YAML-safe quoted scalar without requiring PyYAML."""
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def http_get(url: str, stream: bool = False, retries: int = MAX_RETRIES) -> requests.Response | None:
    """GET with retries. SSL verify=False for UCD institutional certs."""
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


def download_bytes(url: str) -> bytes | None:
    """Stream a remote file fully into memory and return its bytes."""
    r = http_get(url, stream=True)
    if not r:
        return None
    try:
        buf = bytearray()
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                buf.extend(chunk)
        return bytes(buf)
    except Exception as e:
        log.warning(f"    Download failed for {url}: {e}")
        return None
    finally:
        r.close()


def sha256_of_url(url: str) -> str | None:
    """Stream the remote file and compute SHA-256 without keeping it in memory twice."""
    r = http_get(url, stream=True)
    if not r:
        return None
    try:
        h = hashlib.sha256()
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        log.warning(f"    SHA-256 fetch failed for {url}: {e}")
        return None
    finally:
        r.close()


def _safe_unlink(path: Path, retries: int = 5, delay: float = 0.3) -> None:
    """
    Delete a temp file, retrying briefly on Windows if another process (e.g.
    a lingering handle inside pymupdf4llm/fitz) still has it open. Logs a
    warning rather than crashing if it truly can't be deleted — a leftover
    OS temp file is harmless, unlike crashing mid-run.
    """
    for attempt in range(1, retries + 1):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == retries:
                log.warning(f"    Could not delete temp file {path} (still locked) — leaving it in OS temp dir")
                return
            time.sleep(delay)


def count_pdf_pages(pdf_path: Path) -> int | None:
    """Count pages with PyMuPDF if available. pdf_path here is a temp file."""
    if fitz is None:
        return None
    try:
        with fitz.open(str(pdf_path)) as doc:
            return int(doc.page_count)
    except Exception as e:
        log.warning(f"    Could not count PDF pages for {pdf_path.name}: {e}")
        return None


# ── PDF → Markdown via pymupdf4llm ────────────────────────────────────────────
def pdf_to_markdown(pdf_path: Path, source_url: str, label: str) -> str:
    """Convert a (temporary, local) PDF file to Markdown using pymupdf4llm with YAML front matter."""
    total_pages = count_pdf_pages(pdf_path)

    try:
        try:
            chunks = pymupdf4llm.to_markdown(str(pdf_path), page_chunks=True)
        except TypeError:
            chunks = None

        pages_md: list[str] = []

        if isinstance(chunks, list):
            if total_pages is None:
                total_pages = len(chunks)

            for idx, chunk in enumerate(chunks, start=1):
                if isinstance(chunk, dict):
                    text = (
                        chunk.get("text")
                        or chunk.get("markdown")
                        or chunk.get("md")
                        or ""
                    )
                    metadata = chunk.get("metadata") or {}
                    page_num = (
                        metadata.get("page")
                        or metadata.get("page_number")
                        or metadata.get("page_num")
                        or idx
                    )
                else:
                    text = str(chunk)
                    page_num = idx

                text = str(text).strip()
                if not text:
                    continue

                page_total = total_pages if total_pages is not None else "?"
                pages_md.append(f"###### Page {page_num} of {page_total}\n\n{text}")

            body = "\n\n---\n\n".join(pages_md).strip()
        else:
            body = str(pymupdf4llm.to_markdown(str(pdf_path))).strip()

    except Exception as e:
        log.error(f"pymupdf4llm failed on {pdf_path}: {e}")
        return ""

    used_fallback = False
    if not body:
        # ── Fallback: try direct text extraction with fitz (PyMuPDF) ──────────
        if fitz is not None:
            try:
                fitz_body = _extract_text_with_fitz(pdf_path)
                if fitz_body:
                    log.info(f"    Recovered {len(fitz_body):,} chars via fitz fallback for {pdf_path.name}")
                    body = fitz_body
                    used_fallback = True
            except Exception as e:
                log.warning(f"    fitz fallback also failed for {pdf_path.name}: {e}")

    if not body:
        return ""

    total_pages_value = total_pages if total_pages is not None else "unknown"
    converter = "pymupdf+fitz_fallback" if used_fallback else "pymupdf4llm"

    front_matter = (
        "---\n"
        f"title: {yaml_quote(label)}\n"
        f"source_url: {yaml_quote(source_url)}\n"
        f"file: {yaml_quote(pdf_path.name)}\n"
        f"total_pages: {yaml_quote(total_pages_value)}\n"
        "doc_type: governance_pdf\n"
        f"converter: {yaml_quote(converter)}\n"
        "---\n\n"
        f"# {label}\n\n"
        f"> **Source:** [{source_url}]({source_url})  \n"
        f"> **File:** `{pdf_path.name}`  \n"
        f"> **Pages:** {total_pages_value}\n\n"
        "---\n\n"
    )

    return front_matter + body + "\n"


def _extract_text_with_fitz(pdf_path: Path) -> str:
    """Fallback: extract plain text page-by-page using PyMuPDF (fitz).
    Returns empty string if no text can be extracted (image-only PDF)."""
    if fitz is None:
        return ""
    pages_text: list[str] = []
    with fitz.open(str(pdf_path)) as doc:
        total = int(doc.page_count)
        for i, page in enumerate(doc, start=1):
            text = page.get_text().strip()
            if text:
                pages_text.append(f"###### Page {i} of {total}\n\n{text}")
    if not pages_text:
        return ""
    return "\n\n---\n\n".join(pages_text)


# ── Load / save index (stored in Supabase bucket) ──────────────────────────────
def load_index() -> dict:
    """Download index.json from Supabase bucket. Returns empty dict if not found."""
    return storage_service.download_index_json()


def save_index(index: dict) -> None:
    """Upload index.json to Supabase bucket root."""
    storage_service.upload_index_json(index)


# ── Level 1+2: Discover all Governance PDFs ──────────────────────────────────
def discover_pdf_items() -> list[dict]:
    log.info(f"Discovering PDFs from: {GOVERNANCE_URL}")

    r = http_get(GOVERNANCE_URL)
    if not r:
        log.error("Cannot reach governance URL.")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    parsed_base = urlparse(GOVERNANCE_URL)
    base_domain = f"{parsed_base.scheme}://{parsed_base.netloc}"

    # Level 1: detail page links
    detail_pages: list[dict] = []
    seen_detail = {GOVERNANCE_URL}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        href_lower = href.lower()

        if href.startswith("#") or href_lower.startswith(("mailto:", "tel:", "javascript:")):
            continue

        full = urljoin(GOVERNANCE_URL, href)
        if (
            full.startswith(base_domain)
            and full not in seen_detail
            and not href_lower.endswith(".pdf")
        ):
            seen_detail.add(full)
            detail_pages.append({"url": full, "label": a.get_text(strip=True) or full})

    # Direct PDFs on index page
    pdf_items: list[dict] = []
    seen_pdf: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().endswith(".pdf"):
            full = urljoin(GOVERNANCE_URL, href)
            if full not in seen_pdf:
                seen_pdf.add(full)
                label = a.get_text(strip=True) or Path(urlparse(full).path).stem
                slug = slugify(label) or hashlib.md5(full.encode()).hexdigest()[:12]
                pdf_items.append(
                    {
                        "label": label,
                        "pdf_url": full,
                        "detail_url": GOVERNANCE_URL,
                        "slug": slug,
                    }
                )

    log.info(f"Found {len(detail_pages)} detail pages, {len(pdf_items)} direct PDFs on index")

    # Level 2: visit each detail page (parallelized for speed)
    if detail_pages:
        log.info(f"  Crawling {len(detail_pages)} detail pages in parallel ({MAX_WORKERS} workers)...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_crawl_one_detail, dp): dp for dp in detail_pages}
            for future in as_completed(futures):
                result = future.result()
                if result is None:
                    continue
                pdf_url, title, dp_url = result
                if pdf_url in seen_pdf:
                    log.info(f"    [duplicate] {pdf_url}")
                    continue
                seen_pdf.add(pdf_url)
                slug = slugify(title) or hashlib.md5(pdf_url.encode()).hexdigest()[:12]
                log.info(f"    Found: {title[:50]}")
                pdf_items.append({
                    "label": title,
                    "pdf_url": pdf_url,
                    "detail_url": dp_url,
                    "slug": slug,
                })

    log.info(f"Total PDFs discovered: {len(pdf_items)}")
    return pdf_items


def _crawl_one_detail(dp: dict) -> tuple[str, str, str] | None:
    """Crawl a single detail page and return (pdf_url, title, dp_url) or None."""
    dp_url = dp["url"]
    log.info(f"  Detail: {dp['label'][:60]}")

    dp_r = http_get(dp_url)
    if not dp_r:
        return None

    dp_soup = BeautifulSoup(dp_r.text, "html.parser")
    pdf_url: str | None = None

    # Strategy 1: "Download Document" link text
    for a in dp_soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        href = a["href"].strip()
        if "download" in text and ("document" in text or href.lower().endswith(".pdf")):
            pdf_url = urljoin(dp_url, href)
            break

    # Strategy 2: any .pdf href
    if not pdf_url:
        for a in dp_soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().endswith(".pdf"):
                pdf_url = urljoin(dp_url, href)
                break

    # Strategy 3: href containing download/getfile keywords
    if not pdf_url:
        for a in dp_soup.find_all("a", href=True):
            href = a["href"].strip()
            href_lower = href.lower()
            if any(x in href_lower for x in ["download", "getfile", "document", "policy"]):
                pdf_url = urljoin(dp_url, href)
                break

    if not pdf_url:
        log.warning(f"    No PDF found: {dp_url}")
        return None

    # Extract title from detail page table, if present
    title = dp["label"]
    for row in dp_soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) >= 2:
            key = cells[0].get_text(strip=True).lower()
            val = cells[1].get_text(strip=True)
            if "title" in key and val:
                title = val
                break

    return (pdf_url, title, dp_url)


# ── Sync: compare via SHA-256, download+upload new/changed to Supabase ───────
def _process_one_pdf(item: dict, index: dict, bucket_name: str) -> dict | None:
    """Process a single PDF item: check hash, download if new/changed, convert, upload.
    Returns updated index entry dict or None on failure/skip."""
    slug = item["slug"]
    label = item["label"]
    pdf_url = item["pdf_url"]
    detail_url = item["detail_url"]

    existing = index.get(slug)
    stored_hash = existing.get("sha256") if existing else None

    remote_hash = sha256_of_url(pdf_url)

    if remote_hash is None:
        log.warning(f"    Could not compute remote hash — skipping this run: {pdf_url}")
        return {"slug": slug, "action": "failed", "entry": None}

    if existing and stored_hash == remote_hash:
        log.info(f"    [SKIP]     {label[:50]}  (SHA-256 match)")
        return {
            "slug": slug,
            "action": "skipped",
            "entry": {**existing, "label": label, "pdf_url": pdf_url, "detail_url": detail_url, "status": "unchanged"},
        }

    action = "changed" if existing else "new"
    log.info(f"    [{action.upper()}]  {label[:50]}")

    # ── Download PDF bytes ───────────────────────────────────────────────────
    pdf_bytes = download_bytes(pdf_url)
    if pdf_bytes is None:
        log.error(f"    DOWNLOAD FAILED: {pdf_url}")
        return {"slug": slug, "action": "download_failed", "entry": None}

    # ── Size check before upload (Supabase free tier: 50 MB) ─────────────────
    MAX_SIZE_BYTES = 45 * 1024 * 1024  # 45 MB to stay safely under the limit
    if len(pdf_bytes) > MAX_SIZE_BYTES:
        log.error(f"    FILE TOO LARGE ({len(pdf_bytes):,} bytes > {MAX_SIZE_BYTES:,} limit): {slug}")
        return {"slug": slug, "action": "oversized", "entry": None}

    local_hash = sha256_of_bytes(pdf_bytes)
    pdf_filename = f"{slug}.pdf"
    md_filename = f"{slug}.md"

    # ── Convert to Markdown (needs a real file path → use a temp file) ───────
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = Path(tmp.name)

    empty_pages = None
    try:
        md_text = pdf_to_markdown(tmp_path, detail_url, label)
        if not md_text:
            empty_pages = count_pdf_pages(tmp_path)
    except Exception as e:
        log.error(f"    CONVERSION ERROR for {slug}: {e}", exc_info=True)
        return {"slug": slug, "action": "conversion_error", "entry": None}
    finally:
        _safe_unlink(tmp_path)

    if not md_text:
        log.warning(f"    EMPTY CONVERSION: {pdf_filename}  (pages={empty_pages}, size={len(pdf_bytes):,} bytes, url={pdf_url[:80]})")
        return {"slug": slug, "action": "conversion_failed", "entry": None}

    # ── Upload PDF + Markdown to Supabase Storage (with concurrency limit) ────
    if not UPLOAD_SEMAPHORE.acquire(timeout=300):
        log.error(f"    Upload semaphore timeout (deadlock?) for {slug}")
        return {"slug": slug, "action": "upload_failed", "entry": None}
    try:
        pdf_path_in_bucket = upload_to_supabase(BUCKET_PDF_FOLDER, pdf_filename, pdf_bytes, "application/pdf")
        md_path_in_bucket = upload_to_supabase(
            BUCKET_MD_FOLDER, md_filename, md_text.encode("utf-8"), "text/markdown"
        )
    except Exception as e:
        log.error(f"    Supabase upload failed for {slug}: {e}", exc_info=True)
        return {"slug": slug, "action": "upload_failed", "entry": None}
    finally:
        UPLOAD_SEMAPHORE.release()

    log.info(f"    Uploaded:  {bucket_name}/{pdf_path_in_bucket}")
    log.info(f"    Uploaded:  {bucket_name}/{md_path_in_bucket}  ({len(md_text):,} chars)")

    return {
        "slug": slug,
        "action": action,
        "entry": {
            "label": label,
            "pdf_url": pdf_url,
            "detail_url": detail_url,
            "pdf_file": pdf_path_in_bucket,
            "md_file": md_path_in_bucket,
            "sha256": local_hash,
            "status": action,
        },
    }


def sync_and_process(discovered: list[dict], index: dict) -> tuple[dict, dict]:
    """Sync governance PDFs in parallel using a thread pool."""
    bucket_name = storage_service.get_bucket_name()
    stats = {"new": 0, "changed": 0, "skipped": 0, "failed": 0,
             "download_failed": 0, "conversion_failed": 0, "conversion_error": 0,
             "upload_failed": 0, "oversized": 0}

    total = len(discovered)
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(_process_one_pdf, item, index, bucket_name): item
            for item in discovered
        }
        for future in as_completed(futures):
            done += 1
            result = future.result()
            if result is None:
                stats["failed"] += 1
            else:
                action = result["action"]
                if action in ("new", "changed"):
                    if result["entry"]:
                        index[result["slug"]] = result["entry"]
                    stats[action] += 1
                elif action == "skipped":
                    if result["entry"]:
                        index[result["slug"]] = result["entry"]
                    stats["skipped"] += 1
                elif action in ("download_failed", "conversion_failed", "conversion_error",
                               "upload_failed", "oversized"):
                    stats[action] += 1
                    stats["failed"] += 1
                else:
                    stats["failed"] += 1

            pct = done * 100 // total
            log.info(f"  📄 PDF {done}/{total} ({pct}%) — {stats['new']} new, {stats['skipped']} skip, {stats['failed']} fail")

    return index, stats


# ── Student Guides: HTML → Markdown ──────────────────────────────────────────
def _html_block_to_markdown(element) -> str:
    if md_convert is not None:
        try:
            return md_convert(
                str(element),
                heading_style="ATX",
                bullets="-",
                strip=["script", "style", "noscript"],
            ).strip()
        except Exception:
            pass

    lines: list[str] = []
    for tag in element.find_all(True):
        name = tag.name.lower() if tag.name else ""
        text = tag.get_text(" ", strip=True)
        if not text:
            continue

        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(name[1])
            lines.append(f"{'#' * level} {text}")
        elif name == "p":
            lines.append(text)
        elif name in ("li",):
            lines.append(f"- {text}")
        elif name == "a":
            href = tag.get("href", "")
            if href and not href.startswith("#"):
                lines.append(f"[{text}]({href})")
        elif name in ("div", "section", "article", "ul", "ol", "nav"):
            continue
        else:
            lines.append(text)

    seen_lines: list[str] = []
    prev = None
    for line in lines:
        if line != prev:
            seen_lines.append(line)
            prev = line

    return "\n\n".join(seen_lines).strip()


def _scrape_guide_page(url: str, title: str) -> str:
    # Try up to 2 times for flaky student guide pages (http_get already retries 3x internally)
    r = None
    for attempt in range(1, 3):
        r = http_get(url, retries=1)
        if r:
            break
        if attempt < 2:
            log.warning(f"    Guide page attempt {attempt}/2 failed, retrying: {url}")
            time.sleep(3)
    if not r:
        log.warning(f"    Cannot fetch guide page after 2 attempts: {url}")
        return ""

    soup = BeautifulSoup(r.text, "html.parser")

    h1 = soup.find("h1")
    page_title = h1.get_text(strip=True) if h1 else title

    main = (
        soup.find("main")
        or soup.find("div", {"id": "main-content"})
        or soup.find("div", class_=re.compile(r"content|main|body", re.I))
        or soup.body
    )

    intro_paras: list[str] = []
    if main:
        for tag in main.children:
            if not hasattr(tag, "name") or tag.name is None:
                continue
            if tag.name == "p":
                text = tag.get_text(strip=True)
                if text:
                    intro_paras.append(text)
            elif tag.name in ("details", "section") or (
                tag.name == "div"
                and any(
                    cls
                    for cls in (tag.get("class") or [])
                    if re.search(r"accordion|expand|panel|collapse", cls, re.I)
                )
            ):
                break

    sections: list[tuple[str, str]] = []

    details_tags = main.find_all("details") if main else []
    if details_tags:
        for details in details_tags:
            summary = details.find("summary")
            heading = summary.get_text(strip=True) if summary else "Section"

            body_clone = BeautifulSoup(str(details), "html.parser").find("details")
            if body_clone:
                s = body_clone.find("summary")
                if s:
                    s.decompose()
                body_md = _html_block_to_markdown(body_clone)
            else:
                body_md = ""

            if body_md:
                sections.append((heading, body_md))

    if not sections and main:
        accordion_containers = main.find_all("div", class_=re.compile(r"accordion", re.I))
        for container in accordion_containers:
            items = container.find_all(
                "div", class_=re.compile(r"accordion.?item|panel|collapse", re.I)
            ) or container.find_all("div", recursive=False)

            for item in items:
                heading_tag = item.find(re.compile(r"h[2-4]"))
                heading = heading_tag.get_text(strip=True) if heading_tag else ""
                if not heading:
                    btn = item.find("button") or item.find(class_=re.compile(r"toggle|title|trigger", re.I))
                    heading = btn.get_text(strip=True) if btn else "Section"

                item_clone = BeautifulSoup(str(item), "html.parser")
                h = item_clone.find(re.compile(r"h[2-4]"))
                if h:
                    h.decompose()
                body_md = _html_block_to_markdown(item_clone)

                if body_md:
                    sections.append((heading, body_md))

    if not sections and main:
        log.info("    Using generic h2/h3 section fallback")
        current_heading: str | None = None
        current_body_tags: list = []

        def flush_section():
            nonlocal current_heading, current_body_tags
            if current_heading and current_body_tags:
                combined = BeautifulSoup(
                    "<div>" + "".join(str(t) for t in current_body_tags) + "</div>",
                    "html.parser",
                )
                body_md = _html_block_to_markdown(combined)
                if body_md:
                    sections.append((current_heading, body_md))
            current_heading = None
            current_body_tags = []

        for tag in main.find_all(True, recursive=False):
            if tag.name in ("h2", "h3"):
                flush_section()
                current_heading = tag.get_text(strip=True)
            elif current_heading:
                current_body_tags.append(tag)

        flush_section()

    front_matter = (
        "---\n"
        f"title: {yaml_quote(page_title)}\n"
        f"source_url: {yaml_quote(url)}\n"
        "doc_type: student_guide\n"
        "converter: html_scraper\n"
        "---\n\n"
    )

    body_parts: list[str] = [f"# {page_title}\n"]

    if intro_paras:
        body_parts.append("\n".join(intro_paras))

    for heading, body_md in sections:
        body_parts.append(f"## {heading}\n\n{body_md}")

    if not sections and not intro_paras:
        log.warning(f"    No structured content found for {url} — dumping body text")
        body_parts.append(main.get_text("\n", strip=True) if main else "")

    return front_matter + "\n\n".join(body_parts) + "\n"


def discover_and_scrape_student_guides(index: dict) -> tuple[dict, dict]:
    stats = {"new": 0, "changed": 0, "skipped": 0, "failed": 0}

    log.info("=" * 60)
    log.info(f"Student Guides Scraper — {STUDENT_GUIDES_URL}")
    log.info("=" * 60)

    r = http_get(STUDENT_GUIDES_URL)
    if not r:
        log.error("Cannot reach Student Guides index page.")
        return index, stats

    soup = BeautifulSoup(r.text, "html.parser")
    parsed_base = urlparse(STUDENT_GUIDES_URL)
    base_domain = f"{parsed_base.scheme}://{parsed_base.netloc}"

    guide_links: list[dict] = []
    seen_urls: set[str] = {STUDENT_GUIDES_URL}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()

        if href.startswith("#") or href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue

        full_url = urljoin(STUDENT_GUIDES_URL, href)

        if (
            full_url.startswith(base_domain)
            and "/studentadvisers/" in full_url
            and full_url not in seen_urls
            and not full_url.lower().endswith((".pdf", ".docx", ".xlsx"))
        ):
            seen_urls.add(full_url)
            label = a.get_text(strip=True)

            if not label or len(label) < 2:
                parent_text = a.find_parent().get_text(strip=True) if a.find_parent() else ""
                label = parent_text[:80] if parent_text else full_url

            guide_links.append({"url": full_url, "label": label})

    if not guide_links:
        log.warning("No student guide sub-pages found on index page.")
        return index, stats

    log.info(f"Found {len(guide_links)} student guide pages")

    def _process_one_guide(guide: dict) -> dict | None:
        """Scrape and upload a single student guide page."""
        url = guide["url"]
        label = guide["label"]
        slug = "guide_" + (slugify(label) or hashlib.md5(url.encode()).hexdigest()[:12])
        md_filename = f"{slug}.md"

        log.info(f"  Scraping: {label[:60]}")

        md_text = _scrape_guide_page(url, label)
        if not md_text:
            log.warning(f"    FAILED or empty: {url}")
            return {"slug": slug, "action": "failed", "entry": None}

        new_hash = sha256_of_text(md_text)
        return {
            "slug": slug,
            "action": "check",
            "entry": {
                "label": label,
                "source_url": url,
                "md_filename": md_filename,
                "md_text": md_text,
                "sha256": new_hash,
            },
        }

    bucket_name = storage_service.get_bucket_name()

    # Phase 1: scrape all guide pages in parallel
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        scrape_futures = {executor.submit(_process_one_guide, guide): guide for guide in guide_links}
        scrape_results: list[dict] = []
        for future in as_completed(scrape_futures):
            result = future.result()
            if result is None or result["action"] == "failed":
                stats["failed"] += 1
                continue
            scrape_results.append(result)

    # Phase 2: hash-check and upload (sequential to avoid race conditions on index)
    total = len(scrape_results)
    done = 0
    for result in scrape_results:
        done += 1
        entry = result["entry"]
        slug = result["slug"]
        label = entry["label"]
        url = entry["source_url"]
        md_filename = entry["md_filename"]
        md_text = entry["md_text"]
        new_hash = entry["sha256"]

        stored_hash = index.get(slug, {}).get("sha256")

        if stored_hash and stored_hash == new_hash:
            stats["skipped"] += 1
            index[slug] = {
                **index.get(slug, {}),
                "label": label,
                "source_url": url,
                "sha256": new_hash,
                "doc_type": "student_guide",
                "status": "unchanged",
            }
        else:
            action = "changed" if stored_hash else "new"
            log.info(f"    [{action.upper()}] {slug}.md")

            try:
                md_path_in_bucket = upload_to_supabase(
                    BUCKET_MD_FOLDER, md_filename, md_text.encode("utf-8"), "text/markdown"
                )
            except Exception as e:
                log.error(f"    Supabase upload failed for {slug}: {e}")
                stats["failed"] += 1
            else:
                log.info(f"    Uploaded: {bucket_name}/{md_path_in_bucket}  ({len(md_text):,} chars)")
                index[slug] = {
                    "label": label,
                    "source_url": url,
                    "md_file": md_path_in_bucket,
                    "sha256": new_hash,
                    "doc_type": "student_guide",
                    "status": action,
                }
                stats["new" if action == "new" else "changed"] += 1

        pct = done * 100 // total
        log.info(f"  📝 Guide {done}/{total} ({pct}%) — {stats['new']} new, {stats['skipped']} skip, {stats['failed']} fail")

    log.info(f"Student Guides — New: {stats['new']}  Changed: {stats['changed']}  "
             f"Skipped: {stats['skipped']}  Failed: {stats['failed']}")

    return index, stats


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    log.info("=" * 60)
    log.info("UCD PolicyBot Scraper — Governance PDFs + Student Guides → Supabase Storage")
    log.info("=" * 60)

    validate_credentials()

    index = load_index()
    log.info(f"Existing index: {len(index)} entries")

    # ── Source 1: Governance PDFs ─────────────────────────────────────────────
    log.info("=" * 60)
    log.info("Phase 1: Governance PDFs")
    log.info("=" * 60)
    discovered = discover_pdf_items()
    if discovered:
        index, pdf_stats = sync_and_process(discovered, index)
        # Save index immediately after governance PDFs (so re-runs skip already-uploaded files)
        save_index(index)
        log.info("  Index saved after governance PDFs.")
    else:
        log.error("No PDFs discovered.")
        pdf_stats = {"new": 0, "changed": 0, "skipped": 0, "failed": 0}

    # ── Source 2: Student Guides ──────────────────────────────────────────────
    index, guide_stats = discover_and_scrape_student_guides(index)

    # Final index save (student guides + everything)
    save_index(index)
    log.info("  Final index saved.")

    # ── Summary ───────────────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("SYNC COMPLETE")
    log.info("  [Governance PDFs]")
    log.info(f"    New              : {pdf_stats['new']}")
    log.info(f"    Changed          : {pdf_stats['changed']}")
    log.info(f"    Skipped          : {pdf_stats['skipped']}")
    log.info(f"    Failed (total)   : {pdf_stats['failed']}")
    if any(pdf_stats.get(k) for k in ("download_failed", "conversion_failed", "conversion_error", "upload_failed", "oversized")):
        log.info(f"      ─ download    : {pdf_stats.get('download_failed', 0)}")
        log.info(f"      ─ conversion  : {pdf_stats.get('conversion_failed', 0)} (empty) / {pdf_stats.get('conversion_error', 0)} (error)")
        log.info(f"      ─ upload      : {pdf_stats.get('upload_failed', 0)}")
        log.info(f"      ─ too large   : {pdf_stats.get('oversized', 0)}")
    log.info("  [Student Guides]")
    log.info(f"    New     : {guide_stats['new']}")
    log.info(f"    Changed : {guide_stats['changed']}")
    log.info(f"    Skipped : {guide_stats['skipped']}")
    log.info(f"    Failed  : {guide_stats['failed']}")
    bucket = storage_service.get_bucket_name()
    log.info(f"  Supabase bucket : {bucket}")
    log.info(f"  PDFs folder     : {BUCKET_PDF_FOLDER}/")
    log.info(f"  Markdown folder : {BUCKET_MD_FOLDER}/")
    log.info(f"  Index           : {bucket}/index.json")
    log.info("=" * 60)


if __name__ == "__main__":
    main()