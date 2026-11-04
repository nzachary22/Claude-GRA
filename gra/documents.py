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
