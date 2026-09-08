# Turnout Desktop Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Turnout from a `pip install` plus a `uvicorn` command into an application that a volunteer with no terminal installs by double-clicking and runs by double-clicking, on macOS, Windows and Linux.

**Architecture:** The FastAPI app is unchanged in substance. A new launcher module starts uvicorn on a background thread bound to `127.0.0.1`, opens the organiser's default browser at it, and holds a tray icon on the main thread. New modules handle per-OS data locations, a request guard that makes localhost safe, and backup/restore. Briefcase builds the installers; the pipeline is wired for code signing before the certificates exist.

**Tech Stack:** Python 3.12, FastAPI, uvicorn (plain, not `[standard]`), Jinja2, SQLite/WAL, httpx, qrcode, pystray, Briefcase (BeeWare), GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-08-desktop-packaging-design.md`

## Global Constraints

- **The 65 existing tests must keep passing, unmodified, and stay network-free.** Run `pytest tests/test_turnout.py` before and after every task.
- **Development and Docker behaviour must not change.** A source checkout keeps using `./data`; `docker-compose.yml` sets `TURNOUT_DB=/app/data/turnout.db` and that must keep winning.
- **Python 3.10 is the floor** — the codebase uses PEP 604 unions (`int | None`). Target 3.12, matching `Dockerfile`.
- **Bind address is `127.0.0.1` only.** Never `0.0.0.0`. Serving on the local network is explicitly out of scope.
- **App id: `io.github.geocontrol.turnout`.** Reverse-DNS of the GitHub org, which is what Flathub requires when you do not own a domain.
- **Update feed: `https://github.com/geocontrol/turnout/releases/latest/download/latest.json`.**
- **Version lives in one place:** `turnout/__init__.py` as `__version__`. `pyproject.toml` and `latest.json` read from it.
- **No `0.0.0.0`, no telemetry, no install identifiers.** The update check sends a plain HTTP GET and nothing else.
- **Style:** Black at line length 100, Ruff, type hints on all functions, Google-style docstrings on public functions. Match the existing prose voice in docstrings — the codebase explains *why*, not *what*.
- **Commits:** Conventional Commits with a task reference, e.g. `feat(desktop): resolve per-OS data directories`.

## Prerequisites that are not code

Two assets block the release and nobody can write them in a task:

1. **An icon.** One square source PNG at 1024×1024. Briefcase generates `.icns`, `.ico` and every PNG size from it. Blocks Tasks 8–10 on all three platforms.
2. **A LICENSE file.** Briefcase asks for one, and Flathub will not accept a submission without it. Blocks Task 10.

Raise both with the project owner at the start rather than discovering them on release day.

---

### Task 0: Spike — prove the packaging tool before committing to it

**This task gates Tasks 8, 9 and 10. It does not gate Tasks 1–7.** Its output is an answer and a working configuration file, not production code. Timebox: half a day. If Briefcase cannot package a threaded uvicorn server with a tray icon, the answer is PyInstaller and the rest of the plan is unaffected in shape.

**Files:**
- Create: `docs/superpowers/spikes/2026-09-08-packaging-tool.md` (findings)
- Create (throwaway, in a scratch directory outside the repo): trial packaging configs

- [ ] **Step 1: Write the minimum thing worth packaging**

In a scratch directory, not the repo:

```python
# spike_app.py — the shape of the real launcher, and nothing else
import threading, time, webbrowser
import uvicorn
from fastapi import FastAPI

app = FastAPI()

@app.get("/healthz")
def healthz():
    return {"ok": True, "app": "turnout"}

def main():
    config = uvicorn.Config(app, host="127.0.0.1", port=8123, log_level="info")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(2)
    webbrowser.open("http://127.0.0.1:8123/healthz")
    import pystray
    from PIL import Image
    icon = pystray.Icon("spike", Image.new("RGB", (64, 64), "black"),
                        menu=pystray.Menu(pystray.MenuItem("Quit", lambda i: i.stop())))
    icon.run()   # must own the main thread on macOS

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Package it with Briefcase and run the result**

```bash
pip install briefcase
briefcase new          # accept defaults; app name "spike"
# copy spike_app.py in as the app's __main__, add uvicorn/fastapi/pystray/pillow to requires
briefcase create
briefcase build
briefcase run
```

Expected: a browser tab opens showing `{"ok":true,"app":"turnout"}` and a tray icon appears. Record what actually happened.

- [ ] **Step 3: Package the same thing with PyInstaller and run the result**

```bash
pip install pyinstaller
pyinstaller --onedir --noconsole spike_app.py
./dist/spike_app/spike_app        # or dist\spike_app\spike_app.exe on Windows
```

Expected: same outcome. Record any `--hidden-import` flags needed to get there — uvicorn resolves its loop and protocol implementations dynamically, so this is the likeliest place to need them.

- [ ] **Step 4: Package on a second operating system**

Repeat Steps 2 and 3 on Linux (or on macOS if you started on Linux). Two platforms is enough to expose a tool that only works on one. Windows can wait for CI.

- [ ] **Step 5: Write up the findings and commit**

Record in `docs/superpowers/spikes/2026-09-08-packaging-tool.md`: which tool worked, on which platforms, what configuration each needed, and the recommendation. If Briefcase worked, paste its final working `pyproject.toml` section into the document — Task 8 starts from it rather than from guesswork.

```bash
git add docs/superpowers/spikes/2026-09-08-packaging-tool.md
git commit -m "docs(S0): spike findings on Briefcase vs PyInstaller"
```

- [ ] **Step 6: Delete the scratch directory**

A spike's output is the answer, not the code.

---

### Task 1: Per-OS data directories

**Files:**
- Create: `turnout/paths.py`
- Modify: `turnout/db.py:18` (the `DB_PATH` constant) and `turnout/db.py:132-141` (`connect`)
- Modify: `turnout/__init__.py` (add `__version__`)
- Test: `tests/test_paths.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `paths.frozen() -> bool`, `paths.data_dir() -> Path`, `paths.ensure_data_dir() -> Path`, `paths.db_path() -> str`, `paths.log_path() -> Path`, `paths.state_path() -> Path`, `paths.token_path() -> Path`, `paths.backup_dir() -> Path`, `paths.resource_dir() -> Path`. Also `turnout.__version__ -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_paths.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from turnout import paths  # noqa: E402


def test_a_source_checkout_keeps_the_local_data_folder(monkeypatch):
    """Development and docker-compose must behave exactly as they did."""
    monkeypatch.delenv("TURNOUT_DATA_DIR", raising=False)
    monkeypatch.setattr(paths, "frozen", lambda: False)
    assert paths.data_dir() == Path("data")


def test_frozen_mac_uses_application_support(monkeypatch):
    monkeypatch.delenv("TURNOUT_DATA_DIR", raising=False)
    monkeypatch.setattr(paths, "frozen", lambda: True)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert paths.data_dir() == Path.home() / "Library" / "Application Support" / "Turnout"


def test_frozen_windows_uses_localappdata(monkeypatch):
    monkeypatch.delenv("TURNOUT_DATA_DIR", raising=False)
    monkeypatch.setattr(paths, "frozen", lambda: True)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", "/tmp/AppData/Local")
    assert paths.data_dir() == Path("/tmp/AppData/Local/Turnout")


def test_frozen_linux_respects_xdg(monkeypatch):
    monkeypatch.delenv("TURNOUT_DATA_DIR", raising=False)
    monkeypatch.setattr(paths, "frozen", lambda: True)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/share")
    assert paths.data_dir() == Path("/tmp/share/turnout")


def test_an_explicit_data_dir_overrides_everything(monkeypatch):
    monkeypatch.setenv("TURNOUT_DATA_DIR", "/tmp/elsewhere")
    monkeypatch.setattr(paths, "frozen", lambda: True)
    assert paths.data_dir() == Path("/tmp/elsewhere")


def test_turnout_db_still_wins_so_docker_is_unaffected(monkeypatch):
    """docker-compose.yml sets TURNOUT_DB. That must keep working untouched."""
    monkeypatch.setenv("TURNOUT_DB", "/app/data/turnout.db")
    monkeypatch.setattr(paths, "frozen", lambda: True)
    assert paths.db_path() == "/app/data/turnout.db"


def test_everything_else_lives_beside_the_database(monkeypatch):
    monkeypatch.setenv("TURNOUT_DATA_DIR", "/tmp/turnout-test")
    for p in (paths.log_path(), paths.state_path(),
              paths.token_path(), paths.backup_dir()):
        assert str(p).startswith("/tmp/turnout-test")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_paths.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'turnout.paths'`

