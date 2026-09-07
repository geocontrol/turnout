"""What Turnout does on the organiser's behalf.

Every operation here returns a per-channel report rather than a single
success or failure, because a mixed result is the normal case: Eventbrite
closed, Facebook needs you, the typed-in link can only be shrugged at. The
interface shows the whole report. Nothing is quietly swallowed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import capacity as cap
from . import db
from .adapters import CODES, MANUAL, NAMES, Result, can, capability, get
from .merge import merge_people

# Fields whose change is worth pushing to a platform. Editing a note to self
# should not generate five API calls.
PUSHABLE = ["title", "strap", "body", "starts_at", "ends_at",
            "venue", "address", "accessibility", "capacity"]


@dataclass
class ChannelReport:
    channel_id: str
    kind: str
    label: str
    code: str
    result: Result


def slugify(title: str, suffix: str = "") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "event"
    return f"{s}-{suffix}" if suffix else s


# ------------------------------------------------------------------ reading

def get_event(conn, event_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM event WHERE id = ? OR slug = ?",
                       (event_id, event_id)).fetchone()
    return dict(row) if row else None


def get_channels(conn, event_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM channel WHERE event_id = ? ORDER BY created_at", (event_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def credential_for(conn, kind: str, credential_id: str | None = None) -> dict | None:
    """The saved access a channel should use.

    A channel can name one explicitly (a group with two Eventbrite
    organisations), otherwise we take the most recent for that platform.
    """
    if credential_id:
        row = conn.execute("SELECT * FROM credential WHERE id = ?",
                           (credential_id,)).fetchone()
        if row:
            return dict(row)
    # rowid breaks the tie: two credentials saved in the same second must
    # still resolve to the same one on every request, or which account an
    # event publishes to would quietly vary.
    row = conn.execute(
        "SELECT * FROM credential WHERE kind = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (kind,),
    ).fetchone()
    return dict(row) if row else None


def credentials(conn, kind: str | None = None) -> list[dict]:
    sql = "SELECT * FROM credential"
    args: tuple = ()
    if kind:
        sql += " WHERE kind = ?"
        args = (kind,)
    return [dict(r) for r in conn.execute(sql + " ORDER BY created_at, rowid", args)]


def needs_access(conn, channel: dict) -> bool:
    """True when this channel can't be driven yet because nothing is saved."""
    from .adapters import NEEDS_ACCESS
    if channel["kind"] not in NEEDS_ACCESS:
        return False
    return credential_for(conn, channel["kind"],
                          channel.get("credential_id")) is None


def adapter_for(conn, channel: dict):
    return get(channel["kind"],
               credential_for(conn, channel["kind"], channel.get("credential_id")))


def drift(event: dict) -> list[str]:
    """Fields edited since the last push to the platforms."""
    if not event.get("published"):
        return []
    was = json.loads(event["published"])
    return [f for f in PUSHABLE if str(event.get(f)) != str(was.get(f))]


def snapshot(event: dict) -> str:
    return json.dumps({f: event.get(f) for f in PUSHABLE})


# ------------------------------------------------------------------ writing

def _apply(conn, event: dict, channel: dict, res: Result, default_state: str) -> None:
    """Record a result against the channel and the log, uniformly."""
    fields, params = [], []
    if res.external_id:
        fields.append("external_id = ?"); params.append(res.external_id)
    if res.url:
        fields.append("url = ?"); params.append(res.url)
    if res.data:
        cfg = json.loads(channel.get("config") or "{}")
        cfg.update({k: v for k, v in res.data.items()
                    if k in ("ticket_class_id", "record_uri")})
        fields.append("config = ?"); params.append(json.dumps(cfg))

    state = "failed" if not res.ok else ("manual" if res.manual else default_state)
    fields += ["state = ?", "error = ?", "updated_at = ?"]
    params += [state, None if res.ok else res.message, db.now()]
    params.append(channel["id"])

    conn.execute(f"UPDATE channel SET {', '.join(fields)} WHERE id = ?", params)
    db.log(conn, event["id"], CODES.get(channel["kind"], "··"),
           res.message, channel["id"])


