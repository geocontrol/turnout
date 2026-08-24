from .base import (  # noqa: F401
    CAPABILITIES, CODES, NAMES, NOTES, MANUAL, NO, PARTIAL, YES,
    Adapter, RemoteSignup, Result, can, capability, get, register,
)
from . import manual  # noqa: F401,E402  registers manual + facebook
from . import eventbrite  # noqa: F401,E402  registers eventbrite

KINDS = ["eventbrite", "atproto", "facebook", "luma", "meetup", "manual"]

# Kinds with a working adapter today. The rest are declared in the
# capability matrix so the interface can be honest about what is coming,
# but selecting one gets you the manual behaviour.
IMPLEMENTED = {"manual", "facebook", "eventbrite"}

# Kinds that need saved access before Turnout can drive them. A channel of
# one of these kinds without a credential is not broken — it just behaves
# like a typed-in link until access is added.
NEEDS_ACCESS = {"eventbrite", "luma", "meetup", "atproto"}

# What to ask for, per kind, so the form can be specific rather than generic.
ACCESS_FIELDS = {
    "eventbrite": {
        "secret_label": "Private token",
        "secret_hint": "Eventbrite → Account settings → Developer links → API keys",
        "extra": ("organization_id", "Organisation id",
                  "leave blank and Turnout will find it"),
    },
    "luma": {
        "secret_label": "API key",
        "secret_hint": "Luma → Calendar settings → API (needs Luma Plus)",
        "extra": ("calendar_id", "Calendar id", "e.g. cal-abc123"),
    },
    "meetup": {
        "secret_label": "OAuth token",
        "secret_hint": "Needs a Meetup Pro subscription",
        "extra": ("group_urlname", "Group address", "the bit after meetup.com/"),
    },
    "atproto": {
        "secret_label": "App password",
        "secret_hint": "Bluesky → Settings → App passwords. Never your account password.",
        "extra": ("handle", "Handle", "e.g. yourgroup.org"),
    },
}
