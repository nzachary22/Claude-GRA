"""Grant Reporting Assistant as a connector for the Claude app or the ChatGPT app, with no API key.

The user chats with Claude (Claude Desktop) or ChatGPT (the ChatGPT desktop app) under their
normal login. The chat's AI calls this connector's tools on the user's computer, and the connector does what the app's
built-in analyst loop does, with the same code and the same privacy gate:

- the data files are imported into a local SQLite database, read where they are;
- Claude receives the reporting rules, the report or requirements (checked first
  for client names and IDs from the data), the non-identifying data description,
  and aggregate query results only, never individual records;
- every figure in a finished table comes from a local query result, and the
  connector writes the Word and Excel files and the AI activity report itself.

The difference from the app is only who asks the questions: there the app sends
them to the AI service with an API key; here the AI in the chat calls the
tools. See gra/engine.py for the shared rules and checks.

The two chat apps differ only in how the finished tables are delivered (see Client):
Claude builds a Claude doc and a Claude sheet; ChatGPT shows the tables in the chat,
next to the Word and Excel files saved on this computer, which both always get.
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

DOC_TYPES = {".pdf", ".docx", ".xlsx", ".xlsm", ".txt", ".md"}  # CSV files are always treated as data
RECORD_ROWS = 200  # a report sheet this long with ID, personal or note columns looks like client records
MAX_TEXT_CHARS = 400_000
KEEP_RUNS = 5
MAX_LISTED = 300

DELIVERY_RULES = """Delivering the finished tables (do this by default, without asking):
1. Call get_doc_and_sheet. It returns the finished tables twice, ready to use: as a Claude doc (a title and Markdown) and as a Claude sheet (a title and the cells of each tab).
2. Make the Claude doc with Claude's own docs feature (the Claude Docs connector): use the given title and tab name, and put the given Markdown in it as written. Copy every table, figure and note exactly; never retype, round, recalculate, reorder or leave out a figure.
3. Make the Claude sheet with Claude's own Sheets feature (an artifact of the Sheets type, whose cells are written through the Claude Docs connector): use the given title, one tab per entry in "tabs", in order, with the tab's name. Write each tab's "rows" from cell A1 exactly as given: every value is already written the way the sheet reads it, so numbers, percents and dollar amounts come out as numbers and text stays text. Then declare each tab's table (its header row and, if listed, its total rows as the footer), make "bold_rows" bold, wrap the Notes tab's long text and fit the columns. Send no more than about 4,000 cells in one update.
4. In the chat, give the links to the doc and the sheet, say where the Word and Excel files were saved on this computer, and list the "Some considerations to check" items, if any, each with its question and options. Do not repeat the full tables in the chat once the doc holds them.
- After a correction, update the same doc and sheet with the new get_doc_and_sheet content instead of making new ones: replace the doc's content, and rewrite each tab (clear cells that no longer hold anything). If that fails, make new ones and say so.
- If Claude docs or Claude sheets are not available in this chat (no Claude Docs connector, or it refuses), skip what cannot be made without retrying, show the finished tables in the chat exactly as the submit_tables reply gives them, and say once that the same tables are in the Word and Excel files on this computer.
- The doc and the sheet hold only what get_doc_and_sheet returns: the finished tables and notes, which are totals, never client records. Add nothing else from the data to them."""

CHATGPT_DELIVERY_RULES = """Delivering the finished tables (do this by default, without asking):
1. The connector has saved the finished tables as Word and Excel files on this computer; they are the deliverable.
2. In the chat, show the finished tables exactly as the submit_tables reply gives them: copy every table, figure and note exactly; never retype, round, recalculate, reorder or leave out a figure.
3. Then say where the Word and Excel files were saved on this computer, and list the "Some considerations to check" items, if any, each with its question and options.
- Do not rebuild the tables in other documents, spreadsheets or canvases, and do not write or run code to make files from them: the saved files already hold them, and every copy is a chance to change a figure.
- After a correction, show the tables that changed in the same way and say that the files were saved again."""

CHAT_RULES = """You are working in the user's {ai} chat with the Grant Reporting Assistant (GRA) connector. GRA keeps the organization's client data on this computer: it imports the data files into a local database and lets you see only a non-identifying description of the data and aggregate query results. You act as GRA's reporting analyst. Follow GRA's rules below exactly, using the connector's tools run_query, note_assumption and submit_tables.

