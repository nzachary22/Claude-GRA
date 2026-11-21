"""Installs Grant Reporting Assistant (GRA) for the ChatGPT desktop app, on Windows or a Mac.

The ChatGPT desktop app runs local tools ("MCP servers") listed in its settings file,
config.toml in the .codex folder of the user's home folder. It can also run commands on the
computer by itself, so this installer also uses that file to block ChatGPT's own commands
from GRA's private data folders. Started by "Install on Windows.bat" or "Install on Mac.command":

1. copies GRA's connector into this computer's app data folder;
2. sets up Word support (a one-time download of the lxml library);
3. makes the private data folder (your user folder > GRA Private Data) and asks whether the user
   keeps files in other folders that should be private too (folders chosen before are kept);
4. writes ChatGPT's settings: a permissions profile that denies ChatGPT's commands any access
   to those folders and to GRA's working files, an approval policy that turns down requests
   to get around it, and GRA itself, which reads only those folders. Everything else in the
   file is kept, a setting GRA replaces is kept as a comment and restored on removal, and a
   backup of the file is kept next to it. Then it checks that GRA starts.

    python install.py            install or update
    python install.py --remove   take GRA and its settings out of ChatGPT and delete the installed copy

Options for testing: --install-dir DIR, --output-folder DIR, --private-folder DIR,
--add-folder DIR (repeatable), --no-dialogs. CODEX_HOME, when set, is used instead of
~/.codex, as ChatGPT does.
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
PROFILE_MARK = "# Grant Reporting Assistant: keeps ChatGPT's own commands out of GRA's private data folders"
TAG = "  # added by Grant Reporting Assistant"
REPLACED = "# replaced by Grant Reporting Assistant: "
HERE = Path(__file__).resolve().parent
MIN_PYTHON = (3, 11)
_HEADER = re.compile(r"^\s*\[")
# Sections GRA owns: its MCP server and its permissions profile, with their sub-tables.
_OURS = re.compile(r'^\s*\[\s*(?:mcp_servers\s*\.\s*"?grant_reporting_assistant"?|permissions\s*\.\s*"?gra-protected"?)'
                   r'\s*(?:\.[^\]]*)?\]')
# Older sandbox settings that would make ChatGPT ignore the permissions profile.
_LEGACY_TABLE = re.compile(r"^\s*\[\s*sandbox_workspace_write\s*\]")
_FEATURES = re.compile(r"^\s*\[\s*features\s*\]")
_MXC = re.compile(r"^\s*prefer_mxc\s*=")
MXC_LINE = "prefer_mxc = true"
_TOP_KEY = re.compile(r"^\s*(default_permissions|sandbox_mode|approval_policy)\s*=")

sys.path.insert(0, str(HERE / "server"))
from gra import chatgpt_guard as guard  # noqa: E402  (the same checks GRA makes before reading files)


# ---------------------------------------------------------------- where things go

def config_path() -> Path:
    return guard.config_path()


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

def toml_string(s) -> str:
    return json.dumps(str(s))  # JSON's string escapes are all valid TOML


def server_block(python: str, main_py: Path, output: Path, allow: list[Path], work: Path) -> str:
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
        f"GRA_DATA_DIR = {toml_string(work)}",
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
    """The settings text as it was before GRA: GRA's sections and lines out, the settings it replaced back."""
    out, skipping = [], False
    for line in text.splitlines():
        if _OURS.match(line):
            skipping = True
            if out and out[-1].strip() in (MARK, PROFILE_MARK):
                out.pop()
            continue
        if skipping and _HEADER.match(line):
            skipping = False
        if skipping or line.strip() in (MARK, PROFILE_MARK) or line.endswith(TAG):
            continue
        out.append(line[len(REPLACED):] if line.startswith(REPLACED) else line)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + ("\n" if out else "")


