"""Opt-in live evaluation: Claude rebuilds the Juniper Ridge tables and is scored.

    python -m tests.live_eval 2025        (uses the saved API key; costs API credit)

Claude chooses its own labels, so cells are matched to the expected figures by
normalized row label and column header. Results and a progress log are saved in
the local app-data folder, not the repository.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from gra.documents import to_blocks
from gra.engine import ReportSession
from gra.gate import QueryGate
from gra.importer import import_files
from gra.llm import ClaudeClient, get_api_key
from gra.paths import app_data_dir
from gra.schema import describe
from tests.test_juniper_e2e import ROOT, expected


def norm(s: str) -> str:
    s = str(s).lower().replace("–", "-").replace("—", "-")
    s = re.sub(r"^(table\s*\d+[a-z]?|0?[1-9]\s)[\s.:)-]*(?=[a-z])", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def find(tables, row_label, col_header, title_hint=None):
    want_r, want_c = norm(row_label), norm(col_header)
    for t in tables:
        if title_hint and title_hint not in norm(t.title):
            continue
        cols = [norm(h) for h in t.headers]
        if want_c not in cols:
            continue
        ci = cols.index(want_c)
        for r in t.rows:
            lab = norm(r.label)
            if lab == want_r or lab.startswith(want_r + " ") or lab.split(" ")[0] == want_r:
                return r.cells[ci].value if not r.cells[ci].gap else f"gap: {r.cells[ci].gap}"
    return "missing"


def score(session, exp, outcomes):
    checks = []
    for race, ages in exp["table1_race_by_age"].items():
        for age, v in ages.items():
            checks.append(("T1", race, age, v, find(session.tables, race, age)))
    for g, v in exp["table2_gender"].items():
        checks.append(("T2", "clients", g, v, find(session.tables, "number of clients", g, "gender")
                       if find(session.tables, "number of clients", g, "gender") != "missing"
                       else _gender_any(session.tables, g)))
    labels = {o["outcome_code"]: o["label"] for o in outcomes}
    for code, row in exp["table3_problem_by_outcome"].items():
        for oc, label in labels.items():
            checks.append(("T3", code, label, row.get(oc, 0), find(session.tables, code, label)))
    return checks


def _gender_any(tables, header):
    for t in tables:
        if "gender" in norm(t.title):
            cols = [norm(h) for h in t.headers]
            if norm(header) in cols:
                ci = cols.index(norm(header))
                for r in t.rows:
                    if r.kind != "heading" and not r.cells[ci].gap:
                        return r.cells[ci].value
            labels = [norm(r.label) for r in t.rows]
            if norm(header) in labels:
                r = t.rows[labels.index(norm(header))]
                return next((c.value for c in r.cells if c.is_number), "missing")
    return "missing"


def main(year: int):
    data = ROOT / "data" / str(year)
    out = app_data_dir() / "evals" / time.strftime(f"juniper-{year}-%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    db = out / "data.db"
    catalog = import_files(db, sorted(data.glob("*.csv")))
    gate = QueryGate(db, catalog)
    key = get_api_key()
    if not key:
        sys.exit("No API key found.")
    session = ReportSession(gate, describe(db, catalog), to_blocks(ROOT / "report" / "juniper-ridge-grant-report-2025.pdf"),
                            (f"{year}-01-01", f"{year}-12-31"), ClaudeClient(key))
    start = time.time()
    session.run()
    elapsed = time.time() - start
    from tests.juniper_script import read_codes
    _, outcomes = read_codes(data)
    checks = score(session, expected(year), outcomes) if session.tables else []
    ok = sum(1 for c in checks if c[3] == c[4])
    summary = {
        "year": year, "status": session.status, "error": session.error, "seconds": round(elapsed),
        "queries_run": session.queries_used, "refused": sum(1 for e in session.events if e.kind == "refused"),
        "tables": [t.title for t in session.tables], "cells_checked": len(checks), "cells_matching": ok,
        "mismatches": [c for c in checks if c[3] != c[4]][:80],
        "notes": [n.to_dict() for n in session.notes.values()],
        "events": [e.__dict__ for e in session.events],
    }
    (out / "result.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    (out / "tables.json").write_text(json.dumps([t.to_dict() for t in session.tables], indent=2, default=str), encoding="utf-8")
    (out / "queries.json").write_text(json.dumps([{"id": q.query_id, "purpose": q.purpose, "sql": q.sql, "columns": q.columns,
                                                   "rows": q.rows} for q in gate.log], indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("year", "status", "error", "seconds", "queries_run", "refused",
                                              "tables", "cells_checked", "cells_matching")}, indent=2))
    print("saved to", out)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 2025)
