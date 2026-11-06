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
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .activity import read_events, render_text, sent_text
from .activity_report import to_report_docx
from .documents import layout_summary, scan_text, to_blocks
from .engine import ReportSession
from .data_quality import has_gaps, to_quality_docx
from .exports import to_docx, to_xlsx
from .gate import QueryGate
from .importer import READERS, import_files, load_catalog
from .paths import app_data_dir
from .schema import describe

log = logging.getLogger("gra")
STATIC = Path(__file__).parent / "static"
DOC_TYPES = {".pdf", ".docx", ".xlsx", ".xlsm", ".txt", ".md", ".csv", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
KEEP_RUNS = 5


class KeyIn(BaseModel):
    key: str
    provider: str = "anthropic"


class ProviderIn(BaseModel):
    provider: str


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

    @property
    def current_period(self) -> tuple[str, str]:
        """The period the tables cover: as entered, unless a correction changed it."""
        return tuple(self.session.period) if self.session else self.period


def create_app(model_factory=None) -> FastAPI:
    """model_factory() returns a model client; tests pass a scripted one."""
    app = FastAPI(title="Grant Reporting Assistant", version=__version__)
    # Only this computer's own address: stops other web pages reaching the app by DNS tricks.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    runs: dict[str, Run] = {}

    @app.middleware("http")
    async def page_files_stay_current(request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    def default_factory():
        from .llm import make_client

        return make_client()

    factory = model_factory or default_factory

    def get_run(run_id: str) -> Run:
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(404, "Unknown report run.")
        return run

    # ------------------------------------------------------------ settings

    @app.get("/api/status")
    def status():
        from .llm import PROVIDERS, active_provider, key_status, settings_seen

        keys = key_status()
        provider = active_provider()
        return {"version": __version__, "provider": provider,
                "has_key": model_factory is not None or keys[provider], "keys": keys,
                # Open Settings once, so people upgrading with a saved key see the choice of AI service.
                "show_settings": model_factory is None and not settings_seen(),
                "providers": [{"id": k, "label": v["label"]} for k, v in PROVIDERS.items()]}

    @app.post("/api/key")
    def set_key(body: KeyIn):
        from .llm import PROVIDERS, check_api_key, save_api_key, set_provider

        if body.provider not in PROVIDERS:
            raise HTTPException(400, "Choose Anthropic or OpenAI.")
        key = body.key.strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-]{20,300}", key):
            raise HTTPException(400, "That does not look like an API key.")
        problem = check_api_key(key, body.provider)
        if problem:
            raise HTTPException(400, problem)
        save_api_key(key, body.provider)
        set_provider(body.provider)  # a newly saved key means the user wants to use that service
        return {"ok": True}

    @app.post("/api/settings-seen")
    def settings_were_seen():
        from .llm import mark_settings_seen

        mark_settings_seen()
        return {"ok": True}

    @app.post("/api/provider")
    def choose_provider(body: ProviderIn):
        """Switch to a service whose key is already saved. New reports use it; a running one keeps its own."""
        from .llm import PROVIDERS, key_status, set_provider

        if body.provider not in PROVIDERS:
            raise HTTPException(400, "Choose Anthropic or OpenAI.")
        if not key_status()[body.provider]:
            raise HTTPException(400, f"Add a {PROVIDERS[body.provider]['label']} API key first.")
        set_provider(body.provider)
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
        snap = run.session.snapshot(0) if run.session else {"events": [], "tables": [], "notes": [], "report": {}, "queries_used": 0}
        events = run.pre_events + snap["events"]
        status = run.status if run.session is None else _merge_status(run, snap["status"])
        return {
            "id": run.id, "status": status, "error": run.error or snap.get("error"),
            "period": list(run.current_period), "report_files": run.report_names, "data_files": run.data_names,
            "import_summary": run.import_summary,
            "events": events[since:], "event_count": len(events),
            "tables": snap["tables"], "notes": snap["notes"], "report": snap["report"], "queries_used": snap["queries_used"],
            "data_gaps": bool(run.session and run.session.tables
                              and has_gaps(run.session.tables, list(run.session.notes.values()))),
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
        if run.session.gate.contains_protected(text):
            raise HTTPException(400, "The correction mentions a name, ID or note from your client data, which would be "
                                     "sent to the AI. Describe the change without it.")
        run.cancel_requested = False
        run.status = "running"
        run.session.status = "running"
        threading.Thread(target=_correct, args=(run, text[:4000]), daemon=True).start()
        return {"ok": True}

    @app.delete("/api/runs/{run_id}")
    def delete_run(run_id: str):
        """Remove a run's uploads, database, activity record and exports from this computer."""
        if not re.fullmatch(r"[a-f0-9]{12}", run_id):
            raise HTTPException(404, "Unknown report run.")
        run = runs.pop(run_id, None)
        if run and run.session:
            run.session.cancel()
        _remove(app_data_dir() / "runs" / run_id)
        return {"ok": True}

    @app.delete("/api/runs")
    def delete_all_runs():
        for run in list(runs.values()):
            if run.session:
                run.session.cancel()
        runs.clear()
        root = app_data_dir() / "runs"
        count = 0
        if root.exists():
            for folder in root.iterdir():
                if folder.is_dir():
                    _remove(folder)
                    count += 1
        return {"ok": True, "deleted": count}

    @app.post("/api/preview")
    async def preview(data_files: list[UploadFile] = File(...)):
        """Import the exports and show exactly what the AI would be told about them. Nothing is sent."""
        for f in data_files:
            if Path(f.filename or "").suffix.lower() not in READERS:
                raise HTTPException(400, f"{f.filename}: data files must be CSV, Excel or JSON.")
        folder = app_data_dir() / "preview" / uuid.uuid4().hex[:12]
        folder.mkdir(parents=True)
        try:
            names, total = [], 0
            for f in data_files:
                name = _safe_name(f.filename or "file", names)
                content = await f.read()
                total += len(content)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "The uploaded files are too large.")
                (folder / name).write_bytes(content)
                names.append(name)
            db = folder / "data.db"
            catalog = import_files(db, [folder / n for n in names])
            return {"schema": describe(db, catalog), **_preview_groups(db, catalog)}
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        finally:
            _remove(folder)

    @app.get("/api/runs/{run_id}/details")
    def details(run_id: str):
        run = get_run(run_id)
        log = run.session.gate.log if run.session else []
        return {"schema": run.schema_text, "queries": [
            {"id": q.query_id, "purpose": q.purpose, "sql": q.sql, "columns": q.columns, "rows": q.rows[:200],
             "row_count": len(q.rows), "ms": q.elapsed_ms} for q in log]}

    def saved_activity(run_id: str):
        """A run's record and gate; also works after a restart, from the saved folder."""
        if not re.fullmatch(r"[a-f0-9]{12}", run_id):
            raise HTTPException(404, "Unknown report run.")
        folder = app_data_dir() / "runs" / run_id
        events = read_events(folder / "activity.jsonl")
        if not events:
            raise HTTPException(404, "No AI activity was recorded for this run.")
        run = runs.get(run_id)
        gate = run.session.gate if run and run.session else QueryGate(folder / "data.db", load_catalog(folder / "data.db"))
        return events, gate

    @app.get("/api/runs/{run_id}/activity-report")
    def activity_report(run_id: str):
        events, gate = saved_activity(run_id)
        data = to_report_docx(events, gate.catalog, gate.contains_protected, gate.protected_value_count())
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        headers={"Content-Disposition": f'attachment; filename="ai-activity-report-{run_id}.docx"'})

    @app.get("/api/runs/{run_id}/activity-log")
    def activity_log(run_id: str):
        events, gate = saved_activity(run_id)
        hits = [e["seq"] for e in events if sent_text(e) and gate.contains_protected(sent_text(e))]
        text = render_text(events, hits, gate.protected_value_count(), run_id)
        return Response(text, media_type="text/plain; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="ai-activity-log-{run_id}.txt"'})

    @app.get("/api/runs/{run_id}/export.{fmt}")
    def export(run_id: str, fmt: str):
        run = get_run(run_id)
        if not run.session or not run.session.tables:
            raise HTTPException(409, "There are no tables to export yet.")
        tables, notes = run.session.tables, list(run.session.notes.values())
        title = "Grant report tables"
        start, end = run.current_period
        period = f"{start} to {end}"
        stamp = end.replace("-", "")
        if fmt == "docx":
            data = to_docx(tables, notes, title, period, run.session.report, run.session.page_orientation)
            media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        elif fmt == "xlsx":
            data = to_xlsx(tables, notes, title, period, run.session.report, run.session.page_orientation)
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            raise HTTPException(404, "Unknown format.")
        return Response(data, media_type=media, headers={
            "Content-Disposition": f'attachment; filename="grant-report-tables-{stamp}.{fmt}"'})

    @app.get("/api/runs/{run_id}/data-quality")
    def data_quality(run_id: str):
        run = get_run(run_id)
        if not run.session or not run.session.tables:
            raise HTTPException(409, "There are no tables yet.")
        s = run.session
        data = to_quality_docx(s.tables, list(s.notes.values()), s.gate.catalog, [q.sql for q in s.gate.log],
                               s.report, tuple(run.current_period))
        stamp = run.current_period[1].replace("-", "")
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        headers={"Content-Disposition": f'attachment; filename="data-collection-guide-{stamp}.docx"'})

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
        _check_reports(run, gate)
        _event(run, "status", "Connecting to the AI service")
        model = factory()
        layout = layout_summary([run.folder / "report" / n for n in run.report_names])
        run.session = ReportSession(gate, run.schema_text, blocks, run.period, model,
                                    activity_path=run.folder / "activity.jsonl", layout=layout)
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


def _port_state(port: int) -> str:
    """'free', 'gra' when this app already answers on the port, or 'other'."""
    import socket
    import urllib.request

    with socket.socket() as s:
        s.settimeout(1)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return "free"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/", timeout=3) as r:
            return "gra" if b"<title>Grant Reporting Assistant</title>" in r.read(4096) else "other"
    except Exception:
        return "other"


def main():
    import webbrowser

    import uvicorn

    port = 8765
    url = f"http://127.0.0.1:{port}/"
    state = _port_state(port)
    if state == "gra":
        print("The Grant Reporting Assistant is already running. Opening it in the browser.")
        webbrowser.open(url)
        return
    if state == "other":
        raise SystemExit(f"Another program is using port {port}. Close it, then start the app again.")
    from .llm import active_provider

    sdk = "openai" if active_provider() == "openai" else "anthropic"
    threading.Thread(target=lambda: __import__(sdk), daemon=True).start()  # warm up the slow import
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
