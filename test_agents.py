import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database import create_tables, connect, transaction
from agents import run_command, make_bus

NOW=datetime(2030,1,1,9)

@pytest.fixture
def setup(tmp_path):
    path=tmp_path/'test.db'
    create_tables(path)
    with transaction(path) as db:
        db.execute("INSERT INTO venues VALUES (1,'Hall','Campus',10,'Description','',100,1)")
        db.execute("INSERT INTO events VALUES (1,'Workshop','Workshop',1,'2030-01-02T10:00:00','2030-01-02T12:00:00','Test','published')")
        db.execute("INSERT INTO ticket_categories(id,event_id,name,price,quantity,total_quantity) VALUES(1,1,'General',100,1,1)")
    def command(agent,action,now=NOW,**data):
        return run_command(agent,{'action':action,**data},path=path,now=now)
    return path,command

def book(command,email='one@test',qty=1):
    return command('BookingAgent','book',email=email,ticket_category_id=1,quantity=qty)

def test_booking_issues_one_ticket(setup):
    path,c=setup
    r=book(c)
    assert r['performative']=='AGREE'
    assert len(r['content']['ticket_codes'])==1
    with connect(path) as db:
        assert db.execute('SELECT quantity FROM ticket_categories').fetchone()[0]==0
        assert db.execute('SELECT amount FROM payments').fetchone()[0]==100

def test_concurrent_buyers_cannot_oversell(setup):
    path,c=setup
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies=list(pool.map(lambda i:book(c,f'{i}@test'),range(4)))
    assert sum(r['performative']=='AGREE' for r in replies)==1
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM bookings').fetchone()[0]==1
        assert db.execute('SELECT quantity FROM ticket_categories').fetchone()[0]==0

def test_refund_once_uses_original_price(setup):
    path,c=setup
    bid=book(c)['content']['booking_id']
    with transaction(path) as db: db.execute('UPDATE ticket_categories SET price=500')
    assert c('BookingAgent','cancel',email='one@test',booking_id=bid)['content']['refund']==100
    assert c('BookingAgent','cancel',email='one@test',booking_id=bid)['performative']=='REFUSE'
    with connect(path) as db: assert db.execute('SELECT quantity FROM ticket_categories').fetchone()[0]==1

def test_duplicate_checkin_rejected(setup):
    path,c=setup
    code=book(c)['content']['ticket_codes'][0]
    args={'code':code,'event_id':1,'now':datetime(2030,1,2,9,45)}
    assert c('TicketAgent','scan',**args)['performative']=='AGREE'
    assert c('TicketAgent','scan',**args)['performative']=='REFUSE'
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM check_ins').fetchone()[0]==1
        assert db.execute('SELECT repeated_scans FROM ticket_scan_attempts').fetchone()[0]==1

def test_wrong_event_and_early_scan_rejected(setup):
    _,c=setup
    code=book(c)['content']['ticket_codes'][0]
    assert c('TicketAgent','scan',code=code,event_id=2)['performative']=='REFUSE'
    assert c('TicketAgent','scan',code=code,event_id=1)['performative']=='REFUSE'

def test_cancelled_ticket_rejected(setup):
    _,c=setup
    b=book(c)['content']
    c('BookingAgent','cancel',email='one@test',booking_id=b['booking_id'])
    assert c('TicketAgent','scan',code=b['ticket_codes'][0],event_id=1,now=datetime(2030,1,2,10))['performative']=='REFUSE'

def test_venue_conflict_proposes_real_alternative(setup):
    _,c=setup
    r=c('VenueAgent','hire',venue_id=1,email='one@test',start_time='2030-01-02T10:00:00',end_time='2030-01-02T11:00:00',purpose='Meeting',guest_count=2)
    assert r['performative']=='PROPOSE'
    assert r['content']['suggested_start']=='2030-01-03T10:00:00'

def test_hire_capacity_and_approval_rule(setup):
    _,c=setup
    args=dict(venue_id=1,email='one@test',start_time='2030-01-03T10:00:00',end_time='2030-01-03T14:00:00',purpose='Meeting',guest_count=2)
    r=c('VenueAgent','hire',**args)['content']
    assert r['status']=='pending' and r['price']==400
    assert c('VenueAgent','review',hire_id=r['hire_id'],approve=True)['content']['status']=='approved'
    args['guest_count']=11
    assert c('VenueAgent','hire',**args)['performative']=='REFUSE'

def test_fifo_expiry_reoffers_without_overselling(setup):
    path,c=setup
    bid=book(c)['content']['booking_id']
    for email in ('first@test','second@test'):
        assert c('WaitlistAgent','join',email=email,ticket_category_id=1)['performative']=='AGREE'
    c('BookingAgent','cancel',email='one@test',booking_id=bid)
    with connect(path) as db:
        assert db.execute("SELECT attendee_email FROM ticket_holds WHERE status='active'").fetchone()[0]=='first@test'
    assert book(c,'public@test')['performative']=='REFUSE'
    c('WaitlistAgent','tick',now=NOW+timedelta(seconds=121))
    with connect(path) as db:
        assert db.execute("SELECT attendee_email FROM ticket_holds WHERE status='active'").fetchone()[0]=='second@test'
        assert db.execute('SELECT quantity FROM ticket_categories').fetchone()[0]==0
    assert book_at(c,'second@test',NOW+timedelta(seconds=122))['performative']=='AGREE'

