"""Luma adapter.

Luma is the shortest path from a composed event to a live page: one POST
creates it — no draft, no ticket class, no separate publish call — and the
guest list comes back with email addresses in it.

Three things shape this adapter.

  * **A key belongs to one calendar.** There is nothing to choose and nothing
    to type in beside it, so the access form asks for the key and nothing
    else. `check()` reports which calendar the key opened, because a key
    pasted from the wrong calendar is otherwise silent until an event lands
    in the wrong place.
  * **Capacity is Luma's own two settings**, not something reimplemented
    here. `max_capacity` with `waitlist_status: enabled` is exactly the
    channel policy "waiting list"; with it disabled, "close it".
  * **There is no cancel and no delete in the API.** Closing sign-ups sets
    `registration_open` to false, which is a real close — the page stays
    readable, which matters because people still need the address. Calling
    an event off, and removing a guest, are done on Luma by a human, and the
    capability matrix says so rather than pretending otherwise.

NOTE: written against the published v1 description at
https://public-api.luma.com/openapi.json and exercised in tests against a
recorded transport. It has NOT been run against a live Luma Plus calendar —
verify the guest fields on first real use.
"""

from __future__ import annotations

import json

import httpx

from .base import RemoteSignup, Result, register, utc_iso

BASE = "https://public-api.luma.com"

# How Luma's guest states map onto the three Turnout keeps.
#
# `invited` is deliberately absent: somebody was sent an invitation and has
# not answered. That is not a sign-up, and counting it would overstate the
# turnout. Anything unrecognised is counted as going — a place quietly not
# counted fills the room twice over, which is worse than one counted early.
STATUS = {
    "approved": "going",
    "session": "going",
    "waitlist": "waitlist",
    "pending_approval": "waitlist",
    "declined": "cancelled",
}
SKIP = {"invited"}