- [ ] **Step 3: Write `turnout/paths.py`**

```python
"""Where Turnout keeps things on disk.

One module answers this, so "the database" means the same file to the
launcher, the backup writer and the server, on three operating systems.

A source checkout deliberately keeps using ./data. The packaged app is the
only thing that goes looking for an OS application-data directory, because
the alternative — a developer's checkout quietly reading a database in
Library/Application Support — is a confusing bug to chase.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Turnout"


def frozen() -> bool:
    """True when running from a packaged build rather than a source checkout.

    Briefcase and PyInstaller both set sys.frozen. TURNOUT_FROZEN exists so
    the packaged-artifact tests can force the packaged behaviour.
    """
    return bool(getattr(sys, "frozen", False)) or os.environ.get("TURNOUT_FROZEN") == "1"


def data_dir() -> Path:
    """The per-user directory holding the database, logs and backups."""
    override = os.environ.get("TURNOUT_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if not frozen():
        return Path("data")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "turnout"


def ensure_data_dir() -> Path:
    """Create the data directory if it is missing, and return it."""
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> str:
    """The SQLite file. TURNOUT_DB wins, so docker-compose.yml is unaffected."""
    return os.environ.get("TURNOUT_DB") or str(data_dir() / "turnout.db")


def log_path() -> Path:
    """The rotating log. With no terminal, this is the whole support story."""
    return data_dir() / "turnout.log"


def state_path() -> Path:
    """Small JSON: the port in use, and whether update checks are wanted."""
    return data_dir() / "state.json"


def token_path() -> Path:
    """The per-install browser token. Written 0600."""
    return data_dir() / "token"


def backup_dir() -> Path:
    """Where 'Back up now' writes its dated archives."""
    return data_dir() / "backups"


def resource_dir() -> Path:
    """Package data — the Jinja2 templates live under here."""
    return Path(__file__).resolve().parent
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_paths.py -v`
Expected: 7 passed

- [ ] **Step 5: Point `db.py` at it**

In `turnout/db.py`, delete the `DB_PATH` constant on line 18 and add the import. Replace:

```python
DB_PATH = os.environ.get("TURNOUT_DB", "data/turnout.db")
```

with:

```python
from . import paths
```

(placed with the other imports; the `import os` above it stays, it is used elsewhere in the module). Then in `connect`, replace `p = path or DB_PATH` with:

```python
def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or paths.db_path()
```

- [ ] **Step 6: Add the version constant**

`turnout/__init__.py`:

```python
"""Turnout — one event, every platform."""

__version__ = "0.1.0"
```

- [ ] **Step 7: Run the whole suite**

Run: `pytest tests/ -v`
Expected: all 65 existing tests plus 7 new ones pass. The existing tests call `db.connect(":memory:")` explicitly, so none of them touch this path.

- [ ] **Step 8: Commit**

```bash
git add turnout/paths.py turnout/db.py turnout/__init__.py tests/test_paths.py
git commit -m "feat(desktop): resolve per-OS data directories

A packaged app cannot keep its database in the working directory: there
is no working directory to speak of when the thing is launched from a
Dock icon. Development and Docker are deliberately unchanged."
```

---

### Task 2: Make localhost safe

**Files:**
- Create: `turnout/localguard.py`
- Test: `tests/test_localguard.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `LocalGuard(app, port: int, token: str | None = None)` — Starlette `BaseHTTPMiddleware` subclass. `allowed_hosts(port: int) -> set[str]`. Module constants `SAFE_METHODS: set[str]`, `PUBLIC_PREFIXES: tuple[str, ...]`.

**Why this exists:** binding to `127.0.0.1` keeps other machines out. It does not keep out the organiser's own browser — any page they visit can aim requests at `127.0.0.1:8100`, and Turnout authenticates HTML forms with cookies. The database holds live Eventbrite and Luma API keys and named people's email addresses.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_localguard.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from turnout.localguard import LocalGuard  # noqa: E402

PORT = 8100
ORIGIN = f"http://127.0.0.1:{PORT}"


def client(token=None):
    """A throwaway app, so the guard is tested on its own behaviour."""
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.post("/ping")
    def ping_post():
        return {"ok": True}

    @app.get("/l/some-event")
    def public():
        return {"public": True}

    @app.get("/app/enter")
    def enter():
        return {"entered": True}

    app.add_middleware(LocalGuard, port=PORT, token=token)
    return TestClient(app, base_url=ORIGIN)


def test_a_normal_local_request_goes_through():
    assert client().get("/ping").status_code == 200


def test_a_rebound_hostname_is_refused():
    """DNS rebinding: a name that resolves to 127.0.0.1 but is not ours."""
    r = client().get("/ping", headers={"host": f"evil.example.com:{PORT}"})
    assert r.status_code == 400


def test_a_same_origin_post_goes_through():
    r = client().post("/ping", headers={"origin": ORIGIN})
    assert r.status_code == 200


def test_a_cross_site_post_is_refused():
    """The CSRF case: a hostile page driving the organiser's own browser."""
    r = client().post("/ping", headers={"origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_sec_fetch_site_is_believed_when_origin_is_absent():
    r = client().post("/ping", headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403


def test_a_post_with_no_origin_headers_is_allowed():
    """Older browsers omit Origin on form posts. Host checking still applies."""
    assert client().post("/ping").status_code == 200


def test_the_token_is_required_when_one_is_set():
    assert client(token="s3cret").get("/ping").status_code == 403


def test_the_token_admits_you_by_cookie():
    c = client(token="s3cret")
    c.cookies.set("turnout_local", "s3cret")
    assert c.get("/ping").status_code == 200


def test_the_public_page_never_needs_the_token():
    """It is the page printed on a leaflet. It does not get to be private."""
    assert client(token="s3cret").get("/l/some-event").status_code == 200


def test_the_entry_route_never_needs_the_token():
    """It is how you get the cookie in the first place."""
    assert client(token="s3cret").get("/app/enter").status_code == 200
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_localguard.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'turnout.localguard'`

- [ ] **Step 3: Write `turnout/localguard.py`**

```python
"""The guard that makes a localhost server safe to run.

Binding to 127.0.0.1 stops other machines reaching Turnout. It does nothing
about the organiser's own browser, which will happily send requests to
127.0.0.1 on behalf of any page they happen to be reading — and Turnout
authenticates HTML forms with cookies, so those requests would arrive
authenticated. The database holds live platform API keys and the names and
email addresses of everyone who signed up.

Three checks, in order: the Host header must be ours, the caller must hold
this install's token, and an unsafe method must not have come from another
site.
"""

from __future__ import annotations

from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

#: Routes that must work without the token. The public link list is printed
#: on leaflets; /app/enter is how a browser is given the token to begin with.
PUBLIC_PREFIXES = ("/l/", "/healthz", "/app/enter")


def allowed_hosts(port: int) -> set[str]:
    """The only Host values this server will answer to."""
    return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


class LocalGuard(BaseHTTPMiddleware):
    """Host, token and cross-site checks for desktop mode."""

    def __init__(self, app, port: int, token: str | None = None) -> None:
        super().__init__(app)
        self.hosts = allowed_hosts(port)
        self.token = token

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.headers.get("host", "") not in self.hosts:
            return PlainTextResponse("bad host", status_code=400)

        path = request.url.path
        public = path.startswith(PUBLIC_PREFIXES)

        if self.token and not public:
            if request.cookies.get("turnout_local") != self.token:
                return PlainTextResponse(
                    "Open Turnout from its icon.", status_code=403)

        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and urlparse(origin).netloc not in self.hosts:
                return PlainTextResponse("cross-site request refused", status_code=403)
            site = request.headers.get("sec-fetch-site")
            if site is not None and site not in {"same-origin", "none"}:
                return PlainTextResponse("cross-site request refused", status_code=403)

        return await call_next(request)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_localguard.py -v`
Expected: 10 passed

- [ ] **Step 5: Run the whole suite**

Run: `pytest tests/ -v`
Expected: everything passes. Nothing is wired into `main.py` yet, so the existing tests are untouched.

- [ ] **Step 6: Commit**

