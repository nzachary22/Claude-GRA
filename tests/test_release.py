"""The release zip holds program files only."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_release", ROOT / "tools" / "build_release.py")
build_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_release)


def test_release_contains_only_program_files():
    files = build_release.program_files()
    assert {"start.bat", "pyproject.toml", "LICENSE", "README.md", "gra/server.py", "gra/static/index.html"} <= set(files)
    assert not [f for f in files if f.startswith(("tests/", "tools/", ".")) or "__pycache__" in f]


def test_version_matches_package():
    from gra import __version__
    assert build_release.version() == __version__


def test_release_includes_shortcut_icon():
    files = build_release.program_files()
    assert "gra/static/icon.ico" in files
    bat = (ROOT / "start.bat").read_text()
    assert "gra\\static\\icon.ico" in bat and "Grant Reporting Assistant.lnk" in bat
    assert (ROOT / "gra" / "static" / "icon.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"


def test_release_includes_mac_starter(tmp_path):
    import zipfile

    files = build_release.program_files()
    assert {"start.command", "gra/static/icon.icns"} <= set(files)
    assert (ROOT / "gra" / "static" / "icon.icns").read_bytes()[:4] == b"icns"
    with zipfile.ZipFile(build_release.build(tmp_path)) as z:
        info = {i.filename.split("/", 1)[1]: i for i in z.infolist()}
        starter = info["start.command"]
        assert starter.create_system == 3 and (starter.external_attr >> 16) & 0o777 == 0o755
        assert b"\r\n" not in z.read(starter) and z.read(starter).startswith(b"#!/bin/bash\n")
        assert (info["start.bat"].external_attr >> 16) & 0o777 == 0o644
        assert b"\r\n" in z.read(info["start.bat"])


def test_mac_starter_is_executable_in_git():
    mode = build_release.git("ls-files", "-s", "start.command").split()[0]
    assert mode == "100755"
