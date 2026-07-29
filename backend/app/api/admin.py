"""
Admin Dashboard API Router
===========================
Provides endpoints for Supabase CRUD operations, ingestion triggers,
scraper control, and SSE-based live log streaming.

Uses shared storage_service and document_services for all Supabase/Qdrant ops.
Background tasks run as subprocesses (not threads) for safe termination.
"""

import asyncio
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Form, Request, Depends, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel
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


def _emit_log(level: str, msg: str) -> None:
    """Explicit helper — pushes directly to the log queue and prints to real stdout."""
    _push_log(level, msg)
    # Print to real stdout so it appears in the terminal.
    # Use errors='replace' to handle Unicode emoji on Windows (cp1252).
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", errors="replace").decode("ascii"))


# ── Active subprocess tracking (replaces unsafe ctypes thread killing) ────────
_active_process: subprocess.Popen | None = None
_process_lock = threading.Lock()


def _read_subprocess_output(proc: subprocess.Popen, label: str) -> None:
    """Read stdout/stderr from a subprocess concurrently using threads,
    pushing every line to the SSE log.  Using two threads avoids the deadlock
    that happens when a blocking readline() on one pipe starves the other."""

    def _drain(stream, level):
        """Read *stream* line-by-line until EOF, pushing to the log."""
        try:
            for line in iter(stream.readline, ""):
                stripped = line.rstrip("\r\n")
                if stripped:
                    _push_log(level, f"[{label}] {stripped}")
        except Exception as e:
            _push_log("error", f"[{label}] Pipe read error: {e}")

    threads = []
    if proc.stdout:
        t = threading.Thread(target=_drain, args=(proc.stdout, "info"), daemon=True)
        t.start()
        threads.append(t)
    if proc.stderr:
        t = threading.Thread(target=_drain, args=(proc.stderr, "warning"), daemon=True)
        t.start()
        threads.append(t)

    # Wait for both drain threads to finish (both streams are fully consumed)
    for t in threads:
        t.join()


def _run_in_subprocess(module_func: str, label: str) -> subprocess.Popen:
    """Spawn a subprocess running the given Python module function.
    e.g. module_func = 'app.rag_pipeline.scraper:main'
    """
    cmd = [
        sys.executable, "-c",
        f"import sys; sys.path.insert(0, '.'); "
        f"from {module_func.split(':')[0]} import {module_func.split(':')[1]}; "
        f"{module_func.split(':')[1]}()"
    ]

    _emit_log("info", f"🚀 Starting subprocess: {label}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).resolve().parent.parent.parent,  # backend/ dir
        env={**os.environ},
    )

    # Read output in background thread
    reader = threading.Thread(target=_read_subprocess_output, args=(proc, label), daemon=True)
    reader.start()

    return proc


# ── Request models ────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class FilePathRequest(BaseModel):
    file_path: str


class BatchDeleteRequest(BaseModel):
    file_paths: list[str]


# ── Simple auth (local dev tool — not production security) ──────────────────

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "12345"
_admin_sessions: set[str] = set()  # simple token set


@router.post("/login")
async def admin_login(req: LoginRequest):
    """Simple username/password login for the admin dashboard."""
    if req.username == ADMIN_USERNAME and req.password == ADMIN_PASSWORD:
        token = f"session_{os.urandom(16).hex()}"
        _admin_sessions.add(token)
        _emit_log("info", f"✅ Admin login successful")
        return {"status": "ok", "token": token}
    _emit_log("warning", f"❌ Failed login attempt for user '{req.username}'")
    return JSONResponse({"status": "error", "message": "Invalid credentials"}, status_code=401)


@router.post("/logout")
async def admin_logout(req: Request):
    """Invalidate a session token."""
    try:
        body = await req.json()
        token = body.get("token", "")
        _admin_sessions.discard(token)
    except Exception:
        pass
    return {"status": "ok"}


