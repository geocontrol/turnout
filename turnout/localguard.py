"""The guard that makes a localhost server safe to run.

Binding to 127.0.0.1 stops other machines reaching Turnout. It does nothing
about the organiser's own browser, which will happily send requests to
127.0.0.1 on behalf of any page they happen to be reading — and Turnout
authenticates HTML forms with cookies, so those requests would arrive
authenticated. The database holds live platform API keys and the names and
email addresses of everyone who signed up.

Three checks, in order: the Host header must be ours, the caller must hold
this install's token, and an unsafe method must not have come from another
site.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

#: Exact paths that must work without the token.
PUBLIC_PATHS = ("/healthz", "/app/enter")

#: Genuine prefixes: the public link list is /l/{slug} and /l/{slug}/qr.svg.
PUBLIC_PREFIXES = ("/l/",)


def allowed_hosts(port: int) -> set[str]:
    """The only Host values this server will answer to."""
    return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


class LocalGuard(BaseHTTPMiddleware):
    """Host, token and cross-site checks for desktop mode."""

    def __init__(self, app, port: int, token: str | None = None) -> None:
        super().__init__(app)
        self.hosts = allowed_hosts(port)
        self.token = token

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.headers.get("host", "") not in self.hosts:
            return PlainTextResponse("bad host", status_code=400)

        path = request.url.path
        public = path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)

        if self.token and not public:
            supplied = request.cookies.get("turnout_local") or ""
            if not secrets.compare_digest(supplied, self.token):
                return PlainTextResponse(
                    "Open Turnout from its icon.", status_code=403)

        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and urlparse(origin).netloc not in self.hosts:
                return PlainTextResponse("cross-site request refused", status_code=403)
            site = request.headers.get("sec-fetch-site")
            if site is not None and site not in {"same-origin", "none"}:
                return PlainTextResponse("cross-site request refused", status_code=403)

        return await call_next(request)
