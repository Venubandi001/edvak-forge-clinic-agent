"""MCP server for Maplewood Family Clinic. Run with: python -m server.clinic_server (stdio).

Design rules I stuck to:
- The tools enforce the clinic policy, not the model. Even if the model asks for something
  it shouldn't, the tool says no and explains why.
- Patient details are only returned for a patient that find_patient has verified
  *for the current message*.
- Tools return {"ok": false, "error": ...} instead of raising, so the model can read the problem.
"""
from datetime import date, datetime, timedelta
from typing import Optional

from mcp.server.fastmcp import FastMCP

from server import policy
from server.store import Clinic

mcp = FastMCP("maplewood-clinic")
db = Clinic()

QUEUES = db.clinic["routing_queues"]
PRIORITIES = ("routine", "urgent", "emergency")

EMERGENCY_REPLY = {
    "en": "{hi}If this may be an emergency, please call 911 or go to the nearest emergency "
          "department right now. We have alerted our clinical team.",
    "es": "{hi}Si esto puede ser una emergencia, llame al 911 o vaya ahora mismo al "
          "departamento de emergencias más cercano. Hemos avisado a nuestro equipo clínico.",
}


def _err(msg):
    return {"ok": False, "error": msg}


def _not_verified(patient_id):
    if patient_id not in db.verified:
        return _err(f"{patient_id} is not verified for this message. Call find_patient first, "
                    "and if the sender can't be verified, don't share any patient details.")
    return None


def _queue(queue, reason, priority):
    db.queue.append({"message_id": db.message_id, "queue": queue, "priority": priority, "reason": reason})


# ---------- harness only ----------

@mcp.tool()
def begin_message(message_id: str) -> dict:
    """(Harness use) Start a new message. Clears who was verified for the previous one."""
    db.begin_message(message_id)
    return {"ok": True}


# ---------- patient lookup ----------

@mcp.tool()
def find_patient(name: Optional[str] = None, contact: Optional[str] = None,
                 dob: Optional[str] = None) -> dict:
    """Find and verify the sender. Pass the sender's name, the phone/email the message came
    from (contact), and a date of birth (YYYY-MM-DD) only if the sender wrote one.

    Verified means: name + DOB match, or the message came from the phone/email on file
    (an authorised caregiver's phone counts too). If not verified you get no patient details.
    """
    hit = policy.verify_sender(db.patients, name, contact, dob)
    if hit is None:
        return {"ok": True, "verified": False,
                "reason": "No patient on file matches this name + DOB, or this phone/email. "
                          "Do not share any patient information."}
    p = hit["patient"]
    db.verified[p["id"]] = hit["role"]
    out = {"ok": True, "verified": True, "patient_id": p["id"], "patient_name": p["name"],
           "sender_role": hit["role"], "verified_by": hit["method"],
           "preferred_language": p["preferred_language"]}
    if not hit["name_matches"]:
        out["warning"] = "Contact matches the record but the sender name does not."
    return out


@mcp.tool()
def get_patient_summary(patient_id: str) -> dict:
    """Last visit, primary provider, allergies and language for a verified patient."""
    if (e := _not_verified(patient_id)):
        return e
    p = db.patient(patient_id)
    return {"ok": True, "patient_id": p["id"], "name": p["name"], "last_visit": p["last_visit"],
            "primary_provider": p["primary_provider"], "allergies": p["allergies"],
            "preferred_language": p["preferred_language"]}


# ---------- medications ----------

@mcp.tool()
def get_medications(patient_id: str) -> dict:
    """Active medications for a verified patient."""
    if (e := _not_verified(patient_id)):
        return e
    return {"ok": True, "medications": db.meds_for(patient_id)}


@mcp.tool()
def check_refill_eligibility(patient_id: str, medication: str) -> dict:
    """Check one medication against the refill policy. Brand names are fine (Xanax).

    verdict: eligible | controlled_substance | not_seen_12_months | zero_refills |
             too_early (see earliest_refill_date) | not_on_list
    """
    if (e := _not_verified(patient_id)):
        return e
    result = policy.refill_eligibility(db.patient(patient_id), db.meds_for(patient_id),
                                       medication, db.today)
    return {"ok": True, **result}


