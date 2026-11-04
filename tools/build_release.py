"""Build the downloadable release zip from the committed program files only.

    python tools/build_release.py

Writes dist/grant-reporting-assistant-<version>.zip (one top-level folder) and
prints its size and SHA-256. Only tracked files on an allowlist are packed, so
tests, local data, uploads, keys and test runs can never end up in the zip.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOP_FILES = {"start.bat", "pyproject.toml", "LICENSE", "README.md"}
PACKAGE = "gra/"
BLOCKED = re.compile(r"\.(db|sqlite3?|csv|xlsx?|docx|pdf|env|key|pem|log)$|__pycache__|(^|/)\.", re.I)
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def version() -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
    return m.group(1)


def program_files() -> list[str]:
    tracked = git("ls-files").splitlines()
    files = [f for f in tracked if f in TOP_FILES or f.startswith(PACKAGE)]
    bad = [f for f in files if BLOCKED.search(f)]
    if bad:
        sys.exit(f"Refusing to pack: {bad}")
    return sorted(files)


def main() -> None:
    if git("status", "--porcelain", "--untracked-files=no").strip():
        sys.exit("Commit or discard changes first; the zip must match a commit.")
    ver = version()
    name = f"grant-reporting-assistant-{ver}"
    out = ROOT / "dist" / f"{name}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in program_files():
            data = (ROOT / f).read_bytes()
            if f.endswith(".bat"):
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            info = zipfile.ZipInfo(f"{name}/{f}", FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"{out.relative_to(ROOT)}\n{out.stat().st_size:,} bytes\nSHA-256 {digest}\ncommit {git('rev-parse', 'HEAD').strip()}")


if __name__ == "__main__":
    main()
