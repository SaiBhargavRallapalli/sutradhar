"""Pluggable LLM providers.

One narrow interface - ``complete(system, user) -> str`` - implemented three
ways plus a fail-fast stub:

* :class:`OpenAICompatProvider` speaks the chat-completions protocol, so the
  same class works with OpenAI, Groq, Together, OpenRouter, Mistral, Fireworks,
  LM Studio, llama.cpp server, vLLM and Ollama's ``/v1`` endpoint. One
  implementation, many backends.
* :class:`GeminiProvider` speaks Google's ``generateContent`` API. The key goes
  in a header rather than the URL so it can never leak into a request log.
* :class:`MockProvider` is deterministic and offline, which is what lets the
  entire test-suite and the demo run with no network and no credentials.
* :class:`NullProvider` fails loudly and explains how to configure a real one.

Transport is :mod:`urllib` from the standard library - keeping the "zero
runtime dependencies" promise holds at the network layer too.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, cast

from ..config import Settings
from ..exceptions import AIConfigError, AIProviderError
from .guardrails import redact

__all__ = [
    "USER_AGENT",
    "GeminiProvider",
    "MockProvider",
    "NullProvider",
    "OpenAICompatProvider",
    "Provider",
    "resolve_provider",
]

USER_AGENT = "sutradhar/1.0 (+event-planner)"

#: Status codes worth retrying: rate limits and transient server errors.
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 2
_BACKOFF_SECONDS = 0.4


class Provider(ABC):
    """Minimal contract every language-model backend satisfies."""

    #: Short identifier recorded in plan provenance.
    name: str = "provider"

    #: ``False`` for offline stubs, so callers know to prefer the catalogue.
    is_live: bool = False

    @property
    def model(self) -> str:
        """Model identifier; ``"unconfigured"`` unless a live backend names one."""
        return "unconfigured"

    @abstractmethod
    def complete(
        self, system: str, user: str, *, temperature: float = 0.7, timeout: float = 45.0
    ) -> str:
        """Return the model's raw text response for one prompt pair."""

    def is_configured(self) -> bool:
        return True


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout: float,
) -> dict[str, Any]:
    """POST JSON with bounded retries on transient failures.

    Every error path runs its message through :func:`redact` before it leaves
    this function, so a credential embedded in a URL or header can never reach a
    log, a traceback or a terminal.
    """
    body = json.dumps(payload).encode("utf-8")
    request_headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT, **headers}
    last_error = "unknown error"
    deadline = time.monotonic() + timeout

    for attempt in range(_MAX_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AIProviderError("the model request exceeded its total time budget")
        request = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=remaining) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                parsed = json.loads(response.read().decode(charset))
                if not isinstance(parsed, dict):
                    raise AIProviderError("the model endpoint returned a non-object response")
                return cast(dict[str, Any], parsed)
        except urllib.error.HTTPError as exc:
            detail = _read_error(exc)
            if exc.code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS - 1:
                last_error = f"HTTP {exc.code}: {detail}"
                _sleep_within(deadline, _BACKOFF_SECONDS * (2**attempt))
                continue
            raise AIProviderError(
                redact(f"model request failed with HTTP {exc.code}: {detail}")
            ) from exc
        except urllib.error.URLError as exc:
            last_error = f"network error: {exc.reason}"
            if attempt < _MAX_ATTEMPTS - 1:
                _sleep_within(deadline, _BACKOFF_SECONDS * (2**attempt))
                continue
            raise AIProviderError(
                redact(f"could not reach the model endpoint: {exc.reason}")
            ) from exc
        except TimeoutError as exc:
            last_error = "request timed out"
            if attempt < _MAX_ATTEMPTS - 1:
                _sleep_within(deadline, _BACKOFF_SECONDS * (2**attempt))
                continue
            raise AIProviderError("the model request timed out") from exc
        except json.JSONDecodeError as exc:
            raise AIProviderError("the model endpoint returned a non-JSON response") from exc

    raise AIProviderError(redact(f"model request failed: {last_error}"))


