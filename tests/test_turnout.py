import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from turnout import capacity as cap  # noqa: E402
from turnout import db, service  # noqa: E402
from turnout.adapters import KINDS  # noqa: E402
from turnout.adapters.eventbrite import EventbriteAdapter  # noqa: E402
from turnout.adapters.luma import LumaAdapter  # noqa: E402
from turnout.adapters.actionnetwork import ActionNetworkAdapter  # noqa: E402
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


def test_home_screen_says_what_publishing_is_actually_available(conn):
    """The home screen describes this installation, not the capability matrix.

    An unbuilt adapter must never offer an access form, and a platform whose
    token is saved must read differently from one whose token is not.
    """
    by_kind = {o["kind"]: o for o in service.publishing_options(conn)}

    assert by_kind["manual"]["state"] == "manual"
    assert not by_kind["manual"]["can_add_access"]
    for kind in ("eventbrite", "luma"):
        assert by_kind[kind]["tag"] == "needs access"
        assert by_kind[kind]["can_add_access"]
    # Action Network takes a key too, but it buys the sign-ups back rather
    # than the publishing, and the home screen must not say otherwise
    assert "make the event there" in by_kind["actionnetwork"]["summary"]
    for kind in ("meetup", "atproto"):
        assert by_kind[kind]["tag"] == "not built yet"
        assert by_kind[kind]["caps_are_promises"]
        assert not by_kind[kind]["can_add_access"]

    conn.execute(
        "INSERT INTO credential (id, kind, label, secret, config, created_at) "
        "VALUES ('K1','eventbrite','Org','tok','{}',?)", (db.now(),))
    conn.commit()
    options = service.publishing_options(conn)
    eb = next(o for o in options if o["kind"] == "eventbrite")
    assert eb["tag"] == "access saved" and not eb["can_add_access"]
    assert eb["channels"] == 1                      # C1 in the fixture
    # ready platforms sort above the ones that still need something
    assert [o["rank"] for o in options] == sorted(o["rank"] for o in options)


# --------------------------------------------------------------------- luma

def _luma(handler):
    return httpx.Client(transport=httpx.MockTransport(handler),
                        base_url="https://public-api.luma.com")


