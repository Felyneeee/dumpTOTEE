from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
from database import get_db, init_db
from matching import find_best_match_hungarian

app = Flask(__name__)
app.secret_key = "super_secret_tutor_key"

init_db()

# --- AUTHENTICATION & LOGIN ---
@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    role = data.get('role')

    if not username or not password or role not in ['tutor', 'tutee']:
        return jsonify({'success': False, 'message': 'Invalid input fields.'}), 400

    hashed_pw = generate_password_hash(password)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                       (username, hashed_pw, role))
        conn.commit()
        return jsonify({'success': True, 'message': 'Account registered successfully!'})
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'message': 'Username already exists.'}), 400
    finally:
        conn.close()

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')

    conn = get_db()
    cursor = conn.cursor()
    user = cursor.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    if user and check_password_hash(user['password_hash'], password):
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['role'] = user['role']
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
        cursor = conn.cursor()
        # Verify user still exists in DB
        user = cursor.execute("SELECT id FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        conn.close()
        
        if user:
            return jsonify({
                'logged_in': True,
                'user_id': session['user_id'],
                'username': session['username'],
                'role': session['role']
            })
        else:
            # Clear invalid session if user was deleted
            session.clear()

    return jsonify({'logged_in': False})

# --- PROFILE & CRITERIA FORM ---
@app.route('/api/profile', methods=['GET', 'POST'])
def profile():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    user_id = session['user_id']
    conn = get_db()
    cursor = conn.cursor()

    if request.method == 'POST':
        data = request.json
        full_name = data.get('full_name')
        subject = data.get('subject')
        budget_or_rate = float(data.get('budget_or_rate', 0))
        schedule = data.get('schedule')
        location = data.get('location')
        modality = data.get('modality')

        cursor.execute('''
            INSERT INTO profiles (user_id, full_name, subject, budget_or_rate, schedule, location, modality)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                full_name=excluded.full_name,
                subject=excluded.subject,
                budget_or_rate=excluded.budget_or_rate,
                schedule=excluded.schedule,
                location=excluded.location,
                modality=excluded.modality
        ''', (user_id, full_name, subject, budget_or_rate, schedule, location, modality))

        cursor.execute("INSERT INTO activity_history (user_id, action) VALUES (?, ?)", 
                       (user_id, "Updated matching profile criteria."))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Profile criteria updated!'})

    profile_data = cursor.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    if profile_data:
        return jsonify(dict(profile_data))
    return jsonify({})

# --- AUTOMATED HUNGARIAN MATCHING ---
@app.route('/api/match', methods=['GET'])
def match_tutor():
    if 'user_id' not in session or session['role'] != 'tutee':
        return jsonify({'error': 'Only tutees can request matching.'}), 403

    tutee_id = session['user_id']
    conn = get_db()
    cursor = conn.cursor()

    tutee_prof = cursor.execute("SELECT * FROM profiles WHERE user_id = ?", (tutee_id,)).fetchone()
    if not tutee_prof:
        conn.close()
        return jsonify({'success': False, 'message': 'Please complete your matching profile first.'}), 400

    tutee_dict = {
        'subject': tutee_prof['subject'],
        'budget': tutee_prof['budget_or_rate'],
        'schedule': tutee_prof['schedule'],
        'location': tutee_prof['location'],
        'modality': tutee_prof['modality']
    }

    tutors = cursor.execute('''
        SELECT p.*, u.username 
        FROM profiles p 
        JOIN users u ON p.user_id = u.id 
        WHERE u.role = 'tutor'
        AND p.user_id NOT IN (
            SELECT tutor_id 
            FROM hire_requests 
            WHERE tutee_id = ? AND status IN ('pending', 'accepted')
        )
    ''', (tutee_id,)).fetchall()

    tutor_list = []
    for t in tutors:
        tutor_list.append({
            'user_id': t['user_id'],
            'username': t['username'],
            'full_name': t['full_name'],
            'subject': t['subject'],
            'rate': t['budget_or_rate'],
            'schedule': t['schedule'],
            'location': t['location'],
            'modality': t['modality']
        })

    best_tutor, score = find_best_match_hungarian(tutee_dict, tutor_list)
    conn.close()

    if not best_tutor:
        return jsonify({'success': False, 'message': 'No available tutor found matching your requirements.'})

    return jsonify({
        'success': True,
        'tutor': best_tutor,
        'compatibility': score
    })

# --- HIRE REQUEST & DASHBOARD ---
@app.route('/api/hire', methods=['POST'])
def send_hire():
    if 'user_id' not in session or session['role'] != 'tutee':
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.json
    tutor_id = data.get('tutor_id')
    score = data.get('score', 0)
    tutee_id = session['user_id']

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("INSERT INTO hire_requests (tutee_id, tutor_id, match_score, status) VALUES (?, ?, ?, 'pending')",
                   (tutee_id, tutor_id, score))
    
    cursor.execute("INSERT INTO notifications (user_id, message) VALUES (?, ?)",
                   (tutor_id, f"New hire request received from tutee '{session['username']}'!"))
    cursor.execute("INSERT INTO activity_history (user_id, action) VALUES (?, ?)",
                   (tutee_id, f"Sent a hire offer to tutor ID #{tutor_id}."))

    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'Hire request successfully sent!'})

