"""Word and Excel exports of the draft tables, with their notes and gaps."""

from __future__ import annotations

import io
import re

from .engine import Note
from .tables import Cell, Table

GAP_MARK = "n/a"


def display(cell: Cell) -> str:
    if cell.gap:
        return GAP_MARK
    v = cell.value
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    return str(v)


def notes_for(table: Table, notes: list[Note]) -> list[Note]:
    return [n for n in notes if not n.tables or table.id in n.tables]


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

def to_docx(tables: list[Table], notes: list[Note], title: str, period: str) -> bytes:
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
    doc.add_heading(title, level=0)
    doc.add_paragraph(f"Reporting period: {period}. Draft prepared from current local data; review before submission.")

    def shade(cell, hex_color):
        tc_pr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), hex_color)
        tc_pr.append(shd)

    for t in tables:
        doc.add_heading(t.title, level=1)
        grid = doc.add_table(rows=1, cols=len(t.headers) + 1)
        grid.style = "Table Grid"
        grid.alignment = WD_TABLE_ALIGNMENT.CENTER
        hdr = grid.rows[0].cells
        for i, text in enumerate([""] + t.headers):
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

    general = [n for n in notes if not any(t.id in n.tables for t in tables)]
    if general:
        doc.add_heading("Assumptions and data notes", level=1)
        for n in general:
            doc.add_paragraph(_note_line(n), style="List Bullet")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- Excel

def to_xlsx(tables: list[Table], notes: list[Note], title: str, period: str) -> bytes:
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

    for t in tables:
        ws = wb.create_sheet(_sheet_name(t.title or t.id, used))
        ws["A1"] = t.title
        ws["A1"].font = Font(bold=True, size=13)
        ws["A2"] = f"{title} · {period}"
        ws["A2"].font = Font(italic=True, color="595959")
        hr = 4
        for i, h in enumerate([""] + t.headers, start=1):
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
                    x = ws.cell(row=row, column=i, value=cell.value)
                    if isinstance(cell.value, int):
                        x.number_format = "#,##0"
                    elif isinstance(cell.value, float):
                        x.number_format = "#,##0.00"
                x.border = border
            if r.kind in ("subtotal", "total"):
                for i in range(1, len(t.headers) + 2):
                    ws.cell(row=row, column=i).font = Font(bold=True)
                    if r.kind == "total":
                        ws.cell(row=row, column=i).fill = total_fill
        row += 2
        lines = ([t.note] if t.note else []) + _gap_lines(t) + [_note_line(n) for n in notes_for(t, notes)]
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
    for j, h in enumerate(["Id", "Type", "Note", "Tables", "How to change"], start=1):
        c = ws.cell(row=3, column=j, value=h)
        c.font = Font(bold=True)
        c.fill = header_fill
    for i, n in enumerate(notes, start=4):
        for j, v in enumerate([n.id, n.kind, n.text, ", ".join(n.tables), n.how_to_change], start=1):
            ws.cell(row=i, column=j, value=v).alignment = Alignment(wrap_text=True, vertical="top")
    for col, w in zip("ABCDE", (8, 12, 70, 14, 45)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _sheet_name(title: str, used: set[str]) -> str:
    base = re.sub(r"[\[\]\*\?/\\:]", " ", title).strip()[:28] or "Table"
    name, i = base, 2
    while name.lower() in used:
        name = f"{base[:25]} {i}"
        i += 1
    used.add(name.lower())
    return name
