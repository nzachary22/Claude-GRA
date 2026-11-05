"""Small, wholly synthetic fixtures. No real people or organizations."""

import csv
import json
from pathlib import Path

import pytest

from gra.importer import import_files


def write_csv(path: Path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


@pytest.fixture(autouse=True)
def isolated_from_this_computer(tmp_path_factory, monkeypatch):
    """Keep every test away from the real app-data folder and the real saved API keys.

    Tests run on the same Windows account as the app, so without this a test could
    create folders in the real %LOCALAPPDATA%\\GrantReportingAssistant or read a key
    from Windows Credential Manager or the environment.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("GRA_DATA_DIR", str(home / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "localappdata"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # Child processes started by tests get no keyring at all.
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    import keyring

    from gra import llm

    store: dict = {}
    monkeypatch.setattr(keyring, "get_password", lambda service, user: store.get((service, user)))
    monkeypatch.setattr(keyring, "set_password", lambda service, user, key: store.__setitem__((service, user), key))
    monkeypatch.setattr(llm, "_windows_user_env", lambda name: None)


@pytest.fixture
def tiny_files(tmp_path):
    clients = write_csv(tmp_path / "clients.csv",
        ["Client ID", "First Name", "Last Name", "Date of Birth", "Gender", "County", "Intake Notes"],
        [
            ["C-001", "Ava", "Stone", "1980-02-01", "woman", "North", "Tenant reports mold in the unit since spring"],
            ["C-002", "Ben", "Ray", "1975-07-12", "man", "South", "Called about wage claim from a former employer"],
            ["C-003", "Cy", "Moss", "2001-11-30", "", "North", "Needs help with a benefits denial letter received"],
            ["C-004", "Dee", "Lark", "1990-05-05", "nonbinary", "East", "Walk in, eviction hearing set for next week now"],
        ] + [[f"C-{i:03d}", f"F{i}", f"L{i}", f"19{50 + i % 40}-01-15", "woman" if i % 2 else "man",
              ["North", "South", "East"][i % 3], f"Synthetic intake narrative number {i} with enough words"] for i in range(5, 60)])
    cases = write_csv(tmp_path / "cases.csv",
        ["case_id", "client_id", "opened", "closed", "problem_code", "minutes"],
        [[f"K-{i:04d}", f"C-{(i % 59) + 1:03d}", f"2025-{(i % 12) + 1:02d}-10",
          f"2025-{(i % 12) + 1:02d}-20" if i % 5 else "", ["01", "02", "03"][i % 3], 30 + i] for i in range(1, 121)])
    codes = write_csv(tmp_path / "problem_codes.csv", ["problem_code", "label", "sort_order"],
        [["01", "Housing", "01"], ["02", "Employment", "02"], ["03", "Benefits", "03"]])
    return [clients, cases, codes]


@pytest.fixture
def tiny_db(tmp_path, tiny_files):
    db = tmp_path / "data.db"
    catalog = import_files(db, tiny_files)
    return db, catalog


@pytest.fixture
def json_file(tmp_path):
    p = tmp_path / "people.json"
    p.write_text(json.dumps([{"person_id": "P1", "langs": ["en", "es"], "age": 33},
                             {"person_id": "P2", "langs": ["en"], "age": None}]))
    return p
