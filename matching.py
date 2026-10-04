"""
Instant tutor matching - rule based, no database access (easy to test).

Stage 1  HARD FILTERS  - a tutor who fails any of these is never offered:
    subject   tutor teaches the subject (or its whole parent subject)
    grade     tutee's grade level is inside the tutor's grade range
    schedule  at least one shared day with >= 60 minutes of overlapping time, counted only
              inside the tutor's FREE time (availability minus the time already promised
              to the tutor's other open tutees, so two tutees never clash)
    modality  not "online only" vs "in-person only"
    budget    tutor's rate within 20% above the tutee's budget
    distance  in-person sessions must be within 25 km

Stage 2  WMCS (Weighted Multi-Criteria Scoring), 0-100, highest wins:
    subject 25 | grade 15 | budget 15 | schedule 15 | location 10 | modality 10 | reliability 10
    reliability = 40% how often the tutor says yes + 40% tutee star ratings + 20% recent activity
Each criterion yields a 0..1 ratio that is multiplied by its weight; the weights add up to 100.
"""
import math
import re
import unicodedata
from datetime import datetime, timezone

from catalog import LOCATIONS, COLLEGE, split_subject

WEIGHTS = {"subject": 25, "grade": 15, "budget": 15, "schedule": 15,
           "location": 10, "modality": 10, "reliability": 10}
assert sum(WEIGHTS.values()) == 100

BUDGET_TOLERANCE = 0.20     # tutor may cost up to 20% above the tutee's budget
MAX_INPERSON_KM = 25.0      # farthest an in-person session may be
NEAR_KM = 5.0               # within this distance counts as "same area"
MIN_SESSION_MIN = 60        # shortest useful shared time slot
TARGET_SESSION_MIN = 120    # overlap this long (or the tutee's whole window) scores full marks
RATING_PRIOR_MEAN = 3.5     # a tutor with no ratings starts here ...
RATING_PRIOR_WEIGHT = 3     # ... worth this many "virtual" ratings, so 1 vote cannot swing the score


# ---------------------------------------------------------------- helpers
def normalize(text):
    """Lowercase and strip accents so 'Dasmariñas' == 'dasmarinas'."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip().lower()


_COORDS = {normalize(name): xy for name, xy in LOCATIONS.items()}


def _km(a, b):
    """Haversine distance in km (accurate enough at city scale)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def _distance_km(loc_a, loc_b):
    a, b = normalize(loc_a), normalize(loc_b)
    if not a or not b:
        return None
    if a == b:
        return 0.0
    ca, cb = _COORDS.get(a), _COORDS.get(b)
    return _km(ca, cb) if ca and cb else None


def _modality(value):
    v = normalize(value).replace(" ", "-")
    if v in ("in-person", "inperson", "face-to-face", "onsite"):
        return "in-person"
    return v if v in ("online", "both") else "both"


def _minutes(hhmm):
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


