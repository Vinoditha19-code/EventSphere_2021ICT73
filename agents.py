"""Five persistent agents communicating only through the JSON message bus.

SQLite works locally; DATABASE_URL selects PostgreSQL in production.
Each command and its messages commit together. The database module serializes
stock/calendar decisions on both backends.
"""
import json  # Converts agent messages and saved agent state between Python values and JSON text.
import os  # Reads settings such as the database path and secret key from environment variables.
import secrets  # Creates random session keys, payment references and ticket codes.
from datetime import datetime, timedelta  # Works with event dates, booking times and time gaps.
from zoneinfo import ZoneInfo  # Uses the campus timezone instead of depending on the server timezone.
from database import transaction, insert_id  # Uses the shared database path, connections, transactions and table setup.
from message_bus import MessageBus  # Sends JSON messages between the agents and saves their conversation log.


# Get the current campus time for event and booking comparisons.
def campus_now():
    return datetime.now(ZoneInfo(os.getenv('EVENT_TIMEZONE', 'Asia/Colombo'))).replace(tzinfo=None)


# Return a refusal with a clear reason and the matching HTTP status.
def refuse(reason, status=400):
    return 'REFUSE', {'reason': reason, 'http_status': status}


# Return a successful agent decision with its response data.
def agree(**data):
    return 'AGREE', data


# Require a successful internal reply; unexpected failures roll back the transaction.
def required_reply(reply):
    if reply['performative'] != 'AGREE':
        # An unexpected failure after earlier mutations rolls back the whole unit.
        raise RuntimeError(reply['content'].get('reason', 'Agent command failed'))
    return reply['content']


# Shared state-saving and messaging helpers used by the five agents.
class Agent:
    goal = ''
    rules = {}

    # Remember the database connection and the information this object needs.
    def __init__(self, db):
        self.db = db
        self.name = type(self).__name__

    # Load this agent’s saved state, process a message and save its updated state.
    def handle(self, message, bus):
        row = self.db.execute('SELECT state_json FROM agent_state WHERE agent_name=?',
                              (self.name,)).fetchone()
        state = json.loads(row[0]) if row else {'messages_received': 0}
        state.update(goal=self.goal, rules=self.rules, last_conversation=message['conversation_id'])
        state['messages_received'] += 1
        self.db.execute('INSERT INTO agent_state VALUES (?,?) ON CONFLICT(agent_name) '
                        'DO UPDATE SET state_json=excluded.state_json',
                        (self.name, json.dumps(state)))
        kind, content = self.decide(message, bus)
        state = json.loads(self.db.execute('SELECT state_json FROM agent_state WHERE agent_name=?',
                                          (self.name,)).fetchone()[0])
        state['last_decision'] = kind
        self.db.execute('INSERT INTO agent_state VALUES (?,?) ON CONFLICT(agent_name) '
                        'DO UPDATE SET state_json=excluded.state_json',
                        (self.name, json.dumps(state)))
        return kind, content

    # Send a REQUEST through the bus instead of calling another agent directly.
    def ask(self, bus, receiver, action, message, **data):
        return bus.ask(self.name, receiver, {'action': action, **data}, message['conversation_id'])

    # Broadcast an INFORM message so subscribed agents can react.
    def inform(self, bus, message, **data):
        bus.broadcast(self.name, data, message['conversation_id'])


