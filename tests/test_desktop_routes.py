import importlib
import json
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from turnout import db  # noqa: E402
from turnout import paths  # noqa: E402
from turnout import update  # noqa: E402


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    """The app in desktop mode, on a real database file in tmp_path."""
    monkeypatch.setenv("TURNOUT_DESKTOP", "1")
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TURNOUT_DB", raising=False)
    # Nothing on the request path fetches anything any more — the launcher
    # does that on its own thread and base.html reads the cached answer —
    # but leave this off so a regression that puts a fetch back on a page
    # render fails here rather than reaching GitHub from the test suite.
    update.set_enabled(False)
    from turnout import main

    importlib.reload(main)
    conn = db.connect(str(tmp_path / "turnout.db"))
    conn.execute(
        """INSERT INTO event (id, slug, title, starts_at, oversell_pct,
           capacity_mode, created_at, updated_at)
           VALUES ('E','e','Public meeting','2026-09-17T19:00',0,'pool',?,?)""",
        (db.now(), db.now()),
    )
    conn.commit()
    monkeypatch.setattr(main, "_conn", conn)
    client = TestClient(main.app)
    # The organiser gets here by following the launch URL, which sets this
    # cookie. require_organiser now checks it in desktop mode, so a client
    # without one is an unauthenticated stranger — which is the subject of
    # its own test below, not the starting position for all the others.
    client.cookies.set("turnout_local", main.local_token())
    yield main, client
    # Undo the reload. main.DESKTOP is read at import time, so without this
    # every test file that runs later gets a desktop-mode app — and
    # test_turnout.py sorts after test_desktop_routes.py.
    monkeypatch.undo()
    importlib.reload(main)


def test_healthz_identifies_itself_so_a_second_launch_can_recognise_it(desktop):
    _, client = desktop
    body = client.get("/healthz").json()
    assert body["app"] == "turnout"
    assert body["ok"] is True
    assert body["version"]


def test_the_this_computer_page_lists_where_the_data_lives(desktop, tmp_path):
    _, client = desktop
    page = client.get("/app").text
    assert str(tmp_path) in page


def test_the_backup_warning_says_what_a_backup_actually_contains(desktop):
    """The whole point of this page is the honesty of this copy.

    A softened or deleted warning here would not be caught by any other
    test, so this pins the distinctive substrings rather than the exact
    paragraph — ordinary copy-editing survives, deletion or softening does
    not.
    """
    _, client = desktop
    page = client.get("/app").text
    assert "live Eventbrite, Luma and Ticket Tailor keys" in page
    assert "The backup always contains the names and email addresses" in page
    assert "Include saved platform access" in page
    # Off by default: find the checkbox's own markup, not just the label
    # text next to it, and confirm no "checked" attribute sits on it.
    start = page.index('name="include_credentials"')
    end = page.index("Include saved platform access")
    assert "checked" not in page[start:end]


def test_backing_up_writes_an_archive(desktop, tmp_path):
    _, client = desktop
    client.post("/app/backup", data={}, follow_redirects=False)
    assert list((tmp_path / "backups").glob("turnout-backup-*.zip"))


def test_backing_up_leaves_the_keys_out_unless_asked(desktop, tmp_path):
    import sqlite3
    import zipfile
    from turnout import backup as bk

    main, client = desktop
    main._conn.execute(
        """INSERT INTO credential (id, kind, label, secret, created_at)
           VALUES ('K','eventbrite','Union','live-secret-token',?)""",
        (db.now(),),
    )
    main._conn.commit()

    client.post("/app/backup", data={}, follow_redirects=False)
    archive = sorted((tmp_path / "backups").glob("*.zip"))[-1]
    with zipfile.ZipFile(archive) as z:
        (tmp_path / "c.db").write_bytes(z.read(bk.DB_ENTRY))
    c = sqlite3.connect(tmp_path / "c.db")
    assert c.execute("SELECT count(*) FROM credential").fetchone()[0] == 0