@register
class LumaAdapter:
    kind = "luma"

    def __init__(self, credential: dict | None = None,
                 client: httpx.Client | None = None) -> None:
        credential = credential or {}
        self.key = credential.get("secret", "")
        cfg = credential.get("config") or {}
        if isinstance(cfg, str):
            cfg = json.loads(cfg or "{}")
        self.config = cfg
        self._client = client

    # ---------------------------------------------------------------- http

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=BASE, timeout=20.0,
                headers={"x-luma-api-key": self.key,
                         "Content-Type": "application/json"},
            )
        return self._client

    def _call(self, method: str, path: str, **kw) -> dict:
        r = self.client.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("message") or body.get("error") or r.text
            except Exception:
                msg = r.text[:300]
            raise RuntimeError(f"{r.status_code}: {msg}")
        return r.json() if r.content else {}

    def check(self) -> str:
        """What this key opens, in words the organiser can recognise."""
        cal = self._call("GET", "/v1/calendars/get")
        return f"calendar {cal.get('name') or cal.get('slug') or cal.get('id')}"

    # ------------------------------------------------------------- payload

    def _event_payload(self, event: dict) -> dict:
        tz = event.get("timezone") or "Europe/London"
        body = {
            "name": event["title"],
            "timezone": tz,
            "start_at": utc_iso(event["starts_at"], tz),
            "visibility": "public",
        }
        if event.get("ends_at"):
            body["end_at"] = utc_iso(event["ends_at"], tz)
        description = _markdown(event)
        if description:
            body["description_md"] = description
        where = ", ".join(x for x in (event.get("venue"), event.get("address")) if x)
        if where:
            body["geo_address_json"] = {"type": "manual", "address": where}
        return body

    def _capacity_payload(self, event: dict, channel: dict,
                          quantity: int | None) -> dict:
        """Turn the channel's policy into Luma's own capacity settings."""
        policy = channel.get("policy") or "waitlist"
        if policy == "open":
            # Leave it open when the room fills: no ceiling for Luma to enforce.
            return {"max_capacity": None, "waitlist_status": "disabled"}
        total = quantity if quantity is not None else event.get("capacity")
        out = {"waitlist_status": "enabled" if policy == "waitlist" else "disabled"}
        if total:
            out["max_capacity"] = int(total)
        return out

    def _event_url(self, eid: str) -> str:
        """The public address, which create doesn't return.

        Failing here must never fail the publish: the event is already up,
        and an organiser can paste the address in by hand.
        """
        try:
            data = self._call("GET", "/v1/events/get", params={"event_id": eid})
            ev = data.get("event") or data       # returned both ways over time
            return ev.get("url") or ""
        except Exception:
            return ""

    # ------------------------------------------------------------- actions

    def publish(self, event: dict, channel: dict, quantity: int | None) -> Result:
        if channel.get("external_id"):
            # Already made here. Creating a second one would leave a group
            # with two Luma pages and posters pointing at whichever.
            return Result.done("already up on Luma — edits go through Push",
                               external_id=channel["external_id"],
                               url=channel.get("url") or self._event_url(channel["external_id"]))
        try:
            payload = self._event_payload(event)
            payload.update(self._capacity_payload(event, channel, quantity))
            created = self._call("POST", "/v1/events/create", json=payload)
            eid = created["id"]
            return Result.done("created and live on Luma",
                               external_id=eid, url=self._event_url(eid))
        except Exception as exc:
            return Result.failed(str(exc))

    def update(self, event: dict, channel: dict, quantity: int | None,
               changed: list[str]) -> Result:
        eid = channel.get("external_id")
        if not eid:
            return Result.failed("not published here yet")
        try:
            payload = {"event_id": eid, **self._event_payload(event)}
            if quantity is not None:
                payload.update(self._capacity_payload(event, channel, quantity))
            # Notifications stay on: a moved venue or time is news the people
            # already signed up need, and Luma is the one holding their email.
            self._call("POST", "/v1/events/update", json=payload)
            return Result.done("updated " + (", ".join(changed) or "event"))
        except Exception as exc:
            return Result.failed(str(exc))

    def set_open(self, event: dict, channel: dict, open_: bool,
                 policy: str) -> Result:
        eid = channel.get("external_id")
        if not eid:
            return Result.failed("not published here yet")
        try:
            self._call("POST", "/v1/events/update", json={
                "event_id": eid,
                "registration_open": bool(open_),
                # Closing a full room is bookkeeping, not news. The people who
                # already have a place should not get an email about it.
                "suppress_notifications": True,
            })
            return Result.done(
                "registration reopened" if open_ else
                "registration closed — page stays up, no new sign-ups")
        except Exception as exc:
            return Result.failed(str(exc))

    def fetch_signups(self, event: dict, channel: dict) -> list[RemoteSignup]:
        eid = channel.get("external_id")
        if not eid:
            return []
        out: list[RemoteSignup] = []
        cursor, page = None, 0
        while page < 20:                       # hard stop: no unbounded paging
            params: dict = {"event_id": eid, "pagination_limit": 100}
            if cursor:
                params["pagination_cursor"] = cursor
            data = self._call("GET", "/v1/events/guests/list", params=params)
            for entry in data.get("entries", []):
                g = entry.get("guest") or entry     # carried both ways over time
                approval = (g.get("approval_status") or "").lower()
                if approval in SKIP:
                    continue
                out.append(RemoteSignup(
                    external_id=str(g.get("id") or g.get("api_id") or ""),
                    name=_name(g),
                    email=g.get("user_email") or g.get("email"),
                    status=STATUS.get(approval, "going"),
                    created_at=g.get("registered_at") or g.get("joined_at"),
                ))
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not cursor:
                break
            page += 1
        return out

    def delete_signups(self, event: dict, channel: dict) -> Result:
        """Luma keeps its guest list; we can only drop our own copy.

        The API has no guest delete, so saying plainly where the data still
        lives is the whole of what Turnout can honestly offer here.
        """
        return Result.needs_you(
            "removed from Turnout. Luma keeps its own guest list and the API "
            "cannot delete it — remove the guests in Luma if they need to be "
            "gone."
        )


def _name(guest: dict) -> str:
    if guest.get("user_name"):
        return guest["user_name"]
    both = " ".join(x for x in (guest.get("user_first_name"),
                                guest.get("user_last_name")) if x)
    if both:
        return both
    return guest.get("name") or ""


def _markdown(event: dict) -> str:
    """Luma takes Markdown and converts it to its own rich text."""
    parts = []
    if event.get("strap"):
        parts.append(f"**{event['strap']}**")
    body = (event.get("body") or "").strip()
    if body:
        parts.append(body)
    if event.get("accessibility"):
        parts.append(f"_{event['accessibility']}_")
    return "\n\n".join(parts)
