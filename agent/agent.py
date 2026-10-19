"""The agent: Gemini decides which MCP tools to call; this file runs the loop and then
double-checks the result against what the tools actually did.

Where I don't trust the model, plain code takes over:
- tool_calls, routed_to, patient_id and the reply come from the real tool log / server state
- booking, cancelling, refill and routing actions are only reported if a tool really did them
- the safety net can raise urgency or force an escalation, never lower it
- if the LLM crashes, the message still goes to a human (emergencies included)
"""
import json
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent import safety
from agent.llm import GeminiChat, declarations_from_mcp
from agent.prompt import ACTIONS, INTENTS, URGENCY, system_prompt, user_prompt

ROOT = Path(__file__).resolve().parent.parent
HARNESS_TOOLS = ("begin_message", "get_run_state")
MAX_STEPS = 14

# queue -> the action word for a route_to_queue call
ROUTE_ACTION = {"clinician_urgent": "route_to_clinician", "clinician_routine": "route_to_clinician",
                "provider_refills": "route_to_provider", "front_desk": "route_to_front_desk",
                "billing": "route_to_billing"}
# actions that must be backed by a successful tool call; model claims alone are dropped
TOOL_BACKED = {"escalate_emergency", "route_to_clinician", "route_to_provider", "route_to_billing",
               "route_to_front_desk", "prepare_refill_for_approval", "book_appointment",
               "reschedule_appointment", "cancel_appointment"}


