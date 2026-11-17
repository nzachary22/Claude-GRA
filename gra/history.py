"""Past reports: the finished results of earlier runs, kept so they can be opened again.

Each time a report finishes (and again after each correction), its results are saved:
the tables, notes and considerations, the report's headings and definitions, the period
and the names of the files used. Never the uploaded files, the database, any client
record or the API key. They live in the app's own folder: on this computer for the
desktop app, and in this browser's own storage for the browser version.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import __version__
from .engine import Note
from .paths import app_data_dir
from .tables import Cell, Row, Table

KEEP = 30  # the most recent results kept; older ones are deleted
_ID = re.compile(r"[a-f0-9]{12}-\d{1,3}")
# What a past report keeps from a run's results; anything else (events, record values) is left out.
_RESULT_KEYS = ("tables", "notes", "report", "period", "purposes")


def folder() -> Path:
    return app_data_dir() / "history"


def _path(entry_id: str) -> Path:
    if not _ID.fullmatch(entry_id or ""):
        raise KeyError(entry_id)
    return folder() / f"{entry_id}.json"


def save(run_id: str, snapshot: dict, *, report_files: list[str], data_files: list[str],
         data_gaps: bool, orientation: str = "") -> str:
    """Keep one finished result. A run's first result is version 0; each correction adds the next."""
    corrections = len(snapshot.get("corrections") or [])
    entry_id = f"{run_id}-{corrections}"
    entry = {k: snapshot.get(k) for k in _RESULT_KEYS}
    entry.update({
        "id": entry_id, "run": run_id, "corrections": corrections,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "saved_ts": time.time(), "app_version": __version__,
        "report_files": list(report_files), "data_files": list(data_files),
        "data_gaps": bool(data_gaps), "orientation": orientation or "",
    })
    path = _path(entry_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entry, default=str), encoding="utf-8")
    _prune()
    return entry_id


def _entries() -> list[dict]:
    out = []
    if not folder().exists():
        return out
    for p in folder().glob("*.json"):
        try:
            e = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(e, dict) and e.get("id") == p.stem:
            out.append(e)
    out.sort(key=lambda e: (e.get("saved_ts") or 0, e.get("corrections") or 0), reverse=True)
    return out


def summary(e: dict) -> dict:
    report = e.get("report") or {}
    gaps = sum(1 for t in e.get("tables") or [] for r in t.get("rows") or [] for c in r.get("cells") or [] if c.get("gap"))
    return {"id": e["id"], "run": e.get("run"), "corrections": e.get("corrections", 0),
            "saved_at": e.get("saved_at"), "saved_ts": e.get("saved_ts"), "period": e.get("period"),
            "organization": report.get("organization") or "", "title": report.get("title") or "",
            "report_files": e.get("report_files") or [], "tables": len(e.get("tables") or []),
            "notes": len(e.get("notes") or []), "gaps": gaps}


def list_all() -> list[dict]:
    return [summary(e) for e in _entries()]


def get(entry_id: str) -> dict:
    path = _path(entry_id)
    if not path.exists():
        raise KeyError(entry_id)
    return json.loads(path.read_text(encoding="utf-8"))


def delete(entry_id: str) -> bool:
    path = _path(entry_id)
    if path.exists():
        path.unlink()
        return True
    return False


def delete_all() -> int:
    count = 0
    if folder().exists():
        for p in folder().glob("*.json"):
            p.unlink(missing_ok=True)
            count += 1
    return count


def _prune():
    for e in _entries()[KEEP:]:
        (folder() / f"{e['id']}.json").unlink(missing_ok=True)


# ---------------------------------------------------------------- rebuilding a result for the downloads

def tables_of(entry: dict) -> list[Table]:
    out = []
    for t in entry.get("tables") or []:
        rows = [Row(r.get("label", ""), r.get("kind", "data"), int(r.get("indent") or 0),
                    [Cell(c.get("value"), c.get("gap"), c.get("source"), c.get("format"), c.get("decimals"), c.get("how"))
                     for c in r.get("cells") or []])
                for r in t.get("rows") or []]
        out.append(Table(t.get("id", ""), t.get("title", ""), t.get("note", ""), list(t.get("headers") or []), rows,
                         list(t.get("warnings") or []), t.get("row_header", ""), t.get("subtitle", ""),
                         t.get("orientation", "")))
    return out


def notes_of(entry: dict) -> list[Note]:
    fields = set(Note.__dataclass_fields__)
    return [Note(**{k: v for k, v in n.items() if k in fields}) for n in entry.get("notes") or []]
