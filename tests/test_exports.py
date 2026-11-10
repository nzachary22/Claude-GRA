import io

from docx import Document
from openpyxl import load_workbook

from gra.engine import Note
from gra.exports import to_docx, to_xlsx
from gra.tables import Cell, Row, Table


def sample():
    t = Table("t1", "Clients by gender", "Each client counted once.", ["Adults", "Children", "Total"], [
        Row("Group A", "heading", 0, [Cell(), Cell(), Cell()]),
        Row("Women", "detail", 1, [Cell(1200), Cell(0), Cell(1200)]),
        Row("Men", "detail", 1, [Cell(3), Cell(gap="Age not recorded"), Cell(gap="Includes unavailable figures")]),
        Row("Total", "total", 0, [Cell(1203), Cell(gap="Includes unavailable figures"), Cell(gap="x")]),
    ])
    notes = [Note("A1", "assumption", "Blank gender shown as Not recorded.", ["t1"], "Say how to report blanks."),
             Note("W1", "warning", "Totals checked against case counts.", [], "")]
    return [t], notes


def test_word_export_keeps_values_gaps_and_notes():
    tables, notes = sample()
    doc = Document(io.BytesIO(to_docx(tables, notes, "Annual report", "2025-01-01 to 2025-12-31")))
    grid = doc.tables[0]
    assert [c.text for c in grid.rows[0].cells] == ["", "Adults", "Children", "Total"]
    assert grid.rows[2].cells[1].text == "1,200"
    assert grid.rows[3].cells[2].text == "n/a"
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Assumption: Blank gender" in text and "Age not recorded" in text and "Warning: Totals" in text


def formatted():
    t1 = Table("t1", "First", "Note", ["Clients", "Share", "Benefit"], [
        Row("Low income", "detail", 0, [Cell(630), Cell(13, format="percent", decimals=1), Cell(2642416, format="currency")]),
        Row("Rural", "detail", 0, [Cell(823), Cell(40.1, format="percent", decimals=1), Cell(0, format="currency")]),
    ], row_header="Characteristic", subtitle="Unduplicated clients: 2,050")
    t2 = Table("t2", "Second", "", ["Count"], [Row("All", "total", 0, [Cell(5)])])
    t3 = Table("t3", "Third", "", ["Count"], [Row("All", "total", 0, [Cell(7)])])
    notes = [Note("A1", "assumption", "Applies everywhere.", ["t1", "t2", "t3"]),
             Note("A2", "assumption", "Two tables only.", ["t1", "t2"]),
             Note("A3", "gap", "Only the first table.", ["t1"])]
    report = {"organization": "Example Law Center", "title": "Annual report", "subtitle": "Demonstration edition"}
    return [t1, t2, t3], notes, report


def test_word_shows_formats_headers_report_name_and_each_note_once():
    tables, notes, report = formatted()
    doc = Document(io.BytesIO(to_docx(tables, notes, "Fallback", "2026", report)))
    grid = doc.tables[0]
    assert grid.rows[0].cells[0].text == "Characteristic"
    assert [c.text for c in grid.rows[1].cells] == ["Low income", "630", "13.0%", "$2,642,416"]
    assert grid.rows[2].cells[2].text == "40.1%" and grid.rows[2].cells[3].text == "$0"
    text = [p.text for p in doc.paragraphs]
    assert text[0] == "Example Law Center" and "Annual report" in text and "Demonstration edition" in text
    assert "Fallback" not in text
    assert "Unduplicated clients: 2,050" in text
    joined = "\n".join(text)
    assert joined.count("Applies everywhere.") == 1 and joined.count("Two tables only.") == 1
    assert "Two tables only. [Affects: First; Second]" in joined and "Affects" not in next(x for x in text if "everywhere" in x)
    assert joined.count("Only the first table.") == 1


def test_excel_keeps_real_percents_and_currency():
    tables, notes, report = formatted()
    wb = load_workbook(io.BytesIO(to_xlsx(tables, notes, "Fallback", "2026", report)))
    ws = wb["First"]
    assert ws["A4"].value == "Characteristic" and ws["A2"].value == "Unduplicated clients: 2,050"
    assert ws["C5"].value == 0.13 and ws["C5"].number_format == "0.0%"
    assert ws["C6"].value == 0.401 and ws["C6"].number_format == "0.0%"
    assert ws["D5"].value == 2642416 and ws["D5"].number_format == '"$"#,##0'
    sheet_text = [str(c) for r in ws.iter_rows(values_only=True) for c in r if c]
    assert not any("Applies everywhere" in x for x in sheet_text)
    assert any("Only the first table" in x for x in sheet_text)
    assert "Example Law Center" in wb["Notes"]["A2"].value
    notes_col = [wb["Notes"].cell(row=r, column=3).value for r in range(4, 7)]
    assert notes_col == ["Applies everywhere.", "Two tables only.", "Only the first table."]


