"""
LangGraph node functions for the Tool-Synthesis agent.

Each node receives the current AgentState and returns a dict of updated keys.

Nodes:
    poll_node     — polls Calendar / Notion / Slack for new events
    cluster_node  — groups events into time-based clusters (pure, no LLM)
    llm_node      — calls Ollama per cluster to label routines
    pattern_node  — finds sequences repeated 2+ times (pure, no LLM)
    propose_node  — generates tool definition via Ollama, DMs the user
    human_node    — LangGraph interrupt: pauses until Slack reply arrives
    confirm_node  — saves (with optional inline edits) or discards the tool
    executor_node — calls Ollama to write a custom execute() function for the
                    confirmed tool; saves executor_code to tools_store.json
"""

import json
import re
from collections import Counter
from typing import TypedDict, Optional

import ollama
from langgraph.types import interrupt
from detect import _extract_json

from poller import poll_user as _do_poll
from detect import cluster_by_time, interpret_cluster
from storage import get_events, get_users, append_tool, tool_already_saved, read_json, write_json


# ─────────────────────────────────────────────────────────────────────────────
# State schema
# ─────────────────────────────────────────────────────────────────────────────

class AgentState(TypedDict, total=False):
    """
    TypedDict so LangGraph merges partial node returns instead of replacing
    the whole state dict (which would drop user_id and other fields).

    Fields used by the detect-propose graph:
        user_id              str
        events               list[dict]
        clusters             list[list[dict]]
        interpreted_clusters list[dict]
        pattern              dict | None
        tool_definition      dict | None
        dm_channel           str | None
        user_response        str | None

    Fields used by the execute graph:
        slot_value           str   — the value the user provided (e.g. "Alice")
        channel              str   — Slack channel ID to post the checklist into
        execution_result     str | None — one-line summary returned by execute()
    """
    user_id: str
    events: list
    clusters: list
    interpreted_clusters: list
    pattern: Optional[dict]
    tool_definition: Optional[dict]
    dm_channel: Optional[str]
    user_response: Optional[str]
    slot_value: str
    channel: str
    execution_result: Optional[str]
    needs_reconfirmation: Optional[bool]


# ─────────────────────────────────────────────────────────────────────────────
# Node 1 — poll_node
# ─────────────────────────────────────────────────────────────────────────────

def poll_node(state: AgentState) -> dict:
    """
    Calls the real Google Calendar / Notion / Slack pollers for this user,
    then loads all their events from event_log.json.
    """
    user_id = state["user_id"]
    print(f"[node:poll] polling {user_id}")
    try:
        _do_poll(user_id)
    except Exception as e:
        print(f"[node:poll] error: {e}")
    events = get_events(user_id)
    print(f"[node:poll] loaded {len(events)} events for {user_id}")
    return {"events": events}


# ─────────────────────────────────────────────────────────────────────────────
# Node 2 — cluster_node
# ─────────────────────────────────────────────────────────────────────────────

def cluster_node(state: AgentState) -> dict:
    """
    Pure function — no API calls.
    Groups consecutive events within a 10-minute sliding window.
    """
    events = state.get("events") or []
    clusters = cluster_by_time(events, window_minutes=10)
    print(f"[node:cluster] {len(events)} events → {len(clusters)} clusters")
    return {"clusters": clusters}


# ─────────────────────────────────────────────────────────────────────────────
# Node 3 — llm_node
# ─────────────────────────────────────────────────────────────────────────────

def llm_node(state: AgentState) -> dict:
    """
    Calls Ollama (llama3.1:8b) on each cluster to decide if it is a routine
    and, if so, extracts the sequence / slot / description.
    """
    clusters = state.get("clusters") or []
    interpreted = []
    for cluster in clusters:
        result = interpret_cluster(cluster)
        if result:
            interpreted.append(result)
    print(f"[node:llm] {len(clusters)} clusters → {len(interpreted)} routines identified")
    return {"interpreted_clusters": interpreted}


