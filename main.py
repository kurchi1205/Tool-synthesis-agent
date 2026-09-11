import threading
from poller import polling_loop
from detect import detect_loop
from slack_bot import start_bot


def main():
    threads = [
        threading.Thread(
            target=polling_loop,
            kwargs={"interval_seconds": 15},
            daemon=True,
            name="poller",
        ),
        threading.Thread(
            target=detect_loop,
            kwargs={"interval_seconds": 15},
            daemon=True,
            name="detector",
        ),
    ]

    for t in threads:
        t.start()
        print(f"[main] started {t.name}")

    print("[main] starting Slack bot...")
    start_bot()  # blocking — keeps process alive


if __name__ == "__main__":
    main()