```bash
git add turnout/localguard.py tests/test_localguard.py
git commit -m "feat(desktop): guard the local server against the local browser

Localhost is not a security boundary. Host checking defeats DNS
rebinding, Origin and Sec-Fetch-Site checking defeats CSRF, and a
per-install token keeps other local software out."
```

---

### Task 3: Backup and restore

**Files:**
- Create: `turnout/backup.py`
- Test: `tests/test_backup.py`

**Interfaces:**
- Consumes: `paths.backup_dir()`.
- Produces: `backup.write(conn: sqlite3.Connection, dest_dir: Path, include_credentials: bool = False) -> Path`, `backup.read_manifest(archive: Path) -> dict`, `backup.restore(archive: Path, db_file: Path, conn: sqlite3.Connection, backups: Path) -> Path`. Module constants `FORMAT: int`, `MANIFEST: str`, `DB_ENTRY: str`.

**Why the online backup API:** the database runs in WAL mode with the server holding it open. Copying the `.db` file underneath that captures a state where recent commits live only in the `-wal` sidecar. The archive would look fine and be missing data — the worst available failure for the one feature whose job is not losing data.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_backup.py`:

```python
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from turnout import backup, db  # noqa: E402


def _disk_db(tmp_path):
    """A real file, because that is the case backup has to get right."""
    p = tmp_path / "turnout.db"
    c = db.connect(str(p))
    c.execute(
        """INSERT INTO event (id, slug, title, starts_at, oversell_pct,
           capacity_mode, created_at, updated_at)
           VALUES ('E','e','Public meeting','2026-09-17T19:00',0,'pool',?,?)""",
        (db.now(), db.now()))
    c.execute(
        """INSERT INTO credential (id, kind, label, secret, created_at)
           VALUES ('K','eventbrite','Union account','live-secret-token',?)""",
        (db.now(),))
    c.commit()
    return p, c


def _open_archive_db(archive, into):
    with zipfile.ZipFile(archive) as z, open(into, "wb") as f:
        f.write(z.read(backup.DB_ENTRY))
    return sqlite3.connect(into)


def test_a_backup_leaves_the_api_keys_behind(tmp_path):
    """The archive gets emailed to the next volunteer. Keys must not ride along."""
    _, conn = _disk_db(tmp_path)
    archive = backup.write(conn, tmp_path / "backups")
    c = _open_archive_db(archive, tmp_path / "check.db")
    assert c.execute("SELECT count(*) FROM credential").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM event").fetchone()[0] == 1


def test_the_keys_can_be_included_deliberately(tmp_path):
    _, conn = _disk_db(tmp_path)
    archive = backup.write(conn, tmp_path / "backups", include_credentials=True)
    c = _open_archive_db(archive, tmp_path / "check.db")
    assert c.execute("SELECT secret FROM credential").fetchone()[0] == "live-secret-token"


def test_the_manifest_says_whether_keys_are_inside(tmp_path):
    _, conn = _disk_db(tmp_path)
    plain = backup.write(conn, tmp_path / "backups")
    assert backup.read_manifest(plain)["includes_credentials"] is False


def test_a_backup_of_a_live_database_is_complete(tmp_path):
    """WAL mode with the server's connection still open is the normal case,
    and exactly the case a plain file copy gets wrong."""
    p, conn = _disk_db(tmp_path)
    server = db.connect(str(p))
    server.execute(
        """INSERT INTO event (id, slug, title, starts_at, oversell_pct,
           capacity_mode, created_at, updated_at)
           VALUES ('F','f','Second meeting','2026-09-18T19:00',0,'pool',?,?)""",
        (db.now(), db.now()))
    server.commit()

    archive = backup.write(conn, tmp_path / "backups")
    c = _open_archive_db(archive, tmp_path / "check.db")
    assert c.execute("SELECT count(*) FROM event").fetchone()[0] == 2


def test_restore_puts_the_data_back(tmp_path):
    p, conn = _disk_db(tmp_path)
    archive = backup.write(conn, tmp_path / "backups", include_credentials=True)
    conn.execute("DELETE FROM event")
    conn.commit()

    backup.restore(archive, p, conn, tmp_path / "backups")

    after = db.connect(str(p))
    assert after.execute("SELECT title FROM event").fetchone()[0] == "Public meeting"


def test_restore_keeps_a_copy_of_what_it_replaced(tmp_path):
    """Restoring the wrong archive must not be the end of the story."""
    p, conn = _disk_db(tmp_path)
    archive = backup.write(conn, tmp_path / "backups", include_credentials=True)
    conn.execute("UPDATE event SET title = 'Renamed since'")
    conn.commit()

    safety = backup.restore(archive, p, conn, tmp_path / "backups")

    c = _open_archive_db(safety, tmp_path / "safety.db")
    assert c.execute("SELECT title FROM event").fetchone()[0] == "Renamed since"


def test_restore_refuses_a_zip_that_is_not_ours(tmp_path):
    p, conn = _disk_db(tmp_path)
    junk = tmp_path / "holiday-photos.zip"
    with zipfile.ZipFile(junk, "w") as z:
        z.writestr("beach.jpg", b"not a database")

    with pytest.raises(ValueError):
        backup.restore(junk, p, conn, tmp_path / "backups")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_backup.py -v`
Expected: FAIL, `ImportError: cannot import name 'backup' from 'turnout'`

- [ ] **Step 3: Write `turnout/backup.py`**

```python
"""Backup and restore, for the day the volunteer with the laptop moves on.

The database runs in WAL mode and the server holds it open. SQLite's online
backup API takes a consistent snapshot of exactly that situation; copying the
file does not, and produces an archive that looks fine and is missing the most
recent commits.

Saved platform access is left out by default. A backup is a thing people email
to each other and drop in cloud storage, and a group should not discover
afterwards that they posted a live Eventbrite key through it.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

FORMAT = 1
MANIFEST = "turnout-backup.json"
DB_ENTRY = "turnout.db"


def _snapshot(conn: sqlite3.Connection, dest: Path, include_credentials: bool) -> None:
    """Write a consistent copy of the live database to dest."""
    out = sqlite3.connect(dest)
    try:
        with out:
            conn.backup(out)
        if not include_credentials:
            with out:
                out.execute("DELETE FROM credential")
                out.execute("UPDATE channel SET credential_id = NULL")
            # VACUUM, so the deleted secrets are not still sitting in free
            # pages for anyone who opens the file with a hex editor.
            out.execute("VACUUM")
    finally:
        out.close()


def write(conn: sqlite3.Connection, dest_dir: Path,
          include_credentials: bool = False) -> Path:
    """Write a dated backup archive and return its path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    archive = dest_dir / f"turnout-backup-{stamp}.zip"
    staged = dest_dir / f".staging-{stamp}.db"
    try:
        _snapshot(conn, staged, include_credentials)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(MANIFEST, json.dumps({
                "format": FORMAT,
                "written_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "includes_credentials": include_credentials,
            }, indent=2))
            z.write(staged, DB_ENTRY)
    finally:
        staged.unlink(missing_ok=True)
    return archive


def read_manifest(archive: Path) -> dict:
    """Return the archive's manifest, or raise if it is not a Turnout backup."""
    try:
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            if MANIFEST not in names or DB_ENTRY not in names:
                raise ValueError("not a Turnout backup")
            return json.loads(z.read(MANIFEST))
    except zipfile.BadZipFile as exc:
        raise ValueError("not a Turnout backup") from exc


def restore(archive: Path, db_file: Path, conn: sqlite3.Connection,
            backups: Path) -> Path:
    """Replace the live database with the archive's copy.

    Takes a full safety backup of the current data first, including saved
    access, and returns its path — restoring the wrong archive should be
    recoverable, not final. Closes conn: the caller must reopen.
    """
    read_manifest(archive)
    safety = write(conn, backups, include_credentials=True)
    conn.close()
    with zipfile.ZipFile(archive) as z, open(db_file, "wb") as f:
        shutil.copyfileobj(z.open(DB_ENTRY), f)
    # The old WAL and shared-memory sidecars describe a database that no
    # longer exists. Leaving them would corrupt the one just restored.
    for suffix in ("-wal", "-shm"):
        db_file.with_name(db_file.name + suffix).unlink(missing_ok=True)
    return safety
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_backup.py -v`
Expected: 7 passed

- [ ] **Step 5: Run the whole suite**

Run: `pytest tests/ -v`

- [ ] **Step 6: Commit**

```bash
git add turnout/backup.py tests/test_backup.py
git commit -m "feat(desktop): backup and restore via SQLite's online backup API