Working in the chat:
- Never ask the user to paste, attach or upload client data, and never open, read or analyze the data files with any other tool or connector ({other_tools}). Only GRA's tools may touch them. If client records appear in the chat anyway, do not repeat or use them, and tell the user once that GRA keeps records private only when the files are given to GRA by their location on the computer.
- Once the report is started, work through every table without stopping to check in with the user, then submit them all. Mention briefly what you are doing as you go.
- When the tables are accepted, the connector saves the Word and Excel files on this computer and replies with the finished tables. Then {deliver}, as described after these rules.
- When the user answers a consideration or asks for a change, treat it as the user's correction: apply it, re-run only the queries it affects, update or remove the related notes, and submit the tables that change. The connector saves the files again each time, and you {redeliver} (see below).
- In the rules below, "the app" means GRA working through these tools, and "uploaded" documents are the ones the user gave to start_report.

{delivery}

GRA's rules:

"""

CLAUDE_INSTRUCTIONS = """Grant Reporting Assistant (GRA) rebuilds grant-report tables from an organization's client and case data while the client records stay on this computer. To start, the user names their past report (or the grant requirements), their data files (or the folder that holds them) and the reporting period; call start_report, which returns the full working rules to follow. find_files lists a folder's files, preview_data shows what GRA keeps private and what it shares, and start_sample_report runs on a fictional sample. When the tables are done, get_doc_and_sheet gives them ready to deliver as a Claude doc and a Claude sheet, the default way to show the results. Never open, read or analyze the user's client data files with any other tool; only GRA's tools may touch them."""

CHATGPT_INSTRUCTIONS = """Grant Reporting Assistant (GRA) rebuilds grant-report tables from an organization's client and case data while the client records stay on this computer. Never open, read, list or analyze the user's client data files with any other tool (no shell commands, scripts or file reads); only GRA's tools may touch them. To start, the user names their past report (or the grant requirements), their data files (or the folder that holds them) and the reporting period; call start_report, which returns the full working rules to follow. find_files lists a folder's files, preview_data shows what GRA keeps private and what it shares, and start_sample_report runs on a fictional sample. When the tables are done, GRA saves them as Word and Excel files on this computer, and you show them in the chat exactly as GRA gives them."""


@dataclass(frozen=True)
class Client:
    """The chat app the connector serves: what the AI and the app are called, and how results are delivered."""

    key: str
    ai: str
    app: str
    provider: str  # recorded in the AI activity report
    model: str
    instructions: str
    delivery: str
    next_step: str
    other_tools: str
    deliver: str
    redeliver: str
    add_folder: str
    doc_and_sheet: bool

    def rules(self) -> str:
        return CHAT_RULES.format(ai=self.ai, other_tools=self.other_tools, deliver=self.deliver,
                                 redeliver=self.redeliver, delivery=self.delivery)


CLAUDE = Client(
    key="claude", ai="Claude", app="the Claude app", provider="claude-app",
    model="Claude in the Claude app (the user's own login)", instructions=CLAUDE_INSTRUCTIONS,
    delivery=DELIVERY_RULES,
    next_step=("Next, call get_doc_and_sheet and deliver the tables as a Claude doc and a Claude sheet, as GRA's rules "
               "describe. If Claude docs and sheets are not available in this chat, show the user the tables below "
               "exactly as written instead, then the considerations, if any."),
    other_tools="file readers, code execution, computer use or a browser",
    deliver="deliver them as a Claude doc and a Claude sheet", redeliver="update the same Claude doc and Claude sheet",
    add_folder=("The user can add its folder in the Claude app: Settings, Extensions, Grant Reporting Assistant, "
                "Configure, then 'Folders GRA may read'."),
    doc_and_sheet=True)

CHATGPT = Client(
    key="chatgpt", ai="ChatGPT", app="the ChatGPT app", provider="chatgpt-app",
    model="ChatGPT in the ChatGPT app (the user's own login)", instructions=CHATGPT_INSTRUCTIONS,
    delivery=CHATGPT_DELIVERY_RULES,
    next_step=("Next, show the user the tables below exactly as written, say where the Word and Excel files were saved "
               "on this computer, then list the considerations, if any."),
    other_tools=("shell or terminal commands, scripts, file readers, code execution, computer use or a browser; "
                 "to list a folder, use find_files"),
    deliver="show them to the user", redeliver="show the tables that changed again",
    add_folder=("The user can add its folder by running the Grant Reporting Assistant installer for ChatGPT again, "
                "choosing the folder when it asks, and then quitting and reopening the ChatGPT app."),
    doc_and_sheet=False)

CLIENTS = {c.key: c for c in (CLAUDE, CHATGPT)}


def detect_client(client_info: dict | None) -> Client | None:
    """The chat app named in the MCP handshake (clientInfo), if it is one GRA knows; None otherwise."""
    name = str((client_info or {}).get("name") or "").lower()
    if any(w in name for w in ("codex", "chatgpt", "openai")):
        return CHATGPT
    if "claude" in name:
        return CLAUDE
    return None


# Compatibility names: the Claude app's wording, used by tests and older callers.
PROVIDER = CLAUDE.provider
INSTRUCTIONS = CLAUDE_INSTRUCTIONS


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return {"name": name, "description": description, "inputSchema": schema}


# MCP tool hints: every tool works on this computer only (closed world) and none deletes the user's files.
# Read-only tools only look; the others keep the report's state or save the finished files.
READ_ONLY = {"find_files", "preview_data", "run_query", "show_results", "get_doc_and_sheet"}
TITLES = {"find_files": "Find report and data files", "preview_data": "Preview what stays private",
          "start_report": "Start a report", "start_sample_report": "Start the sample report",
          "run_query": "Run a privacy-checked count", "note_assumption": "Note an assumption",
          "submit_tables": "Check and save the tables", "show_results": "Show the finished tables",
          "get_doc_and_sheet": "Get the tables for a doc and sheet"}


def _annotated(tool: dict) -> dict:
    name = tool["name"]
    return {**tool, "annotations": {"title": TITLES.get(name, name), "readOnlyHint": name in READ_ONLY,
                                    "destructiveHint": False, "idempotentHint": name in READ_ONLY,
                                    "openWorldHint": False}}


_PATHS = {"type": "array", "items": {"type": "string"}}
CONNECTOR_TOOLS = [
    _tool("find_files",
          "List the files in a folder and its subfolders one level down (names, types and sizes only; nothing is "
          "opened), to find the user's past report and data files. Use the folder path the user gives.",
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
           "period_end": {"type": "string", "description": "Last day of the reporting period, YYYY-MM-DD."},
           "time_zone": {"type": "string", "description": "Optional. The time zone the organization reports in, as a "
                                                          "name such as America/Los_Angeles, when the report, the "
                                                          "requirements or the user name one. Timestamps in the data "
                                                          "that carry a time zone (such as 2027-01-05T19:00:35Z) are "
                                                          "shown as local date and time there. Default: this "
                                                          "computer's time zone."}},
          ["data_files", "period_start", "period_end"]),
    _tool("start_sample_report",
          "Start a report on GRA's built-in fictional sample (an invented legal aid society's 2025 report and made-up "
          "records), to try GRA safely. Works like start_report.", {}),
    *[{"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]} for t in TOOLS],
    _tool("show_results",
          "Show the current report's finished tables, notes and saved file locations again.", {}),
    _tool("get_doc_and_sheet",
          "Get the current report's finished tables and notes ready to deliver as a Claude doc (title and Markdown) "
          "and a Claude sheet (title and the cells of each tab), the default way to give the user the results. "
          "Holds only the finished tables and notes (totals), never client records.", {}),
]
CONNECTOR_TOOLS = [_annotated(t) for t in CONNECTOR_TOOLS]


class ChatModel:
    """Stands in for the AI service client: in a chat app, the chat's own AI makes the tool calls."""

    stored = None

    def __init__(self, client: Client = CLAUDE):
        self.provider = client.provider
        self.model = client.model

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
    output_root is where finished reports are saved. client is the chat app (Claude by default); when
    fixed_client is False, the app named in the MCP handshake replaces it."""

    def __init__(self, read_roots: list[Path] | None = None, output_root: Path | None = None, word_ready=None,
                 client: Client = CLAUDE, fixed_client: bool = True):
        self.client = client
        self.fixed_client = fixed_client
        self.read_roots = [Path(r).expanduser() for r in read_roots] if read_roots else None
        self.output_root = Path(output_root).expanduser() if output_root else _default_output()
        self.word_ready = word_ready or (lambda: True)  # waits for Word support (lxml) on first use
        self.run: ChatRun | None = None

    # ------------------------------------------------------------ protocol glue

    def initialized(self, params: dict) -> None:
        """Called with the MCP handshake: picks the chat app's wording unless the start command named one."""
        found = detect_client(params.get("clientInfo") if isinstance(params, dict) else None)
        if found and not self.fixed_client:
            self.client = found

    def instructions(self) -> str:
        return self.client.instructions

    def tools(self) -> list[dict]:
        if self.client.doc_and_sheet:
            return CONNECTOR_TOOLS
        return [t for t in CONNECTOR_TOOLS if t["name"] != "get_doc_and_sheet"]

    def call(self, name: str, args: dict) -> tuple[str, bool]:
        handlers = {"find_files": self.find_files, "preview_data": self.preview_data,
                    "start_report": self.start_report, "start_sample_report": self.start_sample_report,
                    "show_results": self.show_results,
                    "get_doc_and_sheet": self.get_doc_and_sheet}
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
            if len(rel.parts) > 2 or any(part.startswith(".") for part in rel.parts) or not p.is_file():
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
        lines = ["What GRA would do with these files (no records are shown). Your data files stay on this computer, "
                 "and the counting happens here. The AI only gets what is described below.", "",
                 "Files: " + "; ".join(f"{f['file']} ({f['rows']:,} rows)" for f in g["files"]), "",
                 "The AI never sees these (IDs, names, personal details and notes):"]
        lines += [f"- {k['header']} ({k['file']}): {k['why']}" for k in g["kept"]] or ["- none"]
        lines += ["", "The AI sees the list of possible answers, for counting:"]
        lines += [f"- {x['header']} ({x['file']}): {x['count']} values, e.g. {', '.join(x['examples'][:4])}"
                  for x in g["listed"]] or ["- none"]
        lines += ["", "The AI sees totals and ranges only (dates, amounts, other columns):"]
        lines += [f"- {x['header']} ({x['file']})" for x in g["totals"]] or ["- none"]
        if g["code_lists"]:
            lines += ["", "The AI sees these code lists in full (they hold no client information):"]
            lines += [f"- {c['file']} ({c['rows']} rows)" for c in g["code_lists"]]
        return "\n".join(lines), False

    # ------------------------------------------------------------ starting a report

    def start_sample_report(self) -> tuple[str, bool]:
        from .api import SAMPLE, SAMPLE_DATA, SAMPLE_PERIOD, SAMPLE_REPORT

        return self._start([SAMPLE / SAMPLE_REPORT], [SAMPLE / n for n in SAMPLE_DATA], SAMPLE_PERIOD, sample=True)

    def start_report(self, data_files: list, period_start: str, period_end: str,
                     report_files: list | None = None, time_zone: str | None = None) -> tuple[str, bool]:
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
        return self._start(reports, data, (str(period_start), str(period_end)), time_zone=time_zone or None)

    def _start(self, reports: list[Path], data: list[Path], period: tuple[str, str], sample: bool = False,
               time_zone: str | None = None):
        from .api import check_report_files
        from .documents import layout_summary

        if not data:
            raise UserError("Give at least one data file (CSV, Excel or JSON).")
        if any(p.suffix.lower() in (".docx", ".xlsx", ".xlsm") for p in reports) and not self.word_ready():
            raise UserError(word_missing(self.client))
        run_id = uuid.uuid4().hex[:12]
        folder = _runs_dir() / run_id
        folder.mkdir(parents=True)
        _prune()
        db = folder / "data.db"
        try:
            catalog = import_files(db, data, time_zone)
        except ValueError as e:
            shutil.rmtree(folder, ignore_errors=True)
            raise UserError(f"Could not read the data files: {e}") from None
        gate = QueryGate(db, catalog)
        try:
            for p in reports:
                _refuse_records(p, folder, self.client.ai)
            checks = check_report_files(reports, gate)
        except ValueError as e:
            shutil.rmtree(folder, ignore_errors=True)
            raise UserError(str(e).replace("Nothing was sent to the AI", f"Nothing from it was shown to {self.client.ai}")) from None
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
        session = ReportSession(gate, describe(db, catalog), blocks, period, ChatModel(self.client),
                                limits=Limits(max_queries=400), activity_path=folder / "activity.jsonl",
                                layout=layout_summary(reports))
        session.begin_round()
        session.status = "running"
        rules = self.client.rules() + SYSTEM_PROMPT
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
        zoned = [f"{t.name}.{c.name}" for t in catalog.tables for c in t.columns if c.note.startswith("converted from timestamps")]
        if zoned:
            where = f"the {time_zone} time zone" if time_zone else "this computer's time zone"
            head.append(f"{len(zoned)} timestamp column(s) that carry a time zone (such as {zoned[0]}) are shown as local "
                        f"date and time in {where}, so their date part is the local calendar day. If the report, the "
                        "requirements or the user say the organization reports in a different time zone, call "
                        "start_report again with time_zone set to it (e.g. America/Los_Angeles).")
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
                       stop_reason="tool_use", model=self.client.model)
        if name == "submit_tables" and not self.word_ready():
            out, err, accepted = word_missing(self.client), True, False
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
                    "\n\n" + self.client.next_step + "\n\n" + results_markdown(s)), False
        return (out + "\n\n" + _files_text(saved) + "\n\n" + self.client.next_step + "\n\n" + results_markdown(s)), False

    def show_results(self) -> tuple[str, bool]:
        run = self._current()
        if not run.session.tables:
            return "No tables have been accepted yet in this report.", False
        return _files_text(run.saved) + "\n\n" + results_markdown(run.session), False

    def get_doc_and_sheet(self) -> tuple[str, bool]:
        import json

        run = self._current()
        if not run.session.tables:
            return "No tables have been accepted yet in this report.", False
        if not self.client.doc_and_sheet:
            return (f"Claude docs and sheets are not available in {self.client.app}. " + self.client.next_step
                    + "\n\n" + _files_text(run.saved) + "\n\n" + results_markdown(run.session)), False
        out = {"doc": claude_doc(run.session), "sheet": claude_sheet(run.session),
               "saved_files": {"folder": str(run.saved[0].parent), "files": [p.name for p in run.saved]} if run.saved else None}
        return ("Deliver these as a Claude doc and a Claude sheet, as GRA's rules describe (copy every value exactly).\n\n"
                + json.dumps(out, ensure_ascii=False, indent=1)), False

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
                            + self.client.add_folder)
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
    """A problem to report back to the chat's AI (and the user) in plain words."""


