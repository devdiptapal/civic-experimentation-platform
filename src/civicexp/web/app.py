"""The web application's core: requests, routing, sign-in, and the HTTP server.

Built on :mod:`http.server` so that running the platform adds nothing to an
agency's software inventory beyond this package. It is meant to run behind
the agency's own TLS-terminating reverse proxy (see
``docs/Deployment-Guide.md``), which handles certificates and rate
limiting; this layer handles everything that depends on knowing who the
person is.

Security properties enforced here, for every response:

* **Sign-in is required** for every page except the sign-in page itself,
  the static assets, and the health check. Sessions are server-side, so
  signing out or disabling an account takes effect immediately.
* **Every state change is a POST carrying a per-session CSRF token**, and
  the session cookie is ``HttpOnly`` and ``SameSite=Strict``.
* **The API takes bearer keys, never cookies**, so a browser session cannot
  be used to post events, and a collection key cannot open a page.
* **Strict headers**: a Content-Security-Policy that allows no inline script
  or style, no framing, no referrer, and no caching of pages that may hold
  pilot information.
* **Bounded input**: request bodies are size-limited before they are read.
"""

from __future__ import annotations

import email.parser
import email.policy
import hmac
import json
import re
import sys
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .. import __version__
from ..auth import ApiKey, LoginThrottle, Session, SessionStore, User
from ..errors import CivicExpError
from ..workspace import Workspace
from .html import Markup, page

__all__ = ["App", "HTTPError", "Request", "Response", "make_server"]

#: Largest request body accepted at all (a spreadsheet upload).
MAX_BODY = 10 * 1024 * 1024
#: Largest body accepted by the collection API.
MAX_API_BODY = 1024 * 1024

STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "civicexp-client.js": "text/javascript; charset=utf-8",
    "icon.svg": "image/svg+xml",
}

SECURITY_HEADERS = (
    (
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), interest-cohort=()"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
)

SESSION_COOKIE = "cx_session"

_ERROR_STATUS = {
    "AUTH_FORBIDDEN": 403,
    "WORKSPACE_NOT_FOUND": 404,
    "LIBRARY_DUPLICATE": 409,
}


def status_for(code: str) -> int:
    if code in _ERROR_STATUS:
        return _ERROR_STATUS[code]
    if code.startswith("LIFECYCLE_"):
        return 409
    if code.startswith(("EVENT_", "CONFIG_", "PRIVACY_", "ASSIGNMENT_", "CSV_", "LIBRARY_")):
        return 422
    if code.startswith("AUTH_"):
        return 400
    return 400


class HTTPError(Exception):
    def __init__(self, status: int, message: str, *, code: str = "HTTP_ERROR") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: Any
    body: bytes
    client: str
    params: dict[str, str] = field(default_factory=dict)
    user: User | None = None
    session: Session | None = None
    api_key: ApiKey | None = None
    _form: dict[str, Any] | None = None
    _files: dict[str, tuple[str, bytes]] | None = None

    def arg(self, name: str, default: str = "") -> str:
        values = self.query.get(name)
        return values[0] if values else default

    @property
    def cookies(self) -> dict[str, str]:
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except Exception:  # malformed cookie header: treat as none
            return {}
        return {k: v.value for k, v in jar.items()}

    def _parse_body(self) -> None:
        if self._form is not None:
            return
        ctype = self.headers.get("Content-Type", "")
        self._form, self._files = {}, {}
        if ctype.startswith("application/x-www-form-urlencoded"):
            parsed = parse_qs(self.body.decode("utf-8", "replace"), keep_blank_values=True)
            self._form = {k: v if len(v) > 1 else v[0] for k, v in parsed.items()}
        elif ctype.startswith("multipart/form-data"):
            message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                f"Content-Type: {ctype}\r\n\r\n".encode() + self.body
            )
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if not name:
                    continue
                payload = part.get_payload(decode=True) or b""
                filename = part.get_filename()
                if filename is not None:
                    self._files[name] = (filename, payload)
                else:
                    self._form[name] = payload.decode("utf-8", "replace")

    def form(self) -> dict[str, Any]:
        self._parse_body()
        assert self._form is not None
        return self._form

    def value(self, name: str, default: str = "") -> str:
        raw = self.form().get(name, default)
        return raw[-1] if isinstance(raw, list) else str(raw)

    def values(self, name: str) -> list[str]:
        raw = self.form().get(name, [])
        return raw if isinstance(raw, list) else [raw]

    def files(self) -> dict[str, tuple[str, bytes]]:
        self._parse_body()
        assert self._files is not None
        return self._files

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPError(400, f"request body is not valid JSON: {exc}",
                            code="API_BAD_JSON") from exc


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def html(cls, markup: str, status: int = 200) -> Response:
        return cls(status, markup.encode("utf-8"))

    @classmethod
    def json(cls, data: Any, status: int = 200) -> Response:
        return cls(status, (json.dumps(data, indent=2, default=str) + "\n").encode("utf-8"),
                   "application/json; charset=utf-8")

    @classmethod
    def redirect(cls, location: str, status: int = 303) -> Response:
        if (
            not location.startswith("/")
            or location.startswith("//")
            or "\\" in location
            or any(ord(c) < 32 for c in location)
        ):
            location = "/"  # never redirect off-site, never split a header
        return cls(status, b"", "text/plain; charset=utf-8", [("Location", location)])

    @classmethod
    def download(cls, data: bytes | str, filename: str, content_type: str) -> Response:
        if isinstance(data, str):
            data = data.encode("utf-8")
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
        return cls(200, data, content_type,
                   [("Content-Disposition", f'attachment; filename="{safe}"')])


