import importlib
import sys
from pathlib import Path
from datetime import timedelta
from fastapi.testclient import TestClient
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database import connect, transaction
from agents import campus_now


def test_http_roles_csrf_booking_hold_and_notifications(tmp_path,monkeypatch):
    # Never let this local HTTP test use a configured cloud database.
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('EVENTSPHERE_DB',str(tmp_path/'routes.db'))
    monkeypatch.setenv('SESSION_SECRET','test-secret-only-not-for-real-use')
    import main
    with TestClient(main.app) as client:
        with transaction() as db:
            for role in ('admin','staff','attendee'):
                db.execute('INSERT INTO users(email,password_hash,role) VALUES (?,?,?)',(role+'@test',main.hasher.hash('password123'),role))
        # Unsafe actions require a session-bound token even before sign-in.
        assert client.post('/login',json={'email':'admin@test','password':'password123'}).status_code==403
        def token(): return {'X-CSRF-Token':client.get('/csrf').json()['csrf_token']}
        def post(path,data={}): return client.post(path,json=data,headers=token())
        def login(role):
            assert post('/login',{'email':role+'@test','password':'password123'}).status_code==200
        login('admin')
        assert post('/venues',{'name':'Hall','location':'Campus','capacity':5,'description':'Test','image':'','hourly_rate':100,'open_for_hire':True}).status_code==201
        start=(campus_now()+timedelta(days=2)).replace(microsecond=0)
        r=post('/events',{'title':'Route Test','category':'Workshop','venue_id':1,'start_time':start.isoformat(),'end_time':(start+timedelta(hours=2)).isoformat(),'description':'Test','ticket_name':'General','ticket_price':100,'ticket_quantity':1})
        assert r.status_code==201,r.text
        assert post('/events/1/publish').status_code==200
        login('attendee')
        assert client.get('/admin/agent-messages').status_code==403
        assert post('/events/1/publish').status_code==403
        booking=post('/bookings',{'ticket_category_id':1,'quantity':1})
        assert booking.status_code==201,booking.text
        assert post('/waitlist',{'ticket_category_id':1}).status_code==201
        assert post('/bookings/1/cancel').status_code==200
        offers=client.get('/waitlist/offers').json()
        assert len(offers)==1
        assert offers[0]['expires_at'].endswith('+05:30')
        assert client.get('/notifications').json()
        claimed=post('/bookings',{'ticket_category_id':1,'quantity':1})
        assert claimed.status_code==201,claimed.text
        code=claimed.json()['ticket_codes'][0]
        assert client.get('/tickets/'+code+'/qr').headers['content-type']=='image/png'
        login('staff')
        assert post('/check-in',{'event_id':1,'code':code}).status_code==400  # too early
        assert client.get('/scanner').status_code==200
        login('admin')
        assert client.get('/admin-page').status_code==200
        assert len(client.get('/admin/agent-state').json())==5
        assert client.get('/admin/agent-messages').json()
        assert post('/logout').status_code==200  # follows redirect to home
        assert client.get('/admin-page').status_code==403
        assert client.get('/').status_code==200

