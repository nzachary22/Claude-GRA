"""The plain-language AI activity report: what stayed local, what was sent, packet by packet.

Response summaries are written by the app from the data actually sent, so a reader
is not relying on the AI's description of its own results.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass

from .activity import sent_text
from .importer import Catalog

SENT, DENIED, REVIEW = "Sent to AI", "Denied", "Sent to AI: REVIEW"
PROTECTED_ROLES = ("identifier", "sensitive", "free_text")
_DENIAL_REASONS = [
    (re.compile(r"outermost SELECT|individual records|SELECT \*", re.I), "asked for individual records rather than totals"),
    (re.compile(r"identifier", re.I), "tried to use a protected ID in a way that is not allowed"),
    (re.compile(r"raw personal|raw values|raw column|MIN/MAX|buckets", re.I), "asked for detail too close to individual values"),
    (re.compile(r"budget", re.I), "the query limit for this report was reached"),
    (re.compile(r"not allowed|not available|read-only", re.I), "used an operation or field that is not allowed"),
]


@dataclass
class Packet:
    number: int
    status: str
    request: str
    response: str


def _short(text, limit: int = 90) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def _words(name: str) -> str:
    return str(name).replace("_", " ").strip()


def _join(items: list[str], limit: int = 4) -> str:
    shown = [_words(x) for x in items[:limit]]
    if len(items) > limit:
        shown.append(f"{len(items) - limit} more")
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]


def _query_summary(content: str) -> str:
    try:
        data = json.loads(content)
        columns, rows = data["columns"], data["rows"]
    except (ValueError, KeyError, TypeError):
        return "Partial totals (long result, shortened)"
    count = f"{len(rows)} row{'' if len(rows) == 1 else 's'}"
    if not rows:
        return f"Empty result for {_join(columns)} (0 rows)"
    groups = [c for i, c in enumerate(columns) if any(isinstance(r[i], str) for r in rows)]
    measures = [c for c in columns if c not in groups]
    if groups:
        # A sample group label lets a reader see the kind of value sent, not just its column name.
        spots = [columns.index(g) for g in groups]
        sample = next((r for r in rows if all(r[i] is not None for i in spots)), None)
        example = f", e.g. {' / '.join(_short(r_i, 24) for r_i in (sample[i] for i in spots))}" if sample else ""
        return f"Totals by {_join(groups)} ({count}){example}"
    return f"Totals: {_join(measures)} ({count})"


def _denial(content: str) -> str:
    reason = next((text for pattern, text in _DENIAL_REASONS if pattern.search(content)), None)
    if reason is None:
        return "Denied: the query could not run. No data sent, only the error."
    return f"Denied: {reason}. No data sent, only the reason."


def build_packets(events: list[dict], is_protected) -> list[Packet]:
    uses = {b.get("id"): b for e in events if e["kind"] == "response"
            for b in e.get("content") or [] if b.get("type") == "tool_use"}
    rows: list[tuple[str, str, str]] = []

    def add(status, request, response, checked=""):
        if status == SENT and checked and is_protected(checked):
            status = REVIEW
        rows.append((status, _short(request), _short(response, 170)))

    for e in events:
        if e["kind"] == "instructions":
            add(SENT, "Report started by the app", "Rules for the AI and its list of tools; no client data", e.get("text", ""))
        if e["kind"] != "message":
            continue
        confirmations: list[str] = []
        pending_name = None
        for b in e.get("content") or []:
            kind = b.get("type")
            if kind in ("document", "image"):
                add(SENT, "Report started by the app", f"Your uploaded report, in full: {b.get('title') or pending_name or 'uploaded file'}")
                pending_name = None
            elif kind == "text":
                text = b.get("text", "")
                match = re.match(r"--- Uploaded reporting document: (.+) ---$", text.strip())
                if match:
                    pending_name = match[1]
                elif pending_name:
                    add(SENT, "Report started by the app", f"Your uploaded report, as text: {pending_name}", text)
                    pending_name = None
                elif "DATABASE DESCRIPTION" in text:
                    add(SENT, "Report started by the app", "Data description: table and column names, counts, category values, date ranges", text)
                elif "asks for this correction" in text:
                    add(SENT, "Your correction", "The correction you typed", text)
                else:
                    add(SENT, "Reminder from the app", "Message asking the AI to continue; no data", text)
            elif kind == "tool_result":
                use = uses.get(b.get("tool_use_id"), {})
                name, args, content = use.get("name"), use.get("input") or {}, str(b.get("content", ""))
                if name == "run_query":
                    if b.get("is_error"):
                        add(DENIED, args.get("purpose") or "Calculation", _denial(content))
                    else:
                        add(SENT, args.get("purpose") or "Calculation", _query_summary(content), content)
                elif name == "submit_tables":
                    if b.get("is_error"):
                        add(SENT, "Submit finished tables", "List of table problems for the AI to fix; no client records", content)
                    else:
                        add(SENT, "Submit finished tables", "Confirmation with table names and sizes", content)
                elif name == "note_assumption":
                    confirmations.append(content)
                else:
                    add(SENT, _words(name or "Tool request"), "Short app reply; no data", content)
        if confirmations:
            count = len(confirmations)
            add(SENT, f"Record {count} note{'' if count == 1 else 's'} for you", "Confirmation only; no data", "\n".join(confirmations))
    return [Packet(i, *row) for i, row in enumerate(rows, start=1)]


def _report_name(events: list[dict]) -> str:
    for e in reversed(events):
        if e["kind"] == "response":
            for b in e.get("content") or []:
                report = (b.get("input") or {}).get("report") if b.get("name") == "submit_tables" else None
                if report and report.get("organization"):
                    return report["organization"]
    return ""


def _local_lines(catalog: Catalog) -> list[str]:
    data = [t for t in catalog.tables if not t.reference]
    records = sum(t.rows for t in data)
    files = ", ".join(sorted({t.source_file for t in data})) or "none"
    protected = [c.source_name for t in data for c in t.columns if c.role in ("identifier", "sensitive")]
    free = [c.source_name for t in data for c in t.columns if c.role == "free_text"]
    lines = [f"All {records:,} individual client and case records, in: {files}"]
    if protected:
        lines.append(f"The values in protected fields: {', '.join(sorted(set(protected)))}")
    if free:
        lines.append(f"Case notes and other free text: {', '.join(sorted(set(free)))}. The AI is not told these exist by name.")
    lines.append("The local database built from your files")
    return lines


def _sent_lines(catalog: Catalog, events: list[dict], packets: list[Packet]) -> list[str]:
    docs = [p.response.split(": ", 1)[1] for p in packets if p.response.startswith("Your uploaded report")]
    lines = [f"Your uploaded report, in full: {', '.join(docs)}" if docs else "No uploaded report was sent"]
    lines.append("Instructions telling the AI how to work")
    lines.append("A description of the data: table and column names, counts, category values and date ranges")
    codes = sorted({t.source_file for t in catalog.tables if t.reference})
    if codes:
        lines.append(f"Code lists, in full: {', '.join(codes)}")
    approved = sum(p.status != DENIED and p.response.startswith(("Totals", "Empty result", "Partial totals")) for p in packets)
    lines.append(f"Totals from {approved} approved quer{'y' if approved == 1 else 'ies'}, never individual records")
    if any(p.request == "Your correction" for p in packets):
        lines.append("Corrections you typed")
    return lines


def to_report_docx(events: list[dict], catalog: Catalog, is_protected, protected_checked: int) -> bytes:
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    packets = build_packets(events, is_protected)
    denied = sum(p.status == DENIED for p in packets)
    review = [p.number for p in packets if p.status == REVIEW]
    start = next((e for e in events if e["kind"] == "run_start"), {})
    period = start.get("period") or ["", ""]
    name = _report_name(events)

    doc = Document()
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = Inches(11), Inches(8.5)
    for side in ("left_margin", "right_margin"):
        setattr(section, side, Inches(0.8))
    section.top_margin = section.bottom_margin = Inches(0.7)
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    doc.add_heading("What Was Sent to the AI", level=0)
    doc.add_paragraph(" · ".join(x for x in (name, f"Reporting period {period[0]} to {period[1]}") if x))

    doc.add_heading("Summary", level=1)
    doc.add_paragraph(f"{len(packets) - denied} information packets were sent to the AI, and {denied} "
                      f"{'was' if denied == 1 else 'were'} denied. Individual client records stayed on this computer.")
    if review:
        p = doc.add_paragraph()
        run = p.add_run(f"Review needed: packets {', '.join(map(str, review))} contain a stored ID or note value.")
        run.bold = True
        run.font.color.rgb = RGBColor(0xB4, 0x23, 0x18)
    else:
        doc.add_paragraph(f"Automatic check: none of the {protected_checked:,} stored IDs and notes appeared in the text sent.")

    def bullets(title, lines):
        doc.add_heading(title, level=1)
        for line in lines:
            doc.add_paragraph(line, style="List Bullet")

    bullets("Stayed on this computer", _local_lines(catalog))
    bullets("Sent to the AI", _sent_lines(catalog, events, packets))

    doc.add_heading("Information packets", level=1)
    doc.add_paragraph("Scan this table for anything concerning. AI requests are in the AI's own words; "
                      "response summaries are written by the app from what was actually sent.")
    widths = (Inches(0.8), Inches(1.3), Inches(3.3), Inches(4.0))
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    table.autofit = False

    def fill(cell, text, width, bold=False, color=None):
        cell.width = width
        cell.text = text
        run = cell.paragraphs[0].runs[0] if cell.paragraphs[0].runs else cell.paragraphs[0].add_run("")
        run.bold = bold
        if color:
            run.font.color.rgb = color

    for cell, text, width in zip(table.rows[0].cells, ("Packet", "Status", "AI request summary", "Response summary"), widths):
        fill(cell, text, width, bold=True)
        shading = OxmlElement("w:shd")
        shading.set(qn("w:val"), "clear")
        shading.set(qn("w:color"), "auto")
        shading.set(qn("w:fill"), "D9E2F3")
        cell._tc.get_or_add_tcPr().append(shading)
    header = table.rows[0]._tr.get_or_add_trPr()
    repeat = OxmlElement("w:tblHeader")
    repeat.set(qn("w:val"), "true")
    header.append(repeat)
    for p in packets:
        cells = table.add_row().cells
        flagged = p.status != SENT
        color = RGBColor(0xB4, 0x23, 0x18) if flagged else None
        fill(cells[0], str(p.number), widths[0])
        fill(cells[1], p.status, widths[1], bold=flagged, color=color)
        fill(cells[2], p.request, widths[2])
        fill(cells[3], p.response, widths[3])

    bullets("Keep in mind", [
        "Uploaded PDFs and images go to the AI in full and are not checked automatically. Review them for client details.",
        "The automatic check skips birth dates and short or number-only values, because they match ordinary numbers and dates.",
        "This shows what this app sent. How the AI service stores data depends on your Anthropic account terms.",
        "The complete word-for-word record is available from Technical details in the app.",
    ])
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
