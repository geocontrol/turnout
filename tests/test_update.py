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


def test_version_comparison_handles_different_segment_counts():
    """Versions with different segment counts must compare correctly."""
    assert update.newer("0.2.0", "0.2") is False
    assert update.newer("0.2", "0.2.0") is False
    assert update.newer("1.0.1", "1.0") is True


def test_a_newer_release_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("turnout.update.CURRENT", "0.1.0")

    def handler(request):
        return httpx.Response(
            200,
            json={
                "version": "0.2.0",
                "url": "https://github.com/geocontrol/turnout/releases/latest",
                "notes": "Ticket Tailor fixes",
            },
        )

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


def test_check_never_raises_even_on_close_failure(monkeypatch, tmp_path):
    """The 'never raises' contract holds even if client.close() fails."""
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))

    class BrokenTransport(httpx.BaseTransport):
        def handle_request(self, request):
            return httpx.Response(200, json={"version": "0.2.0", "url": "x", "notes": ""})

    class BrokenClient(httpx.Client):
        def close(self):
            raise RuntimeError("close is broken")

    client = BrokenClient(transport=BrokenTransport())
    result = update.check(client)
    # Should return the update dict without raising
    assert result is not None
    assert result["version"] == "0.2.0"