def test_entering_sets_the_cookie_and_redirects_the_token_out_of_the_url(desktop):
    main, client = desktop
    token = main.local_token()
    r = client.get(f"/app/enter?k={token}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert r.cookies["turnout_local"] == token


def test_entering_with_the_wrong_token_is_refused(desktop):
    _, client = desktop
    assert client.get("/app/enter?k=wrong", follow_redirects=False).status_code == 403


def test_quitting_sets_the_flag_the_launcher_watches(desktop):
    main, client = desktop
    assert not main.QUIT.is_set()
    client.post("/app/quit", follow_redirects=False)
    assert main.QUIT.is_set()


def test_the_log_can_be_read_back_without_a_terminal(desktop, tmp_path):
    _, client = desktop
    (tmp_path / "turnout.log").write_text("something went wrong at 19:04\n")
    assert "something went wrong" in client.get("/app/log").text


def test_the_routes_defend_themselves_without_the_middleware(desktop):
    """LocalGuard is mounted in one line, in turnout/desktop.py, by the
    launcher. This TestClient has no middleware at all — which is exactly
    what deleting that line would leave behind. Every organiser route must
    still refuse a caller with no cookie, /app/restore above all: it
    replaces the whole database from an upload.
    """
    main, _ = desktop
    stranger = TestClient(main.app)  # no turnout_local cookie

    for method, path in [
        ("get", "/"),
        ("get", "/app"),
        ("get", "/app/log"),
        ("post", "/app/quit"),
        ("post", "/app/backup"),
        ("post", "/app/restore"),
    ]:
        r = getattr(stranger, method)(path, follow_redirects=False)
        assert r.status_code == 403, f"{method.upper()} {path} was not refused"
    assert not main.QUIT.is_set(), "a refused quit set the flag anyway"


def test_the_public_link_list_is_still_public(desktop):
    """The token guards the organiser app. It must not creep onto the page
    that gets printed on a leaflet."""
    main, _ = desktop
    stranger = TestClient(main.app)
    assert stranger.get("/l/e").status_code == 200
    assert stranger.get("/healthz").status_code == 200


def _corrupt_backup(client, tmp_path):
    """A genuine Turnout backup with one byte flipped inside the database.

    Past the SQLite header, so the manifest gate lets it through, and stored
    rather than deflated so the flip lands in the payload verbatim — the
    zip's own CRC is what catches it, part way through the copy.
    """
    from turnout import backup as bk

    client.post("/app/backup", data={"include_credentials": "1"}, follow_redirects=False)
    good = max((tmp_path / "backups").glob("turnout-backup-*.zip"))
    with zipfile.ZipFile(good) as z:
        manifest, dbbytes = z.read(bk.MANIFEST), z.read(bk.DB_ENTRY)

    corrupt = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(corrupt, "w", zipfile.ZIP_STORED) as z:
        z.writestr(bk.MANIFEST, manifest)
        z.writestr(bk.DB_ENTRY, dbbytes)
    raw = bytearray(corrupt.read_bytes())
    raw[raw.index(dbbytes) + 2000] ^= 0xFF
    corrupt.write_bytes(bytes(raw))
    return corrupt


def test_a_restore_that_fails_late_leaves_the_app_serving(desktop, tmp_path):
    """The failure that used to brick Turnout.

    backup.restore() closes the connection before it copies, so anything it
    raised other than ValueError escaped the route with the module-level
    connection left pointing at a closed handle. The volunteer got a 500 on
    the restore and then a 500 on every page afterwards — which, from where
    they sit, looks exactly like having destroyed the database.
    """
    _, client = desktop
    corrupt = _corrupt_backup(client, tmp_path)

    r = client.post(
        "/app/restore",
        files={"file": ("corrupt.zip", corrupt.read_bytes(), "application/zip")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "did+not+finish" in r.headers["location"]

    page = client.get("/")
    assert page.status_code == 200, "the app stopped serving after a failed restore"
    assert "Public meeting" in page.text, "the message promised the data was untouched"


def test_a_file_that_is_not_a_backup_is_still_named_as_such(desktop, tmp_path):
    """The two failures read differently, because they need different
    answers from the person holding the file."""
    _, client = desktop
    junk = tmp_path / "holiday-photos.zip"
    with zipfile.ZipFile(junk, "w") as z:
        z.writestr("beach.jpg", b"not a database")

    r = client.post(
        "/app/restore",
        files={"file": ("holiday-photos.zip", junk.read_bytes(), "application/zip")},
        follow_redirects=False,
    )
    assert "not+a+Turnout+backup" in r.headers["location"]
    assert client.get("/").status_code == 200


# ------------------------------------------------------------ update banner


def _remember_update(tmp_path, *, on=True, version="9.9.9"):
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "update_check": on,
                "last_check": time.time(),
                "latest": {"version": version, "url": "https://example.org/get", "notes": "Fixes"},
            }
        )
    )


def test_the_update_banner_is_on_every_organiser_page(desktop, tmp_path):
    """Until Homebrew, winget and Flathub exist this notice is the only
    update channel there is. Behind a settings link a volunteer has no
    reason to open, it reaches nobody."""
    _, client = desktop
    _remember_update(tmp_path)

    for path in ("/", "/app"):
        assert "Turnout 9.9.9 is available." in client.get(path).text, path


def test_the_banner_is_absent_when_checking_is_switched_off(desktop, tmp_path):
    """Off means off — no fetching, and nothing on screen from an answer
    fetched back when it was on."""
    _, client = desktop
    _remember_update(tmp_path, on=False)

    assert "9.9.9" not in client.get("/").text


def test_the_banner_never_advertises_the_version_already_running(desktop, tmp_path):
    """After an upgrade the cached answer is stale, and a banner offering
    the version you are on would sit there until the next daily check."""
    main, client = desktop
    _remember_update(tmp_path, version=main.__version__)

    assert "is available" not in client.get("/").text


def test_the_public_page_carries_no_desktop_banner(desktop, tmp_path):
    """/l/{slug} is what gets printed on a leaflet. It says nothing about
    this laptop."""
    _, client = desktop
    _remember_update(tmp_path)

    assert "9.9.9" not in client.get("/l/e").text


def test_rendering_a_page_never_reaches_for_the_network(desktop, tmp_path, monkeypatch):
    """update.check() is a five-second timeout when there is no signal, and
    it used to run on every load of the page holding Quit and Restore."""
    _, client = desktop
    _remember_update(tmp_path)

    def explode(*a, **kw):
        raise AssertionError("a page render tried to fetch the update feature")

    monkeypatch.setattr(update, "check", explode)

    assert client.get("/").status_code == 200
    assert client.get("/app").status_code == 200


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
def test_the_local_token_file_is_never_readable_by_other_users(desktop):
    """Written 0600 from the moment it is created, not chmod'd after the fact.

    A write-then-chmod sequence leaves the 32-byte secret sitting at the
    default mode — readable by every local user — for the interval before
    chmod runs. This must never be observable, not just true by the time
    anyone checks.
    """
    main, _ = desktop
    main.local_token()
    mode = paths.token_path().stat().st_mode & 0o777
    assert mode == 0o600
