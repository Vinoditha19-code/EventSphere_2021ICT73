"""Optional integration test. Set TEST_POSTGRES_URL to a test database, never a production URL."""
import os  # Read the separate PostgreSQL test URL.
import sys  # Find the application modules.
from pathlib import Path  # Locate the project folder.
from uuid import uuid4  # Give each run a new isolated schema.
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode  # Add the test schema setting to the URL.
from concurrent.futures import ThreadPoolExecutor  # Test simultaneous last-seat buyers.
from datetime import datetime,timedelta  # Use repeatable event and expiry times.
import pytest  # Run or skip this optional integration test.
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database import create_tables,transaction,connect  # Use the production database layer.
from agents import run_command  # Drive real agents through the JSON message bus.


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='Set TEST_POSTGRES_URL to run the real PostgreSQL integration check')
def test_postgres_agents_and_concurrent_last_seat():
    import psycopg  # Import the real PostgreSQL driver only for this optional test.
    base=os.environ['TEST_POSTGRES_URL']
    schema='eventsphere_test_'+uuid4().hex
    parts=urlsplit(base)
    query=dict(parse_qsl(parts.query))
    query['options']=query.get('options','')+' -c search_path='+schema
    target=urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode(query),parts.fragment))
    with psycopg.connect(base) as db:
        db.execute('CREATE SCHEMA '+schema)
    now=datetime(2030,1,1,9)
    def command(agent,action,at=now,**data):
        return run_command(agent,{'action':action,**data},path=target,now=at)
    try:
        create_tables(target)
        create_tables(target)  # Applying migrations again must be safe.
        v=command('VenueAgent','save_venue',name='Hall',location='Campus',capacity=5,description='Test',image='',hourly_rate=100,open_for_hire=True)['content']['venue_id']
        event=command('VenueAgent','create_event',title='Postgres Test',category='Workshop',venue_id=v,start_time='2030-01-02T10:00:00',end_time='2030-01-02T12:00:00',description='Test',ticket_name='General',ticket_price=100,ticket_quantity=1)['content']['event_id']
        assert command('VenueAgent','publish',event_id=event)['performative']=='AGREE'
        with connect(target) as db:
            category=db.execute('SELECT id FROM ticket_categories WHERE event_id=?',(event,)).fetchone()[0]
        def buy(email,at=now):
            return command('BookingAgent','book',at=at,email=email,ticket_category_id=category,quantity=1)
        # Four separate connections compete for the final seat.
        with ThreadPoolExecutor(max_workers=4) as pool:
            buyers=list(pool.map(lambda i:(str(i)+'@test',buy(str(i)+'@test')),range(4)))
        winners=[(email,r['content']) for email,r in buyers if r['performative']=='AGREE']
        assert len(winners)==1
        email,booking=winners[0]
        for waiter in ('first@test','second@test'):
            assert command('WaitlistAgent','join',email=waiter,ticket_category_id=category)['performative']=='AGREE'
        assert command('BookingAgent','cancel',email=email,booking_id=booking['booking_id'])['content']['refund']==100
        with connect(target) as db:
            assert db.execute("SELECT attendee_email FROM ticket_holds WHERE status='active'").fetchone()[0]=='first@test'
        command('WaitlistAgent','tick',at=now+timedelta(seconds=121))
        claimed=buy('second@test',now+timedelta(seconds=122))
        assert claimed['performative']=='AGREE'
        code=claimed['content']['ticket_codes'][0]
        at=datetime(2030,1,2,9,45)
        assert command('TicketAgent','scan',at=at,event_id=event,code=code)['performative']=='AGREE'
        assert command('TicketAgent','scan',at=at,event_id=event,code=code)['performative']=='REFUSE'
        with connect(target) as db:
            assert db.execute('SELECT quantity FROM ticket_categories WHERE id=?',(category,)).fetchone()[0]==0
            assert db.execute('SELECT COUNT(*) FROM agent_state').fetchone()[0]==5
            assert db.execute('SELECT COUNT(*) FROM agent_notifications').fetchone()[0]>0
            assert db.execute('SELECT repeated_scans FROM ticket_scan_attempts WHERE code=?',(code,)).fetchone()[0]==1
    finally:
        # Remove only the random schema created by this test, never public tables.
        with psycopg.connect(base) as db:
            db.execute('DROP SCHEMA '+schema+' CASCADE')