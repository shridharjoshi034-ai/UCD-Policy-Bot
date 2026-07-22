"""
Cron entry point for the UCD RAG pipeline.

Run order:
  1. scraper.main()  — governance PDFs + student guides → policies_mds/
  2. ingestion.ingest()   — embed + upsert to Qdrant, archive processed MDs

Previously this also called pdf_to_md.convert_pdf_to_markdown(), but that
conversion is now handled inside scraper_slow.py (governance PDFs use
pymupdf4llm; student guides are scraped directly to Markdown). pdf_to_md.py
can be deleted.
"""

from . import scraper
from . import ingestion


def initiate_ingestion():
    scraper.main()
    ingestion.ingest()


if __name__ == "__main__":
    initiate_ingestion()
