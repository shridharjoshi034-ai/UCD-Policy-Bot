"""
UCD PolicyBot Scraper
======================

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
  - SHA-256 hash of remote file vs local file
  - New file     → download PDF + convert to Markdown
  - Changed file → overwrite PDF + overwrite Markdown
  - Unchanged    → skip

Sync logic (student guides):
  - SHA-256 hash of scraped text vs hash stored in index.json
  - New/changed  → overwrite .md in policies_mds/
  - Unchanged    → skip (no write)

Output layout:
  rag_pipeline/
    policies_pdfs/      ← all downloaded PDFs (governance only)
    policies_mds/       ← all converted/scraped Markdowns
    index.json

PDF → Markdown conversion uses pymupdf4llm
HTML → Markdown conversion uses BeautifulSoup + markdownify.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
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

def sha256_of_text(text: str) -> str:
    """SHA-256 hash of a string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

DELAY = 1.5
PDF_TIMEOUT = 30
HTTP_TIMEOUT = 20
MAX_RETRIES = 3


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


def sha256_of_file(path: Path) -> str:
    """SHA-256 hash of a local file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_url(url: str) -> str | None:
    """Stream the remote file and compute SHA-256."""
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



def is_changed(pdf_path: Path, pdf_url: str, stored_hash: str | None) -> tuple[bool, str | None]:
    """
    Compare local file vs remote using SHA-256.
    Returns (changed: bool, remote_hash: str | None).
    """
    local_hash = sha256_of_file(pdf_path)

    if stored_hash and stored_hash != local_hash:
        log.info("    Local file hash mismatch (corrupted/modified?) → re-download")
        remote_hash = sha256_of_url(pdf_url)
        return True, remote_hash

    log.info("    Computing remote SHA-256 …")
    remote_hash = sha256_of_url(pdf_url)
    if remote_hash is None:
        log.warning("    Cannot compute remote hash — assuming unchanged")
        return False, None

    return remote_hash != local_hash, remote_hash


def count_pdf_pages(pdf_path: Path) -> int | None:
    """Count pages with PyMuPDF if available."""
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
    """Convert PDF to Markdown using pymupdf4llm with YAML front matter."""
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

    if not body:
        return ""

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


# ── Load / save index ─────────────────────────────────────────────────────────
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

    # Level 2: visit each detail page
    for dp in detail_pages:
        dp_url = dp["url"]
        log.info(f"  Detail: {dp['label'][:60]}")

        dp_r = http_get(dp_url)
        if not dp_r:
            continue

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
            continue

        if pdf_url in seen_pdf:
            log.info(f"    [duplicate] {pdf_url}")
            continue
        seen_pdf.add(pdf_url)

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

        slug = slugify(title) or hashlib.md5(pdf_url.encode()).hexdigest()[:12]
        log.info(f"    Found: {title[:50]}")

        pdf_items.append(
            {
                "label": title,
                "pdf_url": pdf_url,
                "detail_url": dp_url,
                "slug": slug,
            }
        )

    log.info(f"Total PDFs discovered: {len(pdf_items)}")
    return pdf_items


# ── Sync: compare via SHA-256, download new/changed ──────────────────────────
def sync_and_process(discovered: list[dict], index: dict) -> tuple[dict, dict]:
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

        # ── Determine action ──────────────────────────────────────────────────
        if pdf_exists and md_exists and existing:
            stored_hash = existing.get("sha256")
            changed, remote_hash = is_changed(pdf_path, pdf_url, stored_hash)

            if changed:
                action = "changed"
                log.info(f"  [CHANGED]  {label[:50]}")
            else:
                action = "skip"
                log.info(f"  [SKIP]     {label[:50]}  (SHA-256 match)")

                existing.update(
                    {
                        "label": label,
                        "pdf_url": pdf_url,
                        "detail_url": detail_url,
                        "pdf_file": str(pdf_path),
                        "md_file": str(md_path),
                        "sha256": remote_hash or stored_hash,
                        "status": "unchanged",
                    }
                )
                stats["skipped"] += 1
                continue
        else:
            action = "new"
            log.info(f"  [NEW]      {label[:50]}")

        # ── Download PDF ──────────────────────────────────────────────────────
        log.info(f"    Downloading: {pdf_url}")
        dl = http_get(pdf_url, stream=True)
        if not dl:
            log.error(f"    FAILED: {pdf_url}")
            stats["failed"] += 1
            continue

        try:
            with pdf_path.open("wb") as f:
                for chunk in dl.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
        finally:
            dl.close()

        local_hash = sha256_of_file(pdf_path)
        log.info(f"    PDF saved:  {pdf_path.name}  SHA-256={local_hash[:16]}…")

        # ── Convert to Markdown ───────────────────────────────────────────────
        md = pdf_to_markdown(pdf_path, detail_url, label)
        if not md:
            log.warning(f"    SKIP markdown (empty): {pdf_path.name}")
            stats["failed"] += 1
            continue

        md_path.write_text(md, encoding="utf-8")
        log.info(f"    MD saved:   {md_path.name}  ({len(md):,} chars)")

        # ── Update index ──────────────────────────────────────────────────────
        index[slug] = {
            "label": label,
            "pdf_url": pdf_url,
            "detail_url": detail_url,
            "pdf_file": str(pdf_path),
            "md_file": str(md_path),
            "sha256": local_hash,
            "status": action,
        }

        stats["new" if action == "new" else "changed"] += 1

    return index, stats


# ── Student Guides: HTML → Markdown ──────────────────────────────────────────

def _html_block_to_markdown(element) -> str:
    """
    Convert a BeautifulSoup element to clean Markdown text.

    Prefers markdownify if installed; falls back to a simple text extraction
    that still preserves basic structure (headings, lists, links).
    """
    if md_convert is not None:
        try:
            return md_convert(
                str(element),
                heading_style="ATX",
                bullets="-",
                strip=["script", "style", "noscript"],
            ).strip()
        except Exception:
            pass  # fall through to manual extraction

    # --- manual fallback ---
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
        # block-level containers: let children handle output
        elif name in ("div", "section", "article", "ul", "ol", "nav"):
            continue
        else:
            lines.append(text)

    # Deduplicate consecutive identical lines produced by nested tag traversal
    seen_lines: list[str] = []
    prev = None
    for line in lines:
        if line != prev:
            seen_lines.append(line)
            prev = line

    return "\n\n".join(seen_lines).strip()


def _scrape_guide_page(url: str, title: str) -> str:
    """
    Scrape a single Student Guide category page and return its Markdown content.

    Page structure (from screenshots):
      - H1 / intro paragraph at top
      - "Open All" accordion — each accordion item is a section heading + body

    Strategy:
      1. Extract the page intro (title + description paragraph).
      2. Find all accordion/expandable sections — try multiple CSS patterns
         used by UCD's CMS (details/summary, div[class*=accordion], etc.).
      3. For each section: H2 heading + scraped body content.
      4. Assemble with YAML front matter.
    """
    r = http_get(url)
    if not r:
        log.warning(f"    Cannot fetch guide page: {url}")
        return ""

    soup = BeautifulSoup(r.text, "html.parser")

    # ── Intro ─────────────────────────────────────────────────────────────────
    intro_lines: list[str] = []

    # H1 on the page
    h1 = soup.find("h1")
    page_title = h1.get_text(strip=True) if h1 else title

    # Lead paragraph (first <p> in the main content area)
    main = (
        soup.find("main")
        or soup.find("div", {"id": "main-content"})
        or soup.find("div", class_=re.compile(r"content|main|body", re.I))
        or soup.body
    )

    intro_paras: list[str] = []
    if main:
        # Collect <p> tags before the first accordion/details/section divider
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
                break  # reached accordion area

    # ── Section extraction ────────────────────────────────────────────────────
    sections: list[tuple[str, str]] = []  # (heading, body_markdown)

    # Pattern A: <details><summary>Heading</summary>…body…</details>
    details_tags = main.find_all("details") if main else []
    if details_tags:
        for details in details_tags:
            summary = details.find("summary")
            heading = summary.get_text(strip=True) if summary else "Section"

            # Remove summary from clone so body text is clean
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

    # Pattern B: div[class*="accordion"] children
    if not sections and main:
        accordion_containers = main.find_all(
            "div", class_=re.compile(r"accordion", re.I)
        )
        for container in accordion_containers:
            # Each accordion item: first heading tag = section title, rest = body
            items = container.find_all(
                "div", class_=re.compile(r"accordion.?item|panel|collapse", re.I)
            ) or container.find_all("div", recursive=False)

            for item in items:
                heading_tag = item.find(re.compile(r"h[2-4]"))
                heading = heading_tag.get_text(strip=True) if heading_tag else ""
                if not heading:
                    btn = item.find("button") or item.find(class_=re.compile(r"toggle|title|trigger", re.I))
                    heading = btn.get_text(strip=True) if btn else "Section"

                # Clone and strip heading tag for body
                item_clone = BeautifulSoup(str(item), "html.parser")
                h = item_clone.find(re.compile(r"h[2-4]"))
                if h:
                    h.decompose()
                body_md = _html_block_to_markdown(item_clone)

                if body_md:
                    sections.append((heading, body_md))

    # Pattern C: generic fallback — every h2/h3 is a section divider
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

    # ── Assemble Markdown ─────────────────────────────────────────────────────
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
        # Last-resort: dump all visible text
        log.warning(f"    No structured content found for {url} — dumping body text")
        body_parts.append(main.get_text("\n", strip=True) if main else "")

    return front_matter + "\n\n".join(body_parts) + "\n"


def discover_and_scrape_student_guides(index: dict) -> tuple[dict, dict]:
    """
    Level 1: fetch the Student Guides index page, collect all guide category links.
    Level 2: scrape each guide page → write one .md file per guide.

    Dedup: compare SHA-256 of scraped text vs hash stored in index.json.
    Unchanged → skip. New/changed → overwrite .md in policies_mds/.
    No archive directory involved.

    Returns (updated_index, stats).
    """
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

    # ── Collect guide category links ──────────────────────────────────────────
    # UCD's student guides page uses a grid/tile layout. Each tile is a link to
    # a sub-page. We collect all internal links that live under /studentadvisers/.
    # We exclude the index page itself and any anchor-only links.
    guide_links: list[dict] = []
    seen_urls: set[str] = {STUDENT_GUIDES_URL}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()

        if href.startswith("#") or href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue

        full_url = urljoin(STUDENT_GUIDES_URL, href)

        # Only follow links within the same domain and under studentadvisers path
        if (
            full_url.startswith(base_domain)
            and "/studentadvisers/" in full_url
            and full_url not in seen_urls
            and not full_url.lower().endswith((".pdf", ".docx", ".xlsx"))
        ):
            seen_urls.add(full_url)
            label = a.get_text(strip=True)

            # Skip links with no meaningful text (icon-only links etc.)
            if not label or len(label) < 2:
                # Try parent element for text (tile structure: text may be in sibling)
                parent_text = a.find_parent().get_text(strip=True) if a.find_parent() else ""
                label = parent_text[:80] if parent_text else full_url

            guide_links.append({"url": full_url, "label": label})

    if not guide_links:
        log.warning("No student guide sub-pages found on index page.")
        return index, stats

    log.info(f"Found {len(guide_links)} student guide pages")

    # ── Level 2: scrape each guide page ──────────────────────────────────────
    MD_DIR.mkdir(parents=True, exist_ok=True)

    for guide in guide_links:
        url = guide["url"]
        label = guide["label"]
        slug = "guide_" + (slugify(label) or hashlib.md5(url.encode()).hexdigest()[:12])

        md_path = MD_DIR / f"{slug}.md"

        log.info(f"  Scraping: {label[:60]}")
        log.info(f"    URL: {url}")

        md_text = _scrape_guide_page(url, label)
        if not md_text:
            log.warning(f"    FAILED or empty: {url}")
            stats["failed"] += 1
            continue

        new_hash = sha256_of_text(md_text)

        # Dedup: compare against hash stored in index.json
        stored_hash = index.get(slug, {}).get("sha256")
        if stored_hash and stored_hash == new_hash:
            log.info(f"    [SKIP] No changes detected for {slug}.md")
            stats["skipped"] += 1
            index[slug] = {
                **index.get(slug, {}),
                "label": label,
                "source_url": url,
                "md_file": str(md_path),
                "sha256": new_hash,
                "doc_type": "student_guide",
                "status": "unchanged",
            }
            continue

        action = "changed" if stored_hash else "new"
        log.info(f"    [{action.upper()}] {slug}.md")

        md_path.write_text(md_text, encoding="utf-8")
        log.info(f"    MD saved: {md_path.name}  ({len(md_text):,} chars)")

        index[slug] = {
            "label": label,
            "source_url": url,
            "md_file": str(md_path),
            "sha256": new_hash,
            "doc_type": "student_guide",
            "status": action,
        }

        stats["new" if action == "new" else "changed"] += 1

    log.info(f"Student Guides — New: {stats['new']}  Changed: {stats['changed']}  "
             f"Skipped: {stats['skipped']}  Failed: {stats['failed']}")

    return index, stats


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    MD_DIR.mkdir(parents=True, exist_ok=True)

    log.info("=" * 60)
    log.info("UCD PolicyBot Scraper — Governance PDFs + Student Guides")
    log.info("=" * 60)

    index = load_index()
    log.info(f"Existing index: {len(index)} entries")

    # ── Source 1: Governance PDFs ─────────────────────────────────────────────
    log.info("=" * 60)
    log.info("Phase 1: Governance PDFs")
    log.info("=" * 60)
    discovered = discover_pdf_items()
    if discovered:
        index, pdf_stats = sync_and_process(discovered, index)
    else:
        log.error("No PDFs discovered.")
        pdf_stats = {"new": 0, "changed": 0, "skipped": 0, "failed": 0}

    # ── Source 2: Student Guides ──────────────────────────────────────────────
    index, guide_stats = discover_and_scrape_student_guides(index)

    save_index(index)

    # ── Summary ───────────────────────────────────────────────────────────────
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
