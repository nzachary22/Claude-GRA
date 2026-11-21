"""Installs Grant Reporting Assistant (GRA) for the ChatGPT desktop app, on Windows or a Mac.

The ChatGPT desktop app runs local tools ("MCP servers") listed in its settings file,
config.toml in the .codex folder of the user's home folder. This installer, started by
"Install on Windows.bat" or "Install on Mac.command":

1. copies GRA's connector into this computer's app data folder;
2. sets up Word support (a one-time download of the lxml library);
3. asks which folders GRA may read (the home folder, plus any the user adds, such as a
   Google Drive or shared drive folder; folders chosen before are kept);
4. adds GRA to ChatGPT's settings file (only GRA's own section is changed, and a backup
   of the file is kept next to it), then checks that GRA starts.

    python install.py            install or update
    python install.py --remove   take GRA out of ChatGPT and delete the installed copy

Options for testing: --install-dir DIR, --output-folder DIR, --add-folder DIR (repeatable), --no-dialogs.
CODEX_HOME, when set, is used instead of ~/.codex, as ChatGPT does.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

NAME = "Grant Reporting Assistant"
SERVER = "grant_reporting_assistant"  # the section name in config.toml: [mcp_servers.grant_reporting_assistant]
MARK = "# Grant Reporting Assistant for ChatGPT (added by its installer; run the installer again to change it)"
HERE = Path(__file__).resolve().parent
MIN_PYTHON = (3, 11)
# The section's own lines start at its header and run to the next header that is not part of it.
_OURS = re.compile(r'^\s*\[\s*mcp_servers\s*\.\s*(?:"grant_reporting_assistant"|grant_reporting_assistant)\s*(?:\.[^\]]*)?\]')
_HEADER = re.compile(r"^\s*\[")


# ---------------------------------------------------------------- where things go

def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def config_path() -> Path:
    return codex_home() / "config.toml"


def default_install_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "GrantReportingAssistant"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "GrantReportingAssistant"
    else:
        base = Path.home() / ".local" / "share" / "grant-reporting-assistant"
    return base / "ChatGPT connector"


def documents_folder() -> Path:
    """The Documents folder, including one that Windows has moved (for example into OneDrive)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

            documents = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(documents), 0, None, ctypes.byref(out)) == 0:
                path = out.value
                ctypes.windll.ole32.CoTaskMemFree(out)
                if path and Path(path).is_dir():
                    return Path(path)
        except Exception:
            pass
    return Path.home() / "Documents"


# ---------------------------------------------------------------- ChatGPT's settings file

def toml_string(s: str) -> str:
    """A TOML basic string (JSON's string escapes are all valid TOML)."""
    return json.dumps(str(s))


def server_block(python: str, main_py: Path, output: Path, allow: list[Path]) -> str:
    args = [str(main_py), "--client", "chatgpt", "--output", str(output), "--allow", *[str(a) for a in allow]]
    return "\n".join([
        MARK,
        f"[mcp_servers.{SERVER}]",
        f"command = {toml_string(python)}",
        "args = [" + ", ".join(toml_string(a) for a in args) + "]",
        "startup_timeout_sec = 60",   # a slow first start (antivirus checks) must not count as a failure
        "tool_timeout_sec = 900",     # importing large data files can take minutes
        'default_tools_approval_mode = "approve"',  # GRA's tools only work on this computer, behind its privacy checks
        "",
        f"[mcp_servers.{SERVER}.env]",
        'PYTHONIOENCODING = "utf-8"',
        'PYTHONUTF8 = "1"',
        'PYTHONDONTWRITEBYTECODE = "1"',
        # The ChatGPT app starts connectors with few settings; without these Python cannot find the user's folder.
        *[f"{name} = {toml_string(value)}" for name, value in home_settings().items()],
        "",
    ])


HOME_SETTINGS = ("USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP") if os.name == "nt" \
    else ("HOME",)


def home_settings() -> dict:
    """This computer's home-folder settings, written into ChatGPT's settings so GRA always gets them."""
    return {name: os.environ[name] for name in HOME_SETTINGS if os.environ.get(name)}


def without_ours(text: str) -> str:
    """The settings text with GRA's section (and its marker line) taken out; everything else unchanged."""
    out, skipping = [], False
    for line in text.splitlines():
        if _OURS.match(line):
            skipping = True
            if out and out[-1].strip() == MARK:
                out.pop()
            continue
        if skipping and _HEADER.match(line):
            skipping = False
        if skipping or line.strip() == MARK:
            continue
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + ("\n" if out else "")


