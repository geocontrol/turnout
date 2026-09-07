"""Ticket Tailor adapter.

The most capable platform Turnout talks to, and the least awkward. Everything
the capability matrix asks about is a real endpoint: a cap, a waiting list
that opens when the cap is reached, a close that leaves the page readable,
and an attendee list with names and email addresses on it.

Publishing is four calls, because Ticket Tailor separates what an event *is*
from when it happens:

    1. POST /v1/event_series               -> the event, as a draft
    2. POST /v1/event_series/{id}/events   -> the date it happens on
    3. POST .../ticket_types               -> a free ticket, or nothing
                                              can be booked
    4. POST .../status  status=PUBLISHED   -> live

Each step records what it made, so a failure halfway is picked up rather than
started again — four calls is four chances to end up with two events.

Two things are worth knowing before trusting it with a real event.

  * **A key belongs to one box office**, so the key is the destination and
    there is nothing else to type in. `check()` names the box office it
    opened.
  * **Times are sent exactly as typed.** Ticket Tailor takes a date and a
    clock time and reads them in the box office's own timezone — there is no
    per-event timezone to send. So the box office timezone must be the
    event's. Converting to UTC here would land it hours out.

NOTE: written against the v1 documentation at developers.tickettailor.com and
exercised in tests against a recorded transport. It has NOT been run against
a live box office. Verify the ticket-type fields on first real use.
"""

from __future__ import annotations

import json

import httpx

from .base import RemoteSignup, Result, register

BASE = "https://api.tickettailor.com"

# Issued tickets are either valid or voided; a refund or a cancellation voids
# them. Anything unrecognised counts as going — a place quietly not counted
# fills the room twice over, which is worse than one counted early.
STATUS = {"valid": "going", "voided": "cancelled"}

PAGES = 20                      # 100 per page: 2000 tickets, then it stops


