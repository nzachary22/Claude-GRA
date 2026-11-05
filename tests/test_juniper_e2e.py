"""End-to-end check on the synthetic Juniper Ridge package (kept local, not in Git).

The scripted analyst stands in for the AI; the import, privacy gate, table
assembly and exports are the real application. Expected figures come from the
package's answer key (2025) and its own independent calculation (2024, 2026).
"""

import importlib.util
import io
import json
from pathlib import Path

import pytest

from gra.documents import to_blocks
from gra.engine import ReportSession
from gra.exports import to_docx, to_xlsx
from gra.gate import QueryGate
from gra.importer import import_files
from gra.schema import describe
from tests import juniper_script
from tests.scripted import ScriptedModel

ROOT = Path(__file__).resolve().parents[1] / "juniper-ridge-legal-aid-society"
pytestmark = pytest.mark.skipif(not ROOT.exists(), reason="Juniper Ridge package not present")


def run_year(tmp_path, year, via="direct"):
    data = ROOT / "data" / str(year)
    db = tmp_path / f"j{year}.db"
    catalog = import_files(db, sorted(data.glob("*.csv")))
    gate = QueryGate(db, catalog)
    start, end = f"{year}-01-01", f"{year}-12-31"
    steps = juniper_script.steps(start, end, data)
    if via == "openai":  # the same analyst, but every request and reply goes through the OpenAI translation
        from gra.llm_openai import OpenAIClient
        from tests.test_openai import FakeOpenAI

        fake = FakeOpenAI(steps)
        model = OpenAIClient("test", client=fake)
        model.seen_messages = fake.requests
    else:
        model = ScriptedModel(steps)
    blocks = to_blocks(ROOT / "report" / "juniper-ridge-grant-report-2025.pdf")
    session = ReportSession(gate, describe(db, catalog), blocks, (start, end), model)
    session.run()
    assert session.status == "done", session.error
    return session, model


def expected(year):
    if year == 2025:
        return json.loads((ROOT / "answer-key" / "2025_expected_tables.json").read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("jr_compute", ROOT / "generator" / "compute_tables.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.compute(ROOT / "data" / str(year), year)


def by_label(table):
    return {r.label: [c.value for c in r.cells] for r in table.rows if r.kind != "heading"}


@pytest.mark.parametrize("via", ["direct", "openai"])
@pytest.mark.parametrize("year", [2025, 2024, 2026])
def test_tables_match_independent_calculation(tmp_path, year, via):
    session, _ = run_year(tmp_path, year, via)
    exp = expected(year)
    t1, t2, t3 = session.tables
    pop = session.gate.log[0].rows[0]
    assert pop == [exp["qualifying_cases"], exp["unique_clients"]]

    rows1 = by_label(t1)
    for label, ages in exp["table1_race_by_age"].items():
        assert rows1[label][:4] == [ages[h] for _, h in juniper_script.AGE_COLS], label
    assert rows1["Total"][-1] == exp["unique_clients"]

    assert by_label(t2)["Number of Clients"][:6] == [exp["table2_gender"][h] for _, h in juniper_script.GENDER_COLS]

    _, outcomes = juniper_script.read_codes(ROOT / "data" / str(year))
    rows3 = by_label(t3)
    for label, values in rows3.items():
        code = label.split(" ")[0]
        if code in exp["table3_problem_by_outcome"]:
            want = exp["table3_problem_by_outcome"][code]
            assert values[:10] == [want.get(o["outcome_code"], 0) for o in outcomes], label
    assert rows3["Total"][-1] == exp["qualifying_cases"]


@pytest.mark.parametrize("via", ["direct", "openai"])
def test_nothing_identifying_reaches_the_model(tmp_path, via):
    session, model = run_year(tmp_path, 2025, via)
    sent = json.dumps(model.seen_messages, default=str)
    assert "JR25-CL-" not in sent and "JR25-CA-" not in sent and "JR25-DR-" not in sent
    import csv
    with open(ROOT / "data" / "2025" / "demographic_responses.csv", encoding="utf-8-sig") as f:
        births = {r["birth_date"] for r in csv.DictReader(f) if r["birth_date"]}
    assert not any(b in sent for b in list(births)[:300])


def test_exports_carry_the_same_numbers(tmp_path):
    session, _ = run_year(tmp_path, 2025)
    from docx import Document
    from openpyxl import load_workbook

    clients = expected(2025)["unique_clients"]
    notes = list(session.notes.values())
    doc = Document(io.BytesIO(to_docx(session.tables, notes, "Juniper Ridge", "2025")))
    assert doc.tables[1].rows[1].cells[-1].text == f"{clients:,}"
    wb = load_workbook(io.BytesIO(to_xlsx(session.tables, notes, "Juniper Ridge", "2025")))
    ws = wb.worksheets[0]
    values = [ws.cell(row=r, column=6).value for r in range(5, 16)]
    assert values[-1] == clients
