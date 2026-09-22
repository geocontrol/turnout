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
import time
from typing import Any

import httpx

from . import __version__ as CURRENT
from . import paths

FEED = "https://github.com/geocontrol/turnout/releases/latest/download/latest.json"

#: How long a fetched answer stands before it is worth asking again. This is
#: a notice saying "there is a newer version, whenever you get to it", not a
#: thing anyone needs to the minute.
INTERVAL = 24 * 60 * 60

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

    c_parts = parts(candidate)
    cur_parts = parts(current)
    max_len = max(len(c_parts), len(cur_parts))
    c_padded = c_parts + (0,) * (max_len - len(c_parts))
    cur_padded = cur_parts + (0,) * (max_len - len(cur_parts))
    return c_padded > cur_padded


def _remember(found: dict | None) -> None:
    """Record what the last successful fetch said, for the banner to read.

    Writes `latest` even when it is nothing, so that the banner stops
    appearing the moment the organiser installs the version it names.
    """
    state = _state()
    state["last_check"] = time.time()
    state["latest"] = found
    _write_state(state)


def check(client: httpx.Client | None = None) -> dict | None:
    """Return details of a newer release, or None.

    Never raises. No network in a hall with one bar of signal is the normal
    case, not an error worth putting in front of an organiser.

    A successful fetch is cached in state.json. Nothing is recorded when the
    fetch fails, so a laptop that was offline at launch asks again next time
    rather than staying quiet for a day.
    """
    if not enabled():
        return None
    owned = client is None
    client = client or httpx.Client(timeout=5.0)
    try:
        data = client.get(FEED, follow_redirects=True).json()
        version = str(data.get("version", ""))
        found = None
        if version and newer(version, CURRENT):
            found = {
                "version": version,
                "url": str(data.get("url", "")),
                "notes": str(data.get("notes", "")),
            }
        try:
            _remember(found)
        except OSError as exc:
            log.info("update check: could not cache the answer: %s", exc)
        return found
    except Exception as exc:  # noqa: BLE001 — deliberately total
        log.info("update check did not complete: %s", exc)
    finally:
        if owned:
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001 — deliberately total
                log.info("update check: could not close client: %s", exc)
    return None


def cached() -> dict | None:
    """The last answer, read from disk. Never touches the network.

    This is the one the request path calls, so rendering any page costs a
    small file read rather than a five-second HTTP timeout.

    Returns None when checking is switched off, when nothing has been
    fetched yet, or when the remembered release is no longer newer than what
    is running — the last of which matters right after an upgrade, where a
    stale banner would otherwise sit there advertising the version the
    organiser is already on.
    """
    if not enabled():
        return None
    latest = _state().get("latest")
    if not isinstance(latest, dict):
        return None
    version = str(latest.get("version", ""))
    if not version or not newer(version, CURRENT):
        return None
    return latest


def due(now: float | None = None) -> bool:
    """Whether the cached answer is old enough to be worth refreshing."""
    if not enabled():
        return False
    last = _state().get("last_check")
    if not isinstance(last, (int, float)):
        return True
    return (time.time() if now is None else now) - last >= INTERVAL


def refresh(client: httpx.Client | None = None) -> dict | None:
    """Fetch if it is time to, and return whatever the banner should show.

    Called from the launcher's own thread once per run — never from a
    request. Never raises, for the same reasons check() does not.
    """
    if not due():
        return cached()
    return check(client)
