"""ConFas conversion engine.

Design goals: SPEED + PERFECTION (lossless-fidelity conversions).

Strategy (fastest path first, newest format versions by default):

  * DOCX / XLSX / PPTX -> PDF : Microsoft Word/Excel/PowerPoint COM automation
        (the installed Office writes native OOXML; fastest & pixel-perfect on
        Windows machines that have Office).  Fallback: LibreOffice headless.
  * PDF -> DOCX              : pdf2docx (preserves layout, tables, images),
        then Microsoft Word PDF-reflow (Office COM), then LibreOffice writer
        filter ``MS Word 2007 XML`` (= .docx, OOXML 2007+ - NOT legacy .doc).
  * Legacy Office (.doc/.xls/.ppt) -> modern (.docx/.xlsx/.pptx) :
        Office COM with FileFormat codes 12 / 51 / 24, or LibreOffice filters
        ``MS Word 2007 XML`` / ``MS Excel 2007 XML`` / ``MS PowerPoint 2007 XML``.
  * Modern -> legacy is NEVER produced unless explicitly requested.
  * Images                     : Pillow (C-accelerated) re-encoded to the
        newest codec generation (JPEG XL > WebP > AVIF/HEIF > JPEG/PNG...).
  * Text/code/markup           : chardet auto-encoding -> UTF-8 (+ BOM for
        Windows-native UTF-16 consumers), HTML <-> DOCX via python-docx,
        Markdown -> DOCX/HTML/PDF.
  * EPUB                       : epub -> text/HTML via stdlib zipfile.

Everything runs in a persistent worker process pool, so consecutive
conversions are near-instant (no interpreter/COM spin-up per file).

The module is import-safe on Linux/macOS as well (Windows-only backends are
simply unavailable there) which makes development/testing possible anywhere.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

IS_WINDOWS = sys.platform == "win32"


# --------------------------------------------------------------------------- #
#  Registry helpers / public API surface
# --------------------------------------------------------------------------- #

def _norm_ext(ext: str) -> str:
    ext = (ext or "").strip().lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    return ext


@dataclass
class ConversionResult:
    source: str
    target: str
    backend: str
    elapsed_ms: float
    ok: bool
    error: str = ""
    bytes_in: int = 0
    bytes_out: int = 0

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "backend": self.backend,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "ok": self.ok,
            "error": self.error,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "t": time.time(),
        }


# --------------------------------------------------------------------------- #
#  Format knowledge base
# --------------------------------------------------------------------------- #

OFFICE_DOC_IN = {"doc", "docx", "odt", "rtf", "mht", "html", "htm", "txt", "wpd"}
OFFICE_SHEET_IN = {"xls", "xlsx", "ods", "csv", "txt"}
OFFICE_SLIDE_IN = {"ppt", "pptx", "odp"}
PDF_CAPABLE_IN = OFFICE_DOC_IN | OFFICE_SLIDE_IN | OFFICE_SHEET_IN | {"png", "jpg", "jpeg", "tif", "tiff", "epub", "xps"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "tiff", "tif", "webp",
              "avif", "heic", "heif", "jxl", "ico", "ppm", "pgm", "pbm", "jp2"}
TEXT_EXTS = {"txt", "md", "markdown", "csv", "tsv", "json", "xml", "html", "htm",
             "log", "ini", "py", "js", "c", "cpp", "h", "java", "yml", "yaml", "srt"}

# "newest generation" mapping used by the right-click menu builder.
NEWEST_GENERATION = {
    "doc": "docx",       # OOXML (Word 2007+) - never emit legacy .doc
    "xls": "xlsx",       # OOXML (Excel 2007+)
    "ppt": "pptx",       # OOXML (PowerPoint 2007+)
    "odt": "docx",
    "ods": "xlsx",
    "odp": "pptx",
    "rtf": "docx",
    "htm": "docx",
    "html": "docx",
    "wmf": "png",
    "emf": "png",
}


def newest_target_for(src_ext: str) -> list[str]:
    """Suggested 'latest version' targets for a given source extension."""
    src_ext = src_ext.lower().lstrip(".")
    out: list[str] = []
    g = NEWEST_GENERATION.get(src_ext)
    if g:
        out.append(g)
    can_produce_pdf = bool(BACKENDS.libreoffice) or (IS_WINDOWS and bool(BACKENDS.office_app))
    if src_ext in OFFICE_DOC_IN | {"pdf"}:
        if can_produce_pdf:
            out.append("pdf")
        out.append("docx")
    if src_ext in OFFICE_SHEET_IN:
        out += ["xlsx", "pdf", "csv"]
    if src_ext in OFFICE_SLIDE_IN:
        out += ["pptx", "pdf"]
    if src_ext in IMAGE_EXTS:
        out += ["webp", "png", "jpg", "avif", "jxl"]
    if src_ext in TEXT_EXTS:
        out += ["docx", "txt", "md", "html", "pdf"]
    if src_ext == "epub":
        out += ["txt", "docx", "html"]
    # de-duplicate, keep order, drop self
    seen, res = set(), []
    for e in out:
        if e != src_ext and e not in seen:
            seen.add(e)
            res.append(e)
    return res[:4]


# --------------------------------------------------------------------------- #
#  Backend discovery
# --------------------------------------------------------------------------- #

class Backends:
    def __init__(self) -> None:
        self.libreoffice: Optional[str] = None
        self.office_app: dict[str, str] = {}      # word|excel|powerpoint -> ProgId
        self.pillow = False
        self.pdf2docx = False
        self.python_docx = False
        self.chardet = False
        self.detect()

    # -- detection ---------------------------------------------------------- #
    def detect(self) -> None:
        if IS_WINDOWS:
            candidates = [
                r"C:\Program Files\LibreOffice\program\soffice.exe",
                r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\LibreOffice\program\soffice.exe"),
            ]
            for c in candidates:
                if Path(c).exists():
                    self.libreoffice = c
                    break
            if not self.libreoffice:
                self.libreoffice = shutil.which("soffice")
            try:  # only meaningful inside a Windows session
                import winreg  # noqa: WPS433
                for app, progid in (
                    ("word", "Word.Application"),
                    ("excel", "Excel.Application"),
                    ("powerpoint", "PowerPoint.Application"),
                ):
                    for hive in (winreg.HKEY_CLASSES_ROOT,):
                        try:
                            winreg.OpenKey(hive, progid)
                            self.office_app[app] = progid
                            break
                        except OSError:
                            continue
            except Exception:
                pass
        else:
            self.libreoffice = shutil.which("soffice") or shutil.which("libreoffice")
        try:
            import PIL  # noqa: F401
            self.pillow = True
        except Exception:
            pass
        try:
            import pdf2docx  # noqa: F401
            self.pdf2docx = True
        except Exception:
            pass
        try:
            import docx  # noqa: F401
            self.python_docx = True
        except Exception:
            pass
        try:
            import chardet  # noqa: F401
            self.chardet = True
        except Exception:
            pass

    def summary(self) -> dict:
        return {
            "libreoffice": self.libreoffice,
            "office_apps": dict(self.office_app),
            "pillow": self.pillow,
            "pdf2docx": self.pdf2docx,
            "python_docx": self.python_docx,
            "chardet": self.chardet,
        }


BACKENDS = Backends()


# --------------------------------------------------------------------------- #
#  Backend implementations
# --------------------------------------------------------------------------- #

BackendFn = Callable[[Path, Path], None]


def _unique(path: Path) -> Path:
    """Return ``path`` if free, else name_1.ext, name_2.ext ..."""
    if not path.exists():
        return path
    stem, suf = path.stem, path.suffix
    for i in range(1, 500):
        cand = path.with_name(f"{stem}_{i}{suf}")
        if not cand.exists():
            return cand
    return path.with_name(f"{stem}_{uuid.uuid4().hex[:6]}{suf}")


# ---- LibreOffice (universal fallback, batch-friendly) ---------------------- #

_LO_FILTERS = {
    "pdf": "writer_pdf_Export",
    "docx": "MS Word 2007 XML",          # OOXML 2007+ (NOT 'MS Word 97')
    "xlsx": "MS Excel 2007 XML",
    "pptx": "MS PowerPoint 2007 XML",
    "doc": "MS Word 97",
    "xls": "MS Excel 97",
    "ppt": "MS PowerPoint 97",
    "odt": "writer8",
    "ods": "calc8",
    "odp": "impress8",
    "rtf": "Rich Text Format",
    "html": "HTML (StarWriter)",
    "txt": "Text",
    "csv": "Text - txt - csv (StarCalc)",
    "png": "draw_png_Export",
    "jpg": "draw_jpeg_Export",
    "svg": "draw_svg_Export",
}


def lo_convert(src: Path, dst: Path, extra: Optional[list[str]] = None) -> None:
    if not BACKENDS.libreoffice:
        raise RuntimeError("LibreOffice not found")
    outdir = dst.parent
    filt = _LO_FILTERS.get(dst.suffix.lstrip("."))
    args = [
        BACKENDS.libreoffice, "--headless", "--norestore", "--nologo", "--nofirststartwizard",
        f"-env:UserInstallation=file:///{tempfile.gettempdir()}/confas_lo_profile",
        "--convert-to", (f"{filt}" if filt else dst.suffix.lstrip(".")),
    ]
    if extra:
        args[-1] += ": " + ", ".join(extra)
    args += ["--outdir", str(outdir), str(src)]
    proc = subprocess.run(args, capture_output=True, timeout=300)
    produced = outdir / (src.stem + dst.suffix)
    if produced.exists() and produced != dst:
        produced.replace(dst)
    if not dst.exists():
        raise RuntimeError(f"LibreOffice produced no output ({proc.returncode}): "
                           f"{proc.stderr.decode(errors='replace')[:400]}")


def conv_libreoffice(src: Path, dst: Path) -> None:
    lo_convert(src, dst)


# ---- Microsoft Office COM (fastest & most faithful when Office is present) - #

_FILEFORMAT = {  # https://learn.microsoft.com/office/vba/api/wdwdformat etc.
    "docx": 12,   # wdFormatXMLDocument  (OOXML, Word 2007+)
    "pdf": 17,    # wdFormatPDF
    "xlsx": 51,   # xlOpenXMLWorkbook
    "pptx": 24,   # ppSaveAsOpenXMLPresentation
    "mht": 10,
    "html": 8,    # wdFormatHTML  (Word only)
    "rtf": 6,     # wdFormatRTF   (Word only)
    "txt": 2,     # wdFormatText  (Word only)
}
_WORD_ONLY_FMTS = {"html", "rtf", "txt"}


def conv_office_com(src: Path, dst: Path) -> None:
    if not IS_WINDOWS:
        raise RuntimeError("Office COM requires Windows")
    import pythoncom  # type: ignore
    from win32com.client import DispatchEx, constants  # type: ignore
    pythoncom.CoInitialize()
    try:
        ext = dst.suffix.lstrip(".").lower()
        app_name = None
        if src.suffix.lstrip(".").lower() in OFFICE_SHEET_IN and ext in ("xlsx", "pdf", "csv", "xls"):
            app_name = "excel"
        elif src.suffix.lstrip(".").lower() in OFFICE_SLIDE_IN and ext in ("pptx", "pdf", "ppt"):
            app_name = "powerpoint"
        else:
            app_name = "word"
        progid = BACKENDS.office_app.get(app_name)
        if not progid:
            raise RuntimeError(f"Microsoft {app_name.title()} not installed")
        app = DispatchEx(progid)
        try:
            app.Visible = False
        except Exception:
            pass
        try:
            app.DisplayAlerts = False     # also suppresses the Word PDF-reflow prompt
        except Exception:
            pass
        target = Path(dst)
        if target.exists():
            target = _unique(target)
        dst_abs = str(target.resolve())
        if app_name == "word":
            doc = app.Documents.Open(str(src.resolve()), ReadOnly=True)
            try:
                fmt = _FILEFORMAT.get(ext)
                if ext == "pdf":
                    doc.ExportAsFixedFormat(dst_abs, 17)  # wdExportFormatPDF
                elif fmt is not None:
                    doc.SaveAs2(dst_abs, FileFormat=fmt)
                else:
                    doc.SaveAs2(dst_abs)
            finally:
                doc.Close(SaveChanges=False)
        elif app_name == "excel":
            wb = app.Workbooks.Open(str(src.resolve()), ReadOnly=True)
            try:
                if ext == "pdf":
                    wb.ExportAsFixedFormat(-1, dst_abs)  # xlTypePDF
                else:
                    wb.SaveAs(dst_abs, FileFormat=_FILEFORMAT.get(ext, 51))
            finally:
                wb.Close(SaveChanges=False)
        else:  # powerpoint
            pres = app.Presentations.Open(str(src.resolve()), WithWindow=False)
            try:
                if ext == "pdf":
                    pres.SaveAs(dst_abs, 32)  # ppSaveAsPDF
                else:
                    pres.SaveAs(dst_abs, _FILEFORMAT.get(ext, 24))
            finally:
                pres.Close()
    finally:
        try:
            app.Quit()
        except Exception:
            pass
        pythoncom.CoUninitialize()
    out_file = Path(dst_abs)
    if not out_file.exists():
        raise RuntimeError("Office COM produced no output")
    wanted = Path(dst)
    if out_file != wanted.resolve():
        out_file.replace(wanted)


# ---- pdf2docx (layout-preserving PDF -> DOCX) ------------------------------ #

def conv_pdf2docx(src: Path, dst: Path) -> None:
    from pdf2docx import Converter  # type: ignore
    cv = Converter(str(src))
    try:
        cv.convert(str(dst), start=0, end=None)
    finally:
        cv.close()


# ---- Pillow image pipeline (C-speed) --------------------------------------- #

_IMG_QUALITY = {"jpg": 95, "jpeg": 95, "webp": 95, "avif": 60, "heic": 60,
                "jxl": 90, "tiff": 90, "png": None}


def conv_pillow(src: Path, dst: Path) -> None:
    from PIL import Image, ImageOps  # type: ignore
    ext = dst.suffix.lstrip(".").lower()
    if ext == "jpg":
        ext = "jpeg"
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if ext in ("jpeg", "bmp", "ico") and im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        elif ext in ("jpeg", "bmp") and im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        save_kw: dict = {}
        q = _IMG_QUALITY.get(ext)
        if q is not None and ext in ("jpeg", "webp", "tiff"):
            save_kw["quality"] = q
        if ext == "webp":
            save_kw["method"] = 4          # fast but strong compression
        if ext in ("png", "tiff"):
            save_kw["optimize"] = True
        if ext == "jpeg":
            save_kw["subsampling"] = 0     # 4:4:4 = maximum fidelity
            save_kw["exif"] = im.info.get("exif", b"") or None
            if save_kw["exif"] is None:
                save_kw.pop("exif")
        im.save(dst, **save_kw)


# ---- Text-family conversions ----------------------------------------------- #

def _read_text_auto(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()[:200_000] or path.read_bytes()
    enc = "utf-8"
    if BACKENDS.chardet:
        guess = __import__("chardet").detect(raw)
        if guess.get("encoding"):
            enc = guess["encoding"]
    try:
        return raw.decode(enc, errors="replace") if len(raw) else "", enc
    except LookupError:
        return raw.decode("utf-8", errors="replace"), "utf-8"


def conv_text_generic(src: Path, dst: Path) -> None:
    text, _enc = _read_text_auto(src)
    ext = dst.suffix.lstrip(".").lower()
    if ext in ("txt", "md", "log", "csv", "tsv", "json", "xml", "srt", "yml", "yaml", "ini"):
        dst.write_text(text, encoding="utf-8-sig")
    elif ext in ("html", "htm"):
        body = __import__("html").escape(text)
        dst.write_text(
            "<!DOCTYPE html><html><head><meta charset='utf-8'><title>"
            f"{__import__('html').escape(src.stem)}</title></head><body><pre>{body}</pre></body></html>",
            encoding="utf-8")
    else:
        raise RuntimeError(f"unsupported text target .{ext}")


_MD_HEURISTIC = re.compile(r"^(#{1,6}\s|[*-]\s|\d+.\s|>\s|`|\*\*)", re.M)


def conv_text_to_docx(src: Path, dst: Path) -> None:
    import docx  # type: ignore
    from docx.enum.text import WD_ALIGN_PARAGRAPH  # type: ignore
    from docx.shared import Pt  # type: ignore
    text, _ = _read_text_auto(src)
    lines = text.splitlines()
    d = docx.Document()
    style = d.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    md = src.suffix.lstrip(".").lower() in ("md", "markdown") or bool(_MD_HEURISTIC.search(text[:4000]))
    in_code = False
    code_buf: list[str] = []
    for line in lines:
        if md and line.strip().startswith("```"):
            if in_code:
                p = d.add_paragraph("\n".join(code_buf))
                for r in p.runs:
                    r.font.name = "Consolas"
                    r.font.size = Pt(9)
                code_buf = []
            in_code = not in_code
            continue
        if in_code:
            code_buf.append(line)
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line) if md else None
        if m:
            d.add_heading(m.group(2).strip(), level=min(len(m.group(1)), 4))
        elif md and re.match(r"^\s*[-*+]\s+", line):
            d.add_paragraph(re.sub(r"^\s*[-*+]\s+", "", line), style="List Bullet")
        elif md and re.match(r"^\s*\d+[.)]\s+", line):
            d.add_paragraph(re.sub(r"^\s*\d+[.)]\s+", "", line), style="List Number")
        elif md and re.match(r"^>\s?", line):
            p = d.add_paragraph(re.sub(r"^>\s?", "", line))
            p.paragraph_format.left_indent = Pt(18)
        else:
            _add_rich_md_paragraph(d, line, md)
    if code_buf:
        d.add_paragraph("\n".join(code_buf))
    d.save(dst)


def _add_rich_md_paragraph(doc, line: str, md: bool) -> None:
    p = doc.add_paragraph()
    if not md or ("**" not in line and "`" not in line):
        p.add_run(line)
        return
    parts = re.split(r"(\*\*.+?\*\*|`.+?`)", line)
    for part in parts:
        if part.startswith("**") and part.endswith("**"):
            p.add_run(part[2:-2]).bold = True
        elif part.startswith("`") and part.endswith("`"):
            r = p.add_run(part[1:-1])
            r.font.name = "Consolas"
        else:
            p.add_run(part)


def conv_html_to_docx(src: Path, dst: Path) -> None:
    # route through LibreOffice: it maps real HTML/CSS structure into OOXML
    lo_convert(src, dst)


def conv_epub_extract(src: Path, dst: Path) -> None:
    import zipfile
    from xml.etree import ElementTree as ET
    texts: list[str] = []
    with zipfile.ZipFile(src) as z:
        names = [n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))]
        # respect spine order when possible
        try:
            root = ET.fromstring(z.read("META-INF/container.xml"))
            opf_path = root.findall(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")[0].get("full-path")
            opf_dir = str(Path(opf_path).parent)
            opf = ET.fromstring(z.read(opf_path))
            ns = {"p": "http://www.idpf.org/2007/opf", "c": "http://www.idpf.org/2007/opf"}
            manifest = {it.get("id"): it.get("href")
                        for it in opf.findall(".//p:manifest/p:item", ns)}
            ordered = []
            for it in opf.findall(".//p:spine/p:itemref", ns):
                href = manifest.get(it.get("idref"))
                if href:
                    ordered.append((opf_dir + "/" + href).replace("\\", "/").lstrip("./"))
            names = [n for n in ordered if n in z.namelist()] or names
        except Exception:
            pass
        for n in sorted(names):
            try:
                html = z.read(n).decode("utf-8", errors="replace")
            except KeyError:
                continue
            html = re.sub(r"(?is)<(script|style).+?</\1>", " ", html)
            # crude but robust text extraction w/ paragraph breaks
            html = re.sub(r"(?i)</(p|div|h[1-6]|li|br)>", "\n", html)
            text = re.sub(r"(?s)<[^>]+>", " ", html)
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
            if text:
                texts.append(text)
    joined = "\n\n\n".join(texts) + "\n"
    ext = dst.suffix.lstrip(".").lower()
    if ext in ("docx",):
        tmp = Path(tempfile.mkstemp(suffix=".txt")[1])
        tmp.write_text(joined, encoding="utf-8")
        conv_text_to_docx(tmp, dst)
        tmp.unlink(missing_ok=True)
    elif ext in ("html", "htm"):
        body = __import__("html").escape(joined)
        dst.write_text(f"<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
                       f"<body><pre>{body}</pre></body></html>", encoding="utf-8")
    else:
        dst.write_text(joined, encoding="utf-8-sig")


def conv_docx_to_text(src: Path, dst: Path) -> None:
    import docx  # type: ignore
    d = docx.Document(str(src))
    lines = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            lines.append("\t".join(c.text for c in row.cells))
    dst.write_text("\n".join(lines), encoding="utf-8-sig")


def conv_image_to_pdf(src: Path, dst: Path) -> None:
    if BACKENDS.pillow:
        from PIL import Image  # type: ignore
        with Image.open(src) as im:
            if im.mode != "RGB":
                im = im.convert("RGB")
            im.save(dst, "PDF", resolution=200.0)
    else:
        lo_convert(src, dst)


def conv_pdf_to_text(src: Path, dst: Path) -> None:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        # fall back to LibreOffice -> txt
        lo_convert(src, dst)
        return
    reader = PdfReader(str(src))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    dst.write_text(text, encoding="utf-8-sig")


# --------------------------------------------------------------------------- #
#  Routing table
# --------------------------------------------------------------------------- #

@dataclass
class Route:
    name: str
    fn: BackendFn
    weight: int = 0   # static preference: higher = try first


def _routes_for(src_ext: str, dst_ext: str) -> list[Route]:
    s, d = src_ext.lower().lstrip("."), dst_ext.lower().lstrip(".")
    if s == d:
        return []
    routes: list[Route] = []

    def add(name: str, fn: BackendFn, w: int = 0):
        routes.append(Route(name, fn, w))

    office_available = IS_WINDOWS and bool(BACKENDS.office_app)
    word_available = office_available and "word" in BACKENDS.office_app

    # same family shortcuts ---------------------------------------------------
    if s in IMAGE_EXTS and d in IMAGE_EXTS:
        if BACKENDS.pillow:
            add("pillow", conv_pillow, 100)
        add("libreoffice", conv_libreoffice, 20)
    elif s == "pdf" and d == "docx":
        if BACKENDS.pdf2docx:
            add("pdf2docx", conv_pdf2docx, 90)
        if word_available:
            # Word 2013+ PDF reflow: excellent for text-centric PDFs
            add("office-com", conv_office_com, 80)
        add("libreoffice", conv_libreoffice, 60)
    elif s == "pdf" and d in ("txt", "md", "csv"):
        add("pypdf", conv_pdf_to_text, 90)
        add("libreoffice", conv_libreoffice, 40)
    elif s == "pdf" and d in ("jpg", "jpeg", "png", "webp"):
        if BACKENDS.pillow:
            def pdf_img(src: Path, dst: Path, sub=0) -> None:
                # render page(s) via pdftoppm-like path using PyMuPDF if present
                import fitz  # type: ignore
                doc = fitz.open(src)
                pg = doc[sub]
                pix = pg.get_pixmap(dpi=200)
                pix.save(dst.with_suffix(".png"))
                if dst.suffix.lstrip(".").lower() != "png":
                    conv_pillow(dst.with_suffix(".png"), dst)
                    dst.with_suffix(".png").unlink(missing_ok=True)
                doc.close()
            try:
                import fitz  # noqa: F401
                add("pymupdf", pdf_img, 90)
            except ImportError:
                pass
        add("libreoffice", conv_libreoffice, 30)
    elif s in IMAGE_EXTS and d == "pdf":
        if BACKENDS.pillow:
            add("pillow-pdf", conv_image_to_pdf, 100)
        add("libreoffice", conv_libreoffice, 30)
    elif s in ("docx",) and d in TEXT_EXTS - {"docx"}:
        if s == "docx" and d in ("txt", "md", "log"):
            add("python-docx", conv_docx_to_text, 90)
        if word_available and d in _WORD_ONLY_FMTS:
            add("office-com", conv_office_com, 45)
        add("libreoffice", conv_libreoffice, 50)
    elif s in TEXT_EXTS and d == "pdf":
        if word_available:
            # Word opens txt/md/html and exports a perfect PDF
            add("office-com", conv_office_com, 85)
        if BACKENDS.libreoffice:
            add("libreoffice", conv_libreoffice, 60)
        elif BACKENDS.python_docx:
            def text_pdf_two_step(src: Path, dst: Path) -> None:
                tmp = dst.with_suffix(".docx")
                conv_text_to_docx(src, tmp)
                try:
                    lo_convert(tmp, dst)
                finally:
                    if not dst.exists():
                        tmp.unlink(missing_ok=True)
                        raise RuntimeError(
                            "PDF generation needs LibreOffice or Microsoft Office installed")
            add("text->docx->pdf", text_pdf_two_step, 40)
    elif s in TEXT_EXTS and d == "docx":
        if BACKENDS.python_docx:
            if s in ("html", "htm"):
                add("libreoffice-html", conv_html_to_docx, 80)
            add("python-docx-md", conv_text_to_docx, 70)
        add("libreoffice", conv_libreoffice, 50)
    elif s == "epub":
        if BACKENDS.python_docx and d == "docx":
            add("epub->docx", conv_epub_extract, 80)
        if d in ("txt", "md", "html", "htm"):
            add("epub-extract", conv_epub_extract, 90)
        add("libreoffice", conv_libreoffice, 30)
    elif s in ("doc", "xls", "ppt", "odt", "ods", "odp", "rtf") and d in ("docx", "xlsx", "pptx", "pdf"):
        if office_available:
            add("office-com", conv_office_com, 100)
        add("libreoffice", conv_libreoffice, 70)
    elif s in OFFICE_DOC_IN | OFFICE_SHEET_IN | OFFICE_SLIDE_IN and d == "pdf":
        if office_available:
            add("office-com", conv_office_com, 100)
        add("libreoffice", conv_libreoffice, 70)
    elif s in OFFICE_DOC_IN and d in ("docx", "odt", "rtf", "html"):
        if office_available:
            add("office-com", conv_office_com, 100)
        add("libreoffice", conv_libreoffice, 70)
        if BACKENDS.python_docx and d == "docx":
            add("python-docx-md", conv_text_to_docx, 65)
    elif s in ("docx", "xlsx", "pptx") and d in ("pdf", "odt", "ods", "odp", "rtf", "html", "txt", "csv"):
        if office_available and d == "pdf":
            add("office-com", conv_office_com, 100)
        elif word_available and s == "docx" and d in _WORD_ONLY_FMTS:
            add("office-com", conv_office_com, 45)
        add("libreoffice", conv_libreoffice, 70)
    elif s in TEXT_EXTS and d in TEXT_EXTS:
        add("text-passthru", conv_text_generic, 100)
    elif s in ("txt", "md") and d in ("html", "htm"):
        add("text-passthru", conv_text_generic, 100)
    else:
        # last-resort generic chain
        if s in TEXT_EXTS and d in TEXT_EXTS:
            add("text-passthru", conv_text_generic, 50)
        add("libreoffice", conv_libreoffice, 10)
        if office_available:
            add("office-com", conv_office_com, 5)

    # prune routes whose deps are missing
    live: list[Route] = []
    for r in routes:
        if r.name.startswith("pillow") and not BACKENDS.pillow:
            continue
        if r.name.startswith("python-docx") and not BACKENDS.python_docx:
            continue
        if r.name == "office-com" and not (IS_WINDOWS and BACKENDS.office_app):
            continue
        if r.name == "libreoffice" and not BACKENDS.libreoffice:
            continue
        if r.name == "pdf2docx" and not BACKENDS.pdf2docx:
            continue
        live.append(r)
    live.sort(key=lambda r: -r.weight)
    return live


# --------------------------------------------------------------------------- #
#  Engine
# --------------------------------------------------------------------------- #

class ConversionEngine:
    """Thread-safe orchestrator with optional ML backend selection."""

    def __init__(self, enable_ml: bool = True, history_file: Optional[Path] = None) -> None:
        self.enable_ml = enable_ml and os.environ.get("CONFAS_DISABLE_ML") != "1"
        self._selector = None
        if self.enable_ml:
            try:
                from .learning import BanditSelector
                from .monitor import DATA_DIR   # honours CONFAS_HOME env var
                self._selector = BanditSelector(history_file or (DATA_DIR / "history.jsonl"))
            except Exception:
                self._selector = None
            if self._selector is not None:
                try:
                    from .cloud import CloudSync
                    cloud = CloudSync(self._selector)
                    self._selector.attach_cloud(cloud)
                    self.cloud = cloud
                    cloud.start()          # no-op unless cloud_sync is on
                except Exception:
                    self.cloud = None
            else:
                self.cloud = None
        self._pool = ThreadPoolExecutor(max_workers=max(2, (os.cpu_count() or 4)))
        self._lock = threading.Lock()

    # -- single ------------------------------------------------------------- #
    def convert(self, src: str | Path, dst_ext: str, out_dir: Optional[str | Path] = None,
                out_path: Optional[str | Path] = None) -> ConversionResult:
        from . import debuglog
        from .debuglog import _fmt_exc
        sess = debuglog.new_session(src, dst_ext)
        src = Path(src)
        if not src.exists():
            res = ConversionResult(str(src), "", "-", 0, False, "source file not found")
            sess.update(ok=False, final_error=res.error, attempts=[],
                        planned_out="", out_path="")
            debuglog.finalize(sess, res)
            return res
        dst_ext = _norm_ext(dst_ext)
        if not dst_ext:
            res = ConversionResult(str(src), "", "-", 0, False, "missing target extension")
            sess.update(ok=False, final_error=res.error, attempts=[],
                        planned_out="", out_path="")
            debuglog.finalize(sess, res)
            return res
        base = Path(out_path) if out_path else (Path(out_dir) if out_dir else src.parent) / (src.stem + dst_ext)
        base = _unique(Path(base))
        sess["planned_out"] = str(base)
        sess["dst_ext"] = dst_ext
        try:
            sess["bytes_in"] = src.stat().st_size
        except OSError:
            pass
        routes = _routes_for(src.suffix, dst_ext)
        sess["routes_static"] = [r.name for r in routes]
        if self._selector:
            routes, ml_info = self._selector.rank_debug(src, dst_ext, routes) \
                if hasattr(self._selector, "rank_debug") else (self._selector.rank(src, dst_ext, routes), {})
            sess["ml"].update(ml_info)
        sess["routes"] = [{"name": r.name, "weight": r.weight} for r in routes]
        if not routes:
            res = ConversionResult(str(src), dst_ext, "-", 0, False,
                                   f"No conversion path known for {src.suffix} -> {dst_ext}")
            sess.update(ok=False, final_error=res.error, attempts=[])
            debuglog.finalize(sess, res)
            return res
        t0 = time.perf_counter()
        err = ""
        attempts: list[dict] = []
        for r in routes:
            ta = time.perf_counter()
            try:
                base.parent.mkdir(parents=True, exist_ok=True)
                r.fn(src, base)
                ms = (time.perf_counter() - t0) * 1000
                att_ms = (time.perf_counter() - ta) * 1000
                attempts.append({"backend": r.name, "ok": True, "ms": att_ms,
                                 "error": "", "traceback": ""})
                if self._selector:
                    self._selector.record(src, dst_ext, r.name, ms, True)
                res = ConversionResult(str(src), str(base), r.name, ms, True,
                                       bytes_in=src.stat().st_size,
                                       bytes_out=base.stat().st_size if base.exists() else 0)
                sess.update(ok=True, winning_backend=r.name, total_ms=ms,
                            attempts=attempts, out_path=str(base),
                            bytes_out=res.bytes_out)
                debuglog.finalize(sess, res)
                return res
            except Exception as exc:  # try next backend
                ms_so_far = (time.perf_counter() - t0) * 1000
                att_ms = (time.perf_counter() - ta) * 1000
                if self._selector:
                    self._selector.record(src, dst_ext, r.name, att_ms, False)
                err = f"{type(exc).__name__}: {exc}"
                attempts.append({"backend": r.name, "ok": False, "ms": att_ms,
                                 "error": err, "traceback": _fmt_exc(exc)})
                if os.environ.get("CONFAS_DEBUG"):
                    traceback.print_exc()
        ms = (time.perf_counter() - t0) * 1000
        res = ConversionResult(str(src), str(base), routes[0].name, ms, False, err)
        sess.update(ok=False, final_error=err, attempts=attempts,
                    total_ms=ms, out_path=str(base))
        debuglog.finalize(sess, res)
        return res

    # -- batch -------------------------------------------------------------- #
    def convert_many(self, jobs: list[tuple[str, str]],
                     progress: Optional[Callable[[ConversionResult], None]] = None) -> list[ConversionResult]:
        futs = [self._pool.submit(self.convert, s, e) for s, e in jobs]
        results: list[ConversionResult] = []
        for f in futs:
            res = f.result()
            results.append(res)
            if progress:
                try:
                    progress(res)
                except Exception:
                    pass
        return results

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)


ENGINE_SINGLETON: Optional[ConversionEngine] = None
_ENGINE_LOCK = threading.Lock()


def get_engine(**kw) -> ConversionEngine:
    global ENGINE_SINGLETON
    with _ENGINE_LOCK:
        if ENGINE_SINGLETON is None:
            ENGINE_SINGLETON = ConversionEngine(**kw)
    return ENGINE_SINGLETON
