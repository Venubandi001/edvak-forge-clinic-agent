# Maplewood Family Clinic: AI front-desk agent

An agent that works the patient inbox. It reads each message, uses an MCP tool server over the clinic data to do the right thing, and hands anything risky to a human. Built for the Edvak Forge take-home.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then put your Gemini key in .env (free key: aistudio.google.com/apikey)

python run.py                   # all 40 sample messages -> results.json, trace.json
python eval.py                  # scores against labels_sample.json -> eval_report.md
pytest                          # 40 tests, no API key needed
python chat.py                  # type your own messages and watch the tool calls
```

A new message (what I'll do live):

```bash
python run.py --text "I can't breathe" --from "Daniel Kim" --contact 555-0110
```

The free Gemini tier is slow and capped: about 15 requests a minute and a daily limit per key. A full run of 40 messages takes about 15 minutes. Calls retry with backoff on 429s and network errors, and if a key hits its daily cap the agent moves on to `GEMINI_API_KEY_2`, `_3` (optional extra keys in `.env`). Results are saved after every message.

## Architecture

```
message -> safety net (regex) -> Gemini tool-calling loop <--MCP--> clinic tool server (in-memory data)
                                        |
                          result builder: checks the model's answer against what the tools really did
```

1. **MCP tool server** (`server/`). 14 tools: find/verify patient, medications, refill eligibility, prepare refill, free slots, book/reschedule/cancel, clinic info, route to queue, escalate emergency, draft reply. The clinic rules live in `server/policy.py` as plain functions, and the tools call them. So the rules are enforced by the tools, not by the prompt. Example: `prepare_refill_request` refuses a controlled drug even if the model asks for it.
2. **Agent** (`agent/`). Gemini decides which tool to call next, sees the result, and continues until it can give a final JSON. That is the whole "agent" part: an LLM in a tool-calling loop. No framework, about 200 lines you can read.
3. **Guard code around the model**, because I don't want an emergency to depend on one LLM answer:
   - A regex **safety net** runs on every message. It can only push things up, never down. A hard hit (chest pain, can't breathe, head injury + confusion, low blood sugar, bleeding, self-harm...) forces `escalate_emergency` if the model didn't call it. A soft hit (low mood) forces a clinician routing.
   - After an escalation the loop stops running tools. The reply is fixed text on the server (call 911 or go to the ER, the clinic has been alerted), so the model can't add clinical advice.
   - `tool_calls`, `routed_to`, `patient_id` and `reply_draft` come from the real tool log and server state, not from what the model says. Actions like book, cancel, prepare refill, route are only reported if a tool actually did them.
   - If the LLM call fails, the message still goes to a human (front desk, or an emergency escalation if the net hit).
   - The sender is always verified by code, even if the model skips `find_patient` (it did that on simple hours questions, which left `patient_id` empty).
4. **Privacy**: tools only return patient data for a patient that `find_patient` verified for that message (name + DOB, or phone/email on file, caregivers included). An unverified sender gets nothing back, not even a patient id.

Model: `gemini-3.1-flash-lite-preview` (free tier, supports function calling). I started on `gemini-2.5-flash` but its free tier allows only 20 requests a day, which is not enough for one run. The model sits behind one small file (`agent/llm.py`), so it can be swapped with `GEMINI_MODEL` in `.env`.

## What I reused and what I built

Reused: the `mcp` Python SDK (FastMCP server and stdio client), the `google-genai` SDK, python-dotenv, pytest. I used Claude Code as a coding assistant for parts of this and reviewed and ran everything myself.

Built by me: all the tools and clinic rules, the agent loop, the safety net, the result builder, the eval harness, the tests, and the prompt.

Left out on purpose: a database (JSON in memory is allowed), an agent framework (the loop is short enough), a UI, deployment.

## Results

Final run on the 40 sample messages (`python run.py` then `python eval.py`; full report in `eval_report.md`). Model: `gemini-3.1-flash-lite-preview`, temperature 0, about 12 minutes on the free tier.

**Safety**

| | |
|---|---|
| Emergency recall | **5/5** (M003, M014, M017, M032, M035) |
| Missed emergencies | **0** |
| Emergencies answered with clinical advice | **0** (the reply is fixed text on the server) |
| False emergency alarms | **0** (the "dying to get in" idiom, M024, was booked, not escalated) |
| `must_not` violations | **0** |
| Urgent mood case (M006) reached `clinician_urgent` | yes |

**Accuracy**

| | |
|---|---|
| Urgency inside the accepted set | 39/40 (98%) |
| Actions, exact set match | 38/40 (95%) |
| Actions, all expected present | 39/40 (98%) |
| Action precision / recall | 96% / 98% |
| Intents | 40/40 (100%) |
| `patient_id` (PDF rule: null if unverified) | 40/40; 38/40 against the labels, see below |

**Cost and size:** about 810k input / 9k output tokens and 147 tool calls for a full run. Free tier: $0. At paid Flash-Lite style prices I estimate about $0.27 (my price numbers in `eval.py` are approximate, check current pricing).

**What it gets wrong**

- **M004** (metformin, not seen in 12 months): marked `urgent`, label says routine. It also routes to the provider as well as offering a visit. The clinic policy says to do both, the label for this message only has the visit. A judgement call I did not tune away.
- **M022** (cancel "tomorrow"): the patient has two appointments tomorrow. The agent changes nothing, asks which one in the reply, and routes to the front desk. The label expects a cancel. I chose to ask, because cancelling the wrong visit is worse than one extra message.
- **M011 and M027**: the labels give a `patient_id` for senders who are not verified. The PDF says null, so I output null.

**How I got here (so the numbers are honest).** The first full run was much worse: exact action match was 32%. It added `answer_directly` to almost everything, offered slots instead of booking (M015, M016, M024, M039), searched the wrong week for "the following week" (M034), and used `request_verification` instead of `decline_disclosure` (M027). I fixed these with general prompt rules, never by message id. Along the way I also found and fixed three bugs in my own code:

1. After a model failure a message could end as `no_action` with nothing routed. A test caught it, and a crash now always goes to the front desk unless the message was already escalated as an emergency.
2. On simple questions like opening hours the model skipped `find_patient`, so `patient_id` came back empty. The code now verifies the sender itself.
3. A reply that asked a question but never routed to a human was reported as `no_action`. It is now `acknowledge`, and the prompt requires the routing call.

During one run the wifi dropped. Four messages hit network errors and used the fallback path. The emergency among them (M017) was still escalated by the safety net without the model, so recall stayed 5/5. That run is not the one reported above.

**Limits of these numbers:** one run on 40 messages that I tuned the prompt against. I have not measured run-to-run variance (temperature 0 still is not fully deterministic) and I have not tested unseen messages at scale. Treat them as an upper bound on how it does on new messages.

## Things I decided that the labels disagree with

- **patient_id for M011 and M027.** The labels give a patient id, but the PDF says it is null if the sender isn't a verified patient. I followed the PDF. `eval.py` reports both numbers.
- **M022 (cancel "tomorrow").** Hannah Lee has two appointments tomorrow. The agent is told to change nothing when it can't tell which one is meant, and to ask the front desk. The label expects a cancel, so this may count as a miss. I think asking is safer than cancelling the wrong visit.
- **M004 / refill not seen in 12 months.** The clinic policy says offer an appointment *and* route to the provider. I follow the policy, which can show up as an extra `route_to_provider` against the label. The eval shows both exact-match and "all expected present".

## Known weak spots

- The safety net is regex. It knows common English phrasing and a few Spanish phrases. A paraphrase it doesn't know depends on the model.
- Dates like "next week" or "the 18th" rely on the model using the calendar in the prompt.
- The `must_not` checks in `eval.py` are heuristics (keyword checks on replies), not proof.
- Free-tier rate limits make a full run slow.

## With another week

- Run each message several times and measure disagreement; tune the prompt only with general rules, never per message id.
- More emergency test phrasings (paraphrases, typos, Spanish, caregiver wording) and measure net + model separately.
- A second model as a cross-check on emergencies.
- Proper multilingual handling and a real billing data source.
- Tool-call-level eval (right tool, right arguments), not just final outputs.

## Before this touches real patient data

- A signed BAA with the model provider, or a model hosted inside our own HIPAA environment. The free Gemini tier is not acceptable for PHI.
- Real identity verification (a phone number on file is spoofable) and caregiver consent records.
- Audit log of every tool call and reply, access control, encryption, retention rules, no PHI in app logs.
- A human approves every outbound message and every prescription step. Today replies are drafts only.
- Real queues with SLAs and alerting for emergencies, instead of an in-memory list.
- Much bigger evaluation set, red-teaming for prompt injection, clinical review of the safety rules.
