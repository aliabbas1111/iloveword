"""
ILoveWord — document operations.

Every function here is synchronous and pure with respect to the filesystem:
it takes input paths plus an output path and returns the path it wrote.
main.py runs them in a worker thread so the event loop is never blocked.
"""

from __future__ import annotations

import html as html_lib
import io
import logging
import re
import shutil
import zipfile
from pathlib import Path
from typing import Sequence

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

from core import OCR_LANGS, ToolError, soffice_convert, zip_directory

log = logging.getLogger("iloveword.tools")

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
RTL_CHARS = re.compile(r"[\u0590-\u05FF\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _iter_paragraphs(doc: Document):
    """Every paragraph in the document body, tables, headers and footers."""
    yield from doc.paragraphs
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs
                for inner in cell.tables:
                    for r in inner.rows:
                        for c in r.cells:
                            yield from c.paragraphs
    for section in doc.sections:
        for part in (section.header, section.footer, section.first_page_header,
                     section.first_page_footer, section.even_page_header, section.even_page_footer):
            if part is None:
                continue
            yield from part.paragraphs
            for table in part.tables:
                for row in table.rows:
                    for cell in row.cells:
                        yield from cell.paragraphs


def _get_or_add(parent, tag: str):
    el = parent.find(qn(tag))
    if el is None:
        el = OxmlElement(tag)
        parent.append(el)
    return el


def _pPr(paragraph):
    return paragraph._p.get_or_add_pPr()


def _rPr(run):
    return run._r.get_or_add_rPr()


# --------------------------------------------------------------------------- #
# Merge / split
# --------------------------------------------------------------------------- #


def merge_docx(sources: Sequence[Path], out: Path, page_break: bool = True) -> Path:
    from docxcompose.composer import Composer

    if len(sources) < 2:
        raise ToolError("Merging needs at least two documents.", code="not_enough_files")

    master = Document(str(sources[0]))
    composer = Composer(master)
    for src in sources[1:]:
        if page_break:
            master.add_page_break()
        try:
            composer.append(Document(str(src)))
        except Exception as exc:  # noqa: BLE001 - one bad file shouldn't be a 500
            log.warning("merge: skipping %s (%s)", src.name, exc)
            raise ToolError(
                f"“{src.name}” could not be merged; it may be corrupt or not a real .docx.",
                code="merge_failed",
            )
    composer.save(str(out))
    return out


def _starts_new_page(el) -> bool:
    pPr = el.find(f"{W}pPr")
    if pPr is not None and pPr.find(f"{W}pageBreakBefore") is not None:
        return True
    return False


def _contains_page_break(el) -> bool:
    return any(br.get(f"{W}type") == "page" for br in el.iter(f"{W}br"))


def split_docx(src: Path, outdir: Path, out_zip: Path, mode: str = "pages", every: int = 5) -> Path:
    """
    Split into parts. `mode="pages"` splits on explicit page breaks,
    `mode="count"` splits every `every` paragraphs. Returns a zip of parts.
    """
    probe = Document(str(src))
    blocks = [el for el in probe.element.body.iterchildren() if el.tag in (f"{W}p", f"{W}tbl")]
    if not blocks:
        raise ToolError("The document has no content to split.", code="empty_document")

    groups: list[list[int]] = [[]]
    for i, el in enumerate(blocks):
        if groups[-1] and (
            (mode == "pages" and _starts_new_page(el))
            or (mode == "count" and len(groups[-1]) >= max(1, every))
        ):
            groups.append([])
        groups[-1].append(i)
        if mode == "pages" and _contains_page_break(el) and i != len(blocks) - 1:
            groups.append([])
    groups = [g for g in groups if g]

    if len(groups) < 2:
        raise ToolError(
            "No split points were found. Insert page breaks, or choose “every N paragraphs”.",
            code="no_split_points",
        )

    parts: list[Path] = []
    for n, keep in enumerate(groups, start=1):
        doc = Document(str(src))
        current = [el for el in doc.element.body.iterchildren() if el.tag in (f"{W}p", f"{W}tbl")]
        wanted = set(keep)
        for i, el in enumerate(current):
            if i not in wanted:
                el.getparent().remove(el)
        part = outdir / f"part_{n:02d}.docx"
        doc.save(str(part))
        parts.append(part)

    return zip_directory(parts, out_zip)


# --------------------------------------------------------------------------- #
# Compress / clean / metadata
# --------------------------------------------------------------------------- #


def compress_docx(src: Path, out: Path, max_dimension: int = 1600, quality: int = 78) -> Path:
    """Downsample embedded raster images and re-deflate the package."""
    from PIL import Image

    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(
        out, "w", zipfile.ZIP_DEFLATED, compresslevel=9
    ) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            lower = info.filename.lower()
            if lower.startswith("word/media/") and lower.endswith(
                (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif")
            ):
                data = _shrink_image(Image, data, lower, max_dimension, quality)
            zout.writestr(info.filename, data, zipfile.ZIP_DEFLATED)

    if out.stat().st_size >= src.stat().st_size:
        shutil.copyfile(src, out)  # never hand back something larger
    return out


def _shrink_image(Image, data: bytes, name: str, max_dimension: int, quality: int) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            has_alpha = img.mode in ("RGBA", "LA") or "transparency" in img.info
            if max(img.size) > max_dimension:
                ratio = max_dimension / max(img.size)
                img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                                 Image.LANCZOS)
            buf = io.BytesIO()
            if has_alpha or name.endswith(".png"):
                img.save(buf, format="PNG", optimize=True)
            else:
                img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True,
                                        progressive=True)
            return buf.getvalue() if buf.tell() < len(data) else data
    except Exception:  # noqa: BLE001 — an unreadable image is left untouched
        return data


