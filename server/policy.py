"""Clinic rules as plain functions. No MCP, no LLM in here, so they are easy to test.

Everything the clinic policy in clinic.json says lives here. The MCP tools just call these.
"""
import re
from datetime import date, datetime, timedelta

SLOT_STARTS = (0, 20, 40)  # minutes past the hour

# brand / common names -> the generic name used in medications.json
BRANDS = {
    "xanax": "alprazolam",
    "ambien": "zolpidem",
    "adderall": "amphetamine",
    "oxycontin": "oxycodone",
    "percocet": "oxycodone",
    "zoloft": "sertraline",
    "lexapro": "escitalopram",
    "synthroid": "levothyroxine",
    "lipitor": "atorvastatin",
    "norvasc": "amlodipine",
    "prilosec": "omeprazole",
    "aricept": "donepezil",
    "coumadin": "warfarin",
    "ventolin": "albuterol",
    "glucophage": "metformin",
    "zestril": "lisinopril",
    "lantus": "insulin glargine",
}


def to_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def to_min(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def to_hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _norm_name(s):
    return re.sub(r"[^a-z ]", "", (s or "").lower()).strip()


def _norm_contact(s):
    s = (s or "").strip().lower()
    return s if "@" in s else re.sub(r"\D", "", s)


# ---------- who is this person ----------

def verify_sender(patients, name=None, contact=None, dob=None):
    """Privacy policy: name + DOB match, OR the message came from the phone/email on file.

    A caregiver counts if their phone is the one listed on the patient record.
    Returns the patient record plus how we verified, or None.
    """
    n = _norm_name(name)
    c = _norm_contact(contact)

    if c:
        for p in patients:
            cg = p.get("authorised_caregiver")
            on_file = {_norm_contact(p["phone"]), _norm_contact(p["email"])}
            cg_phone = _norm_contact(cg["phone"]) if cg else None
            if c in on_file or c == cg_phone:
                is_caregiver = bool(cg) and n == _norm_name(cg["name"])
                if c == cg_phone and c not in on_file:
                    is_caregiver = True
                return {
                    "patient": p,
                    "role": "caregiver" if is_caregiver else "patient",
                    "method": "contact_on_file",
                    "name_matches": n == _norm_name(cg["name"] if is_caregiver else p["name"]),
                }

    if n and dob:
        for p in patients:
            if _norm_name(p["name"]) == n and p["dob"] == dob:
                return {"patient": p, "role": "patient", "method": "name_and_dob", "name_matches": True}

    return None


# ---------- refills ----------

def match_medication(meds, query):
    q = (query or "").lower()
    for brand, generic in BRANDS.items():
        if brand in q:
            q += " " + generic
    for m in meds:
        generic_name = m["medication"].lower().split()[0]
        if generic_name in q or m["medication"].lower() in q:
            return m
    return None


def twelve_months_ago(today):
    try:
        return today.replace(year=today.year - 1)
    except ValueError:  # 29 Feb
        return today.replace(year=today.year - 1, day=28)


def refill_eligibility(patient, meds, query, today):
    """Walk through clinic.json refill_policy and say what should happen.

    verdict is the first blocker found, in the order the policy cares about:
    controlled > not seen in 12 months > zero refills > too early.
    """
    med = match_medication(meds, query)
    if med is None:
        return {"verdict": "not_on_list", "medication": query,
                "detail": "Not on the patient's active medication list."}

    supply_end = to_date(med["last_filled"]) + timedelta(days=med["days_supply"])
    earliest = supply_end - timedelta(days=7)
    last_visit = to_date(patient["last_visit"])

    blockers = []
    if med["controlled_schedule"]:
        blockers.append("controlled_substance")
    if last_visit < twelve_months_ago(today):
        blockers.append("not_seen_12_months")
    if med["refills_remaining"] <= 0:
        blockers.append("zero_refills")
    if today < earliest:
        blockers.append("too_early")

    return {
        "verdict": blockers[0] if blockers else "eligible",
        "all_blockers": blockers,
        "medication": med["medication"],
        "prescriber": med["prescriber"],
        "controlled_schedule": med["controlled_schedule"],
        "refills_remaining": med["refills_remaining"],
        "last_visit": patient["last_visit"],
        "supply_runs_out": supply_end.isoformat(),
        "earliest_refill_date": earliest.isoformat(),
    }


# ---------- appointments ----------

def on_leave(provider, d):
    for start, end in provider.get("leave", []):
        if to_date(start) <= d <= to_date(end):
            return True
    return False


def slot_problem(clinic, appts, provider_id, d, start, duration, patient_id=None, ignore_id=None):
    """Return a reason string if this slot cannot be booked, else None."""
    provider = next((p for p in clinic["providers"] if p["id"] == provider_id), None)
    if provider is None:
        return f"Unknown provider {provider_id}."
    if d.strftime("%a") not in clinic["hours"]["days"]:
        return f"Clinic is closed on {d.strftime('%A')}s."
    if d.isoformat() in clinic["holidays"]:
        return f"{d.isoformat()} is a clinic holiday."
    if on_leave(provider, d):
        return f"{provider['name']} is on leave on {d.isoformat()}."

    s = to_min(start)
    e = s + duration
    if s % 60 not in SLOT_STARTS:
        return "Appointments start on the hour or at :20 / :40."
    if s < to_min(clinic["hours"]["open"]) or e > to_min(clinic["hours"]["close"]):
        return "Outside clinic hours (09:00-17:00)."
    lunch_s, lunch_e = (to_min(x) for x in clinic["hours"]["lunch"])
    if s < lunch_e and e > lunch_s:
        return "That overlaps the lunch break (12:00-13:00)."

    for a in appts:
        if a["status"] != "booked" or a["id"] == ignore_id or a["date"] != d.isoformat():
            continue
        overlaps = s < to_min(a["end"]) and e > to_min(a["start"])
        if overlaps and a["provider"] == provider_id:
            return f"{provider['name']} is already booked at {a['start']}-{a['end']}."
        if overlaps and patient_id and a["patient_id"] == patient_id:
            return f"The patient already has an appointment at {a['start']}-{a['end']}."
    return None


def find_free_slots(clinic, appts, visit_type, date_from, date_to, provider_id=None,
                    earliest_start=None, latest_start=None, limit=10):
    duration = clinic["visit_types"][visit_type]
    providers = [provider_id] if provider_id else [p["id"] for p in clinic["providers"]]
    lo = to_min(earliest_start) if earliest_start else 0
    hi = to_min(latest_start) if latest_start else 24 * 60

    found = []
    d = date_from
    while d <= date_to:
        for minute in range(to_min(clinic["hours"]["open"]), to_min(clinic["hours"]["close"]), 20):
            if not lo <= minute <= hi:
                continue
            for pid in providers:
                if slot_problem(clinic, appts, pid, d, to_hhmm(minute), duration) is None:
                    found.append({"provider": pid, "date": d.isoformat(), "weekday": d.strftime("%a"),
                                  "start": to_hhmm(minute), "end": to_hhmm(minute + duration),
                                  "visit_type": visit_type})
        d += timedelta(days=1)
    return found[:limit], len(found)
