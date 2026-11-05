import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from gra.server import create_app
from tests.scripted import ScriptedModel
from tests.test_engine import GENDER_SQL, gender_table


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GRA_DATA_DIR", str(tmp_path / "appdata"))

    def factory():
        return ScriptedModel([
            [("run_query", {"sql": GENDER_SQL, "purpose": "Counting clients by gender"})],
            [("submit_tables", {"tables": [gender_table()]})],
            [("run_query", {"sql": GENDER_SQL.replace("GROUP BY", "WHERE county = 'North' GROUP BY"),
                            "purpose": "Recounting North clients"})],
            [("submit_tables", {"tables": [gender_table("Q2")]})],
        ])
    return TestClient(create_app(factory))


def wait(client, run_id, until=("done", "failed", "cancelled")):
    for _ in range(200):
        r = client.get(f"/api/runs/{run_id}").json()
        if r["status"] in until:
            return r
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def start(client, tiny_files, tmp_path):
    report = tmp_path / "report.txt"
    report.write_text("Table 1: clients by gender")
    files = [("report_files", ("report.txt", report.read_bytes(), "text/plain"))]
    files += [("data_files", (f.name, f.read_bytes(), "text/csv")) for f in tiny_files]
    res = client.post("/api/runs", files=files, data={"period_start": "2025-01-01", "period_end": "2025-12-31"})
    assert res.status_code == 200, res.text
    return res.json()["id"]


def test_upload_prepare_review_export(client, tiny_files, tmp_path):
    run_id = start(client, tiny_files, tmp_path)
    r = wait(client, run_id)
    assert r["status"] == "done"
    assert r["tables"][0]["title"] == "Clients by gender"
    assert r["report"] == {}
    assert r["tables"][0]["rows"][0]["cells"][0]["display"] != ""
    assert any("protected" in e["message"] for e in r["events"])
    for fmt in ("docx", "xlsx"):
        res = client.get(f"/api/runs/{run_id}/export.{fmt}")
        assert res.status_code == 200 and len(res.content) > 1000
    d = client.get(f"/api/runs/{run_id}/details").json()
    assert d["queries"][0]["purpose"] == "Counting clients by gender"
    assert "Stone" not in d["schema"]


def test_correction_recalculates(client, tiny_files, tmp_path):
    run_id = start(client, tiny_files, tmp_path)
    first = wait(client, run_id)["tables"][0]["rows"][-1]["cells"][0]["value"]
    assert client.post(f"/api/runs/{run_id}/correct", json={"text": "Only North county"}).status_code == 200
    r = wait(client, run_id)
    assert r["status"] == "done"
    assert r["tables"][0]["rows"][-1]["cells"][0]["value"] < first


def test_upload_files_stay_outside_repo(client, tiny_files, tmp_path):
    run_id = start(client, tiny_files, tmp_path)
    wait(client, run_id)
    assert (tmp_path / "appdata" / "runs" / run_id / "data.db").exists()


def test_rejects_bad_input(client, tiny_files, tmp_path):
    files = [("report_files", ("x.exe", b"x", "application/octet-stream")),
             ("data_files", (tiny_files[0].name, tiny_files[0].read_bytes(), "text/csv"))]
    assert client.post("/api/runs", files=files, data={"period_start": "2025-01-01", "period_end": "2025-12-31"}).status_code == 400
    assert client.get("/api/runs/nope").status_code == 404


def test_index_served(client):
    res = client.get("/")
    assert res.status_code == 200 and "Grant Reporting Assistant" in res.text


def test_cancel_mid_run(tmp_path, monkeypatch, tiny_files):
    monkeypatch.setenv("GRA_DATA_DIR", str(tmp_path / "appdata"))

    class Slow(ScriptedModel):
        def respond(self, *a, **k):
            time.sleep(0.3)
            return super().respond(*a, **k)

    c = TestClient(create_app(lambda: Slow([[("run_query", {"sql": GENDER_SQL, "purpose": "Counting"})]] * 20)))
    run_id = start(c, tiny_files, tmp_path)
    time.sleep(0.5)
    assert c.post(f"/api/runs/{run_id}/cancel").status_code == 200
    r = wait(c, run_id)
    assert r["status"] == "cancelled"
    assert any(e["kind"] == "cancelled" for e in r["events"])


def test_page_status_does_not_load_the_slow_ai_library():
    code = "import sys, gra.server, gra.llm; gra.llm.get_api_key(); sys.exit('anthropic' in sys.modules or 'openai' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], timeout=120).returncode == 0


