"""Token-bucket rate limiter with sliding window counters.

Supports per-key limits on multiple dimensions simultaneously:

- requests per minute / hour / day
- tokens per minute / day
- maximum concurrent requests

Each dimension is independently enforced.  A request is allowed only if
**all** applicable dimensions have remaining capacity.

The module-level ``rate_limiter`` singleton is thread-safe and ready for use
across the gateway middleware pipeline.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Configuration and result models
# ---------------------------------------------------------------------------


class RateLimitConfig(BaseModel):
    """Rate limit thresholds for a single key."""

    requests_per_minute: Optional[int] = None
    requests_per_hour: Optional[int] = None
    requests_per_day: Optional[int] = None
    tokens_per_minute: Optional[int] = None
    tokens_per_day: Optional[int] = None
    max_concurrent: Optional[int] = None


class RateLimitResult(BaseModel):
    """Outcome of a rate-limit check or consume call."""

    allowed: bool
    limit: Optional[int] = None
    remaining: Optional[int] = None
    reset_at: Optional[float] = None
    retry_after: Optional[float] = None
    denied_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------


class TokenBucket:
    """Classic token-bucket implementation.

    Tokens are replenished continuously at ``rate`` tokens per second up to
    ``capacity``.  Each ``consume(n)`` call attempts to remove *n* tokens.
    """

    def __init__(self, capacity: int, rate: float) -> None:
        """
        Args:
            capacity: Maximum number of tokens the bucket can hold.
            rate: Tokens added per second.
        """
        self.capacity = capacity
        self.rate = rate
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now

    def consume(self, n: int = 1) -> bool:
        """Try to consume *n* tokens. Returns ``True`` on success."""
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def peek(self, n: int = 1) -> bool:
        """Check if *n* tokens are available **without** consuming them."""
        self._refill()
        return self.tokens >= n

    @property
    def remaining(self) -> int:
        self._refill()
        return int(self.tokens)

    @property
    def time_to_available(self) -> float:
        """Seconds until at least 1 token is available."""
        self._refill()
        if self.tokens >= 1:
            return 0.0
        return (1 - self.tokens) / self.rate if self.rate > 0 else float("inf")


# ---------------------------------------------------------------------------
# Sliding window counter
# ---------------------------------------------------------------------------


class SlidingWindowCounter:
    """Approximate sliding-window counter for rate limiting.

    Divides the window into fixed-size sub-buckets and sums recent counts.
    Accuracy improves with more sub-buckets but memory usage grows.
    """

    def __init__(self, window_seconds: float, limit: int, num_buckets: int = 10) -> None:
        self.window = window_seconds
        self.limit = limit
        self.num_buckets = num_buckets
        self.bucket_size = window_seconds / num_buckets
        self._buckets: list[int] = [0] * num_buckets
        self._bucket_timestamps: list[float] = [0.0] * num_buckets
        self._current_bucket = 0
        self._last_update = time.monotonic()

    def _advance(self) -> None:
        """Advance internal pointer and clear stale buckets."""
        now = time.monotonic()
        elapsed = now - self._last_update
        buckets_to_advance = int(elapsed / self.bucket_size)
        if buckets_to_advance <= 0:
            return
        # Clear buckets that have rotated out
        for i in range(min(buckets_to_advance, self.num_buckets)):
            idx = (self._current_bucket + 1 + i) % self.num_buckets
            self._buckets[idx] = 0
            self._bucket_timestamps[idx] = now
        self._current_bucket = (self._current_bucket + buckets_to_advance) % self.num_buckets
        self._last_update = now

    def record(self, count: int = 1) -> None:
        """Record *count* events in the current window."""
        self._advance()
        self._buckets[self._current_bucket] += count

    def current_count(self) -> int:
        """Return the approximate total count within the sliding window."""
        self._advance()
        return sum(self._buckets)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.current_count())

    def allows(self, count: int = 1) -> bool:
        return (self.current_count() + count) <= self.limit

    @property
    def reset_at(self) -> float:
        """Approximate wall-clock time when the oldest bucket expires."""
        return time.time() + self.bucket_size


# ---------------------------------------------------------------------------
# Per-key state
# ---------------------------------------------------------------------------


class _KeyState:
    """Rate limit state for a single key, spanning all dimensions."""

    def __init__(self, config: RateLimitConfig) -> None:
        self.config = config
        self.windows: dict[str, SlidingWindowCounter] = {}
        self.token_windows: dict[str, SlidingWindowCounter] = {}
        self.concurrent: int = 0

        # Request windows
        if config.requests_per_minute is not None:
            self.windows["rpm"] = SlidingWindowCounter(60, config.requests_per_minute)
        if config.requests_per_hour is not None:
            self.windows["rph"] = SlidingWindowCounter(3600, config.requests_per_hour)
        if config.requests_per_day is not None:
            self.windows["rpd"] = SlidingWindowCounter(86400, config.requests_per_day)

        # Token windows
        if config.tokens_per_minute is not None:
            self.token_windows["tpm"] = SlidingWindowCounter(60, config.tokens_per_minute)
        if config.tokens_per_day is not None:
            self.token_windows["tpd"] = SlidingWindowCounter(86400, config.tokens_per_day)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Multi-dimensional rate limiter supporting per-key configuration.

    Thread-safe: all state is guarded by ``_lock``.
    """

    def __init__(self) -> None:
        self._states: dict[str, _KeyState] = {}
        self._configs: dict[str, RateLimitConfig] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_config(self, key_id: str, config: RateLimitConfig) -> None:
        """Set or update the rate-limit configuration for *key_id*."""
        with self._lock:
            self._configs[key_id] = config
            # Rebuild state from the new config
            self._states[key_id] = _KeyState(config)

    # ------------------------------------------------------------------
    # Check / consume
    # ------------------------------------------------------------------

    def check(self, key_id: str, tokens: int = 1) -> RateLimitResult:
        """Check whether a request is allowed **without** consuming capacity.

        Returns a ``RateLimitResult`` indicating whether the request would be
        allowed.  Use ``consume`` to actually deduct capacity.
        """
        with self._lock:
            state = self._states.get(key_id)
            if state is None:
                # No config for this key -- allow by default
                return RateLimitResult(allowed=True)
            return self._evaluate(state, tokens, consume=False)

    def consume(self, key_id: str, tokens: int = 1) -> RateLimitResult:
        """Check and consume capacity.  Returns the result."""
        with self._lock:
            state = self._states.get(key_id)
            if state is None:
                return RateLimitResult(allowed=True)
            return self._evaluate(state, tokens, consume=True)

    def release_concurrent(self, key_id: str) -> None:
        """Release one concurrent slot when a request finishes."""
        with self._lock:
            state = self._states.get(key_id)
            if state is not None and state.concurrent > 0:
                state.concurrent -= 1

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def get_usage(self, key_id: str) -> dict:
        """Get current usage snapshot for *key_id*."""
        with self._lock:
            state = self._states.get(key_id)
            if state is None:
                return {"configured": False}
            usage: dict = {"configured": True}
            for name, window in state.windows.items():
                usage[name] = {
                    "current": window.current_count(),
                    "limit": window.limit,
                    "remaining": window.remaining,
                }
            for name, window in state.token_windows.items():
                usage[name] = {
                    "current": window.current_count(),
                    "limit": window.limit,
                    "remaining": window.remaining,
                }
            if state.config.max_concurrent is not None:
                usage["concurrent"] = {
                    "current": state.concurrent,
                    "limit": state.config.max_concurrent,
                    "remaining": state.config.max_concurrent - state.concurrent,
                }
            return usage

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _evaluate(
        self,
        state: _KeyState,
        tokens: int,
        consume: bool,
    ) -> RateLimitResult:
        """Evaluate all dimensions.  Must hold ``_lock``."""
        # Check concurrency
        if state.config.max_concurrent is not None:
            if state.concurrent >= state.config.max_concurrent:
                return RateLimitResult(
                    allowed=False,
                    limit=state.config.max_concurrent,
                    remaining=0,
                    denied_reason="max_concurrent",
                )

        # Check request windows
        for name, window in state.windows.items():
            if not window.allows(1):
                return RateLimitResult(
                    allowed=False,
                    limit=window.limit,
                    remaining=window.remaining,
                    reset_at=window.reset_at,
                    retry_after=window.bucket_size,
                    denied_reason=name,
                )

        # Check token windows
        for name, window in state.token_windows.items():
            if not window.allows(tokens):
                return RateLimitResult(
                    allowed=False,
                    limit=window.limit,
                    remaining=window.remaining,
                    reset_at=window.reset_at,
                    retry_after=window.bucket_size,
                    denied_reason=name,
                )

        # All checks passed
        if consume:
            # Record in all request windows
            for window in state.windows.values():
                window.record(1)
            # Record tokens in token windows
            for window in state.token_windows.values():
                window.record(tokens)
            # Increment concurrent
            if state.config.max_concurrent is not None:
                state.concurrent += 1

        # Build "most restrictive" remaining count for response
        min_remaining: Optional[int] = None
        min_limit: Optional[int] = None
        min_reset: Optional[float] = None

        for window in state.windows.values():
            r = window.remaining
            if min_remaining is None or r < min_remaining:
                min_remaining = r
                min_limit = window.limit
                min_reset = window.reset_at

        return RateLimitResult(
            allowed=True,
            limit=min_limit,
            remaining=min_remaining,
            reset_at=min_reset,
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

rate_limiter = RateLimiter()
