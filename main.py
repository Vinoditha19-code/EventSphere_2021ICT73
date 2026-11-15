#QRcode
import qrcode
from io import BytesIO
from fastapi.responses import Response

from fastapi.responses import FileResponse #Build the staff scanner page

from agents import ask_venue_agent
import json

from agents import venue_is_free 
from contextlib import closing
import sqlite3 #opens eventsphere.py so we can find the account by email
import secrets #creates a key that protects the session cookie from being changed.

from pydantic import NaiveDatetime  #checks that the start and end values are valid local dates and times
from datetime import datetime, timedelta #check whether the event has already started

from fastapi import FastAPI,HTTPException,Request #Sends a 401 error if the email or password is wrong.
from pydantic import BaseModel #Defines what the login request should contain. Here, email and password are both text.
from pwdlib import PasswordHash#	Checks whether the password entered matches the hash saved in the database.
from starlette.middleware.sessions import SessionMiddleware #gives your app a session so it can remember the user’s role after login



app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=secrets.token_hex(32))
hasher = PasswordHash.recommended()


@app.get("/")
def home():
    return {"message": "Welcome to EventSphere"}


class Login(BaseModel):
    email: str
    password: str


@app.post("/login")
def login(data: Login,request: Request):
    db = sqlite3.connect("eventsphere.db")
    user = db.execute(
        "SELECT password_hash, role FROM users WHERE email = ?",
        (data.email,)
    ).fetchone()
    db.close()

    if user and hasher.verify(data.password, user[0]):
        request.session["role"] = user[1]
        request.session["email"] = data.email.strip().lower()
        return {"message": "Login successful", "role": user[1]}

    raise HTTPException(401, "Incorrect email or password")

@app.get("/admin")
def admin_page(request: Request):
    if request.session.get("role") != "admin":
        raise HTTPException(403, "Admin access only")

    return {"message": "Welcome, admin"}


#Attendee registration
@app.post("/register",status_code=201)
def register(data: Login):
    email = data.email.strip().lower()

    if not email or len(data.password)<8:
        raise HTTPException(400,"Enter an email and at least 8 characters")

    db = sqlite3.connect("eventsphere.db")

    try:
        db.execute(
            "INSERT INTO users (email,password_hash, role) VALUES(?,?,?)",
            (email,hasher.hash(data.password),"attendee")
        )   

        db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Email already registerd!!")
    finally:
        db.close()


#Check staff permissions
@app.get("/staff")
def staff_page(request: Request):
    if request.session.get("role") != "staff":
        raise HTTPException(403, "Staff access only")

        return{"message": "Welcome, staff"}


#Add a venue
class Venue(BaseModel):
    name: str
    location: str
    capacity: int 
    description: str
    image: str
    hourly_rate: float 
    open_for_hire: bool = False


@app.post("/venues", status_code=201)
def add_venue(venue: Venue, request: Request):
    if request.session.get("role") != "admin":
        raise HTTPException(403, "Admin access only")

    if venue.capacity < 1 or venue.hourly_rate < 0:
        raise HTTPException(400, "Invalid capacity or hourly rate")

    db = sqlite3.connect("eventsphere.db")
    db.execute("""
        INSERT INTO venues
        (name, location, capacity, description, image, hourly_rate, open_for_hire)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        venue.name, venue.location, venue.capacity, venue.description,
        venue.image, venue.hourly_rate, venue.open_for_hire
    ))
    db.commit()
    db.close()

    return {"message": "Venue added"}

    
#edit venue
@app.put("/venues/{venue_id}")
def edit_venue(venue_id: int, details: Venue, request: Request):
    if request.session.get("role") != "admin":
        raise HTTPException(403, "Admin access only")

    if details.capacity < 1 or details.hourly_rate < 0:
        raise HTTPException(400, "Invalid capacity or hourly rate")

    db = sqlite3.connect("eventsphere.db")
    result = db.execute("""
        UPDATE venues
        SET name = ?, location = ?, capacity = ?, description = ?,
            image = ?, hourly_rate = ?, open_for_hire = ?
        WHERE id = ?
    """, (
        details.name, details.location, details.capacity, details.description,
        details.image, details.hourly_rate, details.open_for_hire, venue_id
    ))
    db.commit()
    db.close()

    if result.rowcount == 0:
        raise HTTPException(404, "Venue not found")

    return {"message": "Venue updated"}

#Create a draft event with tickets
class Event(BaseModel):
    title: str
    category: str
    venue_id: int
    start_time: NaiveDatetime
    end_time: NaiveDatetime
    description: str
    ticket_name: str
    ticket_price: float
    ticket_quantity: int

@app.post("/events", status_code=201)
def add_event(event: Event, request: Request):
    if request.session.get("role") != "admin":
        raise HTTPException(403, "Admin access only")

    if event.end_time <= event.start_time:
        raise HTTPException(400, "End time must be after start time")

    if event.ticket_quantity < 1 or event.ticket_price < 0:
        raise HTTPException(400, "Invalid ticket quantity or price")

    db = sqlite3.connect("eventsphere.db")

    try:
        hall = db.execute(
            "SELECT capacity FROM venues WHERE id = ?", (event.venue_id,)
        ).fetchone()

        if hall is None:
            raise HTTPException(404, "Venue not found")

        if event.ticket_quantity > hall[0]:
            raise HTTPException(400, "Too many tickets for this venue")

        saved_event = db.execute("""
            INSERT INTO events
            (title, category, venue_id, start_time, end_time, description)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            event.title, event.category, event.venue_id,
            event.start_time.isoformat(), event.end_time.isoformat(),
            event.description
        ))

        event_id = saved_event.lastrowid
        db.execute("""
            INSERT INTO ticket_categories (event_id, name, price, quantity)
            VALUES (?, ?, ?, ?)
        """, (event_id, event.ticket_name, event.ticket_price, event.ticket_quantity))

        db.commit()
    finally:
        db.close()

    return {"message": "Draft event created", "event_id": event_id}

