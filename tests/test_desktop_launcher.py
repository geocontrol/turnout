import importlib
import os
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

import httpx  # noqa: E402

from turnout import desktop, paths, update  # noqa: E402


def _serve(body: bytes) -> tuple[int, HTTPServer]:
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1], srv


def test_a_free_preferred_port_is_taken_as_is():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    port, running = desktop.choose_port(free)
    assert port == free and running is False


def test_a_second_launch_finds_the_first_one():
    """Double-clicking the icon twice must not start a second server."""
    port, srv = _serve(b'{"ok":true,"app":"turnout","version":"0.1.0"}')
    try:
        chosen, running = desktop.choose_port(port)
        assert chosen == port and running is True
    finally:
        srv.shutdown()


def test_a_port_held_by_something_else_is_stepped_around():
    port, srv = _serve(b'{"this":"is not turnout"}')
    try:
        chosen, running = desktop.choose_port(port)
        assert chosen != port and running is False
    finally:
        srv.shutdown()


def test_the_self_test_reports_success(tmp_path, monkeypatch, capsys):
    """Run by a user who has been asked 'what does --self-test say?'."""
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(desktop, "_https_reachable", lambda: True)
    assert desktop.self_test() == 0
    out = capsys.readouterr().out
    assert "templates" in out and "OK" in out


def test_the_self_test_fails_loudly_when_tls_is_broken(tmp_path, monkeypatch, capsys):
    """The classic frozen-app failure: no CA bundle, so every adapter dies."""
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(desktop, "_https_reachable", lambda: False)
    assert desktop.self_test() == 1
    assert "FAILED" in capsys.readouterr().out