def _require_auth(request: Request):
    """FastAPI dependency — rejects requests without a valid session token."""
    token = request.headers.get("X-Admin-Token", "")
    # Also check query param for SSE (EventSource can't send custom headers)
    if not token:
        token = request.query_params.get("token", "")
    if not token or token not in _admin_sessions:
        raise HTTPException(status_code=401, detail="Invalid or missing admin token")


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
    from app.services import storage_service
    try:
        client = storage_service.get_supabase_client()
        bucket = storage_service.get_bucket_name()
        client.storage.from_(bucket).list("", options={"limit": 1})
        url = os.getenv("SUPABASE_URL", "")[:50]
        _emit_log("info", f"✅ Supabase connected — bucket '{bucket}' ready")
        return {
            "status": "ok",
            "message": f"Connected to bucket '{bucket}'",
            "bucket": bucket,
            "url": url + "…" if len(url) >= 50 else url,
        }
    except Exception as e:
        _emit_log("error", f"❌ Supabase connection failed: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/supabase/files", dependencies=[Depends(_require_auth)])
async def supabase_list_files():
    """Recursively list all files in the Supabase bucket."""
    from app.services import storage_service
    try:
        _emit_log("info", "📂 Scanning bucket…")
        files = storage_service.list_files()
        _emit_log("info", f"📂 Found {len(files)} file(s)")
        return {"status": "ok", "files": files}
    except Exception as e:
        _emit_log("error", f"❌ List failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.post("/supabase/upload", dependencies=[Depends(_require_auth)])
async def supabase_upload(
    file: UploadFile = File(...),
    folder: str = Form(""),
):
    """Upload a single file to Supabase storage."""
    from app.services import storage_service
    try:
        contents = await file.read()
        dest_path = (folder.rstrip("/") + "/" if folder else "") + (file.filename or "uploaded_file")
        storage_service.upload_file(dest_path, contents, file.content_type or "application/octet-stream")
        _emit_log("info", f"⬆️ Uploaded: {dest_path} ({len(contents):,} bytes)")
        return {"status": "ok", "path": dest_path, "size": len(contents)}
    except Exception as e:
        _emit_log("error", f"❌ Upload failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.post("/supabase/upload-convert", dependencies=[Depends(_require_auth)])
async def supabase_upload_and_convert(
    file: UploadFile = File(...),
    folder: str = Form(""),
):
    """Upload a PDF, convert to Markdown, store both, AND ingest into Qdrant."""
    from app.services import storage_service
    try:
        contents = await file.read()
        filename = file.filename or "document.pdf"
        base = Path(filename).stem
        prefix = folder.rstrip("/") + "/" if folder else ""

        pdf_path = prefix + filename
        md_path = prefix + base + ".md"

        # Upload original PDF
        storage_service.upload_file(pdf_path, contents, "application/pdf")
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
        storage_service.upload_file(md_path, md_text.encode("utf-8"), "text/markdown")
        _emit_log("info", f"⬆️ Uploaded MD: {md_path} ({len(md_text):,} chars)")

        # ── INGEST into Qdrant ────────────────────────────────────────────────
        _emit_log("info", "🔄 Ingesting into Qdrant…")
        try:
            from app.rag_pipeline.ingestion import ingest_file
            result = ingest_file(md_path)
            _emit_log("info", f"✅ Ingested: {result['filename']} → {result['chunks']} chunks ({result['chars']:,} chars)")
        except Exception as ingest_err:
            _emit_log("warning", f"⚠️ Ingestion failed (file was uploaded): {ingest_err}")

        return {
            "status": "ok",
            "pdf_path": pdf_path,
            "md_path": md_path,
            "md_chars": len(md_text),
        }
    except Exception as e:
        _emit_log("error", f"❌ Upload+Convert failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.post("/supabase/delete", dependencies=[Depends(_require_auth)])
async def supabase_delete(req: FilePathRequest):
    """Delete a single file from Supabase AND its Qdrant chunks (cascading)."""
    from app.services.document_services import delete_document
    try:
        result = delete_document(req.file_path)
        _emit_log("info",
            f"🗑️ Deleted: {req.file_path} "
            f"(Supabase: {result['supabase_deleted']}, Qdrant chunks: {result['qdrant_chunks_deleted']})"
        )
        return {"status": "ok", "deleted": req.file_path, **result}
    except Exception as e:
        _emit_log("error", f"❌ Delete failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.post("/supabase/download-batch", dependencies=[Depends(_require_auth)])
async def supabase_download_batch(req: BatchDeleteRequest):
    """Download multiple files as a ZIP archive."""
    import zipfile
    from fastapi.responses import Response
    from app.services import storage_service

    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in req.file_paths:
                try:
                    file_bytes = storage_service.download_file(file_path)
                    filename = file_path.split("/")[-1]
                    zf.writestr(filename, file_bytes)
                    _emit_log("info", f"📦 Added to ZIP: {file_path}")
                except Exception as e:
                    _emit_log("warning", f"⚠️ Skipping {file_path}: {e}")
                    # Add an error placeholder so the user knows
                    zf.writestr(
                        f"_ERROR_{file_path.split('/')[-1]}.txt",
                        f"Could not download: {e}"
                    )

        buf.seek(0)
        _emit_log("info", f"✅ ZIP created with {len(req.file_paths)} file(s)")
        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": "attachment; filename=policybot_files.zip",
            },
        )
    except Exception as e:
        _emit_log("error", f"❌ ZIP download failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.post("/supabase/delete-batch", dependencies=[Depends(_require_auth)])
async def supabase_delete_batch(req: BatchDeleteRequest):
    """Delete multiple files from Supabase AND their Qdrant chunks (cascading)."""
    from app.services.document_services import delete_documents
    try:
        result = delete_documents(req.file_paths)
        preview = ", ".join(req.file_paths[:5])
        if len(req.file_paths) > 5:
            preview += f" … and {len(req.file_paths) - 5} more"
        _emit_log("info",
            f"🗑️ Batch deleted {len(req.file_paths)} file(s): {preview} "
            f"(Supabase: {result['supabase_deleted']}, Qdrant chunks: {result['qdrant_chunks_deleted']})"
        )
        return {"status": "ok", "deleted_count": len(req.file_paths), **result}
    except Exception as e:
        _emit_log("error", f"❌ Batch delete failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.post("/supabase/delete-all", dependencies=[Depends(_require_auth)])
async def supabase_delete_all():
    """Delete ALL files from Supabase AND clear the Qdrant collection."""
    from app.services.document_services import delete_all_documents
    try:
        _emit_log("warning", "⚠️ Deleting ALL documents from Supabase + Qdrant…")
        result = delete_all_documents()
        _emit_log("info",
            f"✅ All documents deleted — "
            f"Supabase: {result['supabase_deleted']} files, Qdrant cleared: {result['qdrant_cleared']}"
        )
        return {"status": "ok", **result}
    except Exception as e:
        _emit_log("error", f"❌ Delete-all failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.post("/supabase/download", dependencies=[Depends(_require_auth)])
async def supabase_download(req: FilePathRequest):
    """Generate a signed download URL for a file."""
    from app.services import storage_service
    try:
        url = storage_service.create_signed_url(req.file_path, 3600)
        return {"status": "ok", "url": url, "file_path": req.file_path}
    except Exception as e:
        _emit_log("error", f"❌ Download URL failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.post("/supabase/public-url", dependencies=[Depends(_require_auth)])
async def supabase_public_url(req: FilePathRequest):
    """Generate a signed URL for viewing a file (for double-click open / download)."""
    from app.services import storage_service
    try:
        url = storage_service.create_signed_url(req.file_path, 3600)
        return {"status": "ok", "url": url, "file_path": req.file_path}
    except Exception as e:
        _emit_log("error", f"❌ Signed URL failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.post("/qdrant/clear", dependencies=[Depends(_require_auth)])
async def clear_qdrant():
    """Drop and recreate the Qdrant collection (no Supabase deletion)."""
    _emit_log("warning", "🗑️ Dropping Qdrant collection…")

    def _run():
        try:
            from app.services.document_services import ensure_qdrant_collection
            from qdrant_client import QdrantClient
            client = QdrantClient(
                url=os.getenv("QDRANT_URL"),
                api_key=os.getenv("QDRANT_API_KEY"),
            )
            client.delete_collection("ucd_policies")
            _emit_log("info", "✅ Dropped collection 'ucd_policies'")
            ensure_qdrant_collection()
            _emit_log("info", "✅ Re-created empty collection 'ucd_policies'")
        except Exception as e:
            _emit_log("error", f"❌ Qdrant clear failed: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "ok", "message": "Qdrant clear started — watch the live terminal"}


@router.get("/qdrant/stats")
async def qdrant_stats():
    """Return Qdrant collection name and vector/point count.
    No auth required — this is a lightweight health-check metric."""
    try:
        from qdrant_client import QdrantClient
        client = QdrantClient(
            url=os.getenv("QDRANT_URL"),
            api_key=os.getenv("QDRANT_API_KEY"),
        )
        collection_name = "ucd_policies"

        if not client.collection_exists(collection_name):
            return {
                "status": "ok",
                "collection": collection_name,
                "exists": False,
                "point_count": 0,
                "message": f"Collection '{collection_name}' not found",
            }

        info = client.get_collection(collection_name)
        point_count = getattr(info, "points_count", 0)

        # Count unique source_file_name values (distinct ingested files)
        unique_files = 0
        try:
            unique_names = set()
            scroll_result = client.scroll(
                collection_name,
                with_payload=["source_file_name"],
                limit=10000,
            )
            for point in scroll_result[0]:
                if point.payload and point.payload.get("source_file_name"):
                    unique_names.add(point.payload["source_file_name"])
            unique_files = len(unique_names)
        except Exception:
            pass

        return {
            "status": "ok",
            "collection": collection_name,
            "exists": True,
            "point_count": point_count,
            "unique_files": unique_files,
            "message": f"{point_count:,} vectors, {unique_files} files in '{collection_name}'",
        }
    except Exception as e:
        return {
            "status": "error",
            "collection": "ucd_policies",
            "exists": False,
            "point_count": 0,
            "unique_files": 0,
            "message": f"Qdrant unreachable: {e}",
        }


@router.post("/supabase/empty-bucket", dependencies=[Depends(_require_auth)])
async def empty_supabase_bucket():
    """Delete ALL files from the Supabase bucket WITHOUT touching Qdrant.
    More targeted than delete-all (which also clears Qdrant)."""
    from app.services import storage_service
    try:
        _emit_log("warning", "🗑️ Deleting ALL files from Supabase bucket (Qdrant untouched)…")
        files = storage_service.list_files()
        if files:
            storage_service.delete_files(files)
            _emit_log("info", f"✅ Deleted {len(files)} file(s) from Supabase bucket")
        else:
            _emit_log("info", "ℹ️ Bucket already empty")
        return {"status": "ok", "files_deleted": len(files)}
    except Exception as e:
        _emit_log("error", f"❌ Empty bucket failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.post("/qdrant/delete-collection", dependencies=[Depends(_require_auth)])
async def delete_qdrant_collection():
    """PERMANENTLY drop the Qdrant collection — does NOT recreate it.
    More destructive than /qdrant/clear (which recreates an empty collection)."""
    try:
        _emit_log("warning", "💥 Permanently deleting Qdrant collection 'ucd_policies'…")
        from qdrant_client import QdrantClient
        client = QdrantClient(
            url=os.getenv("QDRANT_URL"),
            api_key=os.getenv("QDRANT_API_KEY"),
        )
        client.delete_collection("ucd_policies")
        _emit_log("info", "✅ Permanently deleted collection 'ucd_policies'")
        return {"status": "ok", "message": "Qdrant collection permanently deleted"}
    except Exception as e:
        _emit_log("error", f"❌ Delete collection failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


# ── Stop All: kills subprocess first call, kills backend second call ──────────

_stop_call_count = 0
_stop_last_call = 0.0


@router.post("/stop", dependencies=[Depends(_require_auth)])
async def stop_all():
    """
    Stop All button:
    - First call: kill the active subprocess (scraper/ingestion) via terminate() then kill()
    - Second call within 3 seconds: force-exit the entire backend process (like Ctrl+C×2)
    - Third+ call within 3 seconds: os._exit(1) immediately
    """
    global _active_process, _stop_call_count, _stop_last_call
    from app.services import storage_service

    now = time.time()

    # Reset counter if last call was > 3 seconds ago
    if now - _stop_last_call > 3.0:
        _stop_call_count = 0
    _stop_call_count += 1
    _stop_last_call = now

    if _stop_call_count == 1:
        # First call: kill the subprocess
        with _process_lock:
            proc = _active_process

        if proc is not None and proc.poll() is None:
            _emit_log("warning", "🛑 Stopping active task (SIGTERM)…")
            proc.terminate()
            try:
                proc.wait(timeout=2)
                _emit_log("info", "✅ Active task terminated gracefully")
            except subprocess.TimeoutExpired:
                _emit_log("warning", "🛑 Force-killing active task (SIGKILL)…")
                proc.kill()
                proc.wait()
                _emit_log("info", "✅ Active task force-killed")
            with _process_lock:
                _active_process = None
        else:
            _emit_log("info", "ℹ️ No active task running — click again to stop backend")
            return {"status": "ok", "message": "No active task. Click again within 3s to stop backend."}

        return {"status": "ok", "message": "Active task killed. Click again within 3s to stop backend."}

    elif _stop_call_count == 2:
        # Second call: force exit the backend (os._exit works on all platforms)
        _emit_log("warning", "🛑 Force-stopping backend…")
        # Give SSE clients a moment to see the message
        time.sleep(0.5)
        os._exit(0)

    else:
        # Third+ call: immediate force exit
        _emit_log("error", "💥 Force-exiting backend immediately!")
        time.sleep(0.3)
        os._exit(1)


# ── Scraper / Ingestion endpoints (subprocess-based) ──────────────────────────

@router.post("/ingest", dependencies=[Depends(_require_auth)])
async def trigger_ingestion():
    """Run ingestion pipeline — FIRST drops+recreates Qdrant collection, THEN
    downloads Markdown from Supabase, chunks, embeds, upserts into Qdrant."""
    global _active_process
    _emit_log("info", "🚀 Starting full re-ingestion pipeline…")

    # ── Check for already-running task (brief lock) ───────────────────────────
    with _process_lock:
        if _active_process is not None and _active_process.poll() is None:
            _emit_log("warning", "⚠️ A task is already running. Stop it first.")
            return {"status": "error", "message": "A task is already running"}

    # ── Step 1: Clear Qdrant collection BEFORE ingestion (no lock needed) ────
    _emit_log("warning", "🗑️ Step 1/2: Clearing Qdrant collection 'ucd_policies'…")
    try:
        from qdrant_client import QdrantClient
        client = QdrantClient(
            url=os.getenv("QDRANT_URL"),
            api_key=os.getenv("QDRANT_API_KEY"),
        )
        client.delete_collection("ucd_policies")
        _emit_log("info", "✅ Dropped collection 'ucd_policies'")
        # Recreate the empty collection so ingestion has somewhere to write
        from app.services.document_services import ensure_qdrant_collection
        ensure_qdrant_collection()
        _emit_log("info", "✅ Re-created empty collection 'ucd_policies'")
    except Exception as e:
        _emit_log("error", f"❌ Qdrant clear failed: {e}")
        return {"status": "error", "message": f"Qdrant clear failed: {e}"}

    # ── Step 2: Run ingestion subprocess (brief lock for spawn) ──────────────
    _emit_log("info", "🔄 Step 2/2: Ingesting all Markdown from Supabase…")
    with _process_lock:
        proc = _run_in_subprocess("app.rag_pipeline.ingestion:ingest", "ingestion")
        _active_process = proc

    # Background monitor
    def _monitor():
        global _active_process
        proc.wait()
        with _process_lock:
            if _active_process is proc:
                _active_process = None
        if proc.returncode == 0:
            _emit_log("info", "✅ Ingestion complete")
        elif proc.returncode == -15 or proc.returncode == -9:
            _emit_log("warning", "🛑 Ingestion was stopped")
        else:
            _emit_log("error", f"❌ Ingestion failed (exit code {proc.returncode})")

    threading.Thread(target=_monitor, daemon=True).start()
    return {"status": "ok", "message": "Ingestion started — watch the live terminal"}


@router.post("/scrape", dependencies=[Depends(_require_auth)])
async def trigger_scrape():
    """Run the UCD scraper (governance PDFs + student guides → Supabase)."""
    global _active_process
    _emit_log("info", "🌐 Starting scraper…")

    with _process_lock:
        if _active_process is not None and _active_process.poll() is None:
            _emit_log("warning", "⚠️ A task is already running. Stop it first.")
            return {"status": "error", "message": "A task is already running"}

        proc = _run_in_subprocess("app.rag_pipeline.scraper:main", "scraper")
        _active_process = proc

    def _monitor():
        global _active_process
        proc.wait()
        with _process_lock:
            if _active_process is proc:
                _active_process = None
        if proc.returncode == 0:
            _emit_log("info", "✅ Scraper complete")
        elif proc.returncode == -15 or proc.returncode == -9:
            _emit_log("warning", "🛑 Scraper was stopped")
        else:
            _emit_log("error", f"❌ Scraper failed (exit code {proc.returncode})")

    threading.Thread(target=_monitor, daemon=True).start()
    return {"status": "ok", "message": "Scraper started — watch the live terminal"}


@router.post("/scrape-and-ingest", dependencies=[Depends(_require_auth)])
async def trigger_scrape_and_ingest():
    """Run scraper, then ingestion pipeline (full refresh)."""
    global _active_process
    _emit_log("info", "🌐🔄 Starting full scrape + ingest pipeline…")

    with _process_lock:
        if _active_process is not None and _active_process.poll() is None:
            _emit_log("warning", "⚠️ A task is already running. Stop it first.")
            return {"status": "error", "message": "A task is already running"}

    cmd = (
        f"import sys; sys.path.insert(0, '.'); "
        f"from app.rag_pipeline.scraper import main as scraper_main; "
        f"from app.rag_pipeline.ingestion import ingest; "
        f"import os; "
        f"print('PHASE 1/2: Scraping…'); scraper_main(); "
        f"print('PHASE 2/2: Ingesting…'); ingest(); "
        f"print('Pipeline complete!')"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).resolve().parent.parent.parent,
        env={**os.environ},
    )

    reader = threading.Thread(target=_read_subprocess_output, args=(proc, "scrape+ingest"), daemon=True)
    reader.start()

    with _process_lock:
        _active_process = proc

    def _monitor():
        global _active_process
        proc.wait()
        with _process_lock:
            if _active_process is proc:
                _active_process = None
        if proc.returncode == 0:
            _emit_log("info", "✅ Full pipeline complete")
        elif proc.returncode == -15 or proc.returncode == -9:
            _emit_log("warning", "🛑 Pipeline was stopped")
        else:
            _emit_log("error", f"❌ Pipeline failed (exit code {proc.returncode})")

    threading.Thread(target=_monitor, daemon=True).start()
    return {"status": "ok", "message": "Scrape + Ingest started — watch the live terminal"}


# ── SSE Log stream ────────────────────────────────────────────────────────────

@router.get("/logs/stream")
async def logs_stream(request: Request, token: str = ""):
    """Server-Sent Events stream for live terminal logs.
    Token is accepted via query param since EventSource can't send custom headers."""
    # Validate token from query string (EventSource can't send headers)
    if not token or token not in _admin_sessions:
        # Return a single error event instead of streaming
        async def _error_gen():
            yield f"data: {json.dumps({'level': 'error', 'msg': 'Invalid or expired token. Please log in again.'})}\n\n"
        return StreamingResponse(_error_gen(), media_type="text/event-stream")

    async def _event_generator():
        my_event = threading.Event()
        with _listeners_lock:
            _log_listeners.append(my_event)

        try:
            # Drain backlog
            while True:
                try:
                    entry = _log_queue.get_nowait()
                    yield f"data: {json.dumps(entry)}\n\n"
                except queue.Empty:
                    break

            while True:
                if await request.is_disconnected():
                    break

                drained = False
                while True:
                    try:
                        entry = _log_queue.get_nowait()
                        yield f"data: {json.dumps(entry)}\n\n"
                        drained = True
                    except queue.Empty:
                        break

                if not drained:
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