# Manage venue capacity, the event calendar and private-hire decisions.
class VenueAgent(Agent):
    goal = 'Keep the venue calendar free of conflicts and respect capacity.'
    rules = {'turnaround_minutes': 15, 'auto_approve_hours': 3}

    # Check published events and approved hires, including the 15-minute gap.
    def free(self, venue_id, start, end):
        gap = timedelta(minutes=15)
        start, end = datetime.fromisoformat(start) - gap, datetime.fromisoformat(end) + gap
        rows = self.db.execute("SELECT start_time,end_time FROM events WHERE venue_id=? AND status='published' "
                               "UNION ALL SELECT start_time,end_time FROM hire_requests "
                               "WHERE venue_id=? AND status='approved'", (venue_id, venue_id))
        return all(datetime.fromisoformat(a) >= end or datetime.fromisoformat(b) <= start for a, b in rows)

    # Look up to seven days ahead for an alternative time that is free.
    def conflict(self, venue_id, start, end):
        for day in range(1, 8):
            a = (datetime.fromisoformat(start) + timedelta(days=day)).isoformat()
            b = (datetime.fromisoformat(end) + timedelta(days=day)).isoformat()
            if self.free(venue_id, a, b):
                return 'PROPOSE', {'reason': 'Try this available time', 'http_status': 409,
                                   'suggested_start': a, 'suggested_end': b}
        return refuse('No available time found in the next 7 days', 409)

    # Read the requested action and apply this agent’s decision rules.
    def decide(self, m, bus):
        d = m['content']; action = d.get('action')
        # Handle the save venue, edit venue action.
        if action in ('save_venue', 'edit_venue'):
            if d['capacity'] < 1 or d['hourly_rate'] < 0:
                return refuse('Check the capacity and hourly rate')
            values = [d[k] for k in ('name', 'location', 'capacity', 'description', 'image', 'hourly_rate', 'open_for_hire')]
            # Handle the save venue action.
            if action == 'save_venue':
                new_id = insert_id(self.db, 'INSERT INTO venues(name,location,capacity,description,image,hourly_rate,open_for_hire) VALUES (?,?,?,?,?,?,?)', values)
                return agree(venue_id=new_id, message='Venue added')
            venue_id = d['venue_id']
            if not self.db.execute('SELECT 1 FROM venues WHERE id=?', (venue_id,)).fetchone():
                return refuse('Venue not found', 404)
            allocated = self.db.execute("SELECT COALESCE(MAX(n),0) FROM (SELECT SUM(t.total_quantity) AS n FROM events e JOIN ticket_categories t ON t.event_id=e.id WHERE e.venue_id=? AND e.status!='cancelled' GROUP BY e.id UNION ALL SELECT guest_count FROM hire_requests WHERE venue_id=? AND status='approved') AS allocations", (venue_id, venue_id)).fetchone()[0]
            if d['capacity'] < allocated:
                return refuse('Capacity is below an existing event allocation or approved hire')
            self.db.execute('UPDATE venues SET name=?,location=?,capacity=?,description=?,image=?,hourly_rate=?,open_for_hire=? WHERE id=?', (*values, venue_id))
            return agree(message='Venue updated', venue_id=venue_id)
        # Handle the create event action.
        if action == 'create_event':
            start, end = datetime.fromisoformat(d['start_time']), datetime.fromisoformat(d['end_time'])
            venue = self.db.execute('SELECT capacity FROM venues WHERE id=?', (d['venue_id'],)).fetchone()
            if not venue: return refuse('Venue not found', 404)
            if start <= bus.now or end <= start: return refuse('Choose future times, with end after start')
            if d['ticket_quantity'] < 1 or d['ticket_quantity'] > venue[0] or d['ticket_price'] < 0:
                return refuse('Check ticket quantity, price and venue capacity')
            new_id = insert_id(self.db, 'INSERT INTO events(title,category,venue_id,start_time,end_time,description) VALUES (?,?,?,?,?,?)',
                                  tuple(d[k] for k in ('title','category','venue_id','start_time','end_time','description')))
            event_id = new_id
            required_reply(self.ask(bus, 'BookingAgent', 'create_category', m, event_id=event_id,
                                    name=d['ticket_name'], price=d['ticket_price'], quantity=d['ticket_quantity']))
            return agree(event_id=event_id, message='Event created')
        # Handle the publish, cancel event action.
        if action in ('publish', 'cancel_event'):
            event = self.db.execute('SELECT * FROM events WHERE id=?', (d['event_id'],)).fetchone()
            if not event: return refuse('Event not found', 404)
            # Handle the cancel event action.
            if action == 'cancel_event':
                if event['status'] != 'published': return refuse('Published event not found', 404)
                self.db.execute("UPDATE events SET status='cancelled' WHERE id=?", (event['id'],))
                return agree(message='Event cancelled')
            if event['status'] != 'draft': return refuse('Only draft events can be published', 409)
            if datetime.fromisoformat(event['start_time']) <= bus.now: return refuse('Event must start in the future')
            capacity = self.ask(bus, 'BookingAgent', 'capacity_check', m, event_id=event['id'])
            if capacity['performative'] != 'AGREE': return capacity['performative'], capacity['content']
            if not self.free(event['venue_id'], event['start_time'], event['end_time']):
                return self.conflict(event['venue_id'], event['start_time'], event['end_time'])
            self.db.execute("UPDATE events SET status='published' WHERE id=?", (event['id'],))
            self.inform(bus, m, event='event_published', event_id=event['id'])
            return agree(message='Event published')
        # Handle the hire action.
        if action == 'hire':
            start, end = datetime.fromisoformat(d['start_time']), datetime.fromisoformat(d['end_time'])
            if start <= bus.now or end <= start or d['guest_count'] < 1 or not d['purpose'].strip():
                return refuse('Check the times, purpose and guest count')
            venue = self.db.execute('SELECT * FROM venues WHERE id=?', (d['venue_id'],)).fetchone()
            if not venue: return refuse('Venue not found', 404)
            if not venue['open_for_hire']: return refuse('Venue is closed for private hire')
            if d['guest_count'] > venue['capacity']: return refuse('Too many guests for this venue')
            if not self.free(d['venue_id'], d['start_time'], d['end_time']):
                return self.conflict(d['venue_id'], d['start_time'], d['end_time'])
            hours = (end-start).total_seconds()/3600
            status = 'approved' if hours <= 3 else 'pending'
            price = round(hours*venue['hourly_rate'], 2)
            new_id = insert_id(self.db, 'INSERT INTO hire_requests(venue_id,attendee_email,start_time,end_time,purpose,guest_count,status,quoted_price) VALUES (?,?,?,?,?,?,?,?)',
                                  (d['venue_id'],d['email'],d['start_time'],d['end_time'],d['purpose'].strip(),d['guest_count'],status,price))
            self.inform(bus, m, event='hire_updated', email=d['email'], key=f'hire:{new_id}:{status}', text=f'Your venue hire #{new_id} is {status}.')
            return agree(hire_id=new_id, status=status, price=price)
        # Handle the review action.
        if action == 'review':
            hire = self.db.execute("SELECT * FROM hire_requests WHERE id=? AND status='pending'", (d['hire_id'],)).fetchone()
            if not hire: return refuse('Pending hire not found', 404)
            status = 'rejected'
            if d['approve']:
                venue = self.db.execute('SELECT * FROM venues WHERE id=?', (hire['venue_id'],)).fetchone()
                if not venue or not venue['open_for_hire'] or venue['capacity'] < hire['guest_count']:
                    return refuse('Venue availability or capacity has changed', 409)
                if datetime.fromisoformat(hire['start_time']) <= bus.now: return refuse('Hire has already started')
                if not self.free(hire['venue_id'],hire['start_time'],hire['end_time']):
                    return refuse('Venue is already booked at that time', 409)
                status = 'approved'
            self.db.execute('UPDATE hire_requests SET status=? WHERE id=?', (status,hire['id']))
            self.inform(bus,m,event='hire_updated',email=hire['attendee_email'],key=f"hire:{hire['id']}:{status}",text=f"Your venue hire #{hire['id']} is {status}.")
            return agree(hire_id=hire['id'], status=status)
        return refuse('Unknown venue action')


