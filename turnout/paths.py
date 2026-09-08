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
