"""Bounded resilience controls for live model providers.

Successful identical calls are cached, concurrent duplicates share one
in-flight request, and repeated provider failures open a short circuit breaker.
The wrapper never stores raw prompts as keys and never caches failures.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from ..exceptions import AIProviderError
from .provider import Provider


@dataclass(frozen=True, slots=True)
class ProviderStats:
    upstream_calls: int
    cache_hits: int
    shared_calls: int
    rejected_calls: int
    cache_entries: int


@dataclass(slots=True)
class _CacheEntry:
    value: str
    expires_at: float


@dataclass(slots=True)
class _Pending:
    event: threading.Event
    value: str | None = None
    error: AIProviderError | None = None


class ResilientProvider(Provider):
    """Thread-safe decorator for one live provider."""

    def __init__(
        self,
        inner: Provider,
        *,
        max_entries: int = 128,
        ttl_s: float = 21_600,
        daily_cap: int = 500,
        failure_threshold: int = 3,
        cool_down_s: float = 60,
        clock: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if min(max_entries, daily_cap, failure_threshold) < 1 or min(ttl_s, cool_down_s) <= 0:
            raise ValueError("resilience limits must be positive")
        self._inner = inner
        self.name = inner.name
        self.is_live = inner.is_live
        self._max_entries = max_entries
        self._ttl_s = ttl_s
        self._daily_cap = daily_cap
        self._failure_threshold = failure_threshold
        self._cool_down_s = cool_down_s
        self._clock = clock
        self._utc_now = utc_now
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._pending: dict[str, _Pending] = {}
        self._day = ""
        self._daily_used = 0
        self._failures = 0
        self._open_until = 0.0
        self._probe_in_flight = False
        self._upstream_calls = 0
        self._cache_hits = 0
        self._shared_calls = 0
        self._rejected_calls = 0

    @property
    def model(self) -> str:
        return self._inner.model

    def is_configured(self) -> bool:
        return self._inner.is_configured()

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.7,
        timeout: float = 45.0,
    ) -> str:
        key = self._key(system, user, temperature)
        pending, leader = self._admit(key)
        if not leader:
            if not pending.event.wait(timeout=max(1.0, timeout + 1.0)):
                raise AIProviderError("timed out waiting for an identical in-flight model call")
            if pending.error is not None:
                raise AIProviderError(str(pending.error))
            if pending.value is None:  # pragma: no cover - defensive invariant
                raise AIProviderError("the shared model call completed without a result")
            return pending.value

        try:
            value = self._inner.complete(
                system,
                user,
                temperature=temperature,
                timeout=timeout,
            )
        except AIProviderError as exc:
            self._finish_failure(key, pending, exc)
            raise
        except Exception as exc:  # pragma: no cover - provider contract defense
            safe = AIProviderError("the model provider failed unexpectedly")
            self._finish_failure(key, pending, safe)
            raise safe from exc
        self._finish_success(key, pending, value)
        return value

    def snapshot(self) -> ProviderStats:
        with self._lock:
            return ProviderStats(
                upstream_calls=self._upstream_calls,
                cache_hits=self._cache_hits,
                shared_calls=self._shared_calls,
                rejected_calls=self._rejected_calls,
                cache_entries=len(self._cache),
            )

    def _key(self, system: str, user: str, temperature: float) -> str:
        material = "\0".join((self.name, self.model, f"{temperature:.4f}", system, user))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _admit(self, key: str) -> tuple[_Pending, bool]:
        now = self._clock()
        with self._lock:
            entry = self._cache.pop(key, None)
            if entry is not None and entry.expires_at > now:
                self._cache[key] = entry
                self._cache_hits += 1
                ready = _Pending(threading.Event(), value=entry.value)
                ready.event.set()
                return ready, False

            pending = self._pending.get(key)
            if pending is not None:
                self._shared_calls += 1
                return pending, False

            self._roll_day()
            if not self._breaker_allows(now):
                self._rejected_calls += 1
                raise AIProviderError("model circuit breaker is open; using catalogue fallback")
            if self._daily_used >= self._daily_cap:
                self._rejected_calls += 1
                raise AIProviderError("daily model-call cap reached; using catalogue fallback")

            self._daily_used += 1
            self._upstream_calls += 1
            pending = _Pending(threading.Event())
            self._pending[key] = pending
            return pending, True

    def _roll_day(self) -> None:
        today = self._utc_now().date().isoformat()
        if today != self._day:
            self._day = today
            self._daily_used = 0

    def _breaker_allows(self, now: float) -> bool:
        if self._open_until == 0:
            return True
        if now < self._open_until or self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def _finish_success(self, key: str, pending: _Pending, value: str) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0
            self._probe_in_flight = False
            self._cache[key] = _CacheEntry(value, self._clock() + self._ttl_s)
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)
            self._pending.pop(key, None)
            pending.value = value
            pending.event.set()

    def _finish_failure(self, key: str, pending: _Pending, error: AIProviderError) -> None:
        with self._lock:
            self._failures += 1
            self._probe_in_flight = False
            if self._failures >= self._failure_threshold:
                self._open_until = self._clock() + self._cool_down_s
                self._failures = self._failure_threshold
            self._pending.pop(key, None)
            pending.error = error
            pending.event.set()


__all__ = ["ProviderStats", "ResilientProvider"]
