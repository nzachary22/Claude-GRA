"""Where the app keeps working files.

Client databases, uploads and exports live in a per-user local folder, never in
the repository (which may sit inside a cloud-synced drive).
"""

import os
import sys
from pathlib import Path


def app_data_dir() -> Path:
    override = os.environ.get("GRA_DATA_DIR")
    if override:
        base = Path(override)
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "GrantReportingAssistant"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "GrantReportingAssistant"
    else:
        base = Path.home() / ".local" / "share" / "grant-reporting-assistant"
    base.mkdir(parents=True, exist_ok=True)
    return base
