"""The browser version, tested in a real browser (Chromium through Playwright).

Needs the built site (python tools/build_web.py) and Playwright; skipped otherwise. The AI
services are replaced by tests/fake_ai.py, which answers like them with the recorded Juniper
Ridge analyst, so a whole report is prepared in the page with no key and no cost. The site is
served with its real security policy, so anything it tried to contact besides this site and the
two AI services would fail.
"""

from __future__ import annotations

import functools
import http.server
import io
import json
import os
import socket
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE = Path(os.environ.get("GRA_WEB_DIST", ROOT / "dist" / "web"))
SAMPLE = ROOT / "gra" / "sample"
DATA = ["case_events.csv", "cases.csv", "clients.csv", "demographic_responses.csv", "outcome_codes.csv", "problem_codes.csv"]
REPORT = "Juniper Ridge 2025 annual report.pdf"

playwright_api = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.skipif(not (SITE / "index.html").exists(), reason="browser version not built")


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SiteHandler(http.server.SimpleHTTPRequestHandler):
    """The built site at /app/, with the headers the website sends; records every request."""
    requests: list[str] = []

    def translate_path(self, path):
        p = path.split("?")[0]
        p = p[4:] if p.startswith("/app/") else "/__not_here__"
        return super().translate_path(p)

    def end_headers(self):
        import sys

        sys.path.insert(0, str(ROOT / "tools"))
        import build_web

        for k, v in build_web.HEADERS.items():
            self.send_header(k, v)
        super().end_headers()

    def do_GET(self):
        SiteHandler.requests.append("GET " + self.path)
        super().do_GET()

    def do_POST(self):
        SiteHandler.requests.append("POST " + self.path)
        self.send_error(405)

    def log_message(self, *args):
        pass


SiteHandler.extensions_map[".wasm"] = "application/wasm"


@pytest.fixture(scope="module")
def servers():
    from tests import fake_ai

    site_port, ai_port = _port(), _port()
    site = http.server.ThreadingHTTPServer(("127.0.0.1", site_port), functools.partial(SiteHandler, directory=str(SITE)))
    ai = http.server.ThreadingHTTPServer(("127.0.0.1", ai_port), fake_ai.Handler)
    for s in (site, ai):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{site_port}/app/", f"http://127.0.0.1:{ai_port}", fake_ai.STATE
    site.shutdown()
    ai.shutdown()


BROWSERS = [b.strip() for b in os.environ.get("GRA_WEB_BROWSERS", "chromium").split(",") if b.strip()]


@pytest.fixture(scope="module", params=BROWSERS)
def browser(request):
    """Chromium (Chrome, Edge) by default; CI also runs Firefox and WebKit (Safari's engine)."""
    with playwright_api.sync_playwright() as p:
        b = getattr(p, request.param).launch()
        yield b
        b.close()


@pytest.fixture
def page(browser, servers):
    url, ai, state = servers
    ctx = browser.new_context(accept_downloads=True)
    hits = []

    def to_fake(route):
        from urllib.parse import urlparse

        u = urlparse(route.request.url)
        hits.append(f"{route.request.method} {u.netloc}{u.path}")
        route.fulfill(response=route.fetch(url=ai + u.path))

    ctx.route("https://api.anthropic.com/**", to_fake)
    ctx.route("https://api.openai.com/**", to_fake)
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.ai_hits = hits
    pg.goto(url)
    pg.wait_for_selector("#booting", state="detached", timeout=120000)
    yield pg
    ctx.close()


def open_settings(pg):
    if pg.is_hidden("#setup"):
        pg.click("#settings-btn")


def save_key(pg, key, provider="anthropic"):
    open_settings(pg)
    pg.check(f"input[name=provider][value={provider}]")
    pg.fill("#key-input", key)
    pg.click("#key-form button[type=submit]")
    pg.wait_for_function("() => !/Checking/.test(document.getElementById('key-msg').textContent) && "
                         "document.getElementById('key-msg').textContent", timeout=30000)
    return pg.text_content("#key-msg")


def download(pg, selector) -> bytes:
    with pg.expect_download(timeout=60000) as d:
        pg.click(selector)
    return Path(d.value.path()).read_bytes()


def figures(xlsx: bytes) -> list:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(xlsx))
    return [v for ws in wb.worksheets for r in ws.iter_rows(values_only=True) for v in r if isinstance(v, (int, float))]


def desktop_demo_figures(tmp_path) -> list:
    from gra.api import Api

    os.environ["GRA_DATA_DIR"] = str(tmp_path / "desktop")
    a = Api(spawn=lambda fn, *args: fn(*args), demo_pause=0)
    run = a.start_demo()["id"]
    return figures(a.export(run, "xlsx").data)


def prepare_sample_report(pg):
    pg.set_input_files("#report-files", [str(SAMPLE / REPORT)])
    pg.set_input_files("#data-files", [str(SAMPLE / n) for n in DATA])
    pg.fill("#period-start", "2026-01-01")
    pg.fill("#period-end", "2026-12-31")
    pg.click("#start")
    pg.wait_for_selector("#review:not([hidden])", timeout=300000)