# ─────────────────────────────────────────────────────────────────────────────
# Node 4 — pattern_node
# ─────────────────────────────────────────────────────────────────────────────

def pattern_node(state: AgentState) -> dict:
    """
    Pure matching — no API calls.
    Finds sequences that appear in 2+ clusters and aren't already saved as tools.
    """
    user_id = state["user_id"]
    interpreted = state.get("interpreted_clusters") or []

    counts = Counter(tuple(r["sequence"]) for r in interpreted)
    for seq_tuple, count in counts.items():
        if count >= 2 and not tool_already_saved(user_id, list(seq_tuple)):
            for r in reversed(interpreted):
                if tuple(r["sequence"]) == seq_tuple:
                    print(f"[node:pattern] found repeating pattern: {list(seq_tuple)}")
                    return {"pattern": dict(r)}

    print("[node:pattern] no new repeating pattern")
    return {"pattern": None}


# ─────────────────────────────────────────────────────────────────────────────
# Node 5 — propose_node
# ─────────────────────────────────────────────────────────────────────────────

def propose_node(state: AgentState) -> dict:
    """
    Calls Ollama to produce a rich tool definition (name, description, steps,
    what_you_provide, example), then DMs the Slack user with a full explanation
    and a yes / no prompt.
    """
    user_id = state["user_id"]
    pattern = dict(state["pattern"])

    # Build LLM prompt
    summary = (
        f"Sequence: {' → '.join(pattern['sequence'])}. "
        f"Slot (what varies): {pattern.get('slot', '?')}. "
        f"Example value: {pattern.get('slot_value', '?')}. "
        f"Description: {pattern.get('description', '')}."
    )
    prompt = f"""A user has been detected repeating this workflow pattern:
{summary}

Generate a clear tool definition for this workflow.

Reply with ONLY valid JSON:
{{
  "tool_name": "snake_case name, max 4 words",
  "description": "one clear sentence: what this tool does and when to use it",
  "steps": ["step 1 description", "step 2 description", ...],
  "what_you_provide": "explain what input the user must give when running this tool",
  "example": "example invocation e.g. /tool weekly_sync for Alice"
}}"""

    try:
        print("[node:propose] calling Ollama for tool definition ...")
        resp = ollama.chat(model="llama3.1:8b", messages=[{"role": "user", "content": prompt}])
        raw = resp["message"]["content"].strip()
        definition = _extract_json(raw)
        print(f"[node:propose] definition: {definition.get('tool_name')}")
    except Exception as e:
        print(f"[node:propose] LLM failed: {e} — using fallback definition")
        words = pattern.get("description", "routine").lower().split()[:3]
        definition = {
            "tool_name": "_".join(w.strip(".,") for w in words),
            "description": pattern.get("description", "Detected workflow"),
            "steps": [f"Run {s}" for s in pattern["sequence"]],
            "what_you_provide": f"the {pattern.get('slot', 'value')}",
            "example": f"/tool {pattern.get('tool_name', 'tool')} for [name]",
        }

    pattern.update(definition)

    # Build DM text
    steps_str = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(definition["steps"]))
    msg = (
        f":mag: *I noticed a repeated workflow pattern!*\n\n"
        f"You've done this *{len(pattern['sequence'])}-step sequence* at least twice:\n"
        f"`{' → '.join(pattern['sequence'])}`\n\n"
        f"*Proposed tool:* `{definition['tool_name']}`\n"
        f"_{definition['description']}_\n\n"
        f"*What it does:*\n{steps_str}\n\n"
        f"*What you need to provide:*\n  {definition['what_you_provide']}\n\n"
        f"*Example usage:*\n  `{definition['example']}`\n\n"
        f"───────────────────────\n"
        f"Reply *yes* to save this tool as-is.\n"
        f"Or suggest changes inline, e.g: *yes, call it `prep_meeting` and it's for a topic not a person*\n"
        f"Reply *no* to discard."
    )

    # Lazy import to avoid circular dependency (slack_bot imports agent_graph)
    from slack_bot import app as _slack_app
    users = get_users()
    slack_id = users[user_id]["slack_id"]
    dm = _slack_app.client.conversations_open(users=slack_id)
    dm_channel = dm["channel"]["id"]
    _slack_app.client.chat_postMessage(channel=dm_channel, text=msg)

    print(f"[node:propose] DM sent to {user_id} ({slack_id}) via {dm_channel}")
    return {"pattern": pattern, "tool_definition": definition, "dm_channel": dm_channel}


