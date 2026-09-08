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
import logging
import os
import secrets
import threading
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import __version__, backup, paths, update
from . import capacity as cap
from . import db, service
from .adapters import (
    ACCESS_FIELDS,
    CODES,
    IMPLEMENTED,
    KINDS,
    NAMES,
    NEEDS_ACCESS,
    NOTES,
    can,
    capability,
    get,
)
from .merge import suggest_merges, to_csv_rows

#: Desktop mode: launched from an icon rather than a terminal or a container.
DESKTOP = os.environ.get("TURNOUT_DESKTOP") == "1"

log = logging.getLogger("turnout.main")

templates = Jinja2Templates(directory=str(paths.resource_dir() / "templates"))
templates.env.filters["from_json"] = lambda v: json.loads(v or "{}")
templates.env.globals.update(
    NAMES=NAMES,
    NOTES=NOTES,
    CODES=CODES,
    KINDS=KINDS,
    IMPLEMENTED=IMPLEMENTED,
    can=can,
    capability=capability,
    capability_rows=service.capability_rows,
    NEEDS_ACCESS=NEEDS_ACCESS,
    ACCESS_FIELDS=ACCESS_FIELDS,
    desktop=DESKTOP,
    version=__version__,
    # base.html's update banner. A global rather than per-route context
    # because it belongs on every organiser page; a cached read, because a
    # page render must never wait on the network. turnout/desktop.py does
    # the fetching, once per launch, on its own thread.
    update_available=update.cached,
)

app = FastAPI(title="Turnout", docs_url=None, redoc_url=None)
_conn = None
TOKEN = os.environ.get("TURNOUT_TOKEN")

#: Set by POST /app/quit. turnout/desktop.py watches this and stops the server.
QUIT = threading.Event()


def local_token() -> str | None:
    """This install's browser token, created on first use.

    Written 0600 so other users on a shared machine cannot read it. Only
    meaningful in desktop mode; the hosted deployment uses TURNOUT_TOKEN.
    """
    if not DESKTOP:
        return None
    p = paths.token_path()
    if not p.exists():
        paths.ensure_data_dir()
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    return p.read_text().strip()


def get_conn():
    global _conn
    if _conn is None:
        _conn = db.connect()
    return _conn


def require_organiser(request: Request):
    """Organiser routes only. The public link list never calls this.

    Two deployments, two secrets. A hosted Turnout sets TURNOUT_TOKEN and
    that is the check. A desktop Turnout has no TURNOUT_TOKEN, and used to
    return True to everything — which left /app/restore, a route that
    replaces the whole database from an upload, standing on LocalGuard alone.
    LocalGuard is mounted by the launcher, in one line, in another module: a
    single deletion there would have opened every organiser route with no
    test noticing. So the route layer checks the same per-install token
    itself, and the two defences are genuinely two.

    /app/enter is not routed through here — it is the door the cookie comes
    from, and it does its own comparison against the same token.
    """
    if TOKEN:
        supplied = request.headers.get("authorization", "").removeprefix(
            "Bearer "
        ).strip() or request.cookies.get("turnout_token", "")
        if supplied != TOKEN:
            raise HTTPException(401, "not signed in")
        return True
    if DESKTOP:
        expected = local_token() or ""
        supplied = request.cookies.get("turnout_local", "")
        if not expected or not secrets.compare_digest(supplied, expected):
            raise HTTPException(403, "Open Turnout from its icon.")
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
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "events": events,
            "options": service.publishing_options(conn),
        },
    )


@app.post("/events")
async def create_event(
    request: Request,
    _=Depends(require_organiser),
    title: str = Form(...),
    strap: str = Form(""),
    body: str = Form(""),
    starts_at: str = Form(...),
    ends_at: str = Form(""),
    venue: str = Form(""),
    address: str = Form(""),
    accessibility: str = Form(""),
    contact: str = Form(""),
    capacity: str = Form(""),
    oversell_pct: int = Form(0),
    capacity_mode: str = Form("pool"),
):
    """Everything the home page asked for, in one go.

    The whole event and every platform it should appear on arrive together,
    because the alternative — a title and a date, then five more screens — is
    how a group ends up with an event published without its access notes.
    Only the title and the start are required; the rest can stay blank and be
    filled in later on the event page, which is the same form.
    """
    conn = get_conn()
    eid = db.new_id()
    slug = service.slugify(title, eid[:4])
    conn.execute(
        """INSERT INTO event (id, slug, title, strap, body, starts_at, ends_at,
           venue, address, accessibility, contact, capacity, oversell_pct,
           capacity_mode, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            eid,
            slug,
            title.strip(),
            strap,
            body,
            starts_at,
            ends_at or None,
            venue,
            address,
            accessibility,
            contact,
            int(capacity) if capacity.strip() else None,
            oversell_pct,
            capacity_mode,
            db.now(),
            db.now(),
        ),
    )
    db.log(conn, eid, "··", "event created in Turnout")

    # Platforms are ticked on the same form, each with an optional address for
    # anything the organiser has already put up themselves.
    form = await request.form()
    for kind in form.getlist("kinds"):
        if kind not in KINDS:
            continue
        service.add_channel(conn, eid, kind, url=str(form.get(f"url_{kind}", "")).strip())
    conn.commit()
    return RedirectResponse(f"/events/{eid}", status_code=303)


@app.get("/events/{eid}", response_class=HTMLResponse)
def event_page(
    eid: str,
    request: Request,
    tab: str = "event",
    since: int | None = None,
    _=Depends(require_organiser),
):
    conn = get_conn()
    ev = _event_or_404(conn, eid)
    people = service.people(conn, ev["id"])
    outcome = []
    if since is not None:
        outcome = conn.execute(
            "SELECT * FROM log WHERE event_id = ? AND id > ? ORDER BY id", (ev["id"], since)
        ).fetchall()
    return templates.TemplateResponse(
        request,
        "event.html",
        {
            "e": ev,
            "tab": tab,
            "outcome": outcome,
            "acted": since is not None,
            "channels": service.get_channels(conn, ev["id"]),
            "cap": cap.read(conn, ev["id"]),
            "drift": service.drift(ev),
            "people": people,
            "suggestions": suggest_merges(people),
            "log": conn.execute(
                "SELECT * FROM log WHERE event_id = ? ORDER BY id DESC LIMIT 80", (ev["id"],)
            ).fetchall(),
            "credentials": service.credentials(conn),
            "needs_access": {
                c["id"]: service.needs_access(conn, c) for c in service.get_channels(conn, ev["id"])
            },
        },
    )


@app.post("/events/{eid}")
def edit_event(
    eid: str,
    request: Request,
    _=Depends(require_organiser),
    title: str = Form(...),
    strap: str = Form(""),
    body: str = Form(""),
    starts_at: str = Form(...),
    ends_at: str = Form(""),
    venue: str = Form(""),
    address: str = Form(""),
    accessibility: str = Form(""),
    contact: str = Form(""),
    capacity: str = Form(""),
    oversell_pct: int = Form(0),
    capacity_mode: str = Form("pool"),
):
    conn = get_conn()
    ev = _event_or_404(conn, eid)
    conn.execute(
        """UPDATE event SET title=?, strap=?, body=?, starts_at=?, ends_at=?,
           venue=?, address=?, accessibility=?, contact=?, capacity=?,
           oversell_pct=?, capacity_mode=?, updated_at=? WHERE id=?""",
        (
            title.strip(),
            strap,
            body,
            starts_at,
            ends_at or None,
            venue,
            address,
            accessibility,
            contact,
            int(capacity) if capacity.strip() else None,
            oversell_pct,
            capacity_mode,
            db.now(),
            ev["id"],
        ),
    )
    conn.commit()
    return RedirectResponse(f"/events/{ev['id']}", status_code=303)


@app.post("/events/{eid}/channels")
def add_channel(
    eid: str,
    kind: str = Form(...),
    label: str = Form(""),
    url: str = Form(""),
    allocation: str = Form(""),
    policy: str = Form("waitlist"),
    _=Depends(require_organiser),
):
    conn = get_conn()
    ev = _event_or_404(conn, eid)
    service.add_channel(
        conn,
        ev["id"],
        kind,
        label=label,
        url=url,
        allocation=int(allocation) if allocation.strip() else None,
        policy=policy,
    )
    conn.commit()
    return RedirectResponse(f"/events/{ev['id']}?tab=channels", status_code=303)


@app.post("/channels/{cid}")
def edit_channel(
    cid: str,
    url: str = Form(""),
    allocation: str = Form(""),
    policy: str = Form("waitlist"),
    show_on_list: str = Form(""),
    credential_id: str = Form(""),
    _=Depends(require_organiser),
):
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
        (
            url.strip(),
            int(allocation) if allocation.strip() else None,
            policy,
            1 if show_on_list else 0,
            credential_id or None,
            state,
            db.now(),
            cid,
        ),
    )
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
    since = db.last_log_id(conn, ev["id"])
    service.publish(conn, ev["id"])
    return RedirectResponse(f"/events/{ev['id']}?tab=channels&since={since}", status_code=303)


@app.post("/events/{eid}/push")
def do_push(eid: str, _=Depends(require_organiser)):
    conn = get_conn()
    ev = _event_or_404(conn, eid)
    since = db.last_log_id(conn, ev["id"])
    service.push_update(conn, ev["id"])
    return RedirectResponse(f"/events/{ev['id']}?tab=channels&since={since}", status_code=303)


@app.post("/events/{eid}/close")
def do_close(eid: str, open_: str = Form(""), _=Depends(require_organiser)):
    conn = get_conn()
    ev = _event_or_404(conn, eid)
    since = db.last_log_id(conn, ev["id"])
    service.set_open(conn, ev["id"], bool(open_))
    return RedirectResponse(f"/events/{ev['id']}?tab=channels&since={since}", status_code=303)


@app.post("/events/{eid}/sync")
def do_sync(eid: str, _=Depends(require_organiser)):
    conn = get_conn()
    ev = _event_or_404(conn, eid)
    since = db.last_log_id(conn, ev["id"])
    # Ask first whether the listings are still there. A deleted one has to be
    # noticed here rather than by somebody following a link off a poster.
    service.recheck_listings(conn, ev["id"])
    service.sync_signups(conn, ev["id"])
    service.auto_close_if_full(conn, ev["id"])
    return RedirectResponse(f"/events/{ev['id']}?tab=signups&since={since}", status_code=303)


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
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{ev["slug"]}-signups.csv"'},
    )


@app.get("/access", response_class=HTMLResponse)
def access_page(request: Request, kind: str = "", next: str = "", _=Depends(require_organiser)):
    """Saved platform access — add, replace, test, remove."""
    conn = get_conn()
    return templates.TemplateResponse(
        request,
        "access.html",
        {
            "credentials": service.credentials(conn),
            "open_kind": kind,
            "next": next,
            "fields": ACCESS_FIELDS,
        },
    )


@app.post("/access")
def add_access(
    kind: str = Form(...),
    secret: str = Form(...),
    label: str = Form(""),
    extra: str = Form(""),
    next: str = Form(""),
    _=Depends(require_organiser),
):
    conn = get_conn()
    field = (ACCESS_FIELDS.get(kind, {}).get("extra") or ("extra",))[0]
    conn.execute(
        "INSERT INTO credential (id, kind, label, secret, config, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            db.new_id(),
            kind,
            label.strip() or NAMES.get(kind, kind),
            secret.strip(),
            json.dumps({field: extra.strip() or None}),
            db.now(),
        ),
    )
    conn.commit()
    return RedirectResponse(next or "/access", status_code=303)


@app.post("/access/{cid}")
def update_access(
    cid: str,
    secret: str = Form(""),
    label: str = Form(""),
    extra: str = Form(""),
    next: str = Form(""),
    _=Depends(require_organiser),
):
    """Replace the token or the destination details. Blank secret keeps the old one."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM credential WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such access")
    field = (ACCESS_FIELDS.get(row["kind"], {}).get("extra") or ("extra",))[0]
    conn.execute(
        "UPDATE credential SET secret = ?, label = ?, config = ? WHERE id = ?",
        (
            secret.strip() or row["secret"],
            label.strip() or row["label"],
            json.dumps({field: extra.strip() or None}),
            cid,
        ),
    )
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
        # Each adapter names what its key actually opened — an Eventbrite
        # organisation, a Luma calendar — because a token pasted from the
        # wrong account is otherwise silent until an event lands in it.
        who = adapter.check() if hasattr(adapter, "check") else None
        msg = f"working{' — ' + str(who) if who else ''}"
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
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Turnout//EN",
        "BEGIN:VEVENT",
        f"UID:{ev['id']}@turnout",
        f"SUMMARY:{ev['title']}",
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
    channels = [
        ch for ch in service.get_channels(conn, ev["id"]) if ch["show_on_list"] and ch["url"]
    ]
    return templates.TemplateResponse(
        request,
        "list.html",
        {
            "e": ev,
            "channels": channels,
            "cap": c,
            "remaining_for": c.remaining_for,
            "capability": capability,
        },
    )


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
    """Also how a second launch recognises an already-running Turnout."""
    return {"ok": True, "app": "turnout", "version": __version__}


