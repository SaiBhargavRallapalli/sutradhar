"""Provider transport contract tests with no network access."""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message

import pytest

from sutradhar.ai import provider as provider_module
from sutradhar.ai.provider import GeminiProvider, OpenAICompatProvider, resolve_provider
from sutradhar.config import Settings
from sutradhar.exceptions import AIProviderError


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.body = json.dumps(payload).encode()
        self.headers = Message()
        self.headers["Content-Type"] = "application/json; charset=utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def _http_error(code: int, message: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://model.example",
        code,
        "failure",
        Message(),
        io.BytesIO(json.dumps({"error": {"message": message}}).encode()),
    )


def test_post_json_success_and_non_object(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse({"ok": True}),
    )
    assert provider_module._post_json("https://model.example", {}, {}, timeout=1) == {"ok": True}

    monkeypatch.setattr(
        provider_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(["not", "an", "object"]),
    )
    with pytest.raises(AIProviderError, match="non-object"):
        provider_module._post_json("https://model.example", {}, {}, timeout=1)


def test_post_json_retries_transient_http_failure(monkeypatch) -> None:
    outcomes = [_http_error(503, "busy"), FakeResponse({"ok": True})]
    calls = 0

    def urlopen(*_args, **_kwargs):
        nonlocal calls
        outcome = outcomes[calls]
        calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(provider_module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(provider_module.time, "sleep", lambda _delay: None)
    assert provider_module._post_json("https://model.example", {}, {}, timeout=1)["ok"] is True
    assert calls == 2


def test_post_json_redacts_non_retryable_error(monkeypatch) -> None:
    secret = "gsk_" + "a" * 32
    monkeypatch.setattr(
        provider_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(_http_error(401, secret)),
    )
    with pytest.raises(AIProviderError) as captured:
        provider_module._post_json("https://model.example", {}, {}, timeout=1)
    assert secret not in str(captured.value)
    assert "REDACTED" in str(captured.value)


def test_post_json_reports_network_and_json_failures(monkeypatch) -> None:
    monkeypatch.setattr(provider_module.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(
        provider_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
    )
    with pytest.raises(AIProviderError, match="could not reach"):
        provider_module._post_json("https://model.example", {}, {}, timeout=1)

    response = FakeResponse({})
    response.body = b"not json"
    monkeypatch.setattr(
        provider_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: response,
    )
    with pytest.raises(AIProviderError, match="non-JSON"):
        provider_module._post_json("https://model.example", {}, {}, timeout=1)


def test_openai_and_gemini_extract_text(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_module,
        "_post_json",
        lambda *_args, **_kwargs: {"choices": [{"message": {"content": "openai"}}]},
    )
    openai = OpenAICompatProvider("secret", "https://groq.example/v1", "model-a")
    assert openai.complete("system", "user") == "openai"
    assert openai.model == "model-a"

    monkeypatch.setattr(
        provider_module,
        "_post_json",
        lambda *_args, **_kwargs: {
            "candidates": [{"content": {"parts": [{"text": "gem"}, {"text": "ini"}]}}]
        },
    )
    gemini = GeminiProvider("secret", "model-b")
    assert gemini.complete("system", "user") == "gemini"
    assert gemini.model == "model-b"


def test_provider_response_shape_errors(monkeypatch) -> None:
    monkeypatch.setattr(provider_module, "_post_json", lambda *_args, **_kwargs: {})
    with pytest.raises(AIProviderError, match="missing choices"):
        OpenAICompatProvider("secret", "https://example", "model").complete("s", "u")
    with pytest.raises(AIProviderError, match="missing candidates"):
        GeminiProvider("secret", "model").complete("s", "u")


def test_auto_provider_resolution() -> None:
    openai = resolve_provider(
        Settings(openai_api_key="secret", openai_base_url="https://groq.example/v1")
    )
    assert isinstance(openai, OpenAICompatProvider)

    gemini = resolve_provider(Settings(gemini_api_key="secret"))
    assert isinstance(gemini, GeminiProvider)
