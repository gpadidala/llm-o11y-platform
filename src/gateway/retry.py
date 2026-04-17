"""Retry logic with exponential backoff and jitter.

Provides a single ``retry_with_backoff`` coroutine that wraps an async
callable with configurable retry behaviour.  Retries are triggered only for
errors whose type name (lowercased) matches one of the configured
``retryable_errors`` patterns.

The default retry config uses exponential backoff (base * multiplier^attempt)
capped at ``max_delay``, with optional full-jitter to prevent thundering herds.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Callable, Optional

import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class RetryConfig(BaseModel):
    """Retry behaviour configuration."""

    max_retries: int = 3
    initial_delay: float = 0.5  # seconds
    max_delay: float = 30.0  # seconds
    backoff_multiplier: float = 2.0
    jitter: bool = True
    retryable_errors: list[str] = [
        "rate_limit",
        "timeout",
        "server_error",
        "connection",
        "unavailable",
        "429",
        "500",
        "502",
        "503",
        "504",
    ]


# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = RetryConfig()


# ---------------------------------------------------------------------------
# Retry result (returned alongside the actual result for telemetry)
# ---------------------------------------------------------------------------


class RetryResult(BaseModel):
    """Metadata about the retry execution."""

    attempts: int = 1
    total_delay_ms: float = 0.0
    final_error: Optional[str] = None
    success: bool = True


# ---------------------------------------------------------------------------
# Core retry function
# ---------------------------------------------------------------------------


def _is_retryable(error: Exception, patterns: list[str]) -> bool:
    """Determine whether *error* should trigger a retry.

    Checks the error's class name, string representation, and any ``status_code``
    attribute against the configured patterns.
    """
    error_text = f"{type(error).__name__} {str(error)}".lower()

    # Also check status_code if present (common on HTTP errors)
    status_code = getattr(error, "status_code", None)
    if status_code is not None:
        error_text += f" {status_code}"

    for pattern in patterns:
        if pattern.lower() in error_text:
            return True
    return False


def _compute_delay(
    attempt: int,
    initial: float,
    multiplier: float,
    max_delay: float,
    jitter: bool,
) -> float:
    """Compute the delay before the next retry.

    Uses exponential backoff: ``initial * multiplier ^ attempt``, capped at
    ``max_delay``.  When *jitter* is enabled, the actual delay is a random
    value between 0 and the computed delay (full jitter).
    """
    delay = min(initial * (multiplier ** attempt), max_delay)
    if jitter:
        delay = random.uniform(0, delay)
    return delay


async def retry_with_backoff(
    func: Callable[..., Any],
    config: RetryConfig | None = None,
    **kwargs: Any,
) -> Any:
    """Execute *func* with retry and exponential backoff.

    Args:
        func: An async callable to execute.
        config: Retry configuration.  Uses sensible defaults if ``None``.
        **kwargs: Keyword arguments passed to *func*.

    Returns:
        The return value of *func* on success.

    Raises:
        The last exception if all retries are exhausted.
    """
    cfg = config or _DEFAULT_CONFIG
    last_error: Optional[Exception] = None
    total_delay = 0.0

    for attempt in range(cfg.max_retries + 1):
        try:
            result = await func(**kwargs)
            if attempt > 0:
                logger.info(
                    "retry_succeeded",
                    attempt=attempt + 1,
                    total_delay_ms=round(total_delay * 1000, 2),
                )
            return result

        except Exception as exc:
            last_error = exc

            # Check if we should retry
            if attempt >= cfg.max_retries:
                logger.warning(
                    "retry_exhausted",
                    attempts=attempt + 1,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    total_delay_ms=round(total_delay * 1000, 2),
                )
                raise

            if not _is_retryable(exc, cfg.retryable_errors):
                logger.warning(
                    "retry_skipped_non_retryable",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                raise

            # Compute delay and sleep
            delay = _compute_delay(
                attempt=attempt,
                initial=cfg.initial_delay,
                multiplier=cfg.backoff_multiplier,
                max_delay=cfg.max_delay,
                jitter=cfg.jitter,
            )
            total_delay += delay

            logger.info(
                "retry_attempt",
                attempt=attempt + 1,
                max_retries=cfg.max_retries,
                delay_s=round(delay, 3),
                error=str(exc),
                error_type=type(exc).__name__,
            )

            await asyncio.sleep(delay)

    # Should not reach here, but safety net
    if last_error is not None:
        raise last_error
    raise RuntimeError("retry_with_backoff: unexpected state -- no result and no error")
