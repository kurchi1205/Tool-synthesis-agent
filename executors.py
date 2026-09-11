import os
import requests
from slack_sdk import WebClient
from dotenv import load_dotenv
load_dotenv()

NOTION_TOKEN    = os.getenv("NOTION_TOKEN")
NOTION_DB_ID    = os.getenv("NOTION_DB_ID")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL   = os.getenv("SLACK_CHANNEL")

_slack_client = WebClient(token=SLACK_BOT_TOKEN)


def check_calendar(person: str) -> str:
    # TODO: replace with real Google Calendar API call
    return f"Next meeting with {person}: Thu 4pm (stub)"


def check_notion(person: str) -> str:
    """Real Notion query — filter rows mentioning person."""
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

    pages = resp.json().get("results", [])
    for page in pages:
        for prop in page["properties"].values():
            if prop["type"] == "title":
                title = prop["title"][0]["plain_text"] if prop["title"] else ""
                if person.lower() in title.lower():
                    return f"Open item for {person}: {title}"

    return f"No open items for {person}"


def post_slack(text: str, channel: str = None) -> str:
    """Real Slack message post."""
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


# Map source name → executor function
EXECUTOR_MAP = {
    "calendar": lambda slot: check_calendar(slot),
    "notion":   lambda slot: check_notion(slot),
    "slack":    lambda slot: post_slack(
        draft_message(slot, check_calendar(slot), check_notion(slot))
    ),
}
