"""
Cron entry point for the UCD RAG pipeline.

Run order:
  1. scraper.main()  — governance PDFs + student guides → policies_mds/
  2. ingestion.ingest()   — embed + upsert to Qdrant

PDF → Markdown conversion is handled inside scraper.py:
  - Governance PDFs use pymupdf4llm
  - Student guides are scraped directly to Markdown
"""

from . import scraper
from . import ingestion


def initiate_ingestion():
    scraper.main()
    ingestion.ingest()


if __name__ == "__main__":
    initiate_ingestion()
