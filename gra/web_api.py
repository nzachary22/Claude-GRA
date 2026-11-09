"""The browser version's stand-in for the local server.

The page's script sends each request that the desktop app would send to its
local server (/api/...) to a background worker running Python (Pyodide). The
worker calls handle() here, which routes it to the same api.Api methods the
desktop server uses. Long work (preparing a report, a correction) is queued and
run right after the reply; while it runs, progress is pushed to the page with
publish(), because the worker can't answer while it is busy.
"""

from __future__ import annotations

import json
import re
import time
from typing import Callable
from urllib.parse import parse_qs, unquote

from . import api as api_module
from .api import Api, ApiError, FileOut, Upload
from .activity import ActivityLog
from .engine import ReportSession
from .paths import app_data_dir

_jobs: list[tuple[Callable, tuple]] = []
_publisher: Callable[[str, dict], None] | None = None
_last_publish = 0.0


def _spawn(fn, *args):
    if fn is api_module._work:
        # In two steps, so the imported data is saved in the browser before the first AI request.
        run, factory = args
        _jobs.append((api_module._prepare, (run, factory)))
        _jobs.append((_continue, (run,)))
    else:
        _jobs.append((fn, args))


def _continue(run):
    if run.session is not None and run.status == "running":
        api_module._run_session(run)


API = Api(spawn=_spawn)


def set_publisher(fn: Callable[[str, dict], None]) -> None:
    """fn(run_id, poll_reply) is called with progress while a job runs."""
    global _publisher
    _publisher = fn


def publish(force: bool = False) -> None:
    global _last_publish
    if _publisher is None or (not force and time.monotonic() - _last_publish < 0.25):
        return
    _last_publish = time.monotonic()
    for run_id in list(API.runs):
        run = API.runs[run_id]
        if force or run.status in ("importing", "running") or (run.session and run.session.status == "running"):
            _publisher(run_id, API.poll(run_id, 0))


# Progress: every new event in a run is pushed to the page.
_original_emit = ReportSession._emit
_original_event = api_module._event


def _emit(self, kind, message):
    _original_emit(self, kind, message)
    publish()


def _event(run, kind, message):
    _original_event(run, kind, message)
    publish()


ReportSession._emit = _emit
api_module._event = _event

# The AI record: each line is also handed to the page as it is written. The worker can only save to the
# browser's storage between steps, so if a report is stopped mid-request the page gives the record back.
_activity_hook: Callable[[str, str], None] | None = None
_original_add = ActivityLog.add


def set_activity_hook(fn: Callable[[str, str], None]) -> None:
    global _activity_hook
    _activity_hook = fn


def _add(self, kind, direction, **fields):
    _original_add(self, kind, direction, **fields)
    if _activity_hook and self.path:
        _activity_hook(self.path.parent.name, json.dumps(self.events[-1], default=str))


ActivityLog.add = _add


