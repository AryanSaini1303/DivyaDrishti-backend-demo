import json
import requests

BASE_URL = "http://localhost:8000/ask"
ORIGIN = "WHOLESALE_FASHION"

TURNS = [
    # "Hey, how are you?",
    # "How many franchises do we currently have?",
    # "Which one of them has the most orders?",
    # "What's the current stock situation for that franchise's warehouses?",
    "which all employees are working in the warehouse of the franchise which has the most orders?",
]


def send(question: str, conversation: list):
    resp = requests.post(
        BASE_URL,
        json={"question": question, "conversation": conversation, "origin": ORIGIN},
        stream=True,
    )
    resp.raise_for_status()

    answer_parts = []
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
                answer_parts.append(data["text"])
            elif event_name == "done":
                print()
            elif event_name == "chart_data":
                print(f"  [chart_data] {data}")
            elif event_name == "error":
                print(f"\n  [error] {data['message']}")

    return "".join(answer_parts)


def run():
    conversation = []
    for question in TURNS:
        print(f"\n> {question}")
        answer = send(question, conversation)
        conversation.append({"role": "user", "content": question})
        conversation.append({"role": "assistant", "content": answer})


if __name__ == "__main__":
    run()