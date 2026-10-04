import os
import sqlite3
import time
from functools import wraps

from flask import Flask, render_template, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash

from database import get_db, init_db, utcnow_str
from matching import find_best_match

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "super_secret_tutor_key")

init_db()

OFFER_TTL_HOURS = 24          # how long a tutor has to answer before the offer moves on
RETRY_COOLDOWN_HOURS = 24     # a tutor who declined/ignored a tutee is skipped for this long
OPEN = "('pending', 'accepted', 'termination_pending')"
CHATTABLE = "('accepted', 'termination_pending', 'completed')"
LIVE = ("accepted", "termination_pending")

SCHEDULES = {"weekdays": "Weekdays", "weekends": "Weekends", "flexible": "Flexible"}
MODALITIES = {"online": "Online", "in-person": "In-Person", "both": "Both"}


# ------------------------------------------------------------------ helpers
def auth(role=None):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                return jsonify({"error": "Unauthorized"}), 401
            if role and session.get("role") != role:
                return jsonify({"error": f"Only {role}s can do this."}), 403
            return fn(*args, **kwargs)
        return wrapper
    return deco


def notify(conn, user_id, message):
    conn.execute("INSERT INTO notifications (user_id, message) VALUES (?, ?)", (user_id, message))


def log_activity(conn, user_id, action):
    conn.execute("INSERT INTO activity_history (user_id, action) VALUES (?, ?)", (user_id, action))


def username_of(conn, user_id):
    row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["username"] if row else "unknown"


# ------------------------------------------------------------------ matching core
def create_offer(conn, tutee_id):
    """
    Find the best available tutor for this tutee and reserve them by inserting a
    pending request. MUST be called inside a BEGIN IMMEDIATE transaction so two
    simultaneous searches can never grab the same tutor.
    Returns (offer, None) on success or (None, error_message).
    """
    if conn.execute(f"SELECT 1 FROM hire_requests WHERE tutee_id = ? AND status IN {OPEN}",
                    (tutee_id,)).fetchone():
        return None, "You already have an open request or an ongoing session."

    prof = conn.execute("SELECT * FROM profiles WHERE user_id = ?", (tutee_id,)).fetchone()
    if not prof:
        return None, "Please complete your matching profile first."

    tutee = {
        "subject": prof["subject"], "budget": prof["budget_or_rate"],
        "schedule": prof["schedule"], "location": prof["location"],
        "modality": prof["modality"],
    }

    rows = conn.execute(f'''
        SELECT p.user_id, u.username, u.last_seen, p.full_name, p.subject,
               p.budget_or_rate AS rate, p.schedule, p.location, p.modality,
               (SELECT COUNT(*) FROM hire_requests h WHERE h.tutor_id = p.user_id
                  AND h.status IN ('accepted', 'termination_pending', 'completed')) AS accepted_n,
               (SELECT COUNT(*) FROM hire_requests h WHERE h.tutor_id = p.user_id
                  AND h.status = 'declined') AS declined_n,
               (SELECT COUNT(*) FROM hire_requests h WHERE h.tutor_id = p.user_id
                  AND h.status = 'expired') AS expired_n
        FROM profiles p
        JOIN users u ON u.id = p.user_id
        WHERE u.role = 'tutor'
          AND p.user_id NOT IN (SELECT tutor_id FROM hire_requests WHERE status IN {OPEN})
          AND p.user_id NOT IN (
                SELECT tutor_id FROM hire_requests
                WHERE tutee_id = ? AND status IN ('declined', 'expired')
                  AND datetime(COALESCE(responded_at, created_at)) > datetime('now', ?))
    ''', (tutee_id, f"-{RETRY_COOLDOWN_HOURS} hours")).fetchall()

    tutor, score, breakdown = find_best_match(tutee, [dict(r) for r in rows])
    if not tutor:
        return None, "No available tutor matches your profile right now. Please try again later."

    expires_at = utcnow_str(OFFER_TTL_HOURS)
    try:
        cur = conn.execute('''
            INSERT INTO hire_requests (tutee_id, tutor_id, match_score, status, expires_at)
            VALUES (?, ?, ?, 'pending', ?)
        ''', (tutee_id, tutor["user_id"], score, expires_at))
    except sqlite3.IntegrityError:
        return None, "That tutor was just taken. Please try again."

    tutee_name = username_of(conn, tutee_id)
    notify(conn, tutor["user_id"],
           f"New tutoring offer from '{tutee_name}' ({prof['subject']}). "
           f"Open your homepage to accept or decline.")
    log_activity(conn, tutee_id, f"Matched instantly with tutor '{tutor['username']}' "
                                 f"({score}% compatible). Waiting for their response.")
    return {
        "request_id": cur.lastrowid,
        "tutor": {k: tutor[k] for k in ("user_id", "username", "full_name", "subject",
                                        "rate", "schedule", "location", "modality")},
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
        log_activity(conn, req["tutor_id"], f"Offer #{req['id']} expired without a response.")
        notify(conn, req["tutor_id"], f"Offer #{req['id']} expired because you did not respond in time.")
        offer, err = create_offer(conn, req["tutee_id"])
        if offer:
            notify(conn, req["tutee_id"],
                   f"The previous tutor did not respond in time. We matched you with "
                   f"'{offer['tutor']['username']}' instead.")
        else:
            notify(conn, req["tutee_id"],
                   f"The previous tutor did not respond in time. {err}")


def sweep_expired_offers():
    conn = get_db()
    try:
        if not conn.execute('''
            SELECT 1 FROM hire_requests WHERE status = 'pending' AND expires_at IS NOT NULL
              AND datetime(expires_at) <= datetime('now') LIMIT 1
        ''').fetchone():
            return
        conn.execute("BEGIN IMMEDIATE")
        expire_stale_offers(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        app.logger.exception("Offer sweep failed")
    finally:
        conn.close()


@app.before_request
def housekeeping():
    if not request.path.startswith("/api/") or "user_id" not in session:
        return
    sweep_expired_offers()
    now = time.time()
    if now - session.get("_seen", 0) > 60:      # presence, used by the reliability score
        conn = get_db()
        conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (utcnow_str(), session["user_id"]))
        conn.commit()
        conn.close()
        session["_seen"] = now


# ------------------------------------------------------------------ authentication
@app.route('/api/register', methods=['POST'])
def register():
    data = request.json or {}
    username = (data.get('username') or '').strip()
    password = data.get('password')
    role = data.get('role')

    if not username or not password or role not in ['tutor', 'tutee']:
        return jsonify({'success': False, 'message': 'Invalid input fields.'}), 400

    conn = get_db()
    try:
        conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                     (username, generate_password_hash(password), role))
        conn.commit()
        return jsonify({'success': True, 'message': 'Account registered successfully!'})
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'message': 'Username already exists.'}), 400
    finally:
        conn.close()


