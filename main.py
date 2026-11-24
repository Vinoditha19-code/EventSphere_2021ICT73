"""EventSphere HTTP routes. Business decisions belong to the five agents."""
import asyncio  # Runs the waitlist timer in the background without blocking web requests.
import json  # Converts agent messages and saved agent state between Python values and JSON text.
import logging  # Records background errors in the terminal so we can find problems.
import os  # Reads settings such as the database path and secret key from environment variables.
import secrets  # Creates random session keys, payment references and ticket codes.
import sqlite3  # Opens the SQLite database and handles database errors.
from contextlib import closing, asynccontextmanager, suppress  # Closes connections, manages startup/shutdown and handles expected cancellation.
from datetime import datetime, timedelta  # Works with event dates, booking times and time gaps.
from io import BytesIO  # Treats image bytes as a file in memory.
from pathlib import Path  # Builds file and folder paths safely.
import qrcode  # Creates QR images from the unique ticket codes.
from fastapi import FastAPI, HTTPException, Request, Depends  # Creates the web API, reads requests and sends HTTP error messages.
from fastapi.responses import Response, FileResponse, RedirectResponse  # Returns images, HTML files and redirects to the browser.
from fastapi.staticfiles import StaticFiles  # Lets the browser load saved images from the uploads folder.
from pydantic import BaseModel, NaiveDatetime, ConfigDict  # Checks incoming fields, local date/time values and model settings.
from pwdlib import PasswordHash  # Hashes passwords and checks a password against its saved hash.
from starlette.middleware.sessions import SessionMiddleware  # Stores signed login-session data in the browser cookie.
from PIL import Image, UnidentifiedImageError  # Opens, resizes and saves uploaded images and detects invalid images.
from database import BASE_DIR, connect, create_tables, INTEGRITY_ERRORS  # Uses the shared database path, connections, transactions and table setup.
from agents import run_command, check_waitlist, campus_now  # Uses the agents to make decisions and check waiting attendees.
import shutil  # Copies existing photos into persistent storage.

# Check the waitlist repeatedly while the application is running.
async def waitlist_loop():
    while True:
        # Handle failures so the request or background task can finish safely.
        try:
            await asyncio.to_thread(check_waitlist)
        except Exception:
            logging.exception('Waitlist check failed')
        await asyncio.sleep(15)


# Prepare the database on startup and stop the timer cleanly on shutdown.
@asynccontextmanager
async def lifespan(app):
    create_tables()
    task = asyncio.create_task(waitlist_loop())
    # Handle failures so the request or background task can finish safely.
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


# Reject write requests that do not contain the session’s security token.
async def csrf_guard(request: Request):
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        expected = request.session.get('csrf_token')
        received = request.headers.get('X-CSRF-Token', '')
        if not expected or not secrets.compare_digest(expected, received):
            raise HTTPException(403, 'Refresh this page and try again (CSRF token required)')


# Load a stable signing key so local sessions survive a server restart.
def session_secret():
    value = os.getenv('SESSION_SECRET')
    if value:
        return value
    # A stable local-development key; never commit this file to GitHub.
    path = BASE_DIR / '.session_secret'
    # Handle failures so the request or background task can finish safely.
    try:
        with path.open('x') as file:
            value = secrets.token_hex(32)
            file.write(value)
            return value
    except FileExistsError:
        return path.read_text().strip()


# Create the FastAPI application and apply the security check to its routes.
app = FastAPI(lifespan=lifespan, dependencies=[Depends(csrf_guard)])
# Railway provides the persistent volume path; locally use the project folder.
storage_root = Path(os.getenv('RAILWAY_VOLUME_MOUNT_PATH') or BASE_DIR)
UPLOAD_DIR = storage_root / 'uploads'
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Copy existing repository photos once, without replacing stored photos.
original_uploads = BASE_DIR / 'uploads'
if original_uploads.exists() and original_uploads.resolve() != UPLOAD_DIR.resolve():
    for photo in original_uploads.iterdir():
        destination = UPLOAD_DIR / photo.name
        if photo.is_file() and not destination.exists():
            shutil.copy2(photo, destination)

# Image URLs stay the same, even when the files use persistent storage.
app.mount('/uploads', StaticFiles(directory=UPLOAD_DIR), name='uploads')
app.add_middleware(SessionMiddleware, secret_key=session_secret(), same_site='lax',
                   https_only=os.getenv('HTTPS_ONLY', 'false').lower()=='true')
hasher = PasswordHash.recommended()