class Runner:
    """Starts the MCP server as a subprocess and processes messages through it."""

    def __init__(self, session, make_chat, today, tools):
        self.session = session
        self.make_chat = make_chat
        self.today = today
        self.tools = tools

    async def call(self, name, args=None):
        res = await self.session.call_tool(name, args or {})
        text = res.content[0].text if res.content else "{}"
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"ok": False, "error": text}

    async def process(self, msg):
        """One message in, (result, trace) out. Never raises."""
        log = []  # every tool call that really happened
        trace = {"id": msg["id"], "calls": log, "steps": 0, "tokens_in": 0, "tokens_out": 0, "notes": []}
        scan = safety.scan(msg["body"])
        trace["safety"] = scan
        parsed = None
        try:
            await self.call("begin_message", {"message_id": msg["id"]})
            parsed = await self._loop(msg, scan, log, trace)
        except Exception as e:  # LLM down, quota, bad response ... do not drop the message
            trace["error"] = f"{type(e).__name__}: {e}"
            trace["notes"].append("agent crashed; falling back to safe routing")
        await self._ensure_verified(msg, log)
        await self._enforce_safety(msg, scan, log, trace, crashed=parsed is None)
        return await self._build_result(msg, parsed, log, trace), trace

    # ---- the loop ----
    async def _loop(self, msg, scan, log, trace):
        chat = self.make_chat()
        turn = chat.send(user_prompt(msg, scan))
        emergency = nudged_json = nudged_reply = False
        while True:
            trace["tokens_in"] += turn.tokens_in
            trace["tokens_out"] += turn.tokens_out
            if turn.calls:
                trace["steps"] += 1
                if trace["steps"] > MAX_STEPS:
                    trace["notes"].append("hit step limit")
                    return None
                results = []
                for c in turn.calls:
                    if emergency:
                        # after an escalation nothing else is allowed to happen
                        result = {"ok": False, "error": "Emergency already escalated. Do nothing else; give the final JSON."}
                    elif c["name"] in HARNESS_TOOLS:
                        result = {"ok": False, "error": "unknown tool"}
                    else:
                        result = await self.call(c["name"], c["args"])
                        log.append({"name": c["name"], "args": c["args"], "result": result})
                        if c["name"] == "escalate_emergency" and result.get("ok"):
                            emergency = True
                    results.append((c["name"], result))
                turn = chat.send_results(results)
                continue

            parsed = parse_json(turn.text)
            if parsed is None and not nudged_json:
                nudged_json = True
                turn = chat.send("Reply with ONLY the final JSON object described in the instructions.")
                continue
            if parsed is not None and not nudged_reply and not emergency:
                state = await self.call("get_run_state", {"message_id": msg["id"]})
                needs_reply = not state.get("reply_draft") and "no_action" not in (parsed.get("actions") or []) \
                    and parsed.get("intents") != ["spam"]
                if needs_reply:
                    nudged_reply = True
                    turn = chat.send("You have not saved a reply yet. Call draft_reply, then give the final JSON again.")
                    continue
            return parsed

    # ---- verification must not depend on the model remembering ----
    async def _ensure_verified(self, msg, log):
        """If the model never called find_patient (e.g. a simple hours question), do it here,
        so patient_id is right and we know who we are talking to."""
        if any(c["name"] == "find_patient" for c in log):
            return
        args = {"name": msg["from_name"], "contact": msg["from_contact"]}
        try:
            r = await self.call("find_patient", args)
        except Exception:
            return
        log.append({"name": "find_patient", "args": {**args, "forced": True}, "result": r})

    # ---- safety net after the model ----
    async def _enforce_safety(self, msg, scan, log, trace, crashed):
        names = [c["name"] for c in log]
        lang, name = self._patient_lang_name(log, msg)
        if scan["hard"] and "escalate_emergency" not in names:
            r = await self.call("escalate_emergency", {
                "reason": f"Safety net: {', '.join(scan['hard'])}. Message: {msg['body'][:200]}",
                "language": lang, "patient_name": name})
            log.append({"name": "escalate_emergency", "args": {"forced": True}, "result": r})
            trace["notes"].append("safety net forced an escalation")
        elif scan["soft"] and not any(c["name"] == "route_to_queue" and c["args"].get("queue") == "clinician_urgent"
                                      for c in log) and "escalate_emergency" not in names:
            r = await self.call("route_to_queue", {"queue": "clinician_urgent", "priority": "urgent",
                                                   "reason": f"Safety net: {', '.join(scan['soft'])}"})
            log.append({"name": "route_to_queue", "args": {"queue": "clinician_urgent", "forced": True}, "result": r})
            trace["notes"].append("safety net forced a clinician routing")
        elif crashed and "escalate_emergency" not in names:  # never let a failed message vanish
            r = await self.call("route_to_queue", {"queue": "front_desk", "priority": "routine",
                                                   "reason": "Automated agent failed on this message; needs a human."})
            log.append({"name": "route_to_queue", "args": {"queue": "front_desk", "forced": True}, "result": r})

    @staticmethod
    def _patient_lang_name(log, msg):
        for c in log:
            r = c["result"]
            if c["name"] == "find_patient" and r.get("verified"):
                return ("es" if r.get("preferred_language") == "Spanish" else "en",
                        msg["from_name"] if r.get("sender_role") == "caregiver" else r.get("patient_name"))
        return "en", None

    # ---- build the final result from facts ----
    async def _build_result(self, msg, parsed, log, trace):
        state = await self.call("get_run_state", {"message_id": msg["id"]})
        ok_calls = [c for c in log if c["result"].get("ok")]
        names = [c["name"] for c in log]

        queues = []
        for q in state.get("routed", []):
            if q["queue"] not in queues:
                queues.append(q["queue"])

        emergency = any(c["name"] == "escalate_emergency" for c in ok_calls)

        # actions that tools prove happened
        derived = []
        def add(a):
            if a not in derived:
                derived.append(a)
        for c in ok_calls:
            n, a = c["name"], c["args"]
            if n == "escalate_emergency":
                add("escalate_emergency")
            elif n == "route_to_queue":
                add(ROUTE_ACTION[a["queue"]])
            elif n == "prepare_refill_request":
                add("prepare_refill_for_approval")
            elif n in ("book_appointment", "reschedule_appointment", "cancel_appointment"):
                add(n.replace("_appointment", "") + "_appointment")

        # actions only the model can judge (offers, flags, answers)
        claimed = [a for a in (parsed or {}).get("actions", []) if a in ACTIONS]
        dropped = [a for a in claimed if a in TOOL_BACKED and a not in derived]
        if dropped:
            trace["notes"].append(f"dropped unsupported actions: {dropped}")
        extra = [a for a in claimed if a not in TOOL_BACKED]
        actions = derived + [a for a in extra if a not in derived]
        if len(actions) > 1:  # acknowledge means "nothing else done"; it makes no sense next to a real action
            actions = [a for a in actions if a != "acknowledge"]
        if not actions:
            # no_action is for messages we deliberately ignore (spam). If we wrote a reply, we acknowledged.
            actions = ["acknowledge"] if state.get("reply_draft") else ["no_action"]
        if emergency:
            # after an escalation nothing else may happen, so that is the only action reported
            if actions != ["escalate_emergency"]:
                trace["notes"].append(f"emergency: ignored other actions {[a for a in actions if a != 'escalate_emergency']}")
            actions = ["escalate_emergency"]

        # urgency: model's word, but never below what the safety net / tools say
        urgency = (parsed or {}).get("urgency")
        if urgency not in URGENCY:
            urgency = "routine"
        rank = {"routine": 0, "urgent": 1, "emergency": 2}
        floor = "emergency" if emergency else "routine"
        if floor == "routine" and any(q["queue"] == "clinician_urgent" or q["priority"] == "urgent"
                                      for q in state.get("routed", [])):
            floor = "urgent"
        if rank[floor] > rank[urgency]:
            trace["notes"].append(f"urgency raised {urgency} -> {floor}")
            urgency = floor

        intents = [i for i in (parsed or {}).get("intents", []) if i in INTENTS] or ["other"]

        patient_id = None
        for c in log:
            if c["name"] == "find_patient" and c["result"].get("verified"):
                patient_id = c["result"]["patient_id"]

        trace["model_output"] = parsed
        return {
            "id": msg["id"],
            "patient_id": patient_id,
            "intents": intents,
            "urgency": urgency,
            "actions": actions,
            "routed_to": queues,
            "tool_calls": names,
            "reply_draft": state.get("reply_draft") or "",
        }


def parse_json(text):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


@asynccontextmanager
async def open_runner(make_chat=None, model=None):
    """Spawn the MCP server, connect to it, and hand back a Runner."""
    from datetime import date
    params = StdioServerParameters(command=sys.executable, args=["-m", "server.clinic_server"], cwd=str(ROOT))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            clinic = json.loads((ROOT / "edvak_forge_data" / "clinic.json").read_text())
            today = date.fromisoformat(clinic["today"])
            if make_chat is None:
                decls = declarations_from_mcp(tools, hidden=HARNESS_TOOLS)
                make_chat = lambda: GeminiChat(system_prompt(today), decls, model)
            yield Runner(session, make_chat, today, tools)
