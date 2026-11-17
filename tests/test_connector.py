"""The Claude app connector: the MCP handshake, privacy of what it returns, and a full report on the sample.

The chat's Claude is played by the recorded demo steps (gra/demo.py), the same tool calls the
app's no-key demo makes, so the connector must produce the same tables the app does.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from gra import demo
from gra.api import SAMPLE, SAMPLE_DATA, SAMPLE_PERIOD, SAMPLE_REPORT
from gra.connector import Connector, results_markdown
from gra.mcp_stdio import ToolServer

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GRA_DATA_DIR", str(tmp_path / "appdata"))


def replay(call):
    """Make the demo's tool calls through call(name, args); return the last reply."""
    start, end = SAMPLE_PERIOD
    reply = None
    for step in demo.steps(start, end, SAMPLE):
        for name, args in step:
            reply, err = call(name, json.loads(json.dumps(args)))
            assert not err, reply
    return reply


def test_sample_report_end_to_end(tmp_path):
    c = Connector(output_root=tmp_path / "out")
    text, err = c.call("start_sample_report", {})
    assert not err, text
    assert "=== HOW TO WORK ===" in text and "run_query" in text and "DATABASE DESCRIPTION" in text
    assert "JUNIPER RIDGE LEGAL AID SOCIETY" in text  # the report's text, with its tables' layout
    reply = replay(c.call)
    assert reply.startswith("Accepted.")
    saved = sorted(p.name for p in (tmp_path / "out").rglob("*") if p.is_file())
    assert saved == ["ai-activity-report-20261231.docx", "grant-report-tables-20261231.docx",
                     "grant-report-tables-20261231.xlsx"]
    md = results_markdown(c.run.session)
    for t in c.run.session.tables:
        assert f"### {t.title}" in md
        for r in t.rows:
            for cell in r.cells:
                if cell.display:
                    assert cell.display in md
    shown, err = c.call("show_results", {})
    assert not err and "grant-report-tables-20261231.xlsx" in shown


def test_matches_app_demo(tmp_path):
    """The connector and the app's recorded demo build identical tables from the same calls."""
    from gra.api import Api

    c = Connector(output_root=tmp_path / "out")
    c.call("start_sample_report", {})
    replay(c.call)
    ours = [t.to_dict() for t in c.run.session.tables]

    api = Api(spawn=lambda fn, *a: fn(*a), demo_pause=0)
    run = api.start_demo()
    theirs = [t.to_dict() for t in api.get_run(run["id"]).session.tables]
    strip = lambda ts: [{k: v for k, v in t.items() if k != "rows"} | {"rows": [[c["display"] for c in r["cells"]] for r in t["rows"]]} for t in ts]
    assert strip(ours) == strip(theirs)


def test_activity_record_names_the_claude_app(tmp_path):
    from gra.activity import render_text

    c = Connector(output_root=tmp_path / "out")
    c.call("start_sample_report", {})
    replay(c.call)
    s = c.run.session
    text = render_text(s.activity.events, [], s.gate.protected_value_count(), c.run.id)
    assert "Claude app with the user's own Claude login" in text
    assert "Purpose: Counting clients by gender" in text  # each query Claude asked for is recorded
    assert "Result for request" in text


def test_records_never_returned(tmp_path):
    c = Connector(output_root=tmp_path / "out")
    text, _ = c.call("start_sample_report", {})
    gate = c.run.session.gate
    assert not gate.contains_protected(text)
    reply, err = c.call("run_query", {"sql": "SELECT * FROM clients", "purpose": "Looking at clients"})
    assert err and reply.startswith("Refused")
    ident = next(col.name for t in gate.catalog.tables for col in t.columns if col.role == "identifier")
    table = next(t.name for t in gate.catalog.tables for col in t.columns if col.name == ident)
    reply, err = c.call("run_query", {"sql": f"SELECT {ident}, COUNT(*) AS n FROM {table} GROUP BY {ident}",
                                      "purpose": "Listing IDs"})
    assert err and reply.startswith("Refused")


def test_preview_and_find_files(tmp_path):
    c = Connector(output_root=tmp_path / "out")
    text, err = c.call("find_files", {"folder": str(SAMPLE)})
    assert not err and "clients.csv (data," in text and "Juniper Ridge 2025 annual report.pdf (report" in text
    text, err = c.call("preview_data", {"data_files": [str(SAMPLE / n) for n in SAMPLE_DATA]})
    assert not err and "Kept on this computer" in text
    assert not list((Path(os.environ["GRA_DATA_DIR"]) / "connector-runs").glob("preview-*"))  # preview leaves nothing


