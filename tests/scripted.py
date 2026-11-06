"""A scripted stand-in for the AI, so the full loop can be tested without a network call.

Each step is a list of tool calls ``(name, input)`` or a callable that receives
the latest tool results and returns such a list. An empty list ends the turn
with plain text.
"""

import json
from types import SimpleNamespace


class ScriptedModel:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = 0
        self.seen_messages = []

    def respond(self, system, messages, tools, should_cancel):
        self.seen_messages.append(list(messages))
        step = self.steps[self.calls] if self.calls < len(self.steps) else []
        self.calls += 1
        if callable(step):
            step = step(last_results(messages))
        if not step:
            return SimpleNamespace(content=[{"type": "text", "text": "Done."}], stop_reason="end_turn")
        content = [{"type": "tool_use", "id": f"tu{self.calls}_{i}", "name": n, "input": with_inventory(n, inp)}
                   for i, (n, inp) in enumerate(step)]
        return SimpleNamespace(content=content, stop_reason="tool_use")


def last_results(messages):
    """Tool results from the latest user message, parsed where they are JSON."""
    out = []
    last = messages[-1]
    if last["role"] != "user" or not isinstance(last["content"], list):
        return out
    for b in last["content"]:
        if b.get("type") == "tool_result":
            try:
                out.append(json.loads(b["content"]))
            except (TypeError, ValueError):
                out.append(b["content"])
    return out


def with_inventory(name, inp):
    """Scripts written before report_tables existed list the tables they submit."""
    if name != "submit_tables" or not isinstance(inp, dict) or "report_tables" in inp:
        return inp
    tables = inp.get("tables") if isinstance(inp.get("tables"), list) else []
    return {**inp, "report_tables": [{"id": str(t.get("id")), "title": str(t.get("title", ""))}
                                     for t in tables if isinstance(t, dict) and t.get("id")]}
