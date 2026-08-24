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

BASE = "https://www.eventbriteapi.com/v3"


def _utc(local_iso: str, tz: str) -> str:
    """Eventbrite wants UTC as 'YYYY-MM-DDTHH:MM:SSZ' plus a timezone name.

    We keep the group's wall-clock time as typed and let the platform do the
    conversion, so a change of daylight saving never silently moves an event.
    """
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(local_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return local_iso


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
        try:
            org = self.whoami()
            created = self._call("POST", f"/organizations/{org}/events/",
                                 json=self._event_payload(event))
            eid = created["id"]

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
            self._call("POST", f"/events/{eid}/publish/")

            return Result.done(
                "created, ticketed and published",
                external_id=eid,
                url=created.get("url", ""),
                data={"ticket_class_id": ticket["id"]},
            )
        except Exception as exc:
            return Result.failed(str(exc))

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
