"""Run the agent.

  python run.py                              all 40 sample messages -> results.json (+ trace.json)
  python run.py --only M003,M017             just those
  python run.py --text "..." --from "Robert Chen" --contact 555-0102     one new message (live demo)
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from agent.agent import open_runner  # noqa: E402  (after load_dotenv so the key is set)

DATA = Path(__file__).parent / "edvak_forge_data"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--messages", default=str(DATA / "messages_sample.json"))
    ap.add_argument("--only", help="comma separated message ids")
    ap.add_argument("--out", default="results.json")
    ap.add_argument("--trace", default="trace.json")
    ap.add_argument("--delay", type=float, default=0, help="seconds to wait between messages (free-tier rate limits)")
    ap.add_argument("--model", help="override GEMINI_MODEL")
    ap.add_argument("--text", help="process one new message instead of the file")
    ap.add_argument("--from", dest="sender", default="Unknown")
    ap.add_argument("--contact", default="")
    ap.add_argument("--channel", default="sms")
    args = ap.parse_args()

    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY is not set. Copy .env.example to .env and put your key in it.")

    if args.text:
        messages = [{"id": "NEW1", "received_at": "2026-11-09T09:00:00-06:00", "channel": args.channel,
                     "from_name": args.sender, "from_contact": args.contact, "body": args.text}]
    else:
        messages = json.loads(Path(args.messages).read_text())
        if args.only:
            keep = set(args.only.split(","))
            messages = [m for m in messages if m["id"] in keep]

    results, traces = [], []
    started = time.time()
    async with open_runner(model=args.model) as runner:
        for i, msg in enumerate(messages):
            result, trace = await runner.process(msg)
            results.append(result)
            traces.append(trace)
            flag = "  <-- " + trace["error"][:80] if trace.get("error") else ""
            print(f"{msg['id']:>5}  {result['urgency']:<9} {','.join(result['actions']):<60} "
                  f"-> {','.join(result['routed_to']) or '-'}{flag}", flush=True)
            if not args.text:  # save as we go, so a stopped run still leaves something
                Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))
                Path(args.trace).write_text(json.dumps(traces, indent=2, ensure_ascii=False, default=str))
            if args.delay and i < len(messages) - 1:
                time.sleep(args.delay)

    if args.text:
        print(json.dumps(results[0], indent=2, ensure_ascii=False))
        print("\nnotes:", traces[0]["notes"])
        return
    Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))
    Path(args.trace).write_text(json.dumps(traces, indent=2, ensure_ascii=False, default=str))
    tin = sum(t["tokens_in"] for t in traces)
    tout = sum(t["tokens_out"] for t in traces)
    print(f"\n{len(results)} messages in {time.time() - started:.0f}s, tokens in/out: {tin}/{tout}")
    print(f"wrote {args.out} and {args.trace}. Next: python eval.py {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