def with_ours(text: str, block: str) -> str:
    rest = without_ours(text)
    return rest + ("\n" if rest else "") + block


def parses(text: str) -> bool:
    import tomllib

    try:
        tomllib.loads(text)
        return True
    except tomllib.TOMLDecodeError:
        return False


def current_settings(text: str) -> dict:
    """GRA's section as it stands, if any."""
    import tomllib

    try:
        return tomllib.loads(text).get("mcp_servers", {}).get(SERVER, {}) or {}
    except tomllib.TOMLDecodeError:
        return {}


def allowed_before(text: str) -> list[Path]:
    args = current_settings(text).get("args") or []
    if "--allow" not in args:
        return []
    out = []
    for a in args[args.index("--allow") + 1:]:
        if str(a).startswith("--"):
            break
        out.append(Path(a))
    return out


def write_settings(new_text: str) -> Path:
    """Write ChatGPT's settings file, keeping a backup of the old one. Returns the file's path."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    if old and parses(old) and not parses(new_text):
        raise RuntimeError(f"Could not add GRA to {path} without breaking it, so it was left unchanged.")
    if old:
        shutil.copy2(path, path.with_name(path.name + ".gra-backup"))
    tmp = path.with_name(path.name + ".gra-new")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------- talking to the user

class Ui:
    """Small dialogs when the computer's Python has them (tkinter), else questions in the window."""

    def __init__(self, dialogs: bool = True):
        self.tk = None
        if dialogs:
            try:
                import tkinter
                from tkinter import filedialog, messagebox

                root = tkinter.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                self.tk, self.filedialog, self.messagebox = root, filedialog, messagebox
            except Exception:
                self.tk = None
        self.dialogs = dialogs

    def ask_yes_no(self, title: str, text: str) -> bool:
        if self.tk:
            return bool(self.messagebox.askyesno(title, text, parent=self.tk))
        if not self.dialogs:
            return False
        print("\n" + text)
        return input("Type y for yes or n for no, then press Enter: ").strip().lower().startswith("y")

    def choose_folder(self, title: str) -> Path | None:
        if self.tk:
            chosen = self.filedialog.askdirectory(title=title, mustexist=True, parent=self.tk)
            return Path(chosen) if chosen else None
        if not self.dialogs:
            return None
        typed = input("Paste the folder's location, then press Enter (or just press Enter to skip): ").strip().strip('"')
        return Path(typed) if typed else None

    def tell(self, title: str, text: str, error: bool = False) -> None:
        print("\n" + text)
        if self.tk:
            (self.messagebox.showerror if error else self.messagebox.showinfo)(title, text, parent=self.tk)


def say(text: str) -> None:
    print(text, flush=True)


# ---------------------------------------------------------------- steps

def copy_connector(target: Path) -> Path:
    source = HERE / "server"
    if not (source / "main.py").exists():
        raise RuntimeError("The installer's files are incomplete. Unzip the whole download again, then run the "
                           "installer from the unzipped folder.")
    dest = target / "server"
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(source, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for extra in ("LICENSE",):
        if (HERE / extra).exists():
            shutil.copy2(HERE / extra, target / extra)
    return dest / "main.py"


def server_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    return env


def set_up_word(main_py: Path) -> bool:
    done = subprocess.run([sys.executable, str(main_py), "--setup"], stdin=subprocess.DEVNULL, env=server_env(),
                          timeout=900)
    return done.returncode == 0


def bare_env() -> dict:
    """Roughly the few settings the ChatGPT app gives a connector, so the check catches a missing home folder."""
    keep = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "USERNAME", "LANG", "TERM")
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    return env


