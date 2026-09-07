"""Eventbrite adapter.

Publishing is a three-step dance the API insists on:

    1. POST an event to the organisation  -> creates a DRAFT
    2. POST a ticket class to that event  -> a draft with no tickets
                                             cannot be published
    3. POST .../publish/                  -> live

Closing sign-ups is the interesting one. Eventbrite refuses to unpublish an
event that has any orders against it, which is exactly the situation you are
in when the room fills. So "close" means moving the ticket class's sales end
to now: the page stays up, the history stays intact, and nobody else can
register. That is a `partial` capability and the interface says so.

NOTE: written against the documented v3 endpoints and exercised in tests
against a recorded transport. It has NOT been run against a live Eventbrite
organisation — verify field names on first real use, especially the
attendee status strings, before trusting it with a real event.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from .base import PARTIAL, Result, RemoteSignup, register
from .base import utc_iso as _utc

BASE = "https://www.eventbriteapi.com/v3"


@register
class EventbriteAdapter:
    kind = "eventbrite"

    def __init__(self, credential: dict | None = None,
                 client: httpx.Client | None = None) -> None:
        credential = credential or {}
        self.token = credential.get("secret", "")
        cfg = credential.get("config") or {}
        if isinstance(cfg, str):
            cfg = json.loads(cfg or "{}")
        self.organization_id = cfg.get("organization_id")
        self._client = client

    # ---------------------------------------------------------------- http

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=BASE, timeout=20.0,
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
            )
        return self._client

    def _call(self, method: str, path: str, **kw) -> dict:
        r = self.client.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("error_description") or body.get("error") or r.text
            except Exception:
                msg = r.text[:300]
            raise RuntimeError(f"{r.status_code}: {msg}")
        return r.json() if r.content else {}

    def whoami(self) -> str:
        """Resolve the organisation id, so the organiser never types one."""
        if self.organization_id:
            return self.organization_id
        data = self._call("GET", "/users/me/organizations/")
        orgs = data.get("organizations", [])
        if not orgs:
            raise RuntimeError("this token has no organisations")
        self.organization_id = orgs[0]["id"]
        return self.organization_id

    def check(self) -> str:
        """What this token opens, in words the organiser can recognise."""
        return f"organisation {self.whoami()}"

    def remote_state(self, channel: dict) -> dict | None:
        """What Eventbrite says about the listing we recorded.

        Turnout's own `state` says what happened last time it called; it does
        not know that somebody deleted the event in Eventbrite half an hour
        ago. Nobody finds out until a person follows the link off a poster,
        so it is worth one call to ask.
        """
        eid = channel.get("external_id")
        if not eid:
            return None
        try:
            ev = self._call("GET", f"/events/{eid}/")
        except Exception as exc:
            if "404" in str(exc):
                return {"state": "gone", "status": "not found", "url": ""}
            raise
        status = (ev.get("status") or "").lower()
        if status in ("deleted", "canceled", "cancelled"):
            return {"state": "gone", "status": status, "url": ev.get("url", "")}
        if status == "draft":
            return {"state": "draft", "status": status, "url": ev.get("url", "")}
        return {"state": "live", "status": status, "url": ev.get("url", "")}

    # ------------------------------------------------------------- payload

    def _event_payload(self, event: dict) -> dict:
        tz = event.get("timezone") or "Europe/London"
        body = {
            "name": {"html": event["title"]},
            "description": {"html": _html(event)},
            "start": {"timezone": tz, "utc": _utc(event["starts_at"], tz)},
            "currency": "GBP",
            "listed": True,
            "shareable": True,
        }
        if event.get("ends_at"):
            body["end"] = {"timezone": tz, "utc": _utc(event["ends_at"], tz)}
        if event.get("capacity"):
            body["capacity"] = int(event["capacity"])
        return {"event": body}

    # ------------------------------------------------------------ actions

    def publish(self, event: dict, channel: dict, quantity: int | None) -> Result:
        """Create, ticket, publish — resuming rather than starting over.

        Three calls, and a failure at the second or third leaves a real draft
        event on Eventbrite. So the id of anything created is carried back on
        the failure too, and a second attempt picks up from whichever step is
        missing. Pressing the button twice must never leave a group with two
        events and no way to tell which one the posters point at.
        """
        eid = channel.get("external_id")
        url = channel.get("url") or ""
        tid = _cfg(channel).get("ticket_class_id")
        made_it_now = False
        try:
            if eid and (self.remote_state(channel) or {}).get("state") == "gone":
                # Deleted in Eventbrite since. Resuming it would fail forever;
                # a fresh one is what the organiser is asking for.
                eid, tid, url = None, None, ""
            if not eid:
                org = self.whoami()
                created = self._call("POST", f"/organizations/{org}/events/",
                                     json=self._event_payload(event))
                eid, url, made_it_now = created["id"], created.get("url", ""), True
        except Exception as exc:
            return Result.failed(str(exc))

        try:
            if not tid:
                ticket = self._call(
                    "POST", f"/events/{eid}/ticket_classes/",
                    json={"ticket_class": {
                        "name": "Free entry",
                        "free": True,
                        "quantity_total": quantity or event.get("capacity") or 1000,
                        "minimum_quantity": 1,
                        "maximum_quantity": 4,
                    }},
                )
                tid = ticket["id"]
            answer = self._call("POST", f"/events/{eid}/publish/")
            if answer.get("published") is False:
                # A 200 that says it did not publish. Reported as success once,
                # which is how a draft nobody could open ended up on a poster.
                raise RuntimeError(
                    "Eventbrite would not publish it — it is still a draft "
                    "there. Open it in Eventbrite; it will name what it wants.")
            if not url:
                url = (self.remote_state({"external_id": eid}) or {}).get("url", "")
        except Exception as exc:
            # The event exists whatever went wrong after it was created. Hand
            # its id back so the next attempt finishes this one.
            return Result.failed(str(exc), external_id=eid, url=url,
                                 data={"ticket_class_id": tid} if tid else {})

        return Result.done(
            "created, ticketed and published" if made_it_now
            else "picked up the event already created here and published it",
            external_id=eid, url=url, data={"ticket_class_id": tid})

    def update(self, event: dict, channel: dict, quantity: int | None,
               changed: list[str]) -> Result:
        eid = channel.get("external_id")
        if not eid:
            return Result.failed("not published here yet")
        try:
            self._call("POST", f"/events/{eid}/", json=self._event_payload(event))
            if quantity is not None:
                tid = _cfg(channel).get("ticket_class_id")
                if tid:
                    self._call("POST", f"/events/{eid}/ticket_classes/{tid}/",
                               json={"ticket_class": {"quantity_total": quantity}})
            return Result.done("updated " + (", ".join(changed) or "event"))
        except Exception as exc:
            return Result.failed(str(exc))

    def set_open(self, event: dict, channel: dict, open_: bool, policy: str) -> Result:
        """Close by ending ticket sales, not by unpublishing.

        Unpublish is refused once orders exist, which is precisely when you
        want to close. Moving sales_end works in every case and leaves the
        page readable, which matters — people still need the address.
        """
        eid = channel.get("external_id")
        tid = _cfg(channel).get("ticket_class_id")
        if not eid or not tid:
            return Result.failed("not published here yet")
        try:
            if open_:
                payload = {"sales_end": _utc(event["starts_at"],
                                             event.get("timezone", "Europe/London"))}
                msg = "ticket sales reopened until the event starts"
            else:
                payload = {"sales_end": datetime.now(timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%SZ")}
                msg = "ticket sales ended — page stays up, no new registrations"
            self._call("POST", f"/events/{eid}/ticket_classes/{tid}/",
                       json={"ticket_class": payload})
            return Result.done(msg)
        except Exception as exc:
            return Result.failed(str(exc))

    def fetch_signups(self, event: dict, channel: dict) -> list[RemoteSignup]:
        eid = channel.get("external_id")
        if not eid:
            return []
        out: list[RemoteSignup] = []
        token, page = None, 0
        while page < 20:                       # hard stop: no unbounded paging
            params = {"page_size": 100}
            if token:
                params["continuation"] = token
            data = self._call("GET", f"/events/{eid}/attendees/", params=params)
            for a in data.get("attendees", []):
                profile = a.get("profile") or {}
                out.append(RemoteSignup(
                    external_id=str(a["id"]),
                    name=profile.get("name") or "",
                    email=profile.get("email"),
                    status=_status(a),
                    created_at=a.get("created"),
                ))
            pagination = data.get("pagination") or {}
            token = pagination.get("continuation")
            if not pagination.get("has_more_items") or not token:
                break
            page += 1
        return out

    def delete_signups(self, event: dict, channel: dict) -> Result:
        """Eventbrite keeps orders; we can only drop our own copy.

        Saying so plainly is the point — an organiser who believes the data
        is gone everywhere, when it isn't, is worse off than one who knows
        where it still lives.
        """
        return Result.needs_you(
            "removed from Turnout. Eventbrite keeps its own order records — "
            "delete them in your Eventbrite account if you need them gone."
        )


def _cfg(channel: dict) -> dict:
    raw = channel.get("config") or "{}"
    return json.loads(raw) if isinstance(raw, str) else raw


def _status(attendee: dict) -> str:
    if attendee.get("cancelled") or attendee.get("refunded"):
        return "cancelled"
    s = (attendee.get("status") or "").lower()
    if "not attending" in s or "deleted" in s:
        return "cancelled"
    return "going"


def _html(event: dict) -> str:
    parts = []
    if event.get("strap"):
        parts.append(f"<p><strong>{event['strap']}</strong></p>")
    for para in (event.get("body") or "").split("\n\n"):
        if para.strip():
            parts.append(f"<p>{para.strip()}</p>")
    if event.get("accessibility"):
        parts.append(f"<p><em>{event['accessibility']}</em></p>")
    return "".join(parts)
