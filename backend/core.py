"""
ILoveWord — shared infrastructure.

Everything that is not a document operation lives here: configuration,
upload handling, sandboxed temp workspaces, the LibreOffice wrapper and the
response helpers. Imported by main.py (routing) and tools.py (operations).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

log = logging.getLogger("iloveword")

# --------------------------------------------------------------------------- #
# Configuration (all overridable by environment variable)
# --------------------------------------------------------------------------- #


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


MAX_FILE_BYTES = _int_env("ILW_MAX_FILE_MB", 50) * 1024 * 1024
MAX_REQUEST_BYTES = _int_env("ILW_MAX_REQUEST_MB", 200) * 1024 * 1024
MAX_FILES = _int_env("ILW_MAX_FILES", 20)
SOFFICE_TIMEOUT = _int_env("ILW_SOFFICE_TIMEOUT", 180)
MAX_CONCURRENT_HEAVY = _int_env("ILW_MAX_CONCURRENT_HEAVY", 4)
# Hard address-space ceiling for each LibreOffice child. A malformed document
# that sends soffice into unbounded allocation then dies as a failed job
# instead of triggering the kernel OOM killer on the whole container.
SOFFICE_MEM_BYTES = _int_env("ILW_SOFFICE_MEM_MB", 1024) * 1024 * 1024
CLEANUP_INTERVAL = _int_env("ILW_CLEANUP_INTERVAL_SECONDS", 3600)
WORKSPACE_MAX_AGE = _int_env("ILW_WORKSPACE_MAX_AGE_SECONDS", 3600)
# Guard against zip bombs: refuse a .docx whose members inflate beyond this.
MAX_INFLATED_BYTES = _int_env("ILW_MAX_INFLATED_MB", 500) * 1024 * 1024
OCR_LANGS = os.getenv("ILW_OCR_LANGS", "ara+eng")
TMP_ROOT = Path(os.getenv("ILW_TMP_ROOT", tempfile.gettempdir())) / "iloveword"

CORS_ORIGINS = [o.strip() for o in os.getenv("ILW_CORS_ORIGINS", "*").split(",") if o.strip()]

# Only one LibreOffice-class job per slot; soffice is memory hungry and a
# single shared profile deadlocks under concurrency.
HEAVY_SLOTS = asyncio.Semaphore(MAX_CONCURRENT_HEAVY)

MEDIA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".zip": "application/zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
}

# Magic-byte prefixes. A wrong extension is the #1 cause of opaque 500s, so we
# check the bytes rather than trusting the client's filename.
_SIGNATURES = {
    "zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    "pdf": (b"%PDF",),
    "ole": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),  # legacy .doc + encrypted OOXML
}


class ToolError(HTTPException):
    """A predictable, user-facing failure. Never a stack trace."""

    def __init__(self, detail: str, status_code: int = 400, code: str = "tool_error"):
        super().__init__(status_code=status_code, detail=detail)
        self.code = code


# --------------------------------------------------------------------------- #
# Workspace
# --------------------------------------------------------------------------- #


@dataclass
class Workspace:
    """An isolated scratch directory, deleted after the response is flushed."""

    root: Path

    @classmethod
    def create(cls) -> "Workspace":
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        return cls(Path(tempfile.mkdtemp(prefix="job-", dir=TMP_ROOT)))

    @property
    def inp(self) -> Path:
        p = self.root / "in"
        p.mkdir(exist_ok=True)
        return p

    @property
    def out(self) -> Path:
        p = self.root / "out"
        p.mkdir(exist_ok=True)
        return p

    def dispose(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def purge_stale_workspaces(max_age_seconds: int = 3600) -> int:
    """Remove orphans left by a crash or SIGKILL. Called on startup."""
    import time

    removed = 0
    if not TMP_ROOT.exists():
        return 0
    cutoff = time.time() - max_age_seconds
    for child in TMP_ROOT.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


# --------------------------------------------------------------------------- #
# Upload handling
# --------------------------------------------------------------------------- #

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\u0600-\u06FF\u00C0-\u024F-]+")


def safe_stem(filename: str | None, fallback: str = "document") -> str:
    """Filename → a stem safe for disk, Content-Disposition and zip members."""
    name = Path(filename or "").name
    stem = Path(name).stem
    stem = unicodedata.normalize("NFC", stem)
    stem = _SAFE_NAME.sub("_", stem).strip("._")
    return (stem or fallback)[:80]


def extension_of(filename: str | None) -> str:
    return Path(filename or "").suffix.lower()


async def save_upload(
    upload: UploadFile,
    workspace: Workspace,
    *,
    allowed: Sequence[str],
    signature: str | None = "zip",
    index: int = 0,
) -> Path:
    """
    Stream one upload to disk with validation.

    `allowed` is a list of extensions (".docx"). `signature` names a magic-byte
    family the content must match, or None to skip the check.
    """
    ext = extension_of(upload.filename)
    if ext not in allowed:
        raise ToolError(
            f"Unsupported file type '{ext or upload.filename}'. Expected: {', '.join(allowed)}.",
            status_code=415,
            code="unsupported_type",
        )

    target = workspace.inp / f"{index:03d}_{safe_stem(upload.filename)}{ext}"
    size = 0
    try:
        with target.open("wb") as fh:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise ToolError(
                        f"File exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB limit.",
                        status_code=413,
                        code="file_too_large",
                    )
                fh.write(chunk)
    finally:
        await upload.close()

    if size == 0:
        raise ToolError("The uploaded file is empty.", code="empty_file")

    if signature:
        with target.open("rb") as fh:
            head = fh.read(8)
        expected = _SIGNATURES[signature]
        if not any(head.startswith(sig) for sig in expected):
            if signature == "zip" and head.startswith(_SIGNATURES["ole"][0]):
                raise ToolError(
                    "This file is either password-protected or a legacy .doc. "
                    "Use “Unlock Word” first, or save it as .docx.",
                    code="encrypted_or_legacy",
                )
            raise ToolError(
                "The file contents do not match its extension — it may be renamed or corrupt.",
                code="signature_mismatch",
            )

    if signature == "zip":
        _assert_sane_zip(target)

    return target


def _assert_sane_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as zf:
            total = sum(i.file_size for i in zf.infolist())
            if total > MAX_INFLATED_BYTES:
                raise ToolError("The document expands to an unreasonable size.", code="zip_bomb")
            for info in zf.infolist():
                name = info.filename
                if name.startswith("/") or ".." in Path(name).parts:
                    raise ToolError("The document contains unsafe entry paths.", code="zip_traversal")
    except zipfile.BadZipFile:
        raise ToolError("The document is corrupt and cannot be opened.", code="corrupt_document")


def assert_wordprocessing(path: Path) -> None:
    """A valid .docx zip still has to actually be a Word document."""
    with zipfile.ZipFile(path) as zf:
        if "word/document.xml" not in zf.namelist():
            raise ToolError(
                "This .docx does not contain a Word document part.", code="not_a_word_document"
            )


# --------------------------------------------------------------------------- #
# LibreOffice
# --------------------------------------------------------------------------- #

SOFFICE_BIN = shutil.which("soffice") or shutil.which("libreoffice")


def soffice_available() -> bool:
    return SOFFICE_BIN is not None


def soffice_convert(src: Path, outdir: Path, target: str, *, filter_name: str | None = None) -> Path:
    """
    Convert via LibreOffice headless.

    A private UserInstallation profile per call is what makes this safe to run
    concurrently — the default shared profile takes an exclusive lock and the
    second process silently hangs until timeout.
    """
    if not SOFFICE_BIN:
        raise ToolError(
            "LibreOffice is not installed on the server; this conversion is unavailable.",
            status_code=503,
            code="engine_unavailable",
        )
    outdir.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="soffice-", dir=src.parent))
    convert_to = f"{target}:{filter_name}" if filter_name else target
    cmd = [
        SOFFICE_BIN,
        f"-env:UserInstallation=file://{profile}",
        "--headless",
        "--norestore",
        "--nolockcheck",
        "--nodefault",
        "--nofirststartwizard",
        "--convert-to",
        convert_to,
        "--outdir",
        str(outdir),
        str(src),
    ]
    # RLIMIT_AS / preexec_fn are POSIX-only: `resource` does not exist on
    # Windows, and subprocess.run raises ValueError if preexec_fn is passed
    # there at all. On Windows this cap is simply skipped — LibreOffice runs
    # unbounded, which is fine for local development. In the Docker image
    # (always Linux) the cap is applied as intended.
    preexec_fn = None
    if sys.platform != "win32" and SOFFICE_MEM_BYTES > 0:
        import resource  # local import: keeps the module importable on Windows

        def _cap_memory() -> None:  # runs in the child, after fork, before exec
            try:
                resource.setrlimit(resource.RLIMIT_AS, (SOFFICE_MEM_BYTES, SOFFICE_MEM_BYTES))
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            except (ValueError, OSError):
                pass

        preexec_fn = _cap_memory

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=SOFFICE_TIMEOUT,
            preexec_fn=preexec_fn,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(
            "Conversion timed out. The document may be unusually large or complex.",
            status_code=504,
            code="conversion_timeout",
        )
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    produced = outdir / f"{src.stem}.{target.split(':')[0]}"
    if proc.returncode != 0 or not produced.exists():
        log.warning("soffice failed rc=%s stderr=%s", proc.returncode, proc.stderr[:500])
        raise ToolError(
            "The conversion engine could not process this document.",
            status_code=502,
            code="conversion_failed",
        )
    return produced


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


def deliver(path: Path, workspace: Workspace, download_name: str) -> FileResponse:
    """Return a produced file and schedule the workspace for deletion."""
    if not path.exists() or path.stat().st_size == 0:
        workspace.dispose()
        raise ToolError("The operation produced no output.", status_code=500, code="empty_output")
    media = MEDIA_TYPES.get(Path(download_name).suffix.lower(), "application/octet-stream")
    return FileResponse(
        path,
        media_type=media,
        filename=download_name,
        background=BackgroundTask(workspace.dispose),
        headers={"Cache-Control": "no-store"},
    )


def zip_directory(files: Iterable[Path], destination: Path) -> Path:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in files:
            zf.write(f, arcname=f.name)
    return destination


