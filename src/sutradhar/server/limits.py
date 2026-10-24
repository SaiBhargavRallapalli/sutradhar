"""Bounded, thread-safe admission controls for the public HTTP service."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateDecision:
    allowed: bool
    retry_after: int = 0


class RateLimiter:
    """Sliding-window per-client limiter with bounded client cardinality.

    The old implementation retained one dictionary key for every address ever
    seen. This LRU form evicts expired and least-recently-used clients, so a
    spoofed-address flood cannot grow process memory without bound.
    """

    def __init__(
        self,
        limit: int,
        window_s: float,
        *,
        max_clients: int = 4_096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window_s <= 0 or max_clients < 1:
            raise ValueError("rate limit, window and max_clients must be positive")
        self._limit = limit
        self._window = window_s
        self._max_clients = max_clients
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def check(self, key: str) -> RateDecision:
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            bucket = self._hits.pop(key, deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self._limit:
                self._hits[key] = bucket
                retry = max(1, int(self._window - (now - bucket[0]) + 0.999))
                return RateDecision(False, retry)
            bucket.append(now)
            self._hits[key] = bucket
            while len(self._hits) > self._max_clients:
                self._hits.popitem(last=False)
            return RateDecision(True)

    def allow(self, key: str) -> bool:
        """Compatibility convenience for callers that need only yes/no."""
        return self.check(key).allowed

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._hits)


__all__ = ["RateDecision", "RateLimiter"]
