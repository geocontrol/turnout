"""Turnout — HTTP surface.

Two audiences on one server:

  /                 the organiser app (needs the token, if one is set)
  /l/{slug}         the public link list — no auth, no cookies, no scripts

The public page is deliberately the plainest thing here. It is what gets
printed on a leaflet and opened on a decade-old Android phone in a hall with
one bar of signal.
"""

from __future__ import annotations

import csv
import io
import json
import os
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import capacity as cap
from . import db, service
from .adapters import (ACCESS_FIELDS, CODES, IMPLEMENTED, KINDS, NAMES,
                       NEEDS_ACCESS, NOTES, can, capability, get)
from .merge import suggest_merges, to_csv_rows

HERE = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
templates.env.filters["from_json"] = lambda v: json.loads(v or "{}")
templates.env.globals.update(
    NAMES=NAMES, NOTES=NOTES, CODES=CODES, KINDS=KINDS,
    IMPLEMENTED=IMPLEMENTED, can=can, capability=capability,
    capability_rows=service.capability_rows,
    NEEDS_ACCESS=NEEDS_ACCESS, ACCESS_FIELDS=ACCESS_FIELDS,
)

app = FastAPI(title="Turnout", docs_url=None, redoc_url=None)
_conn = None
TOKEN = os.environ.get("TURNOUT_TOKEN")


def get_conn():
    global _conn
    if _conn is None:
        _conn = db.connect()
    return _conn


def require_organiser(request: Request):
    """Organiser routes only. The public link list never calls this."""
    if not TOKEN:
        return True
    supplied = (request.headers.get("authorization", "").removeprefix("Bearer ").strip()
                or request.cookies.get("turnout_token", ""))
    if supplied != TOKEN:
        raise HTTPException(401, "not signed in")
    return True


def _event_or_404(conn, key: str) -> dict:
    ev = service.get_event(conn, key)
    if ev is None:
        raise HTTPException(404, "no such event")
    return ev


# ---------------------------------------------------------------- organiser

@app.get("/", response_class=HTMLResponse)
def index(request: Request, _=Depends(require_organiser)):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM event ORDER BY starts_at DESC").fetchall()
    events = []
    for r in rows:
        c = cap.read(conn, r["id"])
        events.append({"e": dict(r), "cap": c})
    return templates.TemplateResponse(request, "index.html", {"events": events})


@app.post("/events")
def create_event(title: str = Form(...), starts_at: str = Form(...),
                 _=Depends(require_organiser)):
    conn = get_conn()
    eid = db.new_id()
    slug = service.slugify(title, eid[:4])
    conn.execute(
        """INSERT INTO event (id, slug, title, starts_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?)""",
        (eid, slug, title.strip(), starts_at, db.now(), db.now()))
    conn.commit()
    db.log(conn, eid, "··", "event created in Turnout")
    return RedirectResponse(f"/events/{eid}", status_code=303)


@app.get("/events/{eid}", response_class=HTMLResponse)
def event_page(eid: str, request: Request, tab: str = "event",
               _=Depends(require_organiser)):
    conn = get_conn()
    ev = _event_or_404(conn, eid)
    people = service.people(conn, ev["id"])
    return templates.TemplateResponse(request, "event.html", {
        "e": ev,
        "tab": tab,
        "channels": service.get_channels(conn, ev["id"]),
        "cap": cap.read(conn, ev["id"]),
        "drift": service.drift(ev),
        "people": people,
        "suggestions": suggest_merges(people),
        "log": conn.execute(
            "SELECT * FROM log WHERE event_id = ? ORDER BY id DESC LIMIT 80",
            (ev["id"],)).fetchall(),
        "credentials": service.credentials(conn),
        "needs_access": {c["id"]: service.needs_access(conn, c)
                         for c in service.get_channels(conn, ev["id"])},
    })


@app.post("/events/{eid}")
def edit_event(eid: str, request: Request, _=Depends(require_organiser),
               title: str = Form(...), strap: str = Form(""), body: str = Form(""),
               starts_at: str = Form(...), ends_at: str = Form(""),
               venue: str = Form(""), address: str = Form(""),
               accessibility: str = Form(""), contact: str = Form(""),
               capacity: str = Form(""), oversell_pct: int = Form(0),
               capacity_mode: str = Form("pool")):
    conn = get_conn()
    ev = _event_or_404(conn, eid)
    conn.execute(
        """UPDATE event SET title=?, strap=?, body=?, starts_at=?, ends_at=?,
           venue=?, address=?, accessibility=?, contact=?, capacity=?,
           oversell_pct=?, capacity_mode=?, updated_at=? WHERE id=?""",
        (title.strip(), strap, body, starts_at, ends_at or None, venue, address,
         accessibility, contact, int(capacity) if capacity.strip() else None,
         oversell_pct, capacity_mode, db.now(), ev["id"]))
    conn.commit()
    return RedirectResponse(f"/events/{ev['id']}", status_code=303)