def book_at(c,email,now):
    return c('BookingAgent','book',email=email,ticket_category_id=1,quantity=1,now=now)

def test_held_seat_claim_survives_new_bus_instance(setup):
    path,c=setup
    bid=book(c)['content']['booking_id']
    c('WaitlistAgent','join',email='wait@test',ticket_category_id=1)
    c('BookingAgent','cancel',email='one@test',booking_id=bid)
    create_tables(path)
    assert book(c,'wait@test')['performative']=='AGREE'
    with connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM ticket_holds WHERE status='claimed'").fetchone()[0]==1
        assert db.execute('SELECT quantity FROM ticket_categories').fetchone()[0]==0

def test_notifications_deduplicate_and_state_persists(setup):
    path,c=setup
    with transaction(path) as db:
        bus=make_bus(db,NOW)
        for _ in range(2):
            bus.broadcast('BookingAgent',{'email':'one@test','text':'Hello','key':'same'},'conversation')
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM agent_notifications').fetchone()[0]==1
        state=json.loads(db.execute("SELECT state_json FROM agent_state WHERE agent_name='NotificationAgent'").fetchone()[0])
        assert state['messages_received']==2

def test_event_cancel_refunds_and_releases_holds(setup):
    path,c=setup
    bid=book(c)['content']['booking_id']
    r=c('BookingAgent','cancel_event',event_id=1)
    assert r['content']['total_refund']==100
    assert book(c)['performative']=='REFUSE'

def test_bus_replay_is_idempotent_and_trace_correlated(setup):
    path,c=setup
    with transaction(path) as db:
        bus=make_bus(db,NOW)
        m=bus.envelope('Web','BookingAgent','REQUEST',{'action':'book','email':'one@test','ticket_category_id':1,'quantity':1})
        a=bus.deliver(m); b=bus.deliver(m)
        assert a==b
        logs=[json.loads(row[0]) for row in db.execute('SELECT message FROM agent_messages')]
        assert all(x['conversation_id']==m['conversation_id'] for x in logs)
        assert db.execute('SELECT COUNT(*) FROM bookings').fetchone()[0]==1

def test_unexpected_ticket_failure_rolls_back_stock(setup,monkeypatch):
    path,c=setup
    from agents import TicketAgent
    def fail(*args): raise RuntimeError('Injected failure')
    monkeypatch.setattr(TicketAgent,'decide',fail)
    with pytest.raises(RuntimeError): book(c)
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM bookings').fetchone()[0]==0
        assert db.execute('SELECT quantity FROM ticket_categories').fetchone()[0]==1

def test_migration_preserves_old_records(tmp_path):
    import sqlite3
    path=tmp_path/'old.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE bookings(id INTEGER PRIMARY KEY, attendee_email TEXT NOT NULL,ticket_category_id INTEGER NOT NULL,quantity INTEGER NOT NULL)')
        db.execute("INSERT INTO bookings VALUES(7,'old@test',1,2)")
    create_tables(path); create_tables(path)
    with connect(path) as db:
        assert db.execute('SELECT attendee_email FROM bookings WHERE id=7').fetchone()[0]=='old@test'
        assert 'unit_price' in [r['name'] for r in db.execute('PRAGMA table_info(bookings)')]

def test_capacity_reduction_does_not_break_existing_allocations(setup):
    _,c=setup
    fields=dict(venue_id=1,name='Hall',location='Campus',capacity=0,description='Test',image='',hourly_rate=100,open_for_hire=True)
    assert c('VenueAgent','edit_venue',**fields)['performative']=='REFUSE'
    with_args=dict(title='Larger',category='Workshop',venue_id=1,start_time='2030-01-04T10:00:00',end_time='2030-01-04T12:00:00',description='Test',ticket_name='General',ticket_price=100,ticket_quantity=5)
    assert c('VenueAgent','create_event',**with_args)['performative']=='AGREE'
    fields['capacity']=4
    assert c('VenueAgent','edit_venue',**fields)['performative']=='REFUSE'

def test_venue_turnaround_requires_fifteen_minutes(setup):
    _,c=setup
    args=dict(venue_id=1,email='one@test',start_time='2030-01-02T12:10:00',end_time='2030-01-02T13:10:00',purpose='Meeting',guest_count=2)
    assert c('VenueAgent','hire',**args)['performative']=='PROPOSE'
    args['start_time']='2030-01-02T12:15:00'
    assert c('VenueAgent','hire',**args)['performative']=='AGREE'

def test_failed_large_purchase_keeps_owner_hold(setup):
    path,c=setup
    bid=book(c)['content']['booking_id']
    c('WaitlistAgent','join',email='wait@test',ticket_category_id=1)
    c('BookingAgent','cancel',email='one@test',booking_id=bid)
    assert book(c,'wait@test',2)['performative']=='REFUSE'
    assert book(c,'wait@test',1)['performative']=='AGREE'
    with connect(path) as db:
        assert db.execute('SELECT quantity FROM ticket_categories').fetchone()[0]==0
