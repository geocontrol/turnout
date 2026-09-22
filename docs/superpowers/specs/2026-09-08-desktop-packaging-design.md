# Turnout as a one-click desktop app

**Date:** 2026-09-08
**Status:** Approved design, not yet implemented

Turnout today is `pip install -r requirements.txt` followed by a `uvicorn`
command. That is a reasonable ask of a developer and an unreasonable one of
the people the tool is for — tenants' unions, mutual aid groups, residents'
associations, where the person who ends up running it is a volunteer with a
laptop and no terminal.

This design makes Turnout something you install by double-clicking, and run
by double-clicking, on macOS, Windows and Linux.

## Scope

**In scope.** A signed-capable installer per platform, an icon that launches
the app, a per-user data directory in the right OS location, backup and
restore, a version-check banner, and a release pipeline that builds all of it
from one commit.

**Out of scope, deliberately.** Hosting the public `/l/{slug}` page for the
outside world. Turnout's promise is that the printed URL on a leaflet keeps
working, and a laptop on a domestic connection cannot deliver that. The
desktop app is the **organiser's console**: compose events, hold capacity
across platforms, pull sign-ups back into one list, export. The public-facing
addresses people actually visit are the ones the platforms issue — Eventbrite,
Luma, Ticket Tailor. Hosting the Turnout public page is a separate project
with its own design, and entangling it with packaging would sink both.

**Also out of scope, and named so it is not quietly assumed:** serving on the
local network so an organiser can open the page on their phone at the venue.
It is one line of code and an entirely different security problem.

## Decisions

| Decision | Choice |
|---|---|
| What the app is | Organiser console, local only, bound to `127.0.0.1` |
| Window | The organiser's default browser, plus a tray/menu-bar icon |
| Packaging tool | Briefcase (BeeWare), with PyInstaller as fallback — settled by the step 0 spike |
| Code signing | Pipeline built for it; runs unsigned until certificates are bought |
| Data location | OS per-user application-data directory |
| Handover | Explicit backup and restore producing a dated `.zip` |
| Updates | In-app notice plus Homebrew Cask, winget and Flathub after v1 |

## 1. Runtime shape

### What gets installed

| Platform | Artifact | Where the icon appears |
|---|---|---|
| macOS | `.dmg` containing `Turnout.app` | Applications, Dock |
| Windows | `.msi` | Start menu, optional desktop shortcut |
| Linux | `.deb` and Flatpak | Applications menu via `.desktop` entry |
| Linux (other) | AppImage | Wherever it is saved |

The Flatpak is the one-click Linux path. The AppImage requires the user to
mark the file executable first, so it is offered third, for people on distros
the `.deb` does not fit.

### Launch sequence

1. Resolve the data directory, creating it if absent, and open the SQLite
   file. `db.connect()` already runs `CREATE TABLE IF NOT EXISTS` and the
   column migrations, so a first run needs no setup screen — which preserves
   the README's promise that there is nothing to configure.
2. Attempt to bind port 8100. If it is occupied, probe it. If a running
   Turnout answers, open a browser tab pointing at it and exit — double-
   clicking the icon twice gives a second tab, never a second server. If
   something else holds the port, take an ephemeral one instead. The port in
   use is written to a state file so the tray and subsequent launches find it.
3. Start uvicorn on a background thread: `127.0.0.1` only, one worker, no
   reload. `db.connect()` already passes `check_same_thread=False`, which this
   requires.
4. Poll `/healthz` until it answers, then open the default browser.
5. Start the tray icon **on the main thread**. macOS requires AppKit to own
   the main thread, so this ordering is a constraint rather than a preference.
   Menu: *Open Turnout*, *Back up now*, *Reveal log*, *Quit*.

### The tray is not load-bearing

Linux tray support is unreliable — Wayland and stock GNOME need an
AppIndicator extension many users will not have. If the tray fails to start,
Turnout logs it and carries on serving, and the web UI carries its own *Quit
Turnout* control. No user is left with a running process they cannot see or
stop. The same control exists on every platform, so the quit path is tested
everywhere rather than only where the tray happens to work.

### Localhost is not a security boundary

