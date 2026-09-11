import time
import requests
from datetime import datetime, timezone
from storage import (
    get_users, get_state, update_state,
    append_event, read_json
)
import os
from dotenv import load_dotenv
load_dotenv()

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DB_ID = os.getenv("NOTION_DB_ID")


def poll_calendar(user_id: str, config: dict, last_polled: str) -> list[dict]:
    """
    MVP: read from mock_events/calendar_{user_id}.json
    TODO: replace with real Google Calendar API call using last_polled as updatedMin
    """
    mock_path = f"mock_events/calendar_{user_id}.json"
    try:
        raw = read_json(mock_path)
        return [
            {
                "user_id": user_id,
                "source": "calendar",
                "time": item["start"]["dateTime"],
                "text": item["summary"],
            }
            for item in raw
            if item["start"]["dateTime"] > last_polled
        ]
    except FileNotFoundError:
        return []


def poll_notion(user_id: str, config: dict, last_polled: str) -> list[dict]:
    """Real Notion API call."""
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    body = {
        "filter": {
            "property": "Assignee",
            "rich_text": {"contains": config["notion_filter"]}
        },
        "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}]
    }
    resp = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
        headers=headers,
        json=body,
        timeout=10,
    )
    if resp.status_code != 200:
        return []

    events = []
    for page in resp.json().get("results", []):
        edited = page.get("last_edited_time", "")
        if edited <= last_polled:
            continue
        title = ""
        for prop in page["properties"].values():
            if prop["type"] == "title" and prop["title"]:
                title = prop["title"][0]["plain_text"]
                break
        events.append({
            "user_id": user_id,
            "source": "notion",
            "time": edited,
            "text": title,
        })
    return events


def poll_slack(user_id: str, config: dict, last_polled: str) -> list[dict]:
    """Real Slack conversations.history call."""
    from slack_sdk import WebClient
    client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
    channel = os.getenv("SLACK_CHANNEL")
    resp = client.conversations_history(channel=channel, oldest=last_polled, limit=50)
    events = []
    for msg in resp["messages"]:
        if msg.get("user") == config["slack_id"]:
            events.append({
                "user_id": user_id,
                "source": "slack",
                "time": str(msg["ts"]),
                "text": msg.get("text", ""),
            })
    return events


def poll_user(user_id: str) -> None:
    users = get_users()
    state = get_state()
    config = users[user_id]
    user_state = state.get(user_id, {})

    sources = {
        "calendar": (poll_calendar, "calendar_last_polled"),
        "notion":   (poll_notion,   "notion_last_polled"),
        "slack":    (poll_slack,    "slack_last_polled"),
    }

    for source, (fn, state_key) in sources.items():
        last = user_state.get(state_key, "0")
        try:
            new_events = fn(user_id, config, last)
            for event in new_events:
                append_event(event)
            if new_events:
                latest = max(e["time"] for e in new_events)
                state.setdefault(user_id, {})[state_key] = latest
        except Exception as e:
            print(f"[poller] {user_id}/{source} error: {e}")

    update_state(state)
    print(f"[poller] polled {user_id}")


def poll_now(user_id: str = None) -> None:
    """Trigger an immediate poll. Call this during the demo."""
    users = get_users()
    targets = [user_id] if user_id else list(users.keys())
    for uid in targets:
        poll_user(uid)


def polling_loop(interval_seconds: int = 15) -> None:
    while True:
        poll_now()
        time.sleep(interval_seconds)


if __name__ == "__main__":
    poll_now()
    print("Done. Check event_log.json")