Handler = Callable[["App", Request], Response]


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler
    auth: str  # "session", "api", or "public"


class App:
    """The platform's web application for one workspace."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        secure_cookies: bool = False,
        session_ttl: int = 8 * 3600,
        trust_proxy: bool = False,
    ) -> None:
        self.workspace = workspace
        self.secure_cookies = secure_cookies
        self.trust_proxy = trust_proxy
        self.sessions = SessionStore(ttl_seconds=session_ttl)
        self.throttle = LoginThrottle()
        self.routes: list[Route] = []
        self.version = __version__
        self._flashes: dict[str, list[tuple[str, str]]] = {}
        self._flash_lock = threading.Lock()
        from . import api, pages  # registers routes

        pages.register(self)
        api.register(self)

    # -- routing -------------------------------------------------------------

    def route(self, method: str, pattern: str, *, auth: str = "session") -> Callable[[Handler], Handler]:
        compiled = re.compile("^" + pattern + "$")

        def decorator(handler: Handler) -> Handler:
            self.routes.append(Route(method, compiled, handler, auth))
            return handler

        return decorator

    def handle(self, request: Request) -> Response:
        try:
            response = self._dispatch(request)
        except HTTPError as exc:
            response = self._error(request, exc.status, exc.message, exc.code)
        except CivicExpError as exc:
            response = self._error(request, status_for(exc.code), exc.message, exc.code)
        except Exception:  # pragma: no cover - last resort
            traceback.print_exc(file=sys.stderr)
            response = self._error(request, 500, "Something went wrong on the server. The "
                                   "error has been logged.", "SERVER_ERROR")
        for name, value in SECURITY_HEADERS:
            response.headers.append((name, value))
        if not request.path.startswith("/static/"):
            response.headers.append(("Cache-Control", "no-store"))
        return response

    def _dispatch(self, request: Request) -> Response:
        allowed = []
        for route in self.routes:
            match = route.pattern.match(request.path)
            if not match:
                continue
            if route.method != request.method:
                allowed.append(route.method)
                continue
            request.params = {k: unquote(v) for k, v in match.groupdict().items()}
            if route.auth == "session":
                guard = self._require_session(request)
                if guard is not None:
                    return guard
            elif route.auth == "api":
                self._require_key(request)
            return route.handler(self, request)
        if allowed:
            raise HTTPError(405, "That method is not supported here.", code="HTTP_METHOD")
        raise HTTPError(404, "There is nothing at this address.", code="HTTP_NOT_FOUND")

    # -- authentication ----------------------------------------------------

    def client_key(self, request: Request) -> str:
        if self.trust_proxy:
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client

    def _require_session(self, request: Request) -> Response | None:
        session = self.sessions.get(request.cookies.get(SESSION_COOKIE))
        user = self.workspace.users.get(session.username) if session else None
        if session is None or user is None or user.disabled:
            if request.method == "GET":
                return Response.redirect(f"/login?next={request.path}")
            raise HTTPError(401, "Your session has ended. Sign in again.", code="AUTH_SESSION")
        request.session, request.user = session, user
        if request.method == "POST":
            token = request.value("_csrf")
            if not token or not hmac.compare_digest(token, session.csrf_token):
                raise HTTPError(
                    403,
                    "This form has expired. Go back, reload the page, and try again.",
                    code="AUTH_CSRF",
                )
        return None

    def _require_key(self, request: Request) -> None:
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        key = self.workspace.keys.verify(token)
        if key is None:
            raise HTTPError(401, "A valid API key is required (Authorization: Bearer ...).",
                            code="API_UNAUTHORIZED")
        request.api_key = key

    def sign_in(self, user: User, next_path: str = "/") -> Response:
        session = self.sessions.create(user.username)
        response = Response.redirect(next_path or "/")
        response.headers.append(("Set-Cookie", self._cookie(session.session_id)))
        return response

    def _cookie(self, value: str, *, expire: bool = False) -> str:
        parts = [f"{SESSION_COOKIE}={value}", "Path=/", "HttpOnly", "SameSite=Strict"]
        if self.secure_cookies:
            parts.append("Secure")
        if expire:
            parts.append("Max-Age=0")
        return "; ".join(parts)

    def sign_out(self, request: Request) -> Response:
        if request.session is not None:
            self.sessions.destroy(request.session.session_id)
        response = Response.redirect("/login")
        response.headers.append(("Set-Cookie", self._cookie("", expire=True)))
        return response

    # -- page helpers --------------------------------------------------------

    def flash(self, request: Request, kind: str, message: str) -> None:
        if request.session is None:
            return
        with self._flash_lock:
            self._flashes.setdefault(request.session.session_id, []).append((kind, message))

    def _take_flashes(self, request: Request) -> list[tuple[str, str]]:
        if request.session is None:
            return []
        with self._flash_lock:
            return self._flashes.pop(request.session.session_id, [])

    def page(self, request: Request, title: str, body: Markup, *, section: str = "",
             status: int = 200) -> Response:
        settings = self.workspace.settings
        markup = page(
            title, body,
            user=request.user,
            workspace_name=str(settings.get("name", "")),
            jurisdiction=str(settings.get("jurisdiction", "")),
            section=section,
            flashes=self._take_flashes(request),
            csrf=request.session.csrf_token if request.session else "",
            version=self.version,
            synthetic=bool(settings.get("synthetic_data")),
        )
        return Response.html(markup, status)

    def _error(self, request: Request, status: int, message: str, code: str) -> Response:
        if request.path.startswith("/api/") or request.path == "/healthz":
            return Response.json({"error": {"code": code, "message": message}}, status)
        from .html import render

        body = render(
            '<div class="error-page"><h1>{title}</h1><p>{message}</p>'
            '<p><a href="/">Back to the inbox</a></p></div>',
            title=HTTPStatus(status).phrase if status in HTTPStatus._value2member_map_ else "Error",
            message=message,
        )
        return self.page(request, "Problem", body, status=status)


# ---------------------------------------------------------------------------
# The HTTP server
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "civicexp"
    sys_version = ""

    def _dispatch(self) -> None:
        app: App = self.server.app  # type: ignore[attr-defined]
        parsed = urlsplit(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        limit = MAX_API_BODY if parsed.path.startswith("/api/") else MAX_BODY
        if length < 0 or length > limit:
            self._send(Response.json(
                {"error": {"code": "HTTP_TOO_LARGE",
                           "message": f"request bodies are limited to {limit:,} bytes"}},
                413,
            ))
            return
        body = self.rfile.read(length) if length else b""
        request = Request(
            method=self.command,
            path=unquote(parsed.path) or "/",
            query=parse_qs(parsed.query),
            headers=self.headers,
            body=body,
            client=self.client_address[0],
        )
        if request.path.startswith("/static/"):
            self._send(self._static(request.path[len("/static/"):]))
            return
        self._send(app.handle(request))

    def _static(self, name: str) -> Response:
        if name not in STATIC_FILES:
            return Response(404, b"not found", "text/plain; charset=utf-8")
        response = Response(200, (STATIC_DIR / name).read_bytes(), STATIC_FILES[name])
        response.headers.append(("Cache-Control", "public, max-age=3600"))
        response.headers.append(("X-Content-Type-Options", "nosniff"))
        if name.endswith(".js"):
            # The client library is loaded by agency pages on other origins.
            response.headers.append(("Cross-Origin-Resource-Policy", "cross-origin"))
        return response

    def _send(self, response: Response) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_PATCH = _dispatch

    def log_message(self, format: str, *args: Any) -> None:
        # Log the path without its query string. Nothing identifying travels
        # in a URL by design, and logs should not become a second copy of
        # anything that does by mistake.
        path = urlsplit(getattr(self, "path", "")).path
        sys.stderr.write(
            f"{self.log_date_time_string()} {self.client_address[0]} "
            f"{getattr(self, 'command', '-')} {path} {args[1] if len(args) > 1 else ''}\n"
        )


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(app: App, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    server = _Server((host, port), _Handler)
    server.app = app  # type: ignore[attr-defined]
    return server
