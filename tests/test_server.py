"""HTTP server tests.

The server is the only part of Sutradhar that accepts untrusted input over a
network, so these tests focus on the boundary: correct status codes, refusal
of oversized or wrongly-typed bodies, no path traversal, and no leaking of
internals in error responses.
"""
from __future__ import annotations

import json
import threading
from datetime import date
from http.server import ThreadingHTTPServer

import pytest

from sutradhar.server.app import (
    MAX_BODY_BYTES,
    RateLimiter,
    _coerce_brief,
    _find_web_root,
    make_handler,
)

HOST = "127.0.0.1"


@pytest.fixture
def server():
    """A live server on an ephemeral port, torn down after the test."""
    root = _find_web_root(None)
    limiter = RateLimiter(limit=1000)  # generous, so rate-limit tests can set their own
    handler = make_handler(root, None, limiter, lambda *_: None)
    httpd = ThreadingHTTPServer((HOST, 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def _request(httpd, method: str, path: str, *, data: bytes | None = None,
             ctype: str | None = None, timeout: float = 5.0):
    """Issue a request and return ``(status, headers, body_bytes)``."""
    import urllib.error
    import urllib.request

    headers = {"Content-Type": ctype} if ctype else {}
    req = urllib.request.Request(
        f"http://{HOST}:{httpd.server_address[1]}{path}",
        data=data, headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _valid_brief(**overrides) -> dict:
    payload = {
        "event_type": "wedding",
        "event_date": "2027-06-06",
        "guest_count": 150,
        "budget": "800000",
        "currency": "INR",
        "location": "Pune",
        "city_tier": "tier_2",
        "setting": "open_air",
    }
    payload.update(overrides)
    return payload


def _post_json(httpd, payload, *, ctype: str = "application/json", **kw):
    return _request(httpd, "POST", "/api/plan",
                    data=json.dumps(payload).encode(),
                    ctype=ctype, **kw)


class TestHealth:
    def test_reports_ok_and_version(self, server) -> None:
        status, _, body = _request(server, "GET", "/api/health")
        assert status == 200
        payload = json.loads(body)
        assert payload["ok"] is True
        assert payload["version"]

    def test_health_never_stores_the_response(self, server) -> None:
        _, headers, _ = _request(server, "GET", "/api/health")
        assert headers.get("Cache-Control") == "no-store"


class TestStaticAssets:
    def test_serves_the_index(self, server) -> None:
        status, headers, body = _request(server, "GET", "/")
        assert status == 200
        assert b"<html" in body.lower()
        assert "text/html" in headers["Content-Type"]

    def test_serves_css_and_js_with_correct_types(self, server) -> None:
        for path, expected in (("/styles.css", "text/css"), ("/app.js", "javascript")):
            status, headers, _ = _request(server, "GET", path)
            assert status == 200
            assert expected in headers["Content-Type"]

    def test_sets_security_headers_on_pages(self, server) -> None:
        _, headers, _ = _request(server, "GET", "/")
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert "Content-Security-Policy" in headers

    @pytest.mark.parametrize("path", [
        "/../pyproject.toml",
        "/..%2f..%2fetc/passwd",
        "/../../src/sutradhar/config.py",
        "/./../../Makefile",
    ])
    def test_refuses_path_traversal(self, server, path: str) -> None:
        status, _, _ = _request(server, "GET", path)
        assert status in (403, 404), f"{path} was not refused"

    def test_unknown_asset_is_404(self, server) -> None:
        status, _, _ = _request(server, "GET", "/nope.js")
        assert status == 404

    def test_refuses_unmapped_extensions(self, server, tmp_path) -> None:
        """Even inside the web root, only known asset types are served."""
        status, _, _ = _request(server, "GET", "/app.js.bak")
        assert status in (403, 404)


class TestPlanEndpoint:
    def test_plans_a_valid_brief(self, server) -> None:
        status, _, body = _post_json(server, _valid_brief())
        assert status == 200
        payload = json.loads(body)
        assert payload["ok"] is True
        plan = payload["plan"]
        assert plan["brief"]["guest_count"] == 150
        assert plan["budget"]["unallocated_minor"] == 0

    def test_unknown_endpoint_is_404(self, server) -> None:
        status, _, body = _post_json(server, _valid_brief())
        assert status == 200
        status, _, _ = _request(server, "POST", "/api/other",
                                data=b"{}", ctype="application/json")
        assert status == 404

    def test_get_on_plan_is_404_not_a_crash(self, server) -> None:
        status, _, _ = _request(server, "GET", "/api/plan")
        assert status == 404


class TestRequestValidation:
    def test_rejects_non_json_content_type(self, server) -> None:
        status, _, body = _post_json(server, _valid_brief(), ctype="text/plain")
        assert status == 415
        assert json.loads(body)["ok"] is False

    def test_rejects_missing_content_type(self, server) -> None:
        status, _, _ = _request(server, "POST", "/api/plan", data=b"{}")
        assert status == 415

    def test_rejects_malformed_json(self, server) -> None:
        status, _, body = _request(server, "POST", "/api/plan",
                                   data=b'{"event_type": ', ctype="application/json")
        assert status == 400
        assert "invalid json" in json.loads(body)["error"].lower()

    def test_rejects_a_json_array(self, server) -> None:
        status, _, _ = _request(server, "POST", "/api/plan",
                                data=b"[1,2]", ctype="application/json")
        assert status == 400

    def test_rejects_an_empty_body(self, server) -> None:
        status, _, _ = _request(server, "POST", "/api/plan",
                                data=b"", ctype="application/json")
        assert status == 400

    def test_rejects_an_oversized_body(self, server) -> None:
        big = json.dumps(_valid_brief(notes="x" * (MAX_BODY_BYTES + 1000))).encode()
        status, _, _ = _request(server, "POST", "/api/plan",
                                data=big, ctype="application/json")
        assert status == 413

    def test_rejects_invalid_utf8(self, server) -> None:
        status, _, _ = _request(server, "POST", "/api/plan",
                                data=b'{"a": "\xff\xfe"}', ctype="application/json")
        assert status == 400

    @pytest.mark.parametrize("payload", [
        {"event_type": "wedding"},                      # missing date/guests/budget
        {"event_type": "wedding", "event_date": "2027-06-06",
         "guest_count": 0, "budget": "1000"},            # zero guests
        {"event_type": "wedding", "event_date": "2027-06-06",
         "guest_count": 10, "budget": "0"},              # zero budget
        {"event_type": "nonsense", "event_date": "2027-06-06",
         "guest_count": 10, "budget": "1000"},           # unknown type
        {"event_type": "wedding", "event_date": "not-a-date",
         "guest_count": 10, "budget": "1000"},           # bad date
    ])
    def test_rejects_invalid_briefs_with_422(self, server, payload: dict) -> None:
        status, _, body = _post_json(server, payload)
        assert status == 422, f"{payload} was not rejected"
        assert json.loads(body)["ok"] is False

    def test_error_response_says_what_to_do(self, server) -> None:
        _, _, body = _post_json(server, {"event_type": "wedding"})
        payload = json.loads(body)
        assert payload["error"]
        assert payload.get("hint"), "an error without a hint makes the caller guess"

    def test_error_does_not_leak_a_stack_trace(self, server) -> None:
        _, _, body = _post_json(server, {"event_type": "wedding"})
        text = body.decode()
        assert "Traceback" not in text
        assert 'File "' not in text


class TestCoerceBrief:
    def test_accepts_a_minimal_valid_brief(self) -> None:
        brief = _coerce_brief(_valid_brief())
        assert brief.guest_count == 150

    def test_parses_lakh_shorthand(self) -> None:
        brief = _coerce_brief(_valid_brief(budget="8 lakh"))
        assert brief.budget_total_minor == 80000000

    @pytest.mark.parametrize("field,value", [
        ("title", "x" * 500),
        ("notes", "x" * 5000),
        ("location", "x" * 500),
        ("natural_language", "x" * 9000),
    ])
    def test_rejects_overlong_text_fields(self, field: str, value: str) -> None:
        with pytest.raises(ValueError):
            _coerce_brief(_valid_brief(**{field: value}))

    def test_accepts_taste_profile(self) -> None:
        brief = _coerce_brief(_valid_brief(
            taste={"vibe": "warm", "dietary": ["vegetarian"], "avoid": ["fireworks"]}
        ))
        assert brief.taste.vibe == "warm"
        assert brief.taste.dietary == ("vegetarian",)


class TestRateLimiter:
    def test_allows_up_to_the_limit(self) -> None:
        limiter = RateLimiter(limit=3, window_s=60)
        assert all(limiter.allow("a") for _ in range(3))

    def test_blocks_over_the_limit(self) -> None:
        limiter = RateLimiter(limit=2, window_s=60)
        for _ in range(2):
            limiter.allow("a")
        assert limiter.allow("a") is False

    def test_keys_are_independent(self) -> None:
        limiter = RateLimiter(limit=1, window_s=60)
        assert limiter.allow("a") is True
        assert limiter.allow("b") is True

    def test_window_expiry_allows_requests_again(self) -> None:
        import time

        limiter = RateLimiter(limit=1, window_s=0.05)
        assert limiter.allow("a") is True
        assert limiter.allow("a") is False
        time.sleep(0.06)
        assert limiter.allow("a") is True


class TestWebRootDiscovery:
    def test_finds_the_bundled_web_root(self) -> None:
        root = _find_web_root(None)
        assert root is not None
        assert (root / "index.html").is_file()

    def test_explicit_missing_root_returns_none(self, tmp_path) -> None:
        assert _find_web_root(str(tmp_path)) is None

    def test_explicit_valid_root_is_used(self, tmp_path) -> None:
        (tmp_path / "index.html").write_text("<html></html>")
        assert _find_web_root(str(tmp_path)) == tmp_path.resolve()