#Publish an event
@app.post("/events/{event_id}/publish")
def publish_event(event_id: int, request: Request):
    if request.session.get("role") != "admin":
        raise HTTPException(403, "Admin access only")

    with closing(sqlite3.connect("eventsphere.db")) as db:
        db.execute("BEGIN IMMEDIATE")

        event = db.execute(
            "SELECT venue_id, start_time, end_time, status FROM events WHERE id = ?",
            (event_id,)
        ).fetchone()

        if event is None:
            raise HTTPException(404, "Event not found")

        venue_id, start, end, status = event

        if status != "draft":
            raise HTTPException(400, "Only drafts can be published")

        if not ask_venue_agent(db, venue_id, start, end):
            db.commit()
            raise HTTPException(409, "Venue is already booked")

        db.execute("UPDATE events SET status = 'published' WHERE id = ?", (event_id,))
        db.commit()

    return {"message": "Event published"}

#Cancel a published event
@app.post("/events/{event_id}/cancel")
def cancel_event(event_id: int, request: Request):
    if request.session.get("role") != "admin":
        raise HTTPException(403, "Admin access only")

    with closing(sqlite3.connect("eventsphere.db")) as db:
        result = db.execute("""
            UPDATE events
            SET status = 'cancelled'
            WHERE id = ? AND status = 'published'
        """, (event_id,))

        if result.rowcount == 0:
            raise HTTPException(404, "Published event not found")

        db.commit()

    return {"message": "Event cancelled"}

#Search and filter events
@app.get("/events")
def list_events(search: str = "", category: str = ""):
    with closing(sqlite3.connect("eventsphere.db")) as db:
        db.row_factory = sqlite3.Row

        events = db.execute("""
            SELECT id, title, category, start_time, end_time
            FROM events
            WHERE status = 'published'
              AND LOWER(title) LIKE ?
              AND (? = '' OR LOWER(category) = ?)
            ORDER BY start_time
        """, (
            "%" + search.lower() + "%",
            category.lower(),
            category.lower()
        )).fetchall()

    return [dict(event) for event in events]

#Show event details and tickets
@app.get("/events/{event_id}")
def event_details(event_id: int):
    with closing(sqlite3.connect("eventsphere.db")) as db:
        db.row_factory = sqlite3.Row

        event = db.execute("""
            SELECT * FROM events
            WHERE id = ? AND status = 'published'
        """, (event_id,)).fetchone()

        if event is None:
            raise HTTPException(404, "Published event not found")

        tickets = db.execute("""
            SELECT id, name, price, quantity
            FROM ticket_categories WHERE event_id = ?
        """, (event_id,))

        return {"event": dict(event), "tickets": [dict(ticket) for ticket in tickets]}

#booking
#2 people can't buy one ticket
class Booking(BaseModel):
    ticket_category_id: int
    quantity: int


