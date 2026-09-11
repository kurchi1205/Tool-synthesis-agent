import os
import re
import time
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv
load_dotenv()

from executors import EXECUTOR_MAP, draft_message, check_calendar, check_notion
from storage import (
    get_users, get_tools, append_tool, write_json, read_json,
    find_matching_tool, slack_id_to_user_id, tool_already_saved,
)
from calendar_auth import (
    is_calendar_connected, generate_auth_url, start_callback_server,
)

app = App(token=os.getenv("SLACK_BOT_TOKEN"))

# In-memory pending proposals — { slack_user_id: pattern_dict }
pending_proposals: dict[str, dict] = {}


# ── PART 1: Live-updating checklist ──────────────────────────────────────────

def run_tool(tool: dict, slot_value: str, channel: str, client) -> None:
    """
    Posts a checklist message and updates it step-by-step as each executor runs.
    """
    steps = tool["sequence"]

    def render_checklist(results: dict, running: str = None) -> str:
        lines = [f"Running *{tool['tool_name']}* for *{slot_value}*..."]
        for step in steps:
            if step in results:
                lines.append(f"✅ {step} — {results[step]}")
            elif step == running:
                lines.append(f"\\ {step}")   # backslash = in progress
            else:
                lines.append(f"☐ {step}")
        return "\n".join(lines)

    # Post initial message — all boxes unchecked
    resp = client.chat_postMessage(
        channel=channel,
        text=render_checklist({})
    )
    msg_ts = resp["ts"]

    results = {}
    for step in steps:
        # Show backslash on the currently running step
        client.chat_update(
            channel=channel,
            ts=msg_ts,
            text=render_checklist(results, running=step)
        )

        executor = EXECUTOR_MAP.get(step)
        try:
            result = executor(slot_value) if executor else f"no executor for {step}"
        except Exception as e:
            result = f"error: {e}"

        results[step] = result

        # Tick the completed step to done
        client.chat_update(
            channel=channel,
            ts=msg_ts,
            text=render_checklist(results)
        )
        time.sleep(0.5)  # visible delay so each step ticks off on screen

    # Final completion update
    client.chat_update(
        channel=channel,
        ts=msg_ts,
        text=render_checklist(results) + f"\n\n✅ *{tool['tool_name']}* complete."
    )


# ── PART 2: Proposal + confirmation ──────────────────────────────────────────

def propose_tool(user_id: str, pattern: dict) -> None:
    """Called by detect_loop when a repeated pattern is found for a user."""
    users = get_users()
    slack_id = users[user_id]["slack_id"]

    # Auto-generate tool name from description
    words = pattern.get("description", "routine").lower().split()[:3]
    tool_name = "_".join(w.strip(".,") for w in words)
    pattern["tool_name"] = tool_name

    sequence_str = " → ".join(pattern["sequence"])
    msg = (
        f"I noticed you've done this {len(pattern['sequence'])}-step sequence at least twice:\n"
        f"*{sequence_str}*\n"
        f"Each time for a different *{pattern['slot']}*.\n\n"
        f"Want me to save this as a reusable tool called `{tool_name}`?\n"
        f"Reply *yes* to confirm."
    )

    app.client.chat_postMessage(channel=slack_id, text=msg)
    pending_proposals[slack_id] = pattern
    print(f"[slack_bot] proposed tool '{tool_name}' to {user_id} ({slack_id})")


def confirm_tool(slack_id: str, user_id: str, say) -> None:
    pattern = pending_proposals.pop(slack_id)
    pattern["confirmed"] = True
    append_tool(user_id, pattern)
    print(f"[slack_bot] confirmed tool '{pattern['tool_name']}' for {user_id}")
    say(
        f"✅ Saved `{pattern['tool_name']}`.\n"
        f"Run it anytime with: `/tool {pattern['tool_name']} for [name]`"
    )


# ── PART 4: Slash command — /tool ─────────────────────────────────────────────