def test_only_allowed_folders_are_read(tmp_path):
    allowed, other = tmp_path / "allowed", tmp_path / "other"
    allowed.mkdir()
    other.mkdir()
    (other / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    c = Connector(read_roots=[allowed], output_root=tmp_path / "out")
    text, err = c.call("start_report", {"data_files": [str(other / "data.csv")], "period_start": "2026-01-01",
                                        "period_end": "2026-12-31"})
    assert err and "outside the folders GRA may read" in text
    text, err = c.call("find_files", {"folder": str(other)})
    assert err
    (allowed / ".hidden").mkdir()
    (allowed / ".hidden" / "x.csv").write_text("a\n1\n", encoding="utf-8")
    text, err = c.call("preview_data", {"data_files": [str(allowed / ".hidden" / "x.csv")]})
    assert err and "hidden" in text
    text, err = c.call("start_report", {"data_files": ["data.csv"], "period_start": "2026-01-01",
                                        "period_end": "2026-12-31"})
    assert err and "full path" in text


def test_report_with_client_names_is_refused(tmp_path):
    import csv

    clients = list(csv.DictReader((SAMPLE / "clients.csv").open(encoding="utf-8")))
    name_col = next(iter(clients[0]))  # the client ID column
    report = tmp_path / "report.txt"
    report.write_text(f"Table 1. Clients served\nSpecial thanks to {clients[0][name_col]}.\n" + "x" * 300, encoding="utf-8")
    c = Connector(output_root=tmp_path / "out")
    text, err = c.call("start_report", {"report_files": [str(report)], "data_files": [str(SAMPLE / n) for n in SAMPLE_DATA],
                                        "period_start": "2026-01-01", "period_end": "2026-12-31"})
    assert err and "contains client details" in text and "Nothing from it was shown to Claude" in text
    assert clients[0][name_col] not in text.split("(")[0]


def test_tools_need_a_started_report():
    c = Connector()
    text, err = c.call("run_query", {"sql": "SELECT 1", "purpose": "x"})
    assert err and "start_report" in text


def _rpc(proc, i, method, params=None):
    proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n").encode())
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def _session(cmd, env, cwd=None):
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd)
    try:
        init = _rpc(proc, 1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "test", "version": "1"}})
        assert init["result"]["serverInfo"]["name"] == "grant-reporting-assistant"
        assert init["result"]["protocolVersion"] == "2025-06-18"
        proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        tools = _rpc(proc, 2, "tools/list")["result"]["tools"]
        assert {t["name"] for t in tools} >= {"start_report", "run_query", "note_assumption", "submit_tables"}
        start = _rpc(proc, 3, "tools/call", {"name": "start_sample_report", "arguments": {}})
        assert not start["result"]["isError"], start
        i = [10]

        def call(name, args):
            i[0] += 1
            r = _rpc(proc, i[0], "tools/call", {"name": name, "arguments": args})["result"]
            return r["content"][0]["text"], r["isError"]

        reply = replay(call)
        assert reply.startswith("Accepted.")
        return reply
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
        proc.stderr.close()
        proc.stdout.close()


def test_stdio_server(tmp_path):
    env = {**os.environ, "GRA_DATA_DIR": str(tmp_path / "appdata"), "PYTHONPATH": str(ROOT)}
    reply = _session([sys.executable, "-m", "gra.connector", "--output", str(tmp_path / "docs"), "--allow", str(tmp_path)],
                     env)
    assert str(tmp_path / "docs" / "Grant Reporting Assistant") in reply


