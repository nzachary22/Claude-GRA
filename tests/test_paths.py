import subprocess
import sys
from pathlib import Path

from gra.paths import app_data_dir
from tests.conftest import PLACEHOLDER_KEY


def test_app_data_dir_honors_override(tmp_path, monkeypatch):
    target = tmp_path / "gra-data"
    monkeypatch.setenv("GRA_DATA_DIR", str(target))
    assert app_data_dir() == target
    assert target.is_dir()


def test_app_data_dir_is_outside_repo(monkeypatch):
    monkeypatch.delenv("GRA_DATA_DIR", raising=False)
    repo = Path(__file__).resolve().parents[1]
    assert repo not in app_data_dir().resolve().parents


def test_default_app_data_dir_is_not_the_real_one(tmp_path_factory, monkeypatch):
    monkeypatch.delenv("GRA_DATA_DIR", raising=False)
    assert app_data_dir().is_relative_to(tmp_path_factory.getbasetemp())


def test_child_processes_cannot_reach_real_keys():
    code = ("import keyring, gra.llm; print(type(keyring.get_keyring()).__module__); "
            "print(gra.llm.get_api_key('anthropic')); print(gra.llm.get_api_key('openai'))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True)
    assert out.stdout.split() == ["keyring.backends.null", PLACEHOLDER_KEY, PLACEHOLDER_KEY]


def test_app_data_dir_per_platform(tmp_path, monkeypatch):
    import gra.paths as paths

    monkeypatch.delenv("GRA_DATA_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
    if paths.os.name != "nt":
        monkeypatch.setattr(paths.sys, "platform", "darwin")
        assert app_data_dir() == tmp_path / "Library" / "Application Support" / "GrantReportingAssistant"
        monkeypatch.setattr(paths.sys, "platform", "linux")
        assert app_data_dir() == tmp_path / ".local" / "share" / "grant-reporting-assistant"