A file copy of a WAL database held open by the server captures a state
missing its most recent commits. Saved platform access is excluded by
default, because backups get emailed."
```

---

### Task 4: The update check

**Files:**
- Create: `turnout/update.py`
- Test: `tests/test_update.py`

**Interfaces:**
- Consumes: `paths.state_path()`, `turnout.__version__`.
- Produces: `update.newer(candidate: str, current: str) -> bool`, `update.enabled() -> bool`, `update.set_enabled(on: bool) -> None`, `update.check(client: httpx.Client | None = None) -> dict | None` returning `{"version": str, "url": str, "notes": str}` when an update exists and `None` otherwise. Module constant `FEED: str`.

**Note:** the spec's module list did not name this file; it is split out rather than added to `main.py` because it does network I/O and holds a user setting, and both want testing on their own.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_update.py`:

```python
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from turnout import update  # noqa: E402


def test_version_comparison_is_numeric_not_alphabetical():
    """0.10.0 is newer than 0.9.0, which string comparison gets wrong."""
    assert update.newer("0.10.0", "0.9.0") is True
    assert update.newer("0.9.0", "0.10.0") is False
    assert update.newer("0.1.0", "0.1.0") is False
    assert update.newer("1.0.0", "0.99.99") is True


def test_a_newer_release_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(update, "__dict__", update.__dict__)  # no-op, keeps lint quiet
    monkeypatch.setattr("turnout.update.CURRENT", "0.1.0")

    def handler(request):
        return httpx.Response(200, json={
            "version": "0.2.0",
            "url": "https://github.com/geocontrol/turnout/releases/latest",
            "notes": "Ticket Tailor fixes",
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    found = update.check(client)
    assert found["version"] == "0.2.0"


def test_the_same_version_is_not_an_update(monkeypatch, tmp_path):
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("turnout.update.CURRENT", "0.2.0")

    def handler(request):
        return httpx.Response(200, json={"version": "0.2.0", "url": "x", "notes": ""})

    assert update.check(httpx.Client(transport=httpx.MockTransport(handler))) is None


def test_a_failed_check_is_silent(monkeypatch, tmp_path):
    """No network in a church hall is normal. It is not an error to show."""
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))

    def handler(request):
        raise httpx.ConnectError("no route to host")

    assert update.check(httpx.Client(transport=httpx.MockTransport(handler))) is None


def test_checking_can_be_switched_off(monkeypatch, tmp_path):
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))
    assert update.enabled() is True
    update.set_enabled(False)
    assert update.enabled() is False
    assert json.loads((tmp_path / "state.json").read_text())["update_check"] is False


def test_nothing_is_fetched_when_it_is_switched_off(monkeypatch, tmp_path):
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))
    update.set_enabled(False)

    def handler(request):
        raise AssertionError("the network was touched after being told not to")

    assert update.check(httpx.Client(transport=httpx.MockTransport(handler))) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_update.py -v`
Expected: FAIL, `ImportError: cannot import name 'update' from 'turnout'`

- [ ] **Step 3: Write `turnout/update.py`**

```python
"""Telling an organiser a new version exists, and nothing else.

Fetching this file discloses an IP address and a version number to whoever
hosts it. Turnout's users include campaign groups with reason to care about
that, so the check is one plain HTTP GET carrying no install identifier and
no telemetry, it is described in a sentence on the 'This computer' page, and
it can be switched off.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from . import __version__ as CURRENT
from . import paths

FEED = "https://github.com/geocontrol/turnout/releases/latest/download/latest.json"

log = logging.getLogger("turnout.update")


def _state() -> dict[str, Any]:
    try:
        return json.loads(paths.state_path().read_text())
    except (OSError, ValueError):
        return {}


def _write_state(state: dict[str, Any]) -> None:
    paths.ensure_data_dir()
    paths.state_path().write_text(json.dumps(state, indent=2))


def enabled() -> bool:
    """Whether to check at all. On unless the organiser said otherwise."""
    return bool(_state().get("update_check", True))


def set_enabled(on: bool) -> None:
    """Record the organiser's answer."""
    state = _state()
    state["update_check"] = bool(on)
    _write_state(state)


def newer(candidate: str, current: str) -> bool:
    """Compare dotted numeric versions. 0.10.0 beats 0.9.0."""
    def parts(v: str) -> tuple[int, ...]:
        out = []
        for piece in v.split("."):
            digits = "".join(c for c in piece if c.isdigit())
            out.append(int(digits) if digits else 0)
        return tuple(out)
    return parts(candidate) > parts(current)


def check(client: httpx.Client | None = None) -> dict | None:
    """Return details of a newer release, or None.

    Never raises. No network in a hall with one bar of signal is the normal
    case, not an error worth putting in front of an organiser.
    """
    if not enabled():
        return None
    owned = client is None
    client = client or httpx.Client(timeout=5.0)
    try:
        data = client.get(FEED, follow_redirects=True).json()
        version = str(data.get("version", ""))
        if version and newer(version, CURRENT):
            return {"version": version,
                    "url": str(data.get("url", "")),
                    "notes": str(data.get("notes", ""))}
    except Exception as exc:           # noqa: BLE001 — deliberately total
        log.info("update check did not complete: %s", exc)
    finally:
        if owned:
            client.close()
    return None
```

- [ ] **Step 4: Remove the no-op line from the test**

Delete the `monkeypatch.setattr(update, "__dict__", update.__dict__)` line from `test_a_newer_release_is_reported` — it was scaffolding and does nothing.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_update.py -v`
Expected: 6 passed

- [ ] **Step 6: Run the whole suite and commit**

```bash
pytest tests/ -v
git add turnout/update.py tests/test_update.py
git commit -m "feat(desktop): check for a newer release, and say so

