"""
UCD PolicyBot Scraper (parallel optimised)
==========================================

Same crawling and sync logic, but uses:
- Thread pool for HTTP fetching (detail pages, PDF downloads, student guides)
- Process pool for PDF → Markdown conversion
- HEAD request caching to skip unchanged files early
- Download‑to‑temp to avoid double‑fetching
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
from concurrent.futures import (
    ThreadPoolExecutor,
    ProcessPoolExecutor,
    as_completed,
)
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pymupdf4llm
import requests
import urllib3
from bs4 import BeautifulSoup

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

BASE_DIR = Path(".")
PDF_DIR = BASE_DIR / "policies_pdfs"
MD_DIR = BASE_DIR / "policies_mds"
INDEX_FILE = BASE_DIR / "index.json"

# Concurrency controls (gentle on the server)
MAX_WORKERS_NETWORK = 8          # HTTP threads
MAX_WORKERS_CONVERT = os.cpu_count() or 4   # PDF conversion processes
REQUEST_DELAY_PER_THREAD = 0.1   # seconds after each request (inside each thread)
HTTP_TIMEOUT = 20
PDF_TIMEOUT = 30
MAX_RETRIES = 3

# ── Logger ────────────────────────────────────────────────────────────────────
log = logging.getLogger("ucd_scraper")
log.setLevel(logging.INFO)
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)


# ── Helpers (unchanged logic) ─────────────────────────────────────────────────
def sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text.strip("_")[:80]

def yaml_quote(value: object) -> str:
    return json.dumps("" if value is None else str(value), ensure_ascii=False)

def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def count_pdf_pages(pdf_path: Path) -> int | None:
    if fitz is None:
        return None
    try:
        with fitz.open(str(pdf_path)) as doc:
            return int(doc.page_count)
    except Exception as e:
        log.warning(f"    Could not count PDF pages for {pdf_path.name}: {e}")
        return None

# ── HTTP helpers (with session and retries) ───────────────────────────────────
def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    s.verify = False
    return s

def _http_get(url: str, session: requests.Session | None = None,
              stream: bool = False, retries: int = MAX_RETRIES) -> requests.Response | None:
    """GET with retries. If no session given, creates a temp one."""
    close_session = False
    if session is None:
        session = _make_session()
        close_session = True
    for attempt in range(1, retries + 1):
        try:
            r = session.get(
                url,
                timeout=PDF_TIMEOUT if stream else HTTP_TIMEOUT,
                stream=stream,
            )
            r.raise_for_status()
            time.sleep(REQUEST_DELAY_PER_THREAD)   # polite per‑request pause
            return r
        except requests.RequestException as e:
            log.warning(f"Attempt {attempt}/{retries} failed for {url}: {e}")
            time.sleep(REQUEST_DELAY_PER_THREAD * attempt)
    log.error(f"All retries exhausted: {url}")
    if close_session:
        session.close()
    return None

def _head_request(url: str, session: requests.Session) -> tuple[int | None, str | None, str | None]:
    """Return (content_length, last_modified, etag) from HEAD, or (None, None, None)."""
    try:
        r = session.head(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        cl = r.headers.get("Content-Length")
        lm = r.headers.get("Last-Modified")
        et = r.headers.get("ETag")
        return (int(cl) if cl else None, lm, et)
    except Exception:
        return (None, None, None)


# ── PDF → Markdown (unchanged, but will be called in subprocess) ─────────────
def _convert_single_pdf(pdf_path_str: str, source_url: str, label: str) -> str | None:
    """Convert one PDF to Markdown. Returns the markdown string or None on failure.
    This function must be picklable → use string arguments."""
    pdf_path = Path(pdf_path_str)
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
                    text = chunk.get("text") or chunk.get("markdown") or chunk.get("md") or ""
                    metadata = chunk.get("metadata") or {}
                    page_num = metadata.get("page") or metadata.get("page_number") or metadata.get("page_num") or idx
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
        return None

    if not body:
        return None

    total_pages_value = total_pages if total_pages is not None else "unknown"
    front_matter = (
        "---\n"
        f"title: {yaml_quote(label)}\n"
        f"source_url: {yaml_quote(source_url)}\n"
        f"file: {yaml_quote(pdf_path.name)}\n"
        f"total_pages: {yaml_quote(total_pages_value)}\n"
        "doc_type: governance_pdf\n"
        "converter: pymupdf4llm\n"
        "---\n\n"
        f"# {label}\n\n"
        f"> **Source:** [{source_url}]({source_url})  \n"
        f"> **File:** `{pdf_path.name}`  \n"
        f"> **Pages:** {total_pages_value}\n\n"
        "---\n\n"
    )
    return front_matter + body + "\n"


# ── Load / save index (unchanged) ────────────────────────────────────────────
def load_index() -> dict:
    if not INDEX_FILE.exists():
        return {}
    try:
        raw = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return {Path(e["pdf_file"]).stem: e for e in raw if isinstance(e, dict) and "pdf_file" in e}
        if isinstance(raw, dict):
            return raw
        return {}
    except Exception as e:
        log.warning(f"Could not read index file {INDEX_FILE}: {e}")
        return {}

def save_index(index: dict) -> None:
    INDEX_FILE.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Governance PDF discovery (parallel detail pages) ─────────────────────────
def discover_pdf_items() -> list[dict]:
    log.info(f"Discovering PDFs from: {GOVERNANCE_URL}")
    r = _http_get(GOVERNANCE_URL)
    if not r:
        log.error("Cannot reach governance URL.")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    parsed_base = urlparse(GOVERNANCE_URL)
    base_domain = f"{parsed_base.scheme}://{parsed_base.netloc}"

    # Level 1: collect detail page links
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
    lock = threading.Lock()   # protect shared sets/lists when running in threads

    def _add_direct_pdf(a):
        href = a["href"].strip()
        if href.lower().endswith(".pdf"):
            full = urljoin(GOVERNANCE_URL, href)
            with lock:
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

    for a in soup.find_all("a", href=True):
        _add_direct_pdf(a)

    log.info(f"Found {len(detail_pages)} detail pages, {len(pdf_items)} direct PDFs on index")

    # Level 2: visit each detail page in parallel
    def _process_detail(dp):
        dp_url = dp["url"]
        session = _make_session()
        try:
            dp_r = _http_get(dp_url, session=session)
            if not dp_r:
                return
            dp_soup = BeautifulSoup(dp_r.text, "html.parser")
            pdf_url: str | None = None

            # search strategies (same as original)
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
                return

            with lock:
                if pdf_url in seen_pdf:
                    log.info(f"    [duplicate] {pdf_url}")
                    return
                seen_pdf.add(pdf_url)

            # title extraction
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
            log.info(f"    Found: {title[:50]}")

            with lock:
                pdf_items.append({
                    "label": title,
                    "pdf_url": pdf_url,
                    "detail_url": dp_url,
                    "slug": slug,
                })
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS_NETWORK) as executor:
        futures = [executor.submit(_process_detail, dp) for dp in detail_pages]
        for f in as_completed(futures):
            _ = f.result()  # propagate exceptions if any

    log.info(f"Total PDFs discovered: {len(pdf_items)}")
    return pdf_items


# ── PDF sync (download‑once, HEAD skip, parallel download, parallel conversion) ─
def sync_and_process(discovered: list[dict], index: dict) -> tuple[dict, dict]:
    stats = {"new": 0, "changed": 0, "skipped": 0, "failed": 0}
    pdf_convert_tasks = []          # collect tasks for later parallel conversion
    index_lock = threading.Lock()
    stats_lock = threading.Lock()

    def _process_pdf(item):
        slug = item["slug"]
        label = item["label"]
        pdf_url = item["pdf_url"]
        detail_url = item["detail_url"]

        pdf_path = PDF_DIR / f"{slug}.pdf"
        md_path = MD_DIR / f"{slug}.md"

        existing = index.get(slug)
        pdf_exists = pdf_path.exists()
        md_exists = md_path.exists()

        # Quick check with HEAD if both files exist and metadata present
        if pdf_exists and md_exists and existing:
            session = _make_session()
            try:
                cl, lm, et = _head_request(pdf_url, session)
                # Compare with stored values (if we have them)
                stored_cl = existing.get("content_length")
                stored_lm = existing.get("last_modified")
                # If content-length matches and we trust it, skip
                if cl is not None and stored_cl is not None and cl == stored_cl:
                    # Also check last-modified if available
                    if lm is None or stored_lm is None or lm == stored_lm:
                        log.info(f"  [SKIP]     {label[:50]}  (HEAD unchanged)")
                        with index_lock:
                            existing["status"] = "unchanged"
                            index[slug] = existing
                        with stats_lock:
                            stats["skipped"] += 1
                        return
            finally:
                session.close()

        # Need to download: temp file
        log.info(f"  [NEW/CHG]  {label[:50]}")
        session = _make_session()
        try:
            dl = _http_get(pdf_url, session=session, stream=True)
            if not dl:
                log.error(f"    FAILED download: {pdf_url}")
                with stats_lock:
                    stats["failed"] += 1
                return

            # Write to temp file
            with tempfile.NamedTemporaryFile(delete=False, dir=PDF_DIR, suffix=".pdf") as tmp:
                for chunk in dl.iter_content(chunk_size=65536):
                    if chunk:
                        tmp.write(chunk)
                temp_path = Path(tmp.name)
        finally:
            session.close()

        local_hash = sha256_of_file(temp_path)
        # Compare with stored hash
        if existing and existing.get("sha256") == local_hash:
            # Unchanged – discard temp
            temp_path.unlink()
            log.info(f"    Unchanged after download → skip {pdf_path.name}")
            with index_lock:
                existing["status"] = "unchanged"
                index[slug] = existing
            with stats_lock:
                stats["skipped"] += 1
            return

        # Changed or new: move temp to final path
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        if pdf_path.exists():
            pdf_path.unlink()
        temp_path.rename(pdf_path)

        log.info(f"    PDF saved: {pdf_path.name}  SHA-256={local_hash[:16]}…")

        # Gather metadata for index
        cl_val, lm_val, _ = _head_request(pdf_url, _make_session())
        meta = {
            "label": label,
            "pdf_url": pdf_url,
            "detail_url": detail_url,
            "pdf_file": str(pdf_path),
            "md_file": str(md_path),
            "sha256": local_hash,
            "content_length": cl_val,
            "last_modified": lm_val,
            "status": "changed" if existing else "new",
        }
        with index_lock:
            index[slug] = meta
        with stats_lock:
            if existing:
                stats["changed"] += 1
            else:
                stats["new"] += 1

        # Schedule PDF→MD conversion (to be done in process pool)
        pdf_convert_tasks.append((str(pdf_path), detail_url, label, slug, md_path))

    # Download PDFs in parallel (threads)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS_NETWORK) as executor:
        futures = [executor.submit(_process_pdf, item) for item in discovered]
        for f in as_completed(futures):
            _ = f.result()

    # Convert all gathered PDFs to Markdown in parallel using processes
    if pdf_convert_tasks:
        log.info(f"Converting {len(pdf_convert_tasks)} PDFs to Markdown in parallel...")
        with ProcessPoolExecutor(max_workers=MAX_WORKERS_CONVERT) as pool:
            # We'll submit tasks and collect results
            future_to_task = {
                pool.submit(_convert_single_pdf, path, url, label): (slug, md_path)
                for path, url, label, slug, md_path in pdf_convert_tasks
            }
            for future in as_completed(future_to_task):
                slug, md_path = future_to_task[future]
                try:
                    md = future.result()
                except Exception as e:
                    log.error(f"Conversion failed for {slug}: {e}")
                    md = None
                if md:
                    md_path.write_text(md, encoding="utf-8")
                    log.info(f"    MD saved: {md_path.name}  ({len(md):,} chars)")
                else:
                    log.warning(f"    Empty markdown for {slug} – skipping")

    return index, stats


# ── Student Guides (parallel scraping) ────────────────────────────────────────
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
            lines.append(f"{'#' * int(name[1])} {text}")
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

    seen_lines = []
    prev = None
    for line in lines:
        if line != prev:
            seen_lines.append(line)
            prev = line
    return "\n\n".join(seen_lines).strip()


def _scrape_guide_page(url: str, title: str) -> str:
    r = _http_get(url)
    if not r:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    intro_lines: list[str] = []
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
    body_parts = [f"# {page_title}\n"]
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

    r = _http_get(STUDENT_GUIDES_URL)
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
        log.warning("No student guide sub-pages found.")
        return index, stats

    log.info(f"Found {len(guide_links)} student guide pages")
    MD_DIR.mkdir(parents=True, exist_ok=True)

    index_lock = threading.Lock()
    stats_lock = threading.Lock()

    def _process_guide(guide):
        url = guide["url"]
        label = guide["label"]
        slug = "guide_" + (slugify(label) or hashlib.md5(url.encode()).hexdigest()[:12])
        md_path = MD_DIR / f"{slug}.md"

        log.info(f"  Scraping: {label[:60]}")
        md_text = _scrape_guide_page(url, label)
        if not md_text:
            log.warning(f"    FAILED or empty: {url}")
            with stats_lock:
                stats["failed"] += 1
            return

        new_hash = sha256_of_text(md_text)
        stored_hash = index.get(slug, {}).get("sha256")
        if stored_hash and stored_hash == new_hash:
            log.info(f"    [SKIP] No changes detected for {slug}.md")
            with index_lock:
                index[slug] = {
                    **index.get(slug, {}),
                    "label": label,
                    "source_url": url,
                    "md_file": str(md_path),
                    "sha256": new_hash,
                    "doc_type": "student_guide",
                    "status": "unchanged",
                }
            with stats_lock:
                stats["skipped"] += 1
            return

        action = "changed" if stored_hash else "new"
        log.info(f"    [{action.upper()}] {slug}.md")
        md_path.write_text(md_text, encoding="utf-8")
        log.info(f"    MD saved: {md_path.name}  ({len(md_text):,} chars)")

        with index_lock:
            index[slug] = {
                "label": label,
                "source_url": url,
                "md_file": str(md_path),
                "sha256": new_hash,
                "doc_type": "student_guide",
                "status": action,
            }
        with stats_lock:
            if action == "new":
                stats["new"] += 1
            else:
                stats["changed"] += 1

    with ThreadPoolExecutor(max_workers=MAX_WORKERS_NETWORK) as executor:
        futures = [executor.submit(_process_guide, guide) for guide in guide_links]
        for f in as_completed(futures):
            _ = f.result()

    log.info(f"Student Guides — New: {stats['new']}  Changed: {stats['changed']}  "
             f"Skipped: {stats['skipped']}  Failed: {stats['failed']}")
    return index, stats


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    MD_DIR.mkdir(parents=True, exist_ok=True)

    log.info("=" * 60)
    log.info("UCD PolicyBot Scraper — Governance PDFs + Student Guides (optimised)")
    log.info("=" * 60)

    index = load_index()
    log.info(f"Existing index: {len(index)} entries")

    # Phase 1: Governance PDFs
    log.info("=" * 60)
    log.info("Phase 1: Governance PDFs")
    log.info("=" * 60)
    discovered = discover_pdf_items()
    if discovered:
        index, pdf_stats = sync_and_process(discovered, index)
    else:
        log.error("No PDFs discovered.")
        pdf_stats = {"new": 0, "changed": 0, "skipped": 0, "failed": 0}

    # Phase 2: Student Guides
    index, guide_stats = discover_and_scrape_student_guides(index)

    save_index(index)

    log.info("=" * 60)
    log.info("SYNC COMPLETE")
    log.info("  [Governance PDFs]")
    log.info(f"    New     : {pdf_stats['new']}")
    log.info(f"    Changed : {pdf_stats['changed']}")
    log.info(f"    Skipped : {pdf_stats['skipped']}")
    log.info(f"    Failed  : {pdf_stats['failed']}")
    log.info("  [Student Guides]")
    log.info(f"    New     : {guide_stats['new']}")
    log.info(f"    Changed : {guide_stats['changed']}")
    log.info(f"    Skipped : {guide_stats['skipped']}")
    log.info(f"    Failed  : {guide_stats['failed']}")
    log.info(f"  PDFs dir : {PDF_DIR}")
    log.info(f"  MD dir   : {MD_DIR}")
    log.info(f"  Index    : {INDEX_FILE}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()