def restore_activity(run_id: str, lines: list[str]) -> dict:
    """Write back the AI record of a run that was stopped while the worker was busy."""
    if not re.fullmatch(r"[a-f0-9]{12}", run_id or ""):
        raise ApiError(404, "Unknown report run.")
    folder = app_data_dir() / "runs" / run_id
    if not folder.exists():
        raise ApiError(404, "Unknown report run.")
    from .activity import read_events

    events = read_events(folder / "activity.jsonl")  # what was saved before the stop
    known = {e.get("seq") for e in events}
    events += [e for e in (json.loads(line) for line in lines) if e.get("seq") not in known]
    events.sort(key=lambda e: e.get("seq") or 0)
    events.append({"seq": len(events) + 1, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "direction": "local",
                   "kind": "cancelled", "note": "Stopped at your request while the assistant was working"})
    (folder / "activity.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return {"ok": True}


def run_job() -> bool:
    """Run the next piece of queued long work. Returns True if anything ran."""
    if not _jobs:
        return False
    fn, args = _jobs.pop(0)
    try:
        fn(*args)
    finally:
        publish(force=True)
    return True


def run_jobs() -> bool:
    ran = False
    while run_job():
        ran = True
    return ran


def has_jobs() -> bool:
    return bool(_jobs)


def job_run_id() -> str | None:
    """The run the next queued job belongs to."""
    if not _jobs:
        return None
    run = _jobs[0][1][0]
    return getattr(run, "id", None)


# ---------------------------------------------------------------- routing

_RUN = r"(?P<run>[^/]+)"
ROUTES = [
    ("GET", r"/api/status", lambda m, q, b, f: API.status()),
    ("POST", r"/api/key", lambda m, q, b, f: API.set_key(b.get("key", ""), b.get("provider", "anthropic"))),
    ("DELETE", r"/api/key/(?P<p>[^/]+)", lambda m, q, b, f: API.forget_key(m["p"])),
    ("POST", r"/api/settings-seen", lambda m, q, b, f: API.settings_seen()),
    ("POST", r"/api/provider", lambda m, q, b, f: API.choose_provider(b.get("provider", ""))),
    ("POST", r"/api/runs", lambda m, q, b, f: API.start_run(
        f.get("report_files", []), f.get("data_files", []), _form(b, "period_start"), _form(b, "period_end"),
        _form(b, "saved_choices"))),
    ("GET", r"/api/saved-choices", lambda m, q, b, f: API.list_choices()),
    ("POST", rf"/api/runs/{_RUN}/save-choices", lambda m, q, b, f: API.save_choices(m["run"])),
    ("DELETE", r"/api/saved-choices/(?P<c>[^/]+)", lambda m, q, b, f: API.delete_choices(m["c"])),
    ("POST", r"/api/demo", lambda m, q, b, f: API.start_demo()),
    ("GET", rf"/api/runs/{_RUN}", lambda m, q, b, f: API.poll(m["run"], int(q.get("since", "0") or 0))),
    ("POST", rf"/api/runs/{_RUN}/cancel", lambda m, q, b, f: API.cancel(m["run"])),
    ("POST", rf"/api/runs/{_RUN}/correct", lambda m, q, b, f: API.correct(m["run"], b.get("text", ""))),
    ("DELETE", rf"/api/runs/{_RUN}", lambda m, q, b, f: API.delete_run(m["run"])),
    ("DELETE", r"/api/runs", lambda m, q, b, f: API.delete_all_runs()),
    ("POST", r"/api/preview", lambda m, q, b, f: API.preview(f.get("data_files", []))),
    ("GET", rf"/api/runs/{_RUN}/details", lambda m, q, b, f: API.details(m["run"])),
    ("GET", rf"/api/runs/{_RUN}/activity-report", lambda m, q, b, f: API.activity_report(m["run"])),
    ("GET", rf"/api/runs/{_RUN}/activity-log", lambda m, q, b, f: API.activity_log(m["run"])),
    ("GET", rf"/api/runs/{_RUN}/export\.(?P<fmt>[a-z]+)", lambda m, q, b, f: API.export(m["run"], m["fmt"])),
    ("GET", rf"/api/runs/{_RUN}/data-quality", lambda m, q, b, f: API.data_quality(m["run"])),
    ("POST", rf"/api/web/restore-activity/{_RUN}", lambda m, q, b, f: restore_activity(m["run"], b.get("lines") or [])),
    ("GET", r"/api/sample", lambda m, q, b, f: API.sample()),
    ("GET", r"/api/sample/(?P<name>[^/]+)", lambda m, q, b, f: _sample_file(m["name"])),
]


def _form(body: dict, name: str) -> str:
    v = body.get(name, "")
    return v[0] if isinstance(v, list) else str(v or "")


def _sample_file(name: str) -> FileOut:
    path = API.sample_file(unquote(name))
    media = "application/pdf" if path.suffix == ".pdf" else "text/csv"
    return FileOut(path.read_bytes(), media, path.name)


def handle(method: str, url: str, body_json: str = "", files: list | None = None) -> dict:
    """One request. files: [(field, name, bytes)]. Returns {status, json} or {status, file: (bytes, media, name)}."""
    path, _, query = url.partition("?")
    q = {k: v[-1] for k, v in parse_qs(query).items()}
    try:
        body = json.loads(body_json) if body_json else {}
    except ValueError:
        body = {}
    uploads: dict[str, list[Upload]] = {}
    for field_name, name, content in files or []:
        data = content.to_bytes() if hasattr(content, "to_bytes") else bytes(content)  # a browser array
        uploads.setdefault(field_name, []).append(Upload(str(name), data))
    for verb, pattern, fn in ROUTES:
        m = re.fullmatch(pattern, path)
        if m and verb == method.upper():
            try:
                out = fn(m.groupdict(), q, body, uploads)
            except ApiError as e:
                return {"status": e.status, "json": {"detail": e.detail}}
            except Exception as e:  # never leave the page waiting
                api_module.log.exception("Request %s %s failed", method, path)
                return {"status": 500, "json": {"detail": f"Something went wrong ({type(e).__name__})."}}
            if isinstance(out, FileOut):
                return {"status": 200, "file": (out.data, out.media, out.filename)}
            return {"status": 200, "json": out}
    return {"status": 404, "json": {"detail": "Not found."}}
