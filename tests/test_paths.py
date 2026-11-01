from pathlib import Path

from gra.paths import app_data_dir


def test_app_data_dir_honors_override(tmp_path, monkeypatch):
    target = tmp_path / "gra-data"
    monkeypatch.setenv("GRA_DATA_DIR", str(target))
    assert app_data_dir() == target
    assert target.is_dir()


def test_app_data_dir_is_outside_repo(monkeypatch):
    monkeypatch.delenv("GRA_DATA_DIR", raising=False)
    repo = Path(__file__).resolve().parents[1]
    assert repo not in app_data_dir().resolve().parents
