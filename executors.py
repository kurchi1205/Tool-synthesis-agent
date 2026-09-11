import os
import requests
from datetime import datetime, timedelta, timezone
from slack_sdk import WebClient
from dotenv import load_dotenv
load_dotenv()

NOTION_TOKEN    = os.getenv("NOTION_TOKEN")
NOTION_DB_ID    = os.getenv("NOTION_DB_ID")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL   = os.getenv("SLACK_CHANNEL")

_slack_client = WebClient(token=SLACK_BOT_TOKEN)


# ── Read helpers (used by draft_message) ─────────────────────────────────────

def check_calendar(person: str, user_id: str = None) -> str:
    """Returns the next calendar event involving the given person."""
    from poller import _get_calendar_service
    service = _get_calendar_service(user_id) if user_id else None
    if not service:
        return f"Calendar not connected"

    now = datetime.now(timezone.utc).isoformat()
    try:
        result = service.events().list(
            calendarId="primary",
            timeMin=now,
            q=person,
            singleEvents=True,
            orderBy="startTime",
            maxResults=1,
        ).execute()
        items = result.get("items", [])
        if items:
            ev = items[0]
            start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
            return f"Next event with {person}: {ev.get('summary', '(no title)')} at {start}"
        return f"No upcoming events with {person}"
    except Exception as e:
        return f"Calendar error: {e}"


def check_notion(person: str) -> str:
    """Returns open Notion tasks mentioning the given person."""
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
        headers=headers,
        json={},
        timeout=10,
    )
    if resp.status_code != 200:
        return f"Notion error: {resp.status_code}"

    for page in resp.json().get("results", []):
        for prop in page["properties"].values():
            if prop["type"] == "title":
                title = prop["title"][0]["plain_text"] if prop["title"] else ""
                if person.lower() in title.lower():
                    return f"Open item for {person}: {title}"

    return f"No open items for {person}"


# ── Write helpers ─────────────────────────────────────────────────────────────

def create_calendar_event(
    person: str,
    user_id: str = None,
    title: str = None,
    duration_minutes: int = 30,
) -> str:
    """
    Creates a Google Calendar event on the user's primary calendar.
    Defaults to tomorrow at 10 AM for 30 minutes.
    """
    from poller import _get_calendar_service
    service = _get_calendar_service(user_id) if user_id else None
    if not service:
        return f"Calendar not connected — skipping event creation"

    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    end = start + timedelta(minutes=duration_minutes)
    event_title = title or f"Meeting with {person}"

    body = {
        "summary": event_title,
        "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
        "end":   {"dateTime": end.isoformat(),   "timeZone": "UTC"},
    }

    try:
        created = service.events().insert(calendarId="primary", body=body).execute()
        link = created.get("htmlLink", "")
        when = start.strftime("%a %b %d at %I%p UTC")
        return f"Created '{event_title}' on {when} — {link}"
    except Exception as e:
        return f"Calendar error: {e}"


def add_notion_page(title: str, notes: str = None) -> str:
    """
    Creates a new page in the configured Notion database.
    Optionally adds a notes paragraph as the page body.
    """
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    body = {
        "parent": {"database_id": NOTION_DB_ID},
        "properties": {
            "Name": {"title": [{"text": {"content": title}}]}
        },
    }
    if notes:
        body["children"] = [{
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": notes}}]
            },
        }]

    resp = requests.post(
        "https://api.notion.com/v1/pages",
        headers=headers,
        json=body,
        timeout=10,
    )
    if resp.status_code == 200:
        return f"Added Notion page: '{title}'"
    return f"Notion error {resp.status_code}: {resp.text[:120]}"


def post_slack(text: str, channel: str = None) -> str:
    """Posts a message to the default Slack channel."""
    target = channel or SLACK_CHANNEL
    resp = _slack_client.chat_postMessage(channel=target, text=text)
    if resp["ok"]:
        return "Message sent"
    return f"Slack error: {resp.get('error')}"


def draft_message(person: str, calendar_result: str, notion_result: str) -> str:
    return (
        f"Hi {person}, just checking in ahead of our meeting. "
        f"{calendar_result}. "
        f"On the task front: {notion_result}. Let me know if anything needs attention!"
    )


# ── Executor maps ─────────────────────────────────────────────────────────────

def get_executor_map(user_id: str) -> dict:
    """
    Returns an executor map with user_id bound so calendar calls use
    the correct OAuth token. Always prefer this over EXECUTOR_MAP.
    """
    return {
        "calendar": lambda slot: create_calendar_event(slot, user_id=user_id),
        "notion":   lambda slot: add_notion_page(slot),
        "slack":    lambda slot: post_slack(
            draft_message(slot, check_calendar(slot, user_id=user_id), check_notion(slot))
        ),
    }


# Fallback map used when user_id is not available (e.g. tests)
EXECUTOR_MAP = {
    "calendar": lambda slot: create_calendar_event(slot),
    "notion":   lambda slot: add_notion_page(slot),
    "slack":    lambda slot: post_slack(
        draft_message(slot, check_calendar(slot), check_notion(slot))
    ),
}
