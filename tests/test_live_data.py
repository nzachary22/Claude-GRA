"""Protections added before running on real client exports.

Real case-management exports differ from the synthetic packages: CamelCase
headers, a bare "Client" column that repeats across cases, small exports for a
single grant, and historical reports that may name clients. Wholly synthetic data.
"""

import pytest
from fastapi.testclient import TestClient

from gra.gate import QueryGate, QueryRefused
from gra.importer import import_files, snake
from gra.schema import describe
from gra.server import create_app
from tests.conftest import write_csv
from tests.test_server import client, start, wait  # noqa: F401  (fixture reuse)


def roles(catalog, table):
    return {c.name: c.role for c in catalog.table(table).columns}


def test_camel_case_headers_are_split():
    assert snake("DateOfBirth") == "date_of_birth"
    assert snake("ClientID") == "client_id"
    assert snake("SSNLast4") == "ssn_last4"
    assert snake("Case Number") == "case_number"


def test_real_export_layouts_protect_people(tmp_path):
    names = [f"Person{i} Example" for i in range(40)]
    export = write_csv(tmp_path / "export.csv",
        ["CaseNumber", "ClientID", "ClientName", "DateOfBirth", "Client", "Adverse Party", "Case #", "Gender"],
        [[f"26-{i:04d}", 5000 + i, names[i % 40], f"19{50 + i % 40}-03-0{1 + i % 9}", names[i % 40],
          f"Landlord {i % 30} LLC", f"{9000 + i}", "FMX"[i % 3]] for i in range(300)])
    db = tmp_path / "t.db"
    catalog = import_files(db, [export])
    r = roles(catalog, "export")
    assert r["case_number"] == r["client_id"] == r["client_name"] == "identifier"
    assert r["client"] == "identifier"  # repeats across cases, so it looked like a category before
    assert r["adverse_party"] == "identifier"
    assert r["case"] == "identifier"
    assert r["date_of_birth"] == "sensitive"
    assert r["gender"] == "category"
    text = describe(db, catalog)
    assert "Person1 Example" not in text and "Landlord 3 LLC" not in text


def test_small_export_is_not_treated_as_a_code_list(tmp_path):
    export = write_csv(tmp_path / "grant_export.csv", ["Matter", "Problem", "Opened"],
                       [[f"26-{i:04d}", "01" if i % 2 else "02", "2026-03-03"] for i in range(50)])
    db = tmp_path / "t.db"
    catalog = import_files(db, [export])
    t = catalog.table("grant_export")
    assert not t.reference
    assert roles(catalog, "grant_export")["matter"] == "identifier"
    assert "26-0007" not in describe(db, catalog)


def test_code_list_without_code_in_its_name_is_still_shared(tmp_path):
    f = write_csv(tmp_path / "outcomes.csv", ["outcome", "label", "sort_order"],
                  [["A", "Advice", "1"], ["B", "Brief service", "2"], ["X", "Extended", "3"]])
    catalog = import_files(tmp_path / "t.db", [f])
    assert catalog.table("outcomes").reference


@pytest.fixture
def people_gate(tmp_path):
    f = write_csv(tmp_path / "intake.csv", ["Household", "Birth Date", "Gender", "Town"],
                  [[f"House {i % 150} family", f"19{50 + i % 40}-0{1 + i % 9}-1{i % 10}", "FMX"[i % 3], f"T{i % 5}"]
                   for i in range(400)])
    db = tmp_path / "t.db"
    catalog = import_files(db, [f])
    return QueryGate(db, catalog)


@pytest.mark.parametrize("sql", [
    "SELECT upper(household) AS h, COUNT(*) AS n FROM intake WHERE town = 'T1' GROUP BY 1",
    "SELECT trim(household) || '' AS h, COUNT(*) AS n FROM intake WHERE town = 'T1' GROUP BY 1",
    "SELECT date(birth_date) AS b, COUNT(*) AS n FROM intake WHERE town = 'T1' GROUP BY 1",
])
def test_disguised_personal_values_are_refused(people_gate, sql):
    with pytest.raises(QueryRefused):
        people_gate.run(sql)


