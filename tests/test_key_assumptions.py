"""V.1.5: key assumptions about who the report counts, and guide wording fixes."""

import io

from docx import Document
from openpyxl import load_workbook

from gra.data_quality import display_titles, table_statuses
from gra.engine import SYSTEM_PROMPT, TOOLS, Note
from gra.exports import key_notes, notes_for, shared_notes, to_docx, to_xlsx
from gra.tables import Cell, Row, Table
from tests.test_engine import GENDER_SQL, gender_table, make

KEY = ("note_assumption", {"id": "K1", "kind": "assumption", "key": True,
                           "text": "Every closed case is counted; income eligibility could not be checked.",
                           "tables": ["t1"], "how_to_change": "Ask to count only cases with income recorded."})


def test_prompt_tells_the_ai_to_count_everything_when_a_test_cannot_be_checked():
    assert "assume the uploaded data already holds the organization's reportable" in SYSTEM_PROMPT
    assert "do not narrow the report to the few records that pass" in SYSTEM_PROMPT
    props = next(t for t in TOOLS if t["name"] == "note_assumption")["input_schema"]["properties"]
    assert props["key"]["type"] == "boolean"


def test_key_flag_is_kept_on_the_note(tiny_db):
    session, _ = make(tiny_db, [[("run_query", {"sql": GENDER_SQL, "purpose": "Counting"})],
                                [KEY, ("submit_tables", {"tables": [gender_table()]})]])
    session.run()
    assert session.status == "done"
    note = session.notes["K1"]
    assert note.key is True and note.to_dict()["key"] is True


def tables_and_notes():
    t1 = Table("t1", "Clients", "", ["Count"], [Row("All", "total", 0, [Cell(652)])])
    t2 = Table("t2", "Cases", "", ["Count"], [Row("All", "total", 0, [Cell(652)])])
    notes = [Note("K1", "assumption", "Every record counted.", ["t1"], "Ask for the stricter count.", key=True),
             Note("A1", "assumption", "Only table one.", ["t1"])]
    return [t1, t2], notes


def test_key_notes_lead_the_exports_and_are_not_repeated_beside_tables():
    tables, notes = tables_and_notes()
    assert key_notes(notes) == [notes[0]]
    assert notes[0] not in shared_notes(tables, notes) and notes[0] not in notes_for(tables[0], notes)
    doc = Document(io.BytesIO(to_docx(tables, notes, "Report", "2025")))
    text = [p.text for p in doc.paragraphs]
    head = text.index("Key assumptions: who these tables count")
    assert text[head + 1].startswith("Key assumption: Every record counted.")
    assert sum("Every record counted" in x for x in text) == 1
    wb = load_workbook(io.BytesIO(to_xlsx(tables, notes, "Report", "2025")))
    assert wb["Notes"]["B4"].value == "key assumption"


def test_guide_says_not_completed_and_tells_same_named_tables_apart():
    gap = [Row("All", "detail", 0, [Cell(gap="No roster")])]
    tables = [Table("e1", "Employee Demographics", "", ["Male", "Female"], gap),
              Table("e2", "Employee Demographics", "", ["White", "Black"], gap),
              Table("c", "Clients", "", ["Count"], [Row("All", "detail", 0, [Cell(3)]), Row("X", "detail", 0, [Cell(gap="n")])])]
    names = display_titles(tables)
    assert names["e1"] == "Employee Demographics (Male, Female)" and names["e2"] == "Employee Demographics (White, Black)"
    statuses = {s.title: s.status for s in table_statuses(tables, [])}
    assert statuses["Employee Demographics (Male, Female)"] == "Not completed"
    assert statuses["Clients"] == "Partly complete"
    same = [Table(i, "Staff", "", ["N"], gap) for i in ("a", "b")]
    assert display_titles(same) == {"a": "Staff (1 of 2)", "b": "Staff (2 of 2)"}
