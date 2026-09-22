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
    for p in (paths.log_path(), paths.state_path(), paths.token_path(), paths.backup_dir()):
        assert str(p).startswith("/tmp/turnout-test")