@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (data.get('username'),)).fetchone()

    if user and check_password_hash(user['password_hash'], data.get('password') or ''):
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['role'] = user['role']
        conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (utcnow_str(), user['id']))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'role': user['role'], 'username': user['username']})

    conn.close()
    return jsonify({'success': False, 'message': 'Invalid username or password.'}), 401


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})


@app.route('/api/session', methods=['GET'])
def get_session():
    if 'user_id' in session:
        conn = get_db()
        user = conn.execute("SELECT id FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        conn.close()
        if user:
            return jsonify({'logged_in': True, 'user_id': session['user_id'],
                            'username': session['username'], 'role': session['role']})
        session.clear()
    return jsonify({'logged_in': False})


# ------------------------------------------------------------------ profile
@app.route('/api/profile', methods=['GET', 'POST'])
@auth()
def profile():
    user_id = session['user_id']
    conn = get_db()

    if request.method == 'POST':
        data = request.json or {}
        try:
            amount = float(data.get('budget_or_rate', 0))
        except (TypeError, ValueError):
            conn.close()
            return jsonify({'success': False, 'message': 'Budget / rate must be a number.'}), 400

        full_name = (data.get('full_name') or '').strip()
        subject = (data.get('subject') or '').strip()
        location = (data.get('location') or '').strip()
        schedule = SCHEDULES.get(str(data.get('schedule', '')).strip().lower())
        modality = MODALITIES.get(str(data.get('modality', '')).strip().lower())

        if not (full_name and subject and location and schedule and modality) or amount < 0:
            conn.close()
            return jsonify({'success': False, 'message': 'Please fill in every field correctly.'}), 400

        conn.execute('''
            INSERT INTO profiles (user_id, full_name, subject, budget_or_rate, schedule, location, modality)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                full_name=excluded.full_name, subject=excluded.subject,
                budget_or_rate=excluded.budget_or_rate, schedule=excluded.schedule,
                location=excluded.location, modality=excluded.modality
        ''', (user_id, full_name, subject, amount, schedule, location, modality))
        log_activity(conn, user_id, "Updated matching profile criteria.")
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Profile criteria updated!'})

    row = conn.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return jsonify(dict(row) if row else {})


# ------------------------------------------------------------------ instant matching
@app.route('/api/match', methods=['POST'])
@auth('tutee')
def find_tutor():
    """One click: pick the best available tutor and send them the offer immediately."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")          # lock so two searches can't take the same tutor
        expire_stale_offers(conn)
        offer, err = create_offer(conn, session['user_id'])
        if err:
            conn.rollback()
            return jsonify({'success': False, 'message': err}), 400
        conn.commit()
        t = offer['tutor']
        return jsonify({
            'success': True,
            'message': f"Matched with {t['full_name']} (@{t['username']}) - {offer['compatibility']}% "
                       f"compatible. They'll see your offer when they log in.",
            **offer,
        })
    except Exception:
        conn.rollback()
        app.logger.exception("Matching failed")
        return jsonify({'success': False, 'message': 'Matching failed. Please try again.'}), 500
    finally:
        conn.close()


@app.route('/api/match/cancel', methods=['POST'])
@auth('tutee')
def cancel_offer():
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    req = conn.execute("SELECT * FROM hire_requests WHERE tutee_id = ? AND status = 'pending' "
                       "ORDER BY id DESC LIMIT 1", (session['user_id'],)).fetchone()
    if not req:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'message': 'You have no pending request to cancel.'}), 400

    conn.execute("UPDATE hire_requests SET status = 'cancelled', responded_at = ? WHERE id = ?",
                 (utcnow_str(), req['id']))
    notify(conn, req['tutor_id'], f"Offer #{req['id']} was cancelled by the tutee.")
    log_activity(conn, session['user_id'], f"Cancelled pending request #{req['id']}.")
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'Request cancelled.'})


@app.route('/api/match/status', methods=['GET'])
@auth('tutee')
def match_status():
    uid = session['user_id']
    conn = get_db()
    base = '''
        SELECT hr.*, u.username AS tutor_name, p.full_name AS tutor_full_name,
               p.subject, p.budget_or_rate AS rate, p.schedule, p.location, p.modality
        FROM hire_requests hr
        JOIN users u ON u.id = hr.tutor_id
        LEFT JOIN profiles p ON p.user_id = hr.tutor_id
        WHERE hr.tutee_id = ? AND '''
    current = conn.execute(base + f"hr.status IN {OPEN} ORDER BY hr.id DESC LIMIT 1", (uid,)).fetchone()
    finished = conn.execute(base + "hr.status = 'completed' ORDER BY hr.completed_at DESC, hr.id DESC",
                            (uid,)).fetchall()
    conn.close()
    return jsonify({'current': dict(current) if current else None,
                    'finished': [dict(r) for r in finished]})


# ------------------------------------------------------------------ tutor response
@app.route('/api/hire/respond', methods=['POST'])
@auth('tutor')
def respond_hire():
    data = request.json or {}
    request_id = data.get('request_id')
    status = data.get('status')
    if status not in ('accepted', 'declined'):
        return jsonify({'success': False, 'message': 'Invalid response.'}), 400

    tutor_id = session['user_id']
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        expire_stale_offers(conn)
        req = conn.execute("SELECT * FROM hire_requests WHERE id = ?", (request_id,)).fetchone()
        if not req or req['tutor_id'] != tutor_id:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Offer not found.'}), 404
        if req['status'] != 'pending':
            conn.rollback()
            return jsonify({'success': False,
                            'message': f"This offer is no longer pending (it is {req['status']})."}), 409

        conn.execute("UPDATE hire_requests SET status = ?, responded_at = ? WHERE id = ?",
                     (status, utcnow_str(), request_id))
        tutor_name, tutee_name = session['username'], username_of(conn, req['tutee_id'])
        log_activity(conn, tutor_id, f"{status.capitalize()} offer #{request_id} from '{tutee_name}'.")

        if status == 'accepted':
            notify(conn, req['tutee_id'], f"Tutor '{tutor_name}' accepted your request. You can chat now!")
            message = 'Offer accepted. You can now chat with your tutee.'
        else:
            offer, err = create_offer(conn, req['tutee_id'])     # instantly re-match the tutee
            if offer:
                notify(conn, req['tutee_id'],
                       f"Tutor '{tutor_name}' declined. We matched you with "
                       f"'{offer['tutor']['username']}' instead.")
            else:
                notify(conn, req['tutee_id'], f"Tutor '{tutor_name}' declined. {err}")
            message = 'Offer declined.'
        conn.commit()
        return jsonify({'success': True, 'message': message})
    except Exception:
        conn.rollback()
        app.logger.exception("Respond failed")
        return jsonify({'success': False, 'message': 'Something went wrong.'}), 500
    finally:
        conn.close()


# ------------------------------------------------------------------ two-step termination
@app.route('/api/hire/terminate', methods=['POST'])
@auth()
def terminate_session():
    """
    State machine (the OTHER party must agree before a session is finished):
      accepted --request--> termination_pending --confirm--> completed (DONE)
                                  |--reject (other) / cancel (requester)--> accepted
    """
    data = request.json or {}
    request_id, action = data.get('request_id'), data.get('action', 'request')
    me, my_name = session['user_id'], session['username']

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        req = conn.execute("SELECT * FROM hire_requests WHERE id = ?", (request_id,)).fetchone()
        if not req or me not in (req['tutee_id'], req['tutor_id']):
            conn.rollback()
            return jsonify({'success': False, 'message': 'Session not found.'}), 404

        other = req['tutor_id'] if me == req['tutee_id'] else req['tutee_id']
        other_name = username_of(conn, other)
        requested_by = req['terminate_requested_by']

        def conflict(msg):
            conn.rollback()
            return jsonify({'success': False, 'message': msg}), 409

        if action == 'request':
            if req['status'] != 'accepted':
                return conflict('Only an ongoing session can be terminated.')
            conn.execute('''UPDATE hire_requests SET status = 'termination_pending',
                            terminate_requested_by = ?, terminate_requested_at = ? WHERE id = ?''',
                         (me, utcnow_str(), request_id))
            notify(conn, other, f"'{my_name}' asked to end session #{request_id}. "
                                f"Please confirm or keep the session going.")
            log_activity(conn, me, f"Requested to end session #{request_id} with '{other_name}'.")
            message = 'Termination requested. Waiting for the other party to confirm.'

        elif action in ('confirm', 'reject', 'cancel'):
            if req['status'] != 'termination_pending':
                return conflict('There is no pending termination request for this session.')
            if action == 'cancel':
                if requested_by != me:
                    return conflict('Only the person who asked can withdraw the request.')
            elif requested_by == me:
                return conflict('The other party has to answer your request.')

            if action == 'confirm':
                now = utcnow_str()
                conn.execute("UPDATE hire_requests SET status = 'completed', completed_at = ? WHERE id = ?",
                             (now, request_id))
                notify(conn, requested_by, f"'{my_name}' confirmed. Session #{request_id} is now DONE.")
                for uid, partner in ((req['tutee_id'], req['tutor_id']), (req['tutor_id'], req['tutee_id'])):
                    log_activity(conn, uid, f"Session #{request_id} with "
                                            f"'{username_of(conn, partner)}' marked DONE.")
                message = 'Session marked DONE. The chat is kept in your finished sessions.'
            else:
                conn.execute('''UPDATE hire_requests SET status = 'accepted',
                                terminate_requested_by = NULL, terminate_requested_at = NULL
                                WHERE id = ?''', (request_id,))
                if action == 'reject':
                    notify(conn, requested_by, f"'{my_name}' wants to continue session #{request_id}. "
                                               f"Termination was declined.")
                    log_activity(conn, me, f"Declined termination of session #{request_id}.")
                    message = 'Termination declined. The session continues.'
                else:
                    notify(conn, other, f"'{my_name}' withdrew the termination request for session #{request_id}.")
                    log_activity(conn, me, f"Withdrew termination request for session #{request_id}.")
                    message = 'Termination request withdrawn.'
        else:
            return conflict('Unknown action.')

        conn.commit()
        return jsonify({'success': True, 'message': message})
    except Exception:
        conn.rollback()
        app.logger.exception("Terminate failed")
        return jsonify({'success': False, 'message': 'Something went wrong.'}), 500
    finally:
        conn.close()


# ------------------------------------------------------------------ dashboards
@app.route('/api/dashboard', methods=['GET'])
@auth()
def get_dashboard():
    uid, role = session['user_id'], session['role']
    conn = get_db()
    if role == 'tutor':
        rows = conn.execute('''
            SELECT hr.*, u.username AS tutee_name, p.subject, p.budget_or_rate
            FROM hire_requests hr JOIN users u ON hr.tutee_id = u.id
            LEFT JOIN profiles p ON hr.tutee_id = p.user_id
            WHERE hr.tutor_id = ? ORDER BY hr.id DESC''', (uid,)).fetchall()
    else:
        rows = conn.execute('''
            SELECT hr.*, u.username AS tutor_name, p.subject, p.budget_or_rate
            FROM hire_requests hr JOIN users u ON hr.tutor_id = u.id
            LEFT JOIN profiles p ON hr.tutor_id = p.user_id
            WHERE hr.tutee_id = ? ORDER BY hr.id DESC''', (uid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/tutor/engagements', methods=['GET'])
@auth('tutor')
def get_tutor_engagements():
    conn = get_db()
    rows = conn.execute('''
        SELECT hr.*, u.username AS tutee_name, p.full_name, p.subject, p.schedule,
               p.modality, p.location, p.budget_or_rate AS budget
        FROM hire_requests hr JOIN users u ON hr.tutee_id = u.id
        LEFT JOIN profiles p ON hr.tutee_id = p.user_id
        WHERE hr.tutor_id = ? AND hr.status IN ('pending', 'accepted', 'termination_pending', 'completed')
        ORDER BY hr.id DESC''', (session['user_id'],)).fetchall()
    conn.close()
    items = [dict(r) for r in rows]
    return jsonify({
        'offers': [i for i in items if i['status'] == 'pending'],
        'active': [i for i in items if i['status'] in LIVE],
        'finished': [i for i in items if i['status'] == 'completed'],
    })


# ------------------------------------------------------------------ notifications & history
@app.route('/api/notifications', methods=['GET'])
@auth()
def get_notifications():
    conn = get_db()
    rows = conn.execute("SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC",
                        (session['user_id'],)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/activity', methods=['GET'])
@auth()
def get_activity():
    conn = get_db()
    rows = conn.execute("SELECT * FROM activity_history WHERE user_id = ? ORDER BY id DESC",
                        (session['user_id'],)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ------------------------------------------------------------------ chat (one thread per session)
def _session_for(conn, request_id, user_id):
    req = conn.execute(f"SELECT * FROM hire_requests WHERE id = ? AND status IN {CHATTABLE}",
                       (request_id,)).fetchone()
    return req if req and user_id in (req['tutee_id'], req['tutor_id']) else None


@app.route('/api/messages', methods=['GET', 'POST'])
@auth()
def handle_messages():
    me = session['user_id']
    conn = get_db()

    if request.method == 'POST':
        data = request.json or {}
        text = str(data.get('message') or '').strip()[:1000]
        req = _session_for(conn, data.get('request_id'), me)
        if not req or not text:
            conn.close()
            return jsonify({'success': False, 'error': 'Missing session or message.'}), 400
        if req['status'] not in LIVE:
            conn.close()
            return jsonify({'success': False, 'error': 'This session is DONE. Messaging is closed.'}), 403

        receiver = req['tutor_id'] if me == req['tutee_id'] else req['tutee_id']
        conn.execute("INSERT INTO messages (sender_id, receiver_id, message, request_id) VALUES (?, ?, ?, ?)",
                     (me, receiver, text, req['id']))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    req = _session_for(conn, request.args.get('request_id'), me)
    if not req:
        conn.close()
        return jsonify({'messages': [], 'is_active': False, 'status': None})

    msgs = conn.execute('''
        SELECT m.*, u.username AS sender_name FROM messages m
        JOIN users u ON m.sender_id = u.id
        WHERE m.request_id = ? ORDER BY m.id ASC''', (req['id'],)).fetchall()
    conn.close()
    return jsonify({'messages': [dict(m) for m in msgs],
                    'is_active': req['status'] in LIVE, 'status': req['status']})


@app.route('/api/chat/contacts', methods=['GET'])
@auth()
def get_contacts():
    me = session['user_id']
    conn = get_db()
    rows = conn.execute(f'''
        SELECT hr.id AS request_id, hr.status, u.id AS other_id, u.username
        FROM hire_requests hr
        JOIN users u ON u.id = CASE WHEN hr.tutee_id = ? THEN hr.tutor_id ELSE hr.tutee_id END
        WHERE (hr.tutee_id = ? OR hr.tutor_id = ?) AND hr.status IN {CHATTABLE}
        ORDER BY (hr.status = 'completed'), hr.id DESC''', (me, me, me)).fetchall()
    conn.close()
    return jsonify([{**dict(r), 'is_active': r['status'] in LIVE} for r in rows])


@app.route('/')
def index():
    return render_template('index.html')


if __name__ == '__main__':
    app.run(debug=True, port=5000)
