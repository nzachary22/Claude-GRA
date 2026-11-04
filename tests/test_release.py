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