def test_title_falls_back_when_the_report_names_nothing():
    tables, notes, _ = formatted()
    doc = Document(io.BytesIO(to_docx(tables, notes, "Grant report tables", "2026")))
    assert doc.paragraphs[0].text == "Grant report tables"


def test_excel_export_keeps_numbers_numeric():
    tables, notes = sample()
    wb = load_workbook(io.BytesIO(to_xlsx(tables, notes, "Annual report", "2025")))
    ws = wb["Clients by gender"]
    assert ws.cell(row=6, column=2).value == 1200
    assert ws.cell(row=6, column=2).number_format == "#,##0"
    assert ws.cell(row=7, column=3).value == "n/a"
    assert ws.cell(row=8, column=1).font.bold
    assert wb["Notes"].cell(row=4, column=3).value.startswith("Blank gender")
    assert wb["Notes"].cell(row=4, column=4).value == "Clients by gender"
    assert wb["Notes"].cell(row=5, column=4).value == "All tables"


def test_definitions_are_exported_and_tab_names_read_cleanly():
    tables, notes = sample()
    tables[0].title = "01 Race / ethnicity by age at intake and county"
    report = {"organization": "Example Center", "definitions": [
        {"heading": "What the figures count", "text": "A case counts when it closed in the period.\nEach client counts once."},
        {"text": "Age is measured on the last day of the period."}]}
    doc = Document(io.BytesIO(to_docx(tables, notes, "Fallback", "2026", report)))
    text = [p.text for p in doc.paragraphs]
    assert "Reporting definitions" in text and "What the figures count" in text
    assert "Each client counts once." in text and "Age is measured on the last day of the period." in text
    wb = load_workbook(io.BytesIO(to_xlsx(tables, notes, "Fallback", "2026", report)))
    assert wb.sheetnames[0] == "01 Race-ethnicity by age at"
    assert wb.sheetnames[-1] == "Definitions"
    assert wb["Definitions"]["A3"].value == "What the figures count"
    assert all(len(n) <= 31 for n in wb.sheetnames)
    assert "Definitions" not in load_workbook(io.BytesIO(to_xlsx(tables, notes, "Fallback", "2026", {}))).sheetnames


def test_word_header_rows_repeat_and_stay_compact():
    from docx import Document
    from docx.oxml.ns import qn
    from gra.exports import header_font_size, to_docx
    from gra.tables import Cell, Row, Table
    headers = [f"Column heading number {i}" for i in range(12)]
    rows = [Row(f"Row {r}", "detail", 0, [Cell(value=r) for _ in headers]) for r in range(80)]
    data = to_docx([Table("t1", "Wide table", "", headers, rows)], [], "Grant report tables", "2025")
    table = Document(io.BytesIO(data)).tables[0]
    tr_pr = table.rows[0]._tr.trPr
    assert tr_pr.find(qn("w:tblHeader")) is not None
    assert table.rows[1]._tr.trPr is None or table.rows[1]._tr.trPr.find(qn("w:tblHeader")) is None
    run = table.rows[0].cells[1].paragraphs[0].runs[0]
    assert run.font.size == header_font_size(12) and run.bold


def test_word_rows_never_split_and_short_tables_stay_together():
    from docx.oxml.ns import qn
    from gra.exports import KEEP_TOGETHER_ROWS
    [t], notes = sample()
    t.subtitle = "Calendar year"
    long = Table("t2", "Long table", "", ["Count"], [Row("Section", "heading", 0, [Cell()])] + [
        Row(f"Row {i}", "detail", 1, [Cell(i)]) for i in range(KEEP_TOGETHER_ROWS + 5)])
    doc = Document(io.BytesIO(to_docx([t, long], notes, "Report", "2025")))

    def kept(row):
        return all(p.paragraph_format.keep_with_next for c in row.cells for p in c.paragraphs)

    for table in doc.tables:
        assert all(r._tr.trPr.find(qn("w:cantSplit")) is not None for r in table.rows)
    short, tall = doc.tables
    assert all(kept(r) for r in short.rows[:-1]) and not kept(short.rows[-1])
    assert kept(tall.rows[1])  # its section heading stays with the first row under it
    assert not any(kept(r) for r in tall.rows[2:])  # a long table may break between rows
    subtitle = next(p for p in doc.paragraphs if p.text == "Calendar year")
    assert subtitle.paragraph_format.keep_with_next