@mcp.tool()
def prepare_refill_request(patient_id: str, medication: str) -> dict:
    """Prepare a refill for provider approval (puts it in the provider_refills queue).
    Only works if the medication is eligible. This never approves or sends a prescription.
    """
    if (e := _not_verified(patient_id)):
        return e
    result = policy.refill_eligibility(db.patient(patient_id), db.meds_for(patient_id),
                                       medication, db.today)
    if result["verdict"] != "eligible":
        return _err(f"Not eligible ({result['verdict']}). Nothing was prepared.")
    _queue("provider_refills", f"Refill for approval: {result['medication']} "
           f"(prescriber {result['prescriber']})", "routine")
    return {"ok": True, "prepared": result["medication"], "prescriber": result["prescriber"],
            "status": "waiting for provider approval"}


# ---------- appointments ----------

@mcp.tool()
def get_appointments(patient_id: str) -> dict:
    """Upcoming booked appointments for a verified patient."""
    if (e := _not_verified(patient_id)):
        return e
    mine = [a for a in db.appts if a["patient_id"] == patient_id and a["status"] == "booked"
            and date.fromisoformat(a["date"]) >= db.today]
    mine.sort(key=lambda a: (a["date"], a["start"]))
    mine = [{**a, "weekday": date.fromisoformat(a["date"]).strftime("%a")} for a in mine]
    return {"ok": True, "appointments": mine}


@mcp.tool()
def find_free_slots(visit_type: str, date_from: str, date_to: str, provider: Optional[str] = None,
                    earliest_start: Optional[str] = None, latest_start: Optional[str] = None,
                    limit: int = 10) -> dict:
    """Free slots that obey hours, lunch, holidays, provider leave and the visit duration.

    visit_type: new_patient | annual_physical | follow_up | sick_visit | telehealth
    Dates are YYYY-MM-DD. provider is PATEL, OKAFOR or KIM (leave out for any).
    earliest_start / latest_start (HH:MM) filter by start time, e.g. afternoon = 13:00.
    Not patient data, so no verification needed.
    """
    if visit_type not in db.clinic["visit_types"]:
        return _err(f"visit_type must be one of {list(db.clinic['visit_types'])}")
    try:
        d1, d2 = date.fromisoformat(date_from), date.fromisoformat(date_to)
    except ValueError:
        return _err("Dates must be YYYY-MM-DD.")
    d1 = max(d1, db.today)
    slots, total = policy.find_free_slots(db.clinic, db.appts, visit_type, d1, d2, provider,
                                          earliest_start, latest_start, min(limit, 30))
    return {"ok": True, "slots": slots, "total_matching": total}


def _booking_checks(patient_id, provider, d, start, visit_type, ignore_id=None):
    if visit_type not in db.clinic["visit_types"]:
        return None, _err(f"visit_type must be one of {list(db.clinic['visit_types'])}")
    try:
        day = date.fromisoformat(d)
    except ValueError:
        return None, _err("date must be YYYY-MM-DD.")
    if day < db.today:
        return None, _err("That date is in the past.")
    duration = db.clinic["visit_types"][visit_type]
    why = policy.slot_problem(db.clinic, db.appts, provider, day, start, duration,
                              patient_id=patient_id, ignore_id=ignore_id)
    if why:
        return None, _err(why + " Use find_free_slots to get alternatives.")
    return (day, duration), None


@mcp.tool()
def book_appointment(patient_id: str, provider: str, date: str, start: str, visit_type: str) -> dict:
    """Book a slot for a verified patient. The slot is re-checked here, so a double booking,
    a holiday, lunch time or a provider on leave is refused."""
    if (e := _not_verified(patient_id)):
        return e
    ok, err = _booking_checks(patient_id, provider, date, start, visit_type)
    if err:
        return err
    day, duration = ok
    appt = {"id": db.new_appt_id(), "patient_id": patient_id, "provider": provider,
            "date": date, "start": start, "end": policy.to_hhmm(policy.to_min(start) + duration),
            "visit_type": visit_type, "status": "booked"}
    db.appts.append(appt)
    return {"ok": True, "appointment": {**appt, "weekday": day.strftime("%a")}}


@mcp.tool()
def reschedule_appointment(patient_id: str, appointment_id: str, new_date: str, new_start: str,
                           new_provider: Optional[str] = None) -> dict:
    """Move one of the patient's booked appointments. Same visit type; same provider unless
    new_provider is given. The old slot is freed only if the new one is valid."""
    if (e := _not_verified(patient_id)):
        return e
    old = next((a for a in db.appts if a["id"] == appointment_id and a["patient_id"] == patient_id
                and a["status"] == "booked"), None)
    if old is None:
        return _err(f"{appointment_id} is not a booked appointment for this patient.")
    provider = new_provider or old["provider"]
    ok, err = _booking_checks(patient_id, provider, new_date, new_start, old["visit_type"],
                              ignore_id=old["id"])
    if err:
        return err
    day, duration = ok
    old["status"] = "rescheduled"
    new = {"id": db.new_appt_id(), "patient_id": patient_id, "provider": provider,
           "date": new_date, "start": new_start,
           "end": policy.to_hhmm(policy.to_min(new_start) + duration),
           "visit_type": old["visit_type"], "status": "booked"}
    db.appts.append(new)
    return {"ok": True, "old_appointment": appointment_id,
            "new_appointment": {**new, "weekday": day.strftime("%a")}}