def clean_formatting(src: Path, out: Path, font_name: str = "Calibri", font_size: int = 12,
                     keep_emphasis: bool = True) -> Path:
    doc = Document(str(src))
    drop = {"color", "highlight", "shd", "rFonts", "sz", "szCs", "spacing", "w", "position",
            "outline", "shadow", "emboss", "imprint", "smallCaps", "caps", "effect"}
    if not keep_emphasis:
        drop |= {"b", "bCs", "i", "iCs", "u", "strike"}

    for paragraph in _iter_paragraphs(doc):
        for run in paragraph.runs:
            rPr = _rPr(run)
            for child in list(rPr):
                if child.tag.replace(W, "") in drop:
                    rPr.remove(child)
            run.font.name = font_name
            run.font.size = Pt(font_size)
            rFonts = _get_or_add(rPr, "w:rFonts")
            for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
                rFonts.set(qn(attr), font_name)
    doc.save(str(out))
    return out


def remove_metadata(src: Path, out: Path) -> Path:
    """Strip authorship, revision history and custom properties."""
    blank_core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<dc:title></dc:title><dc:creator></dc:creator><cp:lastModifiedBy></cp:lastModifiedBy>"
        "<cp:revision>1</cp:revision></cp:coreProperties>"
    ).encode()
    blank_app = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        "<Application>ILoveWord</Application><Company></Company><Manager></Manager></Properties>"
    ).encode()

    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            name = info.filename
            if name == "docProps/custom.xml":
                continue
            data = zin.read(name)
            if name == "docProps/core.xml":
                data = blank_core
            elif name == "docProps/app.xml":
                data = blank_app
            elif name == "[Content_Types].xml":
                data = data.replace(
                    b'<Override PartName="/docProps/custom.xml" '
                    b'ContentType="application/vnd.openxmlformats-officedocument.custom-properties+xml"/>',
                    b"",
                )
            elif name == "_rels/.rels":
                data = re.sub(rb"<Relationship[^>]*custom\.xml[^>]*/>", b"", data)
            zout.writestr(info, data)
    return out


# --------------------------------------------------------------------------- #
# RTL, watermark, blank pages, find & replace
# --------------------------------------------------------------------------- #


