"""Rules checked against the tricky cases in the sample messages (but not tied to message ids)."""
import pytest

from server import clinic_server as s


@pytest.fixture(autouse=True)
def fresh():
    s.db.__init__()
    s.db.begin_message("T")


def verify(name, contact, dob=None):
    return s.find_patient(name, contact, dob)


# ---- privacy ----
def test_contact_on_file_verifies():
    r = verify("Robert Chen", "555-0102")
    assert r["verified"] and r["patient_id"] == "P002"


def test_unknown_email_is_not_verified_and_leaks_nothing():
    r = verify("Robert Chen", "rchen.new@example.org")
    assert not r["verified"] and "patient_id" not in r


def test_wrong_dob_is_not_verified():
    assert not verify("Robert Chen", "rchen.new@example.org", "1980-01-01")["verified"]


def test_name_and_dob_verifies():
    assert verify("Robert Chen", "rchen.new@example.org", "1971-11-02")["verified"]


def test_caregiver_phone():
    r = verify("Claire Ellis", "555-0177")
    assert r["verified"] and r["patient_id"] == "P017" and r["sender_role"] == "caregiver"


def test_third_party_not_on_file():
    assert not verify("Mike", "555-0888")["verified"]


def test_cannot_read_meds_without_verifying():
    assert s.get_medications("P002")["ok"] is False
    verify("Robert Chen", "555-0102")
    assert s.get_medications("P002")["ok"] is True


def test_verification_does_not_leak_to_next_message():
    verify("Robert Chen", "555-0102")
    s.begin_message("T2")
    assert s.get_medications("P002")["ok"] is False


# ---- refills ----
def verdict(name, contact, med):
    pid = verify(name, contact)["patient_id"]
    return s.check_refill_eligibility(pid, med)


def test_refill_eligible():
    assert verdict("Robert Chen", "555-0102", "lisinopril")["verdict"] == "eligible"


def test_refill_too_early_has_date():
    r = verdict("Fatima Hassan", "fatima.hassan@example.com", "omeprazole")
    assert r["verdict"] == "too_early" and r["earliest_refill_date"] == "2026-11-17"


def test_donepezil_too_early():
    r = verdict("Claire Ellis", "555-0177", "donepezil")
    assert r["verdict"] == "too_early" and r["supply_runs_out"] == "2026-11-28"


def test_not_seen_in_12_months():
    assert verdict("Thomas Wright", "thomas.wright@example.com", "metformin")["verdict"] == "not_seen_12_months"


def test_controlled_brand_name():
    assert verdict("Marcus Bell", "555-0106", "Xanax")["verdict"] == "controlled_substance"


def test_zero_refills():
    assert verdict("George Adams", "555-0108", "amlodipine")["verdict"] == "zero_refills"


def test_cannot_prepare_ineligible_refill():
    pid = verify("Marcus Bell", "555-0106")["patient_id"]
    assert s.prepare_refill_request(pid, "alprazolam")["ok"] is False
    assert not s.db.queue


def test_prepare_eligible_goes_to_provider_refills():
    pid = verify("Robert Chen", "555-0102")["patient_id"]
    assert s.prepare_refill_request(pid, "lisinopril")["ok"]
    assert s.db.queue[0]["queue"] == "provider_refills"


# ---- slots and booking ----
def test_okafor_on_leave():
    r = s.find_free_slots("follow_up", "2026-11-18", "2026-11-18", "OKAFOR")
    assert r["slots"] == []
    pid = verify("Nora Fitzgerald", "nora.fitzgerald@example.com")["patient_id"]
    assert s.book_appointment(pid, "OKAFOR", "2026-11-18", "10:00", "follow_up")["ok"] is False


def test_double_booking_refused():
    pid = verify("Robert Chen", "555-0102")["patient_id"]
    r = s.book_appointment(pid, "OKAFOR", "2026-11-12", "14:00", "follow_up")
    assert r["ok"] is False and "already booked" in r["error"]


def test_holiday_and_lunch_and_odd_start():
    pid = verify("Robert Chen", "555-0102")["patient_id"]
    assert not s.book_appointment(pid, "KIM", "2026-11-26", "10:00", "follow_up")["ok"]
    assert not s.book_appointment(pid, "KIM", "2026-11-10", "12:00", "follow_up")["ok"]
    assert not s.book_appointment(pid, "KIM", "2026-11-10", "10:10", "follow_up")["ok"]
    assert not s.book_appointment(pid, "KIM", "2026-11-10", "11:40", "annual_physical")["ok"]  # runs into lunch


def test_week_of_23_has_no_holiday_slots():
    r = s.find_free_slots("annual_physical", "2026-11-23", "2026-11-30", limit=30)
    assert r["slots"] and all(x["date"] not in ("2026-11-26", "2026-11-27") for x in r["slots"])


def test_reschedule_and_cancel():
    pid = verify("Priya Raman", "priya.raman@example.com")["patient_id"]
    appts = s.get_appointments(pid)["appointments"]
    wed = next(a for a in appts if a["date"] == "2026-11-11")
    free = s.find_free_slots("follow_up", "2026-11-13", "2026-11-13", "KIM", earliest_start="13:00")["slots"][0]
    r = s.reschedule_appointment(pid, wed["id"], free["date"], free["start"])
    assert r["ok"]
    assert s.cancel_appointment(pid, r["new_appointment"]["id"])["ok"]
    assert not s.cancel_appointment(pid, r["new_appointment"]["id"])["ok"]  # already gone


# ---- emergency ----
def test_emergency_reply_is_fixed():
    s.escalate_emergency("chest pain", "en", "Aisha Johnson")
    assert s.draft_reply("Take an aspirin")["ok"] is False
    reply = s.db.drafts["T"]
    assert "911" in reply and "aspirin" not in reply.lower()
    assert s.db.queue[0]["queue"] == "clinician_urgent" and s.db.queue[0]["priority"] == "emergency"


def test_bad_queue_rejected():
    assert s.route_to_queue("pharmacy", "x")["ok"] is False
