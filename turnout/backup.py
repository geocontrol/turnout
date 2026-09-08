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
        # Autocommit for this connection: VACUUM below refuses to run inside
        # a transaction, and Python's sqlite3 module otherwise opens one
        # implicitly as soon as the DELETE/UPDATE below touch a row.
        out.isolation_level = None
        conn.backup(out)
        if not include_credentials:
            out.execute("DELETE FROM credential")
            out.execute("UPDATE channel SET credential_id = NULL")
            # VACUUM, so the deleted secrets are not still sitting in free
            # pages for anyone who opens the file with a hex editor.
            out.execute("VACUUM")
    finally:
        out.close()


def write(conn: sqlite3.Connection, dest_dir: Path, include_credentials: bool = False) -> Path:
    """Write a dated backup archive and return its path.

    Args:
        conn: The live server connection. Backed up via SQLite's online
            backup API, not a file copy, so a WAL-mode database with pending
            unflushed commits is still captured correctly.
        dest_dir: Directory to write the archive into. Created if missing.
        include_credentials: Whether to carry saved platform access along.
            Off by default — see the module docstring.

    Returns:
        Path to the written .zip archive.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    archive = dest_dir / f"turnout-backup-{stamp}.zip"
    # A restore takes its own safety backup in the same breath as the
    # caller's, and both can land in the same wall-clock second. Without
    # this, the second write() silently overwrites the first archive's
    # file — exactly the kind of data loss this module exists to prevent.
    n = 1
    while archive.exists():
        archive = dest_dir / f"turnout-backup-{stamp}-{n}.zip"
        n += 1
    staged = dest_dir / f".staging-{archive.stem}.db"
    try:
        _snapshot(conn, staged, include_credentials)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(
                MANIFEST,
                json.dumps(
                    {
                        "format": FORMAT,
                        "written_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "includes_credentials": include_credentials,
                    },
                    indent=2,
                ),
            )
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


def restore(archive: Path, db_file: Path, conn: sqlite3.Connection, backups: Path) -> Path:
    """Replace the live database with the archive's copy.

    Takes a full safety backup of the current data first, including saved
    access, and returns its path — restoring the wrong archive should be
    recoverable, not final. Closes conn: the caller must reopen.

    Args:
        archive: The backup archive to restore from.
        db_file: Path to the live database file to overwrite.
        conn: The live server connection, closed by this call.
        backups: Directory to write the pre-restore safety backup into.

    Returns:
        Path to the safety backup taken before the restore.
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
