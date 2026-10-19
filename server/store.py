"""In-memory copy of the clinic data. Lives only as long as the server process (one run)."""
import copy
import json
import os
from datetime import date
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "edvak_forge_data"


class Clinic:
    def __init__(self, data_dir=None):
        d = Path(data_dir or os.environ.get("CLINIC_DATA_DIR") or DEFAULT_DIR)
        load = lambda name: json.loads((d / name).read_text())
        self.clinic = load("clinic.json")
        self.patients = load("patients.json")
        self.meds = load("medications.json")
        self.appts = copy.deepcopy(load("appointments.json"))
        self.today = date.fromisoformat(self.clinic["today"])

        self.queue = []      # everything routed to a human queue
        self.drafts = {}     # message_id -> reply text
        self.locked = set()  # message_ids whose reply is fixed (emergencies)
        self._next_appt = 1

        # privacy scope, reset at the start of every message
        self.message_id = None
        self.verified = {}   # patient_id -> role ("patient" / "caregiver")

    def begin_message(self, message_id):
        self.message_id = message_id
        self.verified = {}

    def patient(self, patient_id):
        return next((p for p in self.patients if p["id"] == patient_id), None)

    def meds_for(self, patient_id):
        return [m for m in self.meds if m["patient_id"] == patient_id]

    def new_appt_id(self):
        n = self._next_appt
        self._next_appt += 1
        return f"N{n:03d}"
