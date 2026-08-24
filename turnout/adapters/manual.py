"""Adapters for channels Turnout cannot drive.

Step 1 of the build is entirely these: an organiser puts the event up
wherever they already do, pastes the address in, and gets a working link
list on day one with no API access at all. That has to be a good
experience rather than a placeholder, because for a group with no budget
it may be the only one they ever use.
"""

from __future__ import annotations

import urllib.parse

from .base import Result, RemoteSignup, register


@register
class ManualAdapter:
    """A link somebody typed in. Stores an address; claims nothing else."""

    kind = "manual"

    def __init__(self, credential: dict | None = None) -> None:
        self.credential = credential

    def publish(self, event: dict, channel: dict, quantity: int | None) -> Result:
        if channel.get("url"):
            return Result.done("link recorded", url=channel["url"])
        return Result.needs_you("add the address once you've put the event up")

    def update(self, event: dict, channel: dict, quantity: int | None,
               changed: list[str]) -> Result:
        what = ", ".join(changed) if changed else "the event"
        return Result.needs_you(f"update {what} on the platform yourself")

    def set_open(self, event: dict, channel: dict, open_: bool, policy: str) -> Result:
        return Result.needs_you(
            "reopen sign-ups there yourself" if open_
            else "close sign-ups there yourself"
        )

    def fetch_signups(self, event: dict, channel: dict) -> list[RemoteSignup]:
        return []

    def delete_signups(self, event: dict, channel: dict) -> Result:
        return Result.needs_you(
            "Turnout holds no sign-ups for this channel — delete them on the "
            "platform itself"
        )


@register
class FacebookAdapter(ManualAdapter):
    """Assisted, not automated.

    Event creation was withdrawn from the Graph API and the remaining
    Official Events API is partner-gated. So Turnout prepares the text and
    opens the composer; the organiser pastes the resulting address back.
    """

    kind = "facebook"

    def compose_url(self, event: dict) -> str:
        text = f"{event['title']}\n\n{event.get('strap', '')}\n\n{event.get('body', '')}"
        return ("https://www.facebook.com/events/create/?"
                + urllib.parse.urlencode({"ref": "turnout", "text": text[:2000]}))

    def publish(self, event: dict, channel: dict, quantity: int | None) -> Result:
        if channel.get("url"):
            return Result.done("link recorded", url=channel["url"])
        return Result.needs_you(
            "Facebook can't be posted to automatically — copy is ready",
            data={"compose_url": self.compose_url(event),
                  "text": f"{event['title']}\n\n{event.get('body', '')}"},
        )
