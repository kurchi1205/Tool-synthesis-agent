import threading
from poller import polling_loop
from detect import detect_loop
from slack_bot import start_bot, setup_loop


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
        threading.Thread(
            target=setup_loop,
            kwargs={"interval_seconds": 3600},
            daemon=True,
            name="setup-checker",
        ),
    ]

    for t in threads:
        t.start()
        print(f"[main] started {t.name}")

    print("[main] starting Slack bot...")
    start_bot()  # blocking — keeps process alive


if __name__ == "__main__":
    main()
