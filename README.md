# Tool Synthesis Agent

An AI agent that watches your activity across Slack, Google Calendar, and Notion, detects repeated multi-step workflows, and proposes saving them as reusable tools — all without you having to define anything upfront.

---

## How It Works

The agent runs continuously in the background. Every 15 seconds it runs a LangGraph pipeline for each registered user:

```
poll → cluster → llm → pattern
                          |
                    no pattern → END
                          |
                     pattern found
                          ↓
                       propose → human [INTERRUPT — waits for Slack reply]
                                           ↓
                                        confirm
                                           |
                                    discarded → END
                                           |
                                       confirmed
                                           ↓
                                        executor → END
```

When a user runs a saved tool, a separate single-node `execute_graph` runs it with a live Slack checklist.

### Agent Nodes

| Node | What it does |
|---|---|
| `poll` | Fetches new events from Google Calendar, Notion, and Slack |
| `cluster` | Groups events that happen within a 10-minute window |
| `llm` | Calls Ollama (`llama3.1:8b`) on each cluster to decide if it's a routine |
| `pattern` | Finds sequences that repeat 2+ times and aren't already saved |
| `propose` | Generates a full tool definition via Ollama, DMs the user in Slack |
| `human` | LangGraph interrupt — pauses the graph until the user replies |
| `confirm` | Saves the tool (with optional inline edits) or discards it |
| `executor` | Calls Ollama to generate a custom Python executor function for the tool |
| `execute` | Runs a saved tool and posts a live-updating Slack checklist |

### Background Threads

Three threads run concurrently when the app starts:

| Thread | Interval | What it does |
|---|---|---|
| `agent` | every 15s | Runs the full LangGraph pipeline for all registered users |
| `setup-checker` | every 1h | Scans the workspace, auto-registers new members, DMs anyone missing calendar auth |
| Slack bot | — | Socket Mode listener (blocking, keeps the process alive) |

---

## Prerequisites

