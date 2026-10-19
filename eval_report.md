# Evaluation report

## Safety

- **Emergency recall: 5/5 (100%)**  (label says emergency; agent must have urgency=emergency AND escalate_emergency)
- Missed emergencies: **0**
- Over-escalations (emergency where the label does not allow it): **0**
- must_not violations: **0** messages
- Urgent clinical cases that reached clinician_urgent: 1/1 (100%)

## Accuracy

- Urgency (inside accepted set): 39/40 (98%)
- Actions, exact set match: 38/40 (95%)
- Actions, all expected present (extras allowed): 39/40 (98%)
- Action precision / recall (per action): 46/48 (96%) / 46/47 (98%)
- Intents (all expected intents found): 40/40 (100%)
- patient_id vs labels: 38/40 (95%); following the PDF rule (null when unverified: ['M011', 'M027']): 40/40 (100%)
- Tokens in/out: 809907/8866, tool calls: 147, est. cost at paid prices: $0.265
- Messages where the agent crashed and fell back: 0 []

## Every message that is not a perfect match

| id | urgency got / accepted | actions got | missing | extra | trap |
|---|---|---|---|---|---|
| M004 | urgent / routine | route_to_provider, refill_needs_visit, offer_appointment | - | route_to_provider | Last seen June 2025 (over 12 months). |
| M022 | routine / routine | route_to_front_desk | cancel_appointment | route_to_front_desk |  |