def _claim(conn, channel: dict) -> bool:
    """Take the channel before calling the platform, in one statement.

    Two presses arriving together both used to read `idle` and both create a
    listing — and two events for one meeting is the mistake with no undo,
    because the posters are already printed. Claiming marks it live *before*
    the call instead of after, so the second press loses. The worst case is a
    channel that says it is up when the call died halfway, which the next
    publish repairs; the alternative was two events nobody can merge.
    """
    cur = conn.execute(
        "UPDATE channel SET state = 'live', updated_at = ? "
        "WHERE id = ? AND state != 'live'", (db.now(), channel["id"]))
    conn.commit()
    return cur.rowcount == 1


def recheck_listings(conn, event_id: str) -> list[ChannelReport]:
    """Ask each platform whether the listing Turnout points at is still there.

    Turnout's own state records what happened when it last called. It cannot
    know that somebody deleted the event on the platform since — and the
    public page keeps sending people to it, which is the worst way for a
    group to find out. A listing that has gone has its address cleared, so
    the link list stops advertising it and Publish makes a fresh one.
    """
    event = get_event(conn, event_id)
    reports = []

    for ch in get_channels(conn, event["id"]):
        adapter = adapter_for(conn, ch)
        if not hasattr(adapter, "remote_state") or not ch["external_id"]:
            continue
        if needs_access(conn, ch):
            continue
        try:
            state = adapter.remote_state(ch)
        except Exception:
            # A platform being unreachable is not evidence that an event was
            # deleted. Leave everything exactly as it is.
            continue
        if state is None or state["state"] == "live":
            continue

        if state["state"] == "gone":
            conn.execute(
                "UPDATE channel SET url = '', external_id = NULL, config = '{}', "
                "state = 'idle', error = ?, updated_at = ? WHERE id = ?",
                (f"the listing was deleted on {NAMES.get(ch['kind'], ch['kind'])}",
                 db.now(), ch["id"]))
            res = Result.needs_you(
                f"that listing is gone from {NAMES.get(ch['kind'], ch['kind'])} "
                "— address cleared so the public page stops pointing at it. "
                "Publish again to make a new one.")
        else:
            conn.execute(
                "UPDATE channel SET url = '', state = 'failed', error = ?, "
                "updated_at = ? WHERE id = ?",
                ("still a draft on the platform", db.now(), ch["id"]))
            res = Result.failed(
                "still a draft there, so nobody can open it — address cleared. "
                "Publish again to finish it off.")
        db.log(conn, event["id"], CODES.get(ch["kind"], "··"), res.message, ch["id"])
        reports.append(ChannelReport(ch["id"], ch["kind"], ch["label"],
                                     CODES.get(ch["kind"], "··"), res))
    conn.commit()
    return reports


def publish(conn, event_id: str, channel_ids: list[str] | None = None
            ) -> list[ChannelReport]:
    event = get_event(conn, event_id)
    channels = [c for c in get_channels(conn, event["id"])
                if channel_ids is None or c["id"] in channel_ids]
    c = cap.read(conn, event["id"])
    reports = []
    already = []

    for ch in channels:
        if ch["state"] == "live" or not _claim(conn, ch):
            # Up already, or another press got here first. Publishing again
            # would make a second listing, which is the one mistake there is
            # no undo for — two pages, and the posters pointing at whichever.
            already.append(ch["label"])
            reports.append(ChannelReport(ch["id"], ch["kind"], ch["label"],
                                         CODES.get(ch["kind"], "··"),
                                         Result.done("already up, left alone")))
            continue
        if needs_access(conn, ch):
            res = Result.needs_you(
                f"no {ch['label']} access saved yet — add it under Access, "
                "or paste the address in by hand")
            _apply(conn, event, ch, res, ch["state"])
            reports.append(ChannelReport(ch["id"], ch["kind"], ch["label"],
                                         CODES.get(ch["kind"], "··"), res))
            continue
        adapter = adapter_for(conn, ch)
        qty = c.target_quantity_for(ch["id"])
        res = adapter.publish(event, ch, qty)
        _apply(conn, event, ch, res, "live")
        reports.append(ChannelReport(ch["id"], ch["kind"], ch["label"],
                                     CODES.get(ch["kind"], "··"), res))

    if already:
        db.log(conn, event["id"], "··",
               f"already up, left alone: {', '.join(already)} — "
               "edits go out with Push everywhere")
    conn.execute("UPDATE event SET published = ?, status = 'live', updated_at = ? "
                 "WHERE id = ?", (snapshot(event), db.now(), event["id"]))
    conn.commit()
    return reports


