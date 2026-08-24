import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from turnout import capacity as cap  # noqa: E402
from turnout import db, service  # noqa: E402
from turnout.adapters.eventbrite import EventbriteAdapter  # noqa: E402
from turnout.merge import merge_people, suggest_merges  # noqa: E402


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    c.execute(
        """INSERT INTO event (id, slug, title, starts_at, capacity, oversell_pct,
           capacity_mode, created_at, updated_at)
           VALUES ('E','e','Public meeting','2026-09-17T19:00',100,0,'pool',?,?)""",
        (db.now(), db.now()))
    for cid, kind, label in [("C1", "eventbrite", "Eventbrite"),
                             ("C2", "meetup", "Meetup"),
                             ("C3", "atproto", "atmo.rsvp")]:
        c.execute(
            """INSERT INTO channel (id, event_id, kind, label, state, allocation,
               created_at, updated_at) VALUES (?,'E',?,?,'live',40,?,?)""",
            (cid, kind, label, db.now(), db.now()))
    c.commit()
    return c


def add(conn, cid, ext, name, email=None, status="going", handle=None):
    conn.execute(
        """INSERT INTO signup (id, event_id, channel_id, external_id, name, email,
           handle, status, created_at, seen_at) VALUES (?,'E',?,?,?,?,?,?,?,?)""",
        (db.new_id(), cid, ext, name, email, handle, status, db.now(), db.now()))
    conn.commit()


# --------------------------------------------------------------------- merge

def test_same_email_across_platforms_is_one_person(conn):
    add(conn, "C1", "1", "Priya Raman", "P.Raman@Gmail.com")
    add(conn, "C2", "2", "Priya", "p.raman@gmail.com")
    people = merge_people(conn, "E")
    assert len(people) == 1
    assert people[0].merged and people[0].keyed
    assert people[0].name == "Priya Raman"      # keeps the fuller name


def test_no_email_never_merges_even_on_identical_names(conn):
    add(conn, "C2", "1", "S. Ahmed", handle="sohail-a")
    add(conn, "C3", "2", "S. Ahmed", handle="@sohail.bsky.social")
    people = merge_people(conn, "E")
    assert len(people) == 2
    assert all(not p.keyed for p in people)
    # ...but it is offered as something for a human to check
    assert len(suggest_merges(people)) == 1


def test_two_different_emails_are_never_suggested_as_a_merge(conn):
    add(conn, "C1", "1", "John Smith", "john@a.com")
    add(conn, "C1", "2", "John Smith", "john@b.com")
    assert suggest_merges(merge_people(conn, "E")) == []


def test_going_beats_waitlist_across_sources(conn):
    add(conn, "C1", "1", "Dan", "dan@x.com", status="waitlist")
    add(conn, "C2", "2", "Dan", "dan@x.com", status="going")
    assert merge_people(conn, "E")[0].status == "going"


def test_headcount_counts_chairs_not_rows(conn):
    add(conn, "C1", "1", "Priya", "p@x.com")
    add(conn, "C2", "2", "Priya", "p@x.com")
    add(conn, "C1", "3", "Marie", "m@x.com")
    assert cap.read(conn, "E").going == 2       # not 3


# ------------------------------------------------------------------ capacity

def test_oversell_margin_is_added_to_places_offered_not_the_room(conn):
    conn.execute("UPDATE event SET capacity=100, oversell_pct=20 WHERE id='E'")
    c = cap.read(conn, "E")
    assert c.capacity == 100 and c.sellable == 120 and c.remaining == 120


def test_pool_mode_offers_the_whole_remainder_to_every_channel(conn):
    for i in range(30):
        add(conn, "C1", str(i), f"P{i}", f"p{i}@x.com")
    c = cap.read(conn, "E")
    assert c.remaining == 70
    assert c.remaining_for("C1") == 70
    # the platform is told a total, not a remainder
    assert c.target_quantity_for("C1") == 100


def test_allocated_mode_confines_each_channel_to_its_share(conn):
    conn.execute("UPDATE event SET capacity_mode='allocated' WHERE id='E'")
    for i in range(35):
        add(conn, "C1", str(i), f"P{i}", f"p{i}@x.com")
    c = cap.read(conn, "E")
    assert c.remaining_for("C1") == 5          # 40 share − 35 taken
    assert c.remaining_for("C2") == 40         # untouched, and stranded


def test_channels_that_cannot_enforce_a_cap_are_told_nothing(conn):
    c = cap.read(conn, "E")
    assert c.remaining_for("C3") is None       # atproto
    assert c.target_quantity_for("C3") is None


