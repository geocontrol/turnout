"""The launcher: what happens when somebody double-clicks the icon.

uvicorn runs on a background thread and the tray icon owns the main one,
because macOS requires AppKit to be on the main thread. The tray is
deliberately optional — Linux tray support is unreliable enough that a user
must never depend on it to stop the program, so Quit also lives in the web UI.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import webbrowser
from typing import Any, Callable

import httpx

from . import __version__, paths

PREFERRED_PORT = 8100
log = logging.getLogger("turnout.desktop")

_logging_configured = False


def setup_logging() -> None:
    """Log to a rotating file. There is no terminal to print to."""
    global _logging_configured
    if _logging_configured:
        return          # a second call would duplicate every line
    _logging_configured = True

    root = logging.getLogger()
    paths.ensure_data_dir()
    handler = logging.handlers.RotatingFileHandler(
        paths.log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    # httpx logs "HTTP Request: <method> <full URL> <status>" at INFO for
    # every call, and that logger propagates to root like everything else.
    # The one URL in this app that must never be written down is the launch
    # link — http://127.0.0.1:{port}/app/enter?k=<token> — and /app/log
    # hands this very file back to the user with instructions to paste it
    # into a support email. Nothing today fetches that URL with httpx (the
    # OS browser opens it instead), but that is exactly the kind of thing a
    # future change could get wrong silently. Keep httpx's own request
    # logging below what reaches the file, so a URL is never the reason a
    # token ends up in it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def is_turnout(port: int) -> bool:
    """Whether the thing already on this port is a Turnout."""
    try:
        body = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1.0).json()
        return body.get("app") == "turnout"
    except Exception:                  # noqa: BLE001 — anything means "not us"
        return False


def _free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def choose_port(preferred: int = PREFERRED_PORT) -> tuple[int, bool]:
    """Return (port, already_running).

    Double-clicking the icon a second time should open a tab, not start a
    second server against the same database.
    """
    if _free(preferred):
        return preferred, False
    if is_turnout(preferred):
        return preferred, True
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1], False


def _write_port(port: int) -> None:
    paths.ensure_data_dir()
    try:
        state = json.loads(paths.state_path().read_text())
    except (OSError, ValueError):
        state = {}
    state["port"] = port
    paths.state_path().write_text(json.dumps(state, indent=2))


def _https_reachable() -> bool:
    """Whether the bundle can complete a TLS handshake.

    A frozen build that did not collect its CA bundle works perfectly until
    an organiser clicks 'Check it works' on their Eventbrite key.
    """
    try:
        httpx.get("https://api.github.com/", timeout=10.0)
        return True
    except Exception:                  # noqa: BLE001
        return False


def self_test() -> int:
    """Diagnostics for 'it will not start — what does --self-test say?'"""
    checks: list[tuple[str, bool]] = []

    d = paths.ensure_data_dir()
    probe = d / ".writable"
    try:
        probe.write_text("x")
        probe.unlink()
        checks.append(("data directory is writable", True))
    except OSError:
        checks.append(("data directory is writable", False))

    checks.append(("templates were bundled",
                   (paths.resource_dir() / "templates" / "base.html").exists()))

    try:
        import qrcode
        import qrcode.image.svg
        qrcode.make("https://example.org",
                    image_factory=qrcode.image.svg.SvgPathImage, border=2)
        checks.append(("QR codes can be drawn", True))
    except Exception:                  # noqa: BLE001
        checks.append(("QR codes can be drawn", False))

    checks.append(("HTTPS works (needed by every platform adapter)",
                   _https_reachable()))

    print(f"Turnout {__version__}")
    print(f"data directory: {paths.data_dir()}")
    for name, ok in checks:
        print(f"  [{'OK' if ok else 'FAILED'}] {name}")
    return 0 if all(ok for _, ok in checks) else 1


def _make_stop(server: Any, tray: list) -> Callable[[], None]:
    """Build the one callback that actually ends the process.

    `server` is a uvicorn.Server (typed loosely so this module never needs
    to import uvicorn just to be imported itself — main() imports it lazily
    so --self-test users do not need it installed).

    /app/quit (via the watcher thread in main()) and the tray's own Quit
    menu item both call this. Stopping the uvicorn server is not enough on
    its own: pystray.Icon.run() blocks the main thread until icon.stop() is
    called, so without reaching into `tray` a successful tray leaves the
    process running forever with a dead server behind it. `tray` starts
    empty and gains the running icon once `_run_tray` hands it back (via
    the `started` callback below) — stop() must stay a no-op on that icon
    until then, since /app/quit can race the tray still starting up.
    """
    def stop() -> None:
        server.should_exit = True
        for icon in tray:
            icon.stop()          # unblocks _run_tray on the main thread
    return stop


def _run_tray(open_url: str, stop: Callable[[], None],
              started: Callable[[object], None]) -> None:
    """Best-effort tray icon. Never load-bearing: Quit is also in the web UI."""
    try:
        import pystray
        from PIL import Image
    except Exception as exc:           # noqa: BLE001
        log.info("no tray support available: %s", exc)
        return
    try:
        image = Image.open(paths.resource_dir() / "resources" / "icon.png")
        icon = pystray.Icon(
            "turnout", image, "Turnout",
            menu=pystray.Menu(
                pystray.MenuItem("Open Turnout",
                                 lambda *_: webbrowser.open(open_url),
                                 default=True),
                pystray.MenuItem("Reveal log",
                                 lambda *_: webbrowser.open(
                                     paths.log_path().as_uri())),
                pystray.MenuItem("Quit Turnout", lambda *_: stop()),
            ))
        started(icon)             # hand it back before we block
        icon.run()
    except Exception as exc:           # noqa: BLE001
        log.info("tray icon could not start, carrying on without it: %s", exc)


def main(argv: list[str] | None = None) -> int:
    """Start the server, open a browser, and hold the tray."""
    argv = sys.argv[1:] if argv is None else argv
    setup_logging()

    if "--self-test" in argv:
        return self_test()

    # Set before turnout.main is imported: it reads TURNOUT_DESKTOP at import
    # time to decide whether the /app routes exist at all.
    os.environ["TURNOUT_DESKTOP"] = "1"

    port, already = choose_port()
    if already:
        log.info("Turnout is already running on %s; opening a tab", port)
        webbrowser.open(f"http://127.0.0.1:{port}/")
        return 0

    import uvicorn

    from . import main as web
    from .localguard import LocalGuard

    token = web.local_token()
    web.app.add_middleware(LocalGuard, port=port, token=token)
    _write_port(port)

    config = uvicorn.Config(web.app, host="127.0.0.1", port=port,
                            log_level="info", access_log=False)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, name="turnout-server",
                     daemon=True).start()

    for _ in range(100):
        if is_turnout(port):
            break
        time.sleep(0.1)
    else:
        log.error("the server did not come up on port %s", port)
        return 1

    open_url = f"http://127.0.0.1:{port}/app/enter?k={token}"
    if os.environ.get("TURNOUT_NO_BROWSER") != "1":
        webbrowser.open(open_url)

    tray: list = []             # gains the running icon, if any, once it starts
    stop = _make_stop(server, tray)

    # POST /app/quit sets this flag; the tray calls stop() directly.
    threading.Thread(target=lambda: (web.QUIT.wait(), stop()),
                     name="turnout-quit-watch", daemon=True).start()

    if os.environ.get("TURNOUT_NO_TRAY") == "1":
        web.QUIT.wait()
    else:
        _run_tray(open_url, stop, tray.append)
        web.QUIT.set()          # tray closed: bring the server down with it

    server.should_exit = True
    time.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
