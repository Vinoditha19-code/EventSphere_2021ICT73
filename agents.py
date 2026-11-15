from datetime import datetime, timedelta

import json
from uuid import uuid4

#check if the venue is free
def venue_is_free(db, venue_id, start_time, end_time):
    gap = timedelta(minutes=15)
    start = datetime.fromisoformat(start_time) - gap
    end = datetime.fromisoformat(end_time) + gap

   
    events = db.execute("""
        SELECT start_time, end_time FROM events
        WHERE venue_id = ? AND status = 'published'

        UNION ALL

        SELECT start_time, end_time FROM hire_requests
        WHERE venue_id = ? AND status = 'approved'
    """, (venue_id, venue_id))

    for saved_start, saved_end in events:
        if datetime.fromisoformat(saved_start) < end and datetime.fromisoformat(saved_end) > start:
            return False

    return True

def ask_venue_agent(db, venue_id, start, end):
    conversation_id = str(uuid4())

    request_message = {
        "conversation_id": conversation_id,
        "sender": "Event Management",
        "receiver": "VenueAgent",
        "performative": "REQUEST",
        "content": {"venue_id": venue_id, "start": start, "end": end}
    }

    available = venue_is_free(db, venue_id, start, end)

    reply_message = {
        "conversation_id": conversation_id,
        "sender": "VenueAgent",
        "receiver": "Event Management",
        "performative": "AGREE" if available else "REFUSE",
        "content": "Venue is free" if available else "Venue is already booked"
    }

    for message in (request_message, reply_message):
        db.execute(
            "INSERT INTO agent_messages (message) VALUES (?)",
            (json.dumps(message),)
        )

    return available