def push_update(conn, event_id: str) -> list[ChannelReport]:
    event = get_event(conn, event_id)
    changed = drift(event)
    if not changed:
        return []
    c = cap.read(conn, event["id"])
    reports = []

    for ch in get_channels(conn, event["id"]):
        if ch["state"] == "idle":
            continue
        if needs_access(conn, ch):
            res = Result.needs_you(
                f"no {ch['label']} access saved yet — add it under Access, "
                "or paste the address in by hand")
            _apply(conn, event, ch, res, ch["state"])
            reports.append(ChannelReport(ch["id"], ch["kind"], ch["label"],
                                         CODES.get(ch["kind"], "··"), res))
            continue
        adapter = adapter_for(conn, ch)
        res = adapter.update(event, ch, c.target_quantity_for(ch["id"]), changed)
        _apply(conn, event, ch, res,
               "closed" if ch["state"] == "closed" else "live")
        reports.append(ChannelReport(ch["id"], ch["kind"], ch["label"],
                                     CODES.get(ch["kind"], "··"), res))

    conn.execute("UPDATE event SET published = ?, updated_at = ? WHERE id = ?",
                 (snapshot(event), db.now(), event["id"]))
    conn.commit()
    return reports


def set_open(conn, event_id: str, open_: bool,
             channel_ids: list[str] | None = None) -> list[ChannelReport]:
    """Close (or reopen) sign-ups, doing whatever each platform actually allows."""
    event = get_event(conn, event_id)
    reports = []

    for ch in get_channels(conn, event["id"]):
        if channel_ids is not None and ch["id"] not in channel_ids:
            continue
        if ch["state"] == "idle":
            continue
        adapter = adapter_for(conn, ch)
        res = adapter.set_open(event, ch, open_, ch["policy"])
        _apply(conn, event, ch, res, "live" if open_ else "closed")
        reports.append(ChannelReport(ch["id"], ch["kind"], ch["label"],
                                     CODES.get(ch["kind"], "··"), res))

    conn.execute("UPDATE event SET status = ?, updated_at = ? WHERE id = ?",
                 ("live" if open_ else "closed", db.now(), event["id"]))
    conn.commit()
    return reports