def fix_rtl(src: Path, out: Path, force_all: bool = False) -> Path:
    """
    Real RTL repair: set paragraph bidi + run rtl + right alignment, and flip
    the section direction. Alignment alone (the old behaviour) does not fix
    punctuation or mixed-script ordering.
    """
    doc = Document(str(src))
    for paragraph in _iter_paragraphs(doc):
        text = paragraph.text
        if not force_all and not RTL_CHARS.search(text):
            continue
        pPr = _pPr(paragraph)
        _get_or_add(pPr, "w:bidi")
        if paragraph.alignment in (None, WD_ALIGN_PARAGRAPH.LEFT):
            paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        for run in paragraph.runs:
            rPr = _rPr(run)
            _get_or_add(rPr, "w:rtl")
            _get_or_add(rPr, "w:cs")
    for section in doc.sections:
        _get_or_add(section._sectPr, "w:bidi")
    doc.save(str(out))
    return out


_WATERMARK_XML = """
<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:r>
    <w:pict xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
      <v:shapetype id="_x0000_t136" coordsize="21600,21600" o:spt="136" adj="10800" path="m@7,l@8,m@5,21600l@6,21600e">
        <v:formulas>
          <v:f eqn="sum #0 0 10800"/><v:f eqn="prod #0 2 1"/><v:f eqn="sum 21600 0 @1"/>
          <v:f eqn="sum 0 0 @2"/><v:f eqn="sum 21600 0 @3"/><v:f eqn="if @0 @3 0"/>
          <v:f eqn="if @0 21600 @1"/><v:f eqn="if @0 0 @2"/><v:f eqn="if @0 @4 21600"/>
          <v:f eqn="mid @5 @6"/><v:f eqn="mid @8 @5"/><v:f eqn="mid @7 @8"/>
          <v:f eqn="mid @6 @7"/><v:f eqn="sum @6 0 @5"/>
        </v:formulas>
        <v:path textpathok="t" o:connecttype="custom"/>
        <v:textpath on="t" fitshape="t"/>
      </v:shapetype>
      <v:shape id="ILoveWordWatermark{idx}" o:spid="_x0000_s2050" type="#_x0000_t136" o:allowincell="f" fillcolor="#{color}" stroked="f" style="position:absolute;margin-left:0;margin-top:0;width:{width}pt;height:{height}pt;rotation:{rotation};z-index:-251654144;mso-position-horizontal:center;mso-position-horizontal-relative:margin;mso-position-vertical:center;mso-position-vertical-relative:margin">
        <v:fill opacity="{opacity}"/>
        <v:textpath style="font-family:&quot;Calibri&quot;;font-size:1pt;font-weight:bold" string="{text}"/>
      </v:shape>
    </w:pict>
  </w:r>
</w:p>
"""