@mcp.tool()
def cancel_appointment(patient_id: str, appointment_id: str) -> dict:
    """Cancel one booked appointment of a verified patient."""
    if (e := _not_verified(patient_id)):
        return e
    appt = next((a for a in db.appts if a["id"] == appointment_id and a["patient_id"] == patient_id
                 and a["status"] == "booked"), None)
    if appt is None:
        return _err(f"{appointment_id} is not a booked appointment for this patient.")
    appt["status"] = "cancelled"
    return {"ok": True, "cancelled": appointment_id}


# ---------- clinic info ----------

@mcp.tool()
def get_clinic_info(check_date: Optional[str] = None) -> dict:
    """Hours, holidays, providers (with leave) and visit types. Give check_date (YYYY-MM-DD)
    to find out whether the clinic is open that day. Public info, no verification needed."""
    c = db.clinic
    out = {"ok": True, "hours": c["hours"], "holidays": c["holidays"],
           "providers": [{"id": p["id"], "name": p["name"], "role": p["role"],
                          "leave": p.get("leave", [])} for p in c["providers"]],
           "visit_types_minutes": c["visit_types"]}
    if check_date:
        try:
            d = date.fromisoformat(check_date)
        except ValueError:
            return _err("check_date must be YYYY-MM-DD.")
        if d.isoformat() in c["holidays"]:
            out["open_on_date"] = {"date": check_date, "open": False, "reason": "clinic holiday"}
        elif d.strftime("%a") not in c["hours"]["days"]:
            out["open_on_date"] = {"date": check_date, "open": False, "reason": "weekend"}
        else:
            out["open_on_date"] = {"date": check_date, "open": True}
    return out


# ---------- humans and replies ----------

@mcp.tool()
def route_to_queue(queue: str, reason: str, priority: str = "routine") -> dict:
    """Hand the current message to a human queue.

    queue: clinician_urgent | clinician_routine | provider_refills | front_desk | billing
    priority: routine | urgent | emergency
    The reason is what the human reads first, so make it short and specific.
    """
    if queue not in QUEUES:
        return _err(f"queue must be one of {QUEUES}")
    if priority not in PRIORITIES:
        return _err(f"priority must be one of {list(PRIORITIES)}")
    _queue(queue, reason, priority)
    return {"ok": True, "routed_to": queue}


@mcp.tool()
def escalate_emergency(reason: str, language: str = "en", patient_name: Optional[str] = None) -> dict:
    """Use for anything that may be a medical emergency. Alerts clinician_urgent at emergency
    priority and sets the reply. The reply is fixed (call 911 / nearest ED, clinic alerted);
    no later draft_reply can change it, so no automated clinical advice goes out.
    """
    _queue("clinician_urgent", reason, "emergency")
    lang = language if language in EMERGENCY_REPLY else "en"
    hi = (("Hola " if lang == "es" else "Hi ") + patient_name.split()[0] + ", ") if patient_name else ""
    db.drafts[db.message_id] = EMERGENCY_REPLY[lang].format(hi=hi)
    db.locked.add(db.message_id)
    return {"ok": True, "routed_to": "clinician_urgent", "reply_draft": db.drafts[db.message_id]}


@mcp.tool()
def draft_reply(text: str) -> dict:
    """Save the reply to send back to the sender (a human reviews it before it goes out).
    Write it in the patient's preferred language. Don't include medical advice or any patient
    details if the sender is not verified."""
    if db.message_id in db.locked:
        return _err("The reply for this message is fixed by escalate_emergency.")
    db.drafts[db.message_id] = text
    return {"ok": True}


@mcp.tool()
def get_run_state(message_id: str) -> dict:
    """(Harness use) What was routed and drafted for a message, so the harness can build the result."""
    return {"ok": True,
            "routed": [q for q in db.queue if q["message_id"] == message_id],
            "reply_draft": db.drafts.get(message_id)}


if __name__ == "__main__":
    mcp.run()
