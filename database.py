import sqlite3
from datetime import datetime, timedelta, timezone

DB_NAME = "tutor_matching.db"

# A request in one of these states "occupies" both the tutee and the tutor.
OPEN_STATUSES = ("pending", "accepted", "termination_pending")


def utcnow_str(hours=0):
    """UTC timestamp in the same format SQLite's CURRENT_TIMESTAMP uses."""
    dt = datetime.now(timezone.utc) + timedelta(hours=hours)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


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
    cursor = conn.cursor()

    cursor.execute('''
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

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS profiles (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT NOT NULL,
            subject TEXT NOT NULL,
            budget_or_rate REAL NOT NULL,
            schedule TEXT NOT NULL,
            location TEXT NOT NULL,
            modality TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    cursor.execute(HIRE_REQUESTS_DDL)

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS activity_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    cursor.execute('''
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

    # ---- migrations for databases created by the old version ----
    _add_column(conn, "users", "last_seen", "TIMESTAMP")
    _add_column(conn, "messages", "request_id", "INTEGER")
    _migrate_hire_requests(conn)

    # Offers that already timed out must not block the unique indexes below.
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

    # Database-level guarantee: a tutee and a tutor can each be in at most ONE
    # open request at a time, even if two matches are triggered simultaneously.
    open_list = ", ".join(f"'{s}'" for s in OPEN_STATUSES)
    for name, col in (("uq_open_tutee", "tutee_id"), ("uq_open_tutor", "tutor_id")):
        try:
            conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON hire_requests({col}) "
                f"WHERE status IN ({open_list})"
            )
        except sqlite3.IntegrityError:
            print(f"WARNING: could not create {name}: existing data has several open "
                  f"requests for the same user. Resolve them and restart.")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