def with_ours(text: str, block: str, private: list[Path]) -> str:
    """Add GRA, its permissions profile and its approval policy, keeping the user's other settings."""
    import tomllib

    rest = without_ours(text)
    cfg = tomllib.loads(rest) if rest.strip() else {}  # a broken file raises here and is left alone
    previous = cfg.get("default_permissions")
    parent = previous if isinstance(previous, str) and previous != ":danger-full-access" else ":workspace"
    keep_policy = cfg.get("approval_policy") == "never"  # already never asks to get around the sandbox
    lines, in_top, in_legacy, in_features = [], True, False, False
    has_features = isinstance(cfg.get("features"), dict)
    for line in rest.splitlines():
        if _HEADER.match(line):
            in_top = False
            in_legacy = bool(_LEGACY_TABLE.match(line))
            in_features = bool(_FEATURES.match(line))
        m = _TOP_KEY.match(line) if in_top else None
        if in_legacy or (m and not (m.group(1) == "approval_policy" and keep_policy)) or \
                (in_features and _MXC.match(line)):
            line = REPLACED + line
        lines.append(line)
        if in_features and _FEATURES.match(line):
            lines.append(MXC_LINE + TAG)  # Windows: the isolation that enforces "deny" (see chatgpt_guard)
    if not has_features:
        lines += ["", "[features]" + TAG, MXC_LINE + TAG]
    top = [f'default_permissions = "{guard.PROFILE}"{TAG}']
    if not keep_policy:
        top.append(guard.approval_line() + TAG)
    body = "\n".join(lines).rstrip("\n")
    profile = "\n".join([PROFILE_MARK, *guard.profile_block(private, parent)])
    return "\n".join(top) + "\n" + (body + "\n\n" if body else "\n") + profile + "\n" + block


def current_settings(text: str) -> dict:
    """GRA's server section as it stands, if any."""
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


def parses(text: str) -> bool:
    import tomllib

    try:
        tomllib.loads(text)
        return True
    except tomllib.TOMLDecodeError:
        return False


def write_settings(new_text: str) -> Path:
    """Write ChatGPT's settings file, keeping a backup of the old one. Returns the file's path."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    if not parses(new_text):
        raise RuntimeError(f"Could not change {path} without breaking it, so it was left unchanged.")
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
    if (HERE / "LICENSE").exists():
        shutil.copy2(HERE / "LICENSE", target / "LICENSE")
    return dest / "main.py"


def server_env(work: Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    if work:
        env["GRA_DATA_DIR"] = str(work)
    return env


def set_up_word(main_py: Path) -> bool:
    done = subprocess.run([sys.executable, str(main_py), "--setup"], stdin=subprocess.DEVNULL, env=server_env(),
                          timeout=900)
    return done.returncode == 0


def bare_env(work: Path | None = None) -> dict:
    """Roughly the few settings the ChatGPT app gives a connector, so the check catches a missing home folder."""
    keep = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "USERNAME", "LANG", "TERM")
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    if work:
        env["GRA_DATA_DIR"] = str(work)
    return env


def check_starts(main_py: Path, output: Path, allow: list[Path], work: Path) -> str | None:
    """Start the installed connector the way ChatGPT will and ask for its tools. Returns a problem, or None."""
    args = [sys.executable, str(main_py), "--client", "chatgpt", "--output", str(output), "--allow", *map(str, allow)]
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "gra-installer-check", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    try:
        done = subprocess.run(args, input="".join(json.dumps(m) + "\n" for m in msgs).encode(), capture_output=True,
                              env=bare_env(work), timeout=120)
        replies = [json.loads(line) for line in done.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
        tools = {t["name"] for r in replies if r.get("id") == 2 for t in r["result"]["tools"]}
        if "start_report" in tools:
            return None
        return (done.stderr.decode("utf-8", "replace").strip()[-800:] or "GRA did not list its tools.")
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def choose_private_folders(ui: Ui, title: str, default: Path, before: list[Path], extra: list[Path],
                           dialogs: bool) -> list[Path]:
    """GRA's private data folders: the default one, the ones chosen before, and any the user adds now."""
    folders = [default] + [p for p in before if p.is_dir() and not guard.too_broad(p)]
    folders += [p.resolve() for p in extra if p.is_dir() and not guard.too_broad(p)]
    while dialogs:
        listed = "\n".join(f"  - {p}" for p in dict.fromkeys(folders))
        if not ui.ask_yes_no(title, "Put your reports and data files in GRA's private data folder. GRA reads files "
                             "only there, and ChatGPT itself will be blocked from opening them:\n\n" + listed +
                             "\n\nDo you also keep report or data files in another folder that should be private, "
                             "such as a Google Drive or shared drive folder?\n\nClick Yes to add a folder, or No to "
                             "continue."):
            break
        chosen = ui.choose_folder("Choose a folder to keep private (only GRA may open it)")
        if not chosen or not chosen.is_dir():
            continue
        if guard.too_broad(chosen):
            ui.tell(title, f"{chosen} holds your whole home folder or drive, so blocking ChatGPT from it would stop "
                    "ChatGPT from working. Choose the folder that holds the report and data files instead.", error=True)
            continue
        folders.append(chosen.resolve())
    return list(dict.fromkeys(folders))


