"""V.2.1: Past reports. Finished results are kept so they can be opened again; uploads and records never are."""

import io
import json
import time

from fastapi.testclient import TestClient

from gra import history
from gra.paths import app_data_dir
from gra.server import create_app
from tests.test_server import client, start, wait  # noqa: F401  (client is a fixture)


def test_a_finished_report_and_each_correction_are_kept(client, tiny_files, tmp_path):
    run_id = start(client, tiny_files, tmp_path)
    first = wait(client, run_id)
    past = client.get("/api/history").json()
    assert [h["id"] for h in past] == [f"{run_id}-0"]
    assert past[0]["tables"] == 1 and past[0]["period"] == ["2025-01-01", "2025-12-31"]
    assert past[0]["report_files"] == ["report.txt"]

    assert client.post(f"/api/runs/{run_id}/correct", json={"text": "Only North county"}).status_code == 200
    second = wait(client, run_id)
    past = client.get("/api/history").json()
    assert [h["id"] for h in past] == [f"{run_id}-1", f"{run_id}-0"]  # newest first
    assert past[0]["corrections"] == 1

    # The version before the correction still holds the original figures.
    old = client.get(f"/api/history/{run_id}-0").json()
    new = client.get(f"/api/history/{run_id}-1").json()
    assert old["tables"] == first["tables"] and new["tables"] == second["tables"]
    assert old["notes"] == first["notes"] and old["purposes"]
    # Only the latest version is the one still open in this session.
    assert new["live"] is True and old["live"] is False
    assert old["has_activity"] is True


def test_past_reports_hold_no_uploads_records_or_key(client, tiny_files, tmp_path):
    run_id = start(client, tiny_files, tmp_path)
    wait(client, run_id)
    saved = (app_data_dir() / "history" / f"{run_id}-0.json").read_text(encoding="utf-8")
    for private in ("Stone", "C-001", "1980-02-01", "Tenant reports mold", "K-0001", "placeholder-not-a-real-key"):
        assert private not in saved
    assert set(json.loads(saved)) >= {"tables", "notes", "report", "period"}
    assert "events" not in json.loads(saved)
    assert list((app_data_dir() / "history").iterdir()) == [app_data_dir() / "history" / f"{run_id}-0.json"]


def test_downloads_work_after_the_run_files_are_deleted_and_after_a_restart(client, tiny_files, tmp_path):
    from docx import Document
    from openpyxl import load_workbook

    run_id = start(client, tiny_files, tmp_path)
    r = wait(client, run_id)
    live_xlsx = client.get(f"/api/runs/{run_id}/export.xlsx").content
    client.delete(f"/api/runs/{run_id}")
    fresh = TestClient(create_app(lambda: None))  # the app started again: nothing in memory
    e = fresh.get(f"/api/history/{run_id}-0").json()
    assert e["live"] is False and e["has_activity"] is False and e["tables"] == r["tables"]

    word = fresh.get(f"/api/history/{run_id}-0/export.docx")
    excel = fresh.get(f"/api/history/{run_id}-0/export.xlsx")
    assert word.status_code == 200 and excel.status_code == 200
    assert "attachment" in word.headers["content-disposition"]
    text = "\n".join(p.text for p in Document(io.BytesIO(word.content)).paragraphs)
    assert "Clients by gender" in text

    def cells(data):
        return [[c.value for c in row] for ws in load_workbook(io.BytesIO(data)) for row in ws.iter_rows()]
    assert cells(excel.content) == cells(live_xlsx)  # the same figures as the live run's download
    assert fresh.get(f"/api/history/{run_id}-0/export.pdf").status_code == 404


def test_delete_one_and_delete_all(client, tiny_files, tmp_path):
    a = start(client, tiny_files, tmp_path)
    wait(client, a)
    b = start(client, tiny_files, tmp_path)
    wait(client, b)
    assert len(client.get("/api/history").json()) == 2
    assert client.delete(f"/api/history/{a}-0").json() == {"ok": True}
    assert [h["id"] for h in client.get("/api/history").json()] == [f"{b}-0"]
    assert client.get(f"/api/history/{a}-0").status_code == 404
    assert client.get(f"/api/runs/{a}").json()["status"] == "done"  # the run itself is untouched
    assert client.delete("/api/history").json() == {"ok": True, "deleted": 1}
    assert client.get("/api/history").json() == []


def test_bad_ids_are_refused(client):
    for bad in ("..%2Fsaved-choices", "abc", "zzzzzzzzzzzz-0", "0123456789ab-x"):
        assert client.get(f"/api/history/{bad}").status_code == 404
        assert client.delete(f"/api/history/{bad}").status_code in (404, 405)


def test_only_the_most_recent_results_are_kept(monkeypatch):
    monkeypatch.setattr(history, "KEEP", 3)
    snap = {"tables": [], "notes": [], "report": {}, "period": ["2025-01-01", "2025-12-31"], "corrections": []}
    ids = []
    for i in range(5):
        ids.append(history.save(f"{i:012x}", snap, report_files=["r.pdf"], data_files=["d.csv"], data_gaps=False))
        time.sleep(0.01)
    assert [h["id"] for h in history.list_all()] == ids[:1:-1]


def test_the_demo_is_not_kept(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = TestClient(create_app(lambda: None))
    c.app.state.api.demo_pause = 0
    run_id = c.post("/api/demo").json()["id"]
    assert wait(c, run_id)["status"] == "done"
    assert c.get("/api/history").json() == []
