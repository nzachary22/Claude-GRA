"""Build the browser version (Grant Reporting Assistant in a web page, nothing to install).

    python tools/build_web.py

Writes dist/web/ (the files to put on the website, in their own folder such as /app/)
and dist/grant-reporting-assistant-web-<version>.zip, and prints the zip's size and
SHA-256.

What goes in:
- the page: gra/static/index.html, app.js, style.css, adapted for the browser, plus web/;
- the app's own Python code (gra/*.py) and the fictional Juniper Ridge sample, as app.zip;
- Python for the browser (Pyodide) and the few libraries the app uses, downloaded from their
  official sources and checked against the fingerprints pinned below. Nothing is fetched by
  the page from anywhere else, so the site's security policy can allow only this site and the
  two AI services.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
CACHE = DIST / ".web-cache"
OUT = DIST / "web"
WEB = ROOT / "web"
STATIC = ROOT / "gra" / "static"

PYODIDE = "0.29.5"
ENGINE_DIR = f"pyodide-{PYODIDE}"  # versioned, so browsers can keep it cached for a long time
# Pyodide's core files (npm package "pyodide"), checked against these fingerprints.
CORE = {
    "pyodide.js": "7f832a350240263d9946a9c3c877f7bcdab6c37d6dc65f72cd3be5905dca62dd",
    "pyodide.asm.js": "356c42f69e1695397e9d8670bd3c2e678248cde76e18bdae1731e848537b47d7",
    "pyodide.asm.wasm": "54309a5a2cfd757b1f0fdbc9093c92503f7b341500110329d932487183912718",
    "python_stdlib.zip": "831fd1e535084b972f87c6a35275de3d236efd5166f5e77ce89497710635e73c",
    "pyodide-lock.json": "14d2c2dba101277999e17135e653d8f15389ad1437f53eae213bf0c3cdff723d",
}
# Built for the browser by Pyodide; fingerprints come from the checked pyodide-lock.json.
PYODIDE_PACKAGES = ["sqlite3", "lxml", "typing-extensions"]
# Plain Python libraries from PyPI, the versions the desktop app is tested with.
PYPI = {
    "sqlglot": ("30.30.0", "06c4c212a7868c7dcff0fd0fda348dd3c5cbad40137b3083a70ecdfe22921cf9"),
    "openpyxl": ("3.1.5", "5282c12b107bffeef825f4617dc029afaf41d0ea60823bbb665ef3079dc79de2"),
    "et-xmlfile": ("2.0.0", "7a91720bc756843502c3b7504c77b8fe44217c85c537d85037f0f536151b2caa"),
    "python-docx": ("1.2.0", "3fd478f3250fbbbfd3b94fe1e985955737c145627498896a8a6bf81f4baf66c7"),
    "pypdf": ("7.0.0", "112a2c7cbabefd8dfe498f7a13977e575e762d58cdb0d832acb8886922602c6f"),
}
SAMPLE = ["Juniper Ridge 2025 annual report.pdf", "README.txt", "case_events.csv", "cases.csv", "clients.csv",
          "demographic_responses.csv", "outcome_codes.csv", "problem_codes.csv"]
FIXED_TIME = (2026, 1, 1, 0, 0, 0)
# Where the app sits on the website. The _headers file (Netlify's format) uses it.
BASE_PATH = "/app"
# The page may load only its own files and talk only to the two AI services; the browser enforces this.
CSP = ("default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self'; "
       "connect-src 'self' https://api.anthropic.com https://api.openai.com; img-src 'self' data: blob:; "
       "style-src 'self' 'unsafe-inline'; frame-src blob:; object-src 'none'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")
HEADERS = {
    "Content-Security-Policy": CSP,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def headers_file(base: str) -> str:
    lines = [f"{base}/*"] + [f"  {k}: {v}" for k, v in HEADERS.items()]
    lines += ["", f"{base}/{ENGINE_DIR}/*", "  Cache-Control: public, max-age=31536000, immutable", "",
              f"{base}/index.html", "  Cache-Control: no-cache", "", f"{base}/", "  Cache-Control: no-cache", ""]
    return "\n".join(lines)


def version() -> str:
    return re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "grant-reporting-assistant-build"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def cached(name: str, expected: str, sources) -> bytes:
    """A file from the cache or the first source that works, only if its fingerprint matches."""
    path = CACHE / name
    if path.exists() and sha256(path.read_bytes()) == expected:
        return path.read_bytes()
    problems = []
    for source in sources:
        try:
            data = source()
        except Exception as e:  # try the next source
            problems.append(f"{e}")
            continue
        if data is None:
            continue
        if sha256(data) != expected:
            problems.append(f"fingerprint mismatch for {name}")
            continue
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data
    raise SystemExit(f"Could not get {name}: {'; '.join(problems) or 'no source'}")


_npm_tar: dict[str, bytes] = {}


def from_npm(name: str):
    def get():
        if not _npm_tar:
            meta = json.loads(fetch(f"https://registry.npmjs.org/pyodide/{PYODIDE}"))
            with tarfile.open(fileobj=io.BytesIO(fetch(meta["dist"]["tarball"]))) as t:
                for m in t.getmembers():
                    if m.isfile():
                        _npm_tar[Path(m.name).name] = t.extractfile(m).read()
        return _npm_tar.get(name)
    return get


def from_cdn(name: str):
    return lambda: fetch(f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE}/full/{name}")


_full_tar: dict[str, bytes] = {}


def from_github_release(name: str, wanted: set[str]):
    """The full Pyodide release from GitHub (about 400 MB), read once for all the files needed."""
    def get():
        if name not in _full_tar:
            url = f"https://github.com/pyodide/pyodide/releases/download/{PYODIDE}/pyodide-{PYODIDE}.tar.bz2"
            print("Downloading the full Pyodide release from GitHub (about 400 MB, once)…")
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "gra-build"}), timeout=1800) as r:
                with tarfile.open(fileobj=r, mode="r|bz2") as t:
                    for m in t:
                        base = Path(m.name).name
                        if m.isfile() and base in wanted:
                            _full_tar[base] = t.extractfile(m).read()
        return _full_tar.get(name)
    return get


def from_pypi(project: str, ver: str):
    def get():
        meta = json.loads(fetch(f"https://pypi.org/pypi/{project}/{ver}/json"))
        wheel = next(u for u in meta["urls"] if u["filename"].endswith("-none-any.whl"))
        return fetch(wheel["url"])
    return get


def engine_files() -> dict[str, bytes]:
    files = {}
    for name, digest in CORE.items():
        files[name] = cached(name, digest, [from_npm(name), from_cdn(name)])
    lock = json.loads(files["pyodide-lock.json"])
    wanted = {lock["packages"][p]["file_name"] for p in PYODIDE_PACKAGES}
    for p in PYODIDE_PACKAGES:
        info = lock["packages"][p]
        name = info["file_name"]
        files[name] = cached(name, info["sha256"], [from_cdn(name), from_github_release(name, wanted)])
    return files


def library_wheels() -> dict[str, bytes]:
    out = {}
    for project, (ver, digest) in PYPI.items():
        name = f"{project.replace('-', '_')}-{ver}.whl"
        out[name] = cached(name, digest, [from_pypi(project, ver)])
    return out


def app_zip() -> bytes:
    """The app's Python code and the fictional sample, as the browser unpacks them."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted((ROOT / "gra").glob("*.py")):
            info = zipfile.ZipInfo(f"gra/{path.name}", FIXED_TIME)
            z.writestr(info, path.read_bytes(), zipfile.ZIP_DEFLATED)
        for name in SAMPLE:
            info = zipfile.ZipInfo(f"gra/sample/{name}", FIXED_TIME)
            z.writestr(info, (ROOT / "gra" / "sample" / name).read_bytes(), zipfile.ZIP_DEFLATED)
    return buf.getvalue()