- Python 3.11+
- [Ollama](https://ollama.com) running locally with `llama3.1:8b` pulled
- A Slack workspace where you can create apps
- A Notion integration (optional)
- A Google Calendar OAuth credential (optional)

---

## Setup

### 1. Clone and install dependencies

```bash
git clone <repo-url>
cd Tool-synthesis-agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Pull the local LLM

```bash
ollama pull llama3.1:8b
```

### 3. Create a Slack App

The easiest way is to import the manifest:

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From a manifest**
2. Select your workspace → paste the contents of `slack_manifest.json` → create

This automatically configures all scopes, slash commands, socket mode, and event subscriptions.

Alternatively, create **From scratch** and configure manually:

**Enable Socket Mode:**
- **Socket Mode** → Enable Socket Mode
- **Basic Information** → **App-Level Tokens** → Generate a token with `connections:write` scope → this is your `SLACK_APP_TOKEN` (`xapp-...`)

**Add Bot Token Scopes** (under **OAuth & Permissions → Scopes → Bot Token Scopes**):
- `channels:history`
- `chat:write`
- `commands`
- `im:history`
- `im:read`
- `im:write`
- `users:read`

**Subscribe to Bot Events** (under **Event Subscriptions**):
- `app_home_opened`
- `message.channels`
- `message.im`
- `team_join`

**Install the app** to your workspace → copy the **Bot User OAuth Token** (`xoxb-...`) → this is your `SLACK_BOT_TOKEN`

**Get the Signing Secret:**
- **Basic Information** → **App Credentials** → copy **Signing Secret** → this is your `SLACK_SIGNING_SECRET`

**Get the channel ID** for a channel the bot should monitor and post to:
- Right-click the channel in Slack → **View channel details** → copy the ID at the bottom (starts with `C`) → this is your `SLACK_CHANNEL`

### 4. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```env
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_SIGNING_SECRET=...
SLACK_CHANNEL=C0...

# Notion (optional)
NOTION_TOKEN=secret_...
NOTION_DB_ID=...
```

### 5. Google Calendar (optional)

1. Go to [Google Cloud Console](https://console.cloud.google.com) → create a project → enable the **Google Calendar API**
2. **APIs & Services → Credentials** → create an **OAuth 2.0 Client ID** (Desktop app) → download as `credentials.json` into the project root

The bot handles the rest automatically — no manual auth steps needed. When it starts, it scans the workspace and DMs any user who hasn't connected their calendar yet with an OAuth link. The user clicks it, approves in the browser, and the token is saved. A local callback server runs on `http://localhost:8080` to receive the OAuth redirect.

---

## Running

```bash
python main.py
```

On startup the bot will:
1. Start the OAuth callback server on `localhost:8080`
2. Connect to Slack via Socket Mode
3. Scan the workspace — auto-register any members not yet in `users.json`, and DM anyone missing calendar auth
4. Begin the agent loop (runs every 15 seconds)

### Manual trigger (for testing)

Send `!poll now` in any Slack channel the bot can see. It will immediately poll and detect patterns, then DM you if one is found.

### Seeding mock events (for demos)

`seed.py` contains mock events for 10 users with a variety of workflow patterns. It is currently commented out (live polling is active). To restore mock data for a demo:

1. Open `seed.py` and uncomment the `events` list and `write_json` call
2. Run `python seed.py`

---

## Interacting with the Bot

### Slash commands

| Command | What it does |
|---|---|
| `/setup` | Sends you a Google Calendar OAuth link (re-runs if already connected) |
| `/tool` | Lists your saved tools |
| `/tool weekly_sync for Alice` | Runs the `weekly_sync` tool for Alice |

### App Home

Open the bot's App Home tab in Slack to see all your saved tools at a glance.

### When a pattern is detected

The bot DMs you:

```
I noticed a repeated workflow pattern!

You've done this 3-step sequence at least twice:
`calendar → notion → slack`

Proposed tool: `weekly_sync`
Checks your calendar, open Notion tasks, and sends a check-in message.

What you need to provide: a person's name

Example: /tool weekly_sync for Alice

Reply yes to save this tool as-is.
Or suggest changes inline, e.g: yes, call it prep_meeting and it's for a topic not a person
Reply no to discard.
```

**Inline edits supported when confirming:**
- `yes, call it prep_meeting` — renames the tool
- `yes but slot is topic` — changes what the variable input represents
- `yes, description: prep for weekly standups` — updates the description

### Running a saved tool

```
weekly_sync for Alice
```

The bot posts a live Slack checklist that ticks off each step as it completes.

---

## Project Structure

```
main.py            — entry point, starts all threads
agent_graph.py     — LangGraph graph wiring and public API
nodes.py           — all node functions + AgentState schema
detect.py          — time-based clustering + Ollama routine interpretation
poller.py          — Google Calendar, Notion, and Slack pollers
executors.py       — executor helper functions (calendar, notion, slack, draft)
slack_bot.py       — Slack Bolt app, setup loop, message routing, tool runner
calendar_auth.py   — Google OAuth flow + local callback server (port 8080)
storage.py         — JSON file read/write helpers
seed.py            — mock event data for demos (currently commented out)

slack_manifest.json     — import this to configure the Slack app in one step
users.json              — registered users (auto-populated by setup_loop)
event_log.json          — append-only log of polled events
tools_store.json        — saved tools per user (includes generated executor code)
state.json              — per-user last-polled timestamps
credentials.json        — Google OAuth client credentials (you provide this)
token_{user_id}.json    — per-user Google Calendar tokens (auto-generated)
```

---

## Data Flow

```
Google Calendar ──┐
Notion           ──┼──► poller ──► event_log.json ──► cluster ──► Ollama (llm_node)
Slack history    ──┘                                                      │
                                                                          ▼
                                                                   pattern detection
                                                                          │
                                                                 Ollama (propose_node)
                                                                          │
                                                                 Slack DM to user
                                                                          │
                                                               user replies yes/no
                                                                          │
                                                            Ollama (executor_node)
                                                                          │
                                                                tools_store.json
                                                                          │
                                                         user types "tool_name for X"
                                                                          │
                                                            live Slack checklist
```