def _sleep_within(deadline: float, delay: float) -> None:
    """Back off without allowing retries to exceed the caller's total budget."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AIProviderError("the model request exceeded its total time budget")
    time.sleep(min(delay, remaining))


def _read_error(exc: urllib.error.HTTPError) -> str:
    """Extract a short, redacted message from an HTTP error body."""
    try:
        raw = exc.read(2_000).decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - defensive
        return exc.reason or "no detail"
    try:
        parsed = json.loads(raw)
        message = parsed.get("error", {}).get("message") if isinstance(parsed, dict) else None
        return str(message or raw)[:400]
    except json.JSONDecodeError:
        return str(raw)[:400]


class OpenAICompatProvider(Provider):
    """Any ``/chat/completions`` endpoint, self-hosted or commercial."""

    name = "openai-compat"
    is_live = True

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def complete(
        self, system: str, user: str, *, temperature: float = 0.7, timeout: float = 45.0
    ) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": 1_400,
        }
        data = _post_json(
            f"{self._base_url}/chat/completions",
            payload,
            {"Authorization": f"Bearer {self._api_key}"},
            timeout=timeout,
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIProviderError(
                "the model response was missing choices[0].message.content"
            ) from exc
        if not isinstance(content, str):
            raise AIProviderError("the model returned a non-text response")
        return content


class GeminiProvider(Provider):
    """Google Gemini via the ``generateContent`` REST API."""

    name = "gemini"
    is_live = True

    _ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def complete(
        self, system: str, user: str, *, temperature: float = 0.7, timeout: float = 45.0
    ) -> str:
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": 1_400,
                "responseMimeType": "application/json",
            },
        }
        data = _post_json(
            self._ENDPOINT.format(model=self._model),
            payload,
            {"x-goog-api-key": self._api_key},
            timeout=timeout,
        )
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        except (KeyError, IndexError, TypeError) as exc:
            raise AIProviderError(
                "the Gemini response was missing candidates[0].content.parts"
            ) from exc
        if not text:
            raise AIProviderError("Gemini returned no text (possibly filtered or empty)")
        return text


class MockProvider(Provider):
    """Deterministic offline stand-in used by tests and the no-key demo.

    Returns valid JSON matching the ideas schema so the full validation and
    grounding path is exercised without a network call.
    """

    name = "mock"
    is_live = False

    _RESPONSE = json.dumps(
        [
            {
                "name": "Warm Lantern Evening",
                "tagline": "String lights, low tables and a long shared meal.",
                "palette": ["amber", "cream", "olive"],
                "decor": "Paper lanterns, linen runners and low floral clusters.",
                "menu": "A shared family-style menu with one live counter.",
                "favors": "A small hand-poured candle.",
                "attire": "Comfortable and warm.",
                "music": "A mellow acoustic set.",
                "activities": "A memory wall and a short group toast.",
                "estimated_delta_pct": 1.0,
            },
            {
                "name": "Garden Afternoon",
                "tagline": "Open air, green tones and an unhurried pace.",
                "palette": ["sage", "white", "gold"],
                "decor": "Potted greenery, festoon lights and a welcome arch.",
                "menu": "A grazing table with a chilled dessert course.",
                "favors": "A sapling in a small pot.",
                "attire": "Smart-casual and sun-friendly.",
                "music": "An acoustic duo.",
                "activities": "Lawn games and a group photo under the lights.",
                "estimated_delta_pct": 2.0,
            },
        ],
        indent=2,
    )

    def complete(
        self, system: str, user: str, *, temperature: float = 0.7, timeout: float = 45.0
    ) -> str:
        return self._RESPONSE


class NullProvider(Provider):
    """Fails with an actionable message when no provider is configured."""

    name = "unconfigured"
    is_live = False

    def complete(
        self, system: str, user: str, *, temperature: float = 0.7, timeout: float = 45.0
    ) -> str:
        raise AIConfigError(
            "no language-model provider is configured. Set SUTRADHAR_OPENAI_API_KEY or "
            "SUTRADHAR_GEMINI_API_KEY (see .env.example), or run with the catalogue-only "
            "planner which needs no key at all."
        )


def resolve_provider(settings: Settings) -> Provider:
    """Pick the provider named by ``settings``, honouring ``auto``.

    ``auto`` selects the first configured live provider and otherwise falls back
    to :class:`MockProvider` - never to an exception - so the planner always
    produces a complete, useful answer.
    """
    choice = settings.provider

    if choice == "mock":
        return MockProvider()
    if choice == "openai":
        return (
            OpenAICompatProvider(
                settings.openai_api_key or "",
                settings.openai_base_url,
                settings.openai_model,
            )
            if settings.has_openai
            else NullProvider()
        )
    if choice == "gemini":
        return (
            GeminiProvider(settings.gemini_api_key or "", settings.gemini_model)
            if settings.has_gemini
            else NullProvider()
        )

    if settings.has_openai:
        return OpenAICompatProvider(
            settings.openai_api_key or "", settings.openai_base_url, settings.openai_model
        )
    if settings.has_gemini:
        return GeminiProvider(settings.gemini_api_key or "", settings.gemini_model)
    return MockProvider()
