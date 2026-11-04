"""Word and Excel exports of the draft tables, with their notes and gaps."""

from __future__ import annotations

import io
import re

from .engine import Note
from .tables import DEFAULT_DECIMALS, GAP_MARK, Cell, Table


def display(cell: Cell) -> str:
    return cell.display


def notes_for(table: Table, notes: list[Note]) -> list[Note]:
    """Notes about this table alone. Notes that span tables are shown once, up front."""
    return [n for n in notes if n.tables == [table.id]]


def shared_notes(tables: list[Table], notes: list[Note]) -> list[Note]:
    ids = {t.id for t in tables}
    return [n for n in notes if not (len(n.tables) == 1 and n.tables[0] in ids)]


def shared_note_line(n: Note, tables: list[Table]) -> str:
    """Name the affected tables unless the note applies to all of them."""
    named = [t.title for t in tables if t.id in n.tables]
    if not n.tables or len(named) == len(tables) or not named:
        return _note_line(n)
    return f"{_note_line(n)} [Affects: {'; '.join(named)}]"


def heading_lines(report: dict, fallback: str) -> tuple[str, list[str]]:
    """The document title and any lines under it, in the report's own words."""
    org, title = report.get("organization"), report.get("title")
    head = org or title or fallback
    below = [x for x in ((title if org else None), report.get("subtitle")) if x]
    return head, below


def excel_format(cell: Cell) -> str | None:
    """The Excel number format that matches the cell's display."""
    d = DEFAULT_DECIMALS.get(cell.format or "", 0) if cell.decimals is None else cell.decimals
    frac = "." + "0" * d if d else ""
    if cell.format == "percent":
        return f"0{frac}%"
    if cell.format == "currency":
        return f'"$"#,##0{frac}'
    if cell.format == "number":
        return f"#,##0{frac}"
    if isinstance(cell.value, int):
        return "#,##0"
    if isinstance(cell.value, float):
        return "#,##0.00"
    return None


def _note_line(n: Note) -> str:
    label = {"assumption": "Assumption", "gap": "Data gap", "warning": "Warning"}.get(n.kind, n.kind.title())
    text = f"{label}: {n.text}"
    if n.how_to_change:
        text += f" (To change: {n.how_to_change})"
    return text


def _gap_lines(table: Table) -> list[str]:
    seen, out = set(), []
    for r in table.rows:
        for h, c in zip(table.headers, r.cells):
            if c.gap and c.gap not in seen and not c.gap.startswith("Includes unavailable"):
                seen.add(c.gap)
                out.append(f"{GAP_MARK} = {c.gap} (first at {r.label} / {h})")
    return out


# ---------------------------------------------------------------- Word

def to_docx(tables: list[Table], notes: list[Note], title: str, period: str, report: dict | None = None) -> bytes:
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10)
    head, below = heading_lines(report or {}, title)
    doc.add_heading(head, level=0)
    for line in below:
        doc.add_paragraph(line)
    doc.add_paragraph(f"Reporting period: {period}. Draft prepared from current local data; review before submission.")
    shared = shared_notes(tables, notes)
    if shared:
        doc.add_heading("Assumptions and notes for more than one table", level=2)
        for n in shared:
            doc.add_paragraph(shared_note_line(n, tables), style="List Bullet").runs[0].font.size = Pt(9)

    def shade(cell, hex_color):
        tc_pr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), hex_color)
        tc_pr.append(shd)

    for t in tables:
        doc.add_heading(t.title, level=1)
        if t.subtitle:
            doc.add_paragraph(t.subtitle).runs[0].italic = True
        grid = doc.add_table(rows=1, cols=len(t.headers) + 1)
        grid.style = "Table Grid"
        grid.alignment = WD_TABLE_ALIGNMENT.CENTER
        hdr = grid.rows[0].cells
        for i, text in enumerate([t.row_header] + t.headers):
            hdr[i].text = text
            for p in hdr[i].paragraphs:
                for run in p.runs:
                    run.bold = True
                if i:
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            shade(hdr[i], "D9E2F3")
        for r in t.rows:
            cells = grid.add_row().cells
            cells[0].text = ("    " * r.indent) + r.label
            if r.kind == "heading":
                merged = cells[0].merge(cells[-1])
                merged.text = r.label
                for run in merged.paragraphs[0].runs:
                    run.bold = True
                shade(merged, "F2F2F2")
                continue
            for i, c in enumerate(r.cells, start=1):
                cells[i].text = display(c)
                p = cells[i].paragraphs[0]
                p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                if c.gap:
                    for run in p.runs:
                        run.font.color.rgb = RGBColor(0xC0, 0x50, 0x00)
            if r.kind in ("subtotal", "total"):
                for cell in cells:
                    for run in cell.paragraphs[0].runs:
                        run.bold = True
                if r.kind == "total":
                    for cell in cells:
                        shade(cell, "EDEDED")
        if t.note:
            p = doc.add_paragraph(t.note)
            p.runs[0].italic = True
        for line in _gap_lines(t):
            doc.add_paragraph(line).runs[0].font.size = Pt(9)
        for n in notes_for(t, notes):
            doc.add_paragraph(_note_line(n), style="List Bullet").runs[0].font.size = Pt(9)

    defs = (report or {}).get("definitions") or []
    if defs:
        doc.add_heading("Reporting definitions", level=1)
        for d in defs:
            if d.get("heading"):
                doc.add_heading(d["heading"], level=3)
            for para in d["text"].split("\n"):
                if para.strip():
                    doc.add_paragraph(para.strip())

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- Excel

