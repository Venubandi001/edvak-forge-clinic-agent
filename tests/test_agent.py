"""Agent plumbing tests with a scripted model (no API key needed).

These check what the code around the model guarantees, not how smart Gemini is.
"""
import asyncio
import json

from agent.agent import open_runner
from agent.llm import Turn
from agent import safety


class FakeChat:
    """Plays back a list of Turns, one per send()."""
    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []

    def send(self, text):
        self.seen.append(text)
        return self.turns.pop(0)

    def send_results(self, results):
        self.seen.append(results)
        return self.turns.pop(0)


def call(tool, **args):
    return {"name": tool, "args": args}


def final(**kw):
    return Turn(text=json.dumps(kw))


def run(turns, msg, crash=False):
    async def go():
        chat = FakeChat(turns)
        if crash:
            def boom():
                raise RuntimeError("model down")
            make = boom
        else:
            make = lambda: chat
        async with open_runner(make_chat=make) as r:
            res, trace = await r.process(msg)
            return res, trace, chat
    return asyncio.run(go())


def m(body, name="Robert Chen", contact="555-0102", id="X1"):
    return {"id": id, "channel": "sms", "from_name": name, "from_contact": contact, "body": body,
            "received_at": "2026-11-09T08:00:00-06:00"}


def test_refill_happy_path_builds_result_from_tool_log():
    turns = [
        Turn(calls=[call("find_patient", name="Robert Chen", contact="555-0102")]),
        Turn(calls=[call("check_refill_eligibility", patient_id="P002", medication="lisinopril")]),
        Turn(calls=[call("prepare_refill_request", patient_id="P002", medication="lisinopril")]),
        Turn(calls=[call("draft_reply", text="Hi Robert, your refill is waiting for approval.")]),
        final(intents=["refill"], urgency="routine", actions=["prepare_refill_for_approval"]),
    ]
    res, trace, _ = run(turns, m("lisinopril refill please"))
    assert res["patient_id"] == "P002"
    assert res["actions"] == ["prepare_refill_for_approval"]
    assert res["routed_to"] == ["provider_refills"]
    assert res["tool_calls"] == ["find_patient", "check_refill_eligibility", "prepare_refill_request", "draft_reply"]
    assert res["reply_draft"].startswith("Hi Robert")


def test_model_cannot_claim_actions_no_tool_did():
    turns = [
        Turn(calls=[call("find_patient", name="Marcus Bell", contact="555-0106")]),
        Turn(calls=[call("draft_reply", text="Hi")]),
        final(intents=["refill"], urgency="routine", actions=["prepare_refill_for_approval", "book_appointment"]),
    ]
    res, trace, _ = run(turns, m("more xanax", "Marcus Bell", "555-0106"))
    assert "prepare_refill_for_approval" not in res["actions"] and "book_appointment" not in res["actions"]
    assert any("dropped" in n for n in trace["notes"])


def test_safety_net_forces_escalation_when_model_misses_it():
    turns = [
        Turn(calls=[call("find_patient", name="Aisha Johnson", contact="555-0103")]),
        Turn(calls=[call("draft_reply", text="Take an aspirin and rest.")]),  # bad model behaviour
        final(intents=["clinical"], urgency="routine", actions=["acknowledge"]),
    ]
    res, trace, _ = run(turns, m("crushing chest pain going into my left arm", "Aisha Johnson", "555-0103"))
    assert res["urgency"] == "emergency"
    assert "escalate_emergency" in res["actions"]
    assert res["routed_to"] == ["clinician_urgent"]
    assert "911" in res["reply_draft"] and "aspirin" not in res["reply_draft"].lower()


def test_nothing_runs_after_an_escalation():
    turns = [
        Turn(calls=[call("find_patient", name="Samuel Foster", contact="samuel.foster@example.com")]),
        Turn(calls=[call("escalate_emergency", reason="bleeding on warfarin", language="en", patient_name="Samuel Foster")]),
        Turn(calls=[call("prepare_refill_request", patient_id="P020", medication="warfarin")]),  # must be refused
        final(intents=["refill", "clinical"], urgency="emergency", actions=["escalate_emergency", "prepare_refill_for_approval"]),
    ]
    res, trace, _ = run(turns, m("refill warfarin, gums bleeding a lot and dark stools", "Samuel Foster",
                                 "samuel.foster@example.com"))
    assert res["actions"] == ["escalate_emergency"]
    assert res["routed_to"] == ["clinician_urgent"]
    assert "prepare_refill_request" not in [c["name"] for c in trace["calls"]]


def test_mood_concern_floor_is_urgent_and_clinician():
    turns = [
        Turn(calls=[call("find_patient", name="Emily Novak", contact="emily.novak@example.com")]),
        Turn(calls=[call("draft_reply", text="Hi Emily")]),
        final(intents=["refill", "clinical"], urgency="routine", actions=[]),
    ]
    res, trace, _ = run(turns, m("refill sertraline. I don't see the point of anything lately", "Emily Novak",
                                 "emily.novak@example.com"))
    assert res["urgency"] == "urgent"
    assert "route_to_clinician" in res["actions"] and "clinician_urgent" in res["routed_to"]


def test_model_crash_still_reaches_a_human():
    res, trace, _ = run([], m("Can I get an appointment?"), crash=True)
    assert res["routed_to"] == ["front_desk"] and "RuntimeError" in trace["error"]


def test_model_crash_with_emergency_text_still_escalates():
    res, trace, _ = run([], m("I can't breathe", "Daniel Kim", "555-0110"), crash=True)
    assert res["urgency"] == "emergency" and res["actions"] == ["escalate_emergency"]
    assert "911" in res["reply_draft"]


