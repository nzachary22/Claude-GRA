"""What the app does for each request, shared by the desktop server and the browser version.

server.py wraps these methods in a local web server (FastAPI); the browser version
(web/) calls the same methods inside the page, through Pyodide. Nothing here
depends on a web framework or on threads: long work is handed to `spawn`, which
starts a thread on the desktop and queues the job in the browser.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import __version__
from .activity import read_events, render_text, sent_text
from .activity_report import to_report_docx
from .data_quality import has_gaps, to_quality_docx
from .documents import layout_summary, scan_text, to_blocks
from .engine import ReportSession
from .exports import to_docx, to_xlsx
from .gate import QueryGate
from .importer import READERS, import_files, load_catalog
from .paths import app_data_dir
from .schema import describe

log = logging.getLogger("gra")
# The fictional Juniper Ridge sample (synthetic data, invented organization) for trying the app safely.
SAMPLE = Path(__file__).parent / "sample"
MAX_CORRECTION = 12000  # several considerations answered together fit easily
SAMPLE_REPORT = "Juniper Ridge 2025 annual report.pdf"
SAMPLE_DATA = ("case_events.csv", "cases.csv", "clients.csv", "demographic_responses.csv", "outcome_codes.csv",
               "problem_codes.csv")
SAMPLE_PERIOD = ("2026-01-01", "2026-12-31")
DOC_TYPES = {".pdf", ".docx", ".xlsx", ".xlsm", ".txt", ".md", ".csv", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
KEEP_RUNS = 5
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class FileOut:
    """A download: its bytes, media type and file name."""
    data: bytes
    media: str
    filename: str


@dataclass
class Upload:
    name: str
    content: bytes


@dataclass
class Run:
    id: str
    folder: Path
    period: tuple[str, str]
    report_names: list[str]
    data_names: list[str]
    status: str = "importing"  # importing | running | done | failed | cancelled
    error: str | None = None
    session: ReportSession | None = None
    import_summary: list[dict] = field(default_factory=list)
    pre_events: list[dict] = field(default_factory=list)
    cancel_requested: bool = False
    schema_text: str = ""
    demo: bool = False  # the recorded no-key demo on the fictional sample
    saved_choices: list[str] = field(default_factory=list)  # reused from an earlier run of the same report

    @property
    def current_period(self) -> tuple[str, str]:
        """The period the tables cover: as entered, unless a correction changed it."""
        return tuple(self.session.period) if self.session else self.period


def _thread(fn, *args):
    import threading

    threading.Thread(target=fn, args=args, daemon=True).start()


class Api:
    """model_factory() returns a model client; tests pass a scripted one. spawn(fn, *args) runs long work."""

    def __init__(self, model_factory=None, spawn: Callable = _thread, demo_pause: float | None = None):
        self.scripted = model_factory is not None
        self.factory = model_factory or _default_factory
        self.spawn = spawn
        self.demo_pause = demo_pause if demo_pause is not None else (0 if self.scripted else 1.5)
        self.runs: dict[str, Run] = {}

    def get_run(self, run_id: str) -> Run:
        run = self.runs.get(run_id)
        if run is None:
            raise ApiError(404, "Unknown report run.")
        return run

    # ------------------------------------------------------------ settings

    def status(self) -> dict:
        from .llm import PROVIDERS, active_provider, key_status, settings_seen

        keys = key_status()
        provider = active_provider()
        return {"version": __version__, "provider": provider,
                "has_key": self.scripted or keys[provider], "keys": keys,
                # Open Settings once, so people upgrading with a saved key see the choice of AI service.
                "show_settings": not self.scripted and not settings_seen() and any(keys.values()),
                "providers": [{"id": k, "label": v["label"]} for k, v in PROVIDERS.items()]}

    def set_key(self, key: str, provider: str = "anthropic") -> dict:
        from .llm import PROVIDERS, check_api_key, save_api_key, set_provider

        if provider not in PROVIDERS:
            raise ApiError(400, "Choose Anthropic or OpenAI.")
        key = (key or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-]{20,300}", key):
            raise ApiError(400, "That does not look like an API key.")
        problem = check_api_key(key, provider)
        if problem:
            raise ApiError(400, problem)
        save_api_key(key, provider)
        set_provider(provider)  # a newly saved key means the user wants to use that service
        return {"ok": True}

    def forget_key(self, provider: str) -> dict:
        """Remove a saved key from this computer (or, in the browser version, from this browser)."""
        from .llm import PROVIDERS, delete_api_key

        if provider not in PROVIDERS:
            raise ApiError(400, "Choose Anthropic or OpenAI.")
        delete_api_key(provider)
        return {"ok": True}

    def settings_seen(self) -> dict:
        from .llm import mark_settings_seen

        mark_settings_seen()
        return {"ok": True}

    def choose_provider(self, provider: str) -> dict:
        """Switch to a service whose key is already saved. New reports use it; a running one keeps its own."""
        from .llm import PROVIDERS, key_status, set_provider

        if provider not in PROVIDERS:
            raise ApiError(400, "Choose Anthropic or OpenAI.")
        if not key_status()[provider]:
            raise ApiError(400, f"Add a {PROVIDERS[provider]['label']} API key first.")
        set_provider(provider)
        return {"ok": True}

    # ------------------------------------------------------------ runs

    def start_run(self, report_files: list[Upload], data_files: list[Upload], period_start: str, period_end: str,
                  saved_choices: str = "") -> dict:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", period_start or "") or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", period_end or ""):
            raise ApiError(400, "Enter the reporting period as two dates.")
        if period_end < period_start:
            raise ApiError(400, "The reporting period ends before it starts.")
        if not report_files or not data_files:
            raise ApiError(400, "Add the report and the data files.")
        for f in report_files:
            if Path(f.name or "").suffix.lower() not in DOC_TYPES:
                raise ApiError(400, f"{f.name}: unsupported report file type.")
        for f in data_files:
            if Path(f.name or "").suffix.lower() not in READERS:
                raise ApiError(400, f"{f.name}: data files must be CSV, Excel or JSON.")

        run_id = uuid.uuid4().hex[:12]
        folder = app_data_dir() / "runs" / run_id
        (folder / "report").mkdir(parents=True)
        (folder / "data").mkdir()
        total = 0
        saved_reports, saved_data = [], []
        for files, sub, saved in ((report_files, "report", saved_reports), (data_files, "data", saved_data)):
            for f in files:
                name = _safe_name(f.name or "file", saved)
                total += len(f.content)
                if total > MAX_UPLOAD_BYTES:
                    shutil.rmtree(folder, ignore_errors=True)
                    raise ApiError(413, "The uploaded files are too large.")
                (folder / sub / name).write_bytes(f.content)
                saved.append(name)

        chosen = next((c for c in _read_choices() if c["id"] == saved_choices), None) if saved_choices else None
        run = Run(run_id, folder, (period_start, period_end), saved_reports, saved_data,
                  saved_choices=list(chosen["choices"]) if chosen else [])
        if chosen:
            _event(run, "status", f"Reusing {len(chosen['choices'])} saved choice(s) from {chosen['name']}")
        self.runs[run_id] = run
        _prune(self.runs)
        self.spawn(_work, run, self.factory)
        return {"id": run_id}

    def start_demo(self) -> dict:
        """The no-key demo: the fictional sample, prepared by a recorded analyst instead of an AI service."""
        from .demo import DemoModel

        run_id = uuid.uuid4().hex[:12]
        folder = app_data_dir() / "runs" / run_id
        (folder / "report").mkdir(parents=True)
        (folder / "data").mkdir()
        for name, sub in [(SAMPLE_REPORT, "report")] + [(n, "data") for n in SAMPLE_DATA]:
            shutil.copyfile(SAMPLE / name, folder / sub / name)
        run = Run(run_id, folder, SAMPLE_PERIOD, [SAMPLE_REPORT], list(SAMPLE_DATA), demo=True)
        self.runs[run_id] = run
        _prune(self.runs)
        pause = self.demo_pause  # tests skip the pauses that make progress visible
        self.spawn(_work, run, lambda: DemoModel(*SAMPLE_PERIOD, SAMPLE, pause))
        return {"id": run_id}

    def poll(self, run_id: str, since: int = 0) -> dict:
        run = self.get_run(run_id)
        snap = run.session.snapshot(0) if run.session else {"events": [], "tables": [], "notes": [], "report": {}, "queries_used": 0}
        events = run.pre_events + snap["events"]
        status = run.status if run.session is None else _merge_status(run, snap["status"])
        return {
            "id": run.id, "status": status, "error": run.error or snap.get("error"),
            "period": list(run.current_period), "report_files": run.report_names, "data_files": run.data_names,
            "import_summary": run.import_summary,
            "events": events[since:], "event_count": len(events),
            "demo": run.demo, "purposes": snap.get("purposes", {}), "corrections": len(snap.get("corrections", [])),
            "tables": snap["tables"], "notes": snap["notes"], "report": snap["report"], "queries_used": snap["queries_used"],
            "data_gaps": bool(run.session and run.session.tables
                              and has_gaps(run.session.tables, list(run.session.notes.values()))),
        }

    def cancel(self, run_id: str) -> dict:
        run = self.get_run(run_id)
        run.cancel_requested = True
        if run.session:
            run.session.cancel()
        return {"ok": True}

    def correct(self, run_id: str, text: str) -> dict:
        run = self.get_run(run_id)
        if run.demo:
            raise ApiError(400, "The demo is a recording, so it can't recalculate. To try changes, add your own "
                                "AI key in Settings and prepare a report from your files or the sample files.")
        if not run.session or run.session.status == "running" or run.status == "importing":
            raise ApiError(409, "The report is still being prepared.")
        text = (text or "").strip()
        if not text:
            raise ApiError(400, "Describe the correction.")
        if len(text) > MAX_CORRECTION:
            raise ApiError(400, "That correction is too long. Shorten it, or split it into two corrections.")
        if run.session.gate.contains_protected(text):
            raise ApiError(400, "The correction mentions a name, ID or note from your client data, which would be "
                                "sent to the AI. Describe the change without it.")
        run.cancel_requested = False
        run.status = "running"
        run.session.status = "running"
        self.spawn(_correct, run, text)
        return {"ok": True}

    def delete_run(self, run_id: str) -> dict:
        """Remove a run's uploads, database, activity record and exports from this computer."""
        if not re.fullmatch(r"[a-f0-9]{12}", run_id or ""):
            raise ApiError(404, "Unknown report run.")
        run = self.runs.pop(run_id, None)
        if run and run.session:
            run.session.cancel()
        _remove(app_data_dir() / "runs" / run_id)
        return {"ok": True}

    def delete_all_runs(self) -> dict:
        for run in list(self.runs.values()):
            if run.session:
                run.session.cancel()
        self.runs.clear()
        root = app_data_dir() / "runs"
        count = 0
        if root.exists():
            for folder in root.iterdir():
                if folder.is_dir():
                    _remove(folder)
                    count += 1
        return {"ok": True, "deleted": count}

    def preview(self, data_files: list[Upload]) -> dict:
        """Import the exports and show exactly what the AI would be told about them. Nothing is sent."""
        for f in data_files:
            if Path(f.name or "").suffix.lower() not in READERS:
                raise ApiError(400, f"{f.name}: data files must be CSV, Excel or JSON.")
        folder = app_data_dir() / "preview" / uuid.uuid4().hex[:12]
        folder.mkdir(parents=True)
        try:
            names, total = [], 0
            for f in data_files:
                name = _safe_name(f.name or "file", names)
                total += len(f.content)
                if total > MAX_UPLOAD_BYTES:
                    raise ApiError(413, "The uploaded files are too large.")
                (folder / name).write_bytes(f.content)
                names.append(name)
            db = folder / "data.db"
            catalog = import_files(db, [folder / n for n in names])
            return {"schema": describe(db, catalog), **_preview_groups(db, catalog)}
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        finally:
            _remove(folder)

    def details(self, run_id: str) -> dict:
        run = self.get_run(run_id)
        qlog = run.session.gate.log if run.session else []
        return {"schema": run.schema_text, "queries": [
            {"id": q.query_id, "purpose": q.purpose, "sql": q.sql, "columns": q.columns, "rows": q.rows[:200],
             "row_count": len(q.rows), "ms": q.elapsed_ms} for q in qlog]}

    def _saved_activity(self, run_id: str):
        """A run's record and gate; also works after a restart, from the saved folder."""
        if not re.fullmatch(r"[a-f0-9]{12}", run_id or ""):
            raise ApiError(404, "Unknown report run.")
        folder = app_data_dir() / "runs" / run_id
        events = read_events(folder / "activity.jsonl")
        if not events:
            raise ApiError(404, "No AI activity was recorded for this run.")
        run = self.runs.get(run_id)
        gate = run.session.gate if run and run.session else QueryGate(folder / "data.db", load_catalog(folder / "data.db"))
        return events, gate

    def activity_report(self, run_id: str) -> FileOut:
        events, gate = self._saved_activity(run_id)
        data = to_report_docx(events, gate.catalog, gate.contains_protected, gate.protected_value_count())
        return FileOut(data, DOCX, f"ai-activity-report-{run_id}.docx")

    def activity_log(self, run_id: str) -> FileOut:
        events, gate = self._saved_activity(run_id)
        hits = [e["seq"] for e in events if sent_text(e) and gate.contains_protected(sent_text(e))]
        text = render_text(events, hits, gate.protected_value_count(), run_id)
        return FileOut(text.encode("utf-8"), "text/plain; charset=utf-8", f"ai-activity-log-{run_id}.txt")

    def export(self, run_id: str, fmt: str) -> FileOut:
        run = self.get_run(run_id)
        if not run.session or not run.session.tables:
            raise ApiError(409, "There are no tables to export yet.")
        tables, notes = run.session.tables, list(run.session.notes.values())
        title = "Grant report tables"
        start, end = run.current_period
        period = f"{start} to {end}"
        stamp = end.replace("-", "")
        if fmt == "docx":
            data = to_docx(tables, notes, title, period, run.session.report, run.session.page_orientation)
            media = DOCX
        elif fmt == "xlsx":
            data = to_xlsx(tables, notes, title, period, run.session.report, run.session.page_orientation)
            media = XLSX
        else:
            raise ApiError(404, "Unknown format.")
        return FileOut(data, media, f"grant-report-tables-{stamp}.{fmt}")

    def data_quality(self, run_id: str) -> FileOut:
        run = self.get_run(run_id)
        if not run.session or not run.session.tables:
            raise ApiError(409, "There are no tables yet.")
        s = run.session
        data = to_quality_docx(s.tables, list(s.notes.values()), s.gate.catalog, [q.sql for q in s.gate.log],
                               s.report, tuple(run.current_period))
        stamp = run.current_period[1].replace("-", "")
        return FileOut(data, DOCX, f"data-collection-guide-{stamp}.docx")

    # ------------------------------------------------------------ saved choices

    def list_choices(self) -> list[dict]:
        return [{k: c[k] for k in ("id", "name", "report_files", "saved_at")} | {"count": len(c["choices"])}
                for c in _read_choices()]

    def save_choices(self, run_id: str) -> dict:
        """Keep this run's choices (the AI's key choices as kept, and the user's own) for the next run of this report."""
        run = self.get_run(run_id)
        if run.demo or not run.session:
            raise ApiError(400, "There are no choices to save for this run.")
        s = run.session
        kept = [f"As last time: {n.text}" for n in s.notes.values() if n.key and n.kind == "assumption"]
        choices = kept + [f"The user asked: {c}" for c in s.corrections]
        if not choices:
            raise ApiError(400, "This run has no choices to save yet.")
        name = " · ".join(x for x in (s.report.get("organization"), s.report.get("title")) if x) or ", ".join(run.report_names)
        saved = [c for c in _read_choices() if c["name"] != name]
        entry = {"id": uuid.uuid4().hex[:12], "name": name, "report_files": list(run.report_names),
                 "choices": choices[:20], "saved_at": time.strftime("%Y-%m-%d")}
        _write_choices([entry] + saved[:19])
        return {"ok": True, "name": name, "count": len(entry["choices"])}

    def delete_choices(self, choice_id: str) -> dict:
        _write_choices([c for c in _read_choices() if c["id"] != choice_id])
        return {"ok": True}

    # ------------------------------------------------------------ the fictional sample

    def sample(self) -> dict:
        """The fictional sample: a past report, its data extract and the period to report on."""
        return {"report": [SAMPLE_REPORT], "data": list(SAMPLE_DATA), "period": list(SAMPLE_PERIOD),
                "name": "Juniper Ridge Legal Aid Society (fictional)"}

    def sample_file(self, name: str) -> Path:
        if name not in (SAMPLE_REPORT, *SAMPLE_DATA):
            raise ApiError(404, "Not a sample file.")
        return SAMPLE / name