def word_missing(client: Client = CLAUDE) -> str:
    return ("Word support is still being set up on this computer (a one-time download), or it could not be "
            "installed. Wait a minute and try again. If it keeps failing, check the internet connection, then "
            f"restart {client.app}.")


WORD_MISSING = word_missing(CLAUDE)


def _refuse_records(path: Path, work: Path, ai: str = "Claude"):
    """Refuse a spreadsheet given as the report that looks like client records rather than report tables.

    The report is shown to Claude in full, so this stops a data export passed as the report, even one
    whose IDs are not in the imported data (the check that follows only knows the imported data)."""
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        return
    try:
        catalog = import_files(work / "report-check.db", [path])
    except Exception:
        return  # not a plain table layout: a formatted report
    finally:
        (work / "report-check.db").unlink(missing_ok=True)
    for t in catalog.tables:
        roles = {c.role for c in t.columns}
        if not t.reference and ("sensitive" in roles or (t.rows >= RECORD_ROWS and roles & {"identifier", "free_text"})):
            raise ValueError(f"{path.name} looks like client records (sheet with {t.rows:,} rows and ID, personal or "
                             f"note columns), not a report. Nothing from it was shown to {ai}. Give it as a data file, "
                             "or give the past report or grant requirements as the report.")


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


NEXT_STEP = CLAUDE.next_step
SHEET_NAME_MAX = 31  # the same tab names as the Excel file
_MONTH_DAY = re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(st|nd|rd|th)?$", re.I)