# Manage seat stock, simulated payments, bookings and refunds.
class BookingAgent(Agent):
    goal = 'Allocate available seats without overselling and refund cancellations once.'
    rules = {'maximum_tickets_per_booking': 6, 'refund_before_start': 'full'}

    # Read the requested action and apply this agent’s decision rules.
    def decide(self, m, bus):
        d=m['content']; action=d.get('action')
        # Handle the create category action.
        if action == 'create_category':
            self.db.execute('INSERT INTO ticket_categories(event_id,name,price,quantity,total_quantity) VALUES (?,?,?,?,?)',
                            (d['event_id'],d['name'],d['price'],d['quantity'],d['quantity']))
            return agree()
        # Handle the capacity check action.
        if action == 'capacity_check':
            row=self.db.execute('SELECT v.capacity,COALESCE(SUM(t.total_quantity),0) AS allocated FROM events e JOIN venues v ON v.id=e.venue_id LEFT JOIN ticket_categories t ON t.event_id=e.id WHERE e.id=? GROUP BY e.id,v.capacity', (d['event_id'],)).fetchone()
            if not row or row['allocated'] > row['capacity']: return refuse('Ticket allocation exceeds venue capacity')
            return agree()
        # Handle the reserve hold, release hold action.
        if action in ('reserve_hold','release_hold'):
            # Handle the reserve hold action.
            if action == 'reserve_hold':
                cur=self.db.execute('UPDATE ticket_categories SET quantity=quantity-1 WHERE id=? AND quantity>0', (d['category_id'],))
                if not cur.rowcount: return refuse('No free seats remain',409)
            else:
                self.db.execute('UPDATE ticket_categories SET quantity=quantity+? WHERE id=?',(d['quantity'],d['category_id']))
            return agree()
        # Handle the validate booking action.
        if action == 'validate_booking':
            row=self.db.execute('SELECT b.*,e.id AS event_id,e.status,e.start_time,e.end_time,c.booking_id AS cancelled FROM bookings b JOIN ticket_categories t ON t.id=b.ticket_category_id JOIN events e ON e.id=t.event_id LEFT JOIN cancellations c ON c.booking_id=b.id WHERE b.id=?',(d['booking_id'],)).fetchone()
            if not row or row['cancelled'] or row['status']!='published': return refuse('Ticket is cancelled or invalid',409)
            return agree(**dict(row))
        # Handle the book action.
        if action == 'book':
            # Expiry and FIFO offers are processed before checking public stock.
            required_reply(self.ask(bus,'WaitlistAgent','tick',m))
            row=self.db.execute("SELECT t.*,e.start_time,e.status FROM ticket_categories t JOIN events e ON e.id=t.event_id WHERE t.id=?",(d['ticket_category_id'],)).fetchone()
            qty=d['quantity']
            if not row or row['status']!='published': return refuse('Published ticket category not found',404)
            if datetime.fromisoformat(row['start_time']) <= bus.now: return refuse('Booking has closed')
            if qty<1 or qty>6: return refuse('Choose between 1 and 6 tickets')
            hold=self.db.execute("SELECT * FROM ticket_holds WHERE attendee_email=? AND ticket_category_id=? AND status='active' AND expires_at>?",(d['email'],row['id'],bus.now.isoformat())).fetchone()
            held=hold['quantity'] if hold else 0
            if qty>row['quantity']+held: return refuse('Not enough tickets available',409)
            self.db.execute('UPDATE ticket_categories SET quantity=quantity-? WHERE id=?',(qty-min(qty,held),row['id']))
            new_id=insert_id(self.db, 'INSERT INTO bookings(attendee_email,ticket_category_id,quantity,unit_price) VALUES (?,?,?,?)',(d['email'],row['id'],qty,row['price']))
            booking_id=new_id
            if hold:
                required_reply(self.ask(bus,'WaitlistAgent','claim',m,hold_id=hold['id'],email=d['email'],booking_id=booking_id))
            reference='PAY-'+secrets.token_hex(4).upper()
            self.db.execute('INSERT INTO payments(booking_id,reference,amount) VALUES (?,?,?)',(booking_id,reference,round(qty*row['price'],2)))
            issued=required_reply(self.ask(bus,'TicketAgent','issue',m,booking_id=booking_id))
            self.inform(bus,m,event='booking_confirmed',email=d['email'],key=f'booking:{booking_id}',text=f'Booking #{booking_id} confirmed: {qty} ticket(s).')
            return agree(booking_id=booking_id,payment_reference=reference,ticket_codes=issued['ticket_codes'])
        # Handle the cancel action.
        if action == 'cancel':
            row=self.db.execute('SELECT b.*,e.start_time FROM bookings b JOIN ticket_categories t ON t.id=b.ticket_category_id JOIN events e ON e.id=t.event_id WHERE b.id=? AND b.attendee_email=?',(d['booking_id'],d['email'])).fetchone()
            if not row: return refuse('Booking not found',404)
            if self.db.execute('SELECT 1 FROM cancellations WHERE booking_id=?',(row['id'],)).fetchone(): return refuse('Booking already cancelled',409)
            if datetime.fromisoformat(row['start_time'])<=bus.now: return refuse('Cancellation closes when the event starts')
            if self.db.execute('SELECT 1 FROM check_ins c JOIN tickets t ON t.id=c.ticket_id WHERE t.booking_id=?',(row['id'],)).fetchone(): return refuse('A checked-in booking cannot be cancelled',409)
            refund=self.cancel(row,m,bus)
            self.inform(bus,m,event='seats_released',category_id=row['ticket_category_id'])
            return agree(message='Booking cancelled',refund=refund)
        # Handle the cancel event action.
        if action == 'cancel_event':
            reply=self.ask(bus,'VenueAgent','cancel_event',m,event_id=d['event_id'])
            if reply['performative']!='AGREE': return reply['performative'],reply['content']
            rows=self.db.execute('SELECT b.* FROM bookings b JOIN ticket_categories t ON t.id=b.ticket_category_id LEFT JOIN cancellations c ON c.booking_id=b.id WHERE t.event_id=? AND c.booking_id IS NULL',(d['event_id'],)).fetchall()
            total=sum(self.cancel(row,m,bus) for row in rows)
            required_reply(self.ask(bus,'WaitlistAgent','cancel_event',m,event_id=d['event_id']))
            self.inform(bus,m,event='event_cancelled',event_id=d['event_id'])
            return agree(message='Event cancelled',bookings_cancelled=len(rows),total_refund=round(total,2))
        return refuse('Unknown booking action')

    # Record one refund, return the seats and notify the booking owner.
    def cancel(self,row,m,bus):
        paid=self.db.execute('SELECT amount FROM payments WHERE booking_id=?',(row['id'],)).fetchone()
        refund=round(paid[0],2) if paid else 0
        self.db.execute('INSERT INTO cancellations(booking_id,refund) VALUES (?,?)',(row['id'],refund))
        self.db.execute('UPDATE ticket_categories SET quantity=quantity+? WHERE id=?',(row['quantity'],row['ticket_category_id']))
        self.inform(bus,m,event='booking_cancelled',email=row['attendee_email'],key=f"refund:{row['id']}",text=f"Booking #{row['id']} cancelled. Refund: Rs. {refund:.2f}.")
        return refund