def _default_factory():
    from .llm import make_client

    return make_client()


# ---------------------------------------------------------------- workers

def _event(run: Run, kind: str, message: str):
    run.pre_events.append({"at": time.time(), "kind": kind, "message": message})


def _work(run: Run, factory):
    if _prepare(run, factory):
        _run_session(run)


def _prepare(run: Run, factory) -> bool:
    """Import the data, check the report and connect: everything before the first AI request.

    Returns True when the report is ready to be worked on with the AI."""
    try:
        _event(run, "status", f"Importing {len(run.data_names)} data file(s)")
        db = run.folder / "data.db"
        catalog = import_files(db, [run.folder / "data" / n for n in run.data_names])
        run.import_summary = [{"table": t.name, "file": t.source_file, "rows": t.rows,
                               "protected_columns": sum(c.role in ("identifier", "sensitive", "free_text") for c in t.columns)}
                              for t in catalog.tables]
        rows = sum(t.rows for t in catalog.tables)
        protected = sum(s["protected_columns"] for s in run.import_summary)
        _event(run, "status", f"Imported {rows:,} records into {len(catalog.tables)} tables; "
                              f"{protected} identifying or free-text columns protected")
        run.schema_text = describe(db, catalog)
        blocks = []
        for n in run.report_names:
            blocks.extend(to_blocks(run.folder / "report" / n))
        if run.cancel_requested:
            run.status = "cancelled"
            _event(run, "cancelled", "Stopped at your request")
            return False
        gate = QueryGate(db, catalog)
        _check_reports(run, gate)
        _event(run, "status", "Starting the recorded demo; nothing is sent to any AI service" if run.demo
               else "Connecting to the AI service")
        model = factory()
        layout = layout_summary([run.folder / "report" / n for n in run.report_names])
        run.session = ReportSession(gate, run.schema_text, blocks, run.period, model,
                                    activity_path=run.folder / "activity.jsonl", layout=layout,
                                    saved_choices=run.saved_choices)
        run.status = "running"
        if run.cancel_requested:
            run.session.cancel()
        return True
    except Exception as e:
        _failed(run, e)
        return False


