"""Client-side throttling so we stay inside provider quotas by construction."""
from __future__ import annotations

import threading
import time as _time
from collections import deque


class TokenBucket:
    """Sliding-window limiter shared across threads.

    `acquire` blocks until a slot is free rather than raising, because the
    caller's alternative is to hammer the provider and earn a hard ban.
    """

    def __init__(self, rate_per_minute: int, *, name: str = "default"):
        self.name = name
        self.capacity = max(1, rate_per_minute)
        self.window = 60.0
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._hits and self._hits[0] <= cutoff:
            self._hits.popleft()

    def try_acquire(self) -> bool:
        with self._lock:
            now = _time.monotonic()
            self._prune(now)
            if len(self._hits) < self.capacity:
                self._hits.append(now)
                return True
            return False

    def acquire(self, timeout: float = 60.0) -> bool:
        deadline = _time.monotonic() + timeout
        while True:
            with self._lock:
                now = _time.monotonic()
                self._prune(now)
                if len(self._hits) < self.capacity:
                    self._hits.append(now)
                    return True
                wait = self._hits[0] + self.window - now
            if _time.monotonic() + wait > deadline:
                return False
            _time.sleep(min(max(wait, 0.01), 1.0))

    @property
    def used(self) -> int:
        with self._lock:
            self._prune(_time.monotonic())
            return len(self._hits)


_buckets: dict[str, TokenBucket] = {}
_registry_lock = threading.Lock()


def get_bucket(name: str, rate_per_minute: int) -> TokenBucket:
    with _registry_lock:
        bucket = _buckets.get(name)
        if bucket is None or bucket.capacity != max(1, rate_per_minute):
            bucket = TokenBucket(rate_per_minute, name=name)
            _buckets[name] = bucket
        return bucket
