"""Grant Reporting Assistant as a connector for the Claude app (Claude Desktop), with no API key.

The user chats with Claude under their normal Claude login. Claude calls this
connector's tools on the user's computer, and the connector does what the app's
built-in analyst loop does, with the same code and the same privacy gate:

- the data files are imported into a local SQLite database, read where they are;
- Claude receives the reporting rules, the report or requirements (checked first
  for client names and IDs from the data), the non-identifying data description,
  and aggregate query results only, never individual records;
- every figure in a finished table comes from a local query result, and the
  connector writes the Word and Excel files and the AI activity report itself.

The difference from the app is only who asks the questions: there the app sends
them to the AI service with an API key; here the Claude in the chat calls the
tools. See gra/engine.py for the shared rules and checks.
"""

from __future__ import annotations

import re
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .engine import SYSTEM_PROMPT, TOOLS, Limits, ReportSession
from .gate import QueryGate
from .importer import READERS, import_files
from .paths import app_data_dir
from .schema import describe
from .tables import GAP_MARK, Table

PROVIDER = "claude-app"
DOC_TYPES = {".pdf", ".docx", ".xlsx", ".xlsm", ".txt", ".md", ".csv"}
MAX_TEXT_CHARS = 400_000
KEEP_RUNS = 5
MAX_LISTED = 300

CHAT_RULES = """You are working in the user's Claude chat with the Grant Reporting Assistant (GRA) connector. GRA keeps the organization's client data on this computer: it imports the data files into a local database and lets you see only a non-identifying description of the data and aggregate query results. You act as GRA's reporting analyst. Follow GRA's rules below exactly, using the connector's tools run_query, note_assumption and submit_tables.

Working in the chat:
- Never ask the user to paste, attach or upload client data, and never open, read or analyze the data files with any other tool or connector (file readers, code execution, computer use or a browser). Only GRA's tools may touch them. If client records appear in the chat anyway, do not repeat or use them, and tell the user once that GRA keeps records private only when the files are given to GRA by their location on the computer.
- Once the report is started, work through every table without stopping to check in with the user, then submit them all. Mention briefly what you are doing as you go.
- When the tables are accepted, the connector saves the Word and Excel files and replies with the finished tables. Show the user those tables exactly as the reply gives them (copy them; never retype, round or recalculate a figure), where the files were saved, and the "Some considerations to check" items, if any, each with its question and options.
- When the user answers a consideration or asks for a change, treat it as the user's correction: apply it, re-run only the queries it affects, update or remove the related notes, and submit the tables that change. The connector saves the files again each time.
- In the rules below, "the app" means GRA working through these tools, and "uploaded" documents are the ones the user gave to start_report.

GRA's rules:

"""

INSTRUCTIONS = """Grant Reporting Assistant (GRA) rebuilds grant-report tables from an organization's client and case data while the client records stay on this computer. To start, the user names their past report (or the grant requirements), their data files (or the folder that holds them) and the reporting period; call start_report, which returns the full working rules to follow. find_files lists a folder's files, preview_data shows what GRA keeps private and what it shares, and start_sample_report runs on a fictional sample. Never open, read or analyze the user's client data files with any other tool; only GRA's tools may touch them."""


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return {"name": name, "description": description, "inputSchema": schema}