def to_xlsx(tables: list[Table], notes: list[Note], title: str, period: str, report: dict | None = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_fill = PatternFill("solid", fgColor="D9E2F3")
    heading_fill = PatternFill("solid", fgColor="F2F2F2")
    total_fill = PatternFill("solid", fgColor="EDEDED")
    gap_font = Font(color="C05000", italic=True)
    used: set[str] = set()
    head, below = heading_lines(report or {}, title)
    shared = shared_notes(tables, notes)

    for t in tables:
        ws = wb.create_sheet(_sheet_name(t.title or t.id, used))
        ws["A1"] = t.title
        ws["A1"].font = Font(bold=True, size=13)
        ws["A2"] = t.subtitle or f"{head} · {period}"
        ws["A2"].font = Font(italic=True, color="595959")
        hr = 4
        for i, h in enumerate([t.row_header] + t.headers, start=1):
            c = ws.cell(row=hr, column=i, value=h)
            c.font = Font(bold=True)
            c.fill = header_fill
            c.border = border
            c.alignment = Alignment(horizontal="center" if i > 1 else "left", vertical="center", wrap_text=True)
        row = hr
        for r in t.rows:
            row += 1
            label = ws.cell(row=row, column=1, value=r.label)
            label.alignment = Alignment(indent=r.indent * 2)
            label.border = border
            if r.kind == "heading":
                ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(t.headers) + 1)
                label.font = Font(bold=True)
                label.fill = heading_fill
                continue
            for i, cell in enumerate(r.cells, start=2):
                if cell.gap:
                    x = ws.cell(row=row, column=i, value=GAP_MARK)
                    x.font = gap_font
                    x.alignment = Alignment(horizontal="right")
                else:
                    # Excel percentages are fractions; the table holds them on a 0-100 scale.
                    stored = cell.value / 100 if cell.format == "percent" and cell.is_number else cell.value
                    x = ws.cell(row=row, column=i, value=stored)
                    fmt = excel_format(cell) if cell.is_number else None
                    if fmt:
                        x.number_format = fmt
                    x.alignment = Alignment(horizontal="right")
                x.border = border
            if r.kind in ("subtotal", "total"):
                for i in range(1, len(t.headers) + 2):
                    ws.cell(row=row, column=i).font = Font(bold=True)
                    if r.kind == "total":
                        ws.cell(row=row, column=i).fill = total_fill
        row += 2
        lines = ([t.note] if t.note else []) + _gap_lines(t) + [_note_line(n) for n in notes_for(t, notes)]
        if shared:
            lines.append("See the Notes sheet for assumptions that apply to more than one table.")
        for line in lines:
            ws.cell(row=row, column=1, value=line).alignment = Alignment(wrap_text=False)
            row += 1
        ws.column_dimensions["A"].width = min(60, max(18, max(len(r.label) + 2 * r.indent for r in t.rows) + 2))
        for i in range(2, len(t.headers) + 2):
            ws.column_dimensions[get_column_letter(i)].width = max(11, min(18, len(t.headers[i - 2]) + 2))
        ws.freeze_panes = f"B{hr + 1}"

    ws = wb.create_sheet("Notes")
    ws["A1"] = "Assumptions, gaps and warnings"
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = " · ".join([head, *below, period])
    ws["A2"].font = Font(italic=True, color="595959")
    for j, h in enumerate(["Id", "Type", "Note", "Tables", "How to change"], start=1):
        c = ws.cell(row=3, column=j, value=h)
        c.font = Font(bold=True)
        c.fill = header_fill
    for i, n in enumerate(notes, start=4):
        affected = "; ".join(t.title for t in tables if t.id in n.tables) or "All tables"
        for j, v in enumerate([n.id, n.kind, n.text, affected, n.how_to_change], start=1):
            ws.cell(row=i, column=j, value=v).alignment = Alignment(wrap_text=True, vertical="top")
    for col, w in zip("ABCDE", (8, 12, 70, 30, 45)):
        ws.column_dimensions[col].width = w

    defs = (report or {}).get("definitions") or []
    if defs:
        ws = wb.create_sheet("Definitions")
        ws["A1"] = "Reporting definitions"
        ws["A1"].font = Font(bold=True, size=13)
        row = 3
        for d in defs:
            if d.get("heading"):
                ws.cell(row=row, column=1, value=d["heading"]).font = Font(bold=True)
                row += 1
            ws.cell(row=row, column=1, value=d["text"]).alignment = Alignment(wrap_text=True, vertical="top")
            row += 2
        ws.column_dimensions["A"].width = 110
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _sheet_name(title: str, used: set[str]) -> str:
    """An Excel tab name (at most 31 characters) that reads cleanly: "/" becomes "-", cut at a word."""
    base = re.sub(r"\s*/\s*", "-", title)
    base = re.sub(r"[\[\]\*\?\\:]", " ", base)
    base = " ".join(base.split()) or "Table"
    base = _fit(base, 31)
    name, i = base, 2
    while name.lower() in used or name.lower() in ("notes", "definitions"):
        name = f"{_fit(base, 28)} {i}"
        i += 1
    used.add(name.lower())
    return name


def _fit(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit + 1].rsplit(" ", 1)[0]
    return (cut if len(cut) >= limit // 2 else text[:limit]).rstrip(" -×x·,")
