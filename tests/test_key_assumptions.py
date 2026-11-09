"""V.1.5: key assumptions about who the report counts, and guide wording fixes."""

import io
import json

from docx import Document
from openpyxl import load_workbook

from gra.data_quality import display_titles, table_statuses
from gra.engine import SYSTEM_PROMPT, TOOLS, Note
from gra.exports import key_notes, notes_for, shared_notes, to_docx, to_xlsx
from gra.tables import Cell, Row, Table
from tests.test_engine import GENDER_SQL, gender_table, make

KEY = ("note_assumption", {"id": "K1", "kind": "assumption", "key": True,
                           "question": "Should only cases with income recorded be counted?",
                           "options": [{"label": "Keep: count every closed case"},
                                       {"label": "Only cases with income", "instruction": "Count only cases with income recorded."},
                                       {"label": "x", "instruction": "y"}, {"label": "fourth is dropped"}, {"bad": 1}],
                           "text": "Every closed case is counted; income eligibility could not be checked.",
                           "tables": ["t1"], "how_to_change": "Ask to count only cases with income recorded."})


def test_prompt_tells_the_ai_to_count_everything_when_a_test_cannot_be_checked():
    assert "assume the uploaded data already holds the organization's reportable" in SYSTEM_PROMPT
    assert "do not narrow the report to the few records that pass" in SYSTEM_PROMPT
    assert "record no key notes; the summary is then not shown" in SYSTEM_PROMPT
    assert "Scope question" in SYSTEM_PROMPT and "Data that looks incomplete" in SYSTEM_PROMPT
    props = next(t for t in TOOLS if t["name"] == "note_assumption")["input_schema"]["properties"]
    assert props["key"]["type"] == "boolean"


def test_key_flag_is_kept_on_the_note(tiny_db):
    session, _ = make(tiny_db, [[("run_query", {"sql": GENDER_SQL, "purpose": "Counting"})],
                                [KEY, ("submit_tables", {"tables": [gender_table()]})]])
    session.run()
    assert session.status == "done"
    note = session.notes["K1"]
    assert note.key is True and note.to_dict()["key"] is True
    assert note.question.startswith("Should only")
    assert [o["label"] for o in note.options] == ["Keep: count every closed case", "Only cases with income", "x"]
    assert note.options[1]["instruction"] == "Count only cases with income recorded."


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
    head = text.index("Some considerations to check")
    assert text[head + 1].startswith("Consideration: Every record counted.")
    assert sum("Every record counted" in x for x in text) == 1
    wb = load_workbook(io.BytesIO(to_xlsx(tables, notes, "Report", "2025")))
    assert wb["Notes"]["B4"].value == "consideration (assumption)"


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


def test_fictional_sample_is_served_and_packed(tmp_path):
    import importlib.util
    from pathlib import Path

    from fastapi.testclient import TestClient

    from gra.server import create_app

    client = TestClient(create_app(lambda: None))
    s = client.get("/api/sample").json()
    assert s["period"] == ["2026-01-01", "2026-12-31"] and len(s["data"]) == 6
    for name in s["report"] + s["data"]:
        res = client.get(f"/api/sample/{name}")
        assert res.status_code == 200 and len(res.content) > 100
    assert client.get("/api/sample/..%2Fserver.py").status_code == 404
    assert client.get("/api/sample/other.csv").status_code == 404
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("build_release", root / "tools" / "build_release.py")
    br = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(br)
    assert br.SAMPLE_FILES <= set(br.program_files())
    assert "Synthetic demonstration edition" in (root / "gra" / "sample" / "README.txt").read_text() or \
        "SYNTHETIC" in (root / "gra" / "sample" / "README.txt").read_text()


def test_guide_shows_percent_missing(tiny_db):
    from tests.test_data_quality import GAP_NOTE, gap_table, run_with, text_of
    from gra.data_quality import to_quality_docx

    s = run_with(tiny_db, [[("run_query", {"sql": GENDER_SQL, "purpose": "Counting"})],
                           [GAP_NOTE, ("submit_tables", {"tables": [gap_table()]})]])
    doc = Document(io.BytesIO(to_quality_docx(s.tables, list(s.notes.values()), s.gate.catalog,
                                              [q.sql for q in s.gate.log], {}, ("2025-01-01", "2025-12-31"))))
    grid = doc.tables[-1]
    assert [c.text for c in grid.rows[0].cells][-1] == "% missing"
    gender = next(r for r in grid.rows if r.cells[0].text == "Gender")
    assert gender.cells[3].text == "1" and gender.cells[4].text == "2%"  # 1 of 59 blank


