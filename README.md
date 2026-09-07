# Turnout

*One event, every platform. Publish once, gather the sign-ups back into one
list, and hold a capacity across all of them.*

Built for campaigning and community groups — tenants' unions, mutual aid
groups, residents' associations — who currently choose between free consumer
platforms that own the relationship and organising suites priced for national
NGOs.

This is **steps 1–3 of the build**: the event record and public link, the
Eventbrite, Luma and Action Network adapters, and the consolidated sign-up
list. It is a usable product
for one real group. Everything after this is widening.

## Quick start

```bash
pip install -r requirements.txt
uvicorn turnout.main:app --reload --port 8100
```

Open <http://localhost:8100>. There is nothing else to configure — no
accounts, no API tokens, no atproto identity.

| Path | What it is |
| --- | --- |
| `/` | start an event — the whole form, and every platform, on one page |
| `/events/{id}` | compose, channels, sign-ups, the link, the log |
| `/l/{slug}` | **the public page** — no auth, no cookies, no JavaScript |
| `/l/{slug}/qr.svg` | QR for the poster |
| `/l/{slug}.ics` | add to calendar |

Set `TURNOUT_TOKEN` to require a token on the organiser routes. The public
page never checks it.

## The three steps, and what each gets you

**1 — Event record and the link.** Compose the event once. Add a channel for
every place it should appear and paste in addresses you made yourself. You
get a public page listing all of them, a QR code, and a calendar file.

This works with **no API access to anything**. For a group with no budget it
may be all they ever use, so it is built as the main path rather than a
fallback. The printed URL never changes, which means a poster stays correct
when you add a platform later.

**2 — Eventbrite.** Add a private token and Turnout creates the listing
itself: draft, free ticket class, publish. Edits push through. Closing
sign-ups moves ticket sales to end now, because Eventbrite refuses to
unpublish once anyone has registered — which is exactly when you want to
close.

**2b — Luma.** Paste in an API key and Turnout creates the event in one
call — Luma has no draft step. Capacity maps onto Luma's own settings, so a
channel set to *waiting list* becomes `max_capacity` plus a Luma waitlist,
and *close it* becomes the same cap with the waitlist off. Closing sign-ups
closes registration and leaves the page readable. The v1 API has no cancel
and no guest delete, and the capability matrix says so rather than pretending.

A Luma key belongs to one calendar, so the key is the whole destination —
there is no calendar id to type in, and *Check it works* names the calendar
the key opened.

**2c — Action Network.** The odd one, and deliberately so. Action Network's
own documentation says events posted through its API *"will not be given a
URL on actionnetwork.org where people can sign"* — so Turnout does not post
them. You make the event there, where it gets a real RSVP page, reminders and
autoresponses, and paste the address in. Turnout finds the matching event
through the API and reads the RSVPs back with names and email addresses,
into the same consolidated list as everything else, and pushes edits to it.

It will not offer what that API cannot do: `capacity` and `visibility` are
system-generated, a status change by the group that owns the event is
*silently ignored*, and neither events nor attendances can be deleted. All
four are declared as `manual` or `no`, so the controls never appear.

Not built yet, and the obvious next step: the Record Attendance Helper, to
push the consolidated list *into* the group's Action Network list — the
direction that matters most for a union that organises out of it.

**3 — The consolidated list.** Sign-ups come back from every platform that
will give them, and merge into one list of people. Capacity is held here, not
on any one platform. Waitlisted people can be given a place. Everything
exports to CSV, and everything can be deleted in one action.

## Publishing twice

Every action reports back per platform — what went out, what wants doing by
hand, what failed — in a banner on the event page, because a mixed result is
the normal case and a silent one hides the failure underneath.

**Publish is safe to press again.** A channel already up is left alone, and
the channel is claimed in one statement before the platform is called, so two
presses arriving together can't both create a listing. A publish that failed
partway carries back the id of whatever it did create, so the next press
finishes that event rather than making a second one. Two listings for one
meeting is the mistake with no undo: the posters are already printed, and
they point at whichever.