def test_allocation_drift_is_surfaced_not_silently_corrected(conn):
    conn.execute("UPDATE event SET capacity_mode='allocated', capacity=100 WHERE id='E'")
    # C1 40 + C2 40 = 80 against 100 places (C3 can't enforce, so isn't counted)
    assert cap.read(conn, "E").allocation_drift == -20


def test_full_is_reported_and_does_not_go_negative(conn):
    for i in range(140):
        add(conn, "C1", str(i), f"P{i}", f"p{i}@x.com")
    c = cap.read(conn, "E")
    assert c.full and c.remaining == 0 and c.pct == 100


def test_uncapped_event_has_no_limit_arithmetic(conn):
    conn.execute("UPDATE event SET capacity=NULL WHERE id='E'")
    c = cap.read(conn, "E")
    assert c.uncapped and c.remaining is None and c.full is False


# --------------------------------------------------------------------- drift

def test_drift_lists_only_fields_that_matter_to_platforms(conn):
    ev = service.get_event(conn, "E")
    conn.execute("UPDATE event SET published=? WHERE id='E'", (service.snapshot(ev),))
    conn.commit()
    conn.execute("UPDATE event SET title='Rescheduled meeting', contact='a@b.c' WHERE id='E'")
    conn.commit()
    assert service.drift(service.get_event(conn, "E")) == ["title"]


# ------------------------------------------------------------------ purging

def test_purge_removes_every_signup_and_reports_what_it_could_not_reach(conn):
    add(conn, "C1", "1", "Priya", "p@x.com")
    add(conn, "C3", "2", "Kez", handle="@kez.bsky.social")
    n, reports = service.purge_signups(conn, "E")
    assert n == 2
    assert conn.execute("SELECT COUNT(*) c FROM signup").fetchone()["c"] == 0
    # Eventbrite keeps its own orders, and says so rather than claiming success
    eb = next(r for r in reports if r.kind == "eventbrite")
    assert eb.result.manual and "Eventbrite keeps" in eb.result.message


# --------------------------------------------------------------- eventbrite

def _fake(handler):
    return httpx.Client(transport=httpx.MockTransport(handler),
                        base_url="https://www.eventbriteapi.com/v3")


EVENT = {"title": "Public meeting", "strap": "On the retrofit plan",
         "body": "Doors 6:30.\n\nStarts 7pm sharp.", "starts_at": "2026-09-17T19:00",
         "ends_at": "2026-09-17T21:00", "timezone": "Europe/London", "capacity": 100,
         "accessibility": "Step-free"}


def test_publish_does_draft_then_ticket_then_publish_in_order():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/organizations/ORG/events/"):
            return httpx.Response(200, json={"id": "EB1", "url": "https://evbt/e/EB1"})
        if "ticket_classes" in request.url.path:
            return httpx.Response(200, json={"id": "TC1"})
        if request.url.path.endswith("/publish/"):
            return httpx.Response(200, json={"published": True})
        return httpx.Response(404, json={"error_description": "nope"})

    a = EventbriteAdapter({"secret": "t", "config": {"organization_id": "ORG"}},
                          client=_fake(handler))
    res = a.publish(EVENT, {}, quantity=120)

    assert res.ok and res.external_id == "EB1"
    assert res.data["ticket_class_id"] == "TC1"
    assert [c[1] for c in calls] == [
        "/v3/organizations/ORG/events/", "/v3/events/EB1/ticket_classes/",
        "/v3/events/EB1/publish/"]


def test_closing_ends_ticket_sales_rather_than_unpublishing():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "TC1"})

    a = EventbriteAdapter({"secret": "t"}, client=_fake(handler))
    res = a.set_open(EVENT, {"external_id": "EB1", "config": '{"ticket_class_id":"TC1"}'},
                     open_=False, policy="close")

    assert res.ok
    assert "unpublish" not in seen["path"]          # would be refused once orders exist
    assert seen["body"]["ticket_class"]["sales_end"].endswith("Z")


def test_attendees_are_paged_and_cancellations_are_carried_through():
    pages = [
        {"attendees": [{"id": "1", "profile": {"name": "Priya", "email": "p@x.com"},
                        "status": "Attending"}],
         "pagination": {"has_more_items": True, "continuation": "K"}},
        {"attendees": [{"id": "2", "profile": {"name": "Dan", "email": "d@x.com"},
                        "status": "Attending", "cancelled": True}],
         "pagination": {"has_more_items": False}},
    ]

    def handler(request):
        return httpx.Response(200, json=pages[1 if "continuation" in str(request.url) else 0])

    a = EventbriteAdapter({"secret": "t"}, client=_fake(handler))
    got = a.fetch_signups(EVENT, {"external_id": "EB1"})

    assert [g.external_id for g in got] == ["1", "2"]
    assert got[0].status == "going" and got[1].status == "cancelled"


