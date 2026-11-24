"""Integration test using a separate PostgreSQL test database."""

import os
import sys
from pathlib import Path
from uuid import uuid4
from urllib.parse import (
    urlsplit,
    urlunsplit,
    parse_qsl,
    urlencode,
    quote,
)
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

# Find application files in the project folder.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import create_tables, connect
from agents import run_command


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="Set TEST_POSTGRES_URL to a separate test database",
)
def test_postgres_agents_and_concurrent_last_seat():
    import psycopg
    from psycopg import sql

    # Use the separate test branch URL.
    base = os.environ["TEST_POSTGRES_URL"]

    # Give this run its own isolated schema.
    schema = "eventsphere_test_" + uuid4().hex

    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query))

    # Tell every test connection to use the isolated schema.
    existing_options = query.get("options", "").strip()
    query["options"] = (
        existing_options + " -c search_path=" + schema
    ).strip()

    # Encode spaces as %20, not +, for PostgreSQL connection URLs.
    target = urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(query, quote_via=quote),
        parts.fragment,
    ))

    # Create only the isolated test schema.
    with psycopg.connect(base, connect_timeout=15) as db:
        db.execute(
            sql.SQL("CREATE SCHEMA {}").format(
                sql.Identifier(schema)
            )
        )

    # Fixed times make the checks repeatable.
    now = datetime(2030, 1, 1, 9)

    def command(agent, action, at=now, **data):
        return run_command(
            agent,
            {"action": action, **data},
            path=target,
            now=at,
        )

    try:
        create_tables(target)

        # Running migrations a second time must be safe.
        create_tables(target)

        # Create a venue.
        venue_reply = command(
            "VenueAgent",
            "save_venue",
            name="Hall",
            location="Campus",
            capacity=5,
            description="Integration test venue",
            image="",
            hourly_rate=100,
            open_for_hire=True,
        )
        assert venue_reply["performative"] == "AGREE"
        venue_id = venue_reply["content"]["venue_id"]

        # Create an event with exactly one ticket.
        event_reply = command(
            "VenueAgent",
            "create_event",
            title="Postgres Test",
            category="Workshop",
            venue_id=venue_id,
            start_time="2030-01-02T10:00:00",
            end_time="2030-01-02T12:00:00",
            description="Integration test event",
            ticket_name="General",
            ticket_price=100,
            ticket_quantity=1,
        )
        assert event_reply["performative"] == "AGREE"
        event_id = event_reply["content"]["event_id"]

        published = command(
            "VenueAgent",
            "publish",
            event_id=event_id,
        )
        assert published["performative"] == "AGREE"

        with connect(target) as db:
            category_id = db.execute(
                "SELECT id FROM ticket_categories WHERE event_id=?",
                (event_id,),
            ).fetchone()[0]

        def buy(email, at=now):
            return command(
                "BookingAgent",
                "book",
                at=at,
                email=email,
                ticket_category_id=category_id,
                quantity=1,
            )

        def competing_buyer(number):
            email = str(number) + "@test"
            return email, buy(email)

        # Four separate connections compete for the last seat.
        with ThreadPoolExecutor(max_workers=4) as pool:
            buyers = list(pool.map(competing_buyer, range(4)))

        winners = [
            (email, reply["content"])
            for email, reply in buyers
            if reply["performative"] == "AGREE"
        ]

        # Exactly one booking must succeed.
        assert len(winners) == 1
        assert sum(
            reply["performative"] == "REFUSE"
            for _, reply in buyers
        ) == 3

        winner_email, booking = winners[0]

        # Join two attendees to the sold-out category.
        for waiter in ("first@test", "second@test"):
            reply = command(
                "WaitlistAgent",
                "join",
                email=waiter,
                ticket_category_id=category_id,
            )
            assert reply["performative"] == "AGREE"

        # Cancel the winning booking and refund its payment.
        cancelled = command(
            "BookingAgent",
            "cancel",
            email=winner_email,
            booking_id=booking["booking_id"],
        )
        assert cancelled["performative"] == "AGREE"
        assert cancelled["content"]["refund"] == 100

        # The first waiter should receive the released seat.
        with connect(target) as db:
            hold = db.execute(
                """
                SELECT attendee_email
                FROM ticket_holds
                WHERE status='active'
                """
            ).fetchone()

            assert hold is not None
            assert hold[0] == "first@test"

        # Expire the first offer after the default 120 seconds.
        swept = command(
            "WaitlistAgent",
            "tick",
            at=now + timedelta(seconds=121),
        )
        assert swept["performative"] == "AGREE"

        # The next waiter claims the re-offered seat.
        claimed = buy(
            "second@test",
            at=now + timedelta(seconds=122),
        )
        assert claimed["performative"] == "AGREE"

        code = claimed["content"]["ticket_codes"][0]

        # Check-in is open 15 minutes before event start.
        scan_time = datetime(2030, 1, 2, 9, 45)

        first_scan = command(
            "TicketAgent",
            "scan",
            at=scan_time,
            event_id=event_id,
            code=code,
        )
        assert first_scan["performative"] == "AGREE"

        # The same ticket cannot be admitted again.
        second_scan = command(
            "TicketAgent",
            "scan",
            at=scan_time,
            event_id=event_id,
            code=code,
        )
        assert second_scan["performative"] == "REFUSE"
        assert (
            second_scan["content"]["reason"]
            == "Ticket already checked in"
        )

        with connect(target) as db:
            remaining = db.execute(
                "SELECT quantity FROM ticket_categories WHERE id=?",
                (category_id,),
            ).fetchone()[0]
            assert remaining == 0

            agent_count = db.execute(
                "SELECT COUNT(*) FROM agent_state"
            ).fetchone()[0]
            assert agent_count == 5

            notification_count = db.execute(
                "SELECT COUNT(*) FROM agent_notifications"
            ).fetchone()[0]
            assert notification_count > 0

            repeated_scans = db.execute(
                """
                SELECT repeated_scans
                FROM ticket_scan_attempts
                WHERE code=?
                """,
                (code,),
            ).fetchone()[0]
            assert repeated_scans == 1

    finally:
        # Delete only this run's schema, including its test records.
        with psycopg.connect(base, connect_timeout=15) as db:
            db.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )