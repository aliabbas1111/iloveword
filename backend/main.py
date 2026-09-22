"""
ILoveWord — API.

Contract (this is what eliminates the 422s):
  • single-file tools take multipart field  `file`
  • the merge tool takes repeated field     `files`
  • every other parameter is a multipart form field, never a query string
  • every response is either a binary attachment or JSON {detail, code}

Run:  uvicorn main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Callable, List

import anyio
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

import tools
from core import (
    CLEANUP_INTERVAL,
    CORS_ORIGINS,
    HEAVY_SLOTS,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_REQUEST_BYTES,
    ToolError,
    Workspace,
    assert_wordprocessing,
    deliver,
    purge_stale_workspaces,
    safe_stem,
    save_upload,
    soffice_available,
    WORKSPACE_MAX_AGE,
)

logging.basicConfig(
    level=os.getenv("ILW_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("iloveword")

DOCX = (".docx",)
DOCX_LIKE = (".docx", ".dotx", ".docm")
PDF = (".pdf",)
IMAGES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
HTML = (".html", ".htm")
MARKDOWN = (".md", ".markdown", ".txt")


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
# Heavy routes spawn LibreOffice or Tesseract — seconds of CPU per request —
# so an unauthenticated flood is a denial of service with no effort required.
# Limits are per client IP. Behind a proxy, uvicorn must run with
# --proxy-headers and --forwarded-allow-ips, or every caller looks like nginx.
RATE_HEAVY = os.getenv("ILW_RATE_HEAVY", "10/minute")
RATE_LIGHT = os.getenv("ILW_RATE_LIGHT", "30/minute")
RATE_DEFAULT = os.getenv("ILW_RATE_DEFAULT", "300/hour")
# memory:// counts per process, which is only correct for a single worker.
# Point this at redis://redis:6379/0 as soon as more than one worker runs.
RATE_STORAGE = os.getenv("ILW_RATE_STORAGE_URI", "memory://")

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=RATE_STORAGE,
    default_limits=[RATE_DEFAULT],
    headers_enabled=True,
)


async def _cleanup_loop() -> None:
    """
    Purging at startup alone leaves orphans between restarts: a worker killed
    mid-request never runs its BackgroundTask, and on a long-lived server that
    is a slow disk leak. So sweep on a timer as well.
    """
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        try:
            removed = await anyio.to_thread.run_sync(
                lambda: purge_stale_workspaces(WORKSPACE_MAX_AGE)
            )
            if removed:
                log.info("cleanup: removed %d stale workspaces", removed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the sweeper must never die
            log.exception("cleanup sweep failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    removed = purge_stale_workspaces(WORKSPACE_MAX_AGE)
    log.info(
        "startup: purged %d stale workspaces; libreoffice=%s; rate-limit store=%s",
        removed, soffice_available(), RATE_STORAGE.split("://")[0],
    )
    sweeper = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper
        purge_stale_workspaces(max_age_seconds=0)


app = FastAPI(
    title="ILoveWord API",
    version="4.0.0",
    summary="Document processing suite — merge, split, convert, protect, OCR.",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    # Credentials and a "*" origin are mutually exclusive per the CORS spec;
    # browsers silently drop the response. Credentials stay off unless an
    # explicit origin list is configured.
    allow_origins=CORS_ORIGINS,
    allow_credentials=CORS_ORIGINS != ["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


# --------------------------------------------------------------------------- #
# Middleware & error handling
# --------------------------------------------------------------------------- #


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_REQUEST_BYTES:
        return JSONResponse(
            {"detail": f"Request exceeds the {MAX_REQUEST_BYTES // (1024 * 1024)} MB limit.",
             "code": "request_too_large"},
            status_code=413,
        )
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    """Turn FastAPI's opaque 422 into an actionable message."""
    missing = [
        ".".join(str(p) for p in err["loc"][1:]) or "body"
        for err in exc.errors()
        if err["type"] in ("missing", "value_error.missing")
    ]
    if missing:
        detail = (
            f"Missing required form field(s): {', '.join(missing)}. "
            "Send the request as multipart/form-data."
        )
    else:
        first = exc.errors()[0]
        detail = f"{'.'.join(str(p) for p in first['loc'][1:])}: {first['msg']}"
    return JSONResponse({"detail": detail, "code": "invalid_request", "fields": missing},
                        status_code=422)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        {"detail": "Too many requests. Please wait a moment and try again.",
         "code": "rate_limited", "limit": str(exc.limit.limit)},
        status_code=429,
        headers={"Retry-After": "60"},
    )