def add_watermark(src: Path, out: Path, text: str, *, color: str = "C0C0C0",
                  opacity: float = 0.5, rotation: int = 315) -> Path:
    from docx.oxml import parse_xml

    text = (text or "").strip()
    if not text:
        raise ToolError("Watermark text is required.", code="missing_parameter")
    if len(text) > 60:
        raise ToolError("Watermark text must be 60 characters or fewer.", code="invalid_parameter")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", color):
        raise ToolError("Colour must be a 6-digit hex value, e.g. C0C0C0.", code="invalid_parameter")

    escaped = html_lib.escape(text, quote=True)
    width = min(468, max(180, len(text) * 24))
    doc = Document(str(src))
    for idx, section in enumerate(doc.sections):
        for header in (section.header, section.first_page_header, section.even_page_header):
            if header is None:
                continue
            xml = _WATERMARK_XML.format(
                idx=idx, text=escaped, color=color, width=width, height=max(60, width // 4),
                opacity=f"{max(0.05, min(1.0, opacity)):.2f}", rotation=rotation,
            )
            paragraph = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
            paragraph._p.addnext(parse_xml(xml))
    doc.save(str(out))
    return out


def remove_blank_pages(src: Path, out: Path) -> Path:
    """
    Remove empty paragraphs and the page breaks that produce blank pages,
    leaving a single separating paragraph where content resumes.
    """
    doc = Document(str(src))
    body = doc.element.body
    blocks = [el for el in body.iterchildren() if el.tag in (f"{W}p", f"{W}tbl")]
    removed = 0
    run_of_empties: list = []

    def flush(keep_one: bool) -> None:
        nonlocal removed
        targets = run_of_empties[1:] if keep_one else run_of_empties
        for el in targets:
            el.getparent().remove(el)
            removed += 1
        run_of_empties.clear()

    for el in blocks:
        if el.tag == f"{W}p":
            text = "".join(t.text or "" for t in el.iter(f"{W}t")).strip()
            has_media = el.find(f".//{W}drawing") is not None or el.find(".//{*}pict") is not None
            if not text and not has_media:
                run_of_empties.append(el)
                continue
        flush(keep_one=True)
    flush(keep_one=False)

    for el in list(body.iter(f"{W}br")):
        if el.get(f"{W}type") == "page":
            parent = el.getparent()
            siblings = "".join(t.text or "" for t in parent.iter(f"{W}t")).strip()
            if not siblings:
                el.getparent().remove(el)
                removed += 1

    doc.save(str(out))
    return out


def find_replace(src: Path, out: Path, search: str, replace: str, *, match_case: bool = True,
                 whole_word: bool = False) -> Path:
    if not search:
        raise ToolError("A search term is required.", code="missing_parameter")

    pattern = re.compile(
        (r"\b%s\b" if whole_word else "%s") % re.escape(search),
        0 if match_case else re.IGNORECASE,
    )
    doc = Document(str(src))
    count = 0

    for paragraph in _iter_paragraphs(doc):
        runs = paragraph.runs
        if not runs:
            continue
        joined = "".join(r.text for r in runs)
        if not pattern.search(joined):
            continue
        new_text, n = pattern.subn(replace, joined)
        count += n
        # Keep the first run's formatting; blank the rest. Matches that span
        # run boundaries are handled correctly because we operate on the join.
        runs[0].text = new_text
        for run in runs[1:]:
            run.text = ""

    if count == 0:
        raise ToolError(f"“{search}” was not found in the document.", code="no_matches")
    doc.save(str(out))
    return out


# --------------------------------------------------------------------------- #
# Protection
# --------------------------------------------------------------------------- #


def protect_docx(src: Path, out: Path, password: str) -> Path:
    """AES-encrypt the package (Word's “Encrypt with Password”)."""
    import msoffcrypto

    if len(password) < 4:
        raise ToolError("Password must be at least 4 characters.", code="weak_password")
    try:
        with src.open("rb") as fin, out.open("wb") as fout:
            office = msoffcrypto.OfficeFile(fin)
            office.encrypt(password, fout)
    except Exception as exc:  # noqa: BLE001
        log.warning("encrypt failed: %s", exc)
        raise ToolError(
            "This document could not be encrypted. It may already be protected.",
            code="encryption_failed",
        )
    return out


def unlock_docx(src: Path, out: Path, password: str | None) -> Path:
    """
    Remove protection the user is entitled to remove.

    Encrypted packages are decrypted with the password the user supplies — no
    password recovery, by design. Editing restrictions (which are an author
    convention, not encryption) are then cleared.
    """
    import msoffcrypto

    working = src
    with src.open("rb") as fh:
        try:
            office = msoffcrypto.OfficeFile(fh)
            encrypted = office.is_encrypted()
        except Exception:  # noqa: BLE001 — a plain .docx isn't an OLE container
            encrypted = False

        if encrypted:
            if not password:
                raise ToolError(
                    "This document is encrypted. Enter its password to unlock it.",
                    status_code=401,
                    code="password_required",
                )
            decrypted = src.with_name(src.stem + "_decrypted.docx")
            try:
                office.load_key(password=password)
                with decrypted.open("wb") as fout:
                    office.decrypt(fout)
            except Exception:  # noqa: BLE001
                raise ToolError(
                    "Incorrect password.", status_code=401, code="bad_password"
                )
            working = decrypted

    with zipfile.ZipFile(working) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "word/settings.xml":
                data = re.sub(rb"<w:documentProtection[^>]*/>", b"", data)
                data = re.sub(rb"<w:writeProtection[^>]*/>", b"", data)
            zout.writestr(info, data)
    return out


# --------------------------------------------------------------------------- #
# Extraction & statistics
# --------------------------------------------------------------------------- #


def extract_text(src: Path, out: Path) -> Path:
    doc = Document(str(src))
    lines: list[str] = []
    for block in doc.element.body.iterchildren():
        if block.tag == f"{W}p":
            lines.append("".join(t.text or "" for t in block.iter(f"{W}t")))
        elif block.tag == f"{W}tbl":
            for row in block.iter(f"{W}tr"):
                cells = ["".join(t.text or "" for t in tc.iter(f"{W}t")) for tc in row.iter(f"{W}tc")]
                lines.append("\t".join(cells))
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def extract_images(src: Path, outdir: Path, out_zip: Path) -> Path:
    found: list[Path] = []
    with zipfile.ZipFile(src) as zf:
        for info in zf.infolist():
            if not info.filename.lower().startswith("word/media/"):
                continue
            name = Path(info.filename).name
            if not name:
                continue
            target = outdir / name
            target.write_bytes(zf.read(info.filename))
            found.append(target)
    if not found:
        raise ToolError("This document contains no embedded images.", code="no_images")
    return zip_directory(found, out_zip)


def word_stats(src: Path, workdir: Path) -> dict:
    doc = Document(str(src))
    paragraphs = [p.text for p in doc.paragraphs]
    text = "\n".join(paragraphs)
    words = re.findall(r"[\w\u0600-\u06FF'’-]+", text, re.UNICODE)
    with zipfile.ZipFile(src) as zf:
        images = sum(1 for n in zf.namelist() if n.lower().startswith("word/media/"))

    pages = None
    try:  # exact page count, via a real render
        import pymupdf

        pdf = soffice_convert(src, workdir, "pdf")
        with pymupdf.open(pdf) as d:
            pages = d.page_count
    except Exception:  # noqa: BLE001 — statistics must never fail the request
        pages = max(1, round(len(words) / 500)) or 1

    rtl_words = sum(1 for w in words if RTL_CHARS.search(w))
    return {
        "words": len(words),
        "characters": len(text),
        "characters_no_spaces": len(re.sub(r"\s", "", text)),
        "paragraphs": sum(1 for p in paragraphs if p.strip()),
        "tables": len(doc.tables),
        "images": images,
        "sections": len(doc.sections),
        "pages": pages,
        "rtl_ratio": round(rtl_words / len(words), 3) if words else 0.0,
        "reading_time_minutes": max(1, round(len(words) / 220)),
    }


# --------------------------------------------------------------------------- #
# Conversions
# --------------------------------------------------------------------------- #


def docx_to_pdf(src: Path, outdir: Path) -> Path:
    return soffice_convert(src, outdir, "pdf")


def pdf_to_docx(src: Path, out: Path, start: int = 0, end: int | None = None) -> Path:
    from pdf2docx import Converter

    cv = None
    try:
        cv = Converter(str(src))
        cv.convert(str(out), start=start, end=end)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "password" in msg or "encrypt" in msg:
            raise ToolError("This PDF is password-protected.", status_code=401,
                            code="pdf_encrypted")
        log.warning("pdf2docx failed: %s", exc)
        raise ToolError(
            "This PDF could not be converted. Scanned PDFs need the OCR tool instead.",
            code="pdf_conversion_failed",
        )
    finally:
        if cv is not None:
            try:
                cv.close()
            except Exception:  # noqa: BLE001
                pass
    return out


def docx_to_images(src: Path, workdir: Path, outdir: Path, out_zip: Path, dpi: int = 150,
                   fmt: str = "jpg") -> Path:
    import pymupdf

    dpi = max(72, min(300, dpi))
    fmt = "png" if fmt.lower() == "png" else "jpg"
    pdf = soffice_convert(src, workdir, "pdf")
    produced: list[Path] = []
    with pymupdf.open(pdf) as doc:
        if doc.page_count == 0:
            raise ToolError("The document has no pages to render.", code="empty_document")
        for number, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=dpi)
            target = outdir / f"page_{number:03d}.{fmt}"
            pix.save(target, jpg_quality=88) if fmt == "jpg" else pix.save(target)
            produced.append(target)
    return zip_directory(produced, out_zip)