# Allow the operation only when a signed-in user has the required role.
def require_role(request, role):
    email = request.session.get('email')
    # Stop here if the signed-in user does not have permission.
    if not email or request.session.get('role') != role:
        raise HTTPException(403, 'Sign in as ' + role)
    return email


# Send the web request to an agent and convert refusals into HTTP errors.
def command(receiver, action, **data):
    reply = run_command(receiver, {'action':action, **data})
    content = reply['content']
    if reply['performative'] != 'AGREE':
        if reply['performative'] == 'PROPOSE':
            detail = {'message':content['reason'], 'suggested_start':content['suggested_start'],
                      'suggested_end':content['suggested_end']}
        else:
            detail = content['reason']
        raise HTTPException(content.get('http_status',400), detail)
    return content


# Give the current browser session its security token for write requests.
@app.get('/csrf')
def csrf_token(request: Request):
    token = request.session.setdefault('csrf_token', secrets.token_urlsafe(32))
    return {'csrf_token':token}


# Send the attendee and sign-in HTML page to the browser.
@app.get("/")
def home():
    return FileResponse(BASE_DIR / "index.html")


# Public session information; private records still require their original roles.
@app.get('/session')
def session_status(request: Request):
    role = request.session.get('role')
    signed_in = bool(request.session.get('email')) and role in {'admin', 'staff', 'attendee'}
    return {'authenticated': signed_in, 'role': role if signed_in else None}


# Public venue catalogue. Do not expose private hire requests or attendee information.
@app.get('/public/venues')
def public_venues():
    with closing(connect()) as db:
        rows = db.execute("""
            SELECT id, name, location, capacity, description, image,
                   hourly_rate, open_for_hire
            FROM venues ORDER BY name
        """).fetchall()
    return [dict(row) for row in rows]


# Fields the browser must send when signing in or registering.
class Login(BaseModel):
    email: str
    password: str


# Check the account password and remember the signed-in user’s email and role.
@app.post("/login")
def login(data: Login, request: Request):
    email = data.email.strip().lower()

    db = connect()
    user = db.execute(
        "SELECT password_hash, role FROM users WHERE email = ?",
        (email,)
    ).fetchone()
    db.close()

    if user and hasher.verify(data.password, user[0]):
        request.session["role"] = user[1]
        request.session["email"] = email
        return {"message": "Login successful", "role": user[1]}

    raise HTTPException(401, "Incorrect email or password")


# Check that the signed-in user has admin access.
@app.get("/admin")
def admin_page(request: Request):
    # Stop here if the signed-in user does not have permission.
    if request.session.get("role") != "admin":
        raise HTTPException(403, "Admin access only")

    return {"message": "Welcome, admin"}


# Attendee registration
# Create an attendee account after checking the email, password and duplicate accounts.
@app.post("/register", status_code=201)
def register(data: Login):
    email = data.email.strip().lower()

    if not email or len(data.password) < 8:
        raise HTTPException(400, "Enter an email and at least 8 characters")

    db = connect()

    # Handle failures so the request or background task can finish safely.
    try:
        db.execute(
            "INSERT INTO users (email, password_hash, role) VALUES (?, ?, ?)",
            (email, hasher.hash(data.password), "attendee")
        )
        db.commit()
    except INTEGRITY_ERRORS:
        raise HTTPException(409, "Email already registered")
    finally:
        db.close()

    return {"message": "Attendee account created"}


#Check staff permissions