def _run_session(run: Run):
    try:
        run.session.run()
        run.status = run.session.status
    except Exception as e:
        _failed(run, e)


def _failed(run: Run, e: Exception):
    log.exception("Report run %s failed", run.id)
    run.status = "failed"
    run.error = str(e) if isinstance(e, (RuntimeError, ValueError)) else f"Could not prepare the report ({type(e).__name__})."
    _event(run, "error", run.error)


def _check_reports(run: Run, gate: QueryGate):
    """Refuse to send a report or requirements file that contains a stored client name, ID or note."""
    for n in run.report_names:
        text = scan_text(run.folder / "report" / n)
        if text is None:
            _event(run, "status", f"{n} could not be checked for client details (image or unreadable file); "
                                  "it is sent as is")
            continue
        found = gate.protected_matches(text, limit=50)
        # Full names, IDs and emails stop the run; a lone word (a surname a staff member may share) is flagged.
        strong = [v for v in found if re.search(r"[\d\s@]", v)]
        if strong:
            shown = ", ".join(f'"{v}"' for v in strong[:3])
            raise ValueError(f"{n} contains client details that also appear in your data ({shown}). "
                             "Nothing was sent to the AI. Remove them from the report, or upload only "
                             "its tables or the grant requirements, and start again.")
        if found:
            _event(run, "warning", f"{n} contains words that also appear in protected client fields "
                                   f"({', '.join(found[:5])}). Check that the report names no clients.")
    if run.report_names:
        _event(run, "status", "Checked the report for client names and IDs from your data; none found")


