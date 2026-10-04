import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

# Absolute path, so the app finds the same database no matter where it is launched from.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.environ.get("TUTOR_DB", os.path.join(BASE_DIR, "tutor_matching.db"))

# A request in one of these states "occupies" the tutee and one seat of the tutor.
OPEN_STATUSES = ("pending", "accepted", "termination_pending")


def utcnow_str(hours=0, minutes=0):
    """UTC timestamp in the same format SQLite's CURRENT_TIMESTAMP uses."""
    dt = datetime.now(timezone.utc) + timedelta(hours=hours, minutes=minutes)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    # isolation_level=None -> we control transactions explicitly (see transaction()).
    conn = sqlite3.connect(DB_NAME, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def connect():
    conn = get_db()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn):
    """BEGIN IMMEDIATE takes the write lock up front, so two simultaneous matches
    can never both read 'tutor has a free seat' and both take it."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


HIRE_REQUESTS_DDL = '''
    CREATE TABLE IF NOT EXISTS hire_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tutee_id INTEGER NOT NULL,
        tutor_id INTEGER NOT NULL,
        match_score REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
            'pending', 'accepted', 'declined', 'expired', 'cancelled',
            'termination_pending', 'completed'
        )),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP,
        responded_at TIMESTAMP,
        terminate_requested_by INTEGER,
        terminate_requested_at TIMESTAMP,
        completed_at TIMESTAMP,
        FOREIGN KEY (tutee_id) REFERENCES users (id),
        FOREIGN KEY (tutor_id) REFERENCES users (id)
    )
'''


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn, table, column, ddl):
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate_hire_requests(conn):
    """Old schema had a CHECK without 'termination_pending'. SQLite cannot alter a
    CHECK in place, so rebuild the table once and keep every existing row."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='hire_requests'"
    ).fetchone()
    if not row or "termination_pending" in row["sql"]:
        return

    conn.execute("ALTER TABLE hire_requests RENAME TO hire_requests_old")
    conn.execute(HIRE_REQUESTS_DDL)
    conn.execute('''
        INSERT INTO hire_requests
            (id, tutee_id, tutor_id, match_score, status, created_at, expires_at, completed_at)
        SELECT id, tutee_id, tutor_id, match_score,
               CASE WHEN status = 'terminated' THEN 'completed' ELSE status END,
               created_at, expires_at,
               CASE WHEN status IN ('completed', 'terminated') THEN CURRENT_TIMESTAMP END
        FROM hire_requests_old
    ''')
    conn.execute("DROP TABLE hire_requests_old")


def init_db():
    conn = get_db()
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('tutor', 'tutee')),
                failed_attempts INTEGER DEFAULT 0,
                cooldown_until TIMESTAMP,
                last_seen TIMESTAMP
            )
        ''')

        # subject / schedule hold readable summaries; the structured data lives in the *_json columns.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                subject TEXT NOT NULL,
                budget_or_rate REAL NOT NULL,
                schedule TEXT NOT NULL,
                location TEXT NOT NULL,
                modality TEXT NOT NULL,
                subjects_json TEXT,
                schedule_json TEXT,
                grade_min INTEGER,
                grade_max INTEGER,
                max_tutees INTEGER DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        conn.execute(HIRE_REQUESTS_DDL)

        conn.execute('''
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                is_read INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS activity_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                receiver_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                request_id INTEGER,
                FOREIGN KEY (sender_id) REFERENCES users (id),
                FOREIGN KEY (receiver_id) REFERENCES users (id)
            )
        ''')

        # ---- migrations for databases created by older versions ----
        _add_column(conn, "users", "last_seen", "TIMESTAMP")
        # password recovery (question id + hashed answer) and its own lockout counter
        _add_column(conn, "users", "recovery_question", "TEXT")
        _add_column(conn, "users", "recovery_answer_hash", "TEXT")
        _add_column(conn, "users", "reset_failed", "INTEGER DEFAULT 0")
        _add_column(conn, "users", "reset_cooldown_until", "TIMESTAMP")
        _add_column(conn, "messages", "request_id", "INTEGER")
        for col, ddl in (("subjects_json", "TEXT"), ("schedule_json", "TEXT"),
                         ("grade_min", "INTEGER"), ("grade_max", "INTEGER"),
                         ("max_tutees", "INTEGER DEFAULT 1")):
            _add_column(conn, "profiles", col, ddl)
        _migrate_hire_requests(conn)

        # Created AFTER the migration above: renaming hire_requests would otherwise
        # re-point this table's foreign key at the dropped hire_requests_old table.
        # One rating per finished session, given by the tutee to the tutor.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL UNIQUE,
                tutor_id INTEGER NOT NULL,
                tutee_id INTEGER NOT NULL,
                stars INTEGER NOT NULL CHECK(stars BETWEEN 1 AND 5),
                feedback TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (request_id) REFERENCES hire_requests (id),
                FOREIGN KEY (tutor_id) REFERENCES users (id),
                FOREIGN KEY (tutee_id) REFERENCES users (id)
            )
        ''')
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ratings_tutor ON ratings(tutor_id)")

        # Offers that already timed out must not block the unique index below.
        conn.execute('''
            UPDATE hire_requests SET status = 'expired', responded_at = CURRENT_TIMESTAMP
            WHERE status = 'pending' AND expires_at IS NOT NULL
              AND datetime(expires_at) <= datetime('now')
        ''')

        # Tie old chat messages to the session they belong to.
        conn.execute('''
            UPDATE messages SET request_id = (
                SELECT MAX(hr.id) FROM hire_requests hr
                WHERE hr.status IN ('accepted', 'termination_pending', 'completed')
                  AND ((hr.tutee_id = messages.sender_id AND hr.tutor_id = messages.receiver_id)
                    OR (hr.tutee_id = messages.receiver_id AND hr.tutor_id = messages.sender_id))
            ) WHERE request_id IS NULL
        ''')

        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_request ON messages(request_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hire_tutor ON hire_requests(tutor_id, status)")

        # A tutor may now take several tutees, so the old one-open-request-per-tutor
        # index must go. Capacity is enforced inside the matching transaction instead.
        conn.execute("DROP INDEX IF EXISTS uq_open_tutor")

        # A tutee can still be in at most ONE open request, guaranteed by the database.
        open_list = ", ".join(f"'{s}'" for s in OPEN_STATUSES)
        try:
            conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS uq_open_tutee ON hire_requests(tutee_id) "
                f"WHERE status IN ({open_list})"
            )
        except sqlite3.IntegrityError:
            print("WARNING: could not create uq_open_tutee: a tutee has several open requests. "
                  "Resolve them and restart.")
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