One plain GET, no identifiers, switchable off, silent on failure."
```

---

### Task 5: Desktop mode in the web app

**Files:**
- Create: `turnout/templates/app.html`
- Modify: `turnout/main.py` — imports and module constants near line 40; `healthz` at line 437
- Test: `tests/test_desktop_routes.py`

**Interfaces:**
- Consumes: `paths`, `backup`, `update`, `turnout.__version__`.
- Produces: `main.DESKTOP: bool`, `main.QUIT: threading.Event`, `main.local_token() -> str | None`, and the routes `GET /app`, `GET /app/enter`, `POST /app/backup`, `POST /app/restore`, `POST /app/quit`, `GET /app/log`. `healthz` now returns `{"ok": True, "app": "turnout", "version": <str>}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_desktop_routes.py`:

```python
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from turnout import db  # noqa: E402


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    """The app in desktop mode, on a real database file in tmp_path."""
    monkeypatch.setenv("TURNOUT_DESKTOP", "1")
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TURNOUT_DB", raising=False)
    from turnout import main
    importlib.reload(main)
    conn = db.connect(str(tmp_path / "turnout.db"))
    conn.execute(
        """INSERT INTO event (id, slug, title, starts_at, oversell_pct,
           capacity_mode, created_at, updated_at)
           VALUES ('E','e','Public meeting','2026-09-17T19:00',0,'pool',?,?)""",
        (db.now(), db.now()))
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
           VALUES ('K','eventbrite','Union','live-secret-token',?)""", (db.now(),))
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_desktop_routes.py -v`
Expected: FAIL — `/app` returns 404.

- [ ] **Step 3: Add the desktop block to `turnout/main.py`**

Add to the imports at the top of the file:

```python
import secrets
import threading
from pathlib import Path

from fastapi import File, UploadFile

from . import __version__, backup, paths, update
```

Change `templates = Jinja2Templates(...)` to use `paths.resource_dir()`:

```python
templates = Jinja2Templates(directory=str(paths.resource_dir() / "templates"))
```

(The existing `HERE = os.path.dirname(__file__)` line can go; check with `grep -n "HERE" turnout/main.py` that nothing else uses it before deleting.)

After `TOKEN = os.environ.get("TURNOUT_TOKEN")`, add:

```python
#: Desktop mode: launched from an icon rather than a terminal or a container.
DESKTOP = os.environ.get("TURNOUT_DESKTOP") == "1"

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
        p.write_text(secrets.token_urlsafe(32))
        p.chmod(0o600)
    return p.read_text().strip()
```

Replace the existing `healthz` at line 437:

```python
@app.get("/healthz")
def healthz():
    """Also how a second launch recognises an already-running Turnout."""
    return {"ok": True, "app": "turnout", "version": __version__}
```

- [ ] **Step 4: Add the desktop routes at the end of `turnout/main.py`**

```python
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
        response.set_cookie("turnout_local", expected, httponly=True,
                            samesite="lax", path="/")
        return response

    @app.get("/app", response_class=HTMLResponse)
    def app_page(request: Request, said: str = "", _=Depends(require_organiser)):
        return templates.TemplateResponse(request, "app.html", {
            "version": __version__,
            "data_dir": str(paths.data_dir()),
            "backups": sorted((p.name for p in paths.backup_dir().glob("*.zip")),
                              reverse=True)[:10],
            "update_check": update.enabled(),
            "available": update.check(),
            "said": said,
        })

    @app.post("/app/backup")
    def app_backup(include_credentials: str = Form(""),
                   _=Depends(require_organiser)):
        archive = backup.write(get_conn(), paths.backup_dir(),
                               include_credentials=bool(include_credentials))
        return RedirectResponse(f"/app?said=Backed+up+to+{archive.name}",
                                status_code=303)

    @app.post("/app/restore")
    async def app_restore(file: UploadFile = File(...),
                          _=Depends(require_organiser)):
        global _conn
        paths.ensure_data_dir()
        staged = paths.backup_dir() / "incoming.zip"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(await file.read())
        try:
            safety = backup.restore(staged, Path(paths.db_path()),
                                    get_conn(), paths.backup_dir())
        except ValueError:
            return RedirectResponse(
                "/app?said=That+file+is+not+a+Turnout+backup", status_code=303)
        finally:
            staged.unlink(missing_ok=True)
        _conn = None       # reopened on the next request, against the new file
        return RedirectResponse(
            f"/app?said=Restored.+Your+previous+data+is+in+{safety.name}",
            status_code=303)

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
        return templates.TemplateResponse(request, "app.html", {
            "stopped": True, "version": __version__,
            "data_dir": str(paths.data_dir()), "backups": [],
            "update_check": update.enabled(), "available": None, "said": "",
        })
```

- [ ] **Step 5: Write `turnout/templates/app.html`**

```html
{% extends "base.html" %}
{% block title %}This computer · Turnout{% endblock %}
{% block content %}
<div class="wrap">

{% if stopped %}
  <div class="banner info"><div class="grow">
    <b>Turnout has stopped.</b> You can close this tab. Open it again from
    the Turnout icon.
  </div></div>
{% else %}

  {% if said %}<div class="banner info"><div class="grow">{{ said }}</div></div>{% endif %}

  {% if available %}
  <div class="banner warn"><div class="grow">
    <b>Version {{ available.version }} is available.</b> {{ available.notes }}
  </div><a class="btn sm" href="{{ available.url }}">Get it</a></div>
  {% endif %}

  <h1>This computer</h1>

  <div class="card">
    <h2>Your data</h2>
    <p>Everything Turnout knows is in one folder on this computer:</p>
    <p class="mono">{{ data_dir }}</p>
    <p class="hint">Nothing is sent anywhere. If this laptop is lost, so is
      the data — which is why the backup below matters.</p>
  </div>

  <div class="card">
    <h2>Back up</h2>
    <form method="post" action="/app/backup">
      <label class="f"><span>
        <input type="checkbox" name="include_credentials" value="1"
               style="width:auto"> Include saved platform access
      </span></label>
      <p class="hint"><b>Leave that unticked unless you know you need it.</b>
        Ticking it puts your live Eventbrite, Luma and Ticket Tailor keys
        inside the backup file. Anyone who gets the file can then act as your
        group on those platforms.</p>
      <p class="hint">The backup always contains the names and email addresses
        of everyone who has signed up. Treat it accordingly.</p>
      <button type="submit">Back up now</button>
    </form>
    {% if backups %}
      <div class="caps">
        {% for b in backups %}<div><span class="mono">{{ b }}</span></div>{% endfor %}
      </div>
    {% endif %}
  </div>

  <div class="card">
    <h2>Restore</h2>
    <form method="post" action="/app/restore" enctype="multipart/form-data">
      <label class="f"><span>Backup file</span>
        <input type="file" name="file" accept=".zip" required></label>
      <p class="hint">This replaces everything currently in Turnout. A copy of
        what is here now is saved first, so a mistake can be undone.</p>
      <button type="submit" class="hot">Restore from this file</button>
    </form>
  </div>

  <div class="card">
    <h2>Updates</h2>
    <form method="post" action="/app/update-check">
      <label class="f"><span>
        <input type="checkbox" name="on" value="1" style="width:auto"
               {% if update_check %}checked{% endif %}> Check for new versions
      </span></label>
      <p class="hint">Turnout asks GitHub whether a newer version exists. That
        request tells GitHub your IP address and which version you are running,
        and nothing else — no account, no identifier, no record of your events.
        Untick it and Turnout never contacts anyone on its own.</p>
      <button type="submit" class="ghost sm">Save</button>
    </form>
    <p class="hint">You are running version {{ version }}.</p>
  </div>

  <div class="card">
    <h2>If something goes wrong</h2>
    <p><a href="/app/log">Open the log</a> — copy it into an email if you are
      asking for help.</p>
  </div>

  <div class="card">
    <h2>Stop Turnout</h2>
    <form method="post" action="/app/quit">
      <button type="submit" class="ghost">Quit Turnout</button>
    </form>
    <p class="hint">Closing this tab leaves Turnout running in the background.
      This stops it properly.</p>
  </div>

{% endif %}
</div>
{% endblock %}
```

- [ ] **Step 6: Add the link to `turnout/templates/base.html`**

In the `hdr_right` area, inside the `<div class="in">` block, before `{% block hdr_right %}`:

```html
  {% if desktop %}<a href="/app" class="tag">This computer</a>{% endif %}
```

and make `desktop` available to every template by adding to the `templates.env.globals.update(...)` call in `main.py`:

```python
    desktop=DESKTOP,
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `pytest tests/test_desktop_routes.py -v`
Expected: 8 passed

- [ ] **Step 8: Run the whole suite**

Run: `pytest tests/ -v`
Expected: all pass. The existing tests do not set `TURNOUT_DESKTOP`, so `DESKTOP` is False and none of these routes exist for them.

- [ ] **Step 9: Commit**

```bash
git add turnout/main.py turnout/templates/app.html turnout/templates/base.html tests/test_desktop_routes.py
git commit -m "feat(desktop): a 'This computer' page for backup, updates and quit

Backup warns in plain words about what a ticked box would put in the
file. Quit exists in the web UI as well as the tray, so a user is never
left with a running process they cannot stop."
```

---

### Task 6: The launcher

**Files:**
- Create: `turnout/desktop.py`
- Test: `tests/test_desktop_launcher.py`
- Create: `turnout/resources/icon.png` (from the prerequisite asset; 1024×1024)

**Interfaces:**
- Consumes: `main.app`, `main.QUIT`, `main.local_token()`, `paths`, `localguard.LocalGuard`.
- Produces: `desktop.choose_port(preferred: int = 8100) -> tuple[int, bool]`, `desktop.is_turnout(port: int) -> bool`, `desktop.setup_logging() -> None`, `desktop.self_test() -> int`, `desktop.main(argv: list[str] | None = None) -> int`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_desktop_launcher.py`:

```python
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

from turnout import desktop  # noqa: E402


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_desktop_launcher.py -v`
Expected: FAIL, `ImportError: cannot import name 'desktop' from 'turnout'`

- [ ] **Step 3: Write `turnout/desktop.py`**

```python
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

import httpx

from . import __version__, paths

PREFERRED_PORT = 8100
log = logging.getLogger("turnout.desktop")


def setup_logging() -> None:
    """Log to a rotating file. There is no terminal to print to."""
    paths.ensure_data_dir()
    handler = logging.handlers.RotatingFileHandler(
        paths.log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


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


def _run_tray(open_url: str, stop) -> None:
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

    def stop() -> None:
        server.should_exit = True

    # POST /app/quit sets this flag; the tray calls stop() directly.
    threading.Thread(target=lambda: (web.QUIT.wait(), stop()),
                     name="turnout-quit-watch", daemon=True).start()

    if os.environ.get("TURNOUT_NO_TRAY") == "1":
        web.QUIT.wait()
    else:
        _run_tray(open_url, stop)
        web.QUIT.set()          # tray closed: bring the server down with it

    server.should_exit = True
    time.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Put the icon in place**

```bash
mkdir -p turnout/resources
# copy the 1024x1024 source PNG from the prerequisites to:
#   turnout/resources/icon.png
```

- [ ] **Step 5: Add the runtime dependencies**

Append to `requirements.in` (created in Task 7 — if running out of order, add to `requirements.txt` for now):

```
pystray>=0.19
pillow>=10.0
```

Install them: `.venv/bin/pip install "pystray>=0.19" "pillow>=10.0"`

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_desktop_launcher.py -v`
Expected: 5 passed

- [ ] **Step 7: Run it for real**

```bash
TURNOUT_FROZEN=1 TURNOUT_DATA_DIR=/tmp/turnout-try .venv/bin/python -m turnout.desktop
```

Expected: a browser opens on `/app/enter?k=…`, redirects to `/`, and the address bar shows `http://127.0.0.1:8100/` with no token in it. A tray icon appears (macOS/Windows; may not on Linux, which is expected and fine). *Quit Turnout* on the "This computer" page stops the process.

Then check the self-test:

```bash
.venv/bin/python -m turnout.desktop --self-test
```

Expected: four `[OK]` lines and exit status 0.

- [ ] **Step 8: Run the whole suite and commit**

```bash
pytest tests/ -v
git add turnout/desktop.py turnout/resources/icon.png tests/test_desktop_launcher.py requirements.in
git commit -m "feat(desktop): the launcher behind the icon

Server on a background thread, tray on the main one because macOS
insists. A second double-click opens a tab rather than a second server.
--self-test exists so 'it will not start' has an answer."
```

---

### Task 7: Split and pin the dependencies

**Files:**
- Create: `requirements.in`, `requirements-dev.txt`, `requirements-docker.txt`
- Modify: `requirements.txt` (becomes generated), `Dockerfile:3-4`
- Modify: `README.md` (the Quick start block)

**Interfaces:**
- Consumes: nothing.
- Produces: a locked `requirements.txt` that Task 8 feeds to Briefcase.

**Why:** `requirements.txt` currently pins nothing, so no two builds are the same, and it includes `pytest`, which would be shipped inside the app. The desktop build also drops `uvicorn[standard]`: the extra pulls in uvloop and httptools, uvloop has no Windows support at all, and their benefit is throughput under load that one local organiser will never produce.

- [ ] **Step 1: Create `requirements.in`**

```
# Runtime dependencies, loosely specified. requirements.txt is generated from
# this with pip-compile — edit this file, never that one.
fastapi>=0.115
uvicorn>=0.30
jinja2>=3.1
httpx>=0.27
qrcode>=7.4
python-multipart>=0.0.9
pystray>=0.19
pillow>=10.0
```

- [ ] **Step 2: Create `requirements-dev.txt`**

```
-r requirements.txt
pytest>=8.0
pip-tools>=7.4
briefcase>=0.3.17
ruff
black
```

- [ ] **Step 3: Create `requirements-docker.txt`**

```
# The container is not the desktop build: it serves many requests and runs on
# Linux only, so the compiled event loop is worth having there.
-r requirements.txt
uvicorn[standard]>=0.30
```

- [ ] **Step 4: Generate the lock**

```bash
.venv/bin/pip install pip-tools
.venv/bin/pip-compile --generate-hashes -o requirements.txt requirements.in
```

Expected: `requirements.txt` is rewritten with exact versions and hashes, and a header saying it was generated.

- [ ] **Step 5: Point the Dockerfile at the container requirements**

`Dockerfile`, replacing lines 3–4:

```dockerfile
COPY requirements.txt requirements-docker.txt ./
RUN pip install --no-cache-dir -r requirements-docker.txt
```

- [ ] **Step 6: Verify the container still builds and serves**

```bash
docker build -t turnout-check .
docker run --rm -p 8199:8100 -e TURNOUT_DB=/tmp/t.db turnout-check &
sleep 5 && curl -s http://127.0.0.1:8199/healthz
```

Expected: `{"ok":true,"app":"turnout","version":"0.1.0"}`. Stop the container afterwards.

- [ ] **Step 7: Update the README Quick start**

Replace the Quick start block with:

```markdown
## Quick start

**If you just want to use it:** download the installer for your computer from
[Releases](https://github.com/geocontrol/turnout/releases), install it, and
open Turnout from your applications. There is nothing to configure — no
accounts, no API tokens.

**If you are working on it:**

```bash
python3 -m venv .venv && source .venv/bin/activate   # Python 3.10 or newer
pip install -r requirements-dev.txt
uvicorn turnout.main:app --reload --port 8100
```

Open <http://localhost:8100>. In a source checkout the database is
`data/turnout.db`, as it always was; the packaged app keeps it in your
computer's application-data folder instead.
```

- [ ] **Step 8: Run the whole suite and commit**

```bash
pytest tests/ -v
git add requirements.in requirements.txt requirements-dev.txt requirements-docker.txt Dockerfile README.md
git commit -m "build: pin runtime deps, split out dev and container extras

An unpinned requirements.txt makes every build different and shipped
pytest inside the app. The desktop build drops uvicorn[standard]:
uvloop does not build on Windows and buys nothing for one local user."
```

---

### Task 8: Briefcase packaging

**Depends on Task 0.** Start from the configuration the spike proved. If the spike chose PyInstaller, build a `turnout.spec` instead and keep every other step — the commands change, the deliverable does not.

**Files:**
- Modify: `pyproject.toml` (create if the spike did not)
- Create: `LICENSE` (from the prerequisite)

**Interfaces:**
- Consumes: `requirements.txt` from Task 7, `turnout/desktop.py:main` from Task 6, `turnout/resources/icon.png`.
- Produces: buildable app bundles. Entry point is `turnout.desktop:main`.

- [ ] **Step 1: Write the Briefcase configuration**

`pyproject.toml` — reconcile against whatever the Task 0 spike proved actually works with the installed Briefcase version; that document is the authority where this differs.

```toml
[project]
name = "turnout"
dynamic = ["version"]
description = "One event, every platform."
requires-python = ">=3.10"

[tool.setuptools.dynamic]
version = {attr = "turnout.__version__"}

[tool.briefcase]
project_name = "Turnout"
bundle = "io.github.geocontrol"
version = "0.1.0"
author = "Mark Simpkins"
author_email = "mark@maahee.com"
url = "https://github.com/geocontrol/turnout"
license.file = "LICENSE"

[tool.briefcase.app.turnout]
formal_name = "Turnout"
description = "One event, every platform. Publish once, gather the sign-ups back into one list."
long_description = """Turnout publishes one event to several platforms, pulls
the sign-ups back into a single list, and holds a capacity across all of them.
Built for campaigning and community groups.
"""
icon = "turnout/resources/icon"
sources = ["turnout"]
test_sources = ["tests"]
requires = [
    "fastapi>=0.115",
    "uvicorn>=0.30",
    "jinja2>=3.1",
    "httpx>=0.27",
    "qrcode>=7.4",
    "python-multipart>=0.0.9",
    "pystray>=0.19",
    "pillow>=10.0",
]
test_requires = ["pytest>=8.0"]

[tool.briefcase.app.turnout.macOS]
universal_build = true
requires = ["std-nslog~=1.0.3"]

[tool.briefcase.app.turnout.windows]
requires = []

[tool.briefcase.app.turnout.linux]
requires = []

[tool.briefcase.app.turnout.linux.system.debian]
system_requires = ["libgirepository1.0-dev", "libcairo2-dev"]
system_runtime_requires = ["gir1.2-gtk-3.0", "gir1.2-ayatanaappindicator3-0.1"]

[tool.briefcase.app.turnout.linux.flatpak]
flatpak_runtime = "org.freedesktop.Platform"
flatpak_runtime_version = "23.08"
flatpak_sdk = "org.freedesktop.Sdk"
```

The AppIndicator runtime requirement is what gives the tray a chance on Linux. It is a *runtime* requirement, not a build one, and the launcher already survives its absence.

- [ ] **Step 2: Confirm the entry point**

Briefcase runs the app's `__main__`. Create `turnout/__main__.py`:

```python
"""Entry point for the packaged app and for `python -m turnout`."""

from .desktop import main

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Add the LICENSE file**

Copy in the licence chosen by the project owner (see Prerequisites). Briefcase reads it, and Flathub will reject a submission without one.

- [ ] **Step 4: Build and run on your own machine**

```bash
.venv/bin/briefcase create
.venv/bin/briefcase build
.venv/bin/briefcase run
```

Expected: Turnout launches, a browser opens on the organiser index, and the data directory is the OS application-data one — not `./data`. Verify:

```bash
ls ~/Library/Application\ Support/Turnout/          # macOS
ls ~/.local/share/turnout/                          # Linux
```

Expected: `turnout.db`, `turnout.log`, `state.json`, `token`.

- [ ] **Step 5: Package an installer**

```bash
.venv/bin/briefcase package --adhoc-sign      # macOS, unsigned for now
```

Expected: a `.dmg` under `dist/`. Install from it into Applications and launch from the Dock icon.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml turnout/__main__.py LICENSE
git commit -m "build: package Turnout with Briefcase

Signing is configuration, not restructuring: the same config takes an
identity when there is one to take."
```

---

### Task 9: Test the packaged artifact, not the source tree

**Files:**
- Create: `tests/test_packaged.py`

**Interfaces:**
- Consumes: an installed or built app, located by the `TURNOUT_PACKAGED_APP` environment variable.
- Produces: nothing other artefacts depend on.

**Why:** almost every packaging bug is invisible from source and obvious here — a template directory that was not collected, a dynamic import that was not followed, a CA bundle that was left out. These tests are skipped unless `TURNOUT_PACKAGED_APP` is set, so they cost nothing in normal development.

- [ ] **Step 1: Write the tests**

Create `tests/test_packaged.py`:

```python
"""Tests that run against a built app, not against the source tree.