_KEPT_WHY = {"identifier": "ID or name", "sensitive": "personal; used only for groups such as age bands",
             "free_text": "notes"}


def _preview_groups(db: Path, catalog) -> dict:
    """The preview in four plain groups: kept here, values listed, totals only, code lists shared in full."""
    import sqlite3

    from .schema import MAX_CODES

    kept, listed, totals, code_lists = [], [], [], []
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        for t in catalog.tables:
            if t.reference:
                code_lists.append({"file": t.source_file, "rows": t.rows,
                                   "columns": [c.source_name for c in t.columns]})
                continue
            for c in t.columns:
                item = {"header": c.source_name, "file": t.source_file}
                if c.role in _KEPT_WHY:
                    kept.append({**item, "why": _KEPT_WHY[c.role]})
                elif c.role == "category" and c.distinct <= MAX_CODES:
                    values = [str(v) for (v,) in conn.execute(
                        f'SELECT DISTINCT "{c.name}" FROM "{t.name}" WHERE "{c.name}" IS NOT NULL ORDER BY 1 LIMIT 6')]
                    listed.append({**item, "count": c.distinct, "examples": values})
                else:
                    totals.append(item)
    finally:
        conn.close()
    return {"files": [{"file": t.source_file, "rows": t.rows} for t in catalog.tables],
            "kept": kept, "listed": listed, "totals": totals, "code_lists": code_lists}


