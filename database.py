
"""SQLite locally; PostgreSQL when DATABASE_URL is set. Existing SQLite data stays in place."""
import os  # Read connection settings from environment variables.  # Reads settings such as the database path and secret key from environment variables.
import re  # Convert parameter markers outside quoted SQL text.
import sqlite3  # Keep the existing local database working.  # Opens the SQLite database and handles database errors.
from contextlib import contextmanager  # Group writes into one transaction.  # Closes connections, manages startup/shutdown and handles expected cancellation.
from pathlib import Path  # Find project files without depending on the terminal folder.  # Builds file and folder paths safely.
from dotenv import load_dotenv  # Load local settings from an optional .env file.  # Load optional settings from the local .env file.

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env', override=False)

# One shared transaction lock serializes agent decisions on PostgreSQL too.
# This simple coursework design favours correctness over parallel write throughput.
WRITE_LOCK = 412302

# Handle failures so the request or background task can finish safely.
try:
    import psycopg  # PostgreSQL driver; installed with psycopg[binary].  # Connect to PostgreSQL and handle its database errors.
except ImportError:
    psycopg = None  # SQLite can still run if the PostgreSQL driver is not installed.

# Both databases can report duplicate emails and other constraint violations.
INTEGRITY_ERRORS = (sqlite3.IntegrityError,)
if psycopg is not None:
    INTEGRITY_ERRORS += (psycopg.IntegrityError,)


# Use the configured database path, or the default project database.
def database_path():
    # The old local database remains the default. It is not uploaded or deleted.
    return os.environ.get('EVENTSPHERE_DB', str(BASE_DIR / 'eventsphere.db'))


# Check whether this connection target is a PostgreSQL URL.
def is_postgres(target):
    return str(target).startswith(('postgresql://', 'postgres://'))


# Group the related fields and behaviour.
class DatabaseRow:
    # Match the two row-access styles already used in the application.
    # Remember the database connection and the information this object needs.
    def __init__(self, names, values):
        self.names = names
        self.values = values
        self.positions = {name: i for i, name in enumerate(names)}

    # Prepare the keys helper used by this test.
    def keys(self):
        return self.names  # Lets dict(row) build the API response.

    # Prepare the   getitem   helper used by this test.
    def __getitem__(self, key):
        return self.values[key if isinstance(key, int) else self.positions[key]]

    # Prepare the   iter   helper used by this test.
    def __iter__(self):
        return iter(self.values)  # Supports: for start, end in rows.

    # Prepare the   len   helper used by this test.
    def __len__(self):
        return len(self.values)


# Build rows that support both named and numbered column access.
def postgres_row(cursor):
    if cursor.description is None:
        return lambda values: DatabaseRow([], values)
    names = [column.name for column in cursor.description]
    return lambda values: DatabaseRow(names, values)


# Convert bound parameter markers for Psycopg while preserving SQL literals.
def postgres_parameters(sql):
    # Keep ? characters inside quoted SQL strings; convert only actual markers.
    tokens = re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")", sql)
    converted = ''.join(token.replace('%', '%%') if i % 2 else token.replace('%', '%%').replace('?', '%s')
                        for i, token in enumerate(tokens))
    # Psycopg treats percent signs as parameter syntax, even inside SQL literals.
    # Escape literal percentages while preserving the markers just added.
    return converted


# Group the related fields and behaviour.
class PostgresConnection:
    # A small database wrapper keeps the existing agent SQL readable.
    dialect = 'postgresql'

    # Remember the database connection and the information this object needs.
    def __init__(self, url):
        if psycopg is None:
            raise RuntimeError('Install requirements.txt to use PostgreSQL')
        self.raw = psycopg.connect(url, row_factory=postgres_row, connect_timeout=15)
        self.raw.execute("SET TIME ZONE 'UTC'")
        self.raw.commit()

    # Prepare the execute helper used by this test.
    def execute(self, sql, parameters=()):
        # Integer 0/1 fields are kept compatible with the existing SQLite schema.
        values = tuple(int(value) if isinstance(value, bool) else value for value in parameters)
        return self.raw.execute(postgres_parameters(sql), values)

    # Prepare the commit helper used by this test.
    def commit(self):
        self.raw.commit()

    # Prepare the rollback helper used by this test.
    def rollback(self):
        self.raw.rollback()

    # Prepare the close helper used by this test.
    def close(self):
        self.raw.close()

    # Prepare the   enter   helper used by this test.
    def __enter__(self):
        return self

    # Prepare the   exit   helper used by this test.
    def __exit__(self, kind, value, traceback):
        # Handle failures so the request or background task can finish safely.
        try:
            self.rollback() if kind else self.commit()
        finally:
            self.close()