# ─────────────────────────────────────────────────────────────────────────────
# Node 6 — human_node  (LangGraph interrupt)
# ─────────────────────────────────────────────────────────────────────────────

def human_node(state: AgentState) -> dict:
    """
    Pauses the graph at this node until the Slack bot resumes it.
    Resume by calling:
        agent_graph.invoke(Command(resume="yes …"), config=thread_config)
    """
    user_response = interrupt("waiting_for_slack_reply")
    return {"user_response": user_response}


# ─────────────────────────────────────────────────────────────────────────────
# Node 7 — confirm_node
# ─────────────────────────────────────────────────────────────────────────────

def confirm_node(state: AgentState) -> dict:
    """
    Three outcomes based on user_response:
      "yes" (no edits)     → save immediately, confirmed=True
      "yes, call it X …"  → apply edits, set needs_reconfirmation=True,
                             route to re_propose_node so the user sees
                             the changes before the tool is saved
      "no"                 → discard
    """
    user_id = state["user_id"]
    pattern = dict(state.get("pattern") or {})
    user_text = (state.get("user_response") or "").strip()

    if "yes" not in user_text.lower():
        print(f"[node:confirm] discarded for {user_id}")
        return {"pattern": None, "needs_reconfirmation": False}

    # Detect inline edits
    has_edits = False

    rename = re.search(r"(?:call it|rename to|name it)\s+[`']?(\w+)[`']?", user_text, re.I)
    if rename:
        pattern["tool_name"] = rename.group(1).lower()
        has_edits = True

    slot = re.search(r"(?:slot is|it's for a|for a)\s+(\w+)", user_text, re.I)
    if slot:
        pattern["slot"] = slot.group(1).lower()
        has_edits = True

    desc = re.search(r"description[:\s]+(.+?)(?:\.|$)", user_text, re.I)
    if desc:
        pattern["description"] = desc.group(1).strip()
        has_edits = True

    if has_edits:
        # Show the updated proposal first — don't save yet
        print(f"[node:confirm] inline edits detected for '{pattern['tool_name']}' — re-proposing")
        pattern.pop("confirmed", None)
        return {"pattern": pattern, "needs_reconfirmation": True}

    # Plain "yes" with no edits — save now
    pattern["confirmed"] = True
    pattern.pop("needs_reconfirmation", None)
    append_tool(user_id, pattern)
    print(f"[node:confirm] saved '{pattern['tool_name']}' for {user_id}")
    return {"pattern": pattern, "needs_reconfirmation": False}


# ─────────────────────────────────────────────────────────────────────────────
# Node 7b — re_propose_node
# ─────────────────────────────────────────────────────────────────────────────

def re_propose_node(state: AgentState) -> dict:
    """
    Sends a DM showing the edited tool definition and asks for a clean yes/no.
    Reached only when confirm_node detected inline edits.
    Clears needs_reconfirmation so the next pass through confirm_node saves directly.
    """
    from slack_bot import app as _slack_app

    user_id = state["user_id"]
    pattern = dict(state["pattern"])
    pattern.pop("needs_reconfirmation", None)

    users = get_users()
    slack_id = users[user_id]["slack_id"]

    msg = (
        f"✏️ *Got it — here's the updated tool:*\n\n"
        f"*Name:* `{pattern['tool_name']}`\n"
        f"*Slot:* `{pattern.get('slot', '?')}`\n"
        f"*Description:* _{pattern.get('description', '')}_\n"
        f"*Sequence:* `{' → '.join(pattern['sequence'])}`\n\n"
        f"Reply *yes* to save, or *no* to discard."
    )

    dm = _slack_app.client.conversations_open(users=slack_id)
    dm_channel = dm["channel"]["id"]
    _slack_app.client.chat_postMessage(channel=dm_channel, text=msg)

    print(f"[node:re_propose] sent updated proposal for '{pattern['tool_name']}' to {user_id}")
    return {"pattern": pattern, "needs_reconfirmation": False, "user_response": None}