def claude_doc(s: ReportSession) -> dict:
    """The finished tables as a Claude doc: a title and the review screen's Markdown under it."""
    from .exports import heading_lines

    head, _ = heading_lines(s.report, "Grant report")
    title = f"{head} tables, {s.period[0]} to {s.period[1]}"
    _, _, rest = results_markdown(s).partition("\n")  # the doc's title replaces the first heading
    lead, _, body = rest.partition("\n\n")  # the lines under the title, each its own paragraph
    body = re.sub(r"^### ", "## ", body, flags=re.M)
    return {"title": title, "tab_name": "Tables", "markdown": "\n\n".join([f"# {title}", *lead.splitlines(), body])}


def claude_sheet(s: ReportSession) -> dict:
    """The finished tables as a Claude sheet: one tab per table, laid out like the Excel file, plus a Notes tab.

    Every cell is written the way the sheet reads typed input: a figure as its display text ("1,234", "12.5%",
    "$1,200.00"), so it becomes that number with that format; any text that the sheet would read as a number,
    a date or a formula starts with an apostrophe, which keeps it as text."""
    from .exports import _note_line, _sheet_name, heading_lines, notes_for, shared_notes, key_notes

    head, below = heading_lines(s.report, "Grant report tables")
    period = f"{s.period[0]} to {s.period[1]}"
    notes = list(s.notes.values())
    shared = shared_notes(s.tables, notes) + key_notes(notes)
    used: set[str] = set()
    tabs = []
    for t in s.tables:
        width = len(t.headers) + 1
        rows = [[_text(t.title)], [_text(t.subtitle or f"{head} · {period}")], [],
                [_text(t.row_header)] + [_text(h) for h in t.headers]]
        bold, totals = [1, 4], []
        for r in t.rows:
            label = "    " * max(0, r.indent) + str(r.label or "")
            if r.kind == "heading":
                rows.append([_text(label)])
                bold.append(len(rows))
                continue
            rows.append([_text(label)] + [_figure(c) for c in r.cells[:len(t.headers)]])
            if r.kind in ("subtotal", "total"):
                bold.append(len(rows))
            if r.kind == "total":
                totals.append(len(rows))
        last = len(rows)
        lines = ([t.note] if t.note else []) + _gap_note(t) + [_note_line(n) for n in notes_for(t, notes)]
        if shared:
            lines.append("See the Notes tab for assumptions that apply to more than one table.")
        if lines:
            rows.append([])
            rows += [[_text(x)] for x in lines]
        tabs.append({"name": _sheet_name(t.title or t.id, used), "rows": _rect(rows, width),
                     "table": {"header_row": 4, "last_row": last, "columns": f"A:{_col(width)}",
                               "total_rows": [n for n in totals if n == last]},
                     "bold_rows": bold})
    rows = [[_text("Assumptions, gaps and warnings")], [_text(" · ".join([head, *below, period]))], [],
            ["Type", "Note", "Tables", "How to change"]]
    for n in sorted(notes, key=lambda n: not n.key):
        affected = "; ".join(t.title for t in s.tables if t.id in n.tables) or "All tables"
        kind = f"consideration ({n.kind})" if n.key else n.kind
        rows.append([_text(kind), _text(n.text + (" " + n.question if n.key and n.question else "")
                           + ("".join(f" Option: {o['label']}." for o in n.options) if n.key else "")),
                     _text(affected), _text(n.how_to_change)])
    tabs.append({"name": "Notes", "rows": _rect(rows, 4),
                 "table": {"header_row": 4, "last_row": max(4, len(rows)), "columns": "A:D", "total_rows": []},
                 "bold_rows": [1, 4], "wrap_columns": "B:D"})
    return {"title": f"{head} tables, {period}", "tabs": tabs}


