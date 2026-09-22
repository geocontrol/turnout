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


# ------------------------------------------------------------------ caching
#
# The banner sits on every organiser page, and update.check() is a
# five-second network timeout when there is no signal. Nothing on the
# request path may call it, so the answer is fetched once at launch and
# read back off disk from then on.


def _feed(version="0.2.0"):
    def handler(request):
        return httpx.Response(
            200, json={"version": version, "url": "https://example.org/get", "notes": "Fixes"}
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_successful_check_is_remembered(monkeypatch, tmp_path):
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("turnout.update.CURRENT", "0.1.0")

    update.check(_feed())

    state = json.loads((tmp_path / "state.json").read_text())
    assert state["latest"]["version"] == "0.2.0"
    assert state["last_check"] > 0


def test_the_cached_answer_needs_no_network(monkeypatch, tmp_path):
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("turnout.update.CURRENT", "0.1.0")
    update.check(_feed())

    def no_network(*a, **kw):
        raise AssertionError("cached() reached for the network")

    monkeypatch.setattr(update.httpx, "Client", no_network)
    assert update.cached()["version"] == "0.2.0"


def test_a_cached_answer_stops_mattering_once_you_are_on_it(monkeypatch, tmp_path):
    """Otherwise the banner advertises the version you just installed until
    the next daily check comes round."""
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("turnout.update.CURRENT", "0.1.0")
    update.check(_feed())

    monkeypatch.setattr("turnout.update.CURRENT", "0.2.0")
    assert update.cached() is None


def test_nothing_is_cached_when_checking_is_off(monkeypatch, tmp_path):
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("turnout.update.CURRENT", "0.1.0")
    update.check(_feed())
    update.set_enabled(False)

    assert update.cached() is None
    assert update.due() is False


def test_a_failed_check_does_not_start_the_clock(monkeypatch, tmp_path):
    """A laptop that was offline at launch asks again next time, rather than
    going quiet for a day on the strength of a failure."""
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))

    def handler(request):
        raise httpx.ConnectError("no route to host")

    update.check(httpx.Client(transport=httpx.MockTransport(handler)))

    written = tmp_path / "state.json"
    assert not written.exists() or "last_check" not in json.loads(written.read_text())
    assert update.due() is True


def test_a_fresh_answer_is_not_fetched_again(monkeypatch, tmp_path):
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("turnout.update.CURRENT", "0.1.0")
    update.check(_feed())

    assert update.due() is False

    def handler(request):
        raise AssertionError("refreshed again inside the interval")

    assert (
        update.refresh(httpx.Client(transport=httpx.MockTransport(handler)))["version"] == "0.2.0"
    )


def test_a_day_old_answer_is_refreshed(monkeypatch, tmp_path):
    monkeypatch.setenv("TURNOUT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("turnout.update.CURRENT", "0.1.0")
    update.check(_feed())

    state = json.loads((tmp_path / "state.json").read_text())
    state["last_check"] -= update.INTERVAL + 1
    (tmp_path / "state.json").write_text(json.dumps(state))

    assert update.due() is True
    assert update.refresh(_feed("0.3.0"))["version"] == "0.3.0"


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