@app.post("/events/{eid}/channels")
def add_channel(eid: str, kind: str = Form(...), label: str = Form(""),
                url: str = Form(""), allocation: str = Form(""),
                policy: str = Form("waitlist"), _=Depends(require_organiser)):
    conn = get_conn()
    ev = _event_or_404(conn, eid)
    state = "live" if (url.strip() and kind in ("manual", "facebook")) else "idle"
    conn.execute(
        """INSERT INTO channel (id, event_id, kind, label, url, allocation,
           policy, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (db.new_id(), ev["id"], kind, label or NAMES.get(kind, kind), url.strip(),
         int(allocation) if allocation.strip() else None, policy, state,
         db.now(), db.now()))
    conn.commit()
    db.log(conn, ev["id"], CODES.get(kind, "··"), f"channel added ({NAMES.get(kind, kind)})")
    return RedirectResponse(f"/events/{ev['id']}?tab=channels", status_code=303)


@app.post("/channels/{cid}")
def edit_channel(cid: str, url: str = Form(""), allocation: str = Form(""),
                 policy: str = Form("waitlist"), show_on_list: str = Form(""),
                 credential_id: str = Form(""), _=Depends(require_organiser)):
    conn = get_conn()
    row = conn.execute("SELECT * FROM channel WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such channel")
    state = row["state"]
    if url.strip() and state == "idle" and row["kind"] in ("manual", "facebook"):
        state = "live"
    conn.execute(
        """UPDATE channel SET url=?, allocation=?, policy=?, show_on_list=?,
           credential_id=?, state=?, updated_at=? WHERE id=?""",
        (url.strip(), int(allocation) if allocation.strip() else None, policy,
         1 if show_on_list else 0, credential_id or None, state, db.now(), cid))
    conn.commit()
    return RedirectResponse(f"/events/{row['event_id']}?tab=channels", status_code=303)


@app.post("/channels/{cid}/delete")
def delete_channel(cid: str, _=Depends(require_organiser)):
    conn = get_conn()
    row = conn.execute("SELECT * FROM channel WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such channel")
    conn.execute("DELETE FROM channel WHERE id = ?", (cid,))
    conn.commit()
    db.log(conn, row["event_id"], CODES.get(row["kind"], "··"), "channel removed")
    return RedirectResponse(f"/events/{row['event_id']}?tab=channels", status_code=303)


# ------------------------------------------------------------------ actions

@app.post("/events/{eid}/publish")
def do_publish(eid: str, _=Depends(require_organiser)):
    conn = get_conn()
    ev = _event_or_404(conn, eid)
    service.publish(conn, ev["id"])
    return RedirectResponse(f"/events/{ev['id']}?tab=channels", status_code=303)


@app.post("/events/{eid}/push")
def do_push(eid: str, _=Depends(require_organiser)):
    conn = get_conn()
    ev = _event_or_404(conn, eid)
    service.push_update(conn, ev["id"])
    return RedirectResponse(f"/events/{ev['id']}?tab=channels", status_code=303)


@app.post("/events/{eid}/close")
def do_close(eid: str, open_: str = Form(""), _=Depends(require_organiser)):
    conn = get_conn()
    ev = _event_or_404(conn, eid)
    service.set_open(conn, ev["id"], bool(open_))
    return RedirectResponse(f"/events/{ev['id']}?tab=channels", status_code=303)


@app.post("/events/{eid}/sync")
def do_sync(eid: str, _=Depends(require_organiser)):
    conn = get_conn()
    ev = _event_or_404(conn, eid)
    service.sync_signups(conn, ev["id"])
    service.auto_close_if_full(conn, ev["id"])
    return RedirectResponse(f"/events/{ev['id']}?tab=signups", status_code=303)


@app.post("/events/{eid}/promote")
async def do_promote(eid: str, request: Request, _=Depends(require_organiser)):
    conn = get_conn()
    ev = _event_or_404(conn, eid)
    form = await request.form()
    service.promote_from_waitlist(conn, ev["id"], form.getlist("signup_id"))
    return RedirectResponse(f"/events/{ev['id']}?tab=signups", status_code=303)


@app.post("/events/{eid}/purge")
def do_purge(eid: str, confirm: str = Form(""), _=Depends(require_organiser)):
    """Delete every sign-up Turnout holds for this event."""
    conn = get_conn()
    ev = _event_or_404(conn, eid)
    if confirm.strip().lower() != "delete":
        raise HTTPException(400, "type DELETE to confirm")
    service.purge_signups(conn, ev["id"])
    return RedirectResponse(f"/events/{ev['id']}?tab=signups", status_code=303)


@app.get("/events/{eid}/signups.csv")
def export_csv(eid: str, _=Depends(require_organiser)):
    conn = get_conn()
    ev = _event_or_404(conn, eid)
    buf = io.StringIO()
    csv.writer(buf).writerows(to_csv_rows(service.people(conn, ev["id"])))
    return Response(buf.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{ev["slug"]}-signups.csv"'})


@app.get("/access", response_class=HTMLResponse)
def access_page(request: Request, kind: str = "", next: str = "",
                _=Depends(require_organiser)):
    """Saved platform access — add, replace, test, remove."""
    conn = get_conn()
    return templates.TemplateResponse(request, "access.html", {
        "credentials": service.credentials(conn),
        "open_kind": kind,
        "next": next,
        "fields": ACCESS_FIELDS,
    })


@app.post("/access")
def add_access(kind: str = Form(...), secret: str = Form(...),
               label: str = Form(""), extra: str = Form(""),
               next: str = Form(""), _=Depends(require_organiser)):
    conn = get_conn()
    field = (ACCESS_FIELDS.get(kind, {}).get("extra") or ("extra",))[0]
    conn.execute(
        "INSERT INTO credential (id, kind, label, secret, config, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (db.new_id(), kind, label.strip() or NAMES.get(kind, kind), secret.strip(),
         json.dumps({field: extra.strip() or None}), db.now()))
    conn.commit()
    return RedirectResponse(next or "/access", status_code=303)


@app.post("/access/{cid}")
def update_access(cid: str, secret: str = Form(""), label: str = Form(""),
                  extra: str = Form(""), next: str = Form(""),
                  _=Depends(require_organiser)):
    """Replace the token or the destination details. Blank secret keeps the old one."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM credential WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such access")
    field = (ACCESS_FIELDS.get(row["kind"], {}).get("extra") or ("extra",))[0]
    conn.execute(
        "UPDATE credential SET secret = ?, label = ?, config = ? WHERE id = ?",
        (secret.strip() or row["secret"], label.strip() or row["label"],
         json.dumps({field: extra.strip() or None}), cid))
    conn.commit()
    return RedirectResponse(next or "/access", status_code=303)


