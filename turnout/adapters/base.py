"""Channel adapters and the capability model.

Every platform gets described honestly before it gets called. The UI reads
this matrix to decide which controls to render, so a control that can't work
is never shown — the organiser is told the limit instead of discovering it
when a button fails.

Values: 'yes' | 'partial' | 'no' | 'manual'
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    "luma": {
        "create": YES, "update": YES, "cancel": YES,
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
         "atproto": "AT", "facebook": "FB", "meetup": "MU"}

NAMES = {"manual": "Typed-in link", "eventbrite": "Eventbrite", "luma": "Luma",
         "atproto": "atmo.rsvp / atproto", "facebook": "Facebook Events",
         "meetup": "Meetup"}

# What the organiser is told, in the interface, about each channel's limits.
NOTES = {
    "manual": "Turnout only stores the address. Everything else you do on the "
              "platform itself.",
    "eventbrite": "Free. Creates a draft, adds a free ticket type, then "
                  "publishes. Closing moves ticket sales to end now, because "
                  "Eventbrite refuses to unpublish once anyone has registered.",
    "luma": "Needs a Luma Plus subscription for API access.",
    "atproto": "The event is a record in your group's own repository. Nobody "
               "can enforce a cap — an RSVP is someone writing to their own "
               "account. Public and permanent.",
    "facebook": "Cannot be automated. Turnout prepares the text and hands you "
                "the composer; paste the resulting address back in.",
    "meetup": "Needs a Meetup Pro subscription. Gives you names but never "
              "email addresses.",
}


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
