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

    c_parts = parts(candidate)
    cur_parts = parts(current)
    max_len = max(len(c_parts), len(cur_parts))
    c_padded = c_parts + (0,) * (max_len - len(c_parts))
    cur_padded = cur_parts + (0,) * (max_len - len(cur_parts))
    return c_padded > cur_padded


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
            try:
                client.close()
            except Exception as exc:   # noqa: BLE001 — deliberately total
                log.info("update check: could not close client: %s", exc)
    return None