@app.exception_handler(HTTPException)
async def http_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        {"detail": exc.detail, "code": getattr(exc, "code", "http_error")},
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        {"detail": "An unexpected server error occurred. The file was not modified.",
         "code": "internal_error"},
        status_code=500,
    )


async def run(fn: Callable, *args, heavy: bool = False, **kwargs):
    """Run a blocking operation off the event loop, throttling heavy jobs."""
    call = lambda: fn(*args, **kwargs)  # noqa: E731
    if heavy:
        async with HEAVY_SLOTS:
            return await anyio.to_thread.run_sync(call)
    return await anyio.to_thread.run_sync(call)


async def one(file: UploadFile, ws: Workspace, allowed, signature="zip", word=True) -> Path:
    path = await save_upload(file, ws, allowed=allowed, signature=signature)
    if word:
        assert_wordprocessing(path)
    return path


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #


@app.get("/health", tags=["meta"])
@limiter.exempt
async def health():
    return {
        "status": "ok",
        "version": app.version,
        "engines": {
            "libreoffice": soffice_available(),
            "ocr": _module_available("pytesseract"),
            "pdf2docx": _module_available("pdf2docx"),
            "encryption": _module_available("msoffcrypto"),
        },
        "limits": {
            "max_file_mb": MAX_FILE_BYTES // (1024 * 1024),
            "max_request_mb": MAX_REQUEST_BYTES // (1024 * 1024),
            "max_files": MAX_FILES,
        },
    }


