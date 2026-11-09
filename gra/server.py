"""Local web server: Upload -> Prepare automatically -> Review.

Binds to 127.0.0.1 only. Uploads, databases and exports are kept in the local
app-data folder, never in the repository. What each request does lives in api.py,
which the browser version (web/) shares.
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.requests import Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .api import MAX_CORRECTION, Api, ApiError, FileOut, Upload  # noqa: F401  (MAX_CORRECTION: tests)

STATIC = Path(__file__).parent / "static"


class KeyIn(BaseModel):
    key: str
    provider: str = "anthropic"


class ProviderIn(BaseModel):
    provider: str


class CorrectionIn(BaseModel):
    text: str


def _download(f: FileOut) -> Response:
    return Response(f.data, media_type=f.media,
                    headers={"Content-Disposition": f'attachment; filename="{f.filename}"'})


async def _uploads(files: list[UploadFile]) -> list[Upload]:
    return [Upload(f.filename or "file", await f.read()) for f in files]


def create_app(model_factory=None) -> FastAPI:
    """model_factory() returns a model client; tests pass a scripted one."""
    app = FastAPI(title="Grant Reporting Assistant", version=__version__)
    # Only this computer's own address: stops other web pages reaching the app by DNS tricks.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    api = Api(model_factory)
    app.state.api = api

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    @app.middleware("http")
    async def page_files_stay_current(request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # ------------------------------------------------------------ settings

    @app.get("/api/status")
    def status():
        return api.status()

    @app.post("/api/key")
    def set_key(body: KeyIn):
        return api.set_key(body.key, body.provider)

    @app.delete("/api/key/{provider}")
    def forget_key(provider: str):
        return api.forget_key(provider)

    @app.post("/api/settings-seen")
    def settings_were_seen():
        return api.settings_seen()

    @app.post("/api/provider")
    def choose_provider(body: ProviderIn):
        return api.choose_provider(body.provider)

    # ------------------------------------------------------------ runs

    @app.post("/api/runs")
    async def start_run(report_files: list[UploadFile] = File(...), data_files: list[UploadFile] = File(...),
                        period_start: str = Form(...), period_end: str = Form(...), saved_choices: str = Form("")):
        return api.start_run(await _uploads(report_files), await _uploads(data_files), period_start, period_end,
                             saved_choices)

    @app.get("/api/saved-choices")
    def list_choices():
        return api.list_choices()

    @app.post("/api/runs/{run_id}/save-choices")
    def save_choices(run_id: str):
        return api.save_choices(run_id)

    @app.delete("/api/saved-choices/{choice_id}")
    def delete_choices(choice_id: str):
        return api.delete_choices(choice_id)

    @app.post("/api/demo")
    def start_demo():
        return api.start_demo()

    @app.get("/api/runs/{run_id}")
    def poll(run_id: str, since: int = 0):
        return api.poll(run_id, since)

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str):
        return api.cancel(run_id)

    @app.post("/api/runs/{run_id}/correct")
    def correct(run_id: str, body: CorrectionIn):
        return api.correct(run_id, body.text)

    @app.delete("/api/runs/{run_id}")
    def delete_run(run_id: str):
        return api.delete_run(run_id)

    @app.delete("/api/runs")
    def delete_all_runs():
        return api.delete_all_runs()

    @app.post("/api/preview")
    async def preview(data_files: list[UploadFile] = File(...)):
        return api.preview(await _uploads(data_files))

    @app.get("/api/runs/{run_id}/details")
    def details(run_id: str):
        return api.details(run_id)

    @app.get("/api/runs/{run_id}/activity-report")
    def activity_report(run_id: str):
        return _download(api.activity_report(run_id))

    @app.get("/api/runs/{run_id}/activity-log")
    def activity_log(run_id: str):
        return _download(api.activity_log(run_id))

    @app.get("/api/runs/{run_id}/export.{fmt}")
    def export(run_id: str, fmt: str):
        return _download(api.export(run_id, fmt))

    @app.get("/api/runs/{run_id}/data-quality")
    def data_quality(run_id: str):
        return _download(api.data_quality(run_id))

    @app.get("/api/sample")
    def sample():
        return api.sample()

    @app.get("/api/sample/{name}")
    def sample_file(name: str):
        return FileResponse(api.sample_file(name))

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


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