# Create unique tickets and prevent a ticket from being admitted twice.
class TicketAgent(Agent):
    goal='Issue unique tickets and admit each valid ticket once.'
    rules={'opens_minutes_before_start':30, 'admissions_per_ticket':1}

    # Read the requested action and apply this agent’s decision rules.
    def decide(self,m,bus):
        d=m['content']; action=d.get('action')
        # Handle the issue action.
        if action=='issue':
            booking=required_reply(self.ask(bus,'BookingAgent','validate_booking',m,booking_id=d['booking_id']))
            codes=[]
            for _ in range(booking['quantity']):
                code=secrets.token_urlsafe(24)
                self.db.execute('INSERT INTO tickets(booking_id,code) VALUES (?,?)',(d['booking_id'],code)); codes.append(code)
            return agree(ticket_codes=codes)
        # Handle the scan action.
        if action=='scan':
            code=d['code'].strip()
            self.db.execute('INSERT INTO ticket_scan_attempts(code,attempts,last_attempt) VALUES (?,1,?) ON CONFLICT(code) DO UPDATE SET attempts=ticket_scan_attempts.attempts+1,last_attempt=excluded.last_attempt',(code,bus.now.isoformat()))
            ticket=self.db.execute('SELECT id,booking_id FROM tickets WHERE code=?',(code,)).fetchone()
            if not ticket: return refuse('Invalid ticket',404)
            reply=self.ask(bus,'BookingAgent','validate_booking',m,booking_id=ticket['booking_id'])
            if reply['performative']!='AGREE': return reply['performative'],reply['content']
            booking=reply['content']
            if booking['event_id']!=d['event_id']: return refuse('This ticket belongs to another event',409)
            if not datetime.fromisoformat(booking['start_time'])-timedelta(minutes=30)<=bus.now<datetime.fromisoformat(booking['end_time']):
                return refuse('Check-in is outside the event admission window')
            cur=self.db.execute('INSERT INTO check_ins(ticket_id) VALUES (?) ON CONFLICT(ticket_id) DO NOTHING',(ticket['id'],))
            if not cur.rowcount:
                self.db.execute('UPDATE ticket_scan_attempts SET repeated_scans=repeated_scans+1 WHERE code=?',(code,))
                return refuse('Ticket already checked in',409)
            self.inform(bus,m,event='ticket_checked_in',ticket_id=ticket['id'],event_id=d['event_id'])
            return agree(message='Check-in successful')
        return refuse('Unknown ticket action')