Set TURNOUT_PACKAGED_APP to the executable inside the built bundle, e.g.

    macOS   dist/Turnout.app/Contents/MacOS/Turnout
    Linux   build/turnout/…/bin/Turnout
    Windows build\\turnout\\…\\Turnout.exe

Unlike the rest of the suite, one of these is allowed to touch the network.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

APP = os.environ.get("TURNOUT_PACKAGED_APP")

pytestmark = pytest.mark.skipif(
    not APP, reason="set TURNOUT_PACKAGED_APP to a built app to run these")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running(tmp_path):
    """The packaged app, serving, with no tray and no browser."""
    env = {**os.environ,
           "TURNOUT_DATA_DIR": str(tmp_path),
           "TURNOUT_NO_TRAY": "1",
           "TURNOUT_NO_BROWSER": "1"}
    proc = subprocess.Popen([APP], env=env)
    port = None
    for _ in range(150):
        state = tmp_path / "state.json"
        if state.exists():
            try:
                port = json.loads(state.read_text()).get("port")
            except ValueError:
                port = None
        if port:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz",
                             timeout=1).json().get("app") == "turnout":
                    break
            except Exception:      # noqa: BLE001
                pass
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("the packaged app never started serving")
    token = (tmp_path / "token").read_text().strip()
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}",
                          headers={"host": f"127.0.0.1:{port}"},
                          cookies={"turnout_local": token})
    try:
        yield client, tmp_path
    finally:
        client.close()
        proc.terminate()
        proc.wait(timeout=20)