@app.post("/bookings", status_code=201)
def book_tickets(booking: Booking, request: Request):
    email = request.session.get("email")

    if not email or request.session.get("role") != "attendee":
        raise HTTPException(403, "Sign in as an attendee")

    category_id = booking.ticket_category_id
    quantity = booking.quantity

    if quantity < 1:
        raise HTTPException(400, "Choose at least one ticket")

    with closing(sqlite3.connect("eventsphere.db")) as db:
        update = db.execute("""
            UPDATE ticket_categories
            SET quantity = quantity - ?
            WHERE id = ? AND quantity >= ?
              AND event_id IN (SELECT id FROM events WHERE status = 'published')
        """, (quantity, category_id, quantity))

        if update.rowcount == 0:
            raise HTTPException(409, "Tickets unavailable")

        booking_row = db.execute("""
            INSERT INTO bookings (attendee_email, ticket_category_id, quantity)
            VALUES (?, ?, ?)
        """, (email, category_id, quantity))
        booking_id = booking_row.lastrowid

        payment_reference = "PAY-" + secrets.token_hex(4).upper()
        db.execute(
            "INSERT INTO payments (booking_id, reference) VALUES (?, ?)",
            (booking_id, payment_reference)
        )

        ticket_codes = []

        for _ in range(quantity):
            code = secrets.token_urlsafe(24)
            db.execute(
                "INSERT INTO tickets (booking_id, code) VALUES (?, ?)",
                (booking_id, code)
            )
            ticket_codes.append(code)

        db.commit()

    return {
        "message": "Booking created",
        "booking_id": booking_id,
        "payment_reference": payment_reference,
        "ticket_codes": ticket_codes
    }
# Show booking history
@app.get("/bookings")
def booking_history(request: Request):
    email = request.session.get("email")

    if not email or request.session.get("role") != "attendee":
        raise HTTPException(403, "Sign in as an attendee")

    with closing(sqlite3.connect("eventsphere.db")) as db:
        db.row_factory = sqlite3.Row

        bookings = db.execute("""
            SELECT b.id AS booking_id, e.title, b.quantity,
                   p.reference AS payment_reference,
                   CASE WHEN c.booking_id IS NULL THEN 'active'
                        ELSE 'cancelled' END AS status,
                   c.refund
            FROM bookings AS b
            JOIN ticket_categories AS t ON t.id = b.ticket_category_id
            JOIN events AS e ON e.id = t.event_id
            LEFT JOIN payments AS p ON p.booking_id = b.id
            LEFT JOIN cancellations AS c ON c.booking_id = b.id
            WHERE b.attendee_email = ?
            ORDER BY b.id DESC
        """, (email,))

        return [dict(booking) for booking in bookings]