def test_out_of_credit_is_explained_in_plain_words():
    from gra.llm import status_message

    assert "out of API credit" in status_message(400, "Your credit balance is too low to access the Anthropic API.")
    assert status_message(500, "server trouble") == "The AI service returned an error (500)."


def test_choosing_the_ai_service(tmp_path, monkeypatch):
    import keyring

    from gra import llm

    monkeypatch.setenv("GRA_DATA_DIR", str(tmp_path / "appdata"))
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm, "_windows_user_env", lambda name: None)
    store = {}
    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get(u))
    monkeypatch.setattr(keyring, "set_password", lambda s, u, k: store.__setitem__(u, k))
    checked = []
    monkeypatch.setattr(llm, "check_api_key", lambda key, provider="anthropic": checked.append(provider))
    c = TestClient(create_app())

    s = c.get("/api/status").json()
    assert s["provider"] == "anthropic" and not s["has_key"] and s["keys"] == {"anthropic": False, "openai": False}
    assert c.post("/api/provider", json={"provider": "openai"}).status_code == 400  # no key yet
    assert c.post("/api/key", json={"key": "sk-proj-abcdefghijklmnopqrstuvwxyz", "provider": "openai"}).status_code == 200
    assert checked == ["openai"] and store == {"openai_api_key": "sk-proj-abcdefghijklmnopqrstuvwxyz"}
    s = c.get("/api/status").json()
    assert s["provider"] == "openai" and s["has_key"]
    assert c.post("/api/key", json={"key": "sk-ant-abcdefghijklmnopqrstuvwxyz"}).status_code == 200  # older page: Anthropic
    assert c.get("/api/status").json()["provider"] == "anthropic"
    assert c.post("/api/provider", json={"provider": "openai"}).status_code == 200
    assert c.get("/api/status").json()["provider"] == "openai"
    assert c.post("/api/provider", json={"provider": "gemini"}).status_code == 400
    assert c.post("/api/key", json={"key": "sk-abcdefghijklmnopqrstuvwxyz", "provider": "x"}).status_code == 400


def test_settings_open_once_for_people_upgrading(tmp_path, monkeypatch):
    monkeypatch.setenv("GRA_DATA_DIR", str(tmp_path / "appdata"))
    c = TestClient(create_app())
    assert c.get("/api/status").json()["show_settings"] is True
    assert c.post("/api/settings-seen").status_code == 200
    assert c.get("/api/status").json()["show_settings"] is False
    from gra import llm

    llm.set_provider("openai")  # other settings are kept
    assert llm.settings_seen() and llm.saved_provider() == "openai"


def test_period_changed_by_a_correction_reaches_exports(tiny_files, tmp_path, monkeypatch):
    import io

    import docx

    monkeypatch.setenv("GRA_DATA_DIR", str(tmp_path / "appdata"))

    def factory():
        return ScriptedModel([
            [("run_query", {"sql": GENDER_SQL, "purpose": "Counting clients by gender"})],
            [("submit_tables", {"tables": [gender_table()]})],
            [("submit_tables", {"report": {"period": {"start": "2025-01-01", "end": "2025-06-30"}},
                                "tables": [gender_table()]})],
        ])
    client = TestClient(create_app(factory))
    run_id = start(client, tiny_files, tmp_path)
    assert wait(client, run_id)["period"] == ["2025-01-01", "2025-12-31"]
    assert client.post(f"/api/runs/{run_id}/correct", json={"text": "First half only"}).status_code == 200
    r = wait(client, run_id)
    assert r["status"] == "done" and r["period"] == ["2025-01-01", "2025-06-30"]
    res = client.get(f"/api/runs/{run_id}/export.docx")
    assert 'grant-report-tables-20250630.docx' in res.headers["content-disposition"]
    text = "\n".join(p.text for p in docx.Document(io.BytesIO(res.content)).paragraphs)
    assert "Reporting period: 2025-01-01 to 2025-06-30." in text and "2025-12-31" not in text
    report = client.get(f"/api/runs/{run_id}/activity-report")
    head = "\n".join(p.text for p in docx.Document(io.BytesIO(report.content)).paragraphs[:3])
    assert "Reporting period 2025-01-01 to 2025-06-30 (changed by a correction from 2025-01-01 to 2025-12-31)" in head
    log = client.get(f"/api/runs/{run_id}/activity-log").text
    assert "Reporting period: 2025-01-01 to 2025-06-30" in log and "changed by a correction" in log