@app.command("/tool")
def handle_slash_tool(ack, body, say, client):
    """
    /tool                          → list this user's saved tools
    /tool <tool_name> for <slot>   → run the named tool
    """
    ack()

    slack_user_id = body["user_id"]
    channel       = body["channel_id"]
    text          = body.get("text", "").strip()

    user_id = slack_id_to_user_id(slack_user_id)
    if not user_id:
        say("I don't recognise your Slack user ID. Ask an admin to add you to users.json.")
        return

    tools = get_tools(user_id)

    # No args → list available tools
    if not text:
        if not tools:
            say("You have no saved tools yet. I'll propose one when I detect a repeated pattern.")
            return
        lines = ["*Your saved tools:*"]
        for t in tools:
            seq = " → ".join(t["sequence"])
            lines.append(f"• `/tool {t['tool_name']} for [name]`  —  _{t.get('description', seq)}_")
        say("\n".join(lines))
        return

    # Args → match and run
    matches = find_matching_tool(user_id, text)

    if len(matches) == 0:
        names = [f"`/tool {t['tool_name']} for [name]`" for t in tools]
        available = "\n".join(names) if names else "none yet"
        say(f"No matching tool found for `{text}`.\nAvailable:\n{available}")
    elif len(matches) > 1:
        names = [f"`{t['tool_name']}`" for t in matches]
        say(f"Multiple tools match: {', '.join(names)}. Be more specific.")
    else:
        tool       = matches[0]
        slot_value = extract_slot(text, tool["slot"])
        run_tool(tool, slot_value, channel, client)


# ── PART 3: Message routing ───────────────────────────────────────────────────

def extract_slot(text: str, slot: str) -> str:
    """Looks for 'for <name>' or 'with <name>'. Falls back to last word."""
    match = re.search(r"\b(?:for|with)\s+(\w+)", text, re.IGNORECASE)
    if match:
        return match.group(1)
    return text.split()[-1]


@app.message("")
def handle_message(message, say, client):
    slack_user_id = message.get("user")
    text = message.get("text", "").strip()
    channel = message["channel"]

    if not slack_user_id or message.get("bot_id"):
        return  # ignore bot messages

    # Manual demo trigger
    if text == "!poll now":
        from poller import poll_now
        from detect import find_repeated_pattern
        user_id = slack_id_to_user_id(slack_user_id)
        poll_now(user_id)
        if user_id:
            pattern = find_repeated_pattern(user_id)
            if pattern and not tool_already_saved(user_id, pattern["sequence"]):
                propose_tool(user_id, pattern)
                say("Polled and found a pattern — check your DMs.")
            else:
                say("Polled. No new pattern found yet.")
        return

    # Pending proposal?
    if slack_user_id in pending_proposals:
        user_id = slack_id_to_user_id(slack_user_id)
        if "yes" in text.lower():
            confirm_tool(slack_user_id, user_id, say)
        else:
            say("Reply *yes* to confirm saving the tool, or ignore to skip.")
        return

    # Match against this user's saved tools
    user_id = slack_id_to_user_id(slack_user_id)
    if not user_id:
        say("I don't recognise your Slack user ID. Ask an admin to add you to users.json.")
        return

    matches = find_matching_tool(user_id, text)

    if len(matches) == 0:
        say("No matching tool found. Type `!poll now` to check for new patterns.")
    elif len(matches) > 1:
        names = [f"`{t['tool_name']}`" for t in matches]
        say(f"Multiple tools match: {', '.join(names)}. Which one did you mean?")
    else:
        tool = matches[0]
        slot_value = extract_slot(text, tool["slot"])
        run_tool(tool, slot_value, channel, client)


# ── PART 5: New user registration + calendar setup ───────────────────────────