def test_bucketed_personal_values_still_work(people_gate):
    r = people_gate.run("SELECT strftime('%Y', birth_date) AS birth_year, COUNT(*) AS n FROM intake GROUP BY 1")
    assert sum(row[1] for row in r.rows) == 400


# ---------------------------------------------------------------- server


def test_other_hosts_are_refused(client):  # noqa: F811
    assert client.get("/api/status", headers={"host": "evil.example"}).status_code == 400
    assert client.get("/api/status").status_code == 200


def test_preview_shows_roles_and_sends_nothing(tmp_path, monkeypatch, tiny_files):
    monkeypatch.setenv("GRA_DATA_DIR", str(tmp_path / "appdata"))

    def no_ai():
        raise AssertionError("preview must not contact the AI")
    c = TestClient(create_app(no_ai))
    files = [("data_files", (f.name, f.read_bytes(), "text/csv")) for f in tiny_files]
    res = c.post("/api/preview", files=files)
    assert res.status_code == 200, res.text
    body = res.json()
    clients = next(t for t in body["tables"] if t["table"] == "clients")
    assert {c["header"]: c["role"] for c in clients["columns"]}["Last Name"] == "identifier"
    assert "Stone" not in body["schema"]
    assert not any((tmp_path / "appdata" / "preview").iterdir())  # nothing kept


def test_report_naming_a_client_is_not_sent(client, tiny_files, tmp_path):  # noqa: F811
    report = tmp_path / "report.txt"
    report.write_text("Success story: client C-002 recovered unpaid wages.")
    files = [("report_files", ("report.txt", report.read_bytes(), "text/plain"))]
    files += [("data_files", (f.name, f.read_bytes(), "text/csv")) for f in tiny_files]
    run_id = client.post("/api/runs", files=files,
                         data={"period_start": "2025-01-01", "period_end": "2025-12-31"}).json()["id"]
    r = wait(client, run_id)
    assert r["status"] == "failed"
    assert "C-002" in r["error"] and "Nothing was sent" in r["error"]
    assert client.get(f"/api/runs/{run_id}/activity-report").status_code == 404  # no AI activity at all


def test_correction_naming_a_client_is_refused(client, tiny_files, tmp_path):  # noqa: F811
    run_id = start(client, tiny_files, tmp_path)
    assert wait(client, run_id)["status"] == "done"
    res = client.post(f"/api/runs/{run_id}/correct", json={"text": "Leave out C-002, Stone"})
    assert res.status_code == 400


def test_delete_run_removes_its_files(client, tiny_files, tmp_path):  # noqa: F811
    run_id = start(client, tiny_files, tmp_path)
    assert wait(client, run_id)["status"] == "done"
    folder = tmp_path / "appdata" / "runs" / run_id
    assert (folder / "data.db").exists()
    assert client.delete(f"/api/runs/{run_id}").status_code == 200
    assert not folder.exists()
    assert client.get(f"/api/runs/{run_id}").status_code == 404
    second = start(client, tiny_files, tmp_path)
    wait(client, second)
    assert client.delete("/api/runs").json()["deleted"] == 1
    assert not any((tmp_path / "appdata" / "runs").iterdir())


def test_common_change_log_values_stay_usable(tmp_path):
    rows = [[f"K{i}", "status", "Closed" if i % 2 else "Reopened"] for i in range(300)]
    rows += [[f"K{i}", "closed_date", f"2026-{1 + i % 12:02d}-{1 + (i // 12) % 28:02d}"] for i in range(300)]
    f = write_csv(tmp_path / "case_changes.csv", ["case_id", "field", "new_value"], rows)
    db = tmp_path / "t.db"
    catalog = import_files(db, [f])
    assert roles(catalog, "case_changes")["new_value"] == "text"
    gate = QueryGate(db, catalog)
    r = gate.run("SELECT CASE WHEN field = 'status' THEN new_value ELSE 'date change' END AS change, "
                 "COUNT(*) AS n FROM case_changes GROUP BY 1")
    assert {row[0] for row in r.rows} == {"Closed", "Reopened", "date change"}