# Keep waiting order and manage timed one-seat reservations.
class WaitlistAgent(Agent):
    goal='Offer released seats in FIFO order and re-offer expired holds.'
    rules={'seats_per_offer':1,'hold_seconds':120,'order':'joined_at, id'}

    # Read the requested action and apply this agent’s decision rules.
    def decide(self,m,bus):
        d=m['content']; action=d.get('action')
        if m['performative']=='INFORM':
            if d.get('event') in ('seats_released','event_published'):
                self.tick(m,bus)
            return agree(message='Broadcast received')
        # Handle the join action.
        if action=='join':
            row=self.db.execute("SELECT t.quantity,e.status,e.start_time FROM ticket_categories t JOIN events e ON e.id=t.event_id WHERE t.id=?",(d['ticket_category_id'],)).fetchone()
            if not row or row['quantity']!=0 or row['status']!='published' or datetime.fromisoformat(row['start_time'])<=bus.now:
                return refuse('Choose a future, published, sold-out category',409)
            old=self.db.execute('SELECT id,status FROM waitlist WHERE attendee_email=? AND ticket_category_id=?',(d['email'],d['ticket_category_id'])).fetchone()
            if old and old['status'] in ('waiting','offered'): return refuse('You have already joined this waitlist',409)
            if old:
                self.db.execute("UPDATE waitlist SET status='waiting',joined_at=? WHERE id=?",(bus.now.isoformat(),old['id']))
            else:
                self.db.execute('INSERT INTO waitlist(attendee_email,ticket_category_id,joined_at) VALUES (?,?,?)',(d['email'],d['ticket_category_id'],bus.now.isoformat()))
            return agree(message='Joined the waitlist')
        # Handle the claim action.
        if action=='claim':
            hold=self.db.execute("SELECT * FROM ticket_holds WHERE id=? AND attendee_email=? AND status='active' AND expires_at>?",(d['hold_id'],d['email'],bus.now.isoformat())).fetchone()
            if not hold: return refuse('Your ticket hold has expired',409)
            self.db.execute("UPDATE ticket_holds SET status='claimed',booking_id=? WHERE id=?",(d['booking_id'],hold['id']))
            self.db.execute("UPDATE waitlist SET status='claimed' WHERE id=?",(hold['waitlist_id'],))
            return agree()
        # Handle the cancel event action.
        if action=='cancel_event':
            rows=self.db.execute("SELECT h.* FROM ticket_holds h JOIN ticket_categories t ON t.id=h.ticket_category_id WHERE t.event_id=? AND h.status='active'",(d['event_id'],)).fetchall()
            for hold in rows: self.release(hold,m,bus,'cancelled')
            self.db.execute("UPDATE waitlist SET status='cancelled' WHERE ticket_category_id IN (SELECT id FROM ticket_categories WHERE event_id=?) AND status IN ('waiting','offered')",(d['event_id'],))
            return agree()
        # Handle the tick action.
        if action=='tick':
            self.tick(m,bus); return agree(message='Waitlist checked')
        return refuse('Unknown waitlist action')

    # Return an unclaimed held seat and update the hold and waitlist status.
    def release(self,hold,m,bus,status):
        required_reply(self.ask(bus,'BookingAgent','release_hold',m,category_id=hold['ticket_category_id'],quantity=hold['quantity']))
        self.db.execute('UPDATE ticket_holds SET status=? WHERE id=?',(status,hold['id']))
        self.db.execute('UPDATE waitlist SET status=? WHERE id=?',(status,hold['waitlist_id']))
        self.inform(bus,m,event='hold_expired',email=hold['attendee_email'],key=f"hold:{hold['id']}:{status}",text='Your ticket hold expired or the event closed. No payment was taken.')

    # Release expired offers and offer available seats in waiting order.
    def tick(self,m,bus):
        holds=self.db.execute("SELECT h.*,e.status AS event_status,e.start_time FROM ticket_holds h JOIN ticket_categories t ON t.id=h.ticket_category_id JOIN events e ON e.id=t.event_id WHERE h.status='active'").fetchall()
        for hold in holds:
            if datetime.fromisoformat(hold['expires_at'])<=bus.now or hold['event_status']!='published' or datetime.fromisoformat(hold['start_time'])<=bus.now:
                self.release(hold,m,bus,'expired')
        waiting=self.db.execute("SELECT w.*,t.name,e.title,e.start_time FROM waitlist w JOIN ticket_categories t ON t.id=w.ticket_category_id JOIN events e ON e.id=t.event_id WHERE w.status='waiting' AND t.quantity>0 AND e.status='published' ORDER BY w.joined_at,w.id").fetchall()
        for row in waiting:
            start=datetime.fromisoformat(row['start_time'])
            if start<=bus.now: continue
            reply=self.ask(bus,'BookingAgent','reserve_hold',m,category_id=row['ticket_category_id'])
            if reply['performative']!='AGREE': continue
            expiry=min(start,bus.now+timedelta(seconds=int(os.getenv('WAITLIST_HOLD_SECONDS','120')))).isoformat()
            new_id=insert_id(self.db, 'INSERT INTO ticket_holds(waitlist_id,attendee_email,ticket_category_id,expires_at) VALUES (?,?,?,?)',(row['id'],row['attendee_email'],row['ticket_category_id'],expiry))
            self.db.execute("UPDATE waitlist SET status='offered',offer_count=offer_count+1 WHERE id=?",(row['id'],))
            self.inform(bus,m,event='hold_offered',email=row['attendee_email'],key=f'hold:{new_id}:offered',text=f"One ticket is held for {row['title']} ({row['name']}). Open My bookings to claim before the hold expires.")