# ─────────────────────────────────────────────────────────────────────────────
# Node 8 — executor_node
# ─────────────────────────────────────────────────────────────────────────────

_EXECUTOR_SANDBOX_DOCS = """
Available functions (already in scope — do NOT import anything):
    check_calendar(person: str) -> str
        Returns the next upcoming Google Calendar event involving the given person.

    check_notion(person: str) -> str
        Returns open Notion tasks that mention the given person.

    create_calendar_event(person: str, title: str = None, duration_minutes: int = 30) -> str
        Creates a Google Calendar event for tomorrow at 10 AM UTC.
        title defaults to "Meeting with <person>".
        Returns a confirmation string with the event link.

    add_notion_page(title: str, notes: str = None) -> str
        Creates a new page in the Notion database with the given title.
        Optionally adds notes as the page body paragraph.
        Returns "Added Notion page: '<title>'" or an error string.

    post_slack(text: str) -> str
        Posts a message to the default Slack channel. Returns "Message sent" or an error.

    draft_message(person: str, calendar_result: str, notion_result: str) -> str
        Builds a ready-to-send Slack message combining calendar + Notion results.
"""


def executor_node(state: AgentState) -> dict:
    """
    Runs only when a tool was confirmed (pattern["confirmed"] == True).

    Calls Ollama to generate a custom Python function:
        def execute(slot_value: str) -> str: ...

    The function body uses the four available executor helpers and is
    tailored to the tool's specific steps and description.

    The generated code is:
      1. Validated with compile() — discarded if it has syntax errors.
      2. Patched into the tool entry in tools_store.json as "executor_code".

    At run-time, execute_node execs this code in a sandbox that
    exposes only the four helper functions.
    """
    pattern = state.get("pattern") or {}
    if not pattern.get("confirmed"):
        return {}  # nothing to do — tool was discarded

    user_id = state["user_id"]
    tool_name = pattern.get("tool_name", "unknown")

    steps_str = "\n".join(
        f"  {i+1}. {s}" for i, s in enumerate(pattern.get("steps", []))
    )
    prompt = f"""You are writing a Python executor function for a workflow automation tool.

Tool name: {tool_name}
Description: {pattern.get('description', '')}
Slot (the input the user provides, e.g. a person's name): {pattern.get('slot', 'value')}
Steps to perform:
{steps_str}

{_EXECUTOR_SANDBOX_DOCS}

Write ONLY this Python function — no imports, no explanation, no markdown:

def execute(slot_value: str) -> str:
    # implement the steps above using the available functions
    ...
    return "<one-line summary of what was done>"

Rules:
- Use slot_value wherever the slot appears (e.g. as the person's name).
- Call the helper functions in the order matching the steps.
- If a step involves scheduling or creating a meeting, use create_calendar_event(slot_value).
- If a step involves adding a task or note, use add_notion_page(slot_value).
- If a step involves sending a message, use post_slack(draft_message(slot_value, ...)) or post_slack(<text>).
- Return a concise one-line summary string.
- Do not use any other imports or globals.
"""

    try:
        print(f"[node:executor] calling Ollama to generate executor for '{tool_name}' ...")
        resp = ollama.chat(model="llama3.1:8b", messages=[{"role": "user", "content": prompt}])
        code = resp["message"]["content"].strip()

        # Strip any markdown code fences (``` or ```python)
        code = re.sub(r"^```(?:python)?", "", code, flags=re.MULTILINE).strip()
        code = re.sub(r"```$", "", code, flags=re.MULTILINE).strip()

        # Validate syntax before saving
        compile(code, f"<executor:{tool_name}>", "exec")
        print(f"[node:executor] generated executor for '{tool_name}':\n{code[:120]}...")

        # Patch executor_code into the saved tool entry
        store = read_json("tools_store.json")
        for tool in store.get(user_id, []):
            if tool.get("tool_name") == tool_name:
                tool["executor_code"] = code
                break
        write_json("tools_store.json", store)

        pattern = dict(pattern)
        pattern["executor_code"] = code

    except SyntaxError as e:
        print(f"[node:executor] generated code has syntax error: {e} — skipping")
    except Exception as e:
        print(f"[node:executor] failed: {e} — tool will fall back to EXECUTOR_MAP")

    return {"pattern": pattern}


