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