def check_starts(main_py: Path, output: Path, allow: list[Path]) -> str | None:
    """Start the installed connector the way ChatGPT will and ask for its tools. Returns a problem, or None."""
    args = [sys.executable, str(main_py), "--client", "chatgpt", "--output", str(output), "--allow", *map(str, allow)]
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "gra-installer-check", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    try:
        done = subprocess.run(args, input="".join(json.dumps(m) + "\n" for m in msgs).encode(), capture_output=True,
                              env=bare_env(), timeout=120)
        replies = [json.loads(line) for line in done.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
        tools = {t["name"] for r in replies if r.get("id") == 2 for t in r["result"]["tools"]}
        if "start_report" in tools:
            return None
        return (done.stderr.decode("utf-8", "replace").strip()[-800:] or "GRA did not list its tools.")
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def install(install_dir: Path, extra: list[Path], dialogs: bool, output: Path | None = None) -> int:
    ui = Ui(dialogs)
    title = f"{NAME} for ChatGPT"
    say(f"Installing {NAME} for the ChatGPT app...")
    settings = config_path()
    old_text = settings.read_text(encoding="utf-8") if settings.exists() else ""

    say("1. Copying GRA to " + str(install_dir))
    main_py = copy_connector(install_dir)

    say("2. Setting up Word support (a one-time download of about 5 MB; this can take a minute)")
    word = set_up_word(main_py)
    if not word:
        say("   Word support could not be set up now. GRA will try again when it first starts, "
            "so check the internet connection.")

    say("3. Choosing the folders GRA may read")
    home = Path.home()
    allow = [p for p in allowed_before(old_text) if p.is_dir()] or [home]
    for p in extra:
        if p.is_dir():
            allow.append(p.resolve())
    if dialogs:
        while True:
            listed = "\n".join(f"  - {p}" for p in allow)
            if not ui.ask_yes_no(title, "GRA only opens reports and data files inside these folders:\n\n" + listed +
                                 "\n\nAre your report and data files somewhere else too, such as a Google Drive or "
                                 "shared drive folder (for example G:\\My Drive)?\n\nClick Yes to add a folder, or No "
                                 "to continue."):
                break
            chosen = ui.choose_folder("Choose a folder GRA may read")
            if chosen and chosen.is_dir():
                allow.append(chosen.resolve())
    allow = list(dict.fromkeys(allow))  # each folder once, in order
    for p in allow:
        say(f"   - {p}")

    output = output or documents_folder()
    say("4. Adding GRA to the ChatGPT app's settings")
    problem = check_starts(main_py, output, allow)
    if problem:
        ui.tell(title, "GRA was copied, but it did not start, so the ChatGPT app's settings were not changed.\n\n"
                + problem, error=True)
        return 1
    path = write_settings(with_ours(old_text, server_block(sys.executable, main_py, output, allow)))
    say(f"   Updated {path}" + (f" (the old one is kept as {path.name}.gra-backup)" if old_text else ""))

    ui.tell(title, f"{NAME} is installed for the ChatGPT app.\n\n"
            "Next:\n"
            "1. Quit the ChatGPT app completely, then open it again.\n"
            "2. Start a new chat and type: Run the Grant Reporting Assistant sample report.\n\n"
            f"Finished reports are saved in {output / NAME}.")
    return 0


def remove(install_dir: Path, dialogs: bool) -> int:
    ui = Ui(dialogs)
    path = config_path()
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if current_settings(text) or MARK in text:
            write_settings(without_ours(text))
            say(f"Took GRA out of {path}")
    shutil.rmtree(install_dir, ignore_errors=True)
    say(f"Deleted {install_dir}")
    ui.tell(f"{NAME} for ChatGPT", f"{NAME} was removed from the ChatGPT app. Quit the ChatGPT app and open it "
            "again to finish. Your saved reports in Documents were kept.")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=f"Install {NAME} for the ChatGPT desktop app.")
    parser.add_argument("--remove", action="store_true", help="take GRA out of ChatGPT and delete the installed copy")
    parser.add_argument("--install-dir", type=Path, default=None)
    parser.add_argument("--output-folder", type=Path, default=None)
    parser.add_argument("--add-folder", type=Path, action="append", default=[])
    parser.add_argument("--no-dialogs", action="store_true")
    a = parser.parse_args(argv)
    if sys.version_info < MIN_PYTHON:
        say(f"{NAME} needs Python 3.11 or newer; this is Python {sys.version.split()[0]}. "
            "Install the newest Python from https://www.python.org/downloads/ and run the installer again.")
        return 1
    target = a.install_dir or default_install_dir()
    try:
        if a.remove:
            return remove(target, not a.no_dialogs)
        return install(target, a.add_folder, not a.no_dialogs, a.output_folder)
    except Exception as e:
        say(f"\nThe installer stopped: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
