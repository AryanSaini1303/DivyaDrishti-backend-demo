import json
import requests

BASE_URL = "http://localhost:8000/ask"
ORIGIN = "WHOLESALE_FASHION"

QUESTIONS = [
    "What's our policy on returns or damaged goods?",
    "What's the minimum order quantity for a new customer?",
    "How many orders does Jhaskcom Pvt ltd Jaipur have, and what's our return policy on damaged goods?",
]


def send(question: str):
    resp = requests.post(
        BASE_URL,
        json={"question": question, "conversation": [], "origin": ORIGIN},
        stream=True,
    )
    resp.raise_for_status()

    event_name = None
    for raw_line in resp.iter_lines(decode_unicode=True):
        if raw_line is None or raw_line == "":
            continue
        if raw_line.startswith("event:"):
            event_name = raw_line.split(":", 1)[1].strip()
        elif raw_line.startswith("data:"):
            data = json.loads(raw_line.split(":", 1)[1].strip())
            if event_name == "meta":
                print(f"  [meta] {data}")
            elif event_name == "token":
                print(data["text"], end="", flush=True)
            elif event_name == "error":
                print(f"\n  [error] {data['message']}")
    print()


def run():
    for question in QUESTIONS:
        print(f"\n> {question}")
        send(question)


if __name__ == "__main__":
    run()