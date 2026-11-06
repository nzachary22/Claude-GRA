"""The data collection guide the user shares with their team."""

import io

from docx import Document
from fastapi.testclient import TestClient

from gra.data_quality import field_completeness, has_gaps, table_statuses, to_quality_docx
from gra.engine import MAX_TABLE_ATTEMPTS
from gra.server import create_app
from tests.scripted import ScriptedModel
from tests.test_engine import GENDER_SQL, gender_table, make
from tests.test_server import start, wait

GAP_NOTE = ("note_assumption", {
    "id": "G1", "kind": "gap", "text": "Income is not recorded, so eligibility cannot be shown.",
    "tables": ["t1"], "how_to_change": "Supply household income.",
    "collect": "Ask every client their household income at intake and record it in the Income field."})


def gap_table():
    t = gender_table()
    t["rows"].insert(3, {"label": "Income eligible", "kind": "detail", "gap": "Income not recorded"})
    t["rows"][-1]["sum_of_rows"] = [0, 1, 2, 4]
    return t


def text_of(data: bytes) -> str:
    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    parts += [c.text for t in doc.tables for r in t.rows for c in r.cells]
    return "\n".join(parts)


def run_with(tiny_db, steps):
    session, _ = make(tiny_db, steps)
    session.run()
    assert session.status == "done"
    return session


def test_guide_lists_what_to_collect_tables_affected_and_field_completeness(tiny_db):
    s = run_with(tiny_db, [[("run_query", {"sql": GENDER_SQL, "purpose": "Counting"})],
                           [GAP_NOTE, ("submit_tables", {"tables": [gap_table()]})]])
    notes = list(s.notes.values())
    assert has_gaps(s.tables, notes)
    (status,) = table_statuses(s.tables, notes)
    assert status.status == "Partly complete" and status.unavailable == 1
    fields = field_completeness(s.gate.catalog, [q.sql for q in s.gate.log])
    gender = next(f for f in fields if f.field == "Gender")
    assert gender.missing == 1 and gender.filled == 58  # one blank gender in the synthetic file
    assert all(f.field != "County" for f in fields)  # not used by this report

    text = text_of(to_quality_docx(s.tables, notes, s.gate.catalog, [q.sql for q in s.gate.log],
                                   {"organization": "Example Center", "title": "Annual report"},
                                   ("2025-01-01", "2025-12-31")))
    assert "Data collection guide" in text and "Example Center" in text
    assert "Ask every client their household income" in text
    assert "Why it matters: Income is not recorded" in text
    assert "Clients by gender" in text and "Partly complete" in text
    assert "Gender" in text and "2%" in text and "% missing" in text
    for secret in ("Stone", "Ava", "C-001", "mold", "1980-02-01"):
        assert secret not in text


def test_no_guide_needed_when_everything_was_built(tiny_db):
    s = run_with(tiny_db, [[("run_query", {"sql": GENDER_SQL, "purpose": "Counting"})],
                           [("submit_tables", {"tables": [gender_table()]})]])
    assert not has_gaps(s.tables, list(s.notes.values()))


def test_not_completed_table_appears_in_the_guide(tiny_db):
    bad = gender_table()
    bad["rows"][0]["cells"] = []
    s = run_with(tiny_db, [[("run_query", {"sql": GENDER_SQL, "purpose": "Counting"})]]
                 + [[("submit_tables", {"tables": [bad]})]] * MAX_TABLE_ATTEMPTS)
    notes = list(s.notes.values())
    assert [x.status for x in table_statuses(s.tables, notes)] == ["Not completed"]
    assert has_gaps(s.tables, notes)


def test_guide_download_and_flag(tiny_files, tmp_path, monkeypatch):
    monkeypatch.setenv("GRA_DATA_DIR", str(tmp_path / "appdata"))
    client = TestClient(create_app(lambda: ScriptedModel([
        [("run_query", {"sql": GENDER_SQL, "purpose": "Counting clients by gender"})],
        [GAP_NOTE, ("submit_tables", {"tables": [gap_table()]})],
    ])))
    run_id = start(client, tiny_files, tmp_path)
    r = wait(client, run_id)
    assert r["data_gaps"] is True
    res = client.get(f"/api/runs/{run_id}/data-quality")
    assert res.status_code == 200
    assert "data-collection-guide-20251231.docx" in res.headers["content-disposition"]
    assert "household income" in text_of(res.content)
