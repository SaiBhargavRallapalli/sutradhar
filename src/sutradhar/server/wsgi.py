"""Production WSGI application for the web UI and JSON planning API."""

from __future__ import annotations

import json
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from http import HTTPStatus
from pathlib import Path
from typing import Any, BinaryIO, Protocol, cast
from urllib.parse import unquote

from waitress import serve as waitress_serve

from ..ai.guardrails import redact
from ..ai.provider import MockProvider, Provider, resolve_provider
from ..ai.resilience import ResilientProvider
from ..config import Settings, load_settings
from ..exceptions import SutradharError
from ..inputs import brief_from_dict
from ..models import Brief, Plan
from ..pipeline import plan_event
from ..version import __version__
from .limits import RateLimiter
from .security import client_key, is_same_origin, security_headers
from .static import StaticStore

MAX_BODY_BYTES = 32 * 1024
MAX_BRIEF_CHARS = 20_000

#: Caps for free-form taste values: text length per entry, and entries per list.
_TASTE_TEXT_LIMIT = 2_000
_TASTE_ENTRY_LIMIT = 40

_WEB_ROOT_CANDIDATES = (
    Path(__file__).resolve().parent.parent.parent / "web",
    Path(__file__).resolve().parent.parent.parent.parent / "web",
)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "event_type",
        "type",
        "event_date",
        "date",
        "guest_count",
        "guests",
        "headcount",
        "budget_total_minor",
        "budget",
        "budget_total",
        "total",
        "currency",
        "currency_code",
        "city_tier",
        "location",
        "title",
        "host_name",
        "host",
        "setting",
        "indoor_outdoor",
        "season",
        "priorities",
        "taste",
        "notes",
        "natural_language",
        "request",
    }
)
_TASTE_FIELDS = frozenset(
    {
        "palette",
        "style_keywords",
        "style",
        "traditions",
        "vibe",
        "dietary",
        "accessibility_notes",
        "accessibility",
        "must_haves",
        "avoid",
    }
)
_RAW_LIMITS = {
    "title": 120,
    "notes": 2_000,
    "location": 120,
    "host_name": 80,
    "natural_language": 4_000,
    "request": 4_000,
}


class StartResponse(Protocol):
    def __call__(
        self,
        status: str,
        headers: list[tuple[str, str]],
        exc_info: tuple[type[BaseException], BaseException, object] | None = None,
    ) -> Callable[[bytes], object] | None: ...


@dataclass(frozen=True, slots=True)
class Response:
    status: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


def _find_web_root(explicit: str | Path | None) -> Path | None:
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        return candidate if (candidate / "index.html").is_file() else None
    # A non-editable wheel install puts this module under site-packages while
    # Render starts the process from the checked-out repository containing
    # ``web/``. Source/editable installs use the module-relative fallbacks.
    for candidate in (Path.cwd() / "web", *_WEB_ROOT_CANDIDATES):
        if (candidate / "index.html").is_file():
            return candidate.resolve()
    return None


def _strict_payload(payload: dict[str, Any]) -> None:
    unknown = sorted(set(payload) - _TOP_LEVEL_FIELDS)
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(unknown)}")
    taste = payload.get("taste")
    if taste is not None and not isinstance(taste, Mapping):
        raise ValueError("taste must be an object")
    if isinstance(taste, Mapping):
        unknown_taste = sorted(set(taste) - _TASTE_FIELDS)
        if unknown_taste:
            raise ValueError(f"unknown taste field(s): {', '.join(unknown_taste)}")
    _check_raw_limits(payload)


def _check_raw_limits(payload: dict[str, Any]) -> None:
    for field, limit in _RAW_LIMITS.items():
        value = payload.get(field)
        if isinstance(value, str) and len(value) > limit:
            raise ValueError(f"{field} is too long (max {limit} characters)")
    taste = payload.get("taste")
    if isinstance(taste, Mapping):
        for key, value in taste.items():
            if isinstance(value, str) and len(value) > _TASTE_TEXT_LIMIT:
                raise ValueError(f"taste.{key} is too long (max {_TASTE_TEXT_LIMIT} characters)")
            if isinstance(value, list) and len(value) > _TASTE_ENTRY_LIMIT:
                raise ValueError(f"taste.{key} accepts at most {_TASTE_ENTRY_LIMIT} entries")


def _coerce_brief(payload: dict[str, Any]) -> Brief:
    _strict_payload(payload)
    brief = brief_from_dict(payload)
    brief.validate()
    return brief


