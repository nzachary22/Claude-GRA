"""A record of everything sent to and received from the AI during a report run.

Events are written to the run's folder just before each request goes out, so the
record exists even if the request fails or is cancelled. Uploaded PDFs and images
are recorded by name, size and fingerprint rather than as unreadable encoded data.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path


def plain(block):
    """A content block as plain data, whether it is a dict or an SDK object."""
    if isinstance(block, dict):
        out = block
    elif hasattr(block, "model_dump"):
        out = block.model_dump(exclude_none=True)
    else:
        out = {"type": type(block).__name__, "text": str(block)}
    # Opaque replay data (signatures, OpenAI's encrypted reasoning and raw items) is not readable, so not recorded.
    out = {k: v for k, v in out.items() if k not in ("signature", "provider_item")}
    source = out.get("source")
    if isinstance(source, dict) and source.get("type") == "base64":
        raw = base64.standard_b64decode(source.get("data", ""))
        out = {"type": out.get("type"), "title": out.get("title"), "media_type": source.get("media_type"),
               "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    return out


class ActivityLog:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self.events: list[dict] = []

    def add(self, kind: str, direction: str, **fields):
        event = {"seq": len(self.events) + 1, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "direction": direction, "kind": kind, **fields}
        self.events.append(event)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")


def read_events(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    with Path(path).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def sent_text(event: dict) -> str:
    """The readable text of an outgoing event, for checking against protected values."""
    if event.get("direction") != "sent":
        return ""
    if event["kind"] == "instructions":
        return event.get("text", "")
    if event["kind"] != "message":
        return ""
    parts = []
    for b in event.get("content") or []:
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") == "tool_result":
            parts.append(str(b.get("content", "")))
    return "\n".join(parts)


def _block_lines(b: dict) -> list[str]:
    kind = b.get("type")
    if kind == "text":
        return [b.get("text", "")]
    if kind in ("document", "image"):
        return [f"[{kind.title()} sent in full: {b.get('title') or 'uploaded file'}, {b.get('media_type')}, "
                f"{b.get('bytes')} bytes, SHA-256 {b.get('sha256')}]"]
    if kind == "tool_result":
        label = "Refused or failed result" if b.get("is_error") else "Result"
        return [f"{label} for request {b.get('tool_use_id')}:", str(b.get("content", ""))]
    if kind == "tool_use":
        args = b.get("input") or {}
        lines = [f"Tool request {b.get('id')}: {b.get('name')}"]
        if b.get("name") == "run_query":
            lines += [f"Purpose: {args.get('purpose')}", "SQL:", str(args.get("sql"))]
        else:
            lines.append(json.dumps(args, indent=1, ensure_ascii=False, default=str))
        return lines
    if kind == "thinking":
        return ["[The AI's internal reasoning step; its text is not provided by the service]"]
    return [json.dumps(b, ensure_ascii=False, default=str)]


def service_names(start: dict) -> tuple[str, str]:
    """The AI service a run used, and whose account terms apply. Runs before V.1.1 always used Claude."""
    if start.get("provider") == "demo":
        return "None (recorded demo with fictional data; nothing was sent to any AI service)", "AI service"
    if start.get("provider") == "openai":
        if start.get("stored") is False:
            return "OpenAI (requests sent with storage turned off)", "OpenAI"
        return "OpenAI", "OpenAI"
    return "Anthropic Claude", "Anthropic"


def render_text(events: list[dict], protected_hits: list[int], protected_checked: int, run_id: str = "") -> str:
    """The log as a readable document."""
    start = next((e for e in events if e["kind"] == "run_start"), {})
    requests = sum(e["kind"] == "request" for e in events)
    uses = [b for e in events if e["kind"] == "response" for b in e.get("content") or [] if b.get("type") == "tool_use"]
    queries = sum(b.get("name") == "run_query" for b in uses)
    results = [b for e in events if e["kind"] == "message" for b in e.get("content") or [] if b.get("type") == "tool_result"]
    refused = sum(bool(b.get("is_error")) for b in results)
    docs = [b for e in events if e["kind"] == "message" for b in e.get("content") or [] if b.get("type") in ("document", "image")]
    period = start.get("period") or ["", ""]
    period = next((e["period"] for e in reversed(events) if e["kind"] == "period_changed"), period)
    service, terms = service_names(start)
    out = [
        "GRANT REPORTING ASSISTANT: AI ACTIVITY LOG", "",
        f"Report run: {run_id}", f"Reporting period: {period[0]} to {period[1]}",
        f"AI service: {service}, requested model {start.get('model') or 'not recorded'}", f"Log created: {start.get('at', '')}", "",
        "SUMMARY", f"Requests to the AI: {requests}", f"Queries the AI asked to run on this computer: {queries}",
        f"Results and tool replies sent back: {len(results)}, including {refused} refused or failed", "",
        "Uploaded files sent in full:",
    ]
    out += [f"  {d.get('title') or 'uploaded file'} ({d.get('bytes')} bytes, SHA-256 {d.get('sha256')})" for d in docs] or ["  None as a file. Any report text sent appears in the record below."]
    out += ["", "PROTECTED-VALUE CHECK"]
    if protected_hits:
        out.append(f"REVIEW NEEDED: stored identifier or note values appear in sent items {', '.join(map(str, protected_hits))}.")
    else:
        out.append("No stored identifier or note value was found in the text sent.")
    out += [f"Values checked: {protected_checked}.",
            "Covers text and results this app sent. Before sending, the text of uploaded reports was also checked for names and IDs from your data; images and scanned PDFs cannot be checked, so review them yourself.",
            "Values under 4 characters or made only of digits, and birth dates, are not checked because they match ordinary numbers and dates.",
            "", "LIMITS OF THIS LOG",
            f"It records what this app sent and received. It cannot show how the AI service stores or handles data; that depends on your {terms} account terms.",
            "The API key and ordinary connection details also go to the service but are not recorded here.",
            "Each request resends the earlier messages in the conversation; each item appears once below.",
            "", "FULL RECORD", ""]
    for e in events:
        head = f"--- Item {e['seq']} | {e['at']} | "
        if e["kind"] == "run_start":
            continue
        if e["kind"] == "request":
            out += [head + f"Request {e.get('number')} sent to the AI", ""]
        elif e["kind"] == "instructions":
            out += [head + "SENT: application instructions ---", e.get("text", ""), ""]
        elif e["kind"] == "tools":
            out += [head + "SENT: tool definitions ---", json.dumps(e.get("tools"), indent=1, ensure_ascii=False), ""]
        elif e["kind"] == "message":
            out.append(head + "SENT ---")
            for b in e.get("content") or []:
                out += _block_lines(b)
            out.append("")
        elif e["kind"] == "period_changed":
            prev = e.get("previous") or ["", ""]
            out += [head + f"LOCAL: reporting period changed by a correction from {prev[0]} to {prev[1]} "
                           f"to {e['period'][0]} to {e['period'][1]} ---", ""]
        elif e["kind"] == "note":
            out += [head + f"LOCAL: the app recorded a {e.get('kind_of_note') or 'warning'} note: {e.get('text', '')} ---", ""]
        elif e["kind"] == "response":
            out.append(head + f"RECEIVED from {e.get('model') or 'the AI'} ---")
            for b in e.get("content") or []:
                out += _block_lines(b)
            out.append("")
    return "\n".join(out)