**And the platform is asked, not assumed.** Turnout's own state records what
happened when it last called; it cannot know somebody deleted the event on
the platform since. *Check listings and fetch sign-ups* asks. A listing that
has gone has its address cleared — the public page stops pointing at it that
moment — and Publish makes a fresh one. A listing still sitting in draft is
said to be a draft, rather than counted as published: a 200 answer saying
`published: false` is not a published event.

## Two rules the code enforces

**Never offer a control a platform cannot honour.** Every adapter declares
what it can do (`turnout/adapters/base.py`) and the interface reads that
matrix. atmo.rsvp cannot enforce a capacity — an RSVP there is somebody
writing a record to their own account — so when the room fills, its entry on
the public page says *still open*, not *waiting list*. There is a test for
that, because it is the kind of thing that quietly rots.

**Email is the only key we trust.** Two people with the same name and no
email address stay two rows, flagged, with a suggestion for a human to check.
The cost of a wrong guess lands on a real person at the door.

## Capacity

One room, several platforms each thinking they control it — the same problem
a hotel solves with channel allotments.

- **Shared** — every platform is told the full remainder; all are closed when
  the room fills. Nothing goes unused; two people signing up in the same
  second can both get in.
- **Fixed share** — each platform gets its own number. Nothing oversells;
  places can strand.

`oversell_pct` is the deliberate no-show margin, applied to the places
offered and never to the number the room actually holds. A free public
meeting routinely runs 20–30%.

## Deleting sign-ups

`POST /events/{id}/purge` deletes every sign-up Turnout holds, then reports
which platforms keep their own copy. The local delete runs first and
unconditionally, so a failing platform call can never leave the data sitting
here. Groups under pressure need this to work the first time.

## Layout

```
turnout/
  db.py           schema; the only table with personal data is `signup`
  capacity.py     pool vs allocated arithmetic
  merge.py        identity merging and merge suggestions
  service.py      publish, push, close, sync, promote, purge
  main.py         HTTP: organiser app + public page
  adapters/
    base.py       capability matrix, Result type, shared UTC conversion
    manual.py     typed-in links, and assisted Facebook
    eventbrite.py the first real one
    luma.py       the second: one call to publish, guest list with emails
    actionnetwork.py  reads RSVPs back out of the group's own list
  templates/      base, index, event, list; _fields.html is the event's own
                  fields, rendered by both the start form and the event page
tests/            57 tests, no network
```

## Status

- Steps 1 and 3 are exercised end to end.
- The Eventbrite adapter is tested against a recorded transport but **has not
  been run against a live Eventbrite organisation**. Verify the attendee
  status strings and the ticket-class field names on first real use.
- The Luma adapter is written against the published v1 description
  (`https://public-api.luma.com/openapi.json`) and tested the same way, but
  **has not been run against a live Luma Plus calendar**. Verify the guest
  fields on first real use. Note that API access needs Luma Plus, so for
  most groups this remains the paid option and step 1 remains the main path.
- The Action Network adapter is written against the v2 documentation and
  tested the same way, but **has not been run against a live API key**.
  Verify the attendance statuses on first real use. API access needs partner
  status.
- Not yet built: Meetup, atproto publishing, pushing sign-ups back into
  Action Network, evidence export, OAuth. Their capabilities are declared so
  the interface can be honest about what is coming; selecting one today gets
  you a typed-in link.

## Interop

Turnout shares a record shape with CultureBlocs and nothing else — no shared
library, no shared database, no dependency in either direction. When atproto
publishing lands (step 4) an event becomes a
`community.lexicon.calendar.event` in the group's own repository, which a
`com.cultureblocs.bead` can reference afterwards.

That interop stays **strictly optional**. A tenants' union must be able to
run Turnout end to end without knowing CultureBlocs exists.
