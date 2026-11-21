"""Starts the Grant Reporting Assistant connector for the Claude app or the ChatGPT app.

The chat app runs this file with the computer's Python. It puts the bundled
app code and libraries on the path, checks the Python version, sets up Word
support (the lxml library, downloaded once for this computer) in the
background, and then serves the connector's tools over standard input and
output. Written for old Python versions too, so a too-old Python still gets a
plain explanation in the chat instead of a silent failure.

    main.py --setup   sets up Word support now and exits (the ChatGPT installer runs this once).
"""

import os
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "lib")]
LXML = "lxml==6.1.3"
MIN_PYTHON = (3, 11)


def log(*parts):
    sys.stderr.write("[gra-connector] " + " ".join(str(p) for p in parts) + "\n")
    sys.stderr.flush()


def data_dir():
    if os.name == "nt":
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local"),
                            "GrantReportingAssistant")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/GrantReportingAssistant")
    else:
        base = os.path.expanduser("~/.local/share/grant-reporting-assistant")
    return os.path.join(base, "connector-lib", "py%d%d-%s" % (sys.version_info[0], sys.version_info[1], sys.platform))


class WordSupport(object):
    """Makes lxml (needed for Word files) importable, installing it once with pip if needed."""

    def __init__(self):
        self.done = threading.Event()
        self.ok = False
        self.target = data_dir()

    def start(self):
        if os.path.isdir(self.target):
            sys.path.append(self.target)
        if self._importable():
            self.ok = True
            self.done.set()
            return
        threading.Thread(target=self._install, name="word-support", daemon=True).start()

    def _importable(self):
        import importlib
        importlib.invalidate_caches()
        try:
            import lxml.etree  # noqa: F401
            return True
        except ImportError:
            return False

    def _install(self):
        try:
            log("Setting up Word support (one-time download of %s)" % LXML)
            cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                   "--no-input", "--only-binary=:all:", "--target", self.target, LXML]
            done = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr, timeout=600)
            if done.returncode == 0:
                if self.target not in sys.path:
                    sys.path.append(self.target)
                self.ok = self._importable()
            log("Word support ready" if self.ok else "Word support could not be set up")
        except Exception as e:
            log("Word support could not be set up:", e)
        finally:
            self.done.set()

    def ready(self, wait=120):
        self.done.wait(wait)
        return self.ok


def too_old():
    """A connector with one tool that explains the problem, for a Python older than 3.11."""
    from gra.mcp_stdio import ToolServer

    found = "%d.%d" % sys.version_info[:2]
    text = ("Grant Reporting Assistant needs Python %d.%d or newer, but the chat app started Python %s (%s). "
            "Install the newest Python from https://www.python.org/downloads/ (on Windows, tick 'Add python.exe "
            "to PATH'), then quit and reopen the Claude or ChatGPT app." % (MIN_PYTHON + (found, sys.executable)))
    tool = {"name": "setup_problem", "description": "Explains why Grant Reporting Assistant cannot start yet.",
            "inputSchema": {"type": "object", "properties": {}}}
    ToolServer("grant-reporting-assistant", "setup", text, lambda: [tool], lambda name, args: (text, True)).serve()


def setup():
    """Set up Word support now, waiting for it; exit status 0 when it is ready."""
    word = WordSupport()
    if os.path.isdir(word.target):
        sys.path.append(word.target)
    word.ok = word._importable()
    if not word.ok:
        word._install()
    sys.exit(0 if word.ok else 1)


def main():
    if "--setup" in sys.argv[1:]:
        setup()
        return
    if sys.version_info < MIN_PYTHON:
        too_old()
        return
    word = WordSupport()
    word.start()
    from gra.connector import main as serve

    serve(sys.argv[1:], word_ready=word.ready)


if __name__ == "__main__":
    main()