def _recency(last_seen):
    if not last_seen:
        return 0.2
    try:
        seen = datetime.strptime(str(last_seen)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.2
    hours = (datetime.now(timezone.utc) - seen).total_seconds() / 3600
    return 1.0 if hours <= 24 else 0.5 if hours <= 24 * 7 else 0.2


def _hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _rating_ratio(total_stars, n):
    """Smoothed average star rating mapped from 1..5 to 0..1."""
    avg = (total_stars + RATING_PRIOR_MEAN * RATING_PRIOR_WEIGHT) / (n + RATING_PRIOR_WEIGHT)
    return (avg - 1) / 4


def free_slots(tutor_slots, booked_slots):
    """
    Tutor availability minus the time already promised to other tutees.
    Returns one slot per day / free interval, in the shape schedule_ratio expects.
    Pieces shorter than MIN_SESSION_MIN are dropped later by schedule_ratio.
    """
    booked = {}
    for b in booked_slots or []:
        try:
            interval = (_minutes(b["start"]), _minutes(b["end"]))
        except (KeyError, ValueError):
            continue
        for day in b.get("days", []):
            booked.setdefault(day, []).append(interval)
    if not booked:
        return tutor_slots or []

    free = []
    for s in tutor_slots or []:
        s0, s1 = _minutes(s["start"]), _minutes(s["end"])
        for day in s["days"]:
            pieces = [(s0, s1)]
            for b0, b1 in sorted(booked.get(day, [])):
                cut = []
                for p0, p1 in pieces:
                    if b1 <= p0 or b0 >= p1:          # no overlap
                        cut.append((p0, p1))
                        continue
                    if p0 < b0:
                        cut.append((p0, b0))
                    if b1 < p1:
                        cut.append((b1, p1))
                pieces = cut
            free += [{"days": [day], "start": _hhmm(a), "end": _hhmm(b)} for a, b in pieces if b > a]
    return free


# ---------------------------------------------------------------- criteria
def subject_match(wanted, offered):
    """
    1.0  tutor teaches exactly this subject / branch
    0.9  tutee wants a branch (Physics) and the tutor teaches the whole subject (Science)
    0.8  tutee wants the whole subject and the tutor teaches one branch of it
    0.0  no match (different subject, or different branches)
    """
    wanted_parent, wanted_branch = split_subject(wanted)
    best = 0.0
    for o in offered or []:
        if o == wanted:
            return 1.0
        parent, branch = split_subject(o)
        if parent != wanted_parent:
            continue
        if branch is None:
            best = max(best, 0.9)
        elif wanted_branch is None:
            best = max(best, 0.8)
    return best


def grade_ratio(grade, lo, hi):
    """None if the grade is outside the tutor's range. Specialists (narrow range) score higher."""
    if not (lo <= grade <= hi):
        return None
    span = hi - lo + 1
    return 1 - 0.5 * (span - 1) / (COLLEGE - 1)


def schedule_ratio(tutee_slots, tutor_slots):
    """
    A slot is {"days": ["mon", ...], "start": "16:00", "end": "18:00"}.
    For every day the tutee wants, find the tutor slot with the longest overlap on that day.
    A day counts when the overlap is >= MIN_SESSION_MIN.  None if no day works.
    ratio = 0.6 * share of the tutee's days covered + 0.4 * how fully the time window is covered.
    """
    best = None
    for ts in tutee_slots or []:
        t0, t1 = _minutes(ts["start"]), _minutes(ts["end"])
        if t1 - t0 < MIN_SESSION_MIN:
            continue
        target = min(t1 - t0, TARGET_SESSION_MIN)
        covered = []
        for day in ts["days"]:
            longest = 0
            for rs in tutor_slots or []:
                if day in rs["days"]:
                    overlap = min(t1, _minutes(rs["end"])) - max(t0, _minutes(rs["start"]))
                    longest = max(longest, overlap)
            if longest >= MIN_SESSION_MIN:
                covered.append(min(1.0, longest / target))
        if covered:
            ratio = (0.6 * len(covered) / len(ts["days"])
                     + 0.4 * sum(covered) / len(covered))
            best = ratio if best is None else max(best, ratio)
    return best


# ---------------------------------------------------------------- scoring
def score_pair(tutee, tutor):
    """Return (score, breakdown) or None if the tutor fails a hard filter."""
    W = WEIGHTS

    # 1. Subject (hard)
    sub = subject_match(tutee.get("subject"), tutor.get("subjects"))
    if sub <= 0:
        return None

    # 2. Grade level (hard)
    try:
        grade = int(tutee.get("grade"))
        g = grade_ratio(grade, int(tutor.get("grade_min")), int(tutor.get("grade_max")))
    except (TypeError, ValueError):
        return None
    if g is None:
        return None

    # 3. Schedule (hard)
    sched = schedule_ratio(tutee.get("schedule"), free_slots(tutor.get("schedule"), tutor.get("booked")))
    if sched is None:
        return None

    # 4. Modality (hard)
    m1, m2 = _modality(tutee.get("modality")), _modality(tutor.get("modality"))
    if m1 != m2 and "both" not in (m1, m2):
        return None
    modality = 1.0 if m1 == m2 else 0.8
    must_meet = "in-person" in (m1, m2)

    # 5. Budget (hard beyond tolerance)
    try:
        budget = max(float(tutee.get("budget") or 0), 0.0)
        rate = max(float(tutor.get("rate") or 0), 0.0)
    except (TypeError, ValueError):
        return None
    if rate <= budget:
        budget_ratio = 1.0
    elif budget > 0 and rate <= budget * (1 + BUDGET_TOLERANCE):
        budget_ratio = 1 - 0.5 * (rate - budget) / (budget * BUDGET_TOLERANCE)
    else:
        return None

    # 6. Location (hard only when the session must be in person)
    dist = _distance_km(tutee.get("location"), tutor.get("location"))
    if dist is not None and dist <= NEAR_KM:
        location = 1.0
    elif dist is not None and dist <= MAX_INPERSON_KM:
        location = 1 - 0.7 * (dist - NEAR_KM) / (MAX_INPERSON_KM - NEAR_KM)
    elif must_meet:
        return None
    else:
        location = 0.0
    if not must_meet:
        location = max(location, 0.6)       # distance barely matters online

    # 7. Reliability: how often the tutor says yes + tutee star ratings + recent activity
    accepted = int(tutor.get("accepted_n") or 0)
    ignored = int(tutor.get("declined_n") or 0) + int(tutor.get("expired_n") or 0)
    accept_rate = (accepted + 2) / (accepted + ignored + 3)      # smoothed; new tutors start ~0.67
    stars = _rating_ratio(float(tutor.get("rating_sum") or 0), int(tutor.get("rating_n") or 0))
    reliability = 0.4 * accept_rate + 0.4 * stars + 0.2 * _recency(tutor.get("last_seen"))

    breakdown = {
        "subject": W["subject"] * sub, "grade": W["grade"] * g,
        "budget": W["budget"] * budget_ratio, "schedule": W["schedule"] * sched,
        "location": W["location"] * location, "modality": W["modality"] * modality,
        "reliability": W["reliability"] * reliability,
    }
    breakdown = {k: round(v, 1) for k, v in breakdown.items()}
    return round(sum(breakdown.values()), 1), breakdown


def rank_tutors(tutee, tutors):
    """All eligible tutors, best first. Ties: emptier schedule, reliability, cheaper, id."""
    ranked = []
    for tutor in tutors:
        result = score_pair(tutee, tutor)
        if result:
            ranked.append((tutor, *result))

    def load(t):
        return int(t.get("open_n") or 0) / max(int(t.get("max_tutees") or 1), 1)

    ranked.sort(key=lambda r: (-r[1], load(r[0]), -r[2]["reliability"],
                               float(r[0].get("rate") or 0), r[0].get("user_id", 0)))
    return ranked


def find_best_match(tutee, tutors):
    """(tutor, score, breakdown) for the single best tutor, or (None, 0.0, None)."""
    ranked = rank_tutors(tutee, tutors)
    return ranked[0] if ranked else (None, 0.0, None)
