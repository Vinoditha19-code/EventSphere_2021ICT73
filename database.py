import sqlite3


def create_tables():
    db = sqlite3.connect("eventsphere.db")
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'staff', 'attendee'))
        )
    """)

    #Venue
    db.execute("""
        CREATE TABLE IF NOT EXISTS venues (
             id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            location TEXT NOT NULL,
            capacity INTEGER NOT NULL,
            description TEXT NOT NULL,
            image TEXT NOT NULL,
            hourly_rate REAL NOT NULL,
            open_for_hire INTEGER NOT NULL DEFAULT 0
        )
    """)

    #Event management
    db.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            venue_id INTEGER NOT NULL REFERENCES venues(id),
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft'
        )
    """)

    #Ticket categories
    db.execute("""
        CREATE TABLE IF NOT EXISTS ticket_categories (
            id INTEGER PRIMARY KEY,
            event_id INTEGER NOT NULL REFERENCES events(id),
            name TEXT NOT NULL,
            price REAL NOT NULL,
            quantity INTEGER NOT NULL
        )
    """)

    #Book tickets
    db.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY,
            attendee_email TEXT NOT NULL,
            ticket_category_id INTEGER NOT NULL REFERENCES ticket_categories(id),
            quantity INTEGER NOT NULL
        )
    """)

    #Simulated payment
    db.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            booking_id INTEGER PRIMARY KEY REFERENCES bookings(id),
            reference TEXT NOT NULL UNIQUE
        )
    """)

    #Cancel a booking and record a refund
    db.execute("""
        CREATE TABLE IF NOT EXISTS cancellations (
            booking_id INTEGER PRIMARY KEY REFERENCES bookings(id),
            refund REAL NOT NULL
        )
    """)

    #Create a unique code for each ticket
    db.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY,
            booking_id INTEGER NOT NULL REFERENCES bookings(id),
            code TEXT NOT NULL UNIQUE
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS check_ins (
            ticket_id INTEGER PRIMARY KEY REFERENCES tickets(id)
        )
    """)

    #request to hire venue
    db.execute("""
        CREATE TABLE IF NOT EXISTS hire_requests (
            id INTEGER PRIMARY KEY,
            venue_id INTEGER NOT NULL REFERENCES venues(id),
            attendee_email TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            purpose TEXT NOT NULL,
            guest_count INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS agent_messages (
            id INTEGER PRIMARY KEY,
            message TEXT NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS waitlist (
            id INTEGER PRIMARY KEY,
            attendee_email TEXT NOT NULL,
            ticket_category_id INTEGER NOT NULL REFERENCES ticket_categories(id),
            UNIQUE (attendee_email, ticket_category_id)
        )
    """)

    db.commit()
    db.close()