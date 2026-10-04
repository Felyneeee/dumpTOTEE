"""
Shared reference data used by the API, the matcher and the web page.
Edit the lists here (e.g. add a subject or a branch) and everything follows.

Subject ids:  "science"          -> the whole subject (any branch)
              "science:physics"  -> one branch
"""

SUBJECTS = [
    {"id": "mathematics", "label": "Mathematics", "branches": [
        ("arithmetic", "Arithmetic"), ("algebra", "Algebra"), ("geometry", "Geometry"),
        ("trigonometry", "Trigonometry"), ("statistics", "Statistics & Probability"),
        ("calculus", "Calculus")]},
    {"id": "english", "label": "English", "branches": [
        ("grammar", "Grammar"), ("reading", "Reading Comprehension"),
        ("writing", "Writing & Composition"), ("literature", "Literature"),
        ("speaking", "Speaking & Conversation")]},
    {"id": "science", "label": "Science", "branches": [
        ("general", "General Science"), ("biology", "Biology"), ("chemistry", "Chemistry"),
        ("physics", "Physics"), ("earth", "Earth Science")]},
    {"id": "history", "label": "History", "branches": [
        ("philippine", "Philippine History"), ("world", "World History"),
        ("asian", "Asian History"), ("araling-panlipunan", "Araling Panlipunan")]},
    {"id": "filipino", "label": "Filipino", "branches": [
        ("balarila", "Balarila (Grammar)"), ("panitikan", "Panitikan (Literature)"),
        ("pagbasa-pagsulat", "Pagbasa at Pagsulat")]},
    {"id": "ict", "label": "Computer / ICT", "branches": [
        ("programming", "Programming"), ("web", "Web Development"),
        ("office", "Office & Spreadsheets")]},
]

# 1-12 are school grades, 13 stands for college (all years).
COLLEGE = 13
GRADE_GROUPS = [
    ("Elementary", range(1, 7)),
    ("Junior High School", range(7, 11)),
    ("Senior High School", range(11, 13)),
    ("College", range(13, 14)),
]


def grade_label(g):
    return "College" if g == COLLEGE else f"Grade {g}"


def grade_range_text(lo, hi):
    if lo is None or hi is None:
        return ""
    return grade_label(lo) if lo == hi else f"{grade_label(lo)} - {grade_label(hi)}"


DAYS = [("mon", "Mon"), ("tue", "Tue"), ("wed", "Wed"), ("thu", "Thu"),
        ("fri", "Fri"), ("sat", "Sat"), ("sun", "Sun")]
DAY_IDS = [d for d, _ in DAYS]
DAY_LABEL = dict(DAYS)

MODALITIES = {"online": "Online", "in-person": "In-Person", "both": "Both"}

# Password recovery: the user picks one of these and sets an answer (stored hashed).
SECURITY_QUESTIONS = [
    ("pet", "What was the name of your first pet?"),
    ("school", "What was the name of your elementary school?"),
    ("street", "What street did you grow up on?"),
    ("nickname", "What was your childhood nickname?"),
    ("city", "In what city or town was your mother born?"),
    ("teacher", "What was the last name of your favorite teacher?"),
]
SECURITY_QUESTION_TEXT = dict(SECURITY_QUESTIONS)

# Cavite cities / municipalities with coordinates (used for distance scoring).
LOCATIONS = {
    "Bacoor": (14.4608, 120.9631), "Cavite City": (14.4831, 120.8986),
    "Dasmariñas": (14.3294, 120.9367), "General Trias": (14.3861, 120.8806),
    "Imus": (14.4297, 120.9367), "Tagaytay": (14.1153, 120.9621),
    "Trece Martires": (14.2828, 120.8672), "Alfonso": (14.1378, 120.8542),
    "Amadeo": (14.1700, 120.9239), "Carmona": (14.3160, 121.0583),
    "General Emilio Aguinaldo": (14.1842, 120.7933), "Indang": (14.1953, 120.8769),
    "Kawit": (14.4442, 120.9025), "Maragondon": (14.2764, 120.7375),
    "Mendez": (14.1297, 120.9078), "Naic": (14.3181, 120.7675),
    "Noveleta": (14.4278, 120.8783), "Rosario": (14.4150, 120.8586),
    "Silang": (14.2289, 120.9744), "Tanza": (14.3589, 120.8528),
    "Ternate": (14.2881, 120.7183),
}

# ---------------------------------------------------------------- subject helpers
_LABELS = {}
for _s in SUBJECTS:
    _LABELS[_s["id"]] = _s["label"]
    for _bid, _blabel in _s["branches"]:
        _LABELS[f'{_s["id"]}:{_bid}'] = f'{_s["label"]} - {_blabel}'


def valid_subject(sid):
    return sid in _LABELS


def subject_label(sid):
    return _LABELS.get(sid, sid)


def split_subject(sid):
    """'science:physics' -> ('science', 'physics');  'science' -> ('science', None)"""
    parent, _, branch = str(sid).partition(":")
    return parent, (branch or None)


def subjects_text(ids):
    return ", ".join(subject_label(i) for i in ids)


def public_catalog():
    """JSON-friendly version for the web page."""
    return {
        "subjects": [{"id": s["id"], "label": s["label"],
                      "branches": [{"id": b, "label": l} for b, l in s["branches"]]}
                     for s in SUBJECTS],
        "grade_groups": [{"label": name, "grades": [{"id": g, "label": grade_label(g)} for g in rng]}
                         for name, rng in GRADE_GROUPS],
        "days": [{"id": d, "label": l} for d, l in DAYS],
        "locations": list(LOCATIONS),
        "modalities": [{"id": k, "label": v} for k, v in MODALITIES.items()],
        "security_questions": [{"id": k, "label": v} for k, v in SECURITY_QUESTIONS],
    }


# ---------------------------------------------------------------- schedule helpers
def schedule_text(slots):
    parts = []
    for s in slots or []:
        days = ", ".join(DAY_LABEL.get(d, d) for d in s["days"])
        parts.append(f'{days} {s["start"]}-{s["end"]}')
    return "; ".join(parts)
