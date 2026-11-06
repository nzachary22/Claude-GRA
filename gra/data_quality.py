"""Data collection guide: a Word report the user can share with their team.

After a run, it lists the information that was missing or inconsistent in the
client records, what staff should collect or record so future reports can be
completed, which tables were affected, and how complete each field the report
relied on was. It holds field names and counts only, never client details,
so it is safe to pass around.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date

from .engine import Note
from .importer import Catalog
from .exports import repeat_header
from .tables import Table

ATTENTION_BELOW = 90.0  # percent filled under which a field is flagged


@dataclass
class TableStatus:
    title: str
    available: int
    unavailable: int
    status: str  # Complete | Partly complete | Not completed


@dataclass
class FieldCompleteness:
    field: str
    source_file: str
    filled: int
    missing: int

    @property
    def percent(self) -> float:
        total = self.filled + self.missing
        return 100.0 * self.filled / total if total else 0.0


def has_gaps(tables: list[Table], notes: list[Note]) -> bool:
    """Whether the run left anything for the team to collect."""
    return bool(action_notes(notes)) or any(s.status != "Complete" for s in table_statuses(tables, notes))


def action_notes(notes: list[Note]) -> list[Note]:
    """Notes that ask the team to collect or record something."""
    return [n for n in notes if not n.id.startswith("app-") and (n.kind == "gap" or n.collect)]


def table_statuses(tables: list[Table], notes: list[Note]) -> list[TableStatus]:
    incomplete = {t for n in notes if n.id.startswith("app-incomplete-") for t in n.tables}
    out = []
    for t in tables:
        cells = [c for r in t.rows if r.kind != "heading" for c in r.cells]
        unavailable = sum(1 for c in cells if c.gap)
        available = sum(1 for c in cells if not c.gap and c.value is not None)
        if t.id in incomplete and any(r.label == "Not completed" for r in t.rows):
            status = "Not completed"
        elif unavailable or any(n.id == f"app-simplified-{t.id}" for n in notes):
            status = "Partly complete"
        else:
            status = "Complete"
        out.append(TableStatus(t.title or t.id, available, unavailable, status))
    return out


def field_completeness(catalog: Catalog, sqls: list[str]) -> list[FieldCompleteness]:
    """How often each field the report's calculations used was filled in, least complete first."""
    words = {w.lower() for sql in sqls for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sql or "")}
    fields = []
    for t in catalog.tables:
        if t.reference or not t.rows:
            continue
        for c in t.columns:
            if c.name.lower() in words:
                fields.append(FieldCompleteness(c.source_name or c.name, t.source_file,
                                                max(t.rows - c.nulls, 0), c.nulls))
    return sorted(fields, key=lambda f: (f.percent, f.field.lower()))


def to_quality_docx(tables: list[Table], notes: list[Note], catalog: Catalog, sqls: list[str],
                    report: dict, period: tuple[str, str], today: date | None = None) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    today = today or date.today()
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)
    muted = RGBColor(0x59, 0x59, 0x59)

    org, title = report.get("organization"), report.get("title") or "the grant report"
    if not title.lower().startswith("the "):
        title = f"the {title}"
    doc.add_heading("Data collection guide", level=0)
    line = " · ".join(x for x in (org, report.get("title"), f"Reporting period {period[0]} to {period[1]}",
                                    f"Prepared {today.isoformat()}") if x)
    p = doc.add_paragraph(line)
    p.runs[0].font.color.rgb = muted

    statuses = table_statuses(tables, notes)
    short = [s for s in statuses if s.status != "Complete"]
    actions = action_notes(notes)
    doc.add_paragraph(
        f"When we prepared {title}, {len(short)} of {len(statuses)} tables could not be completed in full "
        "because some information was missing or recorded inconsistently in our client records, "
        "or the table could not be built from what was recorded. "
        "This guide lists what to collect and record from now on so future reports can be completed.")
    note = doc.add_paragraph("It contains field names and counts only, no client information, so it can be shared "
                             "with the team.")
    note.runs[0].italic = True

    doc.add_heading("What to collect and record", level=1)
    if actions:
        for n in actions:
            item = doc.add_paragraph(style="List Number")
            item.add_run(n.collect or n.how_to_change or n.text).bold = True
            why = doc.add_paragraph(f"Why it matters: {n.text}")
            why.paragraph_format.left_indent = Pt(18)
            named = [t.title for t in tables if t.id in n.tables]
            if named:
                aff = doc.add_paragraph(f"Affects: {'; '.join(named)}")
                aff.paragraph_format.left_indent = Pt(18)
                aff.runs[0].font.color.rgb = muted
    else:
        doc.add_paragraph("No specific information to collect was identified. See the tables below.")

    if short:
        doc.add_heading("Tables affected", level=1)
        grid = doc.add_table(rows=1, cols=4)
        grid.style = "Light Grid Accent 1"
        repeat_header(grid.rows[0])
        for cell, text in zip(grid.rows[0].cells, ("Table", "Figures available", "Figures unavailable", "Status")):
            cell.text = text
        for s in short:
            cells = grid.add_row().cells
            for cell, text in zip(cells, (s.title, f"{s.available:,}", f"{s.unavailable:,}", s.status)):
                cell.text = text
            for cell in cells[1:3]:
                cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT

    fields = field_completeness(catalog, sqls)
    if fields:
        doc.add_heading("How complete our records are", level=1)
        doc.add_paragraph(f"These are the fields the report relied on, least complete first. Fields filled in on fewer "
                          f"than {ATTENTION_BELOW:.0f}% of records are marked as needing attention.")
        grid = doc.add_table(rows=1, cols=5)
        grid.style = "Light Grid Accent 1"
        repeat_header(grid.rows[0])
        for cell, text in zip(grid.rows[0].cells, ("Field", "Data file", "Filled in", "Missing", "% filled")):
            cell.text = text
        for f in fields:
            cells = grid.add_row().cells
            flag = " (needs attention)" if f.percent < ATTENTION_BELOW else ""
            for cell, text in zip(cells, (f.field, f.source_file, f"{f.filled:,}", f"{f.missing:,}",
                                          f"{f.percent:.0f}%{flag}")):
                cell.text = text
            for cell in cells[2:]:
                cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT

    doc.add_heading("Tips for the team", level=1)
    for tip in ("Fill in every field listed above for each new client and case, including at closing.",
                "Use the same spelling and the listed choices every time, so records can be counted together.",
                "If a client declines to answer, record that choice rather than leaving the field blank."):
        doc.add_paragraph(tip, style="List Bullet")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
