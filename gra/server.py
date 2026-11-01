"""Local web server: Upload -> Prepare automatically -> Review.

Binds to 127.0.0.1 only. Uploads, databases and exports are kept in the local
app-data folder, never in the repository.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .documents import to_blocks
from .engine import ReportSession
from .exports import to_docx, to_xlsx
from .gate import QueryGate
from .importer import READERS, import_files
from .paths import app_data_dir
from .schema import describe

log = logging.getLogger("gra")
STATIC = Path(__file__).parent / "static"
DOC_TYPES = {".pdf", ".docx", ".xlsx", ".xlsm", ".txt", ".md", ".csv", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
KEEP_RUNS = 5


class KeyIn(BaseModel):
    key: str


class CorrectionIn(BaseModel):
    text: str


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


def create_app(model_factory=None) -> FastAPI:
    """model_factory() returns a model client; tests pass a scripted one."""
    app = FastAPI(title="Grant Reporting Assistant", version=__version__)
    runs: dict[str, Run] = {}

    def default_factory():
        from .llm import ClaudeClient, get_api_key

        key = get_api_key()
        if not key:
            raise RuntimeError("No AI key is set up yet. Add it under Settings.")
        return ClaudeClient(key)

    factory = model_factory or default_factory

    def get_run(run_id: str) -> Run:
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(404, "Unknown report run.")
        return run

    # ------------------------------------------------------------ settings

    @app.get("/api/status")
    def status():
        from .llm import get_api_key

        return {"version": __version__, "has_key": model_factory is not None or bool(get_api_key())}

    @app.post("/api/key")
    def set_key(body: KeyIn):
        from .llm import check_api_key, save_api_key

        key = body.key.strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-]{20,300}", key):
            raise HTTPException(400, "That does not look like an API key.")
        problem = check_api_key(key)
        if problem:
            raise HTTPException(400, problem)
        save_api_key(key)
        return {"ok": True}

    # ------------------------------------------------------------ runs

    @app.post("/api/runs")
    async def start_run(report_files: list[UploadFile] = File(...), data_files: list[UploadFile] = File(...),
                        period_start: str = Form(...), period_end: str = Form(...)):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", period_start) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", period_end):
            raise HTTPException(400, "Enter the reporting period as two dates.")
        if period_end < period_start:
            raise HTTPException(400, "The reporting period ends before it starts.")
        for f in report_files:
            if Path(f.filename or "").suffix.lower() not in DOC_TYPES:
                raise HTTPException(400, f"{f.filename}: unsupported report file type.")
        for f in data_files:
            if Path(f.filename or "").suffix.lower() not in READERS:
                raise HTTPException(400, f"{f.filename}: data files must be CSV, Excel or JSON.")

        run_id = uuid.uuid4().hex[:12]
        folder = app_data_dir() / "runs" / run_id
        (folder / "report").mkdir(parents=True)
        (folder / "data").mkdir()
        total = 0
        saved_reports, saved_data = [], []
        for files, sub, saved in ((report_files, "report", saved_reports), (data_files, "data", saved_data)):
            for f in files:
                name = _safe_name(f.filename or "file", saved)
                content = await f.read()
                total += len(content)
                if total > MAX_UPLOAD_BYTES:
                    shutil.rmtree(folder, ignore_errors=True)
                    raise HTTPException(413, "The uploaded files are too large.")
                (folder / sub / name).write_bytes(content)
                saved.append(name)

        run = Run(run_id, folder, (period_start, period_end), saved_reports, saved_data)
        runs[run_id] = run
        _prune(runs)
        threading.Thread(target=_work, args=(run, factory), daemon=True).start()
        return {"id": run_id}

    @app.get("/api/runs/{run_id}")
    def poll(run_id: str, since: int = 0):
        run = get_run(run_id)
        snap = run.session.snapshot(0) if run.session else {"events": [], "tables": [], "notes": [], "queries_used": 0}
        events = run.pre_events + snap["events"]
        status = run.status if run.session is None else _merge_status(run, snap["status"])
        return {
            "id": run.id, "status": status, "error": run.error or snap.get("error"),
            "period": run.period, "report_files": run.report_names, "data_files": run.data_names,
            "import_summary": run.import_summary,
            "events": events[since:], "event_count": len(events),
            "tables": snap["tables"], "notes": snap["notes"], "queries_used": snap["queries_used"],
        }

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str):
        run = get_run(run_id)
        run.cancel_requested = True
        if run.session:
            run.session.cancel()
        return {"ok": True}

    @app.post("/api/runs/{run_id}/correct")
    def correct(run_id: str, body: CorrectionIn):
        run = get_run(run_id)
        if not run.session or run.session.status == "running" or run.status == "importing":
            raise HTTPException(409, "The report is still being prepared.")
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "Describe the correction.")
        run.cancel_requested = False
        run.status = "running"
        run.session.status = "running"
        threading.Thread(target=_correct, args=(run, text[:4000]), daemon=True).start()
        return {"ok": True}

    @app.get("/api/runs/{run_id}/details")
    def details(run_id: str):
        run = get_run(run_id)
        log = run.session.gate.log if run.session else []
        return {"schema": run.schema_text, "queries": [
            {"id": q.query_id, "purpose": q.purpose, "sql": q.sql, "columns": q.columns, "rows": q.rows[:200],
             "row_count": len(q.rows), "ms": q.elapsed_ms} for q in log]}

    @app.get("/api/runs/{run_id}/export.{fmt}")
    def export(run_id: str, fmt: str):
        run = get_run(run_id)
        if not run.session or not run.session.tables:
            raise HTTPException(409, "There are no tables to export yet.")
        tables, notes = run.session.tables, list(run.session.notes.values())
        title = "Grant report tables"
        period = f"{run.period[0]} to {run.period[1]}"
        stamp = run.period[1].replace("-", "")
        if fmt == "docx":
            data = to_docx(tables, notes, title, period)
            media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        elif fmt == "xlsx":
            data = to_xlsx(tables, notes, title, period)
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            raise HTTPException(404, "Unknown format.")
        return Response(data, media_type=media, headers={
            "Content-Disposition": f'attachment; filename="grant-report-tables-{stamp}.{fmt}"'})

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


# ---------------------------------------------------------------- workers

def _event(run: Run, kind: str, message: str):
    run.pre_events.append({"at": time.time(), "kind": kind, "message": message})


def _work(run: Run, factory):
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
            return
        gate = QueryGate(db, catalog)
        model = factory()
        run.session = ReportSession(gate, run.schema_text, blocks, run.period, model)
        run.status = "running"
        if run.cancel_requested:
            run.session.cancel()
        run.session.run()
        run.status = run.session.status
    except Exception as e:
        log.exception("Report run %s failed", run.id)
        run.status = "failed"
        run.error = str(e) if isinstance(e, (RuntimeError, ValueError)) else f"Could not prepare the report ({type(e).__name__})."
        _event(run, "error", run.error)


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


def _prune(runs: dict[str, Run]):
    """Keep only the most recent runs on disk and in memory."""
    root = app_data_dir() / "runs"
    if not root.exists():
        return
    folders = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in folders[KEEP_RUNS:]:
        runs.pop(old.name, None)
        shutil.rmtree(old, ignore_errors=True)


app = create_app()


def main():
    import webbrowser

    import uvicorn

    port = 8765
    threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
