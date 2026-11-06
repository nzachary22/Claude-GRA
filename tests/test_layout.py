"""Exported tables follow the reference report's page orientation."""

import io

from docx import Document
from docx.enum.section import WD_ORIENT
from openpyxl import load_workbook
from pypdf import PdfWriter

from gra.documents import layout_summary, page_orientations
from gra.exports import table_orientation, to_docx, to_xlsx
from gra.tables import Cell, Row, Table
from tests.test_engine import GENDER_SQL, gender_table, make


def pdf(path, sizes):
    w = PdfWriter()
    for width, height in sizes:
        w.add_blank_page(width=width, height=height)
    with open(path, "wb") as f:
        w.write(f)
    return path


def table(tid, ncols, orientation=""):
    headers = [f"C{i}" for i in range(ncols)]
    return Table(tid, f"Table {tid}", "", headers, [Row("R", "detail", 0, [Cell(value=1) for _ in headers])],
                 orientation=orientation)


def test_reads_pdf_page_orientations(tmp_path):
    portrait = pdf(tmp_path / "p.pdf", [(612, 792)] * 3)
    mixed = pdf(tmp_path / "m.pdf", [(612, 792), (792, 612), (792, 612), (612, 792)])
    assert page_orientations(portrait) == ["portrait"] * 3
    assert layout_summary([portrait]) == ("p.pdf: 3 page(s), all portrait", "portrait")
    text, single = layout_summary([mixed])
    assert single is None
    assert text == "m.pdf: pages 1 portrait; pages 2-3 landscape; pages 4 portrait"
    assert layout_summary([tmp_path / "notes.txt"]) == ("", None)


def test_orientation_precedence():
    assert table_orientation(table("a", 12), "portrait") == "portrait"  # all-portrait reference wins over width
    assert table_orientation(table("a", 2), "landscape") == "landscape"
    assert table_orientation(table("a", 12, "portrait"), None) == "portrait"  # the table's own page wins
    assert table_orientation(table("a", 12), None) == "landscape"  # unknown layout: wide tables go landscape
    assert table_orientation(table("a", 3), None) == "portrait"


def orientations(data):
    return [s.orientation for s in Document(io.BytesIO(data)).sections]


def test_word_follows_an_all_portrait_reference_even_for_wide_tables():
    data = to_docx([table("a", 3), table("b", 12)], [], "T", "2025", page_orientation="portrait")
    assert set(orientations(data)) == {WD_ORIENT.PORTRAIT}


def test_word_follows_an_all_landscape_reference():
    data = to_docx([table("a", 3)], [], "T", "2025", page_orientation="landscape")
    doc = Document(io.BytesIO(data))
    assert [s.orientation for s in doc.sections] == [WD_ORIENT.LANDSCAPE]
    assert doc.sections[0].page_width > doc.sections[0].page_height


def test_word_follows_each_tables_page_in_a_mixed_reference():
    data = to_docx([table("a", 3, "portrait"), table("b", 3, "landscape"), table("c", 3, "portrait")],
                   [], "T", "2025")
    assert orientations(data) == [WD_ORIENT.PORTRAIT, WD_ORIENT.LANDSCAPE, WD_ORIENT.PORTRAIT]


def test_excel_sheets_carry_the_orientation():
    data = to_xlsx([table("a", 3, "landscape"), table("b", 12)], [], "T", "2025", page_orientation="portrait")
    wb = load_workbook(io.BytesIO(data))
    assert wb["Table a"].page_setup.orientation == "landscape"
    assert wb["Table b"].page_setup.orientation == "portrait"


def test_ai_is_told_the_layout_and_orientation_reaches_the_table(tiny_db):
    spec = gender_table()
    spec["orientation"] = "landscape"
    session, model = make(tiny_db, [[("run_query", {"sql": GENDER_SQL, "purpose": "Counting"})],
                                    [("submit_tables", {"tables": [spec]})]])
    session.layout_text, session.page_orientation = "r.pdf: 2 page(s), all landscape", "landscape"
    session.messages[0]["content"] = session._opening("schema", [], session.period)
    session.run()
    assert "REFERENCE REPORT PAGE LAYOUT" in str(model.seen_messages[0][0])
    assert session.tables[0].orientation == "landscape"