class Application:
    """Small WSGI router with explicit, injectable dependencies."""

    def __init__(
        self,
        settings: Settings,
        provider: Provider,
        static_store: StaticStore,
        *,
        request_limiter: RateLimiter,
        model_limiter: RateLimiter,
        echo: Callable[[str], None],
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.static_store = static_store
        self.request_limiter = request_limiter
        self.model_limiter = model_limiter
        self.echo = echo

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        started = time.monotonic()
        request_id = uuid.uuid4().hex
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        path = unquote(str(environ.get("PATH_INFO") or "/"))
        try:
            response = self._dispatch(environ, method, path)
        except Exception as exc:  # pragma: no cover - final boundary
            self.echo(f"[{request_id}] unhandled {type(exc).__name__}")
            response = _error(500, "planning failed", "the server logged the request ID")

        is_api = path.startswith("/api/")
        headers = [*response.headers, *security_headers(is_api=is_api, request_id=request_id)]
        if not any(key.lower() == "content-length" for key, _ in headers):
            headers.append(("Content-Length", str(len(response.body))))
        start_response(response.status, headers)
        code = response.status.split(" ", maxsplit=1)[0]
        elapsed_ms = round((time.monotonic() - started) * 1_000)
        safe_path = path.replace("\r", "").replace("\n", "")[:256]
        self.echo(f"[{request_id}] {method} {safe_path} {code} {elapsed_ms}ms")
        return [b"" if method == "HEAD" else response.body]

    def _dispatch(self, environ: dict[str, Any], method: str, path: str) -> Response:
        if path == "/api/health" and method in {"GET", "HEAD"}:
            return self._health()
        if path.startswith("/api/") and path != "/api/plan":
            return _error(404, "unknown endpoint", "try POST /api/plan")
        if path == "/api/plan":
            if method != "POST":
                return _error(404, "unknown endpoint", "try POST /api/plan")
            return self._plan(environ)
        if method not in {"GET", "HEAD"}:
            return _error(405, "method not allowed")
        static = self.static_store.get(
            path,
            accept_encoding=str(environ.get("HTTP_ACCEPT_ENCODING") or ""),
            if_none_match=str(environ.get("HTTP_IF_NONE_MATCH") or ""),
            head_only=method == "HEAD",
        )
        return Response(static.status, static.headers, static.body)

    def _health(self) -> Response:
        payload: dict[str, Any] = {
            "ok": True,
            "version": __version__,
            "date": date.today().isoformat(),
            "mode": self.provider.name if self.provider.is_live else "catalogue",
        }
        if isinstance(self.provider, ResilientProvider):
            stats = self.provider.snapshot()
            payload["ai_cache"] = {
                "entries": stats.cache_entries,
                "hits": stats.cache_hits,
                "shared": stats.shared_calls,
            }
        return _json_response(200, payload)

    def _plan(self, environ: dict[str, Any]) -> Response:
        response = _check_origin_and_type(environ)
        if response is not None:
            return response

        response = self._check_rate_limits(environ)
        if response is not None:
            return response

        parsed = _parse_brief(environ)
        if isinstance(parsed, Response):
            return parsed

        plan = self._build_plan(parsed)
        if isinstance(plan, Response):
            return plan
        return _json_response(200, {"ok": True, "plan": plan.to_dict()})

    def _check_rate_limits(self, environ: Mapping[str, Any]) -> Response | None:
        """Apply the general request limit, then the tighter model limit."""
        client = client_key(environ)
        decision = self.request_limiter.check(client)
        if not decision.allowed:
            return _error(
                429,
                "too many requests",
                "wait before trying again",
                extra=(("Retry-After", str(decision.retry_after)),),
            )
        if self.settings.ai_enabled and self.provider.is_live:
            model_decision = self.model_limiter.check(client)
            if not model_decision.allowed:
                return _error(
                    429,
                    "too many model requests",
                    "continue later; offline planning remains available",
                    extra=(("Retry-After", str(model_decision.retry_after)),),
                )
        return None

    def _build_plan(self, brief: Brief) -> Plan | Response:
        """Run the planner, turning an unexpected failure into a 500 response."""
        try:
            return plan_event(
                brief,
                settings=self.settings,
                today=date.today(),
                provider=self.provider,
            )
        except Exception as exc:  # pragma: no cover - defensive domain boundary
            self.echo(f"plan failed: {type(exc).__name__}")
            return _error(500, "planning failed", "the server logged the request ID")


def _check_origin_and_type(environ: Mapping[str, Any]) -> Response | None:
    """Reject cross-site requests and non-JSON bodies before reading anything."""
    if not is_same_origin(environ):
        return _error(403, "cross-site requests are not allowed")
    if _content_type(environ) != "application/json":
        return _error(415, "unsupported media type", "send Content-Type: application/json")
    return None


def _parse_brief(environ: Mapping[str, Any]) -> Brief | Response:
    """Read, decode and validate the request body into a Brief (or an error response)."""
    body = _read_body(environ)
    if isinstance(body, Response):
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return _error(400, "request body is not valid UTF-8")

    payload = _decode_payload(text)
    if isinstance(payload, Response):
        return payload
    try:
        return _coerce_brief(payload)
    except (ValueError, SutradharError) as exc:
        return _error(422, redact(str(exc)), "see the Brief fields in the README")


def _decode_payload(text: str) -> dict[str, Any] | Response:
    """Check the length and decode a JSON object, or explain why it cannot be."""
    if len(text) > MAX_BRIEF_CHARS:
        return _error(413, f"brief too long (max {MAX_BRIEF_CHARS} characters)")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return _error(400, f"invalid JSON: {exc.msg}", "check for trailing commas or quotes")
    if not isinstance(payload, dict):
        return _error(400, "expected a JSON object at the top level")
    return cast(dict[str, Any], payload)


def _content_type(environ: Mapping[str, Any]) -> str:
    return str(environ.get("CONTENT_TYPE") or "").split(";", maxsplit=1)[0].strip().lower()


def _read_body(environ: Mapping[str, Any]) -> bytes | Response:
    raw_length = str(environ.get("CONTENT_LENGTH") or "")
    try:
        length = int(raw_length)
    except ValueError:
        return _error(400, "invalid Content-Length")
    if length <= 0:
        return _error(400, "empty request body", "POST a JSON event brief")
    if length > MAX_BODY_BYTES:
        return _error(413, f"request body too large (max {MAX_BODY_BYTES} bytes)")
    stream = cast(BinaryIO, environ["wsgi.input"])
    body = stream.read(length)
    if len(body) != length:
        return _error(400, "request body ended before Content-Length")
    return body


def _json_response(status: int, payload: dict[str, Any]) -> Response:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Response(
        _status(status),
        (("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store")),
        body,
    )


def _error(
    status: int,
    message: str,
    hint: str = "",
    *,
    extra: tuple[tuple[str, str], ...] = (),
) -> Response:
    payload: dict[str, Any] = {"ok": False, "error": message, "status": status}
    if hint:
        payload["hint"] = hint
    response = _json_response(status, payload)
    return Response(response.status, (*response.headers, *extra), response.body)


def _status(code: int) -> str:
    return f"{code} {HTTPStatus(code).phrase}"


def create_app(
    *,
    settings: Settings | None = None,
    provider: Provider | None = None,
    web_root: str | Path | None = None,
    echo: Callable[[str], None] | None = None,
) -> Application:
    """Build the WSGI app around settings, a provider and the static UI."""
    runtime = settings or load_settings()
    resolved = provider or (resolve_provider(runtime) if runtime.ai_enabled else MockProvider())
    if resolved.is_live and not isinstance(resolved, ResilientProvider):
        resolved = ResilientProvider(
            resolved,
            max_entries=runtime.ai_cache_entries,
            ttl_s=runtime.ai_cache_ttl,
            daily_cap=runtime.ai_daily_cap,
            failure_threshold=runtime.ai_breaker_threshold,
            cool_down_s=runtime.ai_breaker_cooldown,
        )
    logger = echo or (lambda message: print(message, file=sys.stderr, flush=True))
    return Application(
        runtime,
        resolved,
        StaticStore(_find_web_root(web_root)),
        request_limiter=RateLimiter(60, 60.0),
        model_limiter=RateLimiter(20, 600.0),
        echo=logger,
    )


def serve(host: str = "127.0.0.1", port: int = 8787, web_root: str | Path | None = None) -> int:
    """Serve Sutradhar with Waitress's bounded production WSGI runtime."""
    bind = "127.0.0.1" if host in {"", "localhost"} else host
    settings = load_settings()
    app = create_app(settings=settings, web_root=web_root)
    mode = f"model {app.provider.model}" if app.provider.is_live else "offline catalogue"
    print(
        f"  Sutradhar {__version__} serving on http://{bind}:{port}/  ({mode})",
        file=sys.stderr,
        flush=True,
    )
    print("  press Ctrl+C to stop", file=sys.stderr, flush=True)
    proxy_options: dict[str, Any] = {}
    if settings.trust_proxy:
        # Render is the only public ingress. Waitress strips forwarded headers
        # unless its immediate upstream is explicitly trusted.
        proxy_options = {
            "trusted_proxy": "*",
            "trusted_proxy_count": 1,
            "trusted_proxy_headers": {"x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"},
        }
    try:
        waitress_serve(
            cast(Any, app),
            host=bind,
            port=port,
            threads=8,
            channel_timeout=30,
            cleanup_interval=10,
            max_request_body_size=MAX_BODY_BYTES,
            expose_tracebacks=False,
            ident=f"Sutradhar/{__version__}",
            clear_untrusted_proxy_headers=True,
            **proxy_options,
        )
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\n  stopped", file=sys.stderr, flush=True)
    except OSError as exc:
        print(f"error: could not bind {bind}:{port} - {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


__all__ = [
    "MAX_BODY_BYTES",
    "Application",
    "Response",
    "_coerce_brief",
    "_find_web_root",
    "create_app",
    "serve",
]
