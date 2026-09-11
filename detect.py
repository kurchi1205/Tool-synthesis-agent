import json
import sys
import time
import ollama
from storage import get_users, get_events, tool_already_saved
from dotenv import load_dotenv
load_dotenv()

OLLAMA_MODEL = "llama3.1:8b"


def cluster_by_time(events: list[dict], window_minutes: int = 10) -> list[list[dict]]:
    """
    Pure function — no API calls.
    Groups consecutive events within window_minutes of each other.
    """
    if not events:
        return []

    sorted_events = sorted(events, key=lambda e: e["time"])
    clusters = []
    current = [sorted_events[0]]

    for event in sorted_events[1:]:
        from datetime import datetime
        prev_time = datetime.fromisoformat(current[-1]["time"].replace("Z", "+00:00"))
        curr_time = datetime.fromisoformat(event["time"].replace("Z", "+00:00"))
        gap_minutes = (curr_time - prev_time).total_seconds() / 60

        if gap_minutes <= window_minutes:
            current.append(event)
        else:
            if len(current) >= 2:
                clusters.append(current)
            current = [event]

    if len(current) >= 2:
        clusters.append(current)

    return clusters


def interpret_cluster(cluster: list[dict]) -> dict | None:
    """
    Calls Claude. Returns structured JSON or None on failure.
    Retries once on malformed output.
    """
    events_text = "\n".join(
        f"- [{e['source']}] {e['time']}: {e['text']}"
        for e in cluster
    )

    prompt = f"""You are analysing a sequence of user actions across different tools.

Events (in time order):
{events_text}

Determine if these events represent one coherent multi-step routine a user repeats.

Reply with ONLY valid JSON, no explanation:
{{
  "is_routine": true or false,
  "sequence": ["source1", "source2", ...],
  "slot": "the thing that varies each time (e.g. person, topic, vendor)",
  "slot_value": "the specific value in THIS instance",
  "description": "one sentence describing the routine"
}}

If not a routine, reply: {{"is_routine": false}}"""

    for attempt in range(2):
        try:
            print(f"[ollama] calling {OLLAMA_MODEL} (attempt {attempt + 1}/2) with {len(cluster)} events ...")
            resp = ollama.chat(
                model=OLLAMA_MODEL,
                messages=[{"role": "user", "content": prompt}]
            )
            text = resp["message"]["content"].strip()
            print(f"[ollama] raw response: {text[:200]}{'...' if len(text) > 200 else ''}")
            # Strip markdown code fences if present
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            result = json.loads(text)
            print(f"[ollama] parsed: is_routine={result.get('is_routine')}, sequence={result.get('sequence')}")
            if result.get("is_routine"):
                return result
            return None
        except (json.JSONDecodeError, KeyError) as e:
            print(f"[ollama] parse error (attempt {attempt + 1}): {e}")
            if attempt == 0:
                continue
            return None
    return None


def find_repeated_pattern(user_id: str) -> dict | None:
    """
    Scoped entirely to one user.
    Returns a pattern dict if 2+ clusters share the same sequence, else None.
    """
    events = get_events(user_id)
    if len(events) < 4:
        return None

    clusters = cluster_by_time(events)
    interpreted = []

    for cluster in clusters:
        result = interpret_cluster(cluster)
        if result:
            interpreted.append(result)

    # Find if 2+ clusters share the same sequence
    from collections import Counter
    sequence_counts = Counter(
        tuple(r["sequence"]) for r in interpreted
    )

    for sequence_tuple, count in sequence_counts.items():
        if count >= 2:
            # Return the most recent matching cluster's interpretation
            for r in reversed(interpreted):
                if tuple(r["sequence"]) == sequence_tuple:
                    return r

    return None


def detect_loop(interval_seconds: int = 15) -> None:
    # Import here to avoid circular import
    from slack_bot import propose_tool

    while True:
        users = get_users()
        for user_id in users:
            try:
                pattern = find_repeated_pattern(user_id)
                if pattern and not tool_already_saved(user_id, pattern["sequence"]):
                    propose_tool(user_id, pattern)
            except Exception as e:
                print(f"[detect] {user_id} error: {e}")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    user_id = sys.argv[2] if len(sys.argv) > 2 else "u1"
    if "--user" in sys.argv:
        pattern = find_repeated_pattern(user_id)
        print(json.dumps(pattern, indent=2) if pattern else "No pattern found")
    else:
        print("Usage: python detect.py --user u1")