# ----------------------------------------------------------- this computer
#
# Only mounted when Turnout was launched from its icon. A hosted deployment
# has no business offering "quit" or a filesystem path to a log file.

if DESKTOP:

    @app.get("/app/enter")
    def app_enter(k: str = ""):
        """Trade the launch token for a cookie, then get it out of the URL.

        The token arrives as a query parameter because that is the only thing
        you can hand a browser you are opening. It does not stay there: a URL
        lives in history, in the address bar, and in whatever the organiser
        pastes into a support email.
        """
        expected = local_token()
        if not secrets.compare_digest(k, expected or ""):
            raise HTTPException(403, "open Turnout from its icon")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("turnout_local", expected, httponly=True, samesite="lax", path="/")
        return response

    @app.get("/app", response_class=HTMLResponse)
    def app_page(request: Request, said: str = "", _=Depends(require_organiser)):
        return templates.TemplateResponse(
            request,
            "app.html",
            {
                "version": __version__,
                "data_dir": str(paths.data_dir()),
                "backups": sorted((p.name for p in paths.backup_dir().glob("*.zip")), reverse=True)[
                    :10
                ],
                "update_check": update.enabled(),
                "said": said,
            },
        )

    @app.post("/app/backup")
    def app_backup(include_credentials: str = Form(""), _=Depends(require_organiser)):
        archive = backup.write(
            get_conn(), paths.backup_dir(), include_credentials=bool(include_credentials)
        )
        return RedirectResponse(f"/app?said=Backed+up+to+{archive.name}", status_code=303)

    @app.post("/app/restore")
    async def app_restore(file: UploadFile = File(...), _=Depends(require_organiser)):
        global _conn
        paths.ensure_data_dir()
        staged = paths.backup_dir() / "incoming.zip"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(await file.read())
        try:
            safety = backup.restore(staged, Path(paths.db_path()), get_conn(), paths.backup_dir())
        except backup.Unsupported:
            return RedirectResponse(
                "/app?said=That+backup+was+written+by+a+newer+Turnout+than+this+one",
                status_code=303,
            )
        except ValueError:
            return RedirectResponse("/app?said=That+file+is+not+a+Turnout+backup", status_code=303)
        except Exception:  # a full disk must not brick the app
            # restore() closes the connection before it copies anything, so
            # anything escaping it leaves _conn pointing at a closed handle.
            # Letting that propagate turned one failed restore into a 500 on
            # every organiser page afterwards, which from where the volunteer
            # sits looks exactly like having destroyed the database.
            log.exception("restore failed")
            return RedirectResponse(
                "/app?said=The+restore+did+not+finish.+Your+data+has+not+been+changed.",
                status_code=303,
            )
        finally:
            staged.unlink(missing_ok=True)
            _conn = None  # cheap to reopen; never leave a closed handle here
        return RedirectResponse(
            f"/app?said=Restored.+Your+previous+data+is+in+{safety.name}", status_code=303
        )

    @app.post("/app/update-check")
    def app_update_check(on: str = Form(""), _=Depends(require_organiser)):
        update.set_enabled(bool(on))
        return RedirectResponse("/app?said=Saved", status_code=303)

    @app.get("/app/log", response_class=PlainTextResponse)
    def app_log(_=Depends(require_organiser)):
        """The last of the log, because there is no terminal to read it in."""
        try:
            return paths.log_path().read_text()[-200_000:]
        except OSError:
            return "No log yet."

    @app.post("/app/quit", response_class=HTMLResponse)
    def app_quit(request: Request, _=Depends(require_organiser)):
        QUIT.set()
        return templates.TemplateResponse(
            request,
            "app.html",
            {
                "stopped": True,
                "version": __version__,
                "data_dir": str(paths.data_dir()),
                "backups": [],
                "update_check": update.enabled(),
                "said": "",
            },
        )