# Cancel and refund
@app.post("/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: int, request: Request):
    email = request.session.get("email")

    if not email or request.session.get("role") != "attendee":
        raise HTTPException(403, "Sign in as an attendee")

    with closing(sqlite3.connect("eventsphere.db")) as db:
        db.execute("BEGIN IMMEDIATE")

        booking = db.execute("""
            SELECT t.id, b.quantity, t.price, e.start_time, p.reference
            FROM bookings AS b
            JOIN ticket_categories AS t ON t.id = b.ticket_category_id
            JOIN events AS e ON e.id = t.event_id
            LEFT JOIN payments AS p ON p.booking_id = b.id
            WHERE b.id = ? AND b.attendee_email = ?
        """, (booking_id, email)).fetchone()

        if booking is None:
            raise HTTPException(404, "Booking not found")

        category_id, quantity, price, start_time, payment = booking

        if datetime.now() >= datetime.fromisoformat(start_time):
            raise HTTPException(400, "The event has already started")

        refund = 0
        if payment:
            refund = round(quantity * price, 2)

        cancellation = db.execute("""
            INSERT OR IGNORE INTO cancellations (booking_id, refund)
            VALUES (?, ?)
        """, (booking_id, refund))

        if cancellation.rowcount == 0:
            raise HTTPException(409, "Booking already cancelled")

        db.execute("""
            UPDATE ticket_categories SET quantity = quantity + ? WHERE id = ?
        """, (quantity, category_id))
        db.commit()

    return {"message": "Booking cancelled", "refund": refund}

#QR code
@app.get("/tickets/{code}/qr")
def ticket_qr(code: str, request: Request):
    email = request.session.get("email")

    if not email or request.session.get("role") != "attendee":
        raise HTTPException(403, "Sign in as an attendee")

    with closing(sqlite3.connect("eventsphere.db")) as db:
        ticket = db.execute("""
            SELECT t.code
            FROM tickets AS t
            JOIN bookings AS b ON b.id = t.booking_id
            LEFT JOIN cancellations AS c ON c.booking_id = b.id
            WHERE t.code = ? AND b.attendee_email = ?
              AND c.booking_id IS NULL
        """, (code, email)).fetchone()

    if ticket is None:
        raise HTTPException(404, "Active ticket not found")

    image = BytesIO()
    qrcode.make(code).save(image, format="PNG")

    return Response(content=image.getvalue(), media_type="image/png")

#Staff check-in
class CheckIn(BaseModel):
    code: str
    event_id: int


@app.post("/check-in")
def check_in(data: CheckIn, request: Request):
    if request.session.get("role") != "staff":
        raise HTTPException(403, "Staff access only")

    with closing(sqlite3.connect("eventsphere.db")) as db:
        db.execute("BEGIN IMMEDIATE")

        ticket = db.execute("""
            SELECT t.id, e.id, e.status, c.booking_id, e.start_time, e.end_time 
            FROM tickets AS t
            JOIN bookings AS b ON b.id = t.booking_id
            JOIN ticket_categories AS tc ON tc.id = b.ticket_category_id
            JOIN events AS e ON e.id = tc.event_id
            LEFT JOIN cancellations AS c ON c.booking_id = b.id
            WHERE t.code = ?
        """, (data.code,)).fetchone()

        if ticket is None:
            raise HTTPException(404, "Ticket code not found")

        ticket_id, event_id, status, cancelled_booking, start_time, end_time = ticket

        if event_id != data.event_id:
            raise HTTPException(400, "Ticket is for a different event")

        if status != "published" or cancelled_booking is not None:
            raise HTTPException(400, "Ticket is no longer active")

#Checkin time rule
        opens_at = datetime.fromisoformat(start_time) - timedelta(minutes=30)
        closes_at = datetime.fromisoformat(end_time)

        if not opens_at <= datetime.now() < closes_at:
            raise HTTPException(400, "Outside check-in hours")

        result = db.execute(
            "INSERT OR IGNORE INTO check_ins (ticket_id) VALUES (?)",
            (ticket_id,)
        )

        if result.rowcount == 0:
            raise HTTPException(409, "Ticket already used")

        db.commit()

    return {"message": "Check-in successful"}

#Build the staff scanner page
@app.get("/scanner")
def scanner_page(request: Request):
    if request.session.get("role") != "staff":
        raise HTTPException(403, "Staff access only")

    return FileResponse("scanner.html")

#request hire 
class HireRequest(BaseModel):
    venue_id: int
    start_time: NaiveDatetime
    end_time: NaiveDatetime
    purpose: str
    guest_count: int

@app.post("/hires", status_code=201)
def request_hire(hire: HireRequest, request: Request):
    email = request.session.get("email")

    if not email or request.session.get("role") != "attendee":
        raise HTTPException(403, "Sign in as an attendee")

    if hire.end_time <= hire.start_time or hire.guest_count < 1:
        raise HTTPException(400, "Check the times and guest count")

    with closing(sqlite3.connect("eventsphere.db")) as db:
        db.execute("BEGIN IMMEDIATE")

        venue = db.execute(
            "SELECT capacity, open_for_hire, hourly_rate FROM venues WHERE id = ?",
            (hire.venue_id,)
        ).fetchone()

        if venue is None:
            raise HTTPException(404, "Venue not found")

        capacity, open_for_hire, hourly_rate = venue

        if not open_for_hire:
            raise HTTPException(400, "Venue is closed for private hire")

        if hire.guest_count > capacity:
            raise HTTPException(400, "Too many guests for this venue")

        start = hire.start_time.isoformat()
        end = hire.end_time.isoformat()

        if not venue_is_free(db, hire.venue_id, start, end):
            for day in range(1, 8):
                new_start = (hire.start_time + timedelta(days=day)).isoformat()
                new_end = (hire.end_time + timedelta(days=day)).isoformat()

            if venue_is_free(db, hire.venue_id, new_start, new_end):
                    raise HTTPException(409, detail={
                        "message": "Venue is booked. Try this time instead.",
                        "suggested_start": new_start,
                        "suggested_end": new_end
                    })

            raise HTTPException(409, "No matching time found in the next 7 days")

        hours = (hire.end_time - hire.start_time).total_seconds() / 3600
        price = round(hours * hourly_rate, 2)

        status = "pending"
        if hours <= 3:
            status = "approved"

        result = db.execute("""
            INSERT INTO hire_requests
            (venue_id, attendee_email, start_time, end_time, purpose, guest_count, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (hire.venue_id, email, start, end, hire.purpose, hire.guest_count, status))

        db.commit()

    return {"hire_id": result.lastrowid, "status": status, "price": price}

#admin approve or reject pending hires
class HireDecision(BaseModel):
    approve: bool


@app.post("/hires/{hire_id}/review")
def review_hire(hire_id: int, decision: HireDecision, request: Request):
    if request.session.get("role") != "admin":
        raise HTTPException(403, "Admin access only")

    with closing(sqlite3.connect("eventsphere.db")) as db:
        db.execute("BEGIN IMMEDIATE")

        hire = db.execute("""
            SELECT venue_id, start_time, end_time
            FROM hire_requests
            WHERE id = ? AND status = 'pending'
        """, (hire_id,)).fetchone()

        if hire is None:
            raise HTTPException(404, "Pending hire not found")

        status = "rejected"

        if decision.approve:
            venue_id, start, end = hire

            if not venue_is_free(db, venue_id, start, end):
                raise HTTPException(409, "Venue is already booked at that time")

            status = "approved"

        db.execute(
            "UPDATE hire_requests SET status = ? WHERE id = ?",
            (status, hire_id)
        )
        db.commit()

    return {"hire_id": hire_id, "status": status}


#Show ticket sales and check-ins
@app.get("/admin/events/{event_id}/summary")
def event_summary(event_id: int, request: Request):
    if request.session.get("role") != "admin":
        raise HTTPException(403, "Admin access only")

    with closing(sqlite3.connect("eventsphere.db")) as db:
        event = db.execute("""
            SELECT e.title, v.capacity
            FROM events AS e
            JOIN venues AS v ON v.id = e.venue_id
            WHERE e.id = ?
        """, (event_id,)).fetchone()

        if event is None:
            raise HTTPException(404, "Event not found")

        title, capacity = event

        tickets_sold, revenue = db.execute("""
            SELECT COALESCE(SUM(b.quantity), 0),
                   COALESCE(SUM(CASE WHEN p.booking_id IS NOT NULL
                                    THEN b.quantity * t.price ELSE 0 END), 0)
            FROM bookings AS b
            JOIN ticket_categories AS t ON t.id = b.ticket_category_id
            LEFT JOIN payments AS p ON p.booking_id = b.id
            LEFT JOIN cancellations AS c ON c.booking_id = b.id
            WHERE t.event_id = ? AND c.booking_id IS NULL
        """, (event_id,)).fetchone()

        check_ins = db.execute("""
            SELECT COUNT(*)
            FROM check_ins AS c
            JOIN tickets AS t ON t.id = c.ticket_id
            JOIN bookings AS b ON b.id = t.booking_id
            JOIN ticket_categories AS tc ON tc.id = b.ticket_category_id
            WHERE tc.event_id = ?
        """, (event_id,)).fetchone()[0]

    return {
        "event": title,
        "venue_capacity": capacity,
        "tickets_sold": tickets_sold,
        "simulated_revenue": round(revenue, 2),
        "check_ins": check_ins
    }

@app.get("/admin/agent-messages")
def agent_messages(request: Request):
    if request.session.get("role") != "admin":
        raise HTTPException(403, "Admin access only")

    with closing(sqlite3.connect("eventsphere.db")) as db:
        rows = db.execute(
            "SELECT message FROM agent_messages ORDER BY id"
        ).fetchall()

    return [json.loads(row[0]) for row in rows]

#Let an attendee join the waitlist
class WaitlistRequest(BaseModel):
    ticket_category_id: int


@app.post("/waitlist", status_code=201)
def join_waitlist(data: WaitlistRequest, request: Request):
    email = request.session.get("email")
    if not email or request.session.get("role") != "attendee":
        raise HTTPException(403, "Sign in as an attendee")

    with closing(sqlite3.connect("eventsphere.db")) as db:
        result = db.execute("""
            INSERT OR IGNORE INTO waitlist (attendee_email, ticket_category_id)
            SELECT ?, t.id
            FROM ticket_categories AS t
            JOIN events AS e ON e.id = t.event_id
            WHERE t.id = ? AND t.quantity = 0 AND e.status = 'published'
        """, (email, data.ticket_category_id))

        if result.rowcount == 0:
            raise HTTPException(409, "Choose a sold-out category you haven't joined yet")

        db.commit()

    return {"message": "Joined the waitlist"}