@app.route('/api/dashboard', methods=['GET'])
def get_dashboard():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    user_id = session['user_id']
    role = session['role']
    conn = get_db()
    cursor = conn.cursor()

    if role == 'tutor':
        requests = cursor.execute('''
            SELECT hr.*, u.username as tutee_name, p.subject, p.budget_or_rate
            FROM hire_requests hr
            JOIN users u ON hr.tutee_id = u.id
            LEFT JOIN profiles p ON hr.tutee_id = p.user_id
            WHERE hr.tutor_id = ? ORDER BY hr.id DESC
        ''', (user_id,)).fetchall()
    else:
        requests = cursor.execute('''
            SELECT hr.*, u.username as tutor_name, p.subject, p.budget_or_rate
            FROM hire_requests hr
            JOIN users u ON hr.tutor_id = u.id
            LEFT JOIN profiles p ON hr.tutor_id = p.user_id
            WHERE hr.tutee_id = ? ORDER BY hr.id DESC
        ''', (user_id,)).fetchall()

    conn.close()
    
    response = jsonify([dict(r) for r in requests])
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

@app.route('/api/hire/respond', methods=['POST'])
def respond_hire():
    if 'user_id' not in session or session['role'] != 'tutor':
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.json
    request_id = data.get('request_id')
    status = data.get('status')  # 'accepted' or 'declined'

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE hire_requests SET status = ? WHERE id = ?", (status, request_id))

    req_info = cursor.execute("SELECT * FROM hire_requests WHERE id = ?", (request_id,)).fetchone()
    if req_info:
        cursor.execute("INSERT INTO notifications (user_id, message) VALUES (?, ?)",
                       (req_info['tutee_id'], f"Your hire request was {status} by the tutor."))
        cursor.execute("INSERT INTO activity_history (user_id, action) VALUES (?, ?)",
                       (session['user_id'], f"Responded '{status}' to hire request."))

    conn.commit()
    conn.close()
    return jsonify({'success': True})

# --- TUTOR HOMEPAGE ENGAGEMENTS ---
@app.route('/api/tutor/engagements', methods=['GET'])
def get_tutor_engagements():
    if 'user_id' not in session or session['role'] != 'tutor':
        return jsonify([])

    tutor_id = session['user_id']
    conn = get_db()
    cursor = conn.cursor()

    # Query exclusively active 'accepted' engagements
    engagements = cursor.execute('''
        SELECT hr.id as request_id, u.id as tutee_id, u.username as tutee_name, p.full_name, p.subject, p.schedule, p.modality, p.location
        FROM hire_requests hr
        JOIN users u ON hr.tutee_id = u.id
        LEFT JOIN profiles p ON hr.tutee_id = p.user_id
        WHERE hr.tutor_id = ? AND hr.status = 'accepted'
        ORDER BY hr.id DESC
    ''', (tutor_id,)).fetchall()

    conn.close()
    
    response = jsonify([dict(e) for e in engagements])
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

@app.route('/api/tutor/terminate', methods=['POST'])
def terminate_engagement():
    if 'user_id' not in session or session['role'] != 'tutor':
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.json
    request_id = data.get('request_id')

    conn = get_db()
    cursor = conn.cursor()
    
    req_info = cursor.execute("SELECT * FROM hire_requests WHERE id = ?", (request_id,)).fetchone()
    if req_info:
        cursor.execute("UPDATE hire_requests SET status = 'terminated' WHERE id = ?", (request_id,))
        cursor.execute("INSERT INTO notifications (user_id, message) VALUES (?, ?)",
                       (req_info['tutee_id'], f"Your tutoring session with tutor '{session['username']}' has been terminated."))
        cursor.execute("INSERT INTO activity_history (user_id, action) VALUES (?, ?)",
                       (session['user_id'], f"Terminated engagement with tutee ID #{req_info['tutee_id']}."))
        conn.commit()

    conn.close()
    return jsonify({'success': True, 'message': 'Engagement terminated successfully.'})

# --- NOTIFICATIONS & ACTIVITY HISTORY ---
@app.route('/api/notifications', methods=['GET'])
def get_notifications():
    if 'user_id' not in session:
        return jsonify([])
    conn = get_db()
    cursor = conn.cursor()
    notes = cursor.execute("SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC", 
                           (session['user_id'],)).fetchall()
    conn.close()
    return jsonify([dict(n) for n in notes])

@app.route('/api/activity', methods=['GET'])
def get_activity():
    if 'user_id' not in session:
        return jsonify([])
    conn = get_db()
    cursor = conn.cursor()
    acts = cursor.execute("SELECT * FROM activity_history WHERE user_id = ? ORDER BY id DESC", 
                          (session['user_id'],)).fetchall()
    conn.close()
    return jsonify([dict(a) for a in acts])

# --- WORKING CHAT BOX ---
# --- WORKING CHAT BOX ---
@app.route('/api/messages', methods=['GET', 'POST'])
def handle_messages():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    user_id = session['user_id']
    conn = get_db()
    cursor = conn.cursor()

    if request.method == 'POST':
        data = request.json or {}
        receiver_id = data.get('receiver_id')
        msg = data.get('message')

        if not receiver_id or not msg:
            conn.close()
            return jsonify({'error': 'Missing receiver or message content.'}), 400

        try:
            receiver_id = int(receiver_id)
        except (ValueError, TypeError):
            conn.close()
            return jsonify({'error': 'Invalid receiver ID format.'}), 400

        try:
            # Check that an accepted match exists between both users
            allowed = cursor.execute('''
                SELECT 1 FROM hire_requests 
                WHERE status = 'accepted' AND 
                ((tutee_id = ? AND tutor_id = ?) OR (tutee_id = ? AND tutor_id = ?))
            ''', (user_id, receiver_id, receiver_id, user_id)).fetchone()

            if not allowed:
                conn.close()
                return jsonify({'error': 'Chat is only available for active matched tutors and tutees.'}), 403

            cursor.execute("INSERT INTO messages (sender_id, receiver_id, message) VALUES (?, ?, ?)",
                           (user_id, receiver_id, str(msg)))
            conn.commit()
            return jsonify({'success': True})
        except Exception as e:
            conn.rollback()
            return jsonify({'error': f'Database error: {str(e)}'}), 500
        finally:
            conn.close()

    other_id = request.args.get('other_id')
    if not other_id:
        conn.close()
        return jsonify([])

    try:
        other_id = int(other_id)
    except (ValueError, TypeError):
        conn.close()
        return jsonify([])

    try:
        messages = cursor.execute('''
            SELECT m.*, u.username as sender_name 
            FROM messages m 
            JOIN users u ON m.sender_id = u.id
            WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)
            ORDER BY id ASC
        ''', (user_id, other_id, other_id, user_id)).fetchall()

        return jsonify([dict(m) for m in messages])
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

# --- CHAT CONTACTS (RESTRICTED TO ACCEPTED MATCHES) ---
@app.route('/api/chat/contacts', methods=['GET'])
def get_contacts():
    if 'user_id' not in session:
        return jsonify([])

    user_id = session['user_id']
    role = session['role']
    conn = get_db()
    cursor = conn.cursor()

    if role == 'tutee':
        contacts = cursor.execute('''
            SELECT DISTINCT u.id, u.username, u.role 
            FROM hire_requests hr
            JOIN users u ON hr.tutor_id = u.id
            WHERE hr.tutee_id = ? AND hr.status = 'accepted'
        ''', (user_id,)).fetchall()
    else:
        contacts = cursor.execute('''
            SELECT DISTINCT u.id, u.username, u.role 
            FROM hire_requests hr
            JOIN users u ON hr.tutee_id = u.id
            WHERE hr.tutor_id = ? AND hr.status = 'accepted'
        ''', (user_id,)).fetchall()

    conn.close()
    return jsonify([dict(c) for c in contacts])

@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    app.run(debug=True, port=5000)