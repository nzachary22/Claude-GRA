"""Non-identifying description of the imported data, the only view of it the AI gets.

Shared: table and column names, types, privacy roles, row and blank counts,
code lists for category columns, date ranges for non-personal date columns,
and the full contents of small reference (code) tables.
Never shared: identifier, sensitive or free-text values, or any row of a
person-level table. Free-text columns are not even named.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .importer import Catalog

MAX_CODES = 100


def describe(db_path: Path, catalog: Catalog) -> str:
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    lines: list[str] = []
    try:
        for t in catalog.tables:
            kind = "reference/code table" if t.reference else "data table"
            lines.append(f"TABLE {t.name} ({kind}; {t.rows} rows; from file {t.source_file})")
            hidden = 0
            for c in t.columns:
                if c.role == "free_text":
                    hidden += 1
                    continue
                desc = f"  - {c.name} {c.sql_type} role={c.role} distinct={c.distinct} blank={c.nulls}"
                if c.source_name != c.name:
                    desc += f' (header "{c.source_name}")'
                if c.note:
                    desc += f"; {c.note}"
                if c.role == "category" and not t.reference and c.distinct <= MAX_CODES:
                    values = [v for (v,) in conn.execute(
                        f'SELECT DISTINCT "{c.name}" FROM "{t.name}" WHERE "{c.name}" IS NOT NULL ORDER BY 1')]
                    desc += f"; values: {_fmt(values)}"
                elif c.role == "date":
                    lo, hi = conn.execute(f'SELECT MIN("{c.name}"), MAX("{c.name}") FROM "{t.name}"').fetchone()
                    desc += f"; range {lo} to {hi}"
                lines.append(desc)
            if hidden:
                lines.append(f"  ({hidden} free-text column(s) withheld; they cannot be queried)")
            if t.reference:
                cols = [c.name for c in t.columns]
                lines.append("  contents (" + " | ".join(cols) + "):")
                order = ", ".join(f'"{c}"' for c in cols)
                for row in conn.execute(f'SELECT * FROM "{t.name}" ORDER BY {order}'):
                    lines.append("    " + " | ".join("" if v is None else str(v) for v in row))
            lines.append("")
    finally:
        conn.close()
    return "\n".join(lines).rstrip() + "\n"


def _fmt(values) -> str:
    return ", ".join(repr(v) if isinstance(v, str) else str(v) for v in values)
