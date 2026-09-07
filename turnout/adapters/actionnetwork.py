"""Action Network adapter.

Action Network is not another place to publish — for most of the groups this
is built for it is *the list*, the thing they already organise out of. So
this adapter is deliberately shaped the other way round from Eventbrite and
Luma, and it is worth being exact about why.

Action Network's own documentation says of events posted through the API:

    "API-created events will not show up in lists of all of your actions on
    your dashboard or group manage page, they will not be given a URL on
    actionnetwork.org where people can sign, they will not have individual
    manage pages for statistics, and they will not send out autoresponses or
    reminders."

    — https://actionnetwork.org/docs/v2/events

An event nobody can sign is not a channel. So Turnout does not POST events
here and does not pretend to: you make the event in Action Network, where it
gets a real RSVP page, reminders and autoresponses, and paste that address
into the channel. Turnout then finds the matching event through the API and
does the two things the API genuinely does well:

  * **reads the RSVPs back**, with names and email addresses, into the one
    consolidated list — which is the whole point of Turnout;
  * **pushes edits** to the event record, so a moved venue is changed once.

What it cannot do, and says so rather than discovering it at the worst
moment: `capacity` and `visibility` are system-generated and not editable, so
no cap can be pushed; a host group's `status` change is *silently ignored* by
the API, so cancelling is a job for a human; and neither events nor
attendances can be deleted through the API at all.

NOTE: written against the v2 documentation and exercised in tests against a
recorded transport. It has NOT been run against a live Action Network API
key. Verify the attendance statuses on first real use.
"""

from __future__ import annotations

import json
import re

import httpx

from .base import RemoteSignup, Result, register, zoned_iso

BASE = "https://actionnetwork.org/api/v2"

# Attendance statuses. Action Network notes that only 'accepted' and
# 'attended' are set by the system; the rest come from the wider OSDI
# vocabulary. A 'tentative' or unrecognised RSVP is counted as going — a
# place quietly not counted fills the room twice over, which is worse than
# one counted early.
STATUS = {
    "accepted": "going",
    "attended": "going",
    "tentative": "going",
    "needs action": "going",
    "declined": "cancelled",
}

# How far to page. 25 per page is Action Network's enforced maximum, and each
# attendance costs a further call to read the person, so this is 500 RSVPs and
# up to 520 requests. Well past the size of any meeting in a hall.
PAGES = 20

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                   re.I)


