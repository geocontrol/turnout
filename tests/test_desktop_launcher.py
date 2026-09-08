import os
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

import httpx  # noqa: E402

from turnout import desktop, paths  # noqa: E402


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
        except Exception:            # noqa: BLE001 — best-effort cleanup
            pass
        thread.join(timeout=5)
        # desktop.main() sets this via plain os.environ, not monkeypatch, so
        # it survives monkeypatch.undo() unless removed by hand here.
        os.environ.pop("TURNOUT_DESKTOP", None)
