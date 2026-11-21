"""The connector in the ChatGPT app: its wording and delivery, and the installer that adds it to ChatGPT's settings."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from gra.connector import CHATGPT, CLAUDE, Connector, detect_client
from gra.mcp_stdio import ToolServer
from tests.test_connector import _rpc, replay

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gra_chatgpt_install", ROOT / "connector" / "chatgpt" / "install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GRA_DATA_DIR", str(tmp_path / "appdata"))


def test_chat_app_is_detected_from_the_handshake():
    assert detect_client({"name": "codex-mcp-client"}) is CHATGPT
    assert detect_client({"name": "ChatGPT"}) is CHATGPT
    assert detect_client({"name": "claude-ai"}) is CLAUDE
    assert detect_client({"name": "something-else"}) is None and detect_client(None) is None
    c = Connector(fixed_client=False)
    c.initialized({"clientInfo": {"name": "codex-mcp-client"}})
    assert c.client is CHATGPT
    fixed = Connector(client=CLAUDE)
    fixed.initialized({"clientInfo": {"name": "codex-mcp-client"}})
    assert fixed.client is CLAUDE  # a start command that names the app wins


def test_chatgpt_delivers_word_and_excel_and_the_chat_tables(tmp_path):
    c = Connector(output_root=tmp_path / "out", client=CHATGPT)
    assert "get_doc_and_sheet" not in {t["name"] for t in c.tools()}
    assert "Claude" not in c.instructions()
    start, err = c.call("start_sample_report", {})
    assert not err
    how = start.split("=== HOW TO WORK ===")[1].split("GRA's rules:")[0]
    assert "ChatGPT chat" in how and "Claude" not in how and "shell or terminal commands" in how
    assert "Word and Excel files" in how
    reply = replay(c.call)
    assert reply.startswith("Accepted.") and "Claude" not in reply
    assert "show the user the tables below exactly as written" in reply
    assert "grant-report-tables-20261231.xlsx" in reply
    # Asked anyway, the doc-and-sheet tool points back to the chat tables instead of Claude docs.
    again, err = c.call("get_doc_and_sheet", {})
    assert not err and "not available in the ChatGPT app" in again and "### " in again
    from gra.activity import render_text

    s = c.run.session
    text = render_text(s.activity.events, [], s.gate.protected_value_count(), c.run.id)
    assert "ChatGPT app with the user's own ChatGPT login" in text and "OpenAI" in text


def test_claude_wording_is_unchanged(tmp_path):
    c = Connector(output_root=tmp_path / "out")
    assert "get_doc_and_sheet" in {t["name"] for t in c.tools()}
    start, _ = c.call("start_sample_report", {})
    assert "user's Claude chat" in start and "Claude sheet" in start and "ChatGPT" not in start


def test_tools_carry_hints():
    tools = {t["name"]: t for t in Connector().tools()}
    assert tools["run_query"]["annotations"]["readOnlyHint"] is True
    assert tools["submit_tables"]["annotations"]["readOnlyHint"] is False
    assert all(t["annotations"]["destructiveHint"] is False and t["annotations"]["openWorldHint"] is False
               for t in tools.values())


def test_instructions_follow_the_handshake():
    c = Connector(fixed_client=False)
    server = ToolServer("x", "1", c.instructions, c.tools, c.call, on_initialize=c.initialized)
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"clientInfo": {"name": "codex-mcp-client", "version": "1"}}})
    assert "Claude" not in init["result"]["instructions"]
    names = {t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert "get_doc_and_sheet" not in names


# ---------------------------------------------------------------- ChatGPT's settings file

EXISTING = '''model = "gpt-6"   # the user's own settings

[mcp_servers.context7]
command = "npx"
args = ["-y", "@upstash/context7-mcp"]

[mcp_servers.context7.env]
MY_ENV_VAR = "x"

[profiles.work]
model = "other"
'''


def test_settings_keep_everything_else(tmp_path):
    block = install.server_block("C:\\Python313\\python.exe", Path("C:\\Users\\Ana María\\AppData\\GRA\\main.py"),
                                 Path("C:\\Users\\Ana María\\OneDrive\\Documents"),
                                 [Path("C:\\Users\\Ana María"), Path('G:\\My Drive\\Grant "data"')])
    once = install.with_ours(EXISTING, block)
    twice = install.with_ours(once, block)
    assert once == twice  # running the installer again replaces GRA's section, nothing else
    parsed = tomllib.loads(once)
    assert parsed["model"] == "gpt-6" and parsed["profiles"]["work"]["model"] == "other"
    assert parsed["mcp_servers"]["context7"]["env"] == {"MY_ENV_VAR": "x"}
    ours = parsed["mcp_servers"]["grant_reporting_assistant"]
    assert ours["command"] == "C:\\Python313\\python.exe" and ours["args"][1:3] == ["--client", "chatgpt"]
    assert ours["args"][-1] == 'G:\\My Drive\\Grant "data"' and ours["tool_timeout_sec"] >= 600
    assert install.allowed_before(once) == [Path("C:\\Users\\Ana María"), Path('G:\\My Drive\\Grant "data"')]
    assert install.without_ours(once) == EXISTING
    assert install.without_ours(block) == ""


def test_settings_section_in_the_middle_is_replaced(tmp_path):
    middle = "[mcp_servers.grant_reporting_assistant]\ncommand = \"old\"\n\n[mcp_servers.grant_reporting_assistant.env]\n" \
             "A = \"1\"\n\n[other]\nkey = 1\n"
    out = install.with_ours(middle, install.server_block("py", Path("m.py"), Path("d"), [Path("h")]))
    parsed = tomllib.loads(out)
    assert parsed["other"] == {"key": 1} and parsed["mcp_servers"]["grant_reporting_assistant"]["command"] == "py"
    assert "old" not in out


def test_broken_settings_are_never_made_worse(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "config.toml").write_text(EXISTING, encoding="utf-8")
    with pytest.raises(RuntimeError):
        install.write_settings(EXISTING + "\n[[[ not toml")
    assert (tmp_path / "codex" / "config.toml").read_text(encoding="utf-8") == EXISTING


# ---------------------------------------------------------------- the installer, end to end

def _env(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV", "CODEX_HOME")}
    env.update(HOME=str(home), USERPROFILE=str(home), LOCALAPPDATA=str(home / "AppData" / "Local"),
               GRA_DATA_DIR=str(tmp_path / "appdata"), PYTHONUTF8="1")
    return home, env


def _run_as_chatgpt(server: dict, env: dict, tmp_path: Path) -> str:
    """Start GRA the way ChatGPT does, from its settings, and make the sample report's calls."""
    proc = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env={**env, **server.get("env", {})}, cwd=str(tmp_path))
    try:
        init = _rpc(proc, 1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "codex-mcp-client", "version": "1"}})
        assert "Claude" not in init["result"]["instructions"]
        proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        tools = {t["name"] for t in _rpc(proc, 2, "tools/list")["result"]["tools"]}
        assert "start_report" in tools and "get_doc_and_sheet" not in tools
        i = [10]

        def call(name, args):
            i[0] += 1
            r = _rpc(proc, i[0], "tools/call", {"name": name, "arguments": args})["result"]
            return r["content"][0]["text"], r["isError"]

        text, err = call("start_sample_report", {})
        assert not err, text
        return replay(call)
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
        proc.stdout.close()