@register
class TicketTailorAdapter:
    kind = "tickettailor"

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
            # The key goes in as the username with no password, which is the
            # form their own curl examples use.
            self._client = httpx.Client(
                base_url=BASE, timeout=20.0,
                auth=httpx.BasicAuth(self.key, ""),
                headers={"Accept": "application/json"},
            )
        return self._client

    def _call(self, method: str, path: str, **kw) -> dict:
        r = self.client.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("message") or body.get("error_code") or r.text
            except Exception:
                msg = r.text[:300]
            raise RuntimeError(f"{r.status_code}: {msg}")
        body = r.json() if r.content else {}
        return body if isinstance(body, dict) else {"data": body}

    def check(self) -> str:
        """Which box office this key opened.

        A key issued by the wrong box office is otherwise silent until an
        event turns up somewhere nobody is looking.
        """
        data = self._call("GET", "/v1/overview")
        over = data.get("data") or data
        return f"box office {over.get('box_office_name') or 'reachable'}"

    def remote_state(self, channel: dict) -> dict | None:
        """What Ticket Tailor says about the series Turnout recorded.

        `sales_closed` counts as live: the page is still there and still
        readable, which is the whole point of closing rather than deleting.
        """
        sid = channel.get("external_id")
        if not sid:
            return None
        try:
            body = self._call("GET", f"/v1/event_series/{sid}")
        except Exception as exc:
            if "404" in str(exc):
                return {"state": "gone", "status": "not found", "url": ""}
            raise
        series = body.get("data") or body
        status = (series.get("status") or "").lower()
        if status == "draft":
            return {"state": "draft", "status": status, "url": series.get("url", "")}
        return {"state": "live", "status": status, "url": series.get("url", "")}

    # ------------------------------------------------------------- payload

    def _series_payload(self, event: dict, channel: dict,
                        quantity: int | None) -> dict:
        body = {"name": event["title"]}
        description = "\n\n".join(
            x for x in (event.get("strap"), event.get("body"),
                        event.get("accessibility")) if x)
        if description:
            body["description"] = description
        if event.get("venue"):
            body["venue"] = event["venue"]
        total = quantity if quantity is not None else event.get("capacity")
        if total:
            body["max_tickets_sold_per_occurrence"] = int(total)
        body["waitlist_active"] = _waitlist(channel)
        return body

    def _occurrence_payload(self, event: dict) -> dict:
        """A date and a clock time, in the box office's timezone.

        Sent exactly as the organiser typed it. Ticket Tailor has no per-event
        timezone to send it with, so converting would only move the event.
        """
        start_date, start_time = _split(event["starts_at"])
        end_date, end_time = _split(event.get("ends_at") or "")
        body = {"start_date": start_date,
                # end_date is required, and most groups don't set an end. The
                # same day is the honest answer, rather than inventing a
                # finishing time nobody agreed to.
                "end_date": end_date or start_date}
        if start_time:
            body["start_time"] = start_time
        if end_time:
            body["end_time"] = end_time
        return body

    # ------------------------------------------------------------- actions

    def publish(self, event: dict, channel: dict, quantity: int | None) -> Result:
        cfg = _cfg(channel)
        sid = channel.get("external_id")
        occ, ticket = cfg.get("occurrence_id"), cfg.get("ticket_type_id")
        url = channel.get("url") or ""
        made_it_now = False

        def carry(exc: Exception) -> Result:
            """Hand back everything already made, so a retry finishes it."""
            data = {k: v for k, v in (("occurrence_id", occ),
                                      ("ticket_type_id", ticket)) if v}
            return Result.failed(str(exc), external_id=sid, url=url, data=data)

        try:
            if sid and (self.remote_state(channel) or {}).get("state") == "gone":
                # Deleted in Ticket Tailor since. A fresh one is what the
                # organiser is asking for; resuming would fail forever.
                sid, occ, ticket, url = None, None, None, ""
            if not sid:
                series = self._one(self._call(
                    "POST", "/v1/event_series",
                    data=self._series_payload(event, channel, quantity)))
                sid, made_it_now = series["id"], True
                url = series.get("url") or url
        except Exception as exc:
            return carry(exc)

        try:
            if not occ:
                occurrence = self._one(self._call(
                    "POST", f"/v1/event_series/{sid}/events",
                    data=self._occurrence_payload(event)))
                occ = occurrence["id"]
                # The occurrence page is the one to print: it is the date
                # people are booking, not the series it belongs to.
                url = occurrence.get("url") or url
            if not ticket:
                created = self._one(self._call(
                    "POST", f"/v1/event_series/{sid}/ticket_types",
                    data={"name": "Free entry", "price": 0,
                          "quantity": quantity or event.get("capacity") or 1000}))
                ticket = created["id"]
            self._call("POST", f"/v1/event_series/{sid}/status",
                       data={"status": "PUBLISHED"})
        except Exception as exc:
            return carry(exc)

        return Result.done(
            "created, dated, ticketed and published" if made_it_now
            else "picked up the event already created here and published it",
            external_id=sid, url=url,
            data={"occurrence_id": occ, "ticket_type_id": ticket})

    def update(self, event: dict, channel: dict, quantity: int | None,
               changed: list[str]) -> Result:
        sid = channel.get("external_id")
        if not sid:
            return Result.failed("not published here yet")
        occ = _cfg(channel).get("occurrence_id")
        try:
            self._call("POST", f"/v1/event_series/{sid}",
                       data=self._series_payload(event, channel, quantity))
            if occ:
                self._call("POST", f"/v1/event_series/{sid}/events/{occ}",
                           data=self._occurrence_payload(event))
            return Result.done("updated " + (", ".join(changed) or "event"))
        except Exception as exc:
            return Result.failed(str(exc))

    def set_open(self, event: dict, channel: dict, open_: bool,
                 policy: str) -> Result:
        """Close sales, which is a real close and leaves the page up.

        People still need the address after sign-ups shut, and anyone already
        holding a ticket still needs to find it.
        """
        sid = channel.get("external_id")
        if not sid:
            return Result.failed("not published here yet")
        try:
            self._call("POST", f"/v1/event_series/{sid}/status",
                       data={"status": "PUBLISHED" if open_ else "CLOSE_SALES"})
            return Result.done(
                "sales reopened" if open_ else
                "sales closed — page stays up, no new tickets")
        except Exception as exc:
            return Result.failed(str(exc))

    def fetch_signups(self, event: dict, channel: dict) -> list[RemoteSignup]:
        sid = channel.get("external_id")
        if not sid:
            return []
        out: list[RemoteSignup] = []
        after, page = None, 0
        while page < PAGES:
            params = {"event_series_id": sid, "limit": 100}
            if after:
                params["starting_after"] = after
            data = self._call("GET", "/v1/issued_tickets", params=params)
            tickets = data.get("data") or []
            for t in tickets:
                out.append(RemoteSignup(
                    external_id=str(t.get("id") or ""),
                    name=_name(t),
                    email=t.get("email"),
                    status=STATUS.get((t.get("status") or "").lower(), "going"),
                    created_at=_stamp(t.get("created_at")),
                ))
            if len(tickets) < 100:
                break
            after = tickets[-1].get("id")
            if not after:
                break
            page += 1
        return out

    def delete_signups(self, event: dict, channel: dict) -> Result:
        """Ticket Tailor keeps the tickets it issued.

        Voiding a ticket is not deleting the person, and the API has no way to
        remove the record. Saying where the data still is beats implying it
        has gone.
        """
        return Result.needs_you(
            "removed from Turnout. Ticket Tailor keeps its own issued tickets "
            "and the buyers behind them — delete them in your box office if "
            "they need to be gone.")

    # ----------------------------------------------------------- internals

    @staticmethod
    def _one(body: dict) -> dict:
        """A single object, which comes back bare or under `data`."""
        data = body.get("data")
        return data if isinstance(data, dict) else body


def _cfg(channel: dict) -> dict:
    raw = channel.get("config") or "{}"
    return json.loads(raw) if isinstance(raw, str) else raw


def _split(local: str) -> tuple[str, str]:
    """'2026-10-01T19:00' -> ('2026-10-01', '19:00:00')."""
    date, _, time = (local or "").partition("T")
    return date, (f"{time[:5]}:00" if time else "")


def _waitlist(channel: dict) -> str:
    """The channel's policy, in Ticket Tailor's own words.

    `no_tickets_available` is the one that matches "waiting list when it
    fills" — plain `true` would show a waiting list while there are still
    tickets, which is not what anybody asked for.
    """
    return "no_tickets_available" if (channel.get("policy") or "waitlist") == "waitlist" \
        else "false"


def _name(ticket: dict) -> str:
    if ticket.get("full_name"):
        return ticket["full_name"]
    return " ".join(x for x in (ticket.get("first_name"),
                                ticket.get("last_name")) if x)


def _stamp(created_at) -> str | None:
    """Their timestamps are unix seconds; ours are ISO strings."""
    if not created_at:
        return None
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(int(created_at), timezone.utc).isoformat(
            timespec="seconds")
    except (TypeError, ValueError):
        return None