# Open a database connection and let rows be accessed by column name.
def connect(path=None):
    # An explicit path always wins. Tests therefore choose their own temporary DB.
    target = str(path) if path is not None else (os.getenv('DATABASE_URL') or database_path())
    if is_postgres(target):
        return PostgresConnection(target)
    if path is None and os.getenv('DATABASE_URL'):
        raise ValueError('DATABASE_URL must start with postgresql:// or postgres://')
    db = sqlite3.connect(target, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout = 30000')
    return db


# Lock agent writes until this transaction commits or rolls back.
def begin_write(db):
    if getattr(db, 'dialect', 'sqlite') == 'postgresql':
        # Psycopg starts the transaction before this lock query.
        # The lock is shared across app processes and released at commit/rollback.
        db.execute('SELECT pg_advisory_xact_lock(?)', (WRITE_LOCK,))
    else:
        db.execute('BEGIN IMMEDIATE')


# Commit all related changes together, or roll them back if an error occurs.
@contextmanager
def transaction(path=None):
    db = connect(path)
    # Handle failures so the request or background task can finish safely.
    try:
        begin_write(db)
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# Return the generated primary key using the selected database.
def insert_id(db, sql, parameters):
    # PostgreSQL returns generated IDs explicitly; SQLite exposes lastrowid.
    if getattr(db, 'dialect', 'sqlite') == 'postgresql':
        return db.execute(sql.rstrip().rstrip(';') + ' RETURNING id', parameters).fetchone()[0]
    return db.execute(sql, parameters).lastrowid


# Define the existing tables and the extra tables for agent state, messages, offers and notifications.
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL, role TEXT NOT NULL
    CHECK(role IN ('admin','staff','attendee'))
);
CREATE TABLE IF NOT EXISTS venues (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, location TEXT NOT NULL,
    capacity INTEGER NOT NULL, description TEXT NOT NULL, image TEXT NOT NULL,
    hourly_rate REAL NOT NULL, open_for_hire INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL,
    venue_id INTEGER NOT NULL REFERENCES venues(id), start_time TEXT NOT NULL,
    end_time TEXT NOT NULL, description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
);
CREATE TABLE IF NOT EXISTS ticket_categories (
    id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events(id),
    name TEXT NOT NULL, price REAL NOT NULL, quantity INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY, attendee_email TEXT NOT NULL,
    ticket_category_id INTEGER NOT NULL REFERENCES ticket_categories(id),
    quantity INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    booking_id INTEGER PRIMARY KEY REFERENCES bookings(id), reference TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS cancellations (
    booking_id INTEGER PRIMARY KEY REFERENCES bookings(id), refund REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL REFERENCES bookings(id),
    code TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS check_ins (
    ticket_id INTEGER PRIMARY KEY REFERENCES tickets(id)
);
CREATE TABLE IF NOT EXISTS hire_requests (
    id INTEGER PRIMARY KEY, venue_id INTEGER NOT NULL REFERENCES venues(id),
    attendee_email TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
    purpose TEXT NOT NULL, guest_count INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS agent_messages (id INTEGER PRIMARY KEY, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS waitlist (
    id INTEGER PRIMARY KEY, attendee_email TEXT NOT NULL,
    ticket_category_id INTEGER NOT NULL REFERENCES ticket_categories(id),
    UNIQUE(attendee_email, ticket_category_id)
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY, waitlist_id INTEGER NOT NULL UNIQUE REFERENCES waitlist(id),
    attendee_email TEXT NOT NULL, message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS agent_state (
    agent_name TEXT PRIMARY KEY, state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_inbox (
    sequence INTEGER PRIMARY KEY, message_id TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', reply TEXT
);
CREATE TABLE IF NOT EXISTS ticket_holds (
    id INTEGER PRIMARY KEY, waitlist_id INTEGER NOT NULL REFERENCES waitlist(id),
    attendee_email TEXT NOT NULL, ticket_category_id INTEGER NOT NULL REFERENCES ticket_categories(id),
    quantity INTEGER NOT NULL DEFAULT 1, expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', booking_id INTEGER REFERENCES bookings(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_hold_per_waiter
    ON ticket_holds(waitlist_id) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS agent_notifications (
    id INTEGER PRIMARY KEY, attendee_email TEXT NOT NULL,
    message TEXT NOT NULL, message_key TEXT NOT NULL,
    conversation_id TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(attendee_email, message_key)
);
CREATE TABLE IF NOT EXISTS ticket_scan_attempts (
    code TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0,
    repeated_scans INTEGER NOT NULL DEFAULT 0, last_attempt TEXT NOT NULL
);
"""


# Keep SQLite auto-incrementing integer keys; use identity keys on PostgreSQL.
POSTGRES_SCHEMA = re.sub(r'\bid INTEGER PRIMARY KEY',
    'id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY', SCHEMA)
POSTGRES_SCHEMA = POSTGRES_SCHEMA.replace('sequence INTEGER PRIMARY KEY',
    'sequence INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY')
POSTGRES_SCHEMA = POSTGRES_SCHEMA.replace(' REAL', ' DOUBLE PRECISION')
POSTGRES_SCHEMA = POSTGRES_SCHEMA.replace('DEFAULT CURRENT_TIMESTAMP',
    'DEFAULT (CAST(CURRENT_TIMESTAMP AS TEXT))')


# Add a missing column without deleting the existing table or records.
def add_column(db, table, column, declaration):
    # Table/column names below come only from our fixed migration list.
    if getattr(db, 'dialect', 'sqlite') == 'postgresql':
        names = {row[0] for row in db.execute(
            'SELECT column_name FROM information_schema.columns '
            'WHERE table_schema=current_schema() AND table_name=?', (table,))}
    else:
        names = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
    if column not in names:
        if getattr(db, 'dialect', 'sqlite') == 'postgresql' and declaration == 'REAL':
            declaration = 'DOUBLE PRECISION'
        db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')


# Create missing tables and add the columns needed by the new agents.
def create_tables(path=None):
    db = connect(path)
    # Handle failures so the request or background task can finish safely.
    try:
        if getattr(db, 'dialect', 'sqlite') == 'postgresql':
            begin_write(db)
            # The schema contains simple SQL statements, with no procedural blocks.
            for statement in POSTGRES_SCHEMA.split(';'):
                if statement.strip():
                    db.execute(statement)
        else:
            db.executescript(SCHEMA)
            begin_write(db)
        # Add columns without deleting existing users, events or bookings.
        add_column(db, 'bookings', 'unit_price', 'REAL')
        add_column(db, 'payments', 'amount', 'REAL')
        add_column(db, 'ticket_categories', 'total_quantity', 'INTEGER')
        add_column(db, 'waitlist', 'status', "TEXT NOT NULL DEFAULT 'waiting'")
        add_column(db, 'waitlist', 'joined_at', 'TEXT')
        add_column(db, 'waitlist', 'offer_count', 'INTEGER NOT NULL DEFAULT 0')
        add_column(db, 'hire_requests', 'quoted_price', 'REAL')
        # Old bookings did not snapshot prices; use the current category price once.
        db.execute("""UPDATE bookings SET unit_price = (
            SELECT price FROM ticket_categories WHERE id = bookings.ticket_category_id
        ) WHERE unit_price IS NULL""")
        db.execute("""UPDATE payments SET amount = (
            SELECT quantity * unit_price FROM bookings WHERE id = payments.booking_id
        ) WHERE amount IS NULL""")
        db.execute("""UPDATE ticket_categories SET total_quantity = quantity + COALESCE((
            SELECT SUM(b.quantity) FROM bookings b
            LEFT JOIN cancellations c ON c.booking_id = b.id
            WHERE b.ticket_category_id = ticket_categories.id AND c.booking_id IS NULL
        ), 0) WHERE total_quantity IS NULL""")
        db.execute('UPDATE waitlist SET joined_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE joined_at IS NULL')
        db.execute('CREATE INDEX IF NOT EXISTS waiting_order ON waitlist(ticket_category_id, status, joined_at, id)')
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == '__main__':
    create_tables()
    mode = 'PostgreSQL' if os.getenv('DATABASE_URL') else 'SQLite'
    print(mode + ' tables are ready. Existing records in this database were retained.')