def test_installer_from_the_source_folder(tmp_path):
    """install.py with the connector next to it: installs, keeps other settings, reinstalls, removes.
    (The libraries come from this test's Python; the released zip bundles them in server/lib.)"""
    import shutil

    files = tmp_path / "unzipped" / "files"
    shutil.copytree(ROOT / "connector" / "server", files / "server")
    shutil.copytree(ROOT / "gra", files / "server" / "gra", ignore=shutil.ignore_patterns("__pycache__", "static"))
    shutil.copy2(ROOT / "connector" / "chatgpt" / "install.py", files / "install.py")
    home, env = _env(tmp_path)
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(EXISTING, encoding="utf-8")
    drive = tmp_path / "Shared drive"
    drive.mkdir()
    target = tmp_path / "installed"
    docs = tmp_path / "My Documents"
    cmd = [sys.executable, str(files / "install.py"), "--no-dialogs", "--install-dir", str(target),
           "--output-folder", str(docs)]
    done = subprocess.run(cmd + ["--add-folder", str(drive)], env=env, capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout + done.stderr
    text = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["context7"]["command"] == "npx" and parsed["profiles"]["work"]["model"] == "other"
    server = parsed["mcp_servers"]["grant_reporting_assistant"]
    assert Path(server["args"][0]) == target / "server" / "main.py"
    assert server["args"][server["args"].index("--allow") + 1:] == [str(home), str(drive.resolve())]
    assert (home / ".codex" / "config.toml.gra-backup").read_text(encoding="utf-8") == EXISTING

    reply = _run_as_chatgpt(server, env, tmp_path)
    assert reply.startswith("Accepted.") and str(docs / "Grant Reporting Assistant") in reply

    again = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)  # an update keeps the folders
    assert again.returncode == 0, again.stdout + again.stderr
    assert install.allowed_before((home / ".codex" / "config.toml").read_text(encoding="utf-8")) == [home, drive.resolve()]

    gone = subprocess.run(cmd + ["--remove"], env=env, capture_output=True, text=True, timeout=120)
    assert gone.returncode == 0, gone.stdout + gone.stderr
    assert (home / ".codex" / "config.toml").read_text(encoding="utf-8") == EXISTING and not target.exists()


