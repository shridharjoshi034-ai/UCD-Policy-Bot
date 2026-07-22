"""
Admin Dashboard API Router
===========================
Provides endpoints for Supabase CRUD operations, ingestion triggers,
scraper control, and SSE-based live log streaming.

All Supabase + Qdrant credentials are read from environment variables
(.env file). No credentials are accepted in request bodies.
"""

import os
import asyncio
import ctypes
import io
import json
import logging
import queue
import sys
import threading
import time
import tempfile
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, UploadFile, File, Form, Request
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

router = APIRouter(prefix="/admin", tags=["admin"])

# ── In-memory log buffer for SSE streaming ────────────────────────────────────
_log_queue: queue.Queue = queue.Queue(maxsize=2000)
_listeners_lock = threading.Lock()
_log_listeners: list[threading.Event] = []


def _push_log(level: str, msg: str) -> None:
    """Push a log entry into the shared queue and notify SSE listeners."""
    entry = {
        "time": datetime.utcnow().isoformat() + "Z",
        "level": level,
        "msg": msg,
    }
    try:
        _log_queue.put_nowait(entry)
    except queue.Full:
        try:
            _log_queue.get_nowait()
            _log_queue.put_nowait(entry)
        except queue.Empty:
            pass
    with _listeners_lock:
        for evt in _log_listeners:
            evt.set()


# ── Capture ALL stdout/stderr → live terminal ─────────────────────────────────
class _StreamRedirect(io.StringIO):
    """Redirects stdout/stderr to the SSE log queue, line by line."""

    def __init__(self, level: str, original_stream):
        super().__init__()
        self._level = level
        self._original = original_stream

    def write(self, s: str) -> int:
        if s and s.strip():
            # Also write to original stream so it still appears in the real terminal
            self._original.write(s)
            self._original.flush()
            # Push each non-empty line to the log queue
            for line in s.rstrip().split("\n"):
                stripped = line.rstrip("\r")
                if stripped:
                    _push_log(self._level, stripped)
        return len(s)

    def flush(self) -> None:
        self._original.flush()


# Install stdout/stderr capture (save originals first)
_original_stdout = sys.stdout
_original_stderr = sys.stderr
sys.stdout = _StreamRedirect("info", _original_stdout)
sys.stderr = _StreamRedirect("warning", _original_stderr)  # warning, not error — many libs write non-errors to stderr