# The shared page, changed only where the browser version differs. Each change must apply exactly once,
# so a later edit to the desktop page can't silently drop it.
PAGE_CHANGES = [
    ('href="/static/icon.ico"', 'href="static/icon.ico"'),
    (re.compile(r'href="/static/style\.css\?v=\d+"'), 'href="static/style.css?v={v}"'),
    (re.compile(r'<script src="/static/app\.js\?v=\d+"></script>'),
     '<script src="static/bridge.js?v={v}"></script>\n  <script src="static/app.js?v={v}"></script>'),
    ("it is stored in this computer's secure key store (Windows Credential Manager or the Mac Keychain).",
     "it is saved in this browser on this computer, and <b>Forget key</b> removes it."),
    ('<main>', '<div id="booting" class="card booting" role="status"><strong>Starting the assistant…</strong> '
               '<span class="muted">The first visit downloads about 15 MB; after that it opens in a few seconds. '
               'Nothing you add later is uploaded to this website.</span></div>\n\n  <main hidden>'),
]


def page(v: str) -> str:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for old, new in PAGE_CHANGES:
        new = new.replace("{v}", v)
        if isinstance(old, re.Pattern):
            html, n = old.subn(new, html)
        else:
            n = html.count(old)
            html = html.replace(old, new)
        if n != 1:
            raise SystemExit(f"The page changed: could not apply {old!r} exactly once ({n}).")
    return html


def write(path: Path, data: bytes | str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


def build() -> Path:
    v = version()
    if OUT.exists():
        shutil.rmtree(OUT)
    engine = engine_files()
    for name, data in engine.items():
        write(OUT / ENGINE_DIR / name, data)
    wheels = library_wheels()
    for name, data in wheels.items():
        write(OUT / ENGINE_DIR / "lib" / name, data)
    write(OUT / "app.zip", app_zip())
    write(OUT / "index.html", page(v))
    for name in ("app.js", "style.css", "icon.ico"):
        write(OUT / "static" / name, (STATIC / name).read_bytes())
    for path in sorted(WEB.rglob("*")):
        if path.is_file():
            write(OUT / path.relative_to(WEB), path.read_bytes())
    manifest = {"version": v, "engine": ENGINE_DIR, "packages": PYODIDE_PACKAGES,
                "libraries": sorted(wheels), "app": f"app.zip?v={v}"}
    write(OUT / "manifest.json", json.dumps(manifest, indent=1))
    write(OUT / "_headers", headers_file(BASE_PATH))

    zip_path = DIST / f"grant-reporting-assistant-web-{v}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(OUT.rglob("*")):
            if path.is_file():
                z.writestr(zipfile.ZipInfo(path.relative_to(OUT).as_posix(), FIXED_TIME), path.read_bytes(),
                           zipfile.ZIP_DEFLATED)
    data = zip_path.read_bytes()
    print(f"{OUT}  ({sum(p.stat().st_size for p in OUT.rglob('*') if p.is_file()):,} bytes)")
    print(f"{zip_path.name}  {len(data):,} bytes  SHA-256 {sha256(data)}")
    return zip_path


if __name__ == "__main__":
    sys.exit(build() and 0)
