import json
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
        (db.now(), db.now()),
    )
    c.execute(
        """INSERT INTO credential (id, kind, label, secret, created_at)
           VALUES ('K','eventbrite','Union account','live-secret-token',?)""",
        (db.now(),),
    )
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
        (db.now(), db.now()),
    )
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


def test_restore_refuses_an_archive_with_a_corrupt_manifest(tmp_path):
    """The manifest is what stands between a foreign file and an overwrite.
    A truncated one must fail closed, the same as a non-zip."""
    junk = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(junk, "w") as z:
        z.writestr(backup.MANIFEST, "{not valid json")
        z.writestr(backup.DB_ENTRY, b"whatever")

    with pytest.raises(ValueError):
        backup.read_manifest(junk)


def test_a_backup_from_a_newer_turnout_is_refused(tmp_path):
    """FORMAT is written into every manifest and was never read back.

    A format-2 archive would have restored silently into a build that reads
    format 1, and whatever it could not understand would simply be gone.
    """
    p, conn = _disk_db(tmp_path)
    archive = backup.write(conn, tmp_path / "backups")
    with zipfile.ZipFile(archive) as z:
        manifest, dbbytes = json.loads(z.read(backup.MANIFEST)), z.read(backup.DB_ENTRY)
    manifest["format"] = backup.FORMAT + 1
    future = tmp_path / "from-the-future.zip"
    with zipfile.ZipFile(future, "w") as z:
        z.writestr(backup.MANIFEST, json.dumps(manifest))
        z.writestr(backup.DB_ENTRY, dbbytes)

    with pytest.raises(backup.Unsupported):
        backup.restore(future, p, conn, tmp_path / "backups")
    # Still a ValueError, so callers that only know about one failure are fine.
    assert issubclass(backup.Unsupported, ValueError)


def test_a_database_member_that_is_not_sqlite_is_refused(tmp_path):
    """The manifest can be perfect and the payload still be anything at all.

    Restoring it leaves db.connect() raising on every request afterwards,
    which is a much worse outcome than refusing the file.
    """
    junk = tmp_path / "wrong-inside.zip"
    with zipfile.ZipFile(junk, "w") as z:
        z.writestr(backup.MANIFEST, json.dumps({"format": backup.FORMAT}))
        z.writestr(backup.DB_ENTRY, b"PK\x03\x04 or a jpeg or anything else")

    with pytest.raises(ValueError):
        backup.read_manifest(junk)


def test_a_real_backup_still_reads_back(tmp_path):
    """The three new checks must not have closed the door on ourselves."""
    _, conn = _disk_db(tmp_path)
    archive = backup.write(conn, tmp_path / "backups")
    assert backup.read_manifest(archive)["format"] == backup.FORMAT


def test_restore_leaves_the_database_untouched_if_the_copy_fails(tmp_path, monkeypatch):
    """A crash mid-restore is not a reason the volunteer should lose the
    database that was live before they started."""
    p, conn = _disk_db(tmp_path)
    archive = backup.write(conn, tmp_path / "backups", include_credentials=True)

    # restore() also calls write() internally for its safety backup, which
    # uses shutil.copyfileobj too (zipfile writes entries with it). Only
    # sabotage the copy into our own ".restoring" temp file, so the safety
    # backup that comes before it still succeeds.
    real_copyfileobj = backup.shutil.copyfileobj

    def flaky(fsrc, fdst, *args, **kwargs):
        if getattr(fdst, "name", "").endswith(".restoring"):
            fdst.write(b"partial")
            raise OSError("simulated crash mid-copy")
        return real_copyfileobj(fsrc, fdst, *args, **kwargs)

    monkeypatch.setattr(backup.shutil, "copyfileobj", flaky)

    with pytest.raises(OSError):
        backup.restore(archive, p, conn, tmp_path / "backups")

    after = db.connect(str(p))
    assert after.execute("SELECT title FROM event").fetchone()[0] == "Public meeting"
    assert not p.with_name(p.name + ".restoring").exists()