def _figure(c) -> str | None:
    if c.gap:
        return GAP_MARK
    if c.value is None or c.display == "":
        return None
    return c.display if c.is_number else _text(c.display)


def _text(x) -> str | None:
    """Text that stays text in a Claude sheet: an apostrophe first where it would be read as something else."""
    x = str(x or "").rstrip()
    core = x.lstrip()
    if not core:
        return None
    if core[0] in "=+-@'$.(%" or core[0].isdigit() or core.upper() in ("TRUE", "FALSE") or _MONTH_DAY.match(core):
        return "'" + x
    return x


def _gap_note(t: Table) -> list[str]:
    from .exports import _gap_lines

    return _gap_lines(t)


def _rect(rows: list[list], width: int) -> list[list]:
    width = max([width] + [len(r) for r in rows])
    return [r + [None] * (width - len(r)) for r in rows]


def _col(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


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
    """Run the connector over standard input and output, as the Claude app or the ChatGPT app starts it.

    Arguments: --output FOLDER (reports are saved in its "Grant Reporting Assistant" folder), --allow FOLDER
    [FOLDER ...] (folders it may read) and --client claude|chatgpt (the chat app; without it, the app named in
    the MCP handshake, else Claude)."""
    from .mcp_stdio import ToolServer

    argv = list(sys.argv[1:] if argv is None else argv)
    output, roots, mode, client = None, [], None, None
    for a in argv:
        if a in ("--output", "--allow", "--client"):
            mode = a
        elif mode == "--output" and output is None and a.strip() and "${" not in a:
            output = a
        elif mode == "--allow" and a.strip() and "${" not in a:
            roots.append(Path(a))
        elif mode == "--client" and client is None:
            client = CLIENTS.get(a.strip().lower())
            mode = None
    connector = Connector(roots or None, Path(output) / "Grant Reporting Assistant" if output else None, word_ready,
                          client=client or CLAUDE, fixed_client=client is not None)
    ToolServer("grant-reporting-assistant", __version__, connector.instructions, connector.tools, connector.call,
               on_initialize=connector.initialized).serve()

if __name__ == "__main__":
    main()
