"""Keyword safety net. Plain regex, no LLM, runs before and after the model.

It can only push a message UP (towards a human). It never downgrades what the model decided.
- hard hit: looks like a medical emergency -> must be escalated, always.
- soft hit: worrying but not an emergency (low mood etc.) -> a clinician must see it today.

I would rather over-escalate a false alarm than miss a real one. Patterns are kept specific
(e.g. "confused" alone does not match, "dying to get in" does not match) so a human
isn't flooded. English plus a few Spanish phrases. Paraphrases it doesn't know are left to the model.
"""
import re

HARD = {
    "chest_pain": r"chest (pain|pressure|tightness)|pain in my chest|dolor (de|en el) pecho",
    "breathing": r"(can'?t|cannot|unable to|trouble|difficulty|hard to) (breathe|breathing|catch my breath)"
                 r"|short(ness)? of breath|no puedo respirar|me falta el aire",
    "cant_speak": r"can'?t (finish|speak|talk)\b.{0,15}(sentence|word|full)|can'?t (finish|get out) (a |my )?sentence",
    "inhaler_failing": r"inhaler (isn'?t|is not|not|doesn'?t|won'?t) (help|work)",
    "stroke": r"face (is )?droop|slurred speech|sudden(ly)? (weak|numb)|can'?t (move|lift|feel) (my|his|her) (arm|leg|face)"
              r"|worst headache",
    "unresponsive": r"unconscious|unresponsive|passed out|collapsed|not waking|won'?t wake|seizure|convuls",
    "overdose": r"overdos|took too (many|much)|swallowed (a lot|too)|poison",
    "allergic": r"throat (is )?(closing|swelling)|swollen (tongue|throat)|anaphyla|can'?t swallow",
    "bleeding": r"(dark|black|tarry) (stool|stools|poop)|blood in (my |the )?(stool|vomit|urine)|"
                r"vomiting blood|coughing (up )?blood|bleeding.{0,40}(won'?t stop|heavily|a lot|heavy)",
    "self_harm": r"kill myself|suicid|end my life|want to die|don'?t want to (live|be here)|hurt myself|self[- ]harm"
                 r"|better off (dead|without me)",
}

SOFT = {
    "low_mood": r"(feel(ing)?|been|am) (really |very |so |extremely )?(low|depressed|hopeless|down)\b"
                r"|don'?t see the point|no point (in|to)|can'?t go on|hopeless",
}

_hard = {k: re.compile(v, re.I) for k, v in HARD.items()}
_soft = {k: re.compile(v, re.I) for k, v in SOFT.items()}

_HEAD = re.compile(r"(hit|bump(ed)?|struck|banged|knocked).{0,25}head|head injury|fell.{0,40}head", re.I)
_CONFUSED = re.compile(r"confus|disoriented|drowsy|not making sense|vomit", re.I)
_SUGAR = re.compile(r"(blood sugar|glucose|sugar reading)[^.\d]{0,30}(\d{2,3})", re.I)
_LOW_SIGNS = re.compile(r"shaky|sweaty|sweating|confus|faint|dizzy", re.I)


def scan(text):
    """Return {"hard": [...], "soft": [...]} with the names of the patterns that matched."""
    hard = [k for k, rx in _hard.items() if rx.search(text)]
    soft = [k for k, rx in _soft.items() if rx.search(text)]

    if _HEAD.search(text) and _CONFUSED.search(text):
        hard.append("head_injury_confusion")

    m = _SUGAR.search(text)
    if m:
        value = int(m.group(2))
        if value < 54 or (value < 70 and _LOW_SIGNS.search(text)) or value > 400:
            hard.append("blood_sugar")

    return {"hard": hard, "soft": soft}
