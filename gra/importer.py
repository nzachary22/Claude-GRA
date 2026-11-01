"""Import CSV, Excel and JSON exports into a local SQLite database.

Each file (or Excel sheet) becomes one table. Every column is typed and given a
privacy role, which the query gate uses to decide what may leave the computer:

    identifier  record keys and personal identifiers (IDs, names, contact details)
    sensitive   personal values usable in calculations but never shown raw (birth dates)
    free_text   narrative fields (notes); never queryable and never described to the AI
    category    low-cardinality codes whose value list may be shared
    date        ISO dates
    measure     other numbers
    text        other text, queryable but its values are not listed

Small tables with no person-level columns are "reference" tables (code lists);
their contents may be shared with the AI as reporting code definitions.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from dataclasses import dataclass, field, asdict
from pathlib import Path

CATEGORY_MAX_DISTINCT = 100
REFERENCE_MAX_ROWS = 300
FREE_TEXT_MIN_AVG_LEN = 40

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INT_RE = re.compile(r"^-?(0|[1-9]\d*)$")
_REAL_RE = re.compile(r"^-?(0|[1-9]\d*)\.\d+$")

# Name patterns (matched against snake_case column names).
_ID_NAME = re.compile(r"(^|_)(id|uuid|guid|key|number|no|num)$")
_PERSON_NAME = re.compile(
    r"(^|_)(ssn|social_security|email|e_mail|phone|mobile|telephone|cell|fax|address|street|addr|"
    r"zip4|license|passport|alien|a_number|medicaid|medicare|surname|first_name|last_name|"
    r"middle_name|full_name|given_name|maiden_name)(_|$)"
)
# "name" columns are identifiers unless they behave like a short category list.
_NAME_COL = re.compile(r"(^|_)name$")
_SENSITIVE_NAME = re.compile(r"(^|_)(dob|birth|birthdate|date_of_birth|born)(_|$)")
_FREE_TEXT_NAME = re.compile(r"(^|_)(note|notes|narrative|comment|comments|description|memo|summary|details)(_|$)")
_PERSON_TABLE_HINT = re.compile(r"(client|person|people|individual|member|participant|applicant|customer|patient)")


@dataclass
class ColumnInfo:
    name: str
    source_name: str
    sql_type: str  # INTEGER, REAL, TEXT
    role: str
    distinct: int
    nulls: int


@dataclass
class TableInfo:
    name: str
    source_file: str
    rows: int
    columns: list[ColumnInfo] = field(default_factory=list)
    reference: bool = False

    def column(self, name: str) -> ColumnInfo | None:
        return next((c for c in self.columns if c.name == name), None)


@dataclass
class Catalog:
    tables: list[TableInfo] = field(default_factory=list)

    def table(self, name: str) -> TableInfo | None:
        lname = name.lower()
        return next((t for t in self.tables if t.name == lname), None)

    def to_json(self) -> str:
        return json.dumps([asdict(t) for t in self.tables])

    @classmethod
    def from_json(cls, text: str) -> "Catalog":
        tables = []
        for t in json.loads(text):
            cols = [ColumnInfo(**c) for c in t.pop("columns")]
            tables.append(TableInfo(columns=cols, **t))
        return cls(tables)


def snake(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(name).strip()).strip("_").lower()
    if not s:
        s = "col"
    if s[0].isdigit():
        s = "c_" + s
    return s


def _unique(name: str, taken: set[str]) -> str:
    candidate, i = name, 2
    while candidate in taken:
        candidate = f"{name}_{i}"
        i += 1
    taken.add(candidate)
    return candidate


# ---------------------------------------------------------------- readers

def _read_csv(path: Path) -> list[tuple[str, list[str], list[list]]]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not rows:
        return []
    return [(path.stem, rows[0], rows[1:])]


def _read_xlsx(path: Path) -> list[tuple[str, list[str], list[list]]]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(v not in (None, "") for v in r)]
        if not rows:
            continue
        name = path.stem if len(wb.worksheets) == 1 else f"{path.stem}_{ws.title}"
        out.append((name, [str(h) if h is not None else "" for h in rows[0]], rows[1:]))
    wb.close()
    return out


def _read_json(path: Path) -> list[tuple[str, list[str], list[list]]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    groups: dict[str, list] = {}
    if isinstance(data, list):
        groups[path.stem] = data
    elif isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                groups[k] = v
    out = []
    for name, records in groups.items():
        records = [r for r in records if isinstance(r, dict)]
        headers: list[str] = []
        for r in records:
            for k in r:
                if k not in headers:
                    headers.append(k)
        rows = [[_json_cell(r.get(h)) for h in headers] for r in records]
        out.append((name, headers, rows))
    return out


def _json_cell(v):
    if isinstance(v, (list, dict)):
        if isinstance(v, list) and all(isinstance(x, (str, int, float)) for x in v):
            return "|".join(str(x) for x in v)
        return json.dumps(v)
    return v


READERS = {".csv": _read_csv, ".txt": _read_csv, ".tsv": _read_csv, ".xlsx": _read_xlsx, ".xlsm": _read_xlsx, ".json": _read_json}


# ---------------------------------------------------------------- typing

def _clean(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v if v != "" else None
    if hasattr(v, "isoformat"):  # date/datetime cells from Excel become ISO dates
        return v.isoformat()[:10]
    return v


def _infer(values: list) -> str:
    present = [v for v in values if v is not None]
    if not present:
        return "TEXT"
    if all(isinstance(v, bool) or isinstance(v, int) or (isinstance(v, str) and _INT_RE.match(v)) for v in present):
        return "INTEGER"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) or (isinstance(v, str) and (_INT_RE.match(v) or _REAL_RE.match(v))) for v in present):
        return "REAL"
    return "TEXT"


def _convert(v, sql_type: str):
    if v is None:
        return None
    if sql_type == "INTEGER":
        return int(v)
    if sql_type == "REAL":
        return float(v)
    return str(v)


def _classify(col: str, table: str, sql_type: str, values: list, rows: int) -> str:
    present = [v for v in values if v is not None]
    distinct = len(set(present))
    is_date = sql_type == "TEXT" and present and all(_DATE_RE.match(str(v)) for v in present)

    looks_categorical = distinct <= CATEGORY_MAX_DISTINCT and distinct < 0.5 * max(len(present), 1)

    if _SENSITIVE_NAME.search(col) and not (looks_categorical and not is_date):
        return "sensitive"
    if _FREE_TEXT_NAME.search(col):
        return "free_text"
    if _PERSON_NAME.search(col):
        return "identifier"
    if _NAME_COL.search(col) and not looks_categorical:
        return "identifier"
    if _ID_NAME.search(col):
        return "identifier"
    if is_date:
        return "date"
    if sql_type == "TEXT" and present:
        avg_len = sum(len(str(v)) for v in present) / len(present)
        if avg_len >= FREE_TEXT_MIN_AVG_LEN and distinct > max(20, 0.5 * len(present)):
            return "free_text"
        if distinct > CATEGORY_MAX_DISTINCT and distinct >= 0.9 * len(present):
            return "identifier"  # near-unique codes behave like record keys
        if distinct <= CATEGORY_MAX_DISTINCT:
            return "category"
        return "text"
    if sql_type == "INTEGER" and {str(int(v)) for v in present} <= {"0", "1"}:
        return "category"
    return "measure"


def _is_reference(table: TableInfo) -> bool:
    if table.rows > REFERENCE_MAX_ROWS or _PERSON_TABLE_HINT.search(table.name):
        return False
    return not any(
        _PERSON_NAME.search(c.name) or _SENSITIVE_NAME.search(c.name) or c.role == "free_text"
        or _PERSON_TABLE_HINT.search(c.name)
        for c in table.columns
    )


# ---------------------------------------------------------------- import

def import_files(db_path: Path, files: list[Path]) -> Catalog:
    """(Re)build the database at db_path from the given files and return its catalog."""
    db_path = Path(db_path)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    catalog = Catalog()
    taken: set[str] = set()
    try:
        for path in files:
            reader = READERS.get(Path(path).suffix.lower())
            if reader is None:
                raise ValueError(f"Unsupported data file type: {Path(path).name}")
            for raw_name, headers, rows in reader(Path(path)):
                catalog.tables.append(_load_table(conn, raw_name, headers, rows, Path(path).name, taken))
        conn.execute("CREATE TABLE _gra_catalog (json TEXT)")
        conn.execute("INSERT INTO _gra_catalog VALUES (?)", (catalog.to_json(),))
        conn.commit()
    finally:
        conn.close()
    return catalog


def _load_table(conn, raw_name, headers, rows, source_file, taken) -> TableInfo:
    tname = _unique(snake(raw_name), taken)
    col_taken: set[str] = set()
    cols = [_unique(snake(h) if str(h).strip() else f"col_{i + 1}", col_taken) for i, h in enumerate(headers)]
    width = len(cols)
    data = [[_clean(r[i]) if i < len(r) else None for i in range(width)] for r in rows]

    info = TableInfo(name=tname, source_file=source_file, rows=len(data))
    types = []
    for i, cname in enumerate(cols):
        values = [r[i] for r in data]
        sql_type = _infer(values)
        types.append(sql_type)
        present = [v for v in values if v is not None]
        info.columns.append(ColumnInfo(
            name=cname, source_name=str(headers[i]), sql_type=sql_type,
            role=_classify(cname, tname, sql_type, values, len(data)),
            distinct=len(set(present)), nulls=len(values) - len(present),
        ))
    info.reference = _is_reference(info)
    if info.reference:
        for c in info.columns:  # code tables: their codes are definitions, not identifiers
            if c.role == "identifier":
                c.role = "category"

    col_sql = ", ".join(f'"{c}" {t}' for c, t in zip(cols, types))
    conn.execute(f'CREATE TABLE "{tname}" ({col_sql})')
    conn.executemany(
        f'INSERT INTO "{tname}" VALUES ({", ".join("?" * width)})',
        ([_convert(v, t) for v, t in zip(r, types)] for r in data),
    )
    return info


def load_catalog(db_path: Path) -> Catalog:
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        (text,) = conn.execute("SELECT json FROM _gra_catalog").fetchone()
    finally:
        conn.close()
    return Catalog.from_json(text)
