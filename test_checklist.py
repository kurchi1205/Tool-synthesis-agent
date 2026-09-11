# test_checklist.py — run once manually to verify the live checklist works
# Usage: python test_checklist.py
from slack_sdk import WebClient
import os
from dotenv import load_dotenv
load_dotenv()

from executors import EXECUTOR_MAP
from slack_bot import run_tool

client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
channel = os.getenv("SLACK_CHANNEL")

fake_tool = {
    "tool_name": "test_checkin",
    "sequence": ["calendar", "notion", "slack"],
    "slot": "person",
    "confirmed": True,
}

print(f"Posting test checklist to channel {channel} ...")
run_tool(fake_tool, "Alice", channel, client)
print("Done — check your Slack channel.")