_PATHS = {"type": "array", "items": {"type": "string"}}
CONNECTOR_TOOLS = [
    _tool("find_files",
          "List the files in a folder on this computer (names, types and sizes only; nothing is opened), to find "
          "the user's past report and data files. Use the folder path the user gives.",
          {"folder": {"type": "string", "description": "Full path of the folder, e.g. C:\\Users\\me\\Documents\\Grant data"}},
          ["folder"]),
    _tool("preview_data",
          "Show which columns of the data files GRA keeps on this computer and what it may share (category values "
          "and totals), before starting a report. Nothing from the records themselves is returned.",
          {"data_files": {**_PATHS, "description": "Full paths of the data files (CSV, Excel or JSON)."}},
          ["data_files"]),
    _tool("start_report",
          "Start a grant report: import the data files into a protected local database, check the past report for "
          "client names and IDs, and return GRA's working rules, the report and the non-identifying description of "
          "the data. Then follow the rules: run_query, note_assumption, submit_tables. Starting again replaces the "
          "current report.",
          {"report_files": {**_PATHS, "description": "Full paths of the past report or the grant requirements (PDF, "
                                                     "Word, Excel or text). Leave out only if the user attached the "
                                                     "report to this chat instead."},
           "data_files": {**_PATHS, "description": "Full paths of the data files (CSV, Excel or JSON)."},
           "period_start": {"type": "string", "description": "First day of the reporting period, YYYY-MM-DD."},
           "period_end": {"type": "string", "description": "Last day of the reporting period, YYYY-MM-DD."}},
          ["data_files", "period_start", "period_end"]),
    _tool("start_sample_report",
          "Start a report on GRA's built-in fictional sample (an invented legal aid society's 2025 report and made-up "
          "records), to try GRA safely. Works like start_report.", {}),
    *[{"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]} for t in TOOLS],
    _tool("show_results",
          "Show the current report's finished tables, notes and saved file locations again.", {}),
]


class ChatModel:
    """Stands in for the AI service client: in the Claude app, the chat's own Claude makes the tool calls."""

    provider = PROVIDER
    model = "Claude in the Claude app (the user's own login)"
    stored = None

    def respond(self, *args, **kwargs):  # never called: the connector has no loop of its own
        raise RuntimeError("The connector does not send requests to an AI service.")


@dataclass
class ChatRun:
    id: str
    folder: Path
    session: ReportSession
    report_names: list[str]
    data_names: list[str]
    sample: bool = False
    saved: list[Path] = field(default_factory=list)
    calls: int = 0


class Connector:
    """The connector's tools. read_roots limits which folders may be read (None: anywhere);
    output_root is where finished reports are saved."""

    def __init__(self, read_roots: list[Path] | None = None, output_root: Path | None = None, word_ready=None):
        self.read_roots = [Path(r).expanduser() for r in read_roots] if read_roots else None
        self.output_root = Path(output_root).expanduser() if output_root else _default_output()
        self.word_ready = word_ready or (lambda: True)  # waits for Word support (lxml) on first use
        self.run: ChatRun | None = None

    # ------------------------------------------------------------ protocol glue

    def tools(self) -> list[dict]:
        return CONNECTOR_TOOLS

    def call(self, name: str, args: dict) -> tuple[str, bool]:
        handlers = {"find_files": self.find_files, "preview_data": self.preview_data,
                    "start_report": self.start_report, "start_sample_report": self.start_sample_report,
                    "show_results": self.show_results}
        try:
            if name in handlers:
                return handlers[name](**_known(args, handlers[name]))
            if name in ("run_query", "note_assumption", "submit_tables"):
                return self.work(name, args)
            return f"Unknown tool {name}.", True
        except UserError as e:
            return str(e), True

    # ------------------------------------------------------------ finding and previewing files

    def find_files(self, folder: str) -> tuple[str, bool]:
        root = self._folder(folder)
        found = []
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if len(rel.parts) > 3 or any(part.startswith(".") for part in rel.parts) or not p.is_file():
                continue
            suffix = p.suffix.lower()
            kind = ("data" if suffix in READERS else "") + ("/" if suffix in READERS and suffix in DOC_TYPES else "") \
                + ("report" if suffix in DOC_TYPES else "")
            if kind:
                found.append(f"- {rel.as_posix()} ({kind}, {_size(p.stat().st_size)})")
            if len(found) >= MAX_LISTED:
                found.append("- ... (more files not listed)")
                break
        if not found:
            return f"No report or data files (PDF, Word, Excel, CSV, JSON, text) were found in {root}.", False
        return (f"Files in {root} (folder: {root}). Paths are relative to it; pass full paths to the other tools. "
                "Files were listed, not opened.\n" + "\n".join(found)), False

    def preview_data(self, data_files: list) -> tuple[str, bool]:
        from .api import _preview_groups

        paths = self._files(data_files, READERS, "data")
        folder = _runs_dir() / f"preview-{uuid.uuid4().hex[:8]}"
        folder.mkdir(parents=True)
        try:
            catalog = import_files(folder / "data.db", paths)
            g = _preview_groups(folder / "data.db", catalog)
        except ValueError as e:
            raise UserError(f"Could not read the data files: {e}") from None
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        lines = ["What GRA would do with these files (no records are shown):", "",
                 "Files: " + "; ".join(f"{f['file']} ({f['rows']:,} rows)" for f in g["files"]), "",
                 "Kept on this computer, never shared (IDs, names, personal details and notes):"]
        lines += [f"- {k['header']} ({k['file']}): {k['why']}" for k in g["kept"]] or ["- none"]
        lines += ["", "Shared only as a list of possible answers, for counting:"]
        lines += [f"- {x['header']} ({x['file']}): {x['count']} values, e.g. {', '.join(x['examples'][:4])}"
                  for x in g["listed"]] or ["- none"]
        lines += ["", "Shared only as totals and ranges (dates, amounts, other columns):"]
        lines += [f"- {x['header']} ({x['file']})" for x in g["totals"]] or ["- none"]
        if g["code_lists"]:
            lines += ["", "Code lists shared in full (they hold no client information):"]
            lines += [f"- {c['file']} ({c['rows']} rows)" for c in g["code_lists"]]
        return "\n".join(lines), False

    # ------------------------------------------------------------ starting a report

    def start_sample_report(self) -> tuple[str, bool]:
        from .api import SAMPLE, SAMPLE_DATA, SAMPLE_PERIOD, SAMPLE_REPORT

        return self._start([SAMPLE / SAMPLE_REPORT], [SAMPLE / n for n in SAMPLE_DATA], SAMPLE_PERIOD, sample=True)

    def start_report(self, data_files: list, period_start: str, period_end: str,
                     report_files: list | None = None) -> tuple[str, bool]:
        from datetime import date

        try:
            if date.fromisoformat(str(period_start)) > date.fromisoformat(str(period_end)):
                raise UserError("The reporting period ends before it starts.")
        except ValueError:
            raise UserError("Give the reporting period as two dates written YYYY-MM-DD.") from None
        reports = self._files(report_files or [], DOC_TYPES, "report")
        data = self._files(data_files, READERS, "data")
        overlap = {p.resolve() for p in reports} & {p.resolve() for p in data}
        if overlap:
            raise UserError("A file was given as both a report and a data file: "
                            + ", ".join(p.name for p in overlap) + ". Give each file once.")
        return self._start(reports, data, (str(period_start), str(period_end)))

    def _start(self, reports: list[Path], data: list[Path], period: tuple[str, str], sample: bool = False):
        from .api import check_report_files
        from .documents import layout_summary

        if not data:
            raise UserError("Give at least one data file (CSV, Excel or JSON).")
        if any(p.suffix.lower() in (".docx", ".xlsx", ".xlsm") for p in reports) and not self.word_ready():
            raise UserError(WORD_MISSING)
        run_id = uuid.uuid4().hex[:12]
        folder = _runs_dir() / run_id
        folder.mkdir(parents=True)
        _prune()
        db = folder / "data.db"
        try:
            catalog = import_files(db, data)
        except ValueError as e:
            shutil.rmtree(folder, ignore_errors=True)
            raise UserError(f"Could not read the data files: {e}") from None
        gate = QueryGate(db, catalog)
        try:
            checks = check_report_files(reports, gate)
        except ValueError as e:
            shutil.rmtree(folder, ignore_errors=True)
            raise UserError(str(e).replace("Nothing was sent to the AI", "Nothing from it was shown to Claude")) from None
        blocks, unreadable = [], []
        for p in reports:
            text = report_text(p)
            if text is None:
                unreadable.append(p.name)
                continue
            blocks += [{"type": "text", "text": f"--- Uploaded reporting document: {p.name} ---"},
                       {"type": "text", "text": text}]
        if unreadable:
            shutil.rmtree(folder, ignore_errors=True)
            raise UserError(f"{', '.join(unreadable)} has no readable text (it may be a scan). Ask the user to attach "
                            "that report to this chat (it is the report, not client data), then call start_report "
                            "again without it.")
        session = ReportSession(gate, describe(db, catalog), blocks, period, ChatModel(),
                                limits=Limits(max_queries=400), activity_path=folder / "activity.jsonl",
                                layout=layout_summary(reports))
        session.begin_round()
        session.status = "running"
        rules = CHAT_RULES + SYSTEM_PROMPT
        session.activity.add("instructions", "sent", text=rules)
        session.activity.add("tools", "sent", tools=TOOLS)
        session.activity.add("request", "sent", number=1)
        opening = session.messages[0]["content"]
        session.activity.add("message", "sent", role="user", content=opening)
        self.run = ChatRun(run_id, folder, session, [p.name for p in reports], [p.name for p in data], sample)

        rows = sum(t.rows for t in catalog.tables)
        protected = sum(c.role in ("identifier", "sensitive", "free_text") for t in catalog.tables for c in t.columns)
        head = [f"Report started{' on the fictional sample' if sample else ''}. Imported {rows:,} records from "
                f"{len(data)} file(s) into {len(catalog.tables)} tables on this computer; {protected} identifying or "
                "free-text columns are protected and never shown."]
        head += [m for _, m in checks]
        if not reports:
            head.append("No report file was given: use the report or requirements the user attached to this chat.")
        text = "\n".join(head) + "\n\n=== HOW TO WORK ===\n\n" + rules + "\n\n=== THE REPORT AND THE DATA ===\n\n" \
            + "\n\n".join(b["text"] for b in opening)
        return text, False

    # ------------------------------------------------------------ the analyst's tools

    def work(self, name: str, args: dict) -> tuple[str, bool]:
        run = self._current()
        s = run.session
        run.calls += 1
        use_id = f"chat{run.calls}"
        s.activity.add("response", "received", content=[{"type": "tool_use", "id": use_id, "name": name, "input": args}],
                       stop_reason="tool_use", model=ChatModel.model)
        if name == "submit_tables" and not self.word_ready():
            out, err, accepted = WORD_MISSING, True, False
        else:
            out, err, accepted = s.handle_tool(name, args if isinstance(args, dict) else {})
        s.activity.add("message", "sent", role="user", content=[
            {"type": "tool_result", "tool_use_id": use_id, "content": out, **({"is_error": True} if err else {})}])
        s.activity.add("request", "sent", number=run.calls + 1)
        if not accepted:
            return out, err
        s.status = "done"
        s.begin_round()  # a later correction starts a fresh round, keeping these tables
        try:
            saved = self._save(run)
        except Exception as e:  # the tables stand even if a file could not be written
            return (out + f"\n\nThe tables were accepted, but the files could not be saved ({type(e).__name__}: {e})."
                    "\n\n" + results_markdown(s)), False
        return (out + "\n\n" + _files_text(saved) + "\n\nShow the user the tables below exactly as written, then the "
                "considerations, if any.\n\n" + results_markdown(s)), False

    def show_results(self) -> tuple[str, bool]:
        run = self._current()
        if not run.session.tables:
            return "No tables have been accepted yet in this report.", False
        return _files_text(run.saved) + "\n\n" + results_markdown(run.session), False

    # ------------------------------------------------------------ saving

    def _save(self, run: ChatRun) -> list[Path]:
        from .activity_report import to_report_docx
        from .data_quality import has_gaps, to_quality_docx
        from .exports import to_docx, to_xlsx

        s = run.session
        tables, notes = s.tables, list(s.notes.values())
        start, end = s.period
        name = " ".join(x for x in (s.report.get("organization"), s.report.get("title")) if x) or "Grant report"
        out = self.output_root / _safe(f"{name} {start} to {end}")
        out.mkdir(parents=True, exist_ok=True)
        stamp = end.replace("-", "")
        period = f"{start} to {end}"
        files = {
            f"grant-report-tables-{stamp}.docx": lambda: to_docx(tables, notes, "Grant report tables", period, s.report,
                                                                  s.page_orientation),
            f"grant-report-tables-{stamp}.xlsx": lambda: to_xlsx(tables, notes, "Grant report tables", period, s.report,
                                                                  s.page_orientation),
            f"ai-activity-report-{stamp}.docx": lambda: to_report_docx(s.activity.events, s.gate.catalog,
                                                                       s.gate.contains_protected,
                                                                       s.gate.protected_value_count()),
        }
        if has_gaps(tables, notes):
            files[f"data-collection-guide-{stamp}.docx"] = lambda: to_quality_docx(
                tables, notes, s.gate.catalog, [q.sql for q in s.gate.log], s.report, (start, end))
        saved = []
        for fname, make in files.items():
            path = out / fname
            path.write_bytes(make())
            saved.append(path)
        run.saved = saved
        return saved

    # ------------------------------------------------------------ helpers

    def _current(self) -> ChatRun:
        if self.run is None:
            raise UserError("No report has been started. Call start_report (or start_sample_report) first.")
        return self.run

    def _allowed(self, path: Path) -> Path:
        from .api import SAMPLE

        p = path.expanduser()
        if not p.is_absolute():
            raise UserError(f"Give the full path of {path} (for example C:\\Users\\me\\Documents\\{path.name}).")
        p = p.resolve()
        if _inside(p, SAMPLE.resolve()):
            return p
        if self.read_roots is not None and not any(_inside(p, r.resolve()) for r in self.read_roots):
            raise UserError(f"{p} is outside the folders GRA may read ({', '.join(str(r) for r in self.read_roots)}). "
                            "The user can add its folder in the Claude app: Settings, Extensions, Grant Reporting "
                            "Assistant, Configure, then 'Folders GRA may read'.")
        if any(part.startswith(".") for part in p.parts[1:]):
            raise UserError(f"{p} is in a hidden folder, which GRA does not read.")
        return p

    def _folder(self, folder: str) -> Path:
        p = self._allowed(Path(str(folder)))
        if not p.is_dir():
            raise UserError(f"There is no folder at {p}.")
        return p

    def _files(self, items, allowed: set | dict, what: str) -> list[Path]:
        if isinstance(items, str):
            items = [items]
        out = []
        for item in items or []:
            p = self._allowed(Path(str(item)))
            if p.is_dir():
                raise UserError(f"{p} is a folder. Call find_files on it and pass the {what} files themselves.")
            if not p.is_file():
                raise UserError(f"There is no file at {p}.")
            if p.suffix.lower() not in allowed:
                kinds = ", ".join(sorted(allowed))
                raise UserError(f"{p.name} is not a {what} file GRA can read ({kinds}).")
            out.append(p)
        return out


class UserError(Exception):
    """A problem to report back to Claude (and the user) in plain words."""


WORD_MISSING = ("Word support is still being set up on this computer (a one-time download), or it could not be "
                "installed. Wait a minute and try again. If it keeps failing, check the internet connection, then "
                "restart the Claude app.")


def report_text(path: Path) -> str | None:
    """The report's text for Claude, keeping the layout of printed tables; None if it has no readable text."""
    from .documents import extract_text

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        try:
            pages = PdfReader(path).pages
            parts = []
            for i, page in enumerate(pages, start=1):
                try:
                    text = page.extract_text(extraction_mode="layout")
                except Exception:
                    text = page.extract_text() or ""
                parts.append(f"[Page {i} of {len(pages)}]\n{text}")
            text = "\n\n".join(parts)
        except Exception:
            return None
        if len(re.sub(r"\[Page \d+ of \d+\]|\s", "", text)) < 200:
            return None
    else:
        text = extract_text(path)
    lines = [ln.rstrip() for ln in text.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[:MAX_TEXT_CHARS] if text else None


def results_markdown(s: ReportSession) -> str:
    """The finished tables and notes as Markdown, the way the app's review screen shows them."""
    from .exports import heading_lines

    title, lines = heading_lines(s.report, "Grant report tables")
    out = [f"## {title}"] + [f"*{_md(x)}*" for x in lines] + [f"Reporting period: {s.period[0]} to {s.period[1]}", ""]
    for t in s.tables:
        out += _table_md(t) + [""]
    notes = list(s.notes.values())
    key = [n for n in notes if n.key]
    if key:
        out.append("### Some considerations to check")
        for n in key:
            out.append(f"- {_md(n.text)}" + (f" **{_md(n.question)}**" if n.question else ""))
            for o in n.options:
                out.append(f"  - {_md(o['label'])}")
        out.append("")
    other = [n for n in notes if not n.key]
    if other:
        out.append("### Assumptions and notes")
        out += [f"- {_md(n.text)}" for n in other]
        out.append("")
    if any(c.gap for t in s.tables for r in t.rows for c in r.cells):
        out.append(f"{GAP_MARK} = not available from the data.")
    return "\n".join(out).rstrip()


def _table_md(t: Table) -> list[str]:
    out = [f"### {_md(t.title)}"]
    if t.subtitle:
        out.append(f"*{_md(t.subtitle)}*")
    out += ["", "| " + " | ".join([_md(t.row_header) or " "] + [_md(h) for h in t.headers]) + " |",
            "|" + "---|" + "---:|" * len(t.headers)]
    for r in t.rows:
        label = "\u2003" * max(0, r.indent) + _md(r.label)
        cells = [_md(c.display) for c in r.cells] + [""] * (len(t.headers) - len(r.cells))
        if r.kind in ("subtotal", "total", "heading"):
            label = f"**{label.strip()}**" if label.strip() else label
            cells = [f"**{c}**" if c and r.kind != "heading" else c for c in cells]
        out.append("| " + " | ".join([label] + cells[:len(t.headers)]) + " |")
    if t.note:
        out += ["", f"*{_md(t.note)}*"]
    return out


def _md(text) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


def _files_text(saved: list[Path]) -> str:
    if not saved:
        return "No files have been saved yet."
    return f"Saved in {saved[0].parent}:\n" + "\n".join(f"- {p.name}" for p in saved)


def _known(args: dict, fn) -> dict:
    import inspect

    names = set(inspect.signature(fn).parameters)
    return {k: v for k, v in (args or {}).items() if k in names}


def _inside(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9 ._-]+", "-", name).strip(" .-")[:120] or "Grant report"


def _size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


def _runs_dir() -> Path:
    d = app_data_dir() / "connector-runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune():
    folders = sorted((p for p in _runs_dir().iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in folders[KEEP_RUNS:]:
        shutil.rmtree(old, ignore_errors=True)


def _default_output() -> Path:
    return Path.home() / "Documents" / "Grant Reporting Assistant"


def main(argv: list[str] | None = None, word_ready=None):
    """Run the connector over standard input and output, as the Claude app starts it.

    Arguments: --output FOLDER (reports are saved in its "Grant Reporting Assistant" folder) and --allow FOLDER [FOLDER ...] (folders it may read)."""
    from .mcp_stdio import ToolServer

    argv = list(sys.argv[1:] if argv is None else argv)
    output, roots, mode = None, [], None
    for a in argv:
        if a in ("--output", "--allow"):
            mode = a
        elif mode == "--output" and output is None and a.strip() and "${" not in a:
            output = a
        elif mode == "--allow" and a.strip() and "${" not in a:
            roots.append(Path(a))
    connector = Connector(roots or None, Path(output) / "Grant Reporting Assistant" if output else None, word_ready)
    ToolServer("grant-reporting-assistant", __version__, INSTRUCTIONS, connector.tools, connector.call).serve()


if __name__ == "__main__":
    main()
