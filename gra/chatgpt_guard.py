"""Keeps the ChatGPT app's own tools out of GRA's private data folders.

The ChatGPT desktop app can run commands on the computer by itself, so in ChatGPT GRA's
rules alone can't stop it from opening a data file. Its settings file (config.toml in the
.codex folder) can: a permissions profile with "deny" entries makes its sandbox refuse
every command that reads those folders, and a "granular" approval policy auto-rejects the
prompts that would let it ask to run outside the sandbox. GRA's own tools are not affected:
ChatGPT's permission profiles govern its sandboxed commands, not MCP servers.

The ChatGPT installer writes these settings (settings_lines, profile_block). The connector,
in ChatGPT, checks them before it reads any file (protection_problem) and stops if they are
missing, so the data folders GRA reads are always folders ChatGPT itself cannot open.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

PROFILE = "gra-protected"
# Escalation and permission-request prompts are auto-rejected; the other prompt kinds stay as they were.
GRANULAR = {"sandbox_approval": False, "rules": True, "mcp_elicitations": True, "request_permissions": False,
            "skill_approval": True}
PRIVATE_FOLDER = "GRA Private Data"


def work_folder() -> Path:
    """Where GRA keeps each report's working database in ChatGPT (it holds the imported records).

    It is outside AppData on purpose: Windows can redirect a Store app's AppData writes to another
    place, and this folder must be exactly the one ChatGPT is blocked from."""
    return Path.home() / ".grant-reporting-assistant" / "chatgpt-work"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def config_path() -> Path:
    return codex_home() / "config.toml"


def _key_path(key: str) -> Path | None:
    """A filesystem entry's path, or None for special entries (:workspace_roots and the like) and relative paths."""
    if key.startswith(":"):
        return None
    if key == "~" or key.startswith(("~/", "~\\")):
        return Path.home() / key[2:] if len(key) > 1 else Path.home()
    p = Path(key)
    return p if p.is_absolute() else None


def norm(p: Path) -> str:
    try:
        p = p.expanduser().resolve()
    except OSError:
        p = p.expanduser().absolute()
    return os.path.normcase(str(p)).rstrip("\\/") or os.sep


def inside(p: Path, root: Path) -> bool:
    a, b = norm(p), norm(root)
    return a == b or a.startswith(b + os.sep) or (b.endswith(os.sep) and a.startswith(b))


def too_broad(folder: Path) -> bool:
    """A folder ChatGPT must not be blocked from as a whole: the home folder, anything holding it, or a drive's root."""
    return inside(Path.home(), folder) or folder.resolve().parent == folder.resolve()


def _filesystem(cfg: dict, name: str, seen: tuple = ()) -> dict[str, str]:
    """A profile's path entries, its parents' first (named parents only; built-in ones add no deny entries)."""
    if name.startswith(":") or name in seen:
        return {}
    profile = (cfg.get("permissions") or {}).get(name) or {}
    out = _filesystem(cfg, str(profile.get("extends") or ""), seen + (name,)) if profile.get("extends") else {}
    for k, v in (profile.get("filesystem") or {}).items():
        if isinstance(v, str):
            out[k] = v
    return out


def protection_problem(roots: list[Path] | None, text: str | None = None) -> str | None:
    """Why ChatGPT could open files in these folders by itself, or None when its settings block it."""
    if not roots:
        return "GRA has no private data folders set up"
    if text is None:
        path = config_path()
        if not path.exists():
            return f"ChatGPT's settings file ({path}) is missing"
        text = path.read_text(encoding="utf-8")
    try:
        cfg = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return "ChatGPT's settings file can't be read"
    if "sandbox_mode" in cfg:
        return "ChatGPT's settings use the older sandbox_mode setting, which ignores GRA's block"
    name = cfg.get("default_permissions")
    if not isinstance(name, str) or name.startswith(":"):
        return "ChatGPT's default permissions are not set to block GRA's private data folders"
    entries = [(p, v) for k, v in _filesystem(cfg, name).items() if (p := _key_path(k)) is not None]
    for root in roots:
        covering = [(p, v) for p, v in entries if inside(root, p)]
        if not covering:
            return f"ChatGPT is not blocked from {root}"
        deepest = max(len(norm(p)) for p, _ in covering)
        if any(v != "deny" for p, v in covering if len(norm(p)) == deepest):
            return f"ChatGPT is not blocked from {root}"
        if any(v != "deny" for p, v in entries if inside(p, root) and norm(p) != norm(root)):
            return f"ChatGPT may open part of {root}"
    if os.name == "nt" and (cfg.get("features") or {}).get("prefer_mxc") is not True:
        # Windows' older sandbox does not keep commands from reading files; its process isolation (MXC) does.
        return "ChatGPT's Windows isolation (prefer_mxc) is off, so its sandbox doesn't enforce the block"
    policy = cfg.get("approval_policy")
    granular = policy.get("granular") if isinstance(policy, dict) else None
    if policy != "never" and not (isinstance(granular, dict) and granular.get("sandbox_approval") is False
                                  and granular.get("request_permissions") is False):
        return "ChatGPT may still ask to run commands outside its sandbox"
    return None


# ---------------------------------------------------------------- what the installer writes

def toml_string(s) -> str:
    return __import__("json").dumps(str(s))  # JSON's string escapes are all valid TOML


def profile_block(roots: list[Path], parent: str = ":workspace") -> list[str]:
    lines = [f"[permissions.{PROFILE}]",
             'description = "ChatGPT\'s usual access, except Grant Reporting Assistant\'s private data folders, '
             'which only GRA may open."',
             f"extends = {toml_string(parent)}", "",
             f"[permissions.{PROFILE}.filesystem]"]
    lines += [f"{toml_string(r)} = \"deny\"" for r in roots]
    return lines + [""]


def approval_line() -> str:
    inner = ", ".join(f"{k} = {'true' if v else 'false'}" for k, v in GRANULAR.items())
    return f"approval_policy = {{ granular = {{ {inner} }} }}"
