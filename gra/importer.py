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
from datetime import datetime
from pathlib import Path

CATEGORY_MAX_DISTINCT = 100
REFERENCE_MAX_ROWS = 300
FREE_TEXT_MIN_AVG_LEN = 40

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
_SLASH_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_MONEY_RE = re.compile(r"^-?\$\s?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")
_INT_RE = re.compile(r"^-?(0|[1-9]\d*)$")
_REAL_RE = re.compile(r"^-?(0|[1-9]\d*)\.\d+$")

# Name patterns (matched against snake_case column names).
_ID_NAME = re.compile(r"(^|_)(id|ids|uuid|guid|key|number|no|num|nbr)$|^(case|matter|file|record|docket|account|"
                      r"case_file|intake|ticket|reference|ref)$")
_PERSON_NAME = re.compile(
    r"(^|_)(ssn|ss_no|ss_num|social_security|tin|itin|ein|email|e_mail|phone|phones|mobile|telephone|tel|cell|fax|"
    r"address|address1|address2|street|addr|apt|unit_no|zip4|zip_4|license|dl|passport|alien|a_number|uscis|"
    r"medicaid|medicare|mrn|surname|first_name|last_name|middle_name|full_name|given_name|maiden_name|"
    r"aka|alias|initials|latitude|longitude|lat|lng|lon|ip_address|signature)(_|$)"
)
# Columns that name a person (a client, an opposing party, a family member) are
# identifiers whatever their cardinality: one client with many cases repeats.
_PERSON_REF = re.compile(
    r"^(client|applicant|participant|person|individual|customer|patient|caller|beneficiary|"
    r"spouse|guardian|household_member|contact|emergency_contact|"
    r"tenant|landlord|employer|respondent|petitioner|defendant|plaintiff|victim|witness|abuser|"
    r"party|adverse_party|opposing_party|opposing_counsel|adverse|opposing)(_(full_|first_|last_)?names?)?$"
    r"|(^|_)(adverse|opposing)_(party|parties|counsel)(_|$)|(^|_)(client|applicant|spouse|child|tenant|"
    r"landlord|party|contact|guardian|respondent|petitioner|defendant|plaintiff|victim)_(full_|first_|last_)?names?$"
)
# Other "name" columns are identifiers unless they behave like a short category list.
_NAME_COL = re.compile(r"(^|_)names?$")
_SENSITIVE_NAME = re.compile(r"(^|_)(dob|birth|birthdate|date_of_birth|born)(_|$)")
_FREE_TEXT_NAME = re.compile(r"(^|_)(note|notes|narrative|comment|comments|description|memo|summary|details)(_|$)")
_CODE_TABLE_HINT = re.compile(r"(^|_)(code|codes|reference|lookup|lookups|crosswalk|definitions?|dictionary|"
                              r"types|categories|labels|levels|map|mapping|codebook|legend)(_|$)")
_CODE_TABLE_END = re.compile(r"(^|_)(codes?|code_lists?|lookups?|crosswalk|definitions?|dictionary|codebook|legend|"
                             r"categories|mapping)$")
_PERSON_TABLE_HINT = re.compile(r"(client|person|people|individual|member|participant|applicant|customer|patient)")


@dataclass
class ColumnInfo:
    name: str
    source_name: str
    sql_type: str  # INTEGER, REAL, TEXT
    role: str
    distinct: int
    nulls: int
    note: str = ""  # how the app standardized the column's values, if it did


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
    """Column and table names in snake_case; CamelCase headers (DateOfBirth) are split too."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name).strip())
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    s = re.sub(r"[^0-9a-zA-Z]+", "_", s).strip("_").lower()
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


def _normalize(values: list) -> tuple[list, str]:
    """Standardize whole columns of dollar text or slash dates so they can be calculated on."""
    present = [v for v in values if isinstance(v, str)]
    if not present or len(present) != sum(v is not None for v in values):
        return values, ""
    if all(_MONEY_RE.match(v) for v in present):
        convert = lambda v: float(v.replace("$", "").replace(",", "").replace(" ", ""))
        return [None if v is None else convert(v) for v in values], "converted from dollar text such as $1,250.00"
    matches = [_SLASH_DATE_RE.match(v) for v in present]
    if all(matches):
        day_first = any(int(m[1]) > 12 for m in matches)
        if day_first and any(int(m[2]) > 12 for m in matches):
            return values, ""
        fmt, label = ("%d/%m/%Y", "DD/MM/YYYY") if day_first else ("%m/%d/%Y", "MM/DD/YYYY")
        try:
            out = [None if v is None else datetime.strptime(v, fmt).strftime("%Y-%m-%d") for v in values]
        except ValueError:
            return values, ""
        return out, f"converted from {label} to YYYY-MM-DD"
    return values, ""


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
    if _PERSON_NAME.search(col) or _PERSON_REF.search(col):
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
        if distinct >= 0.9 * len(present) and (distinct > CATEGORY_MAX_DISTINCT or len(present) >= 20):
            return "identifier"  # near-unique codes behave like record keys, even in a small export
        if distinct <= CATEGORY_MAX_DISTINCT:
            return "category"
        return "text"
    if sql_type == "INTEGER" and {str(int(v)) for v in present} <= {"0", "1"}:
        return "category"
    return "measure"


def _is_reference(table: TableInfo) -> bool:
    """Whether a table is a code list whose full contents may be shown to the AI.

    A small export of cases or clients must never qualify, so a table without a
    code-list name also needs no record keys, dates or personal values.
    """
    if table.rows > REFERENCE_MAX_ROWS:
        return False
    # A person word blocks a table ("clients", "client_intake"), unless the name ends as a code list,
    # as in an export prefix: "2026_Client_Data_code_lists" holds codes, not clients. Such a table
    # still passes only with no dates and none of the personal columns checked below.
    person_named = bool(_PERSON_TABLE_HINT.search(table.name))
    if person_named and not (_CODE_TABLE_END.search(table.name)
                             and not any(c.role == "date" for c in table.columns)):
        return False
    # A notes column in a table named as a code list explains the codes; elsewhere it stays withheld.
    code_list = bool(_CODE_TABLE_HINT.search(table.name))
    if any(
        _PERSON_NAME.search(c.name) or _SENSITIVE_NAME.search(c.name) or _PERSON_REF.search(c.name)
        or (c.role == "free_text" and not code_list) or _PERSON_TABLE_HINT.search(c.name)
        or c.role == "sensitive"
        for c in table.columns
    ):
        return False
    if code_list:
        return True
    return not any(c.role in ("identifier", "date", "free_text") for c in table.columns)


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
        values, note = _normalize([r[i] for r in data])
        for r, v in zip(data, values):
            r[i] = v
        sql_type = _infer(values)
        types.append(sql_type)
        present = [v for v in values if v is not None]
        info.columns.append(ColumnInfo(
            name=cname, source_name=str(headers[i]), sql_type=sql_type,
            role=_classify(cname, tname, sql_type, values, len(data)),
            distinct=len(set(present)), nulls=len(values) - len(present), note=note,
        ))
    info.reference = _is_reference(info)
    if info.reference:
        for c in info.columns:  # code tables: their entries are definitions, not client data
            if c.role in ("identifier", "free_text"):
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
