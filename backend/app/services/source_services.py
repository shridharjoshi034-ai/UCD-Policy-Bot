import json
from pathlib import Path


def get_source_list():
    """Reads index.json from the rag_pipeline to return the list of
    ingested sources with their metadata."""
    script_dir = Path(__file__).resolve().parent.parent
    index_path = script_dir / "rag_pipeline" / "index.json"

    if not index_path.exists():
        return []

    try:
        raw = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    sources = []
    if isinstance(raw, dict):
        for slug, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            sources.append({
                "source_id": slug,
                "title": entry.get("label", slug),
                "source_url": entry.get("pdf_url") or entry.get("source_url", ""),
                "source_type": entry.get("doc_type", "unknown"),
                "pages_indexed": None,
                "last_indexed_at": None,
            })
    elif isinstance(raw, list):
        for idx, entry in enumerate(raw):
            if not isinstance(entry, dict):
                continue
            file_key = entry.get("pdf_file") or entry.get("md_file") or ""
            slug = Path(file_key).stem if file_key else str(idx)
            sources.append({
                "source_id": slug,
                "title": entry.get("label", slug),
                "source_url": entry.get("pdf_url") or entry.get("source_url", ""),
                "source_type": entry.get("doc_type", "unknown"),
                "pages_indexed": None,
                "last_indexed_at": None,
            })

    return sources