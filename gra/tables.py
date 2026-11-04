"""Report tables: the AI supplies layout and bindings, the app supplies every number.

A table spec (from the AI's submit_tables call) looks like:

    {"id": "t1", "title": "Table 1 ...", "subtitle": "Clients: {clients}", "note": "optional caption",
     "values": {"clients": {"query": "Q1", "column": "n"}},
     "row_header": "Age at intake",
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

Display: a cell, row or column may set "format" (number, percent or currency)
and "decimals"; the most specific wins. Percent values are on a 0-100 scale.
A figure inside a subtitle or note is a {placeholder} bound through "values".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FORMATS = {"number", "percent", "currency"}
DEFAULT_DECIMALS = {"number": 0, "percent": 1, "currency": 0}
_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")
ROW_KINDS = {"heading", "detail", "subtotal", "total"}
GAP_MARK = "n/a"


class TableSpecError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class Cell:
    value: float | int | str | None = None
    gap: str | None = None  # reason the value is unavailable
    source: str | None = None  # e.g. "Q4" or "sum of rows 1-3"
    format: str | None = None  # number | percent | currency; None means plain
    decimals: int | None = None

    @property
    def is_number(self) -> bool:
        return isinstance(self.value, (int, float)) and not isinstance(self.value, bool) and self.gap is None

    @property
    def display(self) -> str:
        """The cell as shown everywhere: on screen, in Copy, in Word."""
        if self.gap:
            return GAP_MARK
        if self.value is None:
            return ""
        if not self.is_number:
            return str(self.value)
        if self.format in FORMATS:
            d = DEFAULT_DECIMALS[self.format] if self.decimals is None else self.decimals
            text = f"{self.value:,.{d}f}"
            return {"percent": f"{text}%", "currency": f"${text}".replace("$-", "-$")}.get(self.format, text)
        if isinstance(self.value, int):
            return f"{self.value:,}"
        return f"{self.value:,.2f}".rstrip("0").rstrip(".")


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
    row_header: str = ""
    subtitle: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "subtitle": self.subtitle, "note": self.note,
            "row_header": self.row_header, "headers": self.headers, "warnings": self.warnings,
            "rows": [{"label": r.label, "kind": r.kind, "indent": r.indent,
                      "cells": [{"value": c.value, "gap": c.gap, "source": c.source, "format": c.format,
                                 "decimals": c.decimals, "display": c.display} for c in r.cells]}
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

    _apply_formats(rows, rows_spec, columns, problems)
    subtitle = _fill_text(spec.get("subtitle"), spec.get("values"), results, problems)
    note = _fill_text(spec.get("note"), spec.get("values"), results, problems)
    if problems:
        raise TableSpecError(problems)
    return Table(id=str(spec.get("id") or f"t{index + 1}"), title=str(spec.get("title", "")),
                 note=note, headers=headers, rows=rows,
                 row_header=str(spec.get("row_header") or ""), subtitle=subtitle)


def _apply_formats(rows, rows_spec, columns, problems):
    """The most specific display setting wins: cell, then row, then column."""
    for r_i, (row, rs) in enumerate(zip(rows, rows_spec)):
        cells_spec = rs.get("cells") if isinstance(rs.get("cells"), list) else []
        for c_i, cell in enumerate(row.cells):
            levels = [cells_spec[c_i] if c_i < len(cells_spec) else None, rs,
                      columns[c_i] if c_i < len(columns) else None]
            levels = [x for x in levels if isinstance(x, dict)]
            fmt = next((x["format"] for x in levels if x.get("format")), None)
            dec = next((x["decimals"] for x in levels if x.get("decimals") is not None), None)
            if fmt is not None and fmt not in FORMATS:
                problems.append(f"row {r_i} col {c_i}: unknown format {fmt!r}")
                continue
            cell.format = fmt
            if dec is not None:
                if not isinstance(dec, int) or isinstance(dec, bool) or not 0 <= dec <= 6:
                    problems.append(f"row {r_i} col {c_i}: decimals must be a whole number from 0 to 6")
                    continue
                cell.decimals = dec


def _fill_text(text, values, results, problems) -> str:
    """Fill {placeholders} from query results, so figures in text stay tied to local data."""
    if not text:
        return ""
    text = str(text)
    values = values if isinstance(values, dict) else {}

    def fill(match):
        name = match.group(1)
        spec = values.get(name)
        if not isinstance(spec, dict):
            problems.append(f"{{{name}}} has no binding in values")
            return match.group(0)
        try:
            cell = _resolve_cell(spec, results, None)
        except TableSpecError as e:
            problems.extend(f"{{{name}}}: {p}" for p in e.problems)
            return match.group(0)
        fmt = spec.get("format")
        if fmt is not None and fmt not in FORMATS:
            problems.append(f"{{{name}}}: unknown format {fmt!r}")
            return match.group(0)
        cell.format, cell.decimals = fmt, spec.get("decimals")
        return cell.display

    return _PLACEHOLDER.sub(fill, text)


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