def test_missing_reply_gets_one_nudge():
    turns = [
        Turn(calls=[call("find_patient", name="Robert Chen", contact="555-0102")]),
        final(intents=["admin"], urgency="routine", actions=["answer_directly"]),
        Turn(calls=[call("draft_reply", text="We are open 9-5.")]),
        final(intents=["admin"], urgency="routine", actions=["answer_directly"]),
    ]
    res, _, chat = run(turns, m("hours?"))
    assert res["reply_draft"] == "We are open 9-5."


def test_unverified_sender_gets_null_patient_id():
    turns = [
        Turn(calls=[call("find_patient", name="Mike", contact="555-0888")]),
        Turn(calls=[call("draft_reply", text="I can't share that.")]),
        final(intents=["third_party"], urgency="routine", actions=["decline_disclosure"]),
    ]
    res, _, _ = run(turns, m("does my wife Grace Park have an appointment?", "Mike", "555-0888"))
    assert res["patient_id"] is None and res["actions"] == ["decline_disclosure"]


# ---- safety net patterns ----
def test_net_catches_real_emergencies():
    for text in ["crushing chest pain", "I can't breathe", "My inhaler isn't helping, I'm wheezing and can't finish sentences",
                 "Mom fell and hit her head, she seems confused now", "blood sugar was 45 and I'm shaky",
                 "gums bleeding a lot and dark stools", "tengo dolor de pecho", "I want to kill myself"]:
        assert safety.scan(text)["hard"], text


def test_net_ignores_idioms_and_normal_requests():
    for text in ["I'm dying to get in before my trip lol", "I'm confused about my bill", "what are your hours?",
                 "my blood sugar was 110 this morning", "Please refill my metformin", "can I see Dr. Patel Tuesday"]:
        assert not safety.scan(text)["hard"], text


def test_net_soft_for_low_mood():
    assert safety.scan("I've been feeling really low lately and some days I don't see the point of anything")["soft"]


# ---- Gemini request shape (offline: only checks our config is valid, no API call) ----
def test_tool_declarations_are_valid_for_gemini():
    from google.genai import types
    from agent.agent import HARNESS_TOOLS
    from agent.llm import declarations_from_mcp

    async def go():
        async with open_runner(make_chat=lambda: None) as r:
            return declarations_from_mcp(r.tools, hidden=HARNESS_TOOLS)
    decls = asyncio.run(go())
    names = [d["name"] for d in decls]
    assert "begin_message" not in names and "get_run_state" not in names
    assert {"find_patient", "check_refill_eligibility", "book_appointment", "route_to_queue",
            "escalate_emergency", "draft_reply"} <= set(names)
    cfg = types.GenerateContentConfig(tools=[types.Tool(function_declarations=decls)])
    fd = cfg.tools[0].function_declarations
    assert len(fd) == len(decls)
    fp = next(d for d in fd if d.name == "find_patient")
    assert set(fp.parameters.properties) == {"name", "contact", "dob"} and not fp.parameters.required
    bk = next(d for d in fd if d.name == "book_appointment")
    assert set(bk.parameters.required) == {"patient_id", "provider", "date", "start", "visit_type"}


def test_reply_without_any_action_is_acknowledge_not_no_action():
    turns = [
        Turn(calls=[call("find_patient", name="Hannah Lee", contact="555-0111")]),
        Turn(calls=[call("draft_reply", text="Which appointment do you mean?")]),
        final(intents=["cancel"], urgency="routine", actions=["route_to_front_desk"]),  # claimed, never done
    ]
    res, trace, _ = run(turns, m("cancel my appointment tomorrow", "Hannah Lee", "555-0111"))
    assert res["actions"] == ["acknowledge"] and res["routed_to"] == []
    assert any("dropped" in n for n in trace["notes"])


def test_spam_stays_no_action():
    turns = [
        Turn(calls=[call("find_patient", name="BillingPro Solutions", contact="offers@billingpro.example")]),
        final(intents=["spam"], urgency="routine", actions=["no_action"]),
    ]
    res, _, _ = run(turns, m("Special offer on billing services", "BillingPro Solutions", "offers@billingpro.example"))
    assert res["actions"] == ["no_action"] and res["reply_draft"] == ""


def test_acknowledge_dropped_when_a_real_action_exists():
    turns = [
        Turn(calls=[call("find_patient", name="Marcus Bell", contact="555-0106")]),
        Turn(calls=[call("route_to_queue", queue="provider_refills", reason="controlled", priority="routine")]),
        Turn(calls=[call("draft_reply", text="Hi Marcus")]),
        final(intents=["refill"], urgency="routine", actions=["route_to_provider", "acknowledge"]),
    ]
    res, _, _ = run(turns, m("more xanax", "Marcus Bell", "555-0106"))
    assert res["actions"] == ["route_to_provider"]


def test_sender_is_verified_even_if_model_skips_find_patient():
    turns = [
        Turn(calls=[call("get_clinic_info")]),
        Turn(calls=[call("draft_reply", text="We are open Mon-Fri 9-5.")]),
        final(intents=["admin"], urgency="routine", actions=["answer_directly"]),
    ]
    res, _, _ = run(turns, m("What are your clinic hours?", "Lucy Nguyen", "lucy.nguyen@example.com"))
    assert res["patient_id"] == "P023" and "find_patient" in res["tool_calls"]
    assert res["actions"] == ["answer_directly"]