def sync_signups(conn, event_id: str) -> list[ChannelReport]:
    """Pull sign-ups back from every channel that will give them to us."""
    event = get_event(conn, event_id)
    reports = []

    for ch in get_channels(conn, event["id"]):
        if not can(ch["kind"], "read_signups") or ch["state"] == "idle":
            continue
        if needs_access(conn, ch):
            continue
        adapter = adapter_for(conn, ch)
        try:
            remote = adapter.fetch_signups(event, ch)
        except Exception as exc:
            res = Result.failed(f"could not read sign-ups: {exc}")
            _apply(conn, event, ch, res, ch["state"])
            reports.append(ChannelReport(ch["id"], ch["kind"], ch["label"],
                                         CODES.get(ch["kind"], "··"), res))
            continue

        added = updated = 0
        for r in remote:
            existing = conn.execute(
                "SELECT id, status FROM signup WHERE channel_id = ? AND external_id = ?",
                (ch["id"], r.external_id),
            ).fetchone()
            if existing:
                if existing["status"] != r.status:
                    conn.execute("UPDATE signup SET status = ?, seen_at = ? WHERE id = ?",
                                 (r.status, db.now(), existing["id"]))
                    updated += 1
            else:
                conn.execute(
                    """INSERT INTO signup (id, event_id, channel_id, external_id,
                       name, email, handle, status, created_at, seen_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (db.new_id(), event["id"], ch["id"], r.external_id, r.name,
                     r.email, r.handle, r.status, r.created_at or db.now(), db.now()),
                )
                added += 1

        res = Result.done(f"{added} new, {updated} changed"
                          if added or updated else "no change")
        db.log(conn, event["id"], CODES.get(ch["kind"], "··"),
               res.message, ch["id"])
        reports.append(ChannelReport(ch["id"], ch["kind"], ch["label"],
                                     CODES.get(ch["kind"], "··"), res))

    conn.commit()
    return reports


def promote_from_waitlist(conn, event_id: str, signup_ids: list[str]) -> int:
    n = 0
    for sid in signup_ids:
        cur = conn.execute(
            "UPDATE signup SET status = 'going', seen_at = ? "
            "WHERE id = ? AND event_id = ? AND status = 'waitlist'",
            (db.now(), sid, event_id))
        n += cur.rowcount
    if n:
        db.log(conn, event_id, "··", f"{n} moved from the waiting list to going")
    conn.commit()
    return n


def auto_close_if_full(conn, event_id: str) -> list[ChannelReport]:
    """Apply each channel's own policy once the room is full."""
    c = cap.read(conn, event_id)
    if not c.full:
        return []
    event = get_event(conn, event_id)
    reports = []

    for ch in get_channels(conn, event["id"]):
        if ch["state"] not in ("live", "drift") or ch["policy"] == "open":
            continue
        adapter = adapter_for(conn, ch)
        res = adapter.set_open(event, ch, False, ch["policy"])
        _apply(conn, event, ch, res, "closed")
        reports.append(ChannelReport(ch["id"], ch["kind"], ch["label"],
                                     CODES.get(ch["kind"], "··"), res))
    conn.commit()
    return reports


def purge_signups(conn, event_id: str) -> tuple[int, list[ChannelReport]]:
    """Delete every sign-up we hold, and say honestly what we could not reach.

    This is the control the audience for this app will judge it on. It runs
    the local delete first, unconditionally, so a failing platform call can
    never leave the data sitting here.
    """
    event = get_event(conn, event_id)
    n = conn.execute("SELECT COUNT(*) AS n FROM signup WHERE event_id = ?",
                     (event["id"],)).fetchone()["n"]
    conn.execute("DELETE FROM signup WHERE event_id = ?", (event["id"],))
    conn.commit()
    db.log(conn, event["id"], "··", f"purged {n} sign-ups from Turnout")

    reports = []
    for ch in get_channels(conn, event["id"]):
        if ch["state"] == "idle":
            continue
        adapter = adapter_for(conn, ch)
        try:
            res = adapter.delete_signups(event, ch)
        except Exception as exc:
            res = Result.failed(str(exc))
        db.log(conn, event["id"], CODES.get(ch["kind"], "··"), res.message, ch["id"])
        reports.append(ChannelReport(ch["id"], ch["kind"], ch["label"],
                                     CODES.get(ch["kind"], "··"), res))
    conn.commit()
    return n, reports


def people(conn, event_id: str):
    return merge_people(conn, event_id)


def capability_rows(kind: str) -> list[tuple[str, str, str]]:
    labels = [("create", "Put the event up"), ("update", "Push edits"),
              ("capacity", "Hold a limit"), ("waitlist", "Waiting list"),
              ("close", "Close sign-ups"), ("read_signups", "Read sign-ups back"),
              ("emails", "Give email addresses")]
    words = {"yes": "yes", "partial": "partly", "no": "no", "manual": "by hand"}
    return [(lbl, capability(kind, k), words[capability(kind, k)]) for k, lbl in labels]


def add_channel(conn, event_id: str, kind: str, label: str = "",
                url: str = "", allocation: int | None = None,
                policy: str = "waitlist") -> str:
    """Put a channel on an event, from wherever it was asked for.

    Shared by the create form on the home page and the add-a-platform form on
    the event page, so the one rule that matters — a typed-in address is
    already live, because there is nothing left to do to it — can't drift
    between them.
    """
    from .adapters import NAMES

    state = "live" if (url.strip() and kind in ("manual", "facebook")) else "idle"
    cid = db.new_id()
    conn.execute(
        """INSERT INTO channel (id, event_id, kind, label, url, allocation,
           policy, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (cid, event_id, kind, label or NAMES.get(kind, kind), url.strip(),
         allocation, policy, state, db.now(), db.now()))
    db.log(conn, event_id, CODES.get(kind, "··"),
           f"channel added ({NAMES.get(kind, kind)})")
    return cid


def publishing_options(conn) -> list[dict]:
    """What each platform can do for this installation, right now.

    The capability matrix says what a platform is capable of; this says what
    is actually available today given what has been built and what access has
    been saved. The home screen shows it so nobody discovers the limit
    halfway through composing an event.
    """
    from .adapters import IMPLEMENTED, KINDS, NAMES, NEEDS_ACCESS, NOTES

    saved = {c["kind"] for c in credentials(conn)}
    in_use = {r["kind"]: r["n"] for r in conn.execute(
        "SELECT kind, COUNT(*) AS n FROM channel GROUP BY kind")}

    out = []
    for kind in KINDS:
        caps_are_promises = False
        if kind not in IMPLEMENTED:
            rank, state, tag = 3, "idle", "not built yet"
            summary = ("Declared so the interface stays honest about what is "
                       "coming. Adding it today gets you a link you manage "
                       "yourself.")
            caps_are_promises = True
        elif kind not in NEEDS_ACCESS:
            rank, state = 0, "manual"
            tag = "assisted" if capability(kind, "create") == MANUAL and kind != "manual" \
                else "always available"
            summary = ("No account and no token. You put the event up, paste "
                       "the address in, and it goes on the public page.")
        elif capability(kind, "create") == MANUAL:
            # Access buys the sign-ups back, not the publishing: Action
            # Network's API cannot put up an event anybody can sign, so
            # promising a listing here would be the one lie this screen exists
            # to prevent.
            if kind in saved:
                rank, state, tag = 0, "live", "access saved"
                summary = ("You make the event there and paste the address in. "
                           "Turnout reads the sign-ups back, with email "
                           "addresses, and pushes edits.")
            else:
                rank, state, tag = 1, "drift", "needs access"
                summary = ("You make the event there and paste the address in. "
                           "Add a key and Turnout also reads the sign-ups back.")
        elif kind in saved:
            rank, state, tag = 0, "live", "access saved"
            summary = ("Turnout creates the listing itself, pushes edits "
                       "through, and reads sign-ups back.")
        else:
            rank, state, tag = 1, "drift", "needs access"
            summary = ("Add access and Turnout takes it over. Until then it "
                       "behaves like a link you manage yourself.")
        out.append({
            "kind": kind, "name": NAMES[kind], "code": CODES.get(kind, "··"),
            "rank": rank, "state": state, "tag": tag, "summary": summary,
            "note": NOTES.get(kind, ""),
            "can_add_access": (kind in IMPLEMENTED and kind in NEEDS_ACCESS
                               and kind not in saved),
            "channels": in_use.get(kind, 0),
            "caps_are_promises": caps_are_promises,
            "rows": capability_rows(kind),
        })
    out.sort(key=lambda o: (o["rank"], KINDS.index(o["kind"])))
    return out