def test_api_errors_surface_the_platform_message_not_a_traceback():
    def handler(request):
        return httpx.Response(400, json={"error_description": "start date is in the past"})

    a = EventbriteAdapter({"secret": "t", "config": {"organization_id": "ORG"}},
                          client=_fake(handler))
    res = a.publish(EVENT, {}, quantity=10)
    assert not res.ok and "start date is in the past" in res.message


def test_wall_clock_time_is_converted_with_the_named_zone():
    payload = EventbriteAdapter({"secret": "t"})._event_payload(EVENT)
    # 19:00 London in September is BST, so 18:00Z
    assert payload["event"]["start"] == {"timezone": "Europe/London",
                                         "utc": "2026-09-17T18:00:00Z"}


# ----------------------------------------------------- honesty on the page

def test_public_page_never_claims_a_channel_is_closed_when_it_cannot_be(conn):
    """atproto has no cap, no waitlist and no way to close.

    However full the room, the page must not tell a reader "waiting list"
    for a platform that has none — that is a promise nobody can keep.
    """
    from turnout.adapters import capability

    assert capability("atproto", "close") == "no"
    assert capability("atproto", "waitlist") == "no"

    closeable = capability("atproto", "close") != "no"
    waitlists = capability("atproto", "waitlist") != "no"
    shut = closeable and True          # room full, policy not 'open'
    assert shut is False and waitlists is False

    # Eventbrite can be closed, so it is allowed to say so.
    assert capability("eventbrite", "close") != "no"


def test_auto_close_reports_channels_it_could_not_close(conn):
    for i in range(120):
        add(conn, "C1", str(i), f"P{i}", f"p{i}@x.com")
    conn.execute("UPDATE event SET published='{}' WHERE id='E'")
    conn.commit()
    reports = service.auto_close_if_full(conn, "E")
    kinds = {r.kind: r.result for r in reports}
    assert kinds["atproto"].manual is True       # named, not silently skipped


# ------------------------------------------------------------------- access

def test_channel_without_saved_access_is_explained_not_failed(conn):
    """A missing token is a next step for the organiser, never an error."""
    conn.execute("UPDATE channel SET state='idle' WHERE id='C1'")
    conn.commit()
    reports = service.publish(conn, "E")
    eb = next(r for r in reports if r.kind == "eventbrite")
    assert eb.result.ok and eb.result.manual
    assert "add it under Access" in eb.result.message
    # ...and the channel is left usable as a typed-in link
    assert conn.execute("SELECT state FROM channel WHERE id='C1'"
                        ).fetchone()["state"] == "manual"


def test_access_can_be_added_after_the_channel_exists(conn):
    assert service.needs_access(conn, dict(
        conn.execute("SELECT * FROM channel WHERE id='C1'").fetchone()))
    conn.execute(
        "INSERT INTO credential (id, kind, label, secret, config, created_at) "
        "VALUES ('K1','eventbrite','Org','tok','{}',?)", (db.now(),))
    conn.commit()
    assert not service.needs_access(conn, dict(
        conn.execute("SELECT * FROM channel WHERE id='C1'").fetchone()))


def test_a_channel_can_name_which_saved_access_to_use(conn):
    """Groups with two Eventbrite organisations need to pick per event."""
    for k, label in [("K1", "Union account"), ("K2", "Branch account")]:
        conn.execute(
            "INSERT INTO credential (id, kind, label, secret, config, created_at) "
            "VALUES (?,'eventbrite',?,?,'{}',?)", (k, label, f"tok-{k}", db.now()))
    conn.execute("UPDATE channel SET credential_id='K1' WHERE id='C1'")
    conn.commit()
    ch = dict(conn.execute("SELECT * FROM channel WHERE id='C1'").fetchone())
    assert service.credential_for(conn, "eventbrite", ch["credential_id"])["label"] \
        == "Union account"
    # with none named, the most recently added is used
    assert service.credential_for(conn, "eventbrite", None)["label"] == "Branch account"


def test_removing_access_leaves_channels_intact(conn):
    conn.execute(
        "INSERT INTO credential (id, kind, label, secret, config, created_at) "
        "VALUES ('K1','eventbrite','Org','tok','{}',?)", (db.now(),))
    conn.execute("UPDATE channel SET credential_id='K1' WHERE id='C1'")
    conn.execute("UPDATE channel SET credential_id=NULL WHERE credential_id='K1'")
    conn.execute("DELETE FROM credential WHERE id='K1'")
    conn.commit()
    ch = dict(conn.execute("SELECT * FROM channel WHERE id='C1'").fetchone())
    assert ch is not None and service.needs_access(conn, ch)
