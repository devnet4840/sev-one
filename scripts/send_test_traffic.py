"""Send a batch of test questions to a running Sev One app.

Why: LaunchDarkly's Monitoring tab only has numbers after real calls. Run this the day before a
demo so each variation has duration, token, cost, and judge data to compare.

Everything this sends is TEST TRAFFIC from three sample engineers. It shows how measurement
works; it is not enough traffic to prove one variation is better than another.

Usage (with the app already running in another window):
    python scripts/send_test_traffic.py                  # 5 rounds, about 60 questions
    python scripts/send_test_traffic.py --rounds 2
    python scripts/send_test_traffic.py --base-url http://localhost:8000

Cost: with a real OpenAI key, every question is one agent call plus one judge call.
Which variation each engineer gets depends on your targeting in LaunchDarkly at the time.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter

import httpx

ENGINEERS = ["qa-dana", "oncall-sam", "oncall-priya"]
QUESTIONS = [
    "What should I do first?",
    "Was it the last deploy?",
    "Should I fail over the database?",
    "Can I flush the cache?",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--rounds", type=int, default=5, help="times to ask every question as every engineer")
    parser.add_argument("--delay", type=float, default=0.5, help="seconds to wait between questions")
    args = parser.parse_args()

    total = args.rounds * len(ENGINEERS) * len(QUESTIONS)
    print(f"Sending {total} test questions to {args.base_url} (this is test traffic).")
    tally: Counter = Counter()
    sent = 0
    with httpx.Client(base_url=args.base_url, timeout=90, headers={"X-Sev-One": "1"}) as client:
        for _ in range(args.rounds):
            for persona in ENGINEERS:
                for question in QUESTIONS:
                    try:
                        r = client.post("/api/chat", json={"persona": persona, "question": question})
                        r.raise_for_status()
                        tally[(persona, r.json()["answer"]["source"])] += 1
                    except httpx.HTTPError as exc:
                        print(f"  {persona}: request failed ({exc}). Is the app running at {args.base_url}?")
                        return 1
                    sent += 1
                    print(f"  {sent}/{total} sent", end="\r")
                    time.sleep(args.delay)

    print(f"\nDone. {sent} questions sent.")
    for persona in ENGINEERS:
        print(f"  {persona}: {tally[(persona, 'ai')]} AI answers, {tally[(persona, 'standard')]} standard messages")
    print("Open the config's Monitoring tab in LaunchDarkly. If it looks empty, wait a few minutes and refresh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