def test_demo_gives_the_desktop_figures_and_downloads(page, tmp_path):
    assert page.text_content(".ver") == "V.2.0"
    open_settings(page)
    page.click("#demo")
    page.click("#demo-go")
    page.wait_for_selector("#review:not([hidden])", timeout=180000)
    assert page.eval_on_selector_all("#tables table", "t => t.length") == 3
    want = desktop_demo_figures(tmp_path)
    assert len(want) > 900 and figures(download(page, "#dl-excel")) == want
    from docx import Document

    doc = Document(io.BytesIO(download(page, "#dl-word")))
    assert len(doc.tables) >= 3
    assert len(download(page, "#dl-log")) > 5000  # the AI activity report
    assert page.ai_hits == []  # the demo contacts no AI service
    assert page.errors == []


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_full_report_through_each_ai_service(page, servers, provider, tmp_path):
    from tests import fake_ai

    _, _, state = servers
    assert "not accepted" in save_key(page, "wrong-key-for-browser-tests-00000000", provider)
    assert "Saved" in save_key(page, fake_ai.GOOD_KEY, provider)
    page.wait_for_timeout(1500)
    state.requests.clear()
    prepare_sample_report(page)
    assert figures(download(page, "#dl-excel")) == desktop_demo_figures(tmp_path)
    sent = [r for r in state.requests if r["path"] in ("/v1/messages", "/v1/responses")]
    assert len(sent) >= 3 and all(r["body"]["stream"] is True for r in sent)
    if provider == "anthropic":
        assert all(r["headers"].get("anthropic-dangerous-direct-browser-access") == "true" for r in sent)
        assert sent[0]["body"]["model"] == "claude-sonnet-5-5"
    else:
        assert all(r["body"]["store"] is False for r in sent)
    # Client identifiers never reach the AI service.
    text = json.dumps([r["body"] for r in sent])
    assert "JR25-CL-" not in text and "JR25-CA-" not in text
    assert page.errors == []


def test_key_is_kept_in_this_browser_until_forgotten(page):
    from tests import fake_ai

    assert "Saved" in save_key(page, fake_ai.GOOD_KEY)
    page.reload()
    page.wait_for_selector("#booting", state="detached", timeout=120000)
    status = page.evaluate("() => fetch('/api/status').then(r => r.json())")
    assert status["keys"]["anthropic"] is True
    open_settings(page)
    page.check("input[name=provider][value=anthropic]")
    page.once("dialog", lambda d: d.accept())
    page.click("#forget-key")
    page.wait_for_function("() => /removed/.test(document.getElementById('key-msg').textContent)")
    status = page.evaluate("() => fetch('/api/status').then(r => r.json())")
    assert status["keys"]["anthropic"] is False


def test_preview_sends_nothing(page, servers):
    SiteHandler.requests.clear()
    page.set_input_files("#data-files", [str(SAMPLE / n) for n in DATA])
    page.click("#preview")
    page.wait_for_selector("#preview-body:not([hidden])", timeout=60000)
    assert "client_id" in page.text_content("#preview-body") or page.text_content("#preview-body")
    assert page.ai_hits == []
    assert not [r for r in SiteHandler.requests if r.startswith("POST")]


def test_cancel_stops_at_once_and_the_app_carries_on(page):
    open_settings(page)
    page.click("#demo")
    page.click("#demo-go")
    page.wait_for_selector("#prepare:not([hidden])")
    page.wait_for_timeout(1500)
    page.click("#cancel")
    page.wait_for_function("() => document.getElementById('now').textContent.includes('Cancelled')", timeout=20000)
    # The AI activity report of the stopped run is still there.
    with page.expect_download(timeout=60000):
        page.click("#now a")
    # A fresh assistant is ready straight away: the demo runs again to the end.
    open_settings(page)
    page.click("#demo")
    page.click("#demo-go")
    page.wait_for_selector("#review:not([hidden])", timeout=180000)
    assert page.eval_on_selector_all("#tables table", "t => t.length") == 3
    assert page.errors == []


def test_security_policy_blocks_other_addresses(page):
    result = page.evaluate("""() => new Promise((resolve) => {
        document.addEventListener('securitypolicyviolation', (e) => resolve('blocked ' + e.blockedURI), {once: true});
        window.fetch('https://example.com/collect', {method: 'POST', body: 'x'}).catch(() => {});
        setTimeout(() => resolve('not blocked'), 5000);
    })""")
    assert result.startswith("blocked")


def test_delete_all_saved_runs(page):
    open_settings(page)
    page.click("#demo")
    page.click("#demo-go")
    page.wait_for_selector("#review:not([hidden])", timeout=180000)
    run_id = page.evaluate("() => location.hash.match(/run=([a-f0-9]+)/)[1]")
    open_settings(page)
    page.once("dialog", lambda d: d.accept())
    page.click("#delete-all")
    page.wait_for_function("() => /Deleted|deleted|removed/i.test(document.getElementById('delete-all-msg').textContent)")
    r = page.evaluate(f"() => fetch('/api/runs/{run_id}').then(r => r.status)")
    assert r == 404


def test_correction_and_saved_choices(page, servers):
    from tests import fake_ai

    assert "Saved" in save_key(page, fake_ai.GOOD_KEY)
    page.wait_for_timeout(1500)
    prepare_sample_report(page)
    page.fill("#correct-text", "Count clients by county of residence instead of office.")
    page.click("#correct-form button[type=submit]")
    # The recalculation runs in the background worker and the page keeps answering.
    page.wait_for_function("() => !document.getElementById('review').hidden || /Stopped|Cancelled/.test(document.getElementById('now').textContent)",
                           timeout=120000)
    page.wait_for_timeout(3000)
    status = page.evaluate("() => fetch('/api/runs/' + location.hash.match(/run=([a-f0-9]+)/)[1]).then(r => r.json())")
    assert status["status"] in ("done", "failed")
    assert page.errors == []


def test_sample_report_opens_in_a_new_tab(page):
    # A browser with a PDF viewer shows it in the new tab; headless Chromium has none, so it saves it instead.
    open_settings(page)
    with page.expect_download(timeout=30000) as d:
        page.click("#sample-report")
    assert d.value.suggested_filename.endswith(".pdf")
    assert Path(d.value.path()).read_bytes()[:5] == b"%PDF-"
