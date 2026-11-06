"""Turn uploaded reports or grant requirements into model content blocks.

These documents describe the reporting requirements and may be sent to the AI.
They are treated as untrusted data: the system prompt tells the model that
instructions inside them do not change its rules, and the privacy gate does
not depend on the model following them anyway.
"""

from __future__ import annotations

import base64
from pathlib import Path

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MAX_TEXT_CHARS = 400_000


def to_blocks(path: Path) -> list[dict]:
    path = Path(path)
    suffix = path.suffix.lower()
    header = {"type": "text", "text": f"--- Uploaded reporting document: {path.name} ---"}
    if suffix == ".pdf":
        data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
        return [header, {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data},
                         "title": path.name}]
    if suffix in IMAGE_TYPES:
        data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
        return [header, {"type": "image", "source": {"type": "base64", "media_type": IMAGE_TYPES[suffix], "data": data}}]
    return [header, {"type": "text", "text": extract_text(path)[:MAX_TEXT_CHARS]}]


def scan_text(path: Path) -> str | None:
    """Readable text of an uploaded document, for checking it before it is sent; None if unreadable (images)."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in IMAGE_TYPES:
        return None
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
        except Exception:
            return None
        return text if len(text.strip()) >= 200 else None  # a scanned PDF has no readable text
    return extract_text(path)


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _docx_text(path)
    if suffix in (".xlsx", ".xlsm"):
        return _xlsx_text(path)
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return ""


def _docx_text(path: Path) -> str:
    from docx import Document

    doc = Document(path)
    parts: list[str] = []
    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = "".join(n.text or "" for n in child.iter() if n.tag.endswith("}t"))
            if text.strip():
                parts.append(text)
        elif tag == "tbl":
            for tr in child.iter():
                if not tr.tag.endswith("}tr"):
                    continue
                cells = []
                for tc in tr:
                    if tc.tag.endswith("}tc"):
                        cells.append(" ".join("".join(t.text or "" for t in tc.iter() if t.tag.endswith("}t")).split()))
                parts.append(" | ".join(cells))
            parts.append("")
    return "\n".join(parts)


def _xlsx_text(path: Path) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        parts.append(f"[Sheet {ws.title}]")
        for row in ws.iter_rows(values_only=True):
            if any(v not in (None, "") for v in row):
                parts.append(" | ".join("" if v is None else str(v) for v in row))
    wb.close()
    return "\n".join(parts)


def page_orientations(path: Path) -> list[str]:
    """Orientation of each page (PDF), section (Word) or sheet (Excel), or [] when unknown.

    Read locally so the exported tables can match the reference report's layout.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader
            out = []
            for page in PdfReader(path).pages:
                w, h = float(page.mediabox.width), float(page.mediabox.height)
                if (page.get("/Rotate") or 0) % 180:
                    w, h = h, w
                out.append("landscape" if w > h else "portrait")
            return out
        if suffix == ".docx":
            from docx import Document
            return ["landscape" if s.page_width and s.page_height and s.page_width > s.page_height else "portrait"
                    for s in Document(path).sections]
        if suffix in (".xlsx", ".xlsm"):
            from openpyxl import load_workbook
            wb = load_workbook(path, read_only=False)
            return ["landscape" if ws.page_setup.orientation == "landscape" else "portrait" for ws in wb.worksheets]
    except Exception:  # an unreadable layout only means no layout hint
        return []
    return []


def layout_summary(paths: list[Path]) -> tuple[str, str | None]:
    """A plain description of the reference documents' page layout, and the single
    orientation they all use ("portrait" or "landscape"), or None when mixed or unknown."""
    lines, seen = [], set()
    for path in paths:
        pages = page_orientations(path)
        if not pages:
            continue
        seen.update(pages)
        unit = {".docx": "section", ".xlsx": "sheet", ".xlsm": "sheet"}.get(Path(path).suffix.lower(), "page")
        if len(set(pages)) == 1:
            lines.append(f"{Path(path).name}: {len(pages)} {unit}(s), all {pages[0]}")
        else:
            runs, start = [], 0
            for i in range(1, len(pages) + 1):
                if i == len(pages) or pages[i] != pages[start]:
                    span = f"{start + 1}" if i - 1 == start else f"{start + 1}-{i}"
                    runs.append(f"{unit}s {span} {pages[start]}")
                    start = i
            lines.append(f"{Path(path).name}: " + "; ".join(runs))
    single = next(iter(seen)) if len(seen) == 1 else None
    return "\n".join(lines), single
