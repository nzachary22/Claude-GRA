"""The plain-language report must be accurate, short and free of client data."""

import io

from docx import Document
from fastapi.testclient import TestClient

from gra.activity import read_events
from gra.activity_report import DENIED, REVIEW, SENT, build_packets, to_report_docx
from gra.engine import ReportSession
from gra.gate import QueryGate
from gra.schema import describe
from gra.server import create_app
from tests.scripted import ScriptedModel
from tests.test_engine import GENDER_SQL, gender_table
from tests.test_server import start, wait

STEPS = [
    [("run_query", {"sql": "SELECT client_id FROM clients", "purpose": "A listing of clients"}),
     ("run_query", {"sql": "SELEC nope", "purpose": "A broken query"}),
     ("run_query", {"sql": GENDER_SQL, "purpose": "Counting clients by gender"}),
     ("run_query", {"sql": "SELECT COUNT(*) AS cases FROM cases", "purpose": "Counting all cases"})],
    [("note_assumption", {"id": "A1", "kind": "assumption", "text": "Blank means unknown.", "tables": ["t1"]}),
     ("note_assumption", {"id": "A2", "kind": "warning", "text": "Check this.", "tables": ["t1"]})],
    [("submit_tables", {"tables": [{"id": "bad", "title": "Bad"}]})],
    [("submit_tables", {"report": {"organization": "Example Center"}, "tables": [gender_table("Q1")]})],
]


def run(tiny_db, tmp_path, blocks=None):
    db, catalog = tiny_db
    gate = QueryGate(db, catalog)
    path = tmp_path / "a.jsonl"
    session = ReportSession(gate, describe(db, catalog), blocks or [
        {"type": "text", "text": "--- Uploaded reporting document: report.pdf ---"},
        {"type": "document", "title": "report.pdf", "source": {"type": "base64", "media_type": "application/pdf", "data": "SGVsbG8="}},
    ] + [], ("2025-01-01", "2025-12-31"), ScriptedModel(STEPS), activity_path=path)
    session.run()
    assert session.status == "done"
    return session, gate, read_events(path), catalog


def test_packets_read_in_plain_language(tiny_db, tmp_path):
    _, gate, events, _ = run(tiny_db, tmp_path)
    rows = build_packets(events, gate.contains_protected)
    assert [p.number for p in rows] == list(range(1, len(rows) + 1))
    by_request = {p.request: p for p in rows}
    assert by_request["Counting clients by gender"].status == SENT
    assert by_request["Counting clients by gender"].response == "Totals by gender (4 rows)"
    assert by_request["Counting all cases"].response == "Totals: cases (1 row)"
    assert by_request["A listing of clients"].status == DENIED
    assert "individual records" in by_request["A listing of clients"].response or "protected ID" in by_request["A listing of clients"].response
    assert by_request["A broken query"].response.startswith("Denied: the query could not run")
    assert by_request["Record 2 notes for you"].response == "Confirmation only; no data"
    assert by_request["Submit finished tables"].response.startswith("List of table problems")
    assert any("report.pdf" in p.response for p in rows)
    assert any(p.response.startswith("Data description") for p in rows)
    assert all(len(p.response) <= 135 for p in rows)


def test_a_protected_value_is_flagged_on_its_packet(tiny_db, tmp_path):
    _, gate, events, _ = run(tiny_db, tmp_path)
    for e in events:
        for b in e.get("content") or []:
            if b.get("type") == "tool_result" and "woman" in str(b.get("content")):
                b["content"] = b["content"].replace("woman", "C-001")
    rows = build_packets(events, gate.contains_protected)
    assert [p.status for p in rows if p.request == "Counting clients by gender"] == [REVIEW]


def text_of(data):
    doc = Document(io.BytesIO(data))
    paragraphs = [p.text for p in doc.paragraphs]
    cells = [[c.text for c in r.cells] for t in doc.tables for r in t.rows]
    return doc, paragraphs, cells


def test_report_says_what_stayed_and_what_left(tiny_db, tmp_path):
    _, gate, events, catalog = run(tiny_db, tmp_path)
    doc, paragraphs, cells = text_of(to_report_docx(events, catalog, gate.contains_protected, gate.protected_value_count()))
    joined = "\n".join(paragraphs)
    for heading in ("What Was Sent to the AI", "Summary", "Stayed on this computer", "Sent to the AI", "Information packets", "Keep in mind"):
        assert heading in paragraphs
    assert "Example Center" in joined and "Reporting period 2025-01-01 to 2025-12-31" in joined
    assert "All 179 individual client and case records, in: cases.csv and clients.csv" in joined
    assert "Client ID" in joined and "Date of Birth" in joined and "Intake Notes" in joined
    assert "Code lists, in full: problem_codes.csv" in joined
    assert "Totals from 2 approved queries, never individual records" in joined
    assert "2 were denied" in joined
    assert cells[0] == ["Packet", "Status", "AI request summary", "Response summary"]
    assert doc.sections[0].page_width > doc.sections[0].page_height


def test_report_contains_no_client_values(tiny_db, tmp_path):
    _, gate, events, catalog = run(tiny_db, tmp_path)
    _, paragraphs, cells = text_of(to_report_docx(events, catalog, gate.contains_protected, gate.protected_value_count()))
    blob = " ".join(paragraphs) + " ".join(c for row in cells for c in row)
    for secret in ("C-001", "Stone", "Ava", "1980-02-01", "mold", "K-0001"):
        assert secret not in blob
    assert "none of the" in blob


def test_review_warning_appears_in_summary(tiny_db, tmp_path):
    _, gate, events, catalog = run(tiny_db, tmp_path)
    _, paragraphs, _ = text_of(to_report_docx(events, catalog, lambda text: "gender" in text and "columns" in text, 5))
    assert any(p.startswith("Review needed: packets") for p in paragraphs)


def test_report_download_including_after_restart(tmp_path, monkeypatch, tiny_files):
    monkeypatch.setenv("GRA_DATA_DIR", str(tmp_path / "appdata"))
    client = TestClient(create_app(lambda: ScriptedModel(STEPS)))
    run_id = start(client, tiny_files, tmp_path)
    wait(client, run_id)
    res = client.get(f"/api/runs/{run_id}/activity-report")
    assert res.status_code == 200 and "ai-activity-report" in res.headers["content-disposition"]
    assert TestClient(create_app(lambda: None)).get(f"/api/runs/{run_id}/activity-report").status_code == 200
    assert client.get("/api/runs/aaaaaaaaaaaa/activity-report").status_code == 404
