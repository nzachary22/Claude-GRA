"""Opt-in live evaluation: the AI rebuilds the Juniper Ridge tables and is scored.

    python -m tests.live_eval 2025            (the service chosen in Settings; costs API credit)
    python -m tests.live_eval 2025 openai     (or anthropic, to pick the service)

The AI chooses its own labels, so cells are matched to the expected figures by
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
from gra.llm import make_client
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


# USD per million tokens: input, cache read, cache write, output (checked 2026-11-05).
PRICES = {
    "claude-sonnet-5-5": (2.00, 0.20, 2.50, 10.00),
    "gpt-6.1-sol": (2.00, 0.20, 0, 10.00),
}
FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")


def track_usage(model):
    """Wrap model.respond so every request's token counts are kept on model.usage_log."""
    model.usage_log = []
    respond = model.respond

    def wrapped(*args, **kwargs):
        response = respond(*args, **kwargs)
        u = getattr(response, "usage", None)
        u = u if isinstance(u, dict) else (u.model_dump() if hasattr(u, "model_dump") else {})
        model.usage_log.append({k: int(u.get(k) or 0) for k in FIELDS})
        return response

    model.respond = wrapped


def estimate_cost(model_id: str, usage_log: list) -> dict:
    totals = {k: sum(u[k] for u in usage_log) for k in FIELDS}
    p = PRICES.get(model_id)
    if p is None:
        return {**totals, "requests": len(usage_log), "usd": None}
    usd = (totals["input_tokens"] * p[0] + totals["cache_read_input_tokens"] * p[1]
           + totals["cache_creation_input_tokens"] * p[2] + totals["output_tokens"] * p[3]) / 1e6
    return {**totals, "requests": len(usage_log), "usd": round(usd, 4)}


def main(year: int, provider: str | None = None):
    data = ROOT / "data" / str(year)
    out = app_data_dir() / "evals" / time.strftime(f"juniper-{year}-%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    db = out / "data.db"
    catalog = import_files(db, sorted(data.glob("*.csv")))
    gate = QueryGate(db, catalog)
    try:
        model = make_client(provider)
    except RuntimeError as e:
        sys.exit(str(e))
    track_usage(model)
    session = ReportSession(gate, describe(db, catalog), to_blocks(ROOT / "report" / "juniper-ridge-grant-report-2025.pdf"),
                            (f"{year}-01-01", f"{year}-12-31"), model)
    start = time.time()
    session.run()
    elapsed = time.time() - start
    from tests.juniper_script import read_codes
    _, outcomes = read_codes(data)
    checks = score(session, expected(year), outcomes) if session.tables else []
    ok = sum(1 for c in checks if c[3] == c[4])
    summary = {
        "year": year, "provider": model.provider, "model": model.model, "status": session.status, "error": session.error, "seconds": round(elapsed),
        "queries_run": session.queries_used, "refused": sum(1 for e in session.events if e.kind == "refused"),
        "tables": [t.title for t in session.tables], "cells_checked": len(checks), "cells_matching": ok,
        "effort": model.effort,
        "cost": estimate_cost(model.model, model.usage_log),
        "mismatches": [c for c in checks if c[3] != c[4]][:80],
        "notes": [n.to_dict() for n in session.notes.values()],
        "events": [e.__dict__ for e in session.events],
    }
    (out / "result.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    (out / "tables.json").write_text(json.dumps([t.to_dict() for t in session.tables], indent=2, default=str), encoding="utf-8")
    (out / "queries.json").write_text(json.dumps([{"id": q.query_id, "purpose": q.purpose, "sql": q.sql, "columns": q.columns,
                                                   "rows": q.rows} for q in gate.log], indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("year", "provider", "model", "status", "error", "seconds", "queries_run", "refused",
                                              "tables", "cells_checked", "cells_matching", "effort", "cost")}, indent=2))
    print("saved to", out)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 2025, sys.argv[2] if len(sys.argv) > 2 else None)
