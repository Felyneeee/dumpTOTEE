"""
Instant tutor matching.

Two stages:
  1. HARD FILTERS - a tutor who fails any of these is never offered:
       subject overlap, schedule overlap, compatible modality,
       budget within tolerance, reachable if the session must be in-person.
  2. WEIGHTED SCORE (0-100) among the survivors, highest wins:
       subject 30 | budget 20 | schedule 15 | location 15 | modality 10 | reliability 10

Everything here is pure (no database access) so it can be tested on its own.
"""
import math
import re
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher

try:
    from geopy.distance import geodesic

    def _km(a, b):
        return geodesic(a, b).kilometers
except ImportError:  # geopy not installed: haversine is accurate enough at city scale
    def _km(a, b):
        lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
        h = (math.sin((lat2 - lat1) / 2) ** 2
             + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
        return 2 * 6371.0088 * math.asin(math.sqrt(h))

WEIGHTS = {"subject": 30, "budget": 20, "schedule": 15,
           "location": 15, "modality": 10, "reliability": 10}

SUBJECT_MIN_SIMILARITY = 0.80   # below this the subjects are treated as different
BUDGET_TOLERANCE = 0.20         # tutor may cost up to 20% above the tutee's budget
MAX_INPERSON_KM = 25.0          # farthest an in-person session may be
NEAR_KM = 5.0                   # within this distance counts as "same area"

CAVITE_COORDINATES = {
    'bacoor': (14.4608, 120.9631), 'cavite city': (14.4831, 120.8986),
    'dasmarinas': (14.3294, 120.9367), 'general trias': (14.3861, 120.8806),
    'imus': (14.4297, 120.9367), 'tagaytay': (14.1153, 120.9621),
    'trece martires': (14.2828, 120.8672), 'alfonso': (14.1378, 120.8542),
    'amadeo': (14.1700, 120.9239), 'carmona': (14.3160, 121.0583),
    'general emilio aguinaldo': (14.1842, 120.7933), 'indang': (14.1953, 120.8769),
    'kawit': (14.4442, 120.9025), 'maragondon': (14.2764, 120.7375),
    'mendez': (14.1297, 120.9078), 'naic': (14.3181, 120.7675),
    'noveleta': (14.4278, 120.8783), 'rosario': (14.4150, 120.8586),
    'silang': (14.2289, 120.9744), 'tanza': (14.3589, 120.8528),
    'ternate': (14.2881, 120.7183),
}


# ---------------------------------------------------------------- helpers
def normalize(text):
    """Lowercase and strip accents so 'Dasmariñas' == 'dasmarinas'."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip().lower()


def _modality(value):
    v = normalize(value).replace(" ", "-")
    if v in ("in-person", "inperson", "face-to-face", "onsite"):
        return "in-person"
    return v if v in ("online", "both") else "both"


def _subject_tokens(text):
    parts = re.split(r"[,/;&+]|\band\b", normalize(text))
    return [p.strip() for p in parts if p.strip()]


def subject_similarity(a, b):
    """0..1. Handles 'Math' vs 'Mathematics' and multi-subject lists like 'Math, Physics'."""
    best = 0.0
    for x in _subject_tokens(a):
        for y in _subject_tokens(b):
            if x == y:
                return 1.0
            short, long_ = sorted((x, y), key=len)
            if len(short) >= 3 and (long_.startswith(short) or short in long_.split()):
                sim = 0.9
            else:
                sim = SequenceMatcher(None, x, y).ratio()
            best = max(best, sim)
    return best


def _distance_km(loc_a, loc_b):
    a, b = normalize(loc_a), normalize(loc_b)
    if not a or not b:
        return None
    if a == b:
        return 0.0
    ca, cb = CAVITE_COORDINATES.get(a), CAVITE_COORDINATES.get(b)
    return _km(ca, cb) if ca and cb else None


def _recency(last_seen):
    if not last_seen:
        return 0.2
    try:
        seen = datetime.strptime(str(last_seen)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.2
    hours = (datetime.now(timezone.utc) - seen).total_seconds() / 3600
    return 1.0 if hours <= 24 else 0.5 if hours <= 24 * 7 else 0.2


# ---------------------------------------------------------------- scoring
def score_pair(tutee, tutor):
    """Return (score, breakdown) or None if the tutor fails a hard filter."""
    W = WEIGHTS

    # 1. Subject (hard)
    sim = subject_similarity(tutee.get("subject"), tutor.get("subject"))
    if sim < SUBJECT_MIN_SIMILARITY:
        return None
    subject = W["subject"] * sim

    # 2. Schedule (hard)
    s1, s2 = normalize(tutee.get("schedule")), normalize(tutor.get("schedule"))
    if s1 == s2:
        schedule = W["schedule"]
    elif "flexible" in (s1, s2):
        schedule = W["schedule"] * 0.8
    else:
        return None

    # 3. Modality (hard)
    m1, m2 = _modality(tutee.get("modality")), _modality(tutor.get("modality"))
    if m1 != m2 and "both" not in (m1, m2):
        return None  # one wants online only, the other in-person only
    modality = W["modality"] if m1 == m2 else W["modality"] * 0.8
    must_meet = "in-person" in (m1, m2)  # nobody can do online -> must be physically close

    # 4. Budget (hard beyond tolerance)
    try:
        budget = max(float(tutee.get("budget") or 0), 0.0)
        rate = max(float(tutor.get("rate") or 0), 0.0)
    except (TypeError, ValueError):
        return None
    if rate <= budget:
        budget_pts = W["budget"]
    elif budget > 0 and rate <= budget * (1 + BUDGET_TOLERANCE):
        over = (rate - budget) / (budget * BUDGET_TOLERANCE)   # 0..1
        budget_pts = W["budget"] * (1 - 0.5 * over)
    else:
        return None

    # 5. Location
    dist = _distance_km(tutee.get("location"), tutor.get("location"))
    if dist is not None and dist <= NEAR_KM:
        location = W["location"]
    elif dist is not None and dist <= MAX_INPERSON_KM:
        location = W["location"] * (1 - 0.7 * (dist - NEAR_KM) / (MAX_INPERSON_KM - NEAR_KM))
    elif must_meet:
        return None  # in-person session but too far (or unknown) -> not workable
    else:
        location = 0.0
    if not must_meet:
        location = max(location, W["location"] * 0.6)  # distance barely matters online

    # 6. Reliability: how often this tutor accepts + whether they were recently active
    accepted = int(tutor.get("accepted_n") or 0)
    ignored = int(tutor.get("declined_n") or 0) + int(tutor.get("expired_n") or 0)
    accept_rate = (accepted + 2) / (accepted + ignored + 3)   # smoothed; new tutors start at 0.67
    reliability_ratio = 0.6 * accept_rate + 0.4 * _recency(tutor.get("last_seen"))
    reliability = W["reliability"] * reliability_ratio

    breakdown = {
        "subject": round(subject, 1), "budget": round(budget_pts, 1),
        "schedule": round(schedule, 1), "location": round(location, 1),
        "modality": round(modality, 1), "reliability": round(reliability, 1),
    }
    return round(sum(breakdown.values()), 1), breakdown


def rank_tutors(tutee, tutors):
    """All eligible tutors, best first. Ties: reliability, then cheaper, then id."""
    ranked = []
    for tutor in tutors:
        result = score_pair(tutee, tutor)
        if result:
            score, breakdown = result
            ranked.append((tutor, score, breakdown))
    ranked.sort(key=lambda r: (-r[1], -r[2]["reliability"],
                               float(r[0].get("rate") or 0), r[0].get("user_id", 0)))
    return ranked


def find_best_match(tutee, tutors):
    """(tutor, score, breakdown) for the single best tutor, or (None, 0.0, None)."""
    ranked = rank_tutors(tutee, tutors)
    return ranked[0] if ranked else (None, 0.0, None)
