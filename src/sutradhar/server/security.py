"""HTTP boundary security shared by every WSGI route.

The browser never needs cross-origin writes, embedded frames, or privileged
device APIs. Keeping these decisions in one module makes the production policy
easy to audit and keeps route code focused on application behavior.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

Environ = Mapping[str, Any]

_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def security_headers(*, is_api: bool, request_id: str) -> list[tuple[str, str]]:
    """Return the invariant hardening headers for one response."""
    return [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Cross-Origin-Opener-Policy", "same-origin"),
        ("Cross-Origin-Resource-Policy", "same-origin"),
        (
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        ),
        ("Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"),
        ("Content-Security-Policy", "default-src 'none'" if is_api else _CSP),
        ("X-Request-Id", request_id),
    ]


def is_same_origin(environ: Environ) -> bool:
    """Reject browser cross-site writes while allowing non-browser API clients."""
    site = str(environ.get("HTTP_SEC_FETCH_SITE") or "").lower()
    if site and site not in {"same-origin", "none"}:
        return False

    origin = str(environ.get("HTTP_ORIGIN") or "")
    if not origin:
        return True
    try:
        origin_host = urlsplit(origin).netloc.lower()
    except ValueError:
        return False
    request_host = str(environ.get("HTTP_HOST") or "").lower()
    return bool(origin_host) and origin_host == request_host


def client_key(environ: Environ) -> str:
    """Return the server-normalized client IP used for rate limiting.

    Waitress validates trusted proxy headers and rewrites ``REMOTE_ADDR``
    before the application runs. Re-parsing ``X-Forwarded-For`` here could
    replace that value with an intermediate proxy in a multi-hop chain.
    """
    raw = str(environ.get("REMOTE_ADDR") or "unknown")
    try:
        return ipaddress.ip_address(raw).compressed
    except ValueError:
        return "unknown"


__all__ = ["Environ", "client_key", "is_same_origin", "security_headers"]
