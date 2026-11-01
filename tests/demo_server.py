"""Developer demo: the real app with the scripted Juniper analyst instead of Claude.

Lets the interface be checked end to end without an API key:
    python -m tests.demo_server   (then upload the Juniper report and one year's CSVs)
The demo analyst always reports the year given by GRA_DEMO_YEAR (default 2026).
"""

import os
import time
from pathlib import Path

import uvicorn

from gra.server import create_app
from tests import juniper_script
from tests.scripted import ScriptedModel

ROOT = Path(__file__).resolve().parents[1] / "juniper-ridge-legal-aid-society"


class SlowScripted(ScriptedModel):
    def respond(self, *args, **kwargs):
        time.sleep(1.2)  # make progress visible
        return super().respond(*args, **kwargs)


def factory():
    year = int(os.environ.get("GRA_DEMO_YEAR", "2026"))
    return SlowScripted(juniper_script.steps(f"{year}-01-01", f"{year}-12-31", ROOT / "data" / str(year)))


if __name__ == "__main__":
    uvicorn.run(create_app(factory), host="127.0.0.1", port=int(os.environ.get("PORT", "8766")), log_level="warning")