def test_protocol_basics():
    server = ToolServer("x", "1", "hi", lambda: [], lambda n, a: ("ok", False))
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle({"jsonrpc": "2.0", "id": 1, "method": "ping"})["result"] == {}
    assert server.handle({"jsonrpc": "2.0", "id": 2, "method": "nope"})["error"]["code"] == -32601
    init = server.handle({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
    assert init["result"]["protocolVersion"] == "2025-11-25" and init["result"]["instructions"] == "hi"


@pytest.mark.skipif(os.environ.get("GRA_CONNECTOR_BUNDLE") != "1", reason="set GRA_CONNECTOR_BUNDLE=1 (needs internet)")
def test_built_bundle_runs_with_a_bare_python(tmp_path):
    """The .mcpb works with only Python installed: bundled libraries, plus Word support set up on first start."""
    sys.path.insert(0, str(ROOT / "tools"))
    from build_connector import build

    bundle = build(tmp_path / "dist")
    folder = tmp_path / "installed"
    with zipfile.ZipFile(bundle) as z:
        z.extractall(folder)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["server"]["entry_point"] == "server/main.py" and (folder / manifest["icon"]).exists()
    bare = tmp_path / "bare"
    subprocess.run([sys.executable, "-m", "venv", str(bare)], check=True)
    python = bare / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    home = tmp_path / "home"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    env.update(GRA_DATA_DIR=str(tmp_path / "appdata"), HOME=str(home), USERPROFILE=str(home),
               LOCALAPPDATA=str(home / "AppData" / "Local"), PYTHONUTF8="1")
    reply = _session([str(python), str(folder / "server" / "main.py"), "--output", str(tmp_path / "docs"),
                      "--allow", str(tmp_path)], env, cwd=str(tmp_path))
    assert (tmp_path / "docs" / "Grant Reporting Assistant").exists() and "grant-report-tables" in reply
    if os.name != "nt":  # the Mac starter finds a Python 3.11+ by itself and starts the connector
        proc = subprocess.Popen(["/bin/sh", str(folder / "server" / "start.sh"), "--output", str(tmp_path)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        try:
            init = _rpc(proc, 1, "initialize", {"protocolVersion": "2025-06-18"})
            assert init["result"]["serverInfo"]["name"] == "grant-reporting-assistant"
        finally:
            proc.stdin.close()
            proc.wait(timeout=30)
            proc.stdout.close()


def test_too_old_python_explains_itself():
    """With a Python older than 3.11 the connector still answers, with one tool that says what to install."""
    script = ("import sys, runpy; sys.version_info = (3, 9, 0, 'final', 0); sys.argv = ['main.py']; "
              f"runpy.run_path({str(ROOT / 'connector' / 'server' / 'main.py')!r}, run_name='__main__')")
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "setup_problem", "arguments": {}}}]
    out = subprocess.run([sys.executable, "-c", script], input="".join(json.dumps(m) + "\n" for m in msgs).encode(),
                         capture_output=True, timeout=60).stdout.decode().splitlines()
    assert json.loads(out[0])["result"]["tools"][0]["name"] == "setup_problem"
    reply = json.loads(out[1])["result"]
    assert reply["isError"] and "needs Python 3.11 or newer" in reply["content"][0]["text"]


def _xlsx(path, header, rows):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def test_client_records_given_as_the_report_are_refused(tmp_path):
    """A data export passed as the report is refused even when its IDs are not in the imported data."""
    records = _xlsx(tmp_path / "other export.xlsx", ["Client ID", "Client Name", "County", "Notes"],
                    [[f"ZZ-{i:05d}", f"Person {i} Example", "North" if i % 2 else "South", f"Called on day {i} about rent"]
                     for i in range(250)])
    birthdays = _xlsx(tmp_path / "small.xlsx", ["Client", "Date of Birth"], [[f"P{i}", f"1980-01-{i + 1:02d}"] for i in range(9)])
    report = _xlsx(tmp_path / "past report.xlsx", ["Legal problem", "Closed cases", "Percent"],
                   [[f"{i:02d}.01 Problem area {i}", i * 3, i / 2] for i in range(1, 40)])
    data = [str(SAMPLE / n) for n in SAMPLE_DATA]
    c = Connector(output_root=tmp_path / "out")
    for bad in (records, birthdays):
        text, err = c.call("start_report", {"report_files": [str(bad)], "data_files": data,
                                            "period_start": "2026-01-01", "period_end": "2026-12-31"})
        assert err and "looks like client records" in text and "Example" not in text, text
    (tmp_path / "x.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    text, err = c.call("start_report", {"report_files": [str(tmp_path / "x.csv")], "data_files": data,
                                        "period_start": "2026-01-01", "period_end": "2026-12-31"})
    assert err and "not a report file" in text
    text, err = c.call("start_report", {"report_files": [str(report)], "data_files": data,
                                        "period_start": "2026-01-01", "period_end": "2026-12-31"})
    assert not err and "Problem area 7" in text
