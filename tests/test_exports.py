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


def test_excel_export_keeps_numbers_numeric():
    tables, notes = sample()
    wb = load_workbook(io.BytesIO(to_xlsx(tables, notes, "Annual report", "2025")))
    ws = wb["Clients by gender"]
    assert ws.cell(row=6, column=2).value == 1200
    assert ws.cell(row=6, column=2).number_format == "#,##0"
    assert ws.cell(row=7, column=3).value == "n/a"
    assert ws.cell(row=8, column=1).font.bold
    assert wb["Notes"].cell(row=4, column=3).value.startswith("Blank gender")
