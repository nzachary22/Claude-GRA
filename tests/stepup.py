"""Signs that a report run had trouble, found without an answer key (live evaluations only).

Recorded by tests/live_eval.py to compare models. In the 2026-11-05 tests these signs
caught crashes, gaps and warnings but missed most wrong figures, so the app does not
use them to switch models.
"""

from __future__ import annotations

MANY_REFUSALS = 6
_TROUBLE_WORDS = ("incomplete", "not included", "could not", "unable", "omitted", "missing", "truncated")


def trouble(session) -> list[str]:
    reasons: list[str] = []
    if session.status != "done":
        reasons.append(f"The run ended as {session.status}" + (f": {session.error}" if session.error else "."))
    if not session.tables:
        reasons.append("No tables were produced.")
    gaps = sum(1 for t in session.tables for r in t.rows for c in r.cells if c.gap)
    if gaps:
        reasons.append(f"{gaps} table cells were left as gaps.")
    for t in session.tables:
        for w in t.warnings:
            reasons.append(f"Table check: {w}")
        reasons.extend(_total_mismatches(t))
    for n in session.notes.values():
        if n.kind == "gap" or (n.kind == "warning" and any(w in n.text.lower() for w in _TROUBLE_WORDS)):
            reasons.append(f"AI note ({n.kind}): {n.text[:200]}")
    refused = sum(1 for e in session.events if e.kind == "refused")
    if refused >= MANY_REFUSALS:
        reasons.append(f"The privacy check refused {refused} queries.")
    return reasons


def _total_mismatches(table) -> list[str]:
    """A total row from its own query that is larger than the sum of the detail rows above it.

    A smaller total is normal (an unduplicated count of people who fall in several rows);
    a larger one means some records appear in no row.
    """
    out = []
    details = []
    for r in table.rows:
        if r.kind == "detail":
            details.append(r)
        elif r.kind == "total" and details:
            for ci, cell in enumerate(r.cells):
                if not cell.is_number or (cell.source or "").startswith("sum"):
                    continue
                parts = [d.cells[ci] for d in details if ci < len(d.cells)]
                if parts and all(p.is_number for p in parts):
                    s = sum(p.value for p in parts)
                    if cell.value > s + 1e-9:
                        header = table.headers[ci] if ci < len(table.headers) else f"column {ci + 1}"
                        out.append(f"{table.title}: '{r.label}' ({header}) is {cell.value}, but its rows add to only {s}.")
    return out
