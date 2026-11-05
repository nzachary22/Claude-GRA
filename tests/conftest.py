"""Small, wholly synthetic fixtures. No real people or organizations."""

import csv
import json
import os
from pathlib import Path

import pytest

# Set before anything imports keyring, which picks its backend once on import.
# Child processes inherit it, so no test can reach the real credential store.
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"

from gra.importer import import_files  # noqa: E402

PLACEHOLDER_KEY = "placeholder-not-a-real-key"


@pytest.fixture(autouse=True)
def isolate_from_real_app(tmp_path_factory, monkeypatch):
    """Keep every test away from the real app-data folder and the real API keys.

    Tests can run while the app is in use on the same computer. A placeholder key
    in the environment means get_api_key never looks in the Windows registry or
    the credential store. Tests of the key logic remove it and supply their own.
    """
    base = tmp_path_factory.mktemp("isolated")
    monkeypatch.setenv("GRA_DATA_DIR", str(base / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(base / "localappdata"))
    monkeypatch.setenv("HOME", str(base / "home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", PLACEHOLDER_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", PLACEHOLDER_KEY)


def write_csv(path: Path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


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