Binding to `127.0.0.1` keeps other machines out. It does not keep out the
organiser's own browser: any page they visit can issue requests to
`127.0.0.1:8100`. Turnout authenticates HTML forms with cookies, so without
protection a hostile page could drive state-changing POSTs against a database
holding live Eventbrite and Luma API keys and named people's email addresses.

Three defences, implemented as middleware active only in desktop mode:

- **Host check.** Reject any request whose `Host` header is not
  `127.0.0.1` or `localhost` on the port in use. Defeats DNS rebinding.
- **Origin check.** Reject unsafe methods whose `Origin` does not match.
  This is the CSRF defence and the one that does the real work.
- **Per-install token.** A random token stored `0600` in the data directory,
  handed to the browser once through the launch URL, set as an `HttpOnly`
  cookie, then redirected away so it does not persist in browser history.

The existing `TURNOUT_TOKEN` mechanism is unchanged and continues to serve
the hosted deployment. This is additional and desktop-only.

## 2. Application changes

Five new modules, each with a single purpose, and four small edits.

### New

**`turnout/paths.py`** — the only module that answers "where does this live".

| Platform | Data directory |
|---|---|
| macOS | `~/Library/Application Support/Turnout` |
| Windows | `%LOCALAPPDATA%\Turnout` |
| Linux | `$XDG_DATA_HOME/turnout`, else `~/.local/share/turnout` |

`TURNOUT_DB` and a new `TURNOUT_DATA_DIR` override it. When Turnout is not
running frozen, it falls back to `./data` — so the development loop and the
existing `docker-compose.yml`, which sets `TURNOUT_DB=/app/data/turnout.db`,
both keep working with no change.

**`turnout/desktop.py`** — the launcher described above. The only module that
knows Turnout is a desktop app.

**`turnout/localguard.py`** — the Host and Origin middleware. Kept separate
because it is a security control and must be testable on its own.

**`turnout/backup.py`** — backup and restore, using SQLite's online backup
API. Not a file copy: the database runs in WAL mode and is held open by the
server, and copying the `.db` file underneath that produces a subtly corrupt
archive. That is the worst available failure for the one feature whose whole
job is not losing data.

**`turnout/templates/app.html`** — a "This computer" page: back up, restore,
reveal the log, check for updates, quit.

### Edited

- **`turnout/db.py`** — `DB_PATH` asks `paths.py` instead of hardcoding
  `data/turnout.db`. Tests pass `":memory:"` explicitly to `db.connect()`, so
  none of them are affected.
- **`turnout/main.py`** — template directory via `paths.py`; desktop-only
  routes and middleware mounted conditionally.
- **`turnout/templates/base.html`** — update banner, and a link to the "This
  computer" page when running in desktop mode.
- **`README.md`** — install instructions for people who are not developers.

### Backups exclude API keys by default

A backup is meant to be emailed to the next volunteer or dropped into cloud
storage. Including the `credential` table would push live Eventbrite and Luma
keys through channels nobody was treating as sensitive.

So: **backups exclude saved platform access by default.** A checkbox includes
it, carrying a plain warning about what that means. Restore prompts for keys
when the archive does not contain them. The friction lands on the safer side.

Sign-up data — real names and email addresses — is included by definition,
because it is the thing being preserved. The page says so in as many words.

### The update check phones home

Fetching a version file discloses an IP address and a Turnout version to
whoever hosts it. Turnout's users include campaign groups with reason to care.
The check is on by default, disclosed in one line on the "This computer" page,
switchable off, and sends nothing beyond an ordinary HTTP GET — no install
identifier, no telemetry.

### Logging

Rotating log file in the data directory. With no terminal there is nowhere
for a traceback to go, and "send us the log file" is the entire support
story. A *Reveal log* control sits next to the backup buttons.

## 3. Build and release

### Dependencies

`requirements.txt` pins nothing and includes `pytest`, so builds are not
reproducible and would ship the test framework inside the app. Split into a
locked runtime set and a dev set.