# ─────────────────────────────────────────────────────────────────────────────
# Node 9 — execute_node  (entry point of the separate execute_graph)
# ─────────────────────────────────────────────────────────────────────────────

def execute_node(state: AgentState) -> dict:
    """
    Runs the tool stored in state["pattern"] for state["slot_value"].
    Posts a live-updating Slack checklist to state["channel"].

    Two paths:
      1. Custom executor  — tool has "executor_code" generated by executor_node.
                           Runs it in a sandbox with the four helper functions.
      2. Default checklist — iterates over tool["sequence"] and calls EXECUTOR_MAP.
    """
    from slack_bot import app as _slack_app
    import time as _time

    tool       = state["pattern"]
    slot_value = state["slot_value"]
    channel    = state["channel"]
    client     = _slack_app.client
    tool_name  = tool["tool_name"]

    from executors import (
        check_calendar, check_notion, post_slack, draft_message,
        create_calendar_event, add_notion_page, get_executor_map,
    )

    user_id    = state["user_id"]
    executor_map = get_executor_map(user_id)

    # ── Path 1: custom executor_code ─────────────────────────────────────────
    if tool.get("executor_code"):
        resp = client.chat_postMessage(
            channel=channel,
            text=f"\\ Running *{tool_name}* for *{slot_value}*..."
        )
        msg_ts = resp["ts"]
        try:
            sandbox = {
                "check_calendar":       lambda person: check_calendar(person, user_id=user_id),
                "check_notion":         check_notion,
                "create_calendar_event": lambda person, **kw: create_calendar_event(person, user_id=user_id, **kw),
                "add_notion_page":      add_notion_page,
                "post_slack":           post_slack,
                "draft_message":        draft_message,
            }
            exec(tool["executor_code"], sandbox)
            summary = sandbox["execute"](slot_value)
            client.chat_update(
                channel=channel,
                ts=msg_ts,
                text=f"✅ *{tool_name}* complete for *{slot_value}*.\n{summary}"
            )
            return {"execution_result": summary}
        except Exception as e:
            error_msg = f"❌ *{tool_name}* failed: {e}"
            client.chat_update(channel=channel, ts=msg_ts, text=error_msg)
            return {"execution_result": error_msg}

    # ── Path 2: default checklist (get_executor_map) ──────────────────────────
    steps = tool["sequence"]

    def render(results: dict, running: str = None) -> str:
        lines = [f"Running *{tool_name}* for *{slot_value}*..."]
        for step in steps:
            if step in results:
                lines.append(f"✅ {step} — {results[step]}")
            elif step == running:
                lines.append(f"\\ {step}")
            else:
                lines.append(f"☐ {step}")
        return "\n".join(lines)

    resp = client.chat_postMessage(channel=channel, text=render({}))
    msg_ts = resp["ts"]
    results = {}

    for step in steps:
        client.chat_update(channel=channel, ts=msg_ts, text=render(results, running=step))
        executor = executor_map.get(step)
        try:
            result = executor(slot_value) if executor else f"no executor for {step}"
        except Exception as e:
            result = f"error: {e}"
        results[step] = result
        client.chat_update(channel=channel, ts=msg_ts, text=render(results))
        _time.sleep(0.5)

    final = render(results) + f"\n\n✅ *{tool_name}* complete."
    client.chat_update(channel=channel, ts=msg_ts, text=final)
    return {"execution_result": final}