# ── Also capture all Python logging output ────────────────────────────────────
class _LogHandler(logging.Handler):
    """Forwards all logging records to the SSE log queue."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            _push_log(record.levelname.lower(), msg)
        except Exception:
            pass


_root_handler = _LogHandler()
_root_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)
# Guard against duplicate handlers on hot reload
if _root_handler not in _root_logger.handlers:
    _root_logger.addHandler(_root_handler)


def _emit_log(level: str, msg: str) -> None:
    """Explicit helper — pushes directly to the log queue (also visible via stdout capture)."""
    _push_log(level, msg)
    # Also print to original stdout so it appears in the real terminal
    print(msg, file=_original_stdout)


# ── Global stop signal + active thread tracking ───────────────────────────────
_stop_flag = threading.Event()
_active_thread: Optional[threading.Thread] = None
_active_lock = threading.Lock()


def _is_stopped() -> bool:
    """Check if the stop flag is set."""
    return _stop_flag.is_set()


def _kill_thread_immediately(thread: threading.Thread) -> bool:
    """Raise SystemExit in the target thread, killing it at the next Python
    bytecode boundary (typically within milliseconds). Returns True on success."""
    tid = thread.ident
    if tid is None:
        return False
    # ctypes.pythonapi.PyThreadState_SetAsyncExc raises an exception in the
    # thread identified by its thread id. We raise SystemExit so cleanup
    # handlers (finally/__exit__) still run.
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(tid), ctypes.py_object(SystemExit)
    )
    if res == 0:
        return False  # invalid thread id
    if res > 1:
        # Somehow hit multiple threads — undo the damage by calling again with None
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), None)
        return False
    return True


def _start_tracked_thread(target_func) -> threading.Thread:
    """Create and start a daemon thread that runs target_func under stop-tracking.
    Registers the thread BEFORE starting to avoid a race window."""
    def _wrapper():
        try:
            if _is_stopped():
                _emit_log("warning", "🛑 Task aborted — stop signal received before start")
                return
            target_func()
        except SystemExit:
            _emit_log("warning", "🛑 Task terminated immediately by stop signal")
        except Exception as e:
            _emit_log("error", f"❌ Task failed: {e}")
        finally:
            with _active_lock:
                global _active_thread
                if _active_thread is threading.current_thread():
                    _active_thread = None

    t = threading.Thread(target=_wrapper, daemon=True)
    with _active_lock:
        _active_thread = t
    t.start()
    return t


# ── Request models ────────────────────────────────────────────────────────────

class FilePathRequest(BaseModel):
    file_path: str


class BatchDeleteRequest(BaseModel):
    file_paths: list[str]


# ── Helpers: read creds from .env ─────────────────────────────────────────────

def _get_supabase_client():
    """Return an authenticated Supabase client using env vars."""
    from supabase import create_client

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env"
        )
    return create_client(url, key)


def _get_bucket_name() -> str:
    bucket = os.getenv("SUPABASE_BUCKET")
    if not bucket:
        raise RuntimeError("SUPABASE_BUCKET must be set in .env")
    return bucket


def _list_all_files(supabase, bucket_name: str, prefix: str = "") -> list[str]:
    """Recursively list all file paths in a Supabase bucket."""
    all_files: list[str] = []
    limit = 100
    offset = 0

    while True:
        try:
            resp = (
                supabase.storage.from_(bucket_name)
                .list(
                    prefix,
                    options={
                        "limit": limit,
                        "offset": offset,
                        "sortBy": {"column": "name", "order": "asc"},
                    },
                )
            )
        except Exception as e:
            _emit_log("error", f"List error at '{prefix}': {e}")
            break

        if not resp:
            break

        for item in resp:
            if item.get("id") is None:  # folder
                all_files.extend(
                    _list_all_files(supabase, bucket_name, prefix + item["name"] + "/")
                )
            else:
                all_files.append(prefix + item["name"])

        if len(resp) < limit:
            break
        offset += limit

    return all_files


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def admin_dashboard():
    """Serve the admin dashboard HTML page."""
    static_dir = Path(__file__).resolve().parent.parent / "static"
    html_path = static_dir / "admin.html"
    if not html_path.exists():
        return HTMLResponse("<h1>Admin dashboard not found</h1>", status_code=404)
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/supabase/status")
async def supabase_status():
    """Check whether Supabase credentials are configured and reachable."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    bucket = os.getenv("SUPABASE_BUCKET")

    if not url or not key or not bucket:
        missing = []
        if not url: missing.append("SUPABASE_URL")
        if not key: missing.append("SUPABASE_SERVICE_ROLE_KEY")
        if not bucket: missing.append("SUPABASE_BUCKET")
        return {
            "status": "not_configured",
            "message": f"Missing env vars: {', '.join(missing)}",
        }

    try:
        from supabase import create_client

        client = create_client(url, key)
        client.storage.from_(bucket).list("", options={"limit": 1})
        _emit_log("info", f"✅ Supabase connected — bucket '{bucket}' ready")
        return {
            "status": "ok",
            "message": f"Connected to bucket '{bucket}'",
            "bucket": bucket,
            "url": url[:50] + "…",
        }
    except Exception as e:
        _emit_log("error", f"❌ Supabase connection failed: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/supabase/files")
