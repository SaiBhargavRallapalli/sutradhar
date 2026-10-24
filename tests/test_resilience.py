"""Concurrency and failure-budget tests for the live-provider decorator."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from sutradhar.ai.provider import Provider
from sutradhar.ai.resilience import ResilientProvider
from sutradhar.exceptions import AIProviderError


class RecordingProvider(Provider):
    name = "recording"
    is_live = True

    def __init__(self, outcomes: list[str | Exception] | None = None, *, delay: float = 0) -> None:
        self.outcomes = list(outcomes or ["ok"])
        self.delay = delay
        self.calls = 0
        self.lock = threading.Lock()

    @property
    def model(self) -> str:
        return "test-model"

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.7,
        timeout: float = 45.0,
    ) -> str:
        del system, user, temperature, timeout
        with self.lock:
            index = self.calls
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        outcome = self.outcomes[min(index, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_successful_identical_calls_are_cached() -> None:
    inner = RecordingProvider(["answer"])
    provider = ResilientProvider(inner)

    assert provider.complete("system", "same") == "answer"
    assert provider.complete("system", "same") == "answer"
    assert inner.calls == 1
    assert provider.snapshot().cache_hits == 1


def test_concurrent_duplicates_share_one_upstream_call() -> None:
    inner = RecordingProvider(["shared"], delay=0.05)
    provider = ResilientProvider(inner)
    gate = threading.Barrier(8)

    def invoke() -> str:
        gate.wait()
        return provider.complete("system", "same", timeout=2)

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: invoke(), range(8))) == ["shared"] * 8

    assert inner.calls == 1
    assert provider.snapshot().shared_calls == 7


def test_failures_are_not_cached() -> None:
    inner = RecordingProvider([AIProviderError("temporary"), "recovered"])
    provider = ResilientProvider(inner, failure_threshold=3)

    with pytest.raises(AIProviderError, match="temporary"):
        provider.complete("system", "same")
    assert provider.complete("system", "same") == "recovered"
    assert inner.calls == 2


def test_daily_cap_rejects_new_calls_but_not_cache_hits() -> None:
    inner = RecordingProvider(["one", "two"])
    provider = ResilientProvider(inner, daily_cap=2)

    assert provider.complete("system", "one") == "one"
    assert provider.complete("system", "one") == "one"
    assert provider.complete("system", "two") == "two"
    with pytest.raises(AIProviderError, match="daily model-call cap"):
        provider.complete("system", "three")
    assert inner.calls == 2


def test_circuit_breaker_fails_fast_then_allows_probe() -> None:
    now = [10.0]
    inner = RecordingProvider([AIProviderError("down"), AIProviderError("still down"), "recovered"])
    provider = ResilientProvider(
        inner,
        failure_threshold=2,
        cool_down_s=30,
        clock=lambda: now[0],
        utc_now=lambda: datetime(2026, 10, 24, tzinfo=timezone.utc),
    )

    for prompt in ("one", "two"):
        with pytest.raises(AIProviderError):
            provider.complete("system", prompt)
    with pytest.raises(AIProviderError, match="circuit breaker"):
        provider.complete("system", "three")
    assert inner.calls == 2

    now[0] += 31
    assert provider.complete("system", "probe") == "recovered"
    assert inner.calls == 3


def test_ttl_expiry_and_lru_bound() -> None:
    now = [1.0]
    inner = RecordingProvider(["a", "b", "c", "a-new"])
    provider = ResilientProvider(
        inner,
        max_entries=2,
        ttl_s=10,
        clock=lambda: now[0],
    )

    assert provider.complete("s", "a") == "a"
    assert provider.complete("s", "b") == "b"
    assert provider.complete("s", "c") == "c"
    assert provider.snapshot().cache_entries == 2
    now[0] += 11
    assert provider.complete("s", "a") == "a-new"
    assert inner.calls == 4
