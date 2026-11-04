"""The AI activity log must be a complete, honest record of what was sent."""

import json

import pytest
from fastapi.testclient import TestClient

from gra.activity import plain, read_events, render_text, sent_text
from gra.engine import ReportSession
from gra.gate import QueryGate
from gra.schema import describe
from gra.server import create_app
from tests.scripted import ScriptedModel
from tests.test_engine import GENDER_SQL, gender_table
from tests.test_server import start, wait

DOC = {"type": "document", "title": "report.pdf",
       "source": {"type": "base64", "media_type": "application/pdf", "data": "SGVsbG8="}}


def run_session(tiny_db, tmp_path, steps, blocks=None):
    db, catalog = tiny_db
    gate = QueryGate(db, catalog)
    model = ScriptedModel(steps)
    path = tmp_path / "activity.jsonl"
    session = ReportSession(gate, describe(db, catalog), blocks or [{"type": "text", "text": "Report text."}],
                            ("2025-01-01", "2025-12-31"), model, activity_path=path)
    session.run()
    return session, model, gate, read_events(path)


STEPS = [
    [("run_query", {"sql": "SELECT client_id FROM clients", "purpose": "A refused query"}),
     ("run_query", {"sql": GENDER_SQL, "purpose": "Counting clients"})],
    [("submit_tables", {"tables": [gender_table()]})],
]


def test_every_outgoing_message_is_recorded(tiny_db, tmp_path):
    session, model, _, events = run_session(tiny_db, tmp_path, STEPS)
    assert sum(e["kind"] == "request" for e in events) == model.calls
    final_request = model.seen_messages[-1]
    outgoing = [m for m in final_request if m["role"] == "user"]
    logged = [e for e in events if e["kind"] == "message"]
    assert [e["content"] for e in logged] == [[plain(b) for b in m["content"]] for m in outgoing]
    assert [e["content"] for e in events if e["kind"] == "response"] == \
        [m["content"] for m in final_request if m["role"] == "assistant"] + \
        [session.messages[-2]["content"]]
    assert events[1]["kind"] == "instructions" and events[2]["kind"] == "tools"


def test_log_is_written_before_the_request_goes_out(tiny_db, tmp_path):
    class Fails(ScriptedModel):
        def respond(self, *a, **k):
            raise RuntimeError("no connection")

    db, catalog = tiny_db
    path = tmp_path / "a.jsonl"
    session = ReportSession(QueryGate(db, catalog), describe(db, catalog), [{"type": "text", "text": "Report text."}],
                            ("2025-01-01", "2025-12-31"), Fails([]), activity_path=path)
    session.run()
    assert session.status == "failed"
    events = read_events(path)
    assert any(e["kind"] == "message" and "Report text." in sent_text(e) for e in events)
    assert events[-1]["kind"] == "request"


def test_corrections_are_recorded(tiny_db, tmp_path):
    steps = STEPS + [[("submit_tables", {"tables": [gender_table()]})]]
    session, _, _, _ = run_session(tiny_db, tmp_path, steps)
    session.correct("Please use the county office rule.")
    text = "\n".join(sent_text(e) for e in read_events(tmp_path / "activity.jsonl"))
    assert "Please use the county office rule." in text


def test_uploaded_files_are_listed_not_embedded(tiny_db, tmp_path):
    _, _, _, events = run_session(tiny_db, tmp_path, STEPS, blocks=[DOC])
    assert "SGVsbG8=" not in json.dumps(events)
    text = render_text(events, [], 5, "abc")
    assert "report.pdf" in text and "5 bytes" in text
    assert "185f8db32271fe25f561a6fc938b2e264306ec304eda518007d1764826381969" in text


def test_report_shows_refusals_sql_and_clean_check(tiny_db, tmp_path):
    _, _, gate, events = run_session(tiny_db, tmp_path, STEPS)
    hits = [e["seq"] for e in events if sent_text(e) and gate.contains_protected(sent_text(e))]
    assert hits == []
    text = render_text(events, hits, gate.protected_value_count(), "abc")
    assert "SELECT client_id FROM clients" in text and "Refused or failed result" in text
    assert "No stored identifier or note value was found" in text
    assert "including 1 refused or failed" in text
    for secret in ("C-001", "Stone", "Ava", "1980-02-01", "mold"):
        assert secret not in text


def test_check_catches_a_protected_value(tiny_db, tmp_path):
    db, catalog = tiny_db
    gate = QueryGate(db, catalog)
    assert gate.contains_protected("Result row for C-001 here")
    assert gate.contains_protected("Note: Tenant reports mold in the unit since spring.")
    assert not gate.contains_protected("Counts for North and South")
    text = render_text([{"seq": 1, "at": "", "direction": "local", "kind": "run_start"}], [7, 9], 3, "abc")
    assert "REVIEW NEEDED" in text and "7, 9" in text


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GRA_DATA_DIR", str(tmp_path / "appdata"))
    return TestClient(create_app(lambda: ScriptedModel(STEPS)))


def test_download_also_works_after_a_restart(client, tiny_files, tmp_path):
    run_id = start(client, tiny_files, tmp_path)
    wait(client, run_id)
    res = client.get(f"/api/runs/{run_id}/activity-log")
    assert res.status_code == 200 and "attachment" in res.headers["content-disposition"]
    assert "AI ACTIVITY LOG" in res.text and "Counting clients" in res.text
    fresh = TestClient(create_app(lambda: None))
    assert fresh.get(f"/api/runs/{run_id}/activity-log").text == res.text


def test_download_rejects_bad_ids(client):
    assert client.get("/api/runs/nope/activity-log").status_code == 404
    assert client.get("/api/runs/aaaaaaaaaaaa/activity-log").status_code == 404


def test_page_files_are_always_revalidated(client):
    for path in ("/", "/static/app.js", "/static/style.css"):
        assert client.get(path).headers["cache-control"] == "no-cache"