@pytest.mark.skipif(os.environ.get("GRA_CONNECTOR_BUNDLE") != "1", reason="set GRA_CONNECTOR_BUNDLE=1 (needs internet)")
def test_built_installer_works(tmp_path):
    """The released zip: its launcher finds Python, installs GRA with the bundled libraries and Word support, and
    ChatGPT's settings start a working connector."""
    sys.path.insert(0, str(ROOT / "tools"))
    from build_chatgpt import TOP, build

    bundle = build(tmp_path / "dist")
    with zipfile.ZipFile(bundle) as z:
        names = set(z.namelist())
        assert {f"{TOP}/Install on Windows.bat", f"{TOP}/Install on Mac.command", f"{TOP}/files/install.py",
                f"{TOP}/files/server/main.py", f"{TOP}/files/server/gra/connector.py"} <= names
        assert b"\r\n" in z.read(f"{TOP}/Install on Windows.bat")
        assert (z.getinfo(f"{TOP}/Install on Mac.command").external_attr >> 16) & 0o777 == 0o755
        z.extractall(tmp_path / "unzipped")
    folder = tmp_path / "unzipped" / TOP
    home, env = _env(tmp_path)
    if os.name == "nt":  # the Windows launcher finds a Python 3.11+ by itself
        cmd = ["cmd", "/c", str(folder / "Install on Windows.bat"), "--no-dialogs"]
    else:  # and so does the Mac launcher
        cmd = ["/bin/sh", str(folder / "Install on Mac.command"), "--no-dialogs"]
    done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stdout + done.stderr
    server = tomllib.loads((home / ".codex" / "config.toml").read_text(encoding="utf-8"))["mcp_servers"][
        "grant_reporting_assistant"]
    reply = _run_as_chatgpt(server, env, tmp_path)
    assert reply.startswith("Accepted.") and "grant-report-tables" in reply


# ---------------------------------------------------------------- a chat app that leaves out the home folder

main_spec = importlib.util.spec_from_file_location("gra_connector_main", ROOT / "connector" / "server" / "main.py")


def _main_module():
    mod = importlib.util.module_from_spec(main_spec)
    main_spec.loader.exec_module(mod)
    return mod


def test_windows_home_is_restored_from_the_install_folder(monkeypatch):
    main = _main_module()
    monkeypatch.setattr(main.os, "name", "nt")
    here = "C:\\Users\\Ana María\\AppData\\Local\\GrantReportingAssistant\\ChatGPT connector\\server"
    env = main.ensure_home_env({"USERNAME": "ana", "PATH": "x"}, here=here)
    assert env["USERPROFILE"] == "C:\\Users\\Ana María"
    assert env["LOCALAPPDATA"].startswith("C:\\Users\\Ana María") and "Local" in env["LOCALAPPDATA"]
    assert env["APPDATA"].endswith("Roaming") and env["TEMP"] == env["TMP"]
    # settings that are there already are never changed
    kept = main.ensure_home_env({"USERPROFILE": "D:\\me", "LOCALAPPDATA": "D:\\me\\L", "TEMP": "D:\\t"}, here=here)
    assert kept["USERPROFILE"] == "D:\\me" and kept["LOCALAPPDATA"] == "D:\\me\\L" and kept["TEMP"] == "D:\\t"


def test_windows_home_falls_back_to_the_user_name(monkeypatch):
    main = _main_module()
    monkeypatch.setattr(main.os, "name", "nt")
    env = main.ensure_home_env({"USERNAME": "nzach", "SYSTEMDRIVE": "C:"}, here="E:\\portable\\server")
    assert env["USERPROFILE"].replace("/", "\\").startswith("C:\\") and env["USERPROFILE"].endswith("nzach")


@pytest.mark.skipif(os.name == "nt", reason="the POSIX account lookup")
def test_mac_home_is_restored_from_the_account():
    import pwd

    main = _main_module()
    assert main.ensure_home_env({"PATH": "/usr/bin"})["HOME"] == pwd.getpwuid(os.getuid()).pw_dir


def test_settings_carry_the_home_folder(monkeypatch):
    for name in install.HOME_SETTINGS:
        monkeypatch.setenv(name, f"/x/{name}")
    block = install.server_block("py", Path("m.py"), Path("d"), [Path("h")])
    env = tomllib.loads(block)["mcp_servers"][install.SERVER]["env"]
    for name in install.HOME_SETTINGS:
        assert env[name] == f"/x/{name}"


def test_connector_starts_without_a_home_folder(tmp_path):
    """Start the connector the way ChatGPT does, with almost no settings, and ask for its tools."""
    env = {"PATH": os.environ.get("PATH", ""), "GRA_DATA_DIR": str(tmp_path / "appdata"), "PYTHONUTF8": "1"}
    for name in ("SYSTEMROOT", "SYSTEMDRIVE", "USERNAME"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "codex-mcp-client"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    done = subprocess.run([sys.executable, str(ROOT / "connector" / "server" / "main.py"), "--client", "chatgpt",
                           "--output", str(tmp_path / "out"), "--allow", str(tmp_path)],
                          input="".join(json.dumps(m) + "\n" for m in msgs).encode(), capture_output=True, env=env,
                          timeout=120)
    replies = [json.loads(line) for line in done.stdout.decode().splitlines() if line.strip()]
    tools = {t["name"] for r in replies if r.get("id") == 2 for t in r["result"]["tools"]}
    assert "start_report" in tools, done.stderr.decode()[-2000:]