@register
class ActionNetworkAdapter:
    kind = "actionnetwork"

    def __init__(self, credential: dict | None = None,
                 client: httpx.Client | None = None) -> None:
        credential = credential or {}
        self.token = credential.get("secret", "")
        cfg = credential.get("config") or {}
        if isinstance(cfg, str):
            cfg = json.loads(cfg or "{}")
        self.config = cfg
        self._client = client
        self._people: dict[str, dict] = {}      # per-call cache, not persisted

    # ---------------------------------------------------------------- http

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=BASE, timeout=20.0,
                headers={"OSDI-API-Token": self.token,
                         "Content-Type": "application/json"},
            )
        return self._client

    def _call(self, method: str, path: str, **kw) -> dict:
        r = self.client.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("error") or body.get("message") or r.text
            except Exception:
                msg = r.text[:300]
            raise RuntimeError(f"{r.status_code}: {self._redact(str(msg))}")
        return r.json() if r.content else {}

    def _redact(self, msg: str) -> str:
        """Keep the key out of the message.

        Action Network answers a bad key with `API Key invalid or not present
        <the key>`, and every adapter message is written to the event log and
        shown in the interface. A token that leaks into a log the group later
        pastes into a support email is a real incident, so it never gets that
        far.
        """
        return msg.replace(self.token, "…") if self.token else msg

    def check(self) -> str:
        """That the key works, and roughly what it is pointed at.

        The entry point answers 200 to any key at all, so this asks for
        events — which is what a wrong key is actually refused for.
        """
        data = self._call("GET", "/events", params={"per_page": 1})
        return f"key accepted — {data.get('total_records', 0)} events on this list"

    # ------------------------------------------------------------- lookup

    def find_event(self, url: str) -> dict | None:
        """The Action Network event whose page is at `url`.

        An RSVP page address carries no id, so short of the organiser pasting
        an API URL there is nothing to do but read the collection and match.
        Bounded, because an unbounded walk of somebody's whole event history
        is not a thing to do behind a button press.
        """
        direct = _UUID.search(url or "")
        if direct and "/api/v2/events/" in (url or ""):
            return self._call("GET", f"/events/{direct.group(0)}")

        want = _canonical(url)
        if not want:
            return None
        page = 1
        while page <= PAGES:
            data = self._call("GET", "/events", params={"page": page})
            for ev in (data.get("_embedded") or {}).get("osdi:events", []):
                if _canonical(ev.get("browser_url")) == want:
                    return ev
            if page >= (data.get("total_pages") or 1):
                return None
            page += 1
        return None

    # ------------------------------------------------------------- payload

    def _event_payload(self, event: dict) -> dict:
        """Only the fields Action Network will actually accept on a PUT.

        `capacity`, `visibility` and `status` are left out on purpose: the
        first two are refused as system-generated and the third is silently
        ignored for the group that owns the event, which is worse — it would
        look like it worked.
        """
        tz = event.get("timezone") or "Europe/London"
        body = {
            "title": event["title"],
            "description": _html(event),
        }
        if event.get("starts_at"):
            body["start_date"] = zoned_iso(event["starts_at"], tz)
        if event.get("ends_at"):
            body["end_date"] = zoned_iso(event["ends_at"], tz)
        location = {}
        if event.get("venue"):
            location["venue"] = event["venue"]
        if event.get("address"):
            location["address_lines"] = [event["address"]]
        if location:
            body["location"] = location
        return body

    # ------------------------------------------------------------- actions

    def publish(self, event: dict, channel: dict, quantity: int | None) -> Result:
        """Adopt the event the organiser made, rather than creating one.

        Creating one through the API would produce a record with no page
        anybody can sign — a channel in name only. Turnout would rather say
        what to do than hand over something that looks published and isn't.
        """
        if channel.get("external_id"):
            return Result.done("already linked to that Action Network event",
                               external_id=channel["external_id"])
        url = (channel.get("url") or "").strip()
        if not url:
            return Result.needs_you(
                "Action Network's API can't put up an event people can sign — "
                "events posted through it get no page on actionnetwork.org. "
                "Make it there, paste the address in, and Turnout will read "
                "the RSVPs back.")
        try:
            found = self.find_event(url)
        except Exception as exc:
            return Result.failed(str(exc))
        if found is None:
            return Result.needs_you(
                "couldn't find that event under this API key. Check the "
                "address, and that the key belongs to the group that owns "
                "the event. The link still works on the public page.")
        eid = _an_id(found)
        if not eid:
            return Result.failed("that event came back without an identifier")
        return Result.done(
            f"linked to your Action Network event — RSVPs will come back "
            f"with names and email addresses ({found.get('total_accepted', 0)} "
            f"so far)",
            external_id=eid, url=found.get("browser_url") or url)

    def update(self, event: dict, channel: dict, quantity: int | None,
               changed: list[str]) -> Result:
        eid = channel.get("external_id")
        if not eid:
            return Result.failed("not linked to an Action Network event yet")
        try:
            self._call("PUT", f"/events/{eid}", json=self._event_payload(event))
            pushed = [c for c in changed if c != "capacity"]
            message = "updated " + (", ".join(pushed) or "event")
            if "capacity" in changed:
                # Said every time it is edited, because it is the one change
                # that looks like it went through and didn't.
                message += (" — the capacity is not editable through the API, "
                            "so set it in Action Network yourself")
            return Result.done(message)
        except Exception as exc:
            return Result.failed(str(exc))

    def set_open(self, event: dict, channel: dict, open_: bool,
                 policy: str) -> Result:
        """Nothing in the v2 API stops an Action Network event taking RSVPs.

        `capacity` and `visibility` are not editable, and a status change from
        the group that owns the event is silently ignored. So this is a job
        handed back, clearly, rather than a call that quietly does nothing.
        """
        return Result.needs_you(
            "reopen sign-ups in Action Network yourself" if open_ else
            "close or unpublish the event in Action Network yourself — the "
            "API has no way to stop RSVPs")

    def fetch_signups(self, event: dict, channel: dict) -> list[RemoteSignup]:
        eid = channel.get("external_id")
        if not eid:
            return []
        out: list[RemoteSignup] = []
        page = 1
        while page <= PAGES:
            data = self._call("GET", f"/events/{eid}/attendances",
                              params={"page": page})
            for a in (data.get("_embedded") or {}).get("osdi:attendances", []):
                person = self._person(a)
                out.append(RemoteSignup(
                    external_id=_an_id(a) or "",
                    name=_name(person),
                    email=_email(person),
                    status=STATUS.get((a.get("status") or "").lower(), "going"),
                    created_at=a.get("created_date"),
                ))
            if page >= (data.get("total_pages") or 1):
                break
            page += 1
        return out

    def _person(self, attendance: dict) -> dict:
        """The activist behind an RSVP, which costs a call each.

        Attendances carry a link to the person, not the person, so names and
        email addresses are one request apiece. A person Turnout can't read
        gives an empty row rather than losing the sign-up: the merge already
        knows what to do with a name it can't match.
        """
        pid = attendance.get("action_network:person_id")
        if not pid:
            href = ((attendance.get("_links") or {}).get("osdi:person") or {}).get("href", "")
            match = _UUID.search(href)
            pid = match.group(0) if match else None
        if not pid:
            return {}
        if pid not in self._people:
            try:
                self._people[pid] = self._call("GET", f"/people/{pid}")
            except Exception:
                self._people[pid] = {}
        return self._people[pid]

    def delete_signups(self, event: dict, channel: dict) -> Result:
        """Action Network does not allow deletion through the API at all.

        Saying exactly where the data still is, and that Turnout cannot reach
        it, is the only honest answer. A group that believes an RSVP list is
        gone when it isn't is worse off than one that knows where to go.
        """
        return Result.needs_you(
            "removed from Turnout. Action Network keeps its own RSVPs and its "
            "API cannot delete them — remove the attendances, and the people "
            "if they should go, in Action Network itself.")


def _canonical(url: str | None) -> str:
    """An address reduced to what identifies the page.

    Organisers paste links with a tracking parameter on the end, or without
    the https, or with a trailing slash. None of that changes which event it
    is.
    """
    if not url:
        return ""
    u = url.strip().lower().split("?")[0].split("#")[0]
    u = u.removeprefix("https://").removeprefix("http://").removeprefix("www.")
    return u.rstrip("/")


def _an_id(resource: dict) -> str | None:
    """The bare uuid out of `["action_network:<uuid>"]`."""
    for ident in resource.get("identifiers") or []:
        if ident.startswith("action_network:"):
            return ident.split(":", 1)[1]
    match = _UUID.search(((resource.get("_links") or {}).get("self") or {}).get("href", ""))
    return match.group(0) if match else None


def _name(person: dict) -> str:
    return " ".join(x for x in (person.get("given_name"),
                                person.get("family_name")) if x)


def _email(person: dict) -> str | None:
    addresses = person.get("email_addresses") or []
    primary = next((a for a in addresses if a.get("primary")), None)
    chosen = primary or (addresses[0] if addresses else None)
    return (chosen or {}).get("address")


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
