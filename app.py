import json
import math
import os
import re
import secrets
import sqlite3
import time
from functools import wraps

from flask import Flask, render_template, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash

from catalog import (DAY_IDS, LOCATIONS, MODALITIES, COLLEGE, public_catalog, valid_subject,
                     split_subject, subjects_text, schedule_text, grade_label, grade_range_text,
                     SECURITY_QUESTION_TEXT)
from database import BASE_DIR, connect, transaction, init_db, utcnow_str, OPEN_STATUSES
from matching import find_best_match, MIN_SESSION_MIN

app = Flask(__name__)


def _secret_key():
    """Env var wins; otherwise a random key is created once and kept next to the app."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(BASE_DIR, ".secret_key")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key)
    return key


app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

init_db()

OFFER_TTL_HOURS = 24          # how long a tutor has to answer before the offer moves on
RETRY_COOLDOWN_HOURS = 24     # a tutor who declined/ignored a tutee is skipped for this long
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCK_MINUTES = 5
OPEN = "(" + ", ".join(f"'{s}'" for s in OPEN_STATUSES) + ")"
CHATTABLE = "('accepted', 'termination_pending', 'completed')"
LIVE = ("accepted", "termination_pending")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


@app.errorhandler(ApiError)
def _api_error(e):
    return jsonify({"success": False, "message": e.message, "error": e.message}), e.status


@app.errorhandler(Exception)
def _unexpected(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("Unhandled error")
    msg = "Something went wrong. Please try again."
    return jsonify({"success": False, "message": msg, "error": msg}), 500


# ------------------------------------------------------------------ helpers
def auth(role=None):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                return jsonify({"error": "Unauthorized", "message": "Please log in."}), 401
            if role and session.get("role") != role:
                return jsonify({"error": f"Only {role}s can do this.",
                                "message": f"Only {role}s can do this."}), 403
            return fn(*args, **kwargs)
        return wrapper
    return deco


def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def notify(conn, user_id, message):
    conn.execute("INSERT INTO notifications (user_id, message) VALUES (?, ?)", (user_id, message))


def log_activity(conn, user_id, action):
    conn.execute("INSERT INTO activity_history (user_id, action) VALUES (?, ?)", (user_id, action))


def username_of(conn, user_id):
    row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["username"] if row else "unknown"


def _loads(text, default):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def with_grade_text(d):
    d = dict(d)
    if d.get("grade_min") is not None:
        d["grade_text"] = grade_range_text(d["grade_min"], d.get("grade_max"))
    return d


def profile_dict(row):
    d = dict(row)
    d["subjects"] = _loads(d.pop("subjects_json", None), [])
    d["schedule_slots"] = _loads(d.pop("schedule_json", None), [])
    d["complete"] = bool(d["subjects"] and d["schedule_slots"] and d.get("grade_min") is not None)
    return with_grade_text(d)


_ID_TAG = re.compile(r"\s*#\d+")


def hide_ids(text):
    """Internal ids ('Offer #12') are never shown; this also cleans rows written by older versions."""
    return _ID_TAG.sub("", str(text or ""))


def norm_answer(text):
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def parse_recovery(data):
    qid = str(data.get("security_question") or "")
    if qid not in SECURITY_QUESTION_TEXT:
        raise ApiError("Please choose a recovery question.")
    answer = norm_answer(data.get("security_answer"))
    if len(answer) < 2:
        raise ApiError("Please enter an answer to your recovery question.")
    return qid, generate_password_hash(answer)


def decoy_question(username):
    """Unknown users get a stable fake question, so the form never reveals who has an account."""
    ids = list(SECURITY_QUESTION_TEXT)
    return ids[sum(str(username).lower().encode()) % len(ids)]


# ------------------------------------------------------------------ profile validation
def parse_slots(raw, role):
    if not isinstance(raw, list) or not raw:
        raise ApiError("Please add your available schedule (days and time).")
    limit = 1 if role == "tutee" else 14
    if len(raw) > limit:
        raise ApiError("A tutee can only set one schedule." if role == "tutee"
                       else "That is too many schedules (max 14).")
    slots = []
    for s in raw:
        s = s if isinstance(s, dict) else {}
        days = [d for d in DAY_IDS if d in (s.get("days") or [])]       # also sorts Mon..Sun
        start, end = str(s.get("start", "")), str(s.get("end", ""))
        if not days:
            raise ApiError("Pick at least one day for every schedule.")
        if not (TIME_RE.match(start) and TIME_RE.match(end)):
            raise ApiError("Please set a valid time (from - to) for every schedule.")
        minutes = (int(end[:2]) * 60 + int(end[3:])) - (int(start[:2]) * 60 + int(start[3:]))
        if minutes < MIN_SESSION_MIN:
            raise ApiError(f"Each schedule must be at least {MIN_SESSION_MIN} minutes and the "
                           f"'to' time must be later than 'from' (same day).")
        slots.append({"days": days, "start": start, "end": end})
    return slots


def parse_grade(value):
    try:
        g = int(value)
    except (TypeError, ValueError):
        raise ApiError("Please choose a grade level.")
    if not 1 <= g <= COLLEGE:
        raise ApiError("Invalid grade level.")
    return g


def parse_profile(data, role):
    full_name = str(data.get("full_name") or "").strip()[:80]
    if not full_name:
        raise ApiError("Please enter your full name.")

    try:
        amount = float(data.get("budget_or_rate"))
    except (TypeError, ValueError):
        raise ApiError("Budget / rate must be a number.")
    if not math.isfinite(amount) or not 0 <= amount <= 100000:
        raise ApiError("Budget / rate must be between 0 and 100,000.")

    location = next((n for n in LOCATIONS if n.lower() == str(data.get("location", "")).strip().lower()), None)
    modality = MODALITIES.get(str(data.get("modality", "")).strip().lower())
    if not location:
        raise ApiError("Please choose a location.")
    if not modality:
        raise ApiError("Please choose a modality.")

    slots = parse_slots(data.get("schedule"), role)

    if role == "tutee":
        subject = data.get("subject")
        if not isinstance(subject, str) or not valid_subject(subject):
            raise ApiError("Please choose a subject from the list.")
        subjects = [subject]
        grade_min = grade_max = parse_grade(data.get("grade_level"))
        max_tutees = 1
    else:
        raw = data.get("subjects")
        if not isinstance(raw, list) or not raw or not all(isinstance(s, str) and valid_subject(s) for s in raw):
            raise ApiError("Please select at least one subject you can teach.")
        whole = {s for s in raw if ":" not in s}
        # selecting "Science" already covers "Science - Physics"
        subjects = [s for s in dict.fromkeys(raw) if ":" not in s or split_subject(s)[0] not in whole]
        grade_min, grade_max = parse_grade(data.get("grade_min")), parse_grade(data.get("grade_max"))
        if grade_min > grade_max:
            raise ApiError("'Lowest grade' must not be higher than 'highest grade'.")
        try:
            max_tutees = int(data.get("max_tutees", 1))
        except (TypeError, ValueError):
            raise ApiError("Number of tutees must be a whole number.")
        if not 1 <= max_tutees <= 50:
            raise ApiError("You can accommodate between 1 and 50 tutees.")

    return {"full_name": full_name, "amount": amount, "location": location, "modality": modality,
            "slots": slots, "subjects": subjects, "grade_min": grade_min, "grade_max": grade_max,
            "max_tutees": max_tutees}


# ------------------------------------------------------------------ matching core
def create_offer(conn, tutee_id):
    """
    Pick the best tutor WITH A FREE SEAT for this tutee and reserve it by inserting a pending
    request. Must run inside transaction(conn) so two searches can never take the last seat.
    Returns (offer, None) on success or (None, error_message).
    """
    if conn.execute(f"SELECT 1 FROM hire_requests WHERE tutee_id = ? AND status IN {OPEN}",
                    (tutee_id,)).fetchone():
        return None, "You already have an open request or an ongoing session."

    prof = conn.execute("SELECT * FROM profiles WHERE user_id = ?", (tutee_id,)).fetchone()
    prof = profile_dict(prof) if prof else None
    if not prof or not prof["complete"]:
        return None, "Please complete your matching profile first."

    tutee = {"subject": prof["subjects"][0], "grade": prof["grade_min"],
             "budget": prof["budget_or_rate"], "schedule": prof["schedule_slots"],
             "location": prof["location"], "modality": prof["modality"]}

    rows = conn.execute(f'''
        SELECT p.user_id, u.username, u.last_seen, p.full_name, p.subject, p.schedule,
               p.subjects_json, p.schedule_json, p.grade_min, p.grade_max,
               p.budget_or_rate AS rate, p.location, p.modality,
               COALESCE(p.max_tutees, 1) AS max_tutees,
               (SELECT COUNT(*) FROM hire_requests h WHERE h.tutor_id = p.user_id
                  AND h.status IN {OPEN}) AS open_n,
               (SELECT COUNT(*) FROM hire_requests h WHERE h.tutor_id = p.user_id
                  AND h.status IN ('accepted', 'termination_pending', 'completed')) AS accepted_n,
               (SELECT COUNT(*) FROM hire_requests h WHERE h.tutor_id = p.user_id
                  AND h.status = 'declined') AS declined_n,
               (SELECT COUNT(*) FROM hire_requests h WHERE h.tutor_id = p.user_id
                  AND h.status = 'expired') AS expired_n,
               (SELECT COALESCE(SUM(r.stars), 0) FROM ratings r WHERE r.tutor_id = p.user_id) AS rating_sum,
               (SELECT COUNT(*) FROM ratings r WHERE r.tutor_id = p.user_id) AS rating_n
        FROM profiles p
        JOIN users u ON u.id = p.user_id
        WHERE u.role = 'tutor'
          AND p.subjects_json IS NOT NULL AND p.schedule_json IS NOT NULL AND p.grade_min IS NOT NULL
          AND (SELECT COUNT(*) FROM hire_requests h WHERE h.tutor_id = p.user_id
                 AND h.status IN {OPEN}) < COALESCE(p.max_tutees, 1)
          AND p.user_id NOT IN (
                SELECT tutor_id FROM hire_requests
                WHERE tutee_id = ? AND status IN ('declined', 'expired')
                  AND datetime(COALESCE(responded_at, created_at)) > datetime('now', ?))
    ''', (tutee_id, f"-{RETRY_COOLDOWN_HOURS} hours")).fetchall()

    # Time already promised to each tutor's other tutees. Pending offers count too: they hold a seat.
    booked = {}
    for b in conn.execute(f'''
            SELECT hr.tutor_id, p.schedule_json FROM hire_requests hr
            JOIN profiles p ON p.user_id = hr.tutee_id
            WHERE hr.status IN {OPEN}''').fetchall():
        booked.setdefault(b["tutor_id"], []).extend(_loads(b["schedule_json"], []))

    tutors = []
    for r in rows:
        t = dict(r)
        t["subjects"] = _loads(t.pop("subjects_json"), [])
        t["schedule_slots"] = _loads(t.pop("schedule_json"), [])
        t["schedule_text"], t["schedule"] = t["schedule"], t["schedule_slots"]   # matcher wants the slots
        t["booked"] = booked.get(t["user_id"], [])
        if t["subjects"] and t["schedule"]:
            tutors.append(t)

    tutor, score, breakdown = find_best_match(tutee, tutors)
    if not tutor:
        return None, "No available tutor matches your profile right now. Please try again later."

    expires_at = utcnow_str(OFFER_TTL_HOURS)
    try:
        cur = conn.execute('''
            INSERT INTO hire_requests (tutee_id, tutor_id, match_score, status, expires_at)
            VALUES (?, ?, ?, 'pending', ?)
        ''', (tutee_id, tutor["user_id"], score, expires_at))
    except sqlite3.IntegrityError:
        return None, "Please try again in a moment."

    notify(conn, tutor["user_id"],
           f"New tutoring offer from '{username_of(conn, tutee_id)}' "
           f"({prof['subject']}, {prof['grade_text']}). Open your homepage to accept or decline.")
    log_activity(conn, tutee_id, f"Matched instantly with tutor '{tutor['username']}' "
                                 f"({score}% compatible). Waiting for their response.")
    return {
        "request_id": cur.lastrowid,
        "tutor": {"user_id": tutor["user_id"], "username": tutor["username"],
                  "full_name": tutor["full_name"], "subject": tutor["subject"],
                  "rate": tutor["rate"], "schedule": tutor["schedule_text"],
                  "location": tutor["location"], "modality": tutor["modality"],
                  "grade_text": grade_range_text(tutor["grade_min"], tutor["grade_max"])},
        "compatibility": score, "breakdown": breakdown, "expires_at": expires_at,
    }, None


def expire_stale_offers(conn):
    """Offers the tutor never answered expire, and the tutee is re-matched automatically."""
    stale = conn.execute('''
        SELECT * FROM hire_requests
        WHERE status = 'pending' AND expires_at IS NOT NULL
          AND datetime(expires_at) <= datetime('now')
    ''').fetchall()
    for req in stale:
        conn.execute("UPDATE hire_requests SET status = 'expired', responded_at = ? WHERE id = ?",
                     (utcnow_str(), req["id"]))
        tutee_name = username_of(conn, req["tutee_id"])
        log_activity(conn, req["tutor_id"], f"Offer from '{tutee_name}' expired without a response.")
        notify(conn, req["tutor_id"], f"The offer from '{tutee_name}' expired because you did not respond in time.")
        offer, err = create_offer(conn, req["tutee_id"])
        if offer:
            notify(conn, req["tutee_id"],
                   f"The previous tutor did not respond in time. We matched you with "
                   f"'{offer['tutor']['username']}' instead.")
        else:
            notify(conn, req["tutee_id"], f"The previous tutor did not respond in time. {err}")


def sweep_expired_offers():
    try:
        with connect() as conn:
            if not conn.execute('''
                SELECT 1 FROM hire_requests WHERE status = 'pending' AND expires_at IS NOT NULL
                  AND datetime(expires_at) <= datetime('now') LIMIT 1
            ''').fetchone():
                return
            with transaction(conn):
                expire_stale_offers(conn)
    except Exception:
        app.logger.exception("Offer sweep failed")


@app.before_request
def housekeeping():
    if not request.path.startswith("/api/") or "user_id" not in session:
        return
    sweep_expired_offers()
    now = time.time()
    if now - session.get("_seen", 0) > 60:      # presence, used by the reliability score
        with connect() as conn:
            conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (utcnow_str(), session["user_id"]))
        session["_seen"] = now


# ------------------------------------------------------------------ authentication
@app.route('/api/catalog')
def catalog():
    return jsonify(public_catalog())


@app.route('/api/register', methods=['POST'])
def register():
    data = body()
    username = str(data.get('username') or '').strip()
    password = data.get('password')
    role = data.get('role')

    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,30}", username):
        raise ApiError("Username must be 3-30 characters: letters, numbers, . _ -")
    if not isinstance(password, str) or len(password) < 6:
        raise ApiError("Password must be at least 6 characters.")
    if role not in ('tutor', 'tutee'):
        raise ApiError("Please choose a role.")
    question, answer_hash = parse_recovery(data)

    with connect() as conn:
        try:
            with transaction(conn):
                if conn.execute("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone():
                    raise ApiError("Username already exists.")
                conn.execute("INSERT INTO users (username, password_hash, role, recovery_question, "
                             "recovery_answer_hash) VALUES (?, ?, ?, ?, ?)",
                             (username, generate_password_hash(password), role, question, answer_hash))
        except sqlite3.IntegrityError:
            raise ApiError("Username already exists.")
    return jsonify({'success': True, 'message': 'Account registered! You can log in now.'})


@app.route('/api/login', methods=['POST'])
def login():
    data = body()
    username, password = str(data.get('username') or '').strip(), str(data.get('password') or '')
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()

        if user and user['cooldown_until'] and user['cooldown_until'] > utcnow_str():
            raise ApiError(f"Too many failed attempts. Try again in {LOGIN_LOCK_MINUTES} minutes.", 429)

        if user and check_password_hash(user['password_hash'], password):
            conn.execute("UPDATE users SET failed_attempts = 0, cooldown_until = NULL, last_seen = ? WHERE id = ?",
                         (utcnow_str(), user['id']))
            session.clear()
            session.update(user_id=user['id'], username=user['username'], role=user['role'])
            return jsonify({'success': True, 'role': user['role'], 'username': user['username']})

        if user:
            attempts = (user['failed_attempts'] or 0) + 1
            lock = utcnow_str(minutes=LOGIN_LOCK_MINUTES) if attempts >= LOGIN_MAX_ATTEMPTS else None
            conn.execute("UPDATE users SET failed_attempts = ?, cooldown_until = ? WHERE id = ?",
                         (0 if lock else attempts, lock, user['id']))
    raise ApiError("Invalid username or password.", 401)


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})


@app.route('/api/session', methods=['GET'])
def get_session():
    if 'user_id' in session:
        with connect() as conn:
            user = conn.execute("SELECT id FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        if user:
            return jsonify({'logged_in': True, 'user_id': session['user_id'],
                            'username': session['username'], 'role': session['role']})
        session.clear()
    return jsonify({'logged_in': False})


# ------------------------------------------------------------------ password recovery
@app.route('/api/forgot/question', methods=['POST'])
def forgot_question():
    username = str(body().get('username') or '').strip()
    if not username:
        raise ApiError("Please enter your username.")
    with connect() as conn:
        user = conn.execute("SELECT recovery_question FROM users WHERE username = ? COLLATE NOCASE",
                            (username,)).fetchone()
    qid = user['recovery_question'] if user and user['recovery_question'] in SECURITY_QUESTION_TEXT \
        else decoy_question(username)
    return jsonify({'success': True, 'question': SECURITY_QUESTION_TEXT[qid]})


@app.route('/api/forgot/reset', methods=['POST'])
def forgot_reset():
    data = body()
    username = str(data.get('username') or '').strip()
    answer, new_password = norm_answer(data.get('answer')), data.get('new_password')
    if not isinstance(new_password, str) or len(new_password) < 6:
        raise ApiError("New password must be at least 6 characters.")

    # No transaction here on purpose: a failed attempt must still be counted.
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
        if user and user['reset_cooldown_until'] and user['reset_cooldown_until'] > utcnow_str():
            raise ApiError(f"Too many failed attempts. Try again in {LOGIN_LOCK_MINUTES} minutes.", 429)

        if user and user['recovery_answer_hash'] and answer \
                and check_password_hash(user['recovery_answer_hash'], answer):
            conn.execute('''UPDATE users SET password_hash = ?, failed_attempts = 0, cooldown_until = NULL,
                            reset_failed = 0, reset_cooldown_until = NULL WHERE id = ?''',
                         (generate_password_hash(new_password), user['id']))
            notify(conn, user['id'], "Your password was changed using your recovery question. "
                                     "If this was not you, change it again right away.")
            log_activity(conn, user['id'], "Password reset using recovery question.")
            return jsonify({'success': True, 'message': 'Password changed. You can log in now.'})

        if user:
            attempts = (user['reset_failed'] or 0) + 1
            lock = utcnow_str(minutes=LOGIN_LOCK_MINUTES) if attempts >= LOGIN_MAX_ATTEMPTS else None
            conn.execute("UPDATE users SET reset_failed = ?, reset_cooldown_until = ? WHERE id = ?",
                         (0 if lock else attempts, lock, user['id']))
    raise ApiError("Incorrect answer, or this account has no recovery question set.")


@app.route('/api/account/recovery', methods=['GET', 'POST'])
@auth()
def account_recovery():
    """Lets a logged-in user (including accounts made before this feature) set the recovery question."""
    uid = session['user_id']
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if request.method == 'GET':
            return jsonify({'question': user['recovery_question'] if user else None})
        data = body()
        if not user or not check_password_hash(user['password_hash'], str(data.get('password') or '')):
            raise ApiError("Your current password is incorrect.", 403)
        question, answer_hash = parse_recovery(data)
        conn.execute('''UPDATE users SET recovery_question = ?, recovery_answer_hash = ?,
                        reset_failed = 0, reset_cooldown_until = NULL WHERE id = ?''',
                     (question, answer_hash, uid))
        log_activity(conn, uid, "Updated password recovery question.")
    return jsonify({'success': True, 'message': 'Recovery question saved.'})


# ------------------------------------------------------------------ profile
@app.route('/api/profile', methods=['GET', 'POST'])
@auth()
def profile():
    user_id, role = session['user_id'], session['role']

    if request.method == 'POST':
        p = parse_profile(body(), role)
        subject_txt, schedule_txt = subjects_text(p["subjects"]), schedule_text(p["slots"])
        with connect() as conn, transaction(conn):
            conn.execute('''
                INSERT INTO profiles (user_id, full_name, subject, budget_or_rate, schedule, location,
                                      modality, subjects_json, schedule_json, grade_min, grade_max, max_tutees)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    full_name=excluded.full_name, subject=excluded.subject,
                    budget_or_rate=excluded.budget_or_rate, schedule=excluded.schedule,
                    location=excluded.location, modality=excluded.modality,
                    subjects_json=excluded.subjects_json, schedule_json=excluded.schedule_json,
                    grade_min=excluded.grade_min, grade_max=excluded.grade_max,
                    max_tutees=excluded.max_tutees
            ''', (user_id, p["full_name"], subject_txt, p["amount"], schedule_txt, p["location"],
                  p["modality"], json.dumps(p["subjects"]), json.dumps(p["slots"]),
                  p["grade_min"], p["grade_max"], p["max_tutees"]))
            log_activity(conn, user_id, "Updated matching profile criteria.")
        return jsonify({'success': True, 'message': 'Profile saved!'})

    with connect() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
    return jsonify(profile_dict(row) if row else {})


# ------------------------------------------------------------------ instant matching
@app.route('/api/match', methods=['POST'])
@auth('tutee')
def find_tutor():
    """One click: pick the best available tutor and send them the offer immediately."""
    with connect() as conn, transaction(conn):
        expire_stale_offers(conn)
        offer, err = create_offer(conn, session['user_id'])
        if err:
            raise ApiError(err)
    t = offer['tutor']
    return jsonify({
        'success': True,
        'message': f"Matched with {t['full_name']} (@{t['username']}) - {offer['compatibility']}% "
                   f"compatible. They'll see your offer when they log in.",
        **offer,
    })


@app.route('/api/match/cancel', methods=['POST'])
@auth('tutee')
def cancel_offer():
    with connect() as conn, transaction(conn):
        req = conn.execute("SELECT * FROM hire_requests WHERE tutee_id = ? AND status = 'pending' "
                           "ORDER BY id DESC LIMIT 1", (session['user_id'],)).fetchone()
        if not req:
            raise ApiError('You have no pending request to cancel.')
        conn.execute("UPDATE hire_requests SET status = 'cancelled', responded_at = ? WHERE id = ?",
                     (utcnow_str(), req['id']))
        notify(conn, req['tutor_id'], f"The offer from '{session['username']}' was cancelled by the tutee.")
        log_activity(conn, session['user_id'],
                     f"Cancelled pending request to '{username_of(conn, req['tutor_id'])}'.")
    return jsonify({'success': True, 'message': 'Request cancelled.'})


@app.route('/api/match/status', methods=['GET'])
@auth('tutee')
def match_status():
    uid = session['user_id']
    base = '''
        SELECT hr.*, u.username AS tutor_name, p.full_name AS tutor_full_name,
               p.subject, p.budget_or_rate AS rate, p.schedule, p.location, p.modality,
               p.grade_min, p.grade_max,
               rt.stars AS rating_stars, rt.feedback AS rating_feedback,
               (SELECT ROUND(AVG(r.stars), 1) FROM ratings r WHERE r.tutor_id = hr.tutor_id) AS tutor_rating_avg,
               (SELECT COUNT(*) FROM ratings r WHERE r.tutor_id = hr.tutor_id) AS tutor_rating_n
        FROM hire_requests hr
        JOIN users u ON u.id = hr.tutor_id
        LEFT JOIN profiles p ON p.user_id = hr.tutor_id
        LEFT JOIN ratings rt ON rt.request_id = hr.id
        WHERE hr.tutee_id = ? AND '''
    with connect() as conn:
        current = conn.execute(base + f"hr.status IN {OPEN} ORDER BY hr.id DESC LIMIT 1", (uid,)).fetchone()
        finished = conn.execute(base + "hr.status = 'completed' ORDER BY hr.completed_at DESC, hr.id DESC",
                                (uid,)).fetchall()
    return jsonify({'current': with_grade_text(current) if current else None,
                    'finished': [with_grade_text(r) for r in finished]})


# ------------------------------------------------------------------ tutor response
@app.route('/api/hire/respond', methods=['POST'])
@auth('tutor')
def respond_hire():
    data = body()
    status = data.get('status')
    if status not in ('accepted', 'declined'):
        raise ApiError('Invalid response.')
    tutor_id, tutor_name = session['user_id'], session['username']

    with connect() as conn, transaction(conn):
        expire_stale_offers(conn)
        req = conn.execute("SELECT * FROM hire_requests WHERE id = ?", (data.get('request_id'),)).fetchone()
        if not req or req['tutor_id'] != tutor_id:
            raise ApiError('Offer not found.', 404)
        if req['status'] != 'pending':
            raise ApiError(f"This offer is no longer pending (it is {req['status']}).", 409)

        conn.execute("UPDATE hire_requests SET status = ?, responded_at = ? WHERE id = ?",
                     (status, utcnow_str(), req['id']))
        tutee_name = username_of(conn, req['tutee_id'])
        log_activity(conn, tutor_id, f"{status.capitalize()} offer from '{tutee_name}'.")

        if status == 'accepted':
            notify(conn, req['tutee_id'], f"Tutor '{tutor_name}' accepted your request. You can chat now!")
            message = 'Offer accepted. You can now chat with your tutee.'
        else:
            offer, err = create_offer(conn, req['tutee_id'])     # instantly re-match the tutee
            if offer:
                notify(conn, req['tutee_id'], f"Tutor '{tutor_name}' declined. We matched you with "
                                              f"'{offer['tutor']['username']}' instead.")
            else:
                notify(conn, req['tutee_id'], f"Tutor '{tutor_name}' declined. {err}")
            message = 'Offer declined.'
    return jsonify({'success': True, 'message': message})


# ------------------------------------------------------------------ two-step termination
@app.route('/api/hire/terminate', methods=['POST'])
@auth()
def terminate_session():
    """
    State machine (the OTHER party must agree before a session is finished):
      accepted --request--> termination_pending --confirm--> completed (DONE)
                                  |--reject (other) / cancel (requester)--> accepted
    Finishing a session frees the tutor's seat and lets the tutee search again.
    """
    data = body()
    request_id, action = data.get('request_id'), data.get('action', 'request')
    me, my_name = session['user_id'], session['username']

    with connect() as conn, transaction(conn):
        req = conn.execute("SELECT * FROM hire_requests WHERE id = ?", (request_id,)).fetchone()
        if not req or me not in (req['tutee_id'], req['tutor_id']):
            raise ApiError('Session not found.', 404)

        other = req['tutor_id'] if me == req['tutee_id'] else req['tutee_id']
        other_name = username_of(conn, other)
        requested_by = req['terminate_requested_by']
        rid = req['id']

        if action == 'request':
            if req['status'] != 'accepted':
                raise ApiError('Only an ongoing session can be terminated.', 409)
            conn.execute('''UPDATE hire_requests SET status = 'termination_pending',
                            terminate_requested_by = ?, terminate_requested_at = ? WHERE id = ?''',
                         (me, utcnow_str(), rid))
            notify(conn, other, f"'{my_name}' asked to end your session. "
                                f"Please confirm or keep the session going.")
            log_activity(conn, me, f"Requested to end session with '{other_name}'.")
            message = 'Termination requested. Waiting for the other party to confirm.'

        elif action in ('confirm', 'reject', 'cancel'):
            if req['status'] != 'termination_pending':
                raise ApiError('There is no pending termination request for this session.', 409)
            if action == 'cancel' and requested_by != me:
                raise ApiError('Only the person who asked can withdraw the request.', 409)
            if action != 'cancel' and requested_by == me:
                raise ApiError('The other party has to answer your request.', 409)

            if action == 'confirm':
                conn.execute("UPDATE hire_requests SET status = 'completed', completed_at = ? WHERE id = ?",
                             (utcnow_str(), rid))
                notify(conn, requested_by, f"'{my_name}' confirmed. Your session is now DONE.")
                for uid, partner in ((req['tutee_id'], req['tutor_id']), (req['tutor_id'], req['tutee_id'])):
                    log_activity(conn, uid, f"Session with '{username_of(conn, partner)}' marked DONE.")
                notify(conn, req['tutee_id'], f"Your session with '{username_of(conn, req['tutor_id'])}' is DONE. "
                                              f"Please rate your tutor on your homepage.")
                message = 'Session marked DONE. The chat is kept in your finished sessions.'
            else:
                conn.execute('''UPDATE hire_requests SET status = 'accepted',
                                terminate_requested_by = NULL, terminate_requested_at = NULL
                                WHERE id = ?''', (rid,))
                if action == 'reject':
                    notify(conn, requested_by, f"'{my_name}' wants to continue the session. "
                                               f"Termination was declined.")
                    log_activity(conn, me, f"Declined termination of session with '{other_name}'.")
                    message = 'Termination declined. The session continues.'
                else:
                    notify(conn, other, f"'{my_name}' withdrew the termination request.")
                    log_activity(conn, me, f"Withdrew termination request for session with '{other_name}'.")
                    message = 'Termination request withdrawn.'
        else:
            raise ApiError('Unknown action.', 409)
    return jsonify({'success': True, 'message': message})


# ------------------------------------------------------------------ tutor rating
@app.route('/api/hire/rate', methods=['POST'])
@auth('tutee')
def rate_tutor():
    """After a session is DONE the tutee can leave 1-5 stars (+ optional feedback), once.
    Ratings feed the reliability part of the matching score."""
    data = body()
    try:
        stars = int(data.get('stars'))
    except (TypeError, ValueError):
        raise ApiError('Please choose 1 to 5 stars.')
    if not 1 <= stars <= 5:
        raise ApiError('Please choose 1 to 5 stars.')
    feedback = str(data.get('feedback') or '').strip()[:500]
    me, my_name = session['user_id'], session['username']

    with connect() as conn, transaction(conn):
        req = conn.execute("SELECT * FROM hire_requests WHERE id = ?", (data.get('request_id'),)).fetchone()
        if not req or req['tutee_id'] != me:
            raise ApiError('Session not found.', 404)
        if req['status'] != 'completed':
            raise ApiError('You can rate your tutor once the session is DONE.', 409)
        if conn.execute("SELECT 1 FROM ratings WHERE request_id = ?", (req['id'],)).fetchone():
            raise ApiError('You already rated this session.', 409)
        conn.execute("INSERT INTO ratings (request_id, tutor_id, tutee_id, stars, feedback) VALUES (?, ?, ?, ?, ?)",
                     (req['id'], req['tutor_id'], me, stars, feedback or None))
        tutor_name = username_of(conn, req['tutor_id'])
        notify(conn, req['tutor_id'], f"'{my_name}' rated your session {stars}/5."
                                      + (" They left feedback." if feedback else ""))
        log_activity(conn, me, f"Rated tutor '{tutor_name}' {stars}/5.")
    return jsonify({'success': True, 'message': 'Thanks for your feedback!'})


# ------------------------------------------------------------------ dashboards
@app.route('/api/dashboard', methods=['GET'])
@auth()
def get_dashboard():
    uid, role = session['user_id'], session['role']
    other, mine = ('tutee_id', 'tutor_id') if role == 'tutor' else ('tutor_id', 'tutee_id')
    name_col = 'tutee_name' if role == 'tutor' else 'tutor_name'
    with connect() as conn:
        rows = conn.execute(f'''
            SELECT hr.*, u.username AS {name_col}, p.subject, p.budget_or_rate
            FROM hire_requests hr JOIN users u ON hr.{other} = u.id
            LEFT JOIN profiles p ON hr.{other} = p.user_id
            WHERE hr.{mine} = ? ORDER BY hr.id DESC''', (uid,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/tutor/engagements', methods=['GET'])
@auth('tutor')
def get_tutor_engagements():
    uid = session['user_id']
    with connect() as conn:
        rows = conn.execute('''
            SELECT hr.*, u.username AS tutee_name, p.full_name, p.subject, p.schedule,
                   p.modality, p.location, p.budget_or_rate AS budget, p.grade_min, p.grade_max,
                   rt.stars AS rating_stars, rt.feedback AS rating_feedback
            FROM hire_requests hr JOIN users u ON hr.tutee_id = u.id
            LEFT JOIN profiles p ON hr.tutee_id = p.user_id
            LEFT JOIN ratings rt ON rt.request_id = hr.id
            WHERE hr.tutor_id = ? AND hr.status IN ('pending', 'accepted', 'termination_pending', 'completed')
            ORDER BY hr.id DESC''', (uid,)).fetchall()
        prof = conn.execute("SELECT max_tutees FROM profiles WHERE user_id = ?", (uid,)).fetchone()
        rating = conn.execute("SELECT ROUND(AVG(stars), 1) AS avg, COUNT(*) AS n FROM ratings WHERE tutor_id = ?",
                              (uid,)).fetchone()
    items = [with_grade_text(r) for r in rows]
    used = sum(1 for i in items if i['status'] in OPEN_STATUSES)
    return jsonify({
        'offers': [i for i in items if i['status'] == 'pending'],
        'active': [i for i in items if i['status'] in LIVE],
        'finished': [i for i in items if i['status'] == 'completed'],
        'capacity': {'used': used, 'max': (prof['max_tutees'] or 1) if prof else None},
        'rating': {'avg': rating['avg'], 'count': rating['n']},
    })


# ------------------------------------------------------------------ notifications & history
@app.route('/api/notifications', methods=['GET'])
@auth()
def get_notifications():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 200",
                            (session['user_id'],)).fetchall()
    return jsonify([{**dict(r), 'message': hide_ids(r['message'])} for r in rows])


@app.route('/api/notifications/read', methods=['POST'])
@auth()
def read_notifications():
    with connect() as conn:
        conn.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (session['user_id'],))
    return jsonify({'success': True})


@app.route('/api/activity', methods=['GET'])
@auth()
def get_activity():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM activity_history WHERE user_id = ? ORDER BY id DESC LIMIT 200",
                            (session['user_id'],)).fetchall()
    return jsonify([{**dict(r), 'action': hide_ids(r['action'])} for r in rows])


# ------------------------------------------------------------------ chat (one thread per session)
def _session_for(conn, request_id, user_id):
    try:
        request_id = int(request_id)
    except (TypeError, ValueError):
        return None
    req = conn.execute(f"SELECT * FROM hire_requests WHERE id = ? AND status IN {CHATTABLE}",
                       (request_id,)).fetchone()
    return req if req and user_id in (req['tutee_id'], req['tutor_id']) else None


@app.route('/api/messages', methods=['GET', 'POST'])
@auth()
def handle_messages():
    me = session['user_id']

    if request.method == 'POST':
        data = body()
        text = str(data.get('message') or '').strip()[:1000]
        with connect() as conn, transaction(conn):
            req = _session_for(conn, data.get('request_id'), me)
            if not req or not text:
                raise ApiError('Missing session or message.')
            if req['status'] not in LIVE:
                raise ApiError('This session is DONE. Messaging is closed.', 403)
            receiver = req['tutor_id'] if me == req['tutee_id'] else req['tutee_id']
            conn.execute("INSERT INTO messages (sender_id, receiver_id, message, request_id) VALUES (?, ?, ?, ?)",
                         (me, receiver, text, req['id']))
        return jsonify({'success': True})

    with connect() as conn:
        req = _session_for(conn, request.args.get('request_id'), me)
        if not req:
            return jsonify({'messages': [], 'is_active': False, 'status': None})
        msgs = conn.execute('''
            SELECT m.*, u.username AS sender_name FROM messages m
            JOIN users u ON m.sender_id = u.id
            WHERE m.request_id = ? ORDER BY m.id ASC''', (req['id'],)).fetchall()
    return jsonify({'messages': [dict(m) for m in msgs],
                    'is_active': req['status'] in LIVE, 'status': req['status']})


@app.route('/api/chat/contacts', methods=['GET'])
@auth()
def get_contacts():
    me = session['user_id']
    with connect() as conn:
        rows = conn.execute(f'''
            SELECT hr.id AS request_id, hr.status, hr.created_at AS started, u.username
            FROM hire_requests hr
            JOIN users u ON u.id = CASE WHEN hr.tutee_id = ? THEN hr.tutor_id ELSE hr.tutee_id END
            WHERE (hr.tutee_id = ? OR hr.tutor_id = ?) AND hr.status IN {CHATTABLE}
            ORDER BY (hr.status = 'completed'), hr.id DESC''', (me, me, me)).fetchall()
    return jsonify([{**dict(r), 'is_active': r['status'] in LIVE} for r in rows])


@app.route('/')
def index():
    return render_template('index.html')


if __name__ == '__main__':
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1")
