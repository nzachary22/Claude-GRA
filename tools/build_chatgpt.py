"""Build the ChatGPT app installer: a zip with one-double-click installers for Windows and Mac.

    python tools/build_chatgpt.py [--version 2.4.0-test.1]

Writes dist/grant-reporting-assistant-chatgpt-<version>.zip and prints its size and SHA-256.
It holds one folder, "Grant Reporting Assistant for ChatGPT", with:

- Install on Windows.bat and Install on Mac.command, which find Python 3.11+ and run files/install.py;
- Uninstall/, the same with --remove;
- How to install.txt, the short version of connector/CHATGPT.md;
- files/install.py and files/server/, the same connector as the Claude app extension
  (built by tools/build_connector.py), started by ChatGPT with --client chatgpt.
"""

from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_connector import libraries, report, server_files, zip_adder  # noqa: E402
from build_release import ROOT, version  # noqa: E402

TOP = "Grant Reporting Assistant for ChatGPT"
SOURCE = ROOT / "connector" / "chatgpt"
# (name in the zip, source file, executable, Windows line endings)
LAUNCHERS = [("Install on Windows.bat", "install-windows.bat", False, True),
             ("Install on Mac.command", "install-mac.command", True, False),
             ("Uninstall/Uninstall on Windows.bat", "uninstall-windows.bat", False, True),
             ("Uninstall/Uninstall on Mac.command", "uninstall-mac.command", True, False),
             ("How to install.txt", "how-to-install.txt", False, True),
             ("files/install.py", "install.py", False, False)]


def _lines(data: bytes, windows: bool) -> bytes:
    data = data.replace(b"\r\n", b"\n")
    return data.replace(b"\n", b"\r\n") if windows else data


def build(dest: Path = ROOT / "dist", lib_cache: Path | None = None, ver: str | None = None) -> Path:
    ver = ver or version()
    out = dest / f"grant-reporting-assistant-chatgpt-{ver}.zip"
    out.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        lib = libraries(Path(tmp), lib_cache)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            add = zip_adder(z, unix=True)
            for name, source, executable, windows in LAUNCHERS:
                add(f"{TOP}/{name}", _lines((SOURCE / source).read_bytes(), windows), executable)
            add(f"{TOP}/files/LICENSE", (ROOT / "LICENSE").read_bytes())
            for name, data, executable in server_files(lib):
                add(f"{TOP}/files/server/{name}", data, executable)
    report(out)
    return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build the ChatGPT app installer zip.")
    parser.add_argument("--version", help="version to stamp instead of pyproject's, e.g. 2.4.0-test.1")
    build(ver=parser.parse_args().version)