def _register_user_if_new(slack_user_id: str, client) -> str | None:
    """
    Checks if the Slack user is in users.json.
    If not, auto-registers them and returns the new user_id.
    Returns existing user_id if already registered.
    """
    users = get_users()

    # Already registered
    existing = slack_id_to_user_id(slack_user_id)
    if existing:
        return existing

    # Fetch their Slack display name
    try:
        profile = client.users_info(user=slack_user_id)["user"]
        label   = profile.get("real_name") or profile.get("name") or slack_user_id
    except Exception:
        label = slack_user_id

    # Assign next user_id
    next_num = len(users) + 1
    user_id  = f"u{next_num}"

    users[user_id] = {
        "slack_id":      slack_user_id,
        "notion_filter": user_id,
        "label":         label,
    }
    write_json("users.json", users)

    # Add to state.json
    import json as _json
    state = read_json("state.json")
    state[user_id] = {
        "calendar_last_polled": "2024-01-01T00:00:00Z",
        "notion_last_polled":   "2024-01-01T00:00:00Z",
        "slack_last_polled":    "0",
    }
    write_json("state.json", state)

    # Add to tools_store.json
    store = read_json("tools_store.json")
    store[user_id] = []
    write_json("tools_store.json", store)

    print(f"[slack_bot] registered new user {user_id} ({label} / {slack_user_id})")
    return user_id


def _prompt_calendar_setup(user_id: str, slack_user_id: str, client):
    """DM the user with a Google Calendar auth link."""
    try:
        auth_url = generate_auth_url(user_id, slack_user_id)
        client.chat_postMessage(
            channel=slack_user_id,
            text=(
                f"Hi! I'm your Tool-Creator bot.\n\n"
                f"To watch your calendar activity I need access to your Google Calendar.\n"
                f"*<{auth_url}|Click here to connect Google Calendar>*\n\n"
                f"After you approve, I'll start learning your workflows automatically."
            ),
        )
        print(f"[slack_bot] sent calendar setup link to {user_id} ({slack_user_id})")
    except FileNotFoundError as e:
        print(f"[slack_bot] calendar setup skipped: {e}")


def _on_calendar_connected(user_id: str, slack_user_id: str):
    """Called by calendar_auth after token is saved — DM the user to confirm."""
    app.client.chat_postMessage(
        channel=slack_user_id,
        text=(
            f"✅ Google Calendar connected! I'll start watching your calendar activity.\n"
            f"Type `/tool` anytime to see your saved tools."
        ),
    )
    print(f"[slack_bot] calendar connected for {user_id}")


@app.event("team_join")
def handle_team_join(event, client):
    """Fires when a new member joins the workspace."""
    slack_user_id = event["user"]["id"]
    print(f"[slack_bot] new member joined: {slack_user_id}")

    # Register and prompt for calendar setup
    user_id = _register_user_if_new(slack_user_id, client)
    _prompt_calendar_setup(user_id, slack_user_id, client)


@app.event("app_home_opened")
def handle_app_home_opened(event, client):
    slack_user_id = event["user"]

    # Register if new
    user_id = _register_user_if_new(slack_user_id, client)

    # Prompt calendar setup if not yet connected
    if not is_calendar_connected(user_id):
        _prompt_calendar_setup(user_id, slack_user_id, client)
        return

    # Already set up — show status in App Home
    tools = get_tools(user_id)
    tool_lines = "\n".join(
        f"• `{t['tool_name']}` — {t.get('description', '')}"
        for t in tools
    ) or "_No tools saved yet. I'll propose one when I detect a pattern._"

    client.views_publish(
        user_id=slack_user_id,
        view={
            "type": "home",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "*Your saved tools:*"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": tool_lines}},
                {"type": "divider"},
                {"type": "section", "text": {"type": "mrkdwn", "text": "_Type `/tool` in any channel to run a tool._"}},
            ],
        },
    )


def start_bot():
    # Start OAuth callback server before the Slack bot
    start_callback_server(on_token_saved=_on_calendar_connected)

    handler = SocketModeHandler(app, os.getenv("SLACK_APP_TOKEN"))
    handler.start()
