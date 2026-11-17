"""Build the Claude app connector: a one-click Claude Desktop extension (.mcpb file).

    python tools/build_connector.py [--version 2.1.0-preview.2]

Writes dist/grant-reporting-assistant-<version>.mcpb and prints its size and SHA-256.
The bundle holds the connector's manifest and starter (connector/), the committed
app code with the fictional sample (gra/, the same allowlist as the release zip,
without the web page files), and the pure-Python libraries it needs (server/lib).
Word support needs lxml, a compiled library that differs by computer, so the
connector downloads the right one once on first start instead.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_release import FIXED_TIME, ROOT, program_files, version  # noqa: E402

# Pure-Python libraries bundled in server/lib, pinned. (lxml is installed on first start; see connector/server/main.py.)
LIBRARIES = ["sqlglot==30.32.0", "openpyxl==3.1.5", "et_xmlfile==2.0.0", "pypdf==7.1.0", "python-docx==1.2.0",
             "typing_extensions==4.16.0"]
CONNECTOR_FILES = ["manifest.json", "icon.png", "server/main.py", "server/start.sh"]
LEAVE_OUT = ("gra/static/",)  # the app's web page; the connector has none


def build(dest: Path = ROOT / "dist", lib_cache: Path | None = None, ver: str | None = None) -> Path:
    """ver overrides the version (default: pyproject's), e.g. 2.1.0-preview.2 for a test build, so the
    Claude app sees a new build as an update rather than the version already installed."""
    ver = ver or version()
    out = dest / f"grant-reporting-assistant-{ver}.mcpb"
    out.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        lib = Path(tmp) / "lib"
        if lib_cache and lib_cache.exists():
            shutil.copytree(lib_cache, lib)
        else:
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                            "--no-deps", "--only-binary=:all:", "--target", str(lib), *LIBRARIES], check=True)
            if lib_cache:
                shutil.copytree(lib, lib_cache)
        compiled = [p.name for p in lib.rglob("*") if p.suffix in (".so", ".pyd", ".dylib")]
        if compiled:
            sys.exit(f"A bundled library is not pure Python, so it would not work on every computer: {compiled}")
        manifest = json.loads((ROOT / "connector" / "manifest.json").read_text(encoding="utf-8"))
        manifest["version"] = ver
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            def add(arcname: str, data: bytes, executable: bool = False):
                info = zipfile.ZipInfo(arcname, FIXED_TIME)
                info.external_attr = (0o755 if executable else 0o644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, data)

            add("manifest.json", (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
            for f in CONNECTOR_FILES[1:]:
                add(f, (ROOT / "connector" / f).read_bytes(), executable=f.endswith(".sh"))
            add("LICENSE", (ROOT / "LICENSE").read_bytes())
            for f in program_files():
                if f.startswith("gra/") and not f.startswith(LEAVE_OUT):
                    add(f"server/{f}", (ROOT / f).read_bytes())
            for p in sorted(lib.rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                    add(f"server/lib/{p.relative_to(lib).as_posix()}", p.read_bytes())
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"{out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}  {out.stat().st_size:,} bytes  SHA-256 {digest}")
    return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build the Claude app extension (.mcpb).")
    parser.add_argument("--version", help="version to stamp instead of pyproject's, e.g. 2.1.0-preview.2")
    build(ver=parser.parse_args().version)