# Save private in-app messages without saving the same notification twice.
class NotificationAgent(Agent):
    goal='Deliver private, persistent notifications without duplicates.'
    rules={'delivery':'in-app','deduplication':'email and message key'}

    # Read the requested action and apply this agent’s decision rules.
    def decide(self,m,bus):
        d=m['content']
        if m['performative']=='INFORM':
            if d.get('email') and d.get('text'):
                self.db.execute('INSERT INTO agent_notifications(attendee_email,message,message_key,conversation_id,created_at) VALUES (?,?,?,?,?) ON CONFLICT(attendee_email,message_key) DO NOTHING',(d['email'],d['text'],d['key'],m['conversation_id'],bus.now.isoformat()))
            return agree(message='Notification processed')
        return refuse('NotificationAgent accepts INFORM broadcasts')


# Create the message bus and register all five agents.
def make_bus(db,now=None):
    bus=MessageBus(db,now or campus_now())
    for cls in (VenueAgent,BookingAgent,TicketAgent,WaitlistAgent,NotificationAgent): bus.register(cls(db))
    return bus


# Process one command and its related messages in one database transaction.
def run_command(receiver,content,path=None,now=None,sender='Web'):
    # Keep these changes together in one transaction.
    with transaction(path) as db:
        bus=make_bus(db,now)
        bus.recover()
        reply=bus.ask(sender,receiver,content)
    return reply


# Ask the waitlist agent to check expired holds and available seats.
def check_waitlist():
    return run_command('WaitlistAgent',{'action':'tick'},sender='Scheduler')