def image_to_docx(src: Path, out: Path, langs: str | None = None) -> Path:
    import pytesseract
    from PIL import Image

    langs = langs or OCR_LANGS
    try:
        with Image.open(src) as img:
            img.load()
            text = pytesseract.image_to_string(img, lang=langs)
    except pytesseract.TesseractNotFoundError:
        raise ToolError("OCR is not installed on the server.", status_code=503,
                        code="engine_unavailable")
    except pytesseract.TesseractError as exc:
        if "Failed loading language" in str(exc):
            with Image.open(src) as img:
                text = pytesseract.image_to_string(img, lang="eng")
        else:
            raise ToolError("The image could not be read by OCR.", code="ocr_failed")
    except Exception:  # noqa: BLE001
        raise ToolError("The image could not be opened.", code="bad_image")

    if not text.strip():
        raise ToolError("No readable text was found in this image.", code="no_text_found")

    doc = Document()
    for line in text.splitlines():
        paragraph = doc.add_paragraph(line)
        if RTL_CHARS.search(line):
            paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            _get_or_add(_pPr(paragraph), "w:bidi")
            for run in paragraph.runs:
                _get_or_add(_rPr(run), "w:rtl")
    doc.save(str(out))
    return out


def docx_to_html(src: Path, out: Path, title: str = "Document") -> Path:
    import mammoth

    with src.open("rb") as fh:
        result = mammoth.convert_to_html(fh)
    body = result.value
    direction = "rtl" if RTL_CHARS.search(re.sub(r"<[^>]+>", "", body)) else "ltr"
    out.write_text(
        "<!DOCTYPE html>\n"
        f'<html lang="ar" dir="{direction}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html_lib.escape(title)}</title><style>"
        "body{max-width:48rem;margin:2rem auto;padding:0 1rem;"
        "font-family:system-ui,'Segoe UI',Tahoma,sans-serif;line-height:1.7;color:#1f2937}"
        "img{max-width:100%;height:auto}table{border-collapse:collapse;width:100%}"
        "td,th{border:1px solid #d1d5db;padding:.5rem}</style></head><body>"
        f"{body}</body></html>",
        encoding="utf-8",
    )
    return out


def html_to_docx(src: Path, outdir: Path) -> Path:
    return soffice_convert(src, outdir, "docx", filter_name="MS Word 2007 XML")


def markdown_to_docx(src: Path, workdir: Path, outdir: Path) -> Path:
    import markdown as md

    text = src.read_text(encoding="utf-8", errors="replace")
    body = md.markdown(text, extensions=["tables", "fenced_code", "sane_lists", "toc"])
    html_path = workdir / f"{src.stem}.html"
    html_path.write_text(
        f'<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>{body}</body></html>',
        encoding="utf-8",
    )
    return soffice_convert(html_path, outdir, "docx", filter_name="MS Word 2007 XML")


def text_to_docx(src: Path, out: Path) -> Path:
    doc = Document()
    for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
        paragraph = doc.add_paragraph(line)
        if RTL_CHARS.search(line):
            paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            _get_or_add(_pPr(paragraph), "w:bidi")
    doc.save(str(out))
    return out