def test_the_launch_token_never_reaches_the_log_file(tmp_path, monkeypatch):
    """The sharpest hazard in the whole plan.

    The token travels as a query parameter — /app/enter?k=<token> — and
    /app/log later hands the log file back to the user with instructions to
    paste it into a support email. If the token is ever written to that
    file, a volunteer will email it to a stranger. uvicorn's access log is
    the only thing that would normally do this, so this test runs the real
    launcher end to end and inspects the actual file on disk, rather than
    trusting that `access_log=False` stays in place.
    """
    monkeypatch.delenv("TURNOUT_DESKTOP", raising=False)
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TURNOUT_NO_BROWSER", "1")
    monkeypatch.setenv("TURNOUT_NO_TRAY", "1")
    # The launcher kicks off an update check on its own thread. The suite is
    # network-free, so switch it off in the data directory it will read.
    update.set_enabled(False)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    monkeypatch.setattr(desktop, "choose_port", lambda *a, **kw: (free_port, False))

    thread = threading.Thread(target=desktop.main, args=([],), daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if desktop.is_turnout(free_port):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("the launcher did not come up in time")

        token = paths.token_path().read_text().strip()
        assert token, "no launch token was written"

        # Two defences, not one. LocalGuard is mounted by the launcher and
        # require_organiser checks the same token at the route; this asks
        # the running server, with both in place, whether a caller holding
        # no cookie can stop it or reach anything else behind /app.
        refused = httpx.post(f"http://127.0.0.1:{free_port}/app/quit")
        assert refused.status_code == 403
        assert desktop.is_turnout(free_port), "a refused quit stopped the server anyway"

        resp = httpx.get(
            f"http://127.0.0.1:{free_port}/app/enter?k={token}",
            follow_redirects=False,
        )
        assert resp.status_code == 303

        from turnout import main as web

        web.QUIT.set()
        thread.join(timeout=5)

        log_text = paths.log_path().read_text(encoding="utf-8")
        assert token not in log_text
    finally:
        try:
            from turnout import main as web

            web.QUIT.set()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
        thread.join(timeout=5)
        # desktop.main() sets this via plain os.environ, not monkeypatch, so
        # it survives monkeypatch.undo() unless removed by hand here.
        os.environ.pop("TURNOUT_DESKTOP", None)


def test_a_missing_tray_does_not_take_the_server_down_with_it(tmp_path, monkeypatch):
    """The spec's promise, on the platform where it matters.

    'If the tray fails to start, Turnout logs it and carries on serving.'
    The launcher could not tell three cases apart — the user quit the tray,
    pystray would not import, the icon would not start — and set QUIT for
    all three. On a Linux desktop with no AppIndicator support that meant
    the organiser double-clicked the icon, the browser opened, and the
    server behind the tab was gone before the page painted.

    A real tray loop cannot be driven headlessly. This is not that: it is
    the import failing exactly as it does on such a machine, with the real
    launcher, a real server, and Quit driven from the web UI.
    """
    monkeypatch.setenv("TURNOUT_DESKTOP", "1")
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TURNOUT_NO_BROWSER", "1")
    monkeypatch.delenv("TURNOUT_NO_TRAY", raising=False)  # take the tray path
    update.set_enabled(False)  # the suite is network-free

    # turnout.main reads TURNOUT_DESKTOP at import time and an earlier test
    # may already have imported it. Reload with desktop mode set, so /app/*
    # exists — and so the launcher gets an app whose middleware stack has
    # not been built yet, which is the only state add_middleware accepts.
    from turnout import main as web

    importlib.reload(web)

    # No pystray, the way a stock GNOME session has no pystray.
    monkeypatch.setitem(sys.modules, "pystray", None)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(desktop, "choose_port", lambda *a, **kw: (port, False))

    web.QUIT.clear()
    thread = threading.Thread(target=desktop.main, args=([],), daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if desktop.is_turnout(port):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("the launcher did not come up in time")

        # The old code shut down 0.8s after opening the browser. Wait past
        # that, then check the organiser still has something to look at.
        time.sleep(2.0)
        assert thread.is_alive(), "the launcher exited without anyone asking it to"
        assert desktop.is_turnout(port), "no tray, so no server — the bug this pins"

        token = paths.token_path().read_text().strip()
        stopped = httpx.post(f"http://127.0.0.1:{port}/app/quit", cookies={"turnout_local": token})
        assert stopped.status_code == 200

        thread.join(timeout=10)
        assert not thread.is_alive(), "Quit in the web UI did not end the process"
    finally:
        web.QUIT.set()
        thread.join(timeout=10)
        # desktop.main() sets TURNOUT_DESKTOP with plain os.environ, so it
        # survives monkeypatch.undo(); drop it and put turnout.main back the
        # way the rest of the suite expects to find it.
        os.environ.pop("TURNOUT_DESKTOP", None)
        importlib.reload(web)


# ---------------------------------------------------------------- entry URL


def test_a_relaunch_opens_the_door_it_has_a_key_to(tmp_path, monkeypatch):
    """Double-clicking the icon while Turnout is already running.

    The turnout_local cookie has no max_age, so it is a session cookie: a
    closed browser, a different browser or a private window and it is gone.
    Sending a relaunch to bare / would meet 'Open Turnout from its icon',
    which is precisely what the organiser just did, and there is no way in
    from there.
    """
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))
    paths.ensure_data_dir()
    paths.token_path().write_text("s3cret-token\n")

    assert desktop._entry_url(8100) == "http://127.0.0.1:8100/app/enter?k=s3cret-token"


def test_a_relaunch_with_no_readable_token_still_opens_something(tmp_path, monkeypatch):
    """An unreadable token file is a bad afternoon, not a crash on launch."""
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path / "nothing-here"))

    assert desktop._entry_url(8100) == "http://127.0.0.1:8100/"


# --------------------------------------------------------------- quit wiring
#
# pystray.Icon.run() blocks the main thread until icon.stop() is called.
# There is no way to drive a real tray icon headlessly in a test, but the
# wiring that decides whether Quit actually reaches the icon is ordinary
# Python and does not need one.


def test_stop_reaches_both_the_server_flag_and_a_running_tray_icon():
    """Without this, a successful tray leaves the process running forever
    with a dead server behind it: stopping uvicorn is not the same as
    unblocking icon.run()."""

    class FakeServer:
        should_exit = False

    class FakeIcon:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    server = FakeServer()
    tray: list = []
    stop = desktop._make_stop(server, tray)

    icon = FakeIcon()
    tray.append(icon)  # what `started` does once _run_tray comes up

    stop()

    assert server.should_exit is True
    assert icon.stopped is True


def test_stop_is_safe_when_the_tray_never_started():
    """/app/quit can race the tray still starting up, or run on a platform
    where the tray failed to start at all — either way there is no icon to
    reach yet, and stop() must still bring the server down."""

    class FakeServer:
        should_exit = False

    server = FakeServer()
    stop = desktop._make_stop(server, [])

    stop()

    assert server.should_exit is True
