"""Channel adapters and the capability model.

Every platform gets described honestly before it gets called. The UI reads
this matrix to decide which controls to render, so a control that can't work
is never shown — the organiser is told the limit instead of discovering it
when a button fails.

Values: 'yes' | 'partial' | 'no' | 'manual'
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

YES, PARTIAL, NO, MANUAL = "yes", "partial", "no", "manual"

CAPABILITIES: dict[str, dict[str, str]] = {
    # Step 1: a URL somebody typed in. Knows nothing, promises nothing.
    "manual": {
        "create": MANUAL, "update": MANUAL, "cancel": MANUAL,
        "capacity": NO, "waitlist": NO, "close": MANUAL,
        "read_signups": NO, "emails": NO, "live": NO,
    },
    "eventbrite": {
        "create": YES, "update": YES, "cancel": YES,
        "capacity": YES, "waitlist": PARTIAL, "close": PARTIAL,
        "read_signups": YES, "emails": YES, "live": YES,
    },
    # The odd one out, and on purpose. Action Network is the group's own list
    # rather than another shop window: events posted through its API get no
    # page anybody can sign, so the organiser makes the event there and
    # Turnout reads the RSVPs back. Capacity and visibility are
    # system-generated, and a status change by the owning group is silently
    # ignored — hence `manual` for the three it cannot honour.
    "actionnetwork": {
        "create": MANUAL, "update": YES, "cancel": MANUAL,
        "capacity": NO, "waitlist": NO, "close": MANUAL,
        "read_signups": YES, "emails": YES, "live": NO,
    },
    # Cancel is MANUAL on purpose: the v1 API has no delete and no cancel
    # endpoint, so calling an event off is a job for a human on Luma.
    "luma": {
        "create": YES, "update": YES, "cancel": MANUAL,
        "capacity": YES, "waitlist": YES, "close": YES,
        "read_signups": YES, "emails": YES, "live": YES,
    },
    "atproto": {
        "create": YES, "update": YES, "cancel": YES,
        "capacity": NO, "waitlist": NO, "close": NO,
        "read_signups": PARTIAL, "emails": NO, "live": YES,
    },
    "facebook": {
        "create": MANUAL, "update": MANUAL, "cancel": MANUAL,
        "capacity": NO, "waitlist": NO, "close": MANUAL,
        "read_signups": NO, "emails": NO, "live": NO,
    },
    "meetup": {
        "create": YES, "update": YES, "cancel": YES,
        "capacity": YES, "waitlist": YES, "close": YES,
        "read_signups": PARTIAL, "emails": NO, "live": NO,
    },
}

CODES = {"manual": "··", "eventbrite": "EB", "luma": "LU",
         "atproto": "AT", "facebook": "FB", "meetup": "MU",
         "actionnetwork": "AN"}

NAMES = {"manual": "Typed-in link", "eventbrite": "Eventbrite", "luma": "Luma",
         "atproto": "atmo.rsvp / atproto", "facebook": "Facebook Events",
         "meetup": "Meetup", "actionnetwork": "Action Network"}

# What the organiser is told, in the interface, about each channel's limits.
NOTES = {
    "manual": "Turnout only stores the address. Everything else you do on the "
              "platform itself.",
    "eventbrite": "Free. Creates a draft, adds a free ticket type, then "
                  "publishes. Closing moves ticket sales to end now, because "
                  "Eventbrite refuses to unpublish once anyone has registered.",
    "luma": "Needs a Luma Plus subscription. One call puts the event up — no "
            "draft step. The key belongs to one calendar, so the key is the "
            "destination. Closing sign-ups closes registration and leaves the "
            "page readable; cancelling outright is not in the API, so do that "
            "on Luma.",
    "atproto": "The event is a record in your group's own repository. Nobody "
               "can enforce a cap — an RSVP is someone writing to their own "
               "account. Public and permanent.",
    "facebook": "Cannot be automated. Turnout prepares the text and hands you "
                "the composer; paste the resulting address back in.",
    "meetup": "Needs a Meetup Pro subscription. Gives you names but never "
              "email addresses.",
    "actionnetwork": "You make the event in Action Network — events posted "
                     "through its API get no page people can sign — and paste "
                     "the address in. Turnout then reads the RSVPs back with "
                     "email addresses and pushes edits. It cannot set a "
                     "capacity, close sign-ups or delete anything there.",
}


def utc_iso(local_iso: str, tz: str) -> str:
    """A wall-clock time as UTC 'YYYY-MM-DDTHH:MM:SSZ'.

    Every platform so far wants the instant in UTC alongside the timezone
    name. We keep the group's wall-clock time exactly as typed and convert
    only on the way out, so a change of daylight saving never silently moves
    an event. Shared, because two adapters getting this subtly different is
    the kind of bug nobody finds until the clocks go back.
    """
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(local_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return local_iso


def zoned_iso(local_iso: str, tz: str) -> str:
    """A wall-clock time with its own UTC offset attached.

    For platforms that read a timestamp as local to the event rather than as
    an instant — Action Network says the time "is assumed to be in the
    timezone local to the event's location". Sending 19:00 with +01:00 on it
    reads correctly whether the far end honours the offset or throws it away
    and keeps the wall clock; sending plain UTC would land an hour out in
    British Summer Time, every time.
    """
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(local_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz))
        return dt.isoformat(timespec="seconds")
    except Exception:
        return local_iso


def capability(kind: str, name: str) -> str:
    return CAPABILITIES.get(kind, CAPABILITIES["manual"]).get(name, NO)


def can(kind: str, name: str) -> bool:
    """True when the platform can do this by itself, without a human."""
    return capability(kind, name) in (YES, PARTIAL)


@dataclass
class Result:
    """The outcome of asking a platform to do something.

    `manual` means the platform can't, and the organiser now has a job. It is
    not an error and must never be presented as one.
    """
    ok: bool
    message: str
    external_id: str | None = None
    url: str | None = None
    manual: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def done(cls, message: str, **kw) -> "Result":
        return cls(ok=True, message=message, **kw)

    @classmethod
    def needs_you(cls, message: str, **kw) -> "Result":
        return cls(ok=True, message=message, manual=True, **kw)

    @classmethod
    def failed(cls, message: str, **kw) -> "Result":
        return cls(ok=False, message=message, **kw)


@dataclass
class RemoteSignup:
    external_id: str
    name: str
    email: str | None = None
    handle: str | None = None
    status: str = "going"           # going | waitlist | cancelled
    created_at: str | None = None


class Adapter(Protocol):
    kind: str

    def publish(self, event: dict, channel: dict, quantity: int | None) -> Result: ...
    def update(self, event: dict, channel: dict, quantity: int | None,
               changed: list[str]) -> Result: ...
    def set_open(self, event: dict, channel: dict, open_: bool,
                 policy: str) -> Result: ...
    def fetch_signups(self, event: dict, channel: dict) -> list[RemoteSignup]: ...
    def delete_signups(self, event: dict, channel: dict) -> Result: ...


_REGISTRY: dict[str, Any] = {}


def register(adapter_cls) -> Any:
    _REGISTRY[adapter_cls.kind] = adapter_cls
    return adapter_cls


def get(kind: str, credential: dict | None = None) -> Adapter:
    cls = _REGISTRY.get(kind)
    if cls is None:
        from .manual import ManualAdapter
        return ManualAdapter()
    try:
        return cls(credential)
    except TypeError:
        return cls()