def _remove(folder: Path):
    """Delete a folder; a database file still held open is retried once."""
    shutil.rmtree(folder, ignore_errors=True)
    if folder.exists():
        time.sleep(0.5)
        shutil.rmtree(folder, ignore_errors=True)


def _correct(run: Run, text: str):
    try:
        run.session.correct(text)
        run.status = run.session.status
    except Exception as e:
        log.exception("Correction for run %s failed", run.id)
        run.status = "failed"
        run.error = f"Could not apply the correction ({type(e).__name__})."


def _merge_status(run: Run, session_status: str) -> str:
    if run.status in ("failed",) and run.error:
        return "failed"
    return session_status if session_status != "ready" else run.status


def _safe_name(name: str, taken: list[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(name).name).strip() or "file"
    candidate, i = base, 2
    while candidate in taken:
        stem, suf = Path(base).stem, Path(base).suffix
        candidate = f"{stem}_{i}{suf}"
        i += 1
    return candidate


def _choices_file() -> Path:
    return app_data_dir() / "saved-choices.json"


def _read_choices() -> list[dict]:
    try:
        data = json.loads(_choices_file().read_text(encoding="utf-8"))
        return [c for c in data if isinstance(c, dict) and {"id", "name", "choices"} <= set(c)]
    except (OSError, ValueError):
        return []


def _write_choices(items: list[dict]):
    path = _choices_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=1), encoding="utf-8")


def _prune(runs: dict[str, Run]):
    """Keep only the most recent runs on disk and in memory."""
    root = app_data_dir() / "runs"
    if not root.exists():
        return
    folders = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in folders[KEEP_RUNS:]:
        runs.pop(old.name, None)
        shutil.rmtree(old, ignore_errors=True)