def _module_available(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


# --------------------------------------------------------------------------- #
# Tools — Word in, Word out
# --------------------------------------------------------------------------- #


@app.post("/api/tools/merge-docx", tags=["tools"])
@limiter.limit(RATE_HEAVY)
async def merge(request: Request,
                files: List[UploadFile] = File(..., description="Two or more .docx files"),
                page_break: bool = Form(True)):
    ws = Workspace.create()
    try:
        if len(files) < 2:
            raise ToolError("Select at least two documents to merge.", code="not_enough_files")
        if len(files) > MAX_FILES:
            raise ToolError(f"At most {MAX_FILES} files per merge.", code="too_many_files")
        saved = []
        for i, f in enumerate(files):
            path = await save_upload(f, ws, allowed=DOCX_LIKE, index=i)
            assert_wordprocessing(path)
            saved.append(path)
        out = ws.out / "merged.docx"
        await run(tools.merge_docx, saved, out, page_break)
        return deliver(out, ws, "merged_document.docx")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/tools/split-docx", tags=["tools"])
@limiter.limit(RATE_HEAVY)
async def split(request: Request, file: UploadFile = File(...), mode: str = Form("pages"),
                every: int = Form(5)):
    ws = Workspace.create()
    try:
        if mode not in ("pages", "count"):
            raise ToolError("mode must be 'pages' or 'count'.", code="invalid_parameter")
        src = await one(file, ws, DOCX_LIKE)
        out = ws.out / "parts.zip"
        await run(tools.split_docx, src, ws.out, out, mode, every)
        return deliver(out, ws, f"{safe_stem(file.filename)}_parts.zip")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/tools/compress-docx", tags=["tools"])
@limiter.limit(RATE_HEAVY)
async def compress(request: Request, file: UploadFile = File(...), max_dimension: int = Form(1600),
                   quality: int = Form(78)):
    ws = Workspace.create()
    try:
        src = await one(file, ws, DOCX_LIKE)
        out = ws.out / "compressed.docx"
        await run(tools.compress_docx, src, out, max(320, min(4000, max_dimension)),
                  max(30, min(95, quality)), heavy=True)
        return deliver(out, ws, f"{safe_stem(file.filename)}_compressed.docx")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/tools/rtl-fixer", tags=["tools"])
@limiter.limit(RATE_LIGHT)
async def rtl_fixer(request: Request, file: UploadFile = File(...), force_all: bool = Form(False)):
    ws = Workspace.create()
    try:
        src = await one(file, ws, DOCX_LIKE)
        out = ws.out / "rtl.docx"
        await run(tools.fix_rtl, src, out, force_all)
        return deliver(out, ws, f"{safe_stem(file.filename)}_rtl.docx")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/tools/watermark-word", tags=["tools"])
@limiter.limit(RATE_LIGHT)
async def watermark(request: Request, file: UploadFile = File(...), text: str = Form(...),
                    color: str = Form("C0C0C0"), opacity: float = Form(0.5),
                    rotation: int = Form(315)):
    ws = Workspace.create()
    try:
        src = await one(file, ws, DOCX_LIKE)
        out = ws.out / "watermarked.docx"
        await run(tools.add_watermark, src, out, text, color=color, opacity=opacity,
                  rotation=rotation)
        return deliver(out, ws, f"{safe_stem(file.filename)}_watermarked.docx")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/tools/remove-blank-pages", tags=["tools"])
@limiter.limit(RATE_LIGHT)
async def blank_pages(request: Request, file: UploadFile = File(...)):
    ws = Workspace.create()
    try:
        src = await one(file, ws, DOCX_LIKE)
        out = ws.out / "cleaned.docx"
        await run(tools.remove_blank_pages, src, out)
        return deliver(out, ws, f"{safe_stem(file.filename)}_cleaned.docx")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/tools/find-replace", tags=["tools"])
@limiter.limit(RATE_LIGHT)
async def find_replace(request: Request, file: UploadFile = File(...), search: str = Form(...),
                       replace: str = Form(""), match_case: bool = Form(True),
                       whole_word: bool = Form(False)):
    ws = Workspace.create()
    try:
        src = await one(file, ws, DOCX_LIKE)
        out = ws.out / "replaced.docx"
        await run(tools.find_replace, src, out, search, replace,
                  match_case=match_case, whole_word=whole_word)
        return deliver(out, ws, f"{safe_stem(file.filename)}_replaced.docx")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/tools/clean-formatting", tags=["tools"])
@limiter.limit(RATE_LIGHT)
async def clean(request: Request, file: UploadFile = File(...), font_name: str = Form("Calibri"),
                font_size: int = Form(12), keep_emphasis: bool = Form(True)):
    ws = Workspace.create()
    try:
        src = await one(file, ws, DOCX_LIKE)
        out = ws.out / "clean.docx"
        await run(tools.clean_formatting, src, out, font_name[:64],
                  max(6, min(72, font_size)), keep_emphasis)
        return deliver(out, ws, f"{safe_stem(file.filename)}_clean.docx")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/tools/remove-metadata", tags=["tools"])
@limiter.limit(RATE_LIGHT)
async def strip_metadata(request: Request, file: UploadFile = File(...)):
    ws = Workspace.create()
    try:
        src = await one(file, ws, DOCX_LIKE)
        out = ws.out / "anonymous.docx"
        await run(tools.remove_metadata, src, out)
        return deliver(out, ws, f"{safe_stem(file.filename)}_no_metadata.docx")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/tools/protect-word", tags=["tools"])
@limiter.limit(RATE_LIGHT)
async def protect(request: Request, file: UploadFile = File(...), password: str = Form(...)):
    ws = Workspace.create()
    try:
        src = await one(file, ws, DOCX_LIKE)
        out = ws.out / "protected.docx"
        await run(tools.protect_docx, src, out, password)
        return deliver(out, ws, f"{safe_stem(file.filename)}_protected.docx")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/tools/unlock-word", tags=["tools"])
@limiter.limit(RATE_LIGHT)
async def unlock(request: Request, file: UploadFile = File(...), password: str = Form("")):
    """
    Removes protection from a document you can already open: encrypted files
    are decrypted with the password you supply. No password recovery.
    """
    ws = Workspace.create()
    try:
        # An encrypted .docx is an OLE container, not a zip — skip both checks.
        src = await save_upload(file, ws, allowed=DOCX_LIKE, signature=None)
        out = ws.out / "unlocked.docx"
        await run(tools.unlock_docx, src, out, password or None)
        return deliver(out, ws, f"{safe_stem(file.filename)}_unlocked.docx")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/tools/extract-text", tags=["tools"])
@limiter.limit(RATE_LIGHT)
async def extract_text(request: Request, file: UploadFile = File(...)):
    ws = Workspace.create()
    try:
        src = await one(file, ws, DOCX_LIKE)
        out = ws.out / "text.txt"
        await run(tools.extract_text, src, out)
        return deliver(out, ws, f"{safe_stem(file.filename)}.txt")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/tools/extract-images", tags=["tools"])
@limiter.limit(RATE_LIGHT)
async def extract_images(request: Request, file: UploadFile = File(...)):
    ws = Workspace.create()
    try:
        src = await one(file, ws, DOCX_LIKE)
        out = ws.out / "images.zip"
        await run(tools.extract_images, src, ws.out, out)
        return deliver(out, ws, f"{safe_stem(file.filename)}_images.zip")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/tools/word-stats", tags=["tools"])
@limiter.limit(RATE_HEAVY)
async def stats(request: Request, file: UploadFile = File(...)):
    """The one tool that answers with JSON rather than a download."""
    ws = Workspace.create()
    try:
        src = await one(file, ws, DOCX_LIKE)
        data = await run(tools.word_stats, src, ws.out, heavy=True)
        return JSONResponse({"filename": file.filename, "stats": data})
    finally:
        ws.dispose()


# --------------------------------------------------------------------------- #
# Conversions
# --------------------------------------------------------------------------- #


@app.post("/api/convert/word-to-pdf", tags=["convert"])
@limiter.limit(RATE_HEAVY)
async def word_to_pdf(request: Request, file: UploadFile = File(...)):
    ws = Workspace.create()
    try:
        src = await one(file, ws, DOCX_LIKE)
        produced = await run(tools.docx_to_pdf, src, ws.out, heavy=True)
        return deliver(produced, ws, f"{safe_stem(file.filename)}.pdf")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/convert/pdf-to-docx", tags=["convert"])
@limiter.limit(RATE_HEAVY)
async def pdf_to_docx(request: Request, file: UploadFile = File(...), start_page: int = Form(1),
                      end_page: int = Form(0)):
    ws = Workspace.create()
    try:
        src = await save_upload(file, ws, allowed=PDF, signature="pdf")
        out = ws.out / "converted.docx"
        start = max(0, start_page - 1)
        end = end_page if end_page > 0 else None
        await run(tools.pdf_to_docx, src, out, start, end, heavy=True)
        return deliver(out, ws, f"{safe_stem(file.filename)}.docx")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/convert/word-to-jpg", tags=["convert"])
@limiter.limit(RATE_HEAVY)
async def word_to_jpg(request: Request, file: UploadFile = File(...), dpi: int = Form(150),
                      image_format: str = Form("jpg")):
    ws = Workspace.create()
    try:
        src = await one(file, ws, DOCX_LIKE)
        out = ws.out / "pages.zip"
        await run(tools.docx_to_images, src, ws.root, ws.out, out, dpi, image_format, heavy=True)
        return deliver(out, ws, f"{safe_stem(file.filename)}_pages.zip")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/convert/image-to-docx", tags=["convert"])
@limiter.limit(RATE_HEAVY)
async def image_to_docx(request: Request, file: UploadFile = File(...), languages: str = Form("")):
    ws = Workspace.create()
    try:
        src = await save_upload(file, ws, allowed=IMAGES, signature=None)
        out = ws.out / "ocr.docx"
        await run(tools.image_to_docx, src, out, languages or None, heavy=True)
        return deliver(out, ws, f"{safe_stem(file.filename)}_ocr.docx")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/convert/word-to-html", tags=["convert"])
@limiter.limit(RATE_LIGHT)
async def word_to_html(request: Request, file: UploadFile = File(...)):
    ws = Workspace.create()
    try:
        src = await one(file, ws, DOCX_LIKE)
        out = ws.out / "document.html"
        await run(tools.docx_to_html, src, out, safe_stem(file.filename))
        return deliver(out, ws, f"{safe_stem(file.filename)}.html")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/convert/html-to-word", tags=["convert"])
@limiter.limit(RATE_HEAVY)
async def html_to_word(request: Request, file: UploadFile = File(...)):
    ws = Workspace.create()
    try:
        src = await save_upload(file, ws, allowed=HTML, signature=None)
        produced = await run(tools.html_to_docx, src, ws.out, heavy=True)
        return deliver(produced, ws, f"{safe_stem(file.filename)}.docx")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/convert/markdown-to-word", tags=["convert"])
@limiter.limit(RATE_HEAVY)
async def markdown_to_word(request: Request, file: UploadFile = File(...)):
    ws = Workspace.create()
    try:
        src = await save_upload(file, ws, allowed=MARKDOWN, signature=None)
        produced = await run(tools.markdown_to_docx, src, ws.root, ws.out, heavy=True)
        return deliver(produced, ws, f"{safe_stem(file.filename)}.docx")
    except BaseException:
        ws.dispose()
        raise


@app.post("/api/convert/text-to-word", tags=["convert"])
@limiter.limit(RATE_LIGHT)
async def text_to_word(request: Request, file: UploadFile = File(...)):
    ws = Workspace.create()
    try:
        src = await save_upload(file, ws, allowed=(".txt",), signature=None)
        out = ws.out / "converted.docx"
        await run(tools.text_to_docx, src, out)
        return deliver(out, ws, f"{safe_stem(file.filename)}.docx")
    except BaseException:
        ws.dispose()
        raise


# --------------------------------------------------------------------------- #
# Static frontend (optional — serves index.html when present)
# --------------------------------------------------------------------------- #

_frontend = Path(__file__).resolve().parent.parent / "frontend"
if _frontend.is_dir():
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")