def test_the_self_test_passes_in_the_bundle():
    """Covers TLS: a build without its CA bundle fails every adapter."""
    done = subprocess.run([APP, "--self-test"], capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_templates_were_collected(running):
    """A Jinja2 directory left out of the bundle breaks every page."""
    client, _ = running
    page = client.get("/").text
    assert 'name="title"' in page
    assert "TURN" in page


def test_an_event_can_be_made_and_read_back(running):
    client, _ = running
    r = client.post("/events", data={"title": "Public meeting",
                                     "starts_at": "2026-10-01T19:00"},
                    headers={"origin": str(client.base_url)},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "Public meeting" in client.get("/").text


def test_qr_codes_render_in_the_bundle(running):
    client, _ = running
    client.post("/events", data={"title": "Poster test",
                                 "starts_at": "2026-10-01T19:00"},
                headers={"origin": str(client.base_url)}, follow_redirects=False)
    slug = None
    for row in client.get("/").text.split('"'):
        if row.startswith("/l/"):
            slug = row.split("/")[2]
            break
    assert slug, "no public link found on the index page"
    r = client.get(f"/l/{slug}/qr.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg")


def test_the_database_lands_in_the_data_directory(running):
    _, data_dir = running
    assert (Path(data_dir) / "turnout.db").exists()
```

- [ ] **Step 2: Run them against your build**

```bash
TURNOUT_PACKAGED_APP="$(pwd)/dist/Turnout.app/Contents/MacOS/Turnout" \
  .venv/bin/pytest tests/test_packaged.py -v
```

Expected: 5 passed. If `test_the_self_test_passes_in_the_bundle` fails on the HTTPS line, the bundle is missing its CA certificates — fix that in the packaging config before going further, because every platform adapter depends on it.

- [ ] **Step 3: Confirm they skip cleanly without a build**

Run: `pytest tests/ -v`
Expected: the packaged tests report as skipped; everything else passes.

- [ ] **Step 4: Commit**

```bash
git add tests/test_packaged.py
git commit -m "test: exercise the built app, not the source tree

Uncollected templates, unfollowed dynamic imports and a missing CA
bundle are all invisible from source and all obvious here."
```

---

### Task 10: The release pipeline

**Files:**
- Create: `.github/workflows/build.yml`
- Create: `.github/workflows/release.yml`
- Create: `scripts/make_latest_json.py`

**Interfaces:**
- Consumes: `pyproject.toml`, `turnout/__version__`, `tests/test_packaged.py`.
- Produces: release artifacts and `latest.json` at the URL `turnout/update.py` reads.

- [ ] **Step 1: Write the per-push build workflow**

`.github/workflows/build.yml`:

```yaml
name: build

on:
  push:
    branches: [main]
  pull_request:

jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements-dev.txt
      - run: pytest tests/ -v

  package:
    needs: tests
    strategy:
      fail-fast: false
      matrix:
        include:
          - { os: macos-14,     target: macOS,   artifact: dmg }
          - { os: windows-2022, target: windows, artifact: msi }
          # Oldest supported Ubuntu: building on a newer glibc produces
          # packages that will not run on the hardware these groups have.
          - { os: ubuntu-22.04, target: linux,   artifact: deb }
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Linux build dependencies
        if: matrix.target == 'linux'
        run: |
          sudo apt-get update
          sudo apt-get install -y libgirepository1.0-dev libcairo2-dev \
            gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
      - run: pip install -r requirements-dev.txt
      - run: briefcase create ${{ matrix.target }}
      - run: briefcase build ${{ matrix.target }}
      - name: Test the built app
        shell: bash
        env:
          TURNOUT_NO_TRAY: "1"
          TURNOUT_NO_BROWSER: "1"
        run: |
          # Briefcase's output path differs per platform. Print the tree the
          # first time this runs on each OS and pin the real path here —
          # this glob is a starting point, not a guarantee.
          find build -maxdepth 6 -name 'Turnout*'
          APP=$(find build -type f -name 'Turnout' -o -type f -name 'Turnout.exe' | head -1)
          test -n "$APP" || { echo "no built app found"; exit 1; }
          echo "testing $APP"
          TURNOUT_PACKAGED_APP="$APP" pytest tests/test_packaged.py -v
      - run: briefcase package ${{ matrix.target }} --adhoc-sign
        if: matrix.target == 'macOS'
      - run: briefcase package ${{ matrix.target }}
        if: matrix.target != 'macOS'
      - uses: actions/upload-artifact@v4
        with:
          name: turnout-${{ matrix.artifact }}
          path: dist/*
```

- [ ] **Step 2: Write the `latest.json` generator**

`scripts/make_latest_json.py`:

```python
"""Write the file turnout/update.py fetches, from the version in the package."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from turnout import __version__  # noqa: E402

notes = sys.argv[1] if len(sys.argv) > 1 else ""
Path("latest.json").write_text(json.dumps({
    "version": __version__,
    "url": "https://github.com/geocontrol/turnout/releases/latest",
    "notes": notes,
}, indent=2))
print(f"latest.json written for {__version__}")
```

- [ ] **Step 3: Write the release workflow, with signing wired but dormant**

`.github/workflows/release.yml`:

```yaml
name: release

on:
  push:
    tags: ["v*"]

jobs:
  package:
    strategy:
      fail-fast: false
      matrix:
        include:
          - { os: macos-14,     target: macOS }
          - { os: windows-2022, target: windows }
          - { os: ubuntu-22.04, target: linux }
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Linux build dependencies
        if: matrix.target == 'linux'
        run: |
          sudo apt-get update
          sudo apt-get install -y libgirepository1.0-dev libcairo2-dev \
            gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
      - run: pip install -r requirements-dev.txt
      - run: briefcase create ${{ matrix.target }}
      - run: briefcase build ${{ matrix.target }}

      # ---- Signing. The secrets do not exist yet, so these steps skip and
      # ---- the build emits unsigned artifacts. Adding the secrets is the
      # ---- whole of "turn signing on"; nothing here changes shape.
      - name: Import Apple signing certificate
        if: matrix.target == 'macOS' && env.MACOS_CERTIFICATE != ''
        env:
          MACOS_CERTIFICATE: ${{ secrets.MACOS_CERTIFICATE }}
          MACOS_CERTIFICATE_PASSWORD: ${{ secrets.MACOS_CERTIFICATE_PASSWORD }}
        run: |
          echo "$MACOS_CERTIFICATE" | base64 --decode > cert.p12
          security create-keychain -p runner build.keychain
          security default-keychain -s build.keychain
          security unlock-keychain -p runner build.keychain
          security import cert.p12 -k build.keychain \
            -P "$MACOS_CERTIFICATE_PASSWORD" -T /usr/bin/codesign
          security set-key-partition-list -S apple-tool:,apple: \
            -s -k runner build.keychain
          rm cert.p12

      - name: Package (macOS, signed and notarised)
        if: matrix.target == 'macOS' && env.MACOS_SIGNING_IDENTITY != ''
        env:
          MACOS_SIGNING_IDENTITY: ${{ secrets.MACOS_SIGNING_IDENTITY }}
        run: |
          briefcase package macOS \
            --identity "$MACOS_SIGNING_IDENTITY" \
            --no-input
      - name: Package (macOS, unsigned)
        if: matrix.target == 'macOS' && env.MACOS_SIGNING_IDENTITY == ''
        env:
          MACOS_SIGNING_IDENTITY: ${{ secrets.MACOS_SIGNING_IDENTITY }}
        run: briefcase package macOS --adhoc-sign

      - name: Package (Windows)
        if: matrix.target == 'windows'
        run: briefcase package windows
      - name: Sign the Windows installer
        if: matrix.target == 'windows' && env.WINDOWS_CERTIFICATE != ''
        env:
          WINDOWS_CERTIFICATE: ${{ secrets.WINDOWS_CERTIFICATE }}
          WINDOWS_CERTIFICATE_PASSWORD: ${{ secrets.WINDOWS_CERTIFICATE_PASSWORD }}
        shell: pwsh
        run: |
          [IO.File]::WriteAllBytes("cert.pfx",
            [Convert]::FromBase64String($env:WINDOWS_CERTIFICATE))
          Get-ChildItem dist/*.msi | ForEach-Object {
            & signtool sign /f cert.pfx /p $env:WINDOWS_CERTIFICATE_PASSWORD `
              /tr http://timestamp.digicert.com /td sha256 /fd sha256 $_.FullName
          }
          Remove-Item cert.pfx

      - name: Package (Linux)
        if: matrix.target == 'linux'
        run: briefcase package linux

      - uses: actions/upload-artifact@v4
        with:
          name: turnout-${{ matrix.target }}
          path: dist/*

  publish:
    needs: package
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: actions/download-artifact@v4
        with:
          path: artifacts
      - run: python scripts/make_latest_json.py "${{ github.event.head_commit.message }}"
      - uses: softprops/action-gh-release@v2
        with:
          files: |
            artifacts/**/*
            latest.json
```

- [ ] **Step 4: Check both workflows parse**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/build.yml')); \
           yaml.safe_load(open('.github/workflows/release.yml'))" && echo "YAML OK"
```

Expected: `YAML OK`. A workflow that does not parse fails silently on GitHub — it simply never runs, with no error against the commit.

- [ ] **Step 5: Push and watch the build workflow**

```bash
git add .github/workflows/build.yml .github/workflows/release.yml scripts/make_latest_json.py
git commit -m "ci: build and test packaged apps on three platforms

Signing steps are present and skip while their secrets are absent, so
buying certificates later means adding two secrets rather than
rewriting the pipeline."
git push
```

Expected: the `build` workflow runs `tests` then three `package` jobs. Every `package` job must reach and pass `Test the built app`. Fix failures before Task 11 — this is where Windows and Linux problems surface for the first time.

- [ ] **Step 6: Cut a test release**

```bash
git tag v0.1.0 && git push origin v0.1.0
```

Expected: a GitHub Release with a `.dmg`, an `.msi`, a `.deb` and `latest.json` attached. Download the `.dmg` on a Mac and confirm what an unsigned build actually does — the wording of the Gatekeeper message is what Task 11 has to document.

---

### Task 11: Documentation and the manual checklist

**Files:**
- Create: `docs/installing.md`
- Create: `docs/release-checklist.md`
- Modify: `README.md`

- [ ] **Step 1: Write `docs/installing.md`**

Written for a volunteer, not a developer. It must cover, per platform: where to download, what to double-click, exactly what the Gatekeeper or SmartScreen warning says while the build is unsigned and the precise clicks to get past it, where the data is kept, and how to back up. Take the wording from the actual dialogs seen in Task 10 Step 6 — paraphrasing a security warning is how people get stuck.

- [ ] **Step 2: Write `docs/release-checklist.md`**

```markdown
# Release checklist

CI covers what CI can. Nobody can automate double-clicking an icon, a
Gatekeeper dialog, or whether a tray icon appears under GNOME.

## Before tagging

- [ ] `__version__` in `turnout/__init__.py` bumped, and `version` in `pyproject.toml` matches
- [ ] `pytest tests/ -v` passes locally
- [ ] The `build` workflow is green on `main`, including `Test the built app` on all three platforms

## On a clean machine or VM, for each of macOS, Windows and Linux

- [ ] Download the installer from the Release page
- [ ] Note exactly what the OS warns, and check `docs/installing.md` still says the same thing
- [ ] Install it
- [ ] Launch from the icon — a browser opens, and there is no token in the address bar
- [ ] Create an event with a capacity and one manual channel
- [ ] Open the public page, and print the QR page from the browser
- [ ] Back up, without ticking the credentials box
- [ ] Confirm the archive holds the event and no credential rows
- [ ] Quit from the tray if there is one, otherwise from the web page
- [ ] Relaunch — the event is still there
- [ ] Double-click the icon a second time while it is running — a tab opens, not a second server
- [ ] Uninstall, and note what is left behind

## Upgrade, which is the one people skip

- [ ] Install the *previous* release
- [ ] Create an event, a channel and a sign-up
- [ ] Install the new release over the top, without uninstalling
- [ ] Launch: the event, channel and sign-up are all still there
- [ ] Check `turnout.log` for migration errors

`db._migrate()` runs `ALTER TABLE` at startup, so this path is live from the
second release onwards and gets more load-bearing with every schema change.

## After the release

- [ ] `latest.json` is attached to the release, and its `version` is the new one
- [ ] An older install shows the update banner within a day
```

- [ ] **Step 3: Add a Status note to the README**

Under `## Status`, add:

```markdown
- The desktop builds are unsigned. macOS and Windows will both warn on first
  launch; `docs/installing.md` says what the warnings look like and what to
  click. Signing is wired into the release pipeline and switches on when
  there are certificates to switch it on with.
- Homebrew Cask, winget and Flathub submissions come after the first stable
  release, not with it. Until then the in-app banner is the update path.
```

- [ ] **Step 4: Commit**

```bash
git add docs/installing.md docs/release-checklist.md README.md
git commit -m "docs: installing Turnout, and the manual release checklist

The upgrade test is on the list because it is the one everybody skips
and the one that loses a group's data."
```

---

## What this plan does not do

Named so they are not mistaken for oversights:

- **Hosting the public page.** Out of scope in the spec; its own project.
- **Serving on the local network.** One line, entirely different security problem.
- **Encrypting credentials at rest** with the OS keychain. The backup exclusion is the mitigation this project ships; the `credential` table's own comment already anticipates being stored differently later.
- **Homebrew, winget and Flathub submissions.** After v1, per the spec.
- **Silent auto-update.** Rejected during brainstorming.
