"""Report tables: the AI supplies layout and bindings, the app supplies every number.

A table spec (from the AI's submit_tables call) looks like:

    {"id": "t1", "title": "Table 1 ...", "note": "optional caption",
     "columns": [{"header": "Under 18"}, ..., {"header": "Total", "sum_of_columns": [0, 1, 2, 3]}],
     "rows": [
        {"label": "Consumer & finance", "kind": "heading"},
        {"label": "01.01 Debt collection", "kind": "detail", "indent": 1,
         "cells": [{"query": "Q4", "match": {"code": "01.01", "outcome": "advice"}, "column": "n",
                    "missing_means_zero": true}, ...]},
        {"label": "Subtotal", "kind": "subtotal", "sum_of_rows": [1, 2]},
        {"label": "Total", "kind": "total", "sum_of_rows": [...]}]}

Cell forms: a query binding (above); {"gap": "why the figure is unavailable"};
{"text": "..."} for non-numeric labels; or null where a column or row sum fills it.
Literal numbers are rejected so that no figure can bypass the local data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ROW_KINDS = {"heading", "detail", "subtotal", "total"}


class TableSpecError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class Cell:
    value: float | int | str | None = None
    gap: str | None = None  # reason the value is unavailable
    source: str | None = None  # e.g. "Q4" or "sum of rows 1-3"

    @property
    def is_number(self) -> bool:
        return isinstance(self.value, (int, float)) and not isinstance(self.value, bool) and self.gap is None


@dataclass
class Row:
    label: str
    kind: str
    indent: int
    cells: list[Cell]


@dataclass
class Table:
    id: str
    title: str
    note: str
    headers: list[str]
    rows: list[Row]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "note": self.note, "headers": self.headers,
            "warnings": self.warnings,
            "rows": [{"label": r.label, "kind": r.kind, "indent": r.indent,
                      "cells": [{"value": c.value, "gap": c.gap, "source": c.source} for c in r.cells]}
                     for r in self.rows],
        }


def resolve_tables(specs: list[dict], results: dict[str, "object"]) -> list[Table]:
    """Resolve every spec against query results (QueryResult by id). Raises TableSpecError."""
    if not isinstance(specs, list) or not specs:
        raise TableSpecError(["submit_tables needs a non-empty list of tables."])
    problems: list[str] = []
    tables: list[Table] = []
    seen_ids: set[str] = set()
    for i, spec in enumerate(specs):
        try:
            t = _resolve_table(spec, results, i)
            if t.id in seen_ids:
                raise TableSpecError([f"duplicate table id {t.id}"])
            seen_ids.add(t.id)
            tables.append(t)
        except TableSpecError as e:
            label = spec.get("id", f"#{i + 1}") if isinstance(spec, dict) else f"#{i + 1}"
            problems.extend(f"table {label}: {p}" for p in e.problems)
    if problems:
        raise TableSpecError(problems)
    return tables


def _resolve_table(spec: dict, results, index: int) -> Table:
    if not isinstance(spec, dict):
        raise TableSpecError(["each table must be an object"])
    problems: list[str] = []
    columns = spec.get("columns") or []
    rows_spec = spec.get("rows") or []
    if not columns:
        problems.append("columns are required")
    if not rows_spec:
        problems.append("rows are required")
    if problems:
        raise TableSpecError(problems)
    ncol = len(columns)
    headers = [str(c.get("header", "")) if isinstance(c, dict) else str(c) for c in columns]

    rows: list[Row] = []
    for r_i, rs in enumerate(rows_spec):
        kind = rs.get("kind", "detail")
        if kind not in ROW_KINDS:
            problems.append(f"row {r_i}: unknown kind {kind!r}")
            kind = "detail"
        cells_spec = rs.get("cells")
        cells: list[Cell] = []
        if kind == "heading" or rs.get("sum_of_rows") is not None:
            cells = [Cell() for _ in range(ncol)]
        else:
            if not isinstance(cells_spec, list) or len(cells_spec) != ncol:
                problems.append(f"row {r_i} ({rs.get('label')}): needs exactly {ncol} cells")
                cells = [Cell() for _ in range(ncol)]
            else:
                for c_i, cs in enumerate(cells_spec):
                    try:
                        cells.append(_resolve_cell(cs, results, columns[c_i]))
                    except TableSpecError as e:
                        problems.extend(f"row {r_i} col {c_i}: {p}" for p in e.problems)
                        cells.append(Cell(gap="unresolved"))
        rows.append(Row(str(rs.get("label", "")), kind, int(rs.get("indent", 0) or 0), cells))

    # Row sums (subtotals, totals) in row order, so totals may sum subtotals.
    for r_i, rs in enumerate(rows_spec):
        idx = rs.get("sum_of_rows")
        if idx is None:
            continue
        if not isinstance(idx, list) or not all(isinstance(j, int) and 0 <= j < len(rows) and j != r_i for j in idx):
            problems.append(f"row {r_i}: sum_of_rows must list other row indexes")
            continue
        for c_i in range(ncol):
            if isinstance(columns[c_i], dict) and columns[c_i].get("sum_of_columns") is not None:
                continue
            rows[r_i].cells[c_i] = _sum([rows[j].cells[c_i] for j in idx], f"sum of rows {_span(idx)}")

    # Column sums (e.g. a Total column) for every row.
    for c_i, col in enumerate(columns):
        if not isinstance(col, dict) or col.get("sum_of_columns") is None:
            continue
        idx = col["sum_of_columns"]
        if not isinstance(idx, list) or not all(isinstance(j, int) and 0 <= j < ncol and j != c_i for j in idx):
            problems.append(f"column {c_i}: sum_of_columns must list other column indexes")
            continue
        for row in rows:
            if row.kind == "heading":
                continue
            row.cells[c_i] = _sum([row.cells[j] for j in idx], f"sum of columns {_span(idx)}")

    if problems:
        raise TableSpecError(problems)
    return Table(id=str(spec.get("id") or f"t{index + 1}"), title=str(spec.get("title", "")),
                 note=str(spec.get("note", "") or ""), headers=headers, rows=rows)


def _resolve_cell(cs, results, column) -> Cell:
    if cs is None:
        if isinstance(column, dict) and column.get("sum_of_columns") is not None:
            return Cell()
        raise TableSpecError(["cell is empty; bind it to a query, a gap or text"])
    if not isinstance(cs, dict):
        raise TableSpecError(["literal values are not allowed; bind every number to a query result"])
    if "gap" in cs:
        return Cell(gap=str(cs["gap"]) or "Not available")
    if "text" in cs:
        if isinstance(cs["text"], (int, float)) or _looks_numeric(cs["text"]):
            raise TableSpecError(["numbers cannot be entered as text; bind them to a query result"])
        return Cell(value=str(cs["text"]))
    if "query" not in cs:
        raise TableSpecError(["cell must have query, gap or text"])
    qid = cs["query"]
    result = results.get(qid)
    if result is None:
        raise TableSpecError([f"unknown query id {qid}"])
    col_name = cs.get("column")
    if col_name not in result.columns:
        if col_name is None and len(result.columns) == 1:
            col_name = result.columns[0]
        else:
            raise TableSpecError([f"{qid} has no column {col_name!r} (columns: {result.columns})"])
    ci = result.columns.index(col_name)
    match = cs.get("match") or {}
    for k in match:
        if k not in result.columns:
            raise TableSpecError([f"{qid} has no column {k!r} to match on"])
    hits = [row for row in result.rows
            if all(_same(row[result.columns.index(k)], v) for k, v in match.items())]
    if len(hits) > 1:
        raise TableSpecError([f"{qid} match {match} hits {len(hits)} rows; make it unique"])
    if not hits:
        if cs.get("missing_means_zero"):
            return Cell(value=0, source=qid)
        raise TableSpecError([f"{qid} has no row matching {match}; if no row means a count of zero, "
                              "set missing_means_zero, otherwise mark the cell as a gap"])
    value = hits[0][ci]
    if value is None:
        if cs.get("missing_means_zero"):
            return Cell(value=0, source=qid)
        return Cell(gap="No value in the data", source=qid)
    if isinstance(value, float) and value.is_integer() and cs.get("integer", True):
        value = int(value)
    if isinstance(value, float) and cs.get("decimals") is not None:
        value = round(value, int(cs["decimals"]))
    return Cell(value=value, source=qid)


def _sum(cells: list[Cell], source: str) -> Cell:
    gaps = [c for c in cells if c.gap]
    if gaps:
        return Cell(gap="Includes unavailable figures", source=source)
    nums = [c.value for c in cells if c.is_number]
    if len(nums) != len(cells):
        return Cell(gap="Cannot total non-numeric cells", source=source)
    total = sum(nums)
    if isinstance(total, float) and total.is_integer():
        total = int(total)
    return Cell(value=total, source=source)


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return str(a) == str(b)


def _looks_numeric(s) -> bool:
    try:
        float(str(s).replace(",", ""))
        return True
    except ValueError:
        return False


def _span(idx: list[int]) -> str:
    return ", ".join(str(i) for i in idx) if len(idx) <= 6 else f"{idx[0]}-{idx[-1]} ({len(idx)} rows)"