The desktop build drops `uvicorn[standard]` for plain `uvicorn`. The
`[standard]` extra pulls uvloop and httptools — compiled extensions, with
uvloop having no Windows support at all — and their benefit is throughput
under load that a single local organiser will never produce. Removing them
takes the most awkward things to freeze out of the bundle at no practical
cost. The Docker image keeps `[standard]`.

### The icon does not exist yet

Briefcase generates `.icns`, `.ico` and the PNG sizes from one source image,
but that image has to be drawn. It is on the critical path for all three
platforms.

### CI

GitHub Actions, one job per target:

- macOS, Apple Silicon and Intel — universal2 if every wheel cooperates, two
  builds otherwise.
- Windows.
- Linux, built on the oldest Ubuntu we intend to support. Building against a
  newer glibc produces packages that will not run on older machines, which is
  exactly the hardware these groups have. Briefcase builds Linux system
  packages in a container, which handles this correctly.

Signing and notarisation steps are present in the workflow from the start,
reading credentials from CI secrets. Those secrets do not exist yet, so the
steps skip and the build emits unsigned artifacts. Buying certificates later
means adding two secrets, not restructuring the pipeline.

Releases publish `.dmg`, `.msi`, `.deb`, Flatpak bundle and AppImage to a
GitHub Release, alongside the `latest.json` the in-app check reads.

### Package managers come after v1

Homebrew Cask, winget and Flathub each require a submission reviewed by a
human, and each expects a stable, already-published app to point at. The
sequence is: direct downloads working and tested, then submit to the three,
then the in-app banner becomes the fallback for people who installed manually
rather than the primary update channel.

## 4. Verification

The 65 existing tests are untouched and stay network-free.

**New unit tests.** Path resolution per platform, monkeypatched. The Host and
Origin guard as an accept/reject matrix. Backup and restore round-tripping
*while the server holds the database open* — the case a file copy fails. Port
selection and already-running detection.

**Smoke test against the frozen artifact**, not the source tree. Launch the
built app headless, assert `/healthz` answers, render a page, generate a QR
SVG, quit cleanly. Nearly every packaging bug is invisible from source and
obvious here: an uncollected template directory, a missing hidden import, a
tray call that hard-fails with no display. A `TURNOUT_NO_TRAY` flag lets CI
run headless.

**One networked test, on the packaged artifact only.** The frozen app must
complete a real HTTPS request. The classic freeze failure is an uncollected
CA bundle, where everything works until an organiser clicks *Check it works*
on their Eventbrite key and gets a TLS error.

**Manual checklist per platform**, because no runner can test double-clicking
an icon, or Gatekeeper, or whether the tray appears under GNOME. On a clean
VM: install, double-click, browser opens, create an event, print the QR page,
quit from the tray, relaunch, data still there, uninstall, note what is left
behind.

**Upgrade test.** Install the previous version, create real data, install the
new one over it, confirm the migration ran and nothing was lost. `_migrate()`
runs `ALTER TABLE` at startup, so this path is live from the second release.

## Sequencing

**Step 0 is a spike, and it gates everything else.** Package Turnout with
both Briefcase and PyInstaller; produce a `.app` and a `.deb` each way; see
which actually launches and serves a page with a threaded uvicorn server and
a tray icon. Briefcase is the recommendation because it turns five installer
formats across three operating systems into configuration and makes signing a
flag. But a threaded HTTP server is not its home turf, and that needs proving
against this app rather than assuming. If it fights us we fall back to
PyInstaller, having spent half a day rather than a week.

The application changes in section 2 are identical either way, so that work
is not blocked on the spike's outcome.

## Risks

| Risk | Response |
|---|---|
| Briefcase cannot package a threaded server cleanly | Step 0 spike; PyInstaller fallback |
| Unsigned Windows build trips antivirus heuristics | Known behaviour of frozen Python executables; signing resolves it, and is the reason the pipeline is built for it now |
| Tray unavailable on the user's Linux desktop | Tray is optional by design; *Quit* also lives in the web UI |
| Backup archive leaks API keys | Credentials excluded by default; inclusion is opt-in and warned |
| CA bundle not collected, breaking all adapters | Explicit networked smoke test on the packaged artifact |
| Old glibc machines cannot run the Linux build | Build on the oldest supported Ubuntu, in a container |