@app.post("/access/{cid}/delete")
def delete_access(cid: str, _=Depends(require_organiser)):
    conn = get_conn()
    conn.execute("UPDATE channel SET credential_id = NULL WHERE credential_id = ?", (cid,))
    conn.execute("DELETE FROM credential WHERE id = ?", (cid,))
    conn.commit()
    return RedirectResponse("/access?checked=removed", status_code=303)


@app.post("/access/{cid}/check")
def check_access(cid: str, _=Depends(require_organiser)):
    """Ask the platform whether the token works, before an event depends on it."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM credential WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such access")
    cred = dict(row)
    try:
        adapter = get(cred["kind"], cred)
        who = adapter.whoami() if hasattr(adapter, "whoami") else None
        msg = f"working{' — organisation ' + str(who) if who else ''}"
    except Exception as exc:
        msg = f"not working: {exc}"
    return RedirectResponse(f"/access?checked={quote(msg)}", status_code=303)


# --------------------------------------------------------------- public page

@app.get("/l/{slug}.ics")
def ics(slug: str):
    """Add to calendar — free, and it stops the 'what time was it again?' emails."""
    conn = get_conn()
    ev = service.get_event(conn, slug)
    if ev is None:
        raise HTTPException(404, "no such event")

    def stamp(v: str | None) -> str:
        return (v or "").replace("-", "").replace(":", "")[:15] or ""

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Turnout//EN", "BEGIN:VEVENT",
        f"UID:{ev['id']}@turnout", f"SUMMARY:{ev['title']}",
        f"DTSTART:{stamp(ev['starts_at'])}",
    ]
    if ev["ends_at"]:
        lines.append(f"DTEND:{stamp(ev['ends_at'])}")
    if ev["venue"] or ev["address"]:
        lines.append(f"LOCATION:{ev['venue']} {ev['address']}".strip())
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return PlainTextResponse("\r\n".join(lines), media_type="text/calendar")


# NOTE: this must stay below the .ics route. A single path segment matches
# "slug.ics" too, and Starlette takes the first route that matches.
@app.get("/l/{slug}", response_class=HTMLResponse)
def link_list(slug: str, request: Request):
    """The one URL. No auth, no cookies, no JavaScript."""
    conn = get_conn()
    ev = service.get_event(conn, slug)
    if ev is None:
        raise HTTPException(404, "no such event")
    c = cap.read(conn, ev["id"])
    channels = [ch for ch in service.get_channels(conn, ev["id"])
                if ch["show_on_list"] and ch["url"]]
    return templates.TemplateResponse(request, "list.html", {
        "e": ev, "channels": channels, "cap": c,
        "remaining_for": c.remaining_for, "capability": capability,
    })


@app.get("/l/{slug}/qr.svg")
def qr(slug: str, request: Request):
    """A QR for the poster, the wall and the door."""
    import qrcode
    import qrcode.image.svg

    conn = get_conn()
    if service.get_event(conn, slug) is None:
        raise HTTPException(404, "no such event")
    url = str(request.url_for("link_list", slug=slug))
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml")


@app.get("/healthz")
def healthz():
    return {"ok": True}
