"""Type messages by hand and watch the agent work.  python chat.py

The server stays up between messages, so a booking you make stays booked for the next one
(same as a real run). Type 'quit' to stop, or just press Enter on the name to reuse the last sender.
"""
import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from agent.agent import open_runner  # noqa: E402

SAMPLES = """Examples (name / contact / message):
  Robert Chen / 555-0102 / Can I get my lisinopril refilled?
  Daniel Kim / 555-0110 / My inhaler isn't helping, I can't finish sentences
  Mike / 555-0888 / Does Grace Park have an appointment this week?
  Grace Park / grace.park@example.com / Book a follow-up with Dr. Patel Tuesday morning
  Linda Morales / 555-0101 / Necesito mas metformina por favor
"""


async def main():
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY is not set. Put it in .env first.")
    print(SAMPLES)
    n, last = 0, ("", "")
    async with open_runner() as runner:
        while True:
            name = input(f"Sender name [{last[0]}]: ").strip() or last[0]
            if name.lower() == "quit":
                return
            contact = input(f"Contact phone/email [{last[1]}]: ").strip() or last[1]
            body = input("Message: ").strip()
            if not body:
                continue
            last = (name, contact)
            n += 1
            msg = {"id": f"LIVE{n}", "received_at": "2026-11-09T09:00:00-06:00", "channel": "sms",
                   "from_name": name, "from_contact": contact, "body": body}
            print("\n...working")
            result, trace = await runner.process(msg)
            print("\n--- tool calls ---")
            for c in trace["calls"]:
                ok = "ok " if c["result"].get("ok") else "ERR"
                print(f"  {ok} {c['name']}({json.dumps(c['args'], ensure_ascii=False)[:90]})")
            print("\n--- result ---")
            print(json.dumps(result, indent=2, ensure_ascii=False))
            if trace["notes"]:
                print("notes:", trace["notes"])
            if trace.get("error"):
                print("ERROR:", trace["error"][:200])
            print()


if __name__ == "__main__":
    asyncio.run(main())
