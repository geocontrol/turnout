import importlib
import sys
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
    # /app calls update.check(), which defaults to enabled and would
    # otherwise make a real HTTP request to GitHub. The test suite is
    # network-free, so disable it before the app (and its TestClient) exist.
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
    yield main, TestClient(main.app)
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