def install(install_dir: Path, extra: list[Path], dialogs: bool, output: Path | None = None,
            private: Path | None = None) -> int:
    ui = Ui(dialogs)
    title = f"{NAME} for ChatGPT"
    say(f"Installing {NAME} for the ChatGPT app...")
    settings = config_path()
    old_text = settings.read_text(encoding="utf-8") if settings.exists() else ""
    if old_text and not parses(old_text):
        ui.tell(title, f"The ChatGPT app's settings file ({settings}) has a mistake in it, so GRA did not change it. "
                "Fix or remove that file, then run the installer again.", error=True)
        return 1

    say("1. Copying GRA to " + str(install_dir))
    main_py = copy_connector(install_dir)

    say("2. Setting up Word support (a one-time download of about 5 MB; this can take a minute)")
    if not set_up_word(main_py):
        say("   Word support could not be set up now. GRA will try again when it first starts, "
            "so check the internet connection.")

    say("3. Choosing GRA's private data folders")
    output = output or documents_folder()
    # In the home folder, not Documents: Windows often keeps Documents in OneDrive, which would copy client
    # records to the cloud, and the folder must be the exact one ChatGPT is blocked from.
    private = private or Path.home() / guard.PRIVATE_FOLDER
    private.mkdir(parents=True, exist_ok=True)
    before = [p for p in allowed_before(old_text) if p != output / guard.PRIVATE_FOLDER]  # the old default spot
    folders = choose_private_folders(ui, title, private.resolve(), before, extra, dialogs)
    for p in folders:
        say(f"   - {p}")
    work = guard.work_folder()
    work.mkdir(parents=True, exist_ok=True)

    say("4. Updating the ChatGPT app's settings")
    problem = check_starts(main_py, output, folders, work)
    if problem:
        ui.tell(title, "GRA was copied, but it did not start, so the ChatGPT app's settings were not changed.\n\n"
                + problem, error=True)
        return 1
    new_text = with_ours(old_text, server_block(sys.executable, main_py, output, folders, work), [*folders, work])
    left = guard.protection_problem([*folders, work], new_text)
    if left:  # never leave GRA in ChatGPT without its block
        ui.tell(title, f"GRA could not set up ChatGPT's block ({left}), so the settings were not changed.", error=True)
        return 1
    path = write_settings(new_text)
    say(f"   Updated {path}" + (f" (the old one is kept as {path.name}.gra-backup)" if old_text else ""))

    ui.tell(title, f"{NAME} is installed for the ChatGPT app.\n\n"
            f"Put your reports and data files in:\n{private}\n\n"
            "GRA reads files only there (and in any other private folder you added), and ChatGPT itself is blocked "
            "from opening them.\n\n"
            "Next:\n"
            "1. Quit the ChatGPT app completely, then open it again.\n"
            "2. Start a new chat and type: Run the Grant Reporting Assistant sample report.\n\n"
            f"Finished tables are saved in {output / NAME}.")
    return 0


def remove(install_dir: Path, dialogs: bool) -> int:
    ui = Ui(dialogs)
    path = config_path()
    if path.exists():
        text = path.read_text(encoding="utf-8")
        cleaned = without_ours(text)
        if cleaned != text:
            write_settings(cleaned)
            say(f"Took GRA and its settings out of {path}")
    shutil.rmtree(install_dir, ignore_errors=True)
    shutil.rmtree(guard.work_folder(), ignore_errors=True)
    say(f"Deleted {install_dir}")
    ui.tell(f"{NAME} for ChatGPT", f"{NAME} was removed from the ChatGPT app. Quit the ChatGPT app and open it "
            "again to finish. Your files in GRA Private Data and your saved reports in Documents were kept.")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=f"Install {NAME} for the ChatGPT desktop app.")
    parser.add_argument("--remove", action="store_true", help="take GRA out of ChatGPT and delete the installed copy")
    parser.add_argument("--install-dir", type=Path, default=None)
    parser.add_argument("--output-folder", type=Path, default=None)
    parser.add_argument("--private-folder", type=Path, default=None)
    parser.add_argument("--add-folder", type=Path, action="append", default=[])
    parser.add_argument("--no-dialogs", action="store_true")
    a = parser.parse_args(argv)
    target = a.install_dir or default_install_dir()
    try:
        if a.remove:
            return remove(target, not a.no_dialogs)
        return install(target, a.add_folder, not a.no_dialogs, a.output_folder, a.private_folder)
    except Exception as e:
        say(f"\nThe installer stopped: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    if sys.version_info < MIN_PYTHON:
        print(f"{NAME} needs Python 3.11 or newer; this is Python {sys.version.split()[0]}. "
              "Install the newest Python from https://www.python.org/downloads/ and run the installer again.")
        sys.exit(1)
    sys.exit(main())