#Add a venue
# Fields needed to add or update a venue.
class Venue(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    name: str
    location: str
    capacity: int 
    description: str
    image: str
    hourly_rate: float 
    open_for_hire: bool = False


# Ask VenueAgent to save a venue after checking admin access.
@app.post("/venues", status_code=201)
def add_venue(venue: Venue, request: Request):
    require_role(request, 'admin')
    return command('VenueAgent', 'save_venue', **venue.model_dump(mode='json'))

    
#edit venue
# Ask VenueAgent to update a venue and check its capacity rules.
@app.put("/venues/{venue_id}")
def edit_venue(venue_id: int, details: Venue, request: Request):
    require_role(request, 'admin')
    return command('VenueAgent', 'edit_venue', venue_id=venue_id, **details.model_dump(mode='json'))

#Create a draft event with tickets
# Fields needed to create a draft event and its initial tickets.
class Event(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    title: str
    category: str
    venue_id: int
    start_time: NaiveDatetime
    end_time: NaiveDatetime
    description: str
    ticket_name: str
    ticket_price: float
    ticket_quantity: int

# Ask VenueAgent to create a draft event and its ticket category.
@app.post("/events", status_code=201)
def add_event(event: Event, request: Request):
    require_role(request, 'admin')
    return command('VenueAgent', 'create_event', **event.model_dump(mode='json'))

#Publish an event
# Check admin access and ask the agents to publish an available event.
@app.post("/events/{event_id}/publish")
def publish_event(event_id: int, request: Request):
    require_role(request, 'admin')
    return command('VenueAgent', 'publish', event_id=event_id)

#Cancel a published event
# Ask BookingAgent to cancel the event and refund active bookings.
@app.post("/events/{event_id}/cancel")
def cancel_event(event_id: int, request: Request):
    require_role(request, 'admin')
    return command('BookingAgent', 'cancel_event', event_id=event_id)

#Search and filter events
# Find published events using the optional title and category filters.
@app.get("/events")
def list_events(search: str = "", category: str = ""):
    # Use the database connection for this block and close it afterward.
    with closing(connect()) as db:

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
# Return a published event and its ticket-category availability.
@app.get("/events/{event_id}")
def event_details(event_id: int):
    # Use the database connection for this block and close it afterward.
    with closing(connect()) as db:

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
# Ticket category and number of tickets requested by an attendee.
class Booking(BaseModel):
    ticket_category_id: int
    quantity: int


# Ask BookingAgent to allocate tickets for the signed-in attendee.
@app.post("/bookings", status_code=201)
def book_tickets(booking: Booking, request: Request):
    email = require_role(request, 'attendee')
    return command('BookingAgent', 'book', email=email, **booking.model_dump())
# Show booking history
# Return only the signed-in attendee’s bookings and refund details.
@app.get("/bookings")
def booking_history(request: Request):
    email = request.session.get("email")

    # Stop here if the signed-in user does not have permission.
    if not email or request.session.get("role") != "attendee":
        raise HTTPException(403, "Sign in as an attendee")

    # Use the database connection for this block and close it afterward.
    with closing(connect()) as db:

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
# Ask BookingAgent to cancel the attendee’s own booking.
@app.post("/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: int, request: Request):
    email = require_role(request, 'attendee')
    return command('BookingAgent', 'cancel', email=email, booking_id=booking_id)

#QR code
# Return a QR image only when this attendee owns a valid ticket.
@app.get("/tickets/{code}/qr")
def ticket_qr(code: str, request: Request):
    email = request.session.get("email")

    # Stop here if the signed-in user does not have permission.
    if not email or request.session.get("role") != "attendee":
        raise HTTPException(403, "Sign in as an attendee")

    # Use the database connection for this block and close it afterward.
    with closing(connect()) as db:
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
# Event ID and scanned or typed ticket code sent by staff.
class CheckIn(BaseModel):
    code: str
    event_id: int


# Allow staff to ask TicketAgent to validate and admit a ticket.
@app.post("/check-in")
def check_in(data: CheckIn, request: Request):
    require_role(request, 'staff')
    return command('TicketAgent', 'scan', **data.model_dump())

#Build the staff scanner page
# Check that the signed-in user has staff access.
@app.get("/staff")
def staff_page(request: Request):
    # Stop here if the signed-in user does not have permission.
    if request.session.get("role") != "staff":
        raise HTTPException(403, "Staff access only")

    return {"message": "Welcome, staff"}

#request hire 
# Venue, local dates, purpose and guest count for a private hire.
class HireRequest(BaseModel):
    venue_id: int
    start_time: NaiveDatetime
    end_time: NaiveDatetime
    purpose: str
    guest_count: int


# Ask VenueAgent to check the dates, capacity, price and hire decision.
@app.post("/hires", status_code=201)
def request_hire(hire: HireRequest, request: Request):
    email = require_role(request, 'attendee')
    return command('VenueAgent', 'hire', email=email, **hire.model_dump(mode='json'))

#admin approve or reject pending hires
# The admin’s approve-or-reject choice.
class HireDecision(BaseModel):
    approve: bool


# Allow an admin to approve or reject a pending venue hire.
@app.post("/hires/{hire_id}/review")
def review_hire(hire_id: int, decision: HireDecision, request: Request):
    require_role(request, 'admin')
    return command('VenueAgent', 'review', hire_id=hire_id, approve=decision.approve)


#Show ticket sales and check-ins
# Calculate active ticket sales, simulated revenue and recorded check-ins.
@app.get("/admin/events/{event_id}/summary")
def event_summary(event_id: int, request: Request):
    require_role(request, 'admin')
    # Use the database connection for this block and close it afterward.
    with closing(connect()) as db:
        event=db.execute('SELECT e.title,v.capacity FROM events e JOIN venues v ON v.id=e.venue_id WHERE e.id=?',(event_id,)).fetchone()
        if not event:
            raise HTTPException(404,'Event not found')
        sold=db.execute('SELECT COALESCE(SUM(b.quantity),0),COALESCE(SUM(p.amount),0) FROM bookings b JOIN ticket_categories t ON t.id=b.ticket_category_id LEFT JOIN payments p ON p.booking_id=b.id LEFT JOIN cancellations c ON c.booking_id=b.id WHERE t.event_id=? AND c.booking_id IS NULL',(event_id,)).fetchone()
        scans=db.execute('SELECT COUNT(*) FROM check_ins c JOIN tickets tk ON tk.id=c.ticket_id JOIN bookings b ON b.id=tk.booking_id JOIN ticket_categories t ON t.id=b.ticket_category_id WHERE t.event_id=?',(event_id,)).fetchone()[0]
        return {'event':event['title'],'venue_capacity':event['capacity'],'tickets_sold':sold[0],'simulated_revenue':round(sold[1],2),'check_ins':scans}

# Allow admins to read the saved JSON agent conversations.
@app.get("/admin/agent-messages")
def agent_messages(request: Request):
    # Stop here if the signed-in user does not have permission.
    if request.session.get("role") != "admin":
        raise HTTPException(403, "Admin access only")

    # Use the database connection for this block and close it afterward.
    with closing(connect()) as db:
        rows = db.execute(
            "SELECT message FROM agent_messages ORDER BY id"
        ).fetchall()

    return [json.loads(row[0]) for row in rows]

#Let an attendee join the waitlist
# The sold-out ticket category the attendee wants to join.
class WaitlistRequest(BaseModel):
    ticket_category_id: int


# Ask WaitlistAgent to add the attendee to a sold-out ticket category.
@app.post("/waitlist", status_code=201)
def join_waitlist(data: WaitlistRequest, request: Request):
    email = require_role(request, 'attendee')
    return command('WaitlistAgent', 'join', email=email, **data.model_dump())

#Show QR tickets inside “My bookings.”
# Return ticket codes only for the attendee’s own active booking.
@app.get("/bookings/{booking_id}/tickets")
def booking_tickets(booking_id: int, request: Request):
    email = request.session.get("email")

    # Stop here if the signed-in user does not have permission.
    if not email or request.session.get("role") != "attendee":
        raise HTTPException(403, "Sign in as an attendee")

    # Use the database connection for this block and close it afterward.
    with closing(connect()) as db:
        tickets = db.execute("""
            SELECT t.code
            FROM tickets AS t
            JOIN bookings AS b ON b.id = t.booking_id
            JOIN ticket_categories AS tc ON tc.id = b.ticket_category_id
            JOIN events AS e ON e.id = tc.event_id
            LEFT JOIN cancellations AS c ON c.booking_id = b.id
            WHERE b.id = ? AND b.attendee_email = ?
              AND c.booking_id IS NULL AND e.status = 'published'
        """, (booking_id, email)).fetchall()

        return [ticket[0] for ticket in tickets]


# Send the scanner page only to signed-in staff.
@app.get("/scanner")
def scanner_page(request: Request):
    # Stop here if the signed-in user does not have permission.
    if request.session.get("role") != "staff":
        raise HTTPException(403, "Staff access only")

    return FileResponse(BASE_DIR / "scanner.html")

# Send the admin HTML page only to signed-in admins.
@app.get("/admin-page")
def admin_interface(request: Request):
    # Stop here if the signed-in user does not have permission.
    if request.session.get("role") != "admin":
        raise HTTPException(403, "Admin access only")

    return FileResponse(BASE_DIR / "admin.html")


# Check the upload size and image contents before saving a resized JPEG.
@app.post("/venue-image")
async def upload_venue_image(request: Request):
    # Stop here if the signed-in user does not have permission.
    if request.session.get("role") != "admin":
        raise HTTPException(403, "Admin access only")

    content = bytearray()

    async for chunk in request.stream():
        content.extend(chunk)

        if len(content) > 5 * 1024 * 1024:
            raise HTTPException(400, "Choose an image smaller than 5 MB")

    filename = secrets.token_hex(16) + ".jpg"

    # Handle failures so the request or background task can finish safely.
    try:
        with Image.open(BytesIO(content)) as image:
            image.thumbnail((1600, 1600))
            image.convert("RGB").save(str(UPLOAD_DIR / filename), "JPEG")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(400, "Choose a valid JPG or PNG image")

    return {"image": "/uploads/" + filename}

#Display saved venues on the admin page. This will let you select a venue for editing later.
# Return saved venue details to an admin.
@app.get("/venues")
def list_venues(request: Request):
    # Stop here if the signed-in user does not have permission.
    if request.session.get("role") != "admin":
        raise HTTPException(403, "Admin access only")

    # Use the database connection for this block and close it afterward.
    with closing(connect()) as db:
        venues = db.execute("SELECT * FROM venues ORDER BY id").fetchall()

    return [dict(venue) for venue in venues]

#Show all events on the admin page
# Return all events, including drafts and cancelled events, to an admin.
@app.get("/admin/events")
def admin_events(request: Request):
    # Stop here if the signed-in user does not have permission.
    if request.session.get("role") != "admin":
        raise HTTPException(403, "Admin access only")

    # Use the database connection for this block and close it afterward.
    with closing(connect()) as db:

        events = db.execute("""
            SELECT id, title, start_time, status
            FROM events
            ORDER BY id DESC
        """).fetchall()

    return [dict(event) for event in events]

# Admin: display all venue hire requests.
# Return private-hire requests and their venue names to an admin.
@app.get("/admin/hires")
def admin_hires(request: Request):
    # Stop here if the signed-in user does not have permission.
    if request.session.get("role") != "admin":
        raise HTTPException(403, "Admin access only")

    # Use the database connection for this block and close it afterward.
    with closing(connect()) as db:

        rows = db.execute("""
            SELECT
                h.id,
                h.venue_id,
                v.name AS venue_name,
                h.attendee_email,
                h.start_time,
                h.end_time,
                h.purpose,
                h.guest_count,
                h.status
            FROM hire_requests AS h
            JOIN venues AS v ON v.id = h.venue_id
            ORDER BY h.id DESC
        """).fetchall()

        return [dict(row) for row in rows]

# Attendees: list venues available for private hire.
# List venues that attendees can request for private hire.
@app.get("/hire-venues")
def hire_venues(request: Request):
    # Stop here if the signed-in user does not have permission.
    if request.session.get("role") != "attendee":
        raise HTTPException(403, "Sign in as an attendee")

    # Use the database connection for this block and close it afterward.
    with closing(connect()) as db:

        rows = db.execute("""
            SELECT id, name, location, capacity, hourly_rate
            FROM venues
            WHERE open_for_hire = 1
            ORDER BY name
        """).fetchall()

        return [dict(row) for row in rows]

# Show only the signed-in attendee's notifications.
# Combine old and new notifications belonging to this attendee.
@app.get("/notifications")
def list_notifications(request: Request):
    email = require_role(request, 'attendee')
    # Use the database connection for this block and close it afterward.
    with closing(connect()) as db:
        rows = db.execute("""
            SELECT 'old-' || id AS id, message, created_at FROM notifications WHERE attendee_email=?
            UNION ALL
            SELECT 'agent-' || id AS id, message, created_at FROM agent_notifications WHERE attendee_email=?
            ORDER BY created_at DESC, id DESC
        """, (email, email)).fetchall()
        return [dict(row) for row in rows]

# Clear the login session and return to sign in.
# Clear the login session and return the browser to the sign-in page.
@app.post("/logout")
def logout(request: Request):
    request.session.clear()

    return RedirectResponse(
        url="/",
        status_code=303
    )

# Update offers and return this attendee’s active holds with timezone-aware expiry times.
@app.get('/waitlist/offers')
def waitlist_offers(request: Request):
    email=require_role(request,'attendee')
    command('WaitlistAgent','tick')
    # Use the database connection for this block and close it afterward.
    with closing(connect()) as db:
        rows=db.execute("SELECT h.id,h.ticket_category_id,h.expires_at,t.name AS category,e.title,t.price FROM ticket_holds h JOIN ticket_categories t ON t.id=h.ticket_category_id JOIN events e ON e.id=t.event_id WHERE h.attendee_email=? AND h.status='active' ORDER BY h.expires_at",(email,)).fetchall()
        timezone = __import__('zoneinfo').ZoneInfo(os.getenv('EVENT_TIMEZONE','Asia/Colombo'))
        return [{**dict(row),'expires_at':datetime.fromisoformat(row['expires_at']).replace(tzinfo=timezone).isoformat()} for row in rows]


# Allow admins to inspect each agent’s saved goals, rules and counters.
@app.get('/admin/agent-state')
def agent_state(request: Request):
    require_role(request,'admin')
    # Use the database connection for this block and close it afterward.
    with closing(connect()) as db:
        return [{'agent':row['agent_name'],**json.loads(row['state_json'])} for row in db.execute('SELECT * FROM agent_state ORDER BY agent_name')]
