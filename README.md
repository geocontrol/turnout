# Turnout

*One event, every platform. Publish once, gather the sign-ups back into one
list, and hold a capacity across all of them.*

Built for campaigning and community groups — tenants' unions, mutual aid
groups, residents' associations — who currently choose between free consumer
platforms that own the relationship and organising suites priced for national
NGOs.

This is **steps 1–3 of the build**: the event record and public link, the
Eventbrite adapter, and the consolidated sign-up list. It is a usable product
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
| `/` | your events |
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

**3 — The consolidated list.** Sign-ups come back from every platform that
will give them, and merge into one list of people. Capacity is held here, not
on any one platform. Waitlisted people can be given a place. Everything
exports to CSV, and everything can be deleted in one action.

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
    base.py       capability matrix + Result type
    manual.py     typed-in links, and assisted Facebook
    eventbrite.py the real one
  templates/      base, index, event, list
tests/            21 tests, no network
```

## Status

- Steps 1 and 3 are exercised end to end.
- The Eventbrite adapter is tested against a recorded transport but **has not
  been run against a live Eventbrite organisation**. Verify the attendee
  status strings and the ticket-class field names on first real use.
- Not yet built: Luma, Meetup, atproto publishing, RSVP ingest, evidence
  export, OAuth. Their capabilities are declared so the interface can be
  honest about what is coming; selecting one today gets you a typed-in link.

## Interop

Turnout shares a record shape with CultureBlocs and nothing else — no shared
library, no shared database, no dependency in either direction. When atproto
publishing lands (step 4) an event becomes a
`community.lexicon.calendar.event` in the group's own repository, which a
`com.cultureblocs.bead` can reference afterwards.

That interop stays **strictly optional**. A tenants' union must be able to
run Turnout end to end without knowing CultureBlocs exists.
