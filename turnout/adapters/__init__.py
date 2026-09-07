from .base import (  # noqa: F401
    CAPABILITIES, CODES, NAMES, NOTES, MANUAL, NO, PARTIAL, YES,
    Adapter, RemoteSignup, Result, can, capability, get, register,
)
from . import manual  # noqa: F401,E402  registers manual + facebook
from . import eventbrite  # noqa: F401,E402  registers eventbrite
from . import luma  # noqa: F401,E402  registers luma
from . import actionnetwork  # noqa: F401,E402  registers actionnetwork
from . import tickettailor  # noqa: F401,E402  registers tickettailor

KINDS = ["eventbrite", "luma", "tickettailor", "actionnetwork", "atproto",
         "facebook", "meetup", "manual"]

# Kinds with a working adapter today. The rest are declared in the
# capability matrix so the interface can be honest about what is coming,
# but selecting one gets you the manual behaviour.
IMPLEMENTED = {"manual", "facebook", "eventbrite", "luma", "actionnetwork",
               "tickettailor"}

# Kinds that need saved access before Turnout can drive them. A channel of
# one of these kinds without a credential is not broken — it just behaves
# like a typed-in link until access is added.
NEEDS_ACCESS = {"eventbrite", "luma", "meetup", "atproto", "actionnetwork",
                "tickettailor"}

# What to ask for, per kind, so the form can be specific rather than generic.
ACCESS_FIELDS = {
    "eventbrite": {
        "secret_label": "Private token",
        "secret_hint": "Eventbrite → Account settings → Developer links → API keys",
        "extra": ("organization_id", "Organisation id",
                  "leave blank and Turnout will find it"),
    },
    # No second field: a Luma key is issued for one calendar and carries the
    # destination with it. Asking for a calendar id would be asking for
    # something the organiser cannot get wrong or right.
    "luma": {
        "secret_label": "API key",
        "secret_hint": "Luma → Calendar settings → Options → API "
                       "(needs Luma Plus). The key covers that one calendar.",
    },
    # Like Luma, no second field: the key carries the box office with it.
    "tickettailor": {
        "secret_label": "API key",
        "secret_hint": "Ticket Tailor → Box office settings → API "
                       "(app.tickettailor.com/box-office/api). The key covers "
                       "that one box office.",
    },
    # Like Luma, no second field: an Action Network key is issued for one
    # group's list and carries the destination with it.
    "actionnetwork": {
        "secret_label": "API key",
        "secret_hint": "Action Network → Start Organizing → Details → "
                       "API & Sync (needs partner status)",
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
