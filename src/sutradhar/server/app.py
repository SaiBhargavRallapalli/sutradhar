"""A deliberately small HTTP server for Sutradhar.

Design constraints, in order of importance:

1. **No dependencies.** Built on :mod:`http.server` so the whole project keeps
   its "zero runtime dependencies" promise.
2. **Safe by default.** Binds ``127.0.0.1`` unless explicitly told otherwise,
   caps request size, refuses non-JSON bodies, sets conservative security
   headers, and never reflects request data into headers (no XSS via
   ``Content-Disposition`` or similar).
3. **Stateless.** Every request builds a fresh planner; nothing is cached in
   memory, so there is no cross-user data leakage path.

This server is intended for local demos and evaluation. It is *not* a
production deployment target — see ``docs/SECURITY.md`` for what a production
deployment would additionally require (TLS termination, authn, rate limits at
the edge, etc.).
"""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .. import Brief
from ..ai.guardrails import redact
from ..config import load_settings
from ..exceptions import SutradharError
from ..inputs import brief_from_dict
from ..models import Plan
from ..pipeline import plan_event
from ..version import __version__

MAX_BODY_BYTES = 256 * 1024  # a brief is a few KB; anything larger is abusive
MAX_BRIEF_CHARS = 20_000
REQUEST_WINDOW_S = 60.0
MAX_REQUESTS_PER_WINDOW = 30

_WEB_ROOT_CANDIDATES = (
    Path(__file__).resolve().parent.parent.parent / "web",
    Path(__file__).resolve().parent.parent.parent.parent / "web",
)

_CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}

_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class RateLimiter:
    """Fixed-window limiter (process-local). Cheap and honest about its limits."""

    def __init__(self, limit: int = MAX_REQUESTS_PER_WINDOW, window_s: float = REQUEST_WINDOW_S) -> None:
        self._limit = limit
        self._window = window_s
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.setdefault(key, [])
            cutoff = now - self._window
            bucket[:] = [t for t in bucket if t > cutoff]
            if len(bucket) >= self._limit:
                return False
            bucket.append(now)
            return True


def _find_web_root(explicit: str | None) -> Path | None:
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        return candidate if (candidate / "index.html").is_file() else None
    for candidate in _WEB_ROOT_CANDIDATES:
        if (candidate / "index.html").is_file():
            return candidate.resolve()
    return None


#: Per-field caps checked against the *raw* payload, before parsing. Checking
#: here rather than after means an over-long field is rejected with a clear
#: message instead of being silently truncated by the parser.
_RAW_LIMITS: dict[str, int] = {
    "title": 120,
    "notes": 2_000,
    "location": 120,
    "host_name": 80,
    "natural_language": 4_000,
    "request": 4_000,
}


def _check_raw_limits(payload: dict[str, Any]) -> None:
    """Reject over-long text fields up front, naming the offending field."""
    for field, limit in _RAW_LIMITS.items():
        value = payload.get(field)
        if isinstance(value, str) and len(value) > limit:
            raise ValueError(f"{field} is too long (max {limit} characters)")
    taste = payload.get("taste")
    if isinstance(taste, dict):
        for key, value in taste.items():
            if isinstance(value, str) and len(value) > 2_000:
                raise ValueError(f"taste.{key} is too long (max 2000 characters)")
            if isinstance(value, list) and len(value) > 40:
                raise ValueError(f"taste.{key} accepts at most 40 entries")


def _coerce_brief(payload: dict[str, Any]) -> Brief:
    """Turn an untrusted JSON object into a validated :class:`Brief`.

    Raises :class:`ValueError` or :class:`~sutradhar.exceptions.SutradharError`
    on bad input; the HTTP layer maps either to a 422 response.
    """
    _check_raw_limits(payload)
    brief = brief_from_dict(payload)
    brief.validate()
    return brief