async def supabase_list_files():
    """Recursively list all files in the Supabase bucket (creds from .env)."""
    try:
        client = _get_supabase_client()
        bucket = _get_bucket_name()
        _emit_log("info", f"📂 Scanning bucket '{bucket}'…")
        files = _list_all_files(client, bucket)
        _emit_log("info", f"📂 Found {len(files)} file(s)")
        return {"status": "ok", "files": files}
    except Exception as e:
        _emit_log("error", f"❌ List failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.post("/supabase/upload")
async def supabase_upload(
    file: UploadFile = File(...),
    folder: str = Form(""),
):
    """Upload a single file to Supabase storage (creds from .env)."""
    try:
        client = _get_supabase_client()
        bucket = _get_bucket_name()

        contents = await file.read()
        dest_path = (folder.rstrip("/") + "/" if folder else "") + (file.filename or "uploaded_file")

        client.storage.from_(bucket).upload(
            dest_path,
            contents,
            {"content-type": file.content_type or "application/octet-stream"},
        )
        _emit_log("info", f"⬆️ Uploaded: {dest_path} ({len(contents):,} bytes)")
        return {"status": "ok", "path": dest_path, "size": len(contents)}
    except Exception as e:
        _emit_log("error", f"❌ Upload failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.post("/supabase/upload-convert")
async def supabase_upload_and_convert(
    file: UploadFile = File(...),
    folder: str = Form(""),
):
    """Upload a PDF, convert to Markdown via pymupdf4llm, store both (creds from .env)."""
    try:
        client = _get_supabase_client()
        bucket = _get_bucket_name()

        contents = await file.read()
        filename = file.filename or "document.pdf"
        base = Path(filename).stem
        prefix = folder.rstrip("/") + "/" if folder else ""

        pdf_path = prefix + filename
        md_path = prefix + base + ".md"

        # Upload original PDF
        client.storage.from_(bucket).upload(
            pdf_path, contents, {"content-type": "application/pdf"}
        )
        _emit_log("info", f"⬆️ Uploaded PDF: {pdf_path} ({len(contents):,} bytes)")

        # Convert PDF → Markdown
        _emit_log("info", f"🔄 Converting {filename} → Markdown…")
        try:
            import pymupdf4llm

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(contents)
                tmp_path = tmp.name

            md_text = pymupdf4llm.to_markdown(tmp_path)
            os.unlink(tmp_path)

            if not md_text:
                raise ValueError("pymupdf4llm returned empty output")
        except Exception as conv_err:
            _emit_log("error", f"❌ PDF→MD conversion failed: {conv_err}")
            return JSONResponse(
                {"status": "error", "message": f"Conversion failed: {conv_err}"},
                status_code=400,
            )

        # Upload Markdown
        client.storage.from_(bucket).upload(
            md_path,
            md_text.encode("utf-8"),
            {"content-type": "text/markdown; charset=utf-8"},
        )
        _emit_log("info", f"⬆️ Uploaded MD: {md_path} ({len(md_text):,} chars)")

        return {
            "status": "ok",
            "pdf_path": pdf_path,
            "md_path": md_path,
            "md_chars": len(md_text),
        }
    except Exception as e:
        _emit_log("error", f"❌ Upload+Convert failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.post("/supabase/delete-batch")
async def supabase_delete_batch(req: BatchDeleteRequest):
    """Delete multiple files from Supabase storage in a single batch call."""
    try:
        client = _get_supabase_client()
        bucket = _get_bucket_name()
        client.storage.from_(bucket).remove(req.file_paths)
        preview = ", ".join(req.file_paths[:5])
        if len(req.file_paths) > 5:
            preview += f" … and {len(req.file_paths) - 5} more"
        _emit_log("info", f"🗑️ Batch deleted {len(req.file_paths)} file(s): {preview}")
        return {"status": "ok", "deleted_count": len(req.file_paths)}
    except Exception as e:
        _emit_log("error", f"❌ Batch delete failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.post("/supabase/delete")
async def supabase_delete(req: FilePathRequest):
    """Delete a single file from Supabase storage (creds from .env)."""
    try:
        client = _get_supabase_client()
        bucket = _get_bucket_name()
        client.storage.from_(bucket).remove([req.file_path])
        _emit_log("info", f"🗑️ Deleted: {req.file_path}")
        return {"status": "ok", "deleted": req.file_path}
    except Exception as e:
        _emit_log("error", f"❌ Delete failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.post("/supabase/delete-all")
async def supabase_delete_all():
    """Delete all files from the Supabase bucket (use with caution!)."""
    try:
        client = _get_supabase_client()
        bucket = _get_bucket_name()
        files = _list_all_files(client, bucket)
        _emit_log("warning", f"⚠️ Deleting ALL {len(files)} files from '{bucket}'…")

        batch_size = 500
        for i in range(0, len(files), batch_size):
            batch = files[i : i + batch_size]
            client.storage.from_(bucket).remove(batch)
            _emit_log("info", f"🗑️ Deleted batch {i // batch_size + 1}: {len(batch)} files")

        _emit_log("info", f"✅ All {len(files)} files deleted from Supabase")
        return {"status": "ok", "deleted_count": len(files)}
    except Exception as e:
        _emit_log("error", f"❌ Delete-all failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.post("/supabase/download")
async def supabase_download(req: FilePathRequest):
    """Generate a signed download URL for a file."""
    try:
        client = _get_supabase_client()
        bucket = _get_bucket_name()
        url = client.storage.from_(bucket).create_signed_url(req.file_path, 300)
        return {"status": "ok", "url": url.get("signedURL", url) if isinstance(url, dict) else url}
    except Exception as e:
        _emit_log("error", f"❌ Download URL failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.post("/qdrant/clear")
async def clear_qdrant():
    """Drop and recreate the Qdrant 'ucd_policies' collection (creds from .env)."""
    _emit_log("warning", "🗑️ Dropping Qdrant collection 'ucd_policies'…")

    def _run():
        try:
            from qdrant_client import QdrantClient, models

            client = QdrantClient(
                url=os.getenv("QDRANT_URL"),
                api_key=os.getenv("QDRANT_API_KEY"),
            )
            client.delete_collection("ucd_policies")
            _emit_log("info", "✅ Dropped collection 'ucd_policies'")

            client.create_collection(
                collection_name="ucd_policies",
                vectors_config=models.VectorParams(
                    size=1024,
                    distance=models.Distance.COSINE,
                ),
                sparse_vectors_config={
                    "sparse": models.SparseVectorParams()
                },
            )
            client.create_payload_index(
                collection_name="ucd_policies",
                field_name="source_file_id",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
            _emit_log("info", "✅ Re-created empty collection 'ucd_policies'")
        except Exception as e:
            _emit_log("error", f"❌ Qdrant clear failed: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "ok", "message": "Qdrant clear started — watch the live terminal"}


@router.post("/stop")
async def stop_all():
    """Immediately kill any running background task and set the stop flag.
    Uses ctypes to raise SystemExit in the active thread — terminates
    within milliseconds. Call again to clear the flag."""
    if _stop_flag.is_set():
        _stop_flag.clear()
        _emit_log("info", "🟢 Stop flag cleared — new tasks can run again")
        return {"status": "ok", "message": "Stop flag cleared — tasks will run normally"}

    _stop_flag.set()

    with _active_lock:
        t = _active_thread

    if t is not None and t.is_alive():
        killed = _kill_thread_immediately(t)
        if killed:
            _emit_log("warning", "🛑 Active task killed immediately via SystemExit injection")
        else:
            _emit_log("warning", "🛑 Stop flag set but unable to inject exception into active thread")
    else:
        _emit_log("warning", "🛑 Stop signal sent — no active task to kill (flag set for next task)")

    return {"status": "ok", "message": "Stop signal sent — active task killed if running"}


@router.post("/ingest")
async def trigger_ingestion():
    """Run ingestion.py directly — reads local policies_mds/ → chunks → embeds → Qdrant."""
    _emit_log("info", "🚀 Starting ingestion pipeline (ingestion.py)…")

    def _task():
        from app.rag_pipeline import ingestion as ing_module
        before = time.perf_counter()
        ing_module.ingest()
        elapsed = time.perf_counter() - before
        _emit_log("info", f"✅ Ingestion complete in {elapsed:.1f}s")

    _start_tracked_thread(_task)
    return {"status": "ok", "message": "Ingestion started — watch the live terminal"}


@router.post("/scrape")
async def trigger_scrape():
    """Run the UCD scraper (governance PDFs + student guides)."""
    _emit_log("info", "🌐 Starting scraper…")

    def _task():
        from app.rag_pipeline import scraper as scraper_module
        before = time.perf_counter()
        scraper_module.main()
        elapsed = time.perf_counter() - before
        _emit_log("info", f"✅ Scraper complete in {elapsed:.1f}s")

    _start_tracked_thread(_task)
    return {"status": "ok", "message": "Scraper started — watch the live terminal"}


@router.post("/scrape-and-ingest")
async def trigger_scrape_and_ingest():
    """Run scraper, then ingestion pipeline (full refresh). Stop flag checked between phases."""
    _emit_log("info", "🌐🔄 Starting full scrape + re-ingest pipeline…")

    def _task():
        from app.rag_pipeline import scraper as scraper_module
        from app.rag_pipeline import ingestion as ing_module

        _emit_log("info", "─" * 50)
        _emit_log("info", "PHASE 1/2: Scraping UCD sources…")
        before = time.perf_counter()
        scraper_module.main()
        elapsed = time.perf_counter() - before
        _emit_log("info", f"✅ Scrape done ({elapsed:.1f}s)")

        if _is_stopped():
            _emit_log("warning", "🛑 Pipeline aborted — stop signal after scrape, skipping ingestion")
            return

        _emit_log("info", "─" * 50)
        _emit_log("info", "PHASE 2/2: Ingesting into Qdrant…")
        before = time.perf_counter()
        ing_module.ingest()
        elapsed = time.perf_counter() - before
        _emit_log("info", f"✅ Ingestion done ({elapsed:.1f}s)")
        _emit_log("info", "─" * 50)
        _emit_log("info", "🎉 Full pipeline complete!")

    _start_tracked_thread(_task)
    return {"status": "ok", "message": "Scrape + Ingest started — watch the live terminal"}


@router.get("/logs/stream")
async def logs_stream(request: Request):
    """Server-Sent Events stream for live terminal logs."""

    async def _event_generator():
        my_event = threading.Event()
        with _listeners_lock:
            _log_listeners.append(my_event)

        try:
            # Drain any backlog
            while True:
                try:
                    entry = _log_queue.get_nowait()
                    yield f"data: {json.dumps(entry)}\n\n"
                except queue.Empty:
                    break

            while True:
                if await request.is_disconnected():
                    break

                # Drain queue
                drained = False
                while True:
                    try:
                        entry = _log_queue.get_nowait()
                        yield f"data: {json.dumps(entry)}\n\n"
                        drained = True
                    except queue.Empty:
                        break

                if not drained:
                    # Poll every 500ms — threading.Event is already set by _push_log(),
                    # so we just need a fast poll loop instead of blocking in run_in_executor
                    my_event.clear()
                    try:
                        await asyncio.wait_for(
                            asyncio.get_running_loop().run_in_executor(None, my_event.wait, 0.5),
                            timeout=0.6,
                        )
                    except (asyncio.TimeoutError, TimeoutError):
                        yield ": keepalive\n\n"
        finally:
            with _listeners_lock:
                try:
                    _log_listeners.remove(my_event)
                except ValueError:
                    pass

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