def test_sample_report_link_opens_the_pdf():
    from pathlib import Path
    from urllib.parse import unquote

    from fastapi.testclient import TestClient

    from gra.server import create_app

    html = (Path(__file__).resolve().parents[1] / "gra" / "static" / "index.html").read_text()
    href = html.split('id="sample-report" href="')[1].split('"')[0]
    res = TestClient(create_app(lambda: None)).get(href)
    assert res.status_code == 200 and res.headers["content-type"] == "application/pdf"
    assert "attachment" not in res.headers.get("content-disposition", "")
    assert unquote(href).endswith("annual report.pdf")


def test_demo_runs_with_no_key_and_cannot_be_corrected(monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from gra.server import create_app

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    c = TestClient(create_app(lambda: (_ for _ in ()).throw(AssertionError("no AI service in the demo"))))
    run_id = c.post("/api/demo").json()["id"]
    for _ in range(300):
        r = c.get(f"/api/runs/{run_id}").json()
        if r["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert r["status"] == "done" and r["demo"] is True, r.get("error")
    assert [t["title"] for t in r["tables"]] == ["01 Race / ethnicity by age", "02 Gender",
                                                 "03 Legal problems × case closure outcomes"]
    assert not any(n["key"] for n in r["notes"])  # complete tables: no considerations to raise
    assert any("nothing is sent to any AI service" in e["message"] for e in r["events"])
    assert c.post(f"/api/runs/{run_id}/correct", json={"text": "x"}).status_code == 400
    log = c.get(f"/api/runs/{run_id}/activity-log").text
    assert "recorded demo with fictional data" in log


def test_figures_explain_how_they_were_counted(tiny_db):
    session, _ = make(tiny_db, [[("run_query", {"sql": GENDER_SQL, "purpose": "Counting clients by gender"})],
                                [("submit_tables", {"tables": [gender_table()]})]])
    session.run()
    snap = session.snapshot()
    assert snap["purposes"]["Q1"] == "Counting clients by gender"
    rows = snap["tables"][0]["rows"]
    detail = next(r for r in rows if r["kind"] == "detail")
    how = detail["cells"][0]["how"]
    assert how["query"] == "Q1" and how["match"]
    total = next(r for r in rows if r["kind"] == "total")
    assert total["cells"][0]["how"]["sum_of"]


def test_saved_choices_are_kept_and_reused(tiny_files, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from gra.server import create_app
    from tests.scripted import ScriptedModel
    from tests.test_server import start, wait

    seen = []

    def factory():
        m = ScriptedModel([[("run_query", {"sql": GENDER_SQL, "purpose": "Counting"})],
                           [KEY, ("submit_tables", {"tables": [gender_table()]})],
                           [("submit_tables", {"tables": [gender_table()]})]])
        seen.append(m)
        return m

    c = TestClient(create_app(factory))
    run_id = start(c, tiny_files, tmp_path)
    assert wait(c, run_id)["status"] == "done"
    assert c.post(f"/api/runs/{run_id}/correct", json={"text": "Count only cases with income recorded."}).status_code == 200
    wait(c, run_id)
    res = c.post(f"/api/runs/{run_id}/save-choices").json()
    assert res["count"] == 2
    (saved,) = c.get("/api/saved-choices").json()
    assert saved["report_files"] == ["report.txt"]

    report = tmp_path / "report.txt"
    files = [("report_files", ("report.txt", report.read_bytes(), "text/plain"))]
    files += [("data_files", (f.name, f.read_bytes(), "text/csv")) for f in tiny_files]
    run2 = c.post("/api/runs", files=files, data={"period_start": "2026-01-01", "period_end": "2026-12-31",
                                                  "saved_choices": saved["id"]}).json()["id"]
    r2 = wait(c, run2)
    opening = json.dumps(seen[-1].seen_messages[0][0]["content"])
    assert "SAVED CHOICES" in opening and "Count only cases with income recorded." in opening
    assert any("Reusing 2 saved choice(s)" in e["message"] for e in r2["events"])
    assert c.delete(f"/api/saved-choices/{saved['id']}").status_code == 200
    assert c.get("/api/saved-choices").json() == []


def test_prompt_flags_big_swings_from_the_past_report():
    assert "200% or more" in SYSTEM_PROMPT and "at least one of the two figures is 10 or more" in SYSTEM_PROMPT



def test_long_corrections_are_refused_not_cut(monkeypatch):
    from gra import api
    assert api.MAX_CORRECTION >= 8000
    from pathlib import Path
    src = Path(api.__file__).read_text()
    assert "text[:4000]" not in src and "too long" in src
