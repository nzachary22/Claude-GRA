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


def _known_folder(guid):
    """A Windows known folder (such as the user's profile) asked of Windows itself, or None."""
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

        f = uuid.UUID(guid).fields
        b = uuid.UUID(guid).bytes
        g = GUID(f[0], f[1], f[2], (ctypes.c_ubyte * 8)(*bytearray(b[8:])))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) == 0:
            path = out.value
            ctypes.windll.ole32.CoTaskMemFree(out)
            if path and os.path.isdir(path):
                return path
    except Exception:
        pass
    return None


def ensure_home_env(env=None, here=HERE):
    """Fill in the home-folder settings a chat app may leave out when it starts GRA.

    The ChatGPT app starts connectors with only a few settings, so on Windows USERPROFILE (the user's folder)
    and LOCALAPPDATA can be missing and Python stops with "Could not determine home directory". Each missing
    one is asked of Windows (or, elsewhere, of the user account), so GRA and the tools it starts find them.
    Settings that are already there are never changed.
    """
    env = os.environ if env is None else env

    def missing(name):
        return not env.get(name)

    if os.name == "nt":
        if missing("USERPROFILE"):
            home = None
            if env.get("HOMEDRIVE") and env.get("HOMEPATH"):
                home = env["HOMEDRIVE"] + env["HOMEPATH"]
            home = home if home and os.path.isdir(home) else _known_folder("5E6C858F-0E22-4760-9AFE-EA3317B67173")
            if not home:
                # GRA is installed inside the user's folder: ...\<user>\AppData\Local\GrantReportingAssistant\...
                low = here.lower()
                cut = low.find("\\appdata\\")
                if cut > 0:
                    home = here[:cut]
            if not home and env.get("USERNAME"):
                home = os.path.join((env.get("SYSTEMDRIVE") or "C:") + "\\", "Users", env["USERNAME"])
            if home:
                env["USERPROFILE"] = home
        home = env.get("USERPROFILE") or ""
        folders = (("LOCALAPPDATA", "F1B32785-6FBA-4FCF-9D55-7B8E7F157091", ("AppData", "Local")),
                   ("APPDATA", "3EB685DB-65F9-4CF6-A03A-E3EF65729F3D", ("AppData", "Roaming")))
        for name, guid, parts in folders:
            if missing(name):
                found = _known_folder(guid) or (os.path.join(home, *parts) if home else None)
                if found:
                    env[name] = found
        if env.get("LOCALAPPDATA"):
            for name in ("TEMP", "TMP"):
                if missing(name):
                    env[name] = os.path.join(env["LOCALAPPDATA"], "Temp")
    elif missing("HOME"):
        try:
            import pwd
            env["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
        except Exception:
            pass
    return env


ensure_home_env()
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