def make_handler(
    web_root: Path | None,
    provider: Any,
    limiter: RateLimiter,
    echo: Callable[[str], None],
) -> type[BaseHTTPRequestHandler]:
    """Build a request handler with its configuration closed over."""

    class Handler(BaseHTTPRequestHandler):
        server_version = f"sutradhar/{__version__}"
        protocol_version = "HTTP/1.1"
        # Idle clients are dropped instead of holding a thread forever
        # (slow-loris). Thirty seconds is generous for a sub-second planner.
        timeout = 30

        # -- plumbing ------------------------------------------------------
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            echo(f"[{self.log_date_time_string()}] {fmt % args}")

        def _headers(self, status: int, ctype: str, length: int,
                     extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
            if self.close_connection:
                # Told the peer we are done with this connection; without this
                # header a keep-alive client waits for a body that never comes.
                self.send_header("Connection", "close")
            if self.path.startswith("/api/"):
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'none'")
            else:
                self.send_header("Content-Security-Policy", _CSP)
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()

        def _send_json(self, status: int, payload: dict[str, Any], extra: dict[str, str] | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(body), extra)
            self.wfile.write(body)

        def _error(self, status: int, message: str, *, hint: str = "",
                   drain: bool = False) -> None:
            """Send a JSON error, optionally closing the connection.

            ``drain=True`` is for errors raised *before* the request body was
            read. The bytes are still in flight, so the only safe options are
            to read them or to close the socket; leaving them unread makes the
            client see a connection reset instead of our status code.
            """
            if not drain:
                self.close_connection = False
            else:
                self.close_connection = True
            payload: dict[str, Any] = {"ok": False, "error": message, "status": status}
            if hint:
                payload["hint"] = hint
            self._send_json(status, payload)

        # -- routes --------------------------------------------------------
        def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
            """Answer HEAD with headers and no body.

            ``BaseHTTPRequestHandler`` does not provide this, so a HEAD request
            would otherwise 501 - which breaks link checkers, monitoring probes
            and any browser prefetch. Mirrors the GET routing but suppresses the
            payload, as RFC 9110 requires.
            """
            parsed = urlparse(self.path)
            if parsed.path == "/api/health":
                body = json.dumps(
                    {"ok": True, "version": __version__, "date": date.today().isoformat()}
                ).encode("utf-8")
                self._headers(200, "application/json; charset=utf-8", len(body))
                return
            if parsed.path.startswith("/api/"):
                self._error(404, "unknown endpoint", hint="try POST /api/plan")
                return
            self._serve_static(parsed.path, head_only=True)

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            parsed = urlparse(self.path)
            if parsed.path == "/api/health":
                self._send_json(200, {"ok": True, "version": __version__, "date": date.today().isoformat()})
                return
            if parsed.path.startswith("/api/"):
                self._error(404, "unknown endpoint", hint="try POST /api/plan")
                return
            self._serve_static(parsed.path)

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            parsed = urlparse(self.path)
            if parsed.path != "/api/plan":
                self._error(404, "unknown endpoint", hint="try POST /api/plan")
                return

            client = self.client_address[0] if self.client_address else "unknown"
            if not limiter.allow(client):
                self._error(429, "too many requests", hint=f"try again in {int(REQUEST_WINDOW_S)} seconds")
                return

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._error(400, "invalid Content-Length", drain=True)
                return
            if length <= 0:
                self._error(400, "empty request body",
                            hint="POST a JSON object describing the event", drain=True)
                return
            if length > MAX_BODY_BYTES:
                self._error(413, f"request body too large (max {MAX_BODY_BYTES} bytes)", drain=True)
                return

            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._error(415, "unsupported media type",
                            hint="send Content-Type: application/json", drain=True)
                return

            raw = self.rfile.read(length)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                self._error(400, "request body is not valid UTF-8")
                return
            if len(text) > MAX_BRIEF_CHARS:
                self._error(413, f"brief too long (max {MAX_BRIEF_CHARS} characters)")
                return

            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                self._error(400, f"invalid JSON: {exc.msg}", hint="check for trailing commas or quotes")
                return
            if not isinstance(payload, dict):
                self._error(400, "expected a JSON object at the top level")
                return

            try:
                brief = _coerce_brief(payload)
            except (ValueError, SutradharError) as exc:
                # Both are caller-fixable problems, so 422 with the message
                # surfaced. Anything else must not reach here uncaught, or the
                # connection dies without a response.
                self._error(422, redact(str(exc)), hint="see the Brief fields in the README")
                return

            try:
                plan = plan_event(brief, today=date.today(), provider=provider)
            except Exception as exc:  # pragma: no cover - defensive
                self.log_message("plan failed: %s", exc)
                self._error(500, "planning failed", hint="the server logged the details")
                return

            self._send_json(200, {"ok": True, "plan": plan.to_dict()})

        # -- static assets -------------------------------------------------
        def _serve_static(self, raw_path: str, *, head_only: bool = False) -> None:
            if web_root is None:
                self._error(404, "no web UI bundled with this install",
                            hint="run `make web` from a source checkout")
                return
            path = raw_path or "/"
            if path == "/":
                path = "/index.html"
            # Resolve inside web_root only; refuse anything that escapes it.
            candidate = (web_root / path.lstrip("/")).resolve()
            if not str(candidate).startswith(str(web_root)):
                self._error(403, "forbidden")
                return
            if not candidate.is_file():
                self._error(404, "not found")
                return
            if candidate.suffix not in _CTYPES:
                self._error(403, "forbidden")
                return
            body = candidate.read_bytes()
            self._headers(200, _CTYPES[candidate.suffix], len(body))
            if not head_only:
                self.wfile.write(body)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8787, web_root: str | Path | None = None) -> int:
    """Start the server and block until interrupted. Returns a process exit code."""
    from ..ai import resolve_provider

    settings = load_settings()
    provider = resolve_provider(settings) if settings.ai_enabled else None
    root = _find_web_root(str(web_root) if web_root else None)
    limiter = RateLimiter()
    bind = "127.0.0.1" if host in ("localhost", "") else host

    def echo(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    handler = make_handler(root, provider, limiter, echo)
    try:
        httpd = ThreadingHTTPServer((bind, port), handler)
    except OSError as exc:
        echo(f"error: could not bind {bind}:{port} - {exc}")
        return 1

    live = bool(getattr(provider, "is_configured", lambda: False)())
    mode = f"model {provider.model}" if live else "offline catalogue"
    echo(f"  Sutradhar {__version__} serving on http://{bind}:{port}/  ({mode})")
    echo("  press Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        echo("\n  stopped")
    finally:
        httpd.server_close()
    return 0


__all__ = ["serve", "make_handler", "RateLimiter", "MAX_BODY_BYTES"]