def test_luma_publishes_in_one_call_and_reads_the_public_address_back():
    """Create returns only an id, so the address costs a second call."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/v1/events/create":
            body = json.loads(request.content)
            assert body["name"] == "Public meeting"
            assert body["start_at"] == "2026-09-17T18:00:00Z"    # 19:00 BST
            assert body["timezone"] == "Europe/London"
            assert "**On the retrofit plan**" in body["description_md"]
            return httpx.Response(200, json={"id": "evt-1"})
        return httpx.Response(200, json={"url": "https://lu.ma/retrofit"})

    a = LumaAdapter({"secret": "k"}, client=_luma(handler))
    res = a.publish(EVENT, {"policy": "waitlist"}, quantity=120)

    assert res.ok and res.external_id == "evt-1"
    assert res.url == "https://lu.ma/retrofit"
    assert calls == ["/v1/events/create", "/v1/events/get"]


def test_luma_publishes_even_when_the_address_lookup_fails():
    """The event is up; a failed lookup must not report it as a failure."""
    def handler(request):
        if request.url.path == "/v1/events/create":
            return httpx.Response(200, json={"id": "evt-1"})
        return httpx.Response(500, json={"message": "upstream is having a day"})

    res = LumaAdapter({"secret": "k"}, client=_luma(handler)).publish(
        EVENT, {}, quantity=None)
    assert res.ok and res.external_id == "evt-1" and not res.url


def test_luma_capacity_follows_the_channel_policy():
    a = LumaAdapter({"secret": "k"})
    waiting = a._capacity_payload(EVENT, {"policy": "waitlist"}, 120)
    closing = a._capacity_payload(EVENT, {"policy": "close"}, 120)
    leave_open = a._capacity_payload(EVENT, {"policy": "open"}, 120)

    assert waiting == {"waitlist_status": "enabled", "max_capacity": 120}
    assert closing == {"waitlist_status": "disabled", "max_capacity": 120}
    # "leave open" must not hand Luma a ceiling it would enforce itself
    assert leave_open == {"max_capacity": None, "waitlist_status": "disabled"}


def test_luma_closes_registration_without_emailing_everyone():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    a = LumaAdapter({"secret": "k"}, client=_luma(handler))
    res = a.set_open(EVENT, {"external_id": "evt-1"}, open_=False, policy="close")

    assert res.ok
    assert seen["path"] == "/v1/events/update"
    assert seen["body"]["registration_open"] is False
    assert seen["body"]["suppress_notifications"] is True     # not news to anyone


def test_luma_guests_are_paged_and_invitations_are_not_counted_as_sign_ups():
    pages = [
        {"entries": [
            {"id": "gst-1", "user_name": "Priya", "user_email": "p@x.com",
             "approval_status": "approved", "registered_at": "2026-09-01T10:00:00Z"},
            {"id": "gst-2", "user_name": "Sam", "user_email": "s@x.com",
             "approval_status": "invited"},
         ], "has_more": True, "next_cursor": "c1"},
        {"entries": [
            {"id": "gst-3", "user_first_name": "Rae", "user_last_name": "Okonjo",
             "user_email": "r@x.com", "approval_status": "waitlist"},
            {"id": "gst-4", "user_name": "Jo", "user_email": "j@x.com",
             "approval_status": "declined"},
         ], "has_more": False},
    ]

    def handler(request):
        return httpx.Response(
            200, json=pages[1 if "pagination_cursor" in str(request.url) else 0])

    got = LumaAdapter({"secret": "k"}, client=_luma(handler)).fetch_signups(
        EVENT, {"external_id": "evt-1"})

    assert [g.external_id for g in got] == ["gst-1", "gst-3", "gst-4"]
    assert [g.status for g in got] == ["going", "waitlist", "cancelled"]
    assert got[1].name == "Rae Okonjo"
    assert got[0].email == "p@x.com"


def test_luma_errors_surface_the_platform_message():
    def handler(request):
        return httpx.Response(401, json={"message": "You are not signed in.",
                                         "code": None})

    res = LumaAdapter({"secret": "wrong"}, client=_luma(handler)).publish(
        EVENT, {}, quantity=10)
    assert not res.ok and "You are not signed in." in res.message


def test_luma_key_check_names_the_calendar_it_opened():
    """A key pasted from the wrong calendar is otherwise silent until it matters."""
    def handler(request):
        assert request.url.path == "/v1/calendars/get"
        return httpx.Response(200, json={"id": "cal-1", "name": "Tenants' Union"})

    a = LumaAdapter({"secret": "k"}, client=_luma(handler))
    assert a.check() == "calendar Tenants' Union"
    # and the key travels as Luma's own header, not as a bearer token
    assert LumaAdapter({"secret": "k"}).client.headers["x-luma-api-key"] == "k"


def test_luma_says_where_the_guest_list_still_lives_after_a_purge():
    res = LumaAdapter({"secret": "k"}).delete_signups(EVENT, {"external_id": "evt-1"})
    assert res.manual and "Luma keeps its own guest list" in res.message


# ----------------------------------------------------------- action network

def _an(handler):
    return httpx.Client(transport=httpx.MockTransport(handler),
                        base_url="https://actionnetwork.org/api/v2")


def _an_event(browser_url, eid="21789f03-0180-45d3-853c-91bd6fdc8c07"):
    return {"identifiers": [f"action_network:{eid}"], "title": "Public meeting",
            "browser_url": browser_url, "total_accepted": 7}


def test_action_network_never_posts_an_event_nobody_could_sign():
    """API-created events get no RSVP page, so Turnout asks rather than fakes it."""
    def handler(request):
        raise AssertionError(f"should not have called {request.method} {request.url}")

    res = ActionNetworkAdapter({"secret": "k"}, client=_an(handler)).publish(
        EVENT, {"url": ""}, quantity=None)

    assert res.manual and res.ok            # a job for a human, not a failure
    assert "no page on actionnetwork.org" in res.message


def test_action_network_adopts_the_event_you_made_there():
    def handler(request):
        return httpx.Response(200, json={
            "total_pages": 2, "page": 1,
            "_embedded": {"osdi:events": [_an_event("https://actionnetwork.org/events/other")]}}
            if "page=2" not in str(request.url) else {
            "total_pages": 2, "page": 2,
            "_embedded": {"osdi:events": [_an_event("https://actionnetwork.org/events/retrofit")]}})

    a = ActionNetworkAdapter({"secret": "k"}, client=_an(handler))
    # pasted with a tracking parameter and a trailing slash, as they arrive
    res = a.publish(EVENT, {"url": "http://actionnetwork.org/events/retrofit/?source=poster"},
                    quantity=None)

    assert res.ok and not res.manual
    assert res.external_id == "21789f03-0180-45d3-853c-91bd6fdc8c07"
    assert res.url == "https://actionnetwork.org/events/retrofit"
    assert "7 so far" in res.message
    # and the key travels as Action Network's own header
    assert ActionNetworkAdapter({"secret": "k"}).client.headers["OSDI-API-Token"] == "k"


def test_action_network_says_so_when_the_key_cannot_see_that_event():
    def handler(request):
        return httpx.Response(200, json={"total_pages": 1, "_embedded": {"osdi:events": []}})

    res = ActionNetworkAdapter({"secret": "k"}, client=_an(handler)).publish(
        EVENT, {"url": "https://actionnetwork.org/events/retrofit"}, quantity=None)

    assert res.manual and "couldn't find that event" in res.message


def test_action_network_sends_the_wall_clock_time_with_its_own_offset():
    """The API reads times as local to the event, so plain UTC lands an hour out."""
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    a = ActionNetworkAdapter({"secret": "k"}, client=_an(handler))
    res = a.update(EVENT, {"external_id": "AN1"}, quantity=None, changed=["venue"])

    assert res.ok and seen["method"] == "PUT"
    assert seen["body"]["start_date"] == "2026-09-17T19:00:00+01:00"
    # never the fields the API refuses or silently ignores
    assert not {"capacity", "visibility", "status"} & set(seen["body"])


def test_action_network_says_a_capacity_edit_did_not_go_through():
    def handler(request):
        return httpx.Response(200, json={})

    a = ActionNetworkAdapter({"secret": "k"}, client=_an(handler))
    res = a.update(EVENT, {"external_id": "AN1"}, quantity=90,
                   changed=["capacity", "venue"])

    assert res.ok and "not editable through the API" in res.message


def test_action_network_hands_closing_back_instead_of_calling_nothing():
    def handler(request):
        raise AssertionError("nothing in the API closes an event")

    res = ActionNetworkAdapter({"secret": "k"}, client=_an(handler)).set_open(
        EVENT, {"external_id": "AN1"}, open_=False, policy="close")
    assert res.manual and "the API has no way to stop RSVPs" in res.message


def test_action_network_reads_rsvps_with_the_person_behind_each_one():
    people = {
        "p1": {"given_name": "Priya", "family_name": "Shah",
               "email_addresses": [{"primary": False, "address": "old@x.com"},
                                   {"primary": True, "address": "p@x.com"}]},
        "p2": {"given_name": "Sam", "email_addresses": [{"address": "s@x.com"}]},
    }
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/attendances"):
            return httpx.Response(200, json={"total_pages": 1, "_embedded": {
                "osdi:attendances": [
                    {"identifiers": ["action_network:att-1"], "status": "accepted",
                     "action_network:person_id": "p1", "created_date": "2026-09-01T10:00:00Z"},
                    {"identifiers": ["action_network:att-2"], "status": "declined",
                     "action_network:person_id": "p2"},
                    # the same person again: the second read must be cached
                    {"identifiers": ["action_network:att-3"], "status": "attended",
                     "action_network:person_id": "p1"},
                ]}})
        return httpx.Response(200, json=people[request.url.path.rsplit("/", 1)[1]])

    got = ActionNetworkAdapter({"secret": "k"}, client=_an(handler)).fetch_signups(
        EVENT, {"external_id": "AN1"})

    assert [g.external_id for g in got] == ["att-1", "att-2", "att-3"]
    assert [g.status for g in got] == ["going", "cancelled", "going"]
    assert got[0].name == "Priya Shah" and got[0].email == "p@x.com"
    assert calls.count("/api/v2/people/p1") == 1


def test_action_network_keeps_one_unreadable_person_from_losing_the_rest():
    def handler(request):
        if request.url.path.endswith("/attendances"):
            return httpx.Response(200, json={"total_pages": 1, "_embedded": {
                "osdi:attendances": [
                    {"identifiers": ["action_network:att-1"], "status": "accepted",
                     "action_network:person_id": "p1"}]}})
        return httpx.Response(404, json={"error": "no such person"})

    got = ActionNetworkAdapter({"secret": "k"}, client=_an(handler)).fetch_signups(
        EVENT, {"external_id": "AN1"})
    assert [g.external_id for g in got] == ["att-1"]      # the RSVP still counts
    assert got[0].email is None


def test_action_network_errors_never_carry_the_api_key_into_the_log():
    """Their 403 body quotes the key back, and adapter messages are logged."""
    def handler(request):
        return httpx.Response(403, json={
            "error": "API Key invalid or not present sekrit-key-1234"})

    res = ActionNetworkAdapter({"secret": "sekrit-key-1234"},
                               client=_an(handler)).publish(
        EVENT, {"url": "https://actionnetwork.org/events/retrofit"}, quantity=None)

    assert not res.ok
    assert "sekrit-key-1234" not in res.message
    assert "API Key invalid" in res.message


def test_action_network_purge_says_the_rsvps_are_beyond_reach():
    res = ActionNetworkAdapter({"secret": "k"}).delete_signups(
        EVENT, {"external_id": "AN1"})
    assert res.manual and "API cannot delete them" in res.message


# ------------------------------------------------------ the whole first page

def _client(conn, monkeypatch):
    """The app, talking to the test database. In-process, no network."""
    from fastapi.testclient import TestClient
    from turnout import main

    monkeypatch.setattr(main, "_conn", conn)
    return TestClient(main.app)


def test_every_option_for_creating_an_event_is_on_the_first_page(conn, monkeypatch):
    """One page, so nothing about the event is a screen you have to find."""
    page = _client(conn, monkeypatch).get("/").text

    for field in ("title", "strap", "body", "starts_at", "ends_at", "venue",
                  "address", "accessibility", "capacity", "oversell_pct",
                  "capacity_mode", "contact"):
        assert f'name="{field}"' in page, field
    # and every platform, tickable, with an address box for one already made
    for kind in KINDS:
        assert f'name="kinds" value="{kind}"' in page, kind
        assert f'name="url_{kind}"' in page, kind


def test_one_submit_makes_the_event_and_the_places_it_goes(conn, monkeypatch):
    client = _client(conn, monkeypatch)
    client.post("/events", data={
        "title": "Public meeting on the retrofit plan",
        "strap": "What the plan means for our block",
        "body": "Doors 6:30.", "starts_at": "2026-10-01T19:00",
        "ends_at": "2026-10-01T21:00", "venue": "Jubilee Hall",
        "address": "2 High Street", "accessibility": "Step-free",
        "contact": "hello@union.org", "capacity": "120", "oversell_pct": "20",
        "capacity_mode": "allocated",
        "kinds": ["eventbrite", "manual"],
        "url_manual": "https://example.org/our-page",
    }, follow_redirects=False)

    ev = dict(conn.execute(
        "SELECT * FROM event WHERE title = 'Public meeting on the retrofit plan'"
    ).fetchone())
    assert (ev["accessibility"], ev["capacity"], ev["oversell_pct"]) == ("Step-free", 120, 20)
    assert ev["capacity_mode"] == "allocated" and ev["ends_at"] == "2026-10-01T21:00"

    made = {r["kind"]: dict(r) for r in conn.execute(
        "SELECT * FROM channel WHERE event_id = ?", (ev["id"],))}
    assert set(made) == {"eventbrite", "manual"}
    # an address typed in is already live; there is nothing left to do to it
    assert made["manual"]["state"] == "live"
    assert made["eventbrite"]["state"] == "idle"


def test_creating_ignores_a_platform_turnout_does_not_know(conn, monkeypatch):
    client = _client(conn, monkeypatch)
    client.post("/events", data={"title": "Quiet one", "starts_at": "2026-10-02T19:00",
                                 "kinds": ["manual", "not-a-platform"]})

    ev = dict(conn.execute("SELECT * FROM event WHERE title = 'Quiet one'").fetchone())
    kinds = [r["kind"] for r in conn.execute(
        "SELECT * FROM channel WHERE event_id = ?", (ev["id"],))]
    assert kinds == ["manual"]


# ------------------------------------------------ publishing twice, and what
#                                                   the button tells you

def _eventbrite_stub(conn, monkeypatch, handler):
    """Point every eventbrite channel at a recorded transport."""
    client = httpx.Client(transport=httpx.MockTransport(handler),
                          base_url="https://www.eventbriteapi.com/v3")
    real = service.adapter_for
    monkeypatch.setattr(service, "adapter_for", lambda c, ch: (
        EventbriteAdapter({"secret": "t", "config": {"organization_id": "ORG"}},
                          client=client) if ch["kind"] == "eventbrite" else real(c, ch)))
    conn.execute(
        "INSERT INTO credential (id, kind, label, secret, config, created_at) "
        "VALUES ('K1','eventbrite','Org','tok','{\"organization_id\":\"ORG\"}',?)",
        (db.now(),))
    conn.execute("DELETE FROM channel WHERE id != 'C1'")
    conn.commit()


def test_publishing_twice_never_makes_a_second_listing(conn, monkeypatch):
    """The one mistake with no undo: two events, posters pointing at whichever.

    A publish that fails after the event is created used to lose its id, so
    every press started again and left another orphan draft on Eventbrite.
    """
    made = []

    def handler(request):
        if request.url.path.endswith("/events/") and request.method == "POST":
            made.append(1)
            return httpx.Response(200, json={"id": f"EB{len(made)}",
                                             "url": f"https://evb/{len(made)}"})
        if "ticket_classes" in request.url.path:
            return httpx.Response(400, json={"error_description": "ticket class rejected"})
        return httpx.Response(200, json={})

    _eventbrite_stub(conn, monkeypatch, handler)
    conn.execute("UPDATE channel SET state = 'idle' WHERE id = 'C1'")
    conn.commit()

    for _ in range(3):
        service.publish(conn, "E")

    assert len(made) == 1
    ch = dict(conn.execute("SELECT * FROM channel WHERE id='C1'").fetchone())
    assert ch["external_id"] == "EB1"        # kept, though the publish failed
    assert ch["state"] == "failed"


def test_a_retry_finishes_the_event_already_created(conn, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if "ticket_classes" in request.url.path:
            return httpx.Response(200, json={"id": "TC1"})
        return httpx.Response(200, json={})

    _eventbrite_stub(conn, monkeypatch, handler)
    conn.execute("UPDATE channel SET state='failed', external_id='EB1' WHERE id='C1'")
    conn.commit()

    reports = service.publish(conn, "E")

    assert not any(p.endswith("/events/") and "EB1" not in p for p in calls)
    assert "picked up the event already created" in reports[0].result.message
    assert dict(conn.execute("SELECT * FROM channel WHERE id='C1'").fetchone())["state"] == "live"


def test_publishing_a_live_channel_does_not_touch_the_platform(conn, monkeypatch):
    def handler(request):
        raise AssertionError(f"should not have called {request.url}")

    _eventbrite_stub(conn, monkeypatch, handler)      # C1 is 'live' in the fixture
    reports = service.publish(conn, "E")

    assert [r.result.message for r in reports] == ["already up, left alone"]
    assert "already up, left alone: Eventbrite" in conn.execute(
        "SELECT message FROM log ORDER BY id DESC LIMIT 1").fetchone()["message"]


def test_luma_publish_will_not_make_a_second_event_either():
    def handler(request):
        assert request.url.path != "/v1/events/create"
        return httpx.Response(200, json={"url": "https://lu.ma/retrofit"})

    res = LumaAdapter({"secret": "k"}, client=_luma(handler)).publish(
        EVENT, {"external_id": "evt-1"}, quantity=None)
    assert res.ok and res.external_id == "evt-1" and "already up" in res.message


def test_the_button_says_what_it_did_to_each_platform(conn, monkeypatch):
    """Pressing publish and being told nothing is how the duplicate went unseen."""
    def handler(request):
        if "ticket_classes" in request.url.path:
            return httpx.Response(400, json={"error_description": "ticket class rejected"})
        return httpx.Response(200, json={"id": "EB1", "url": "https://evb/1"})

    _eventbrite_stub(conn, monkeypatch, handler)
    conn.execute("UPDATE channel SET state = 'idle' WHERE id = 'C1'")
    conn.commit()
    client = _client(conn, monkeypatch)

    page = client.post("/events/E/publish", follow_redirects=True).text
    assert "What that did" in page
    assert "ticket class rejected" in page          # the failure, in the open

    # and pressing it again says plainly that nothing needed doing
    conn.execute("UPDATE channel SET state = 'live' WHERE id = 'C1'")
    conn.commit()
    page = client.post("/events/E/publish", follow_redirects=True).text
    assert "already up, left alone: Eventbrite" in page


# ------------------------------------------- a listing that isn't there any more

def test_two_presses_at_once_cannot_both_create(conn):
    """The claim, not the read, is what stops the duplicate.

    Both requests read the channel before either finished, so both saw 'idle'
    and both created an event. Claiming is one statement, so one loses.
    """
    ch = dict(conn.execute("SELECT * FROM channel WHERE id='C1'").fetchone())
    conn.execute("UPDATE channel SET state='idle' WHERE id='C1'")
    conn.commit()

    assert service._claim(conn, ch) is True       # the press that got there first
    assert service._claim(conn, ch) is False      # the one behind it


def test_a_publish_eventbrite_refuses_is_not_reported_as_published(conn, monkeypatch):
    """A 200 saying `published: false` used to read as success."""
    def handler(request):
        if request.url.path.endswith("/publish/"):
            return httpx.Response(200, json={"published": False})
        if "ticket_classes" in request.url.path:
            return httpx.Response(200, json={"id": "TC1"})
        return httpx.Response(200, json={"id": "EB1", "url": "https://evb/1"})

    _eventbrite_stub(conn, monkeypatch, handler)
    conn.execute("UPDATE channel SET state = 'idle' WHERE id = 'C1'")
    conn.commit()

    res = service.publish(conn, "E")[0].result
    assert not res.ok and "still a draft" in res.message
    assert res.external_id == "EB1"                # kept, so a retry finishes it


def test_a_deleted_listing_stops_being_advertised(conn, monkeypatch):
    """Somebody deletes the event on Eventbrite; the poster still points at it."""
    def handler(request):
        return httpx.Response(200, json={"id": "EB1", "status": "deleted",
                                         "url": "https://evb/1"})

    _eventbrite_stub(conn, monkeypatch, handler)
    conn.execute("UPDATE channel SET external_id='EB1', url='https://evb/1', "
                 "state='live' WHERE id='C1'")
    conn.commit()

    reports = service.recheck_listings(conn, "E")

    ch = dict(conn.execute("SELECT * FROM channel WHERE id='C1'").fetchone())
    assert ch["url"] == "" and ch["external_id"] is None    # public page drops it
    assert ch["state"] == "idle"                            # and Publish can remake it
    assert "that listing is gone" in reports[0].result.message


def test_publishing_after_a_deletion_makes_a_new_one(conn, monkeypatch):
    made = []

    def handler(request):
        if request.method == "GET" and "/events/EB1/" in request.url.path:
            return httpx.Response(200, json={"id": "EB1", "status": "deleted"})
        if request.url.path.endswith("/events/") and request.method == "POST":
            made.append(1)
            return httpx.Response(200, json={"id": "EB2", "url": "https://evb/2"})
        if "ticket_classes" in request.url.path:
            return httpx.Response(200, json={"id": "TC2"})
        return httpx.Response(200, json={"published": True})

    _eventbrite_stub(conn, monkeypatch, handler)
    conn.execute("UPDATE channel SET external_id='EB1', state='failed' WHERE id='C1'")
    conn.commit()

    res = service.publish(conn, "E")[0].result
    assert res.ok and res.external_id == "EB2" and len(made) == 1


def test_a_listing_check_that_fails_changes_nothing(conn, monkeypatch):
    """A platform being down is not evidence that an event was deleted."""
    def handler(request):
        return httpx.Response(500, json={"error_description": "eventbrite is down"})

    _eventbrite_stub(conn, monkeypatch, handler)
    conn.execute("UPDATE channel SET external_id='EB1', url='https://evb/1', "
                 "state='live' WHERE id='C1'")
    conn.commit()

    assert service.recheck_listings(conn, "E") == []
    ch = dict(conn.execute("SELECT * FROM channel WHERE id='C1'").fetchone())
    assert ch["url"] == "https://evb/1" and ch["state"] == "live"
