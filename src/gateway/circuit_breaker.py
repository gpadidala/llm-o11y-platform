"""Circuit breaker pattern for provider resilience.

Implements the standard three-state circuit breaker:

- **CLOSED** -- Normal operation.  Failures are counted.
- **OPEN** -- The provider has exceeded the failure threshold.  All requests
  are rejected immediately (fail-fast) until the recovery timeout expires.
- **HALF_OPEN** -- After the recovery timeout, a limited number of probe
  requests are allowed through.  If they succeed the circuit closes; if they
  fail the circuit re-opens.

Each provider (identified by a string key such as ``"openai:gpt-4o"``) gets
its own independent circuit.  The module-level ``circuit_breaker`` singleton
is thread-safe.
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------


class CircuitState(str, Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# ---------------------------------------------------------------------------
# Per-provider circuit state
# ---------------------------------------------------------------------------


class _CircuitData:
    """Internal state for a single circuit."""

    __slots__ = (
        "state",
        "failure_count",
        "success_count",
        "last_failure_time",
        "opened_at",
        "half_open_successes",
        "half_open_failures",
        "total_requests",
        "total_failures",
        "total_successes",
        "state_changes",
    )

    def __init__(self) -> None:
        self.state: CircuitState = CircuitState.CLOSED
        self.failure_count: int = 0
        self.success_count: int = 0
        self.last_failure_time: float = 0.0
        self.opened_at: float = 0.0
        self.half_open_successes: int = 0
        self.half_open_failures: int = 0
        # Lifetime counters for monitoring
        self.total_requests: int = 0
        self.total_failures: int = 0
        self.total_successes: int = 0
        self.state_changes: int = 0


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Per-provider circuit breaker for resilient upstream calls.

    Thread-safe: all mutable state is protected by ``_lock``.

    Args:
        failure_threshold: Number of consecutive failures before the circuit
            opens.
        recovery_timeout: Seconds to wait in OPEN state before transitioning
            to HALF_OPEN.
        half_open_max: Number of successful probe requests required in
            HALF_OPEN to close the circuit again.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max: int = 3,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max = half_open_max
        self._circuits: dict[str, _CircuitData] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_execute(self, provider: str) -> bool:
        """Check if the circuit allows execution for *provider*.

        Returns ``True`` if the circuit is CLOSED or HALF_OPEN (within the
        probe allowance).  Automatically transitions from OPEN to HALF_OPEN
        when the recovery timeout has elapsed.
        """
        with self._lock:
            circuit = self._get_or_create(provider)
            self._maybe_transition(circuit)

            if circuit.state == CircuitState.CLOSED:
                return True
            elif circuit.state == CircuitState.HALF_OPEN:
                # Allow a limited number of probes
                probes_in_flight = circuit.half_open_successes + circuit.half_open_failures
                return probes_in_flight < self._half_open_max
            else:
                # OPEN
                return False

    def record_success(self, provider: str) -> None:
        """Record a successful call, potentially closing the circuit."""
        with self._lock:
            circuit = self._get_or_create(provider)
            circuit.total_requests += 1
            circuit.total_successes += 1

            if circuit.state == CircuitState.HALF_OPEN:
                circuit.half_open_successes += 1
                if circuit.half_open_successes >= self._half_open_max:
                    self._transition(circuit, CircuitState.CLOSED)
            elif circuit.state == CircuitState.CLOSED:
                # Reset consecutive failure counter on success
                circuit.failure_count = 0
                circuit.success_count += 1

    def record_failure(self, provider: str) -> None:
        """Record a failed call, potentially opening the circuit."""
        with self._lock:
            circuit = self._get_or_create(provider)
            circuit.total_requests += 1
            circuit.total_failures += 1
            circuit.last_failure_time = time.monotonic()

            if circuit.state == CircuitState.HALF_OPEN:
                circuit.half_open_failures += 1
                # Any failure in half-open re-opens the circuit
                self._transition(circuit, CircuitState.OPEN)
            elif circuit.state == CircuitState.CLOSED:
                circuit.failure_count += 1
                if circuit.failure_count >= self._failure_threshold:
                    self._transition(circuit, CircuitState.OPEN)

    def get_state(self, provider: str) -> CircuitState:
        """Get the current circuit state for *provider*."""
        with self._lock:
            circuit = self._get_or_create(provider)
            self._maybe_transition(circuit)
            return circuit.state

    def get_all_states(self) -> dict:
        """Get all circuit states for monitoring dashboards."""
        with self._lock:
            result: dict = {}
            for provider, circuit in self._circuits.items():
                self._maybe_transition(circuit)
                result[provider] = {
                    "state": circuit.state.value,
                    "failure_count": circuit.failure_count,
                    "total_requests": circuit.total_requests,
                    "total_failures": circuit.total_failures,
                    "total_successes": circuit.total_successes,
                    "state_changes": circuit.state_changes,
                    "last_failure": circuit.last_failure_time,
                }
            return result

    def reset(self, provider: str) -> None:
        """Manually reset a circuit to CLOSED.  Useful for admin overrides."""
        with self._lock:
            circuit = self._get_or_create(provider)
            self._transition(circuit, CircuitState.CLOSED)

    def reset_all(self) -> None:
        """Reset all circuits to CLOSED."""
        with self._lock:
            for circuit in self._circuits.values():
                self._transition(circuit, CircuitState.CLOSED)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create(self, provider: str) -> _CircuitData:
        """Retrieve or initialise circuit data.  Must hold ``_lock``."""
        if provider not in self._circuits:
            self._circuits[provider] = _CircuitData()
        return self._circuits[provider]

    def _maybe_transition(self, circuit: _CircuitData) -> None:
        """Check if an OPEN circuit should move to HALF_OPEN.  Must hold ``_lock``."""
        if circuit.state == CircuitState.OPEN:
            elapsed = time.monotonic() - circuit.opened_at
            if elapsed >= self._recovery_timeout:
                self._transition(circuit, CircuitState.HALF_OPEN)

    def _transition(self, circuit: _CircuitData, new_state: CircuitState) -> None:
        """Perform a state transition.  Must hold ``_lock``."""
        if circuit.state == new_state:
            return
        circuit.state = new_state
        circuit.state_changes += 1
        if new_state == CircuitState.OPEN:
            circuit.opened_at = time.monotonic()
        elif new_state == CircuitState.HALF_OPEN:
            circuit.half_open_successes = 0
            circuit.half_open_failures = 0
        elif new_state == CircuitState.CLOSED:
            circuit.failure_count = 0
            circuit.success_count = 0
            circuit.half_open_successes = 0
            circuit.half_open_failures = 0


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

circuit_breaker = CircuitBreaker()
