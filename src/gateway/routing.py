"""Intelligent request routing with multiple strategies.

Supports six routing strategies:
- SINGLE: Direct to one provider/model
- FALLBACK: Try providers in priority order until one succeeds
- LOADBALANCE: Weighted round-robin distribution across providers
- COST_OPTIMIZED: Route to the cheapest available provider for the request
- LATENCY_OPTIMIZED: Route to the provider with the lowest observed latency
- CANARY: Split traffic between a primary and canary target for A/B testing

The module-level ``routing_engine`` singleton maintains latency history and
error counts, enabling adaptive routing decisions that improve over time.
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from enum import Enum
from typing import Optional

from pydantic import BaseModel

from src.providers.base import MODEL_PRICING


# ---------------------------------------------------------------------------
# Configuration models
# ---------------------------------------------------------------------------


class RoutingStrategy(str, Enum):
    """Available routing strategies."""

    SINGLE = "single"
    FALLBACK = "fallback"
    LOADBALANCE = "loadbalance"
    COST_OPTIMIZED = "cost"
    LATENCY_OPTIMIZED = "latency"
    CANARY = "canary"


class RouteTarget(BaseModel):
    """A single routing destination."""

    provider: str
    model: str
    weight: float = 1.0


class RoutingConfig(BaseModel):
    """Configuration for a routing decision."""

    strategy: RoutingStrategy = RoutingStrategy.SINGLE
    targets: list[RouteTarget]
    max_retries: int = 1
    canary_weight: float = 10.0  # percentage of traffic to canary (second target)


# ---------------------------------------------------------------------------
# Routing engine
# ---------------------------------------------------------------------------

# Maximum number of latency samples kept per provider for rolling average.
_LATENCY_WINDOW = 100


class RoutingEngine:
    """Routes requests based on strategy, performance history, and cost.

    Thread-safe: all mutable state is protected by a single lock.  The lock
    scope is kept small so hot paths (select_target) are fast.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # provider -> deque of recent latencies (ms)
        self._latency_history: dict[str, deque[float]] = {}
        # provider -> cumulative error count
        self._error_counts: dict[str, int] = {}
        # provider -> cumulative request count
        self._request_counts: dict[str, int] = {}
        # Round-robin index used by LOADBALANCE
        self._rr_index: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select_target(self, config: RoutingConfig) -> RouteTarget:
        """Select the best target based on the routing strategy.

        Raises ``ValueError`` if *config.targets* is empty.
        """
        if not config.targets:
            raise ValueError("RoutingConfig must contain at least one target")

        strategy = config.strategy

        if strategy == RoutingStrategy.SINGLE:
            return self._strategy_single(config)
        elif strategy == RoutingStrategy.FALLBACK:
            return self._strategy_fallback(config)
        elif strategy == RoutingStrategy.LOADBALANCE:
            return self._strategy_loadbalance(config)
        elif strategy == RoutingStrategy.COST_OPTIMIZED:
            return self._strategy_cost(config)
        elif strategy == RoutingStrategy.LATENCY_OPTIMIZED:
            return self._strategy_latency(config)
        elif strategy == RoutingStrategy.CANARY:
            return self._strategy_canary(config)
        else:
            # Fallback for any future additions
            return config.targets[0]

    def record_result(
        self,
        provider: str,
        model: str,
        latency_ms: float,
        success: bool,
    ) -> None:
        """Record the outcome of a request for adaptive routing."""
        key = f"{provider}:{model}"
        with self._lock:
            # Latency
            if key not in self._latency_history:
                self._latency_history[key] = deque(maxlen=_LATENCY_WINDOW)
            self._latency_history[key].append(latency_ms)

            # Counts
            self._request_counts[key] = self._request_counts.get(key, 0) + 1
            if not success:
                self._error_counts[key] = self._error_counts.get(key, 0) + 1

    def get_stats(self) -> dict:
        """Return a snapshot of routing statistics."""
        with self._lock:
            stats: dict = {}
            all_keys = set(self._request_counts.keys()) | set(self._latency_history.keys())
            for key in sorted(all_keys):
                latencies = list(self._latency_history.get(key, []))
                avg_latency = sum(latencies) / len(latencies) if latencies else None
                stats[key] = {
                    "requests": self._request_counts.get(key, 0),
                    "errors": self._error_counts.get(key, 0),
                    "avg_latency_ms": round(avg_latency, 2) if avg_latency is not None else None,
                    "p50_latency_ms": round(sorted(latencies)[len(latencies) // 2], 2) if latencies else None,
                    "p99_latency_ms": round(
                        sorted(latencies)[int(len(latencies) * 0.99)], 2
                    ) if latencies else None,
                    "sample_count": len(latencies),
                }
            return stats

    # ------------------------------------------------------------------
    # Strategy implementations
    # ------------------------------------------------------------------

    def _strategy_single(self, config: RoutingConfig) -> RouteTarget:
        """Return the first target."""
        return config.targets[0]

    def _strategy_fallback(self, config: RoutingConfig) -> RouteTarget:
        """Return the first target whose error rate is below 50%.

        Targets are evaluated in list order.  If all targets have high error
        rates the first target is returned (caller will attempt it anyway and
        the circuit breaker / retry layers handle actual failures).
        """
        with self._lock:
            for target in config.targets:
                key = f"{target.provider}:{target.model}"
                total = self._request_counts.get(key, 0)
                errors = self._error_counts.get(key, 0)
                # Give the benefit of the doubt to targets with no history
                if total == 0 or (errors / total) < 0.5:
                    return target
        # All targets look unhealthy -- return first (best effort)
        return config.targets[0]

    def _strategy_loadbalance(self, config: RoutingConfig) -> RouteTarget:
        """Weighted round-robin across targets.

        Each call advances a shared counter.  The counter position is mapped
        to a target according to cumulative weight.  This gives O(n) selection
        but n is tiny (number of providers).
        """
        targets = config.targets
        total_weight = sum(t.weight for t in targets)
        if total_weight <= 0:
            return random.choice(targets)

        with self._lock:
            self._rr_index += 1
            position = self._rr_index

        # Map position to a target via cumulative weight
        slot = position % int(total_weight * 100)  # scale for fractional weights
        cumulative = 0.0
        for target in targets:
            cumulative += target.weight * 100
            if slot < cumulative:
                return target
        # Rounding guard
        return targets[-1]

    def _strategy_cost(self, config: RoutingConfig) -> RouteTarget:
        """Route to the cheapest provider based on MODEL_PRICING.

        Cost is estimated as the sum of input + output price.  Targets whose
        model is not in MODEL_PRICING are assigned a high sentinel cost so
        known-cheap models are preferred.
        """
        _SENTINEL_COST = 999_999.0

        def _cost_score(target: RouteTarget) -> float:
            pricing = MODEL_PRICING.get(target.model)
            if pricing is None:
                return _SENTINEL_COST
            input_price, output_price = pricing
            return input_price + output_price

        ranked = sorted(config.targets, key=_cost_score)
        return ranked[0]

    def _strategy_latency(self, config: RoutingConfig) -> RouteTarget:
        """Route to the provider with the lowest observed average latency.

        Targets without latency history are assigned a neutral score of
        500 ms so they get a fair chance to be tested.
        """
        _DEFAULT_LATENCY = 500.0

        def _avg_latency(target: RouteTarget) -> float:
            key = f"{target.provider}:{target.model}"
            with self._lock:
                history = self._latency_history.get(key)
                if not history:
                    return _DEFAULT_LATENCY
                return sum(history) / len(history)

        ranked = sorted(config.targets, key=_avg_latency)
        return ranked[0]

    def _strategy_canary(self, config: RoutingConfig) -> RouteTarget:
        """Split traffic: primary (first target) gets most, canary (second) gets the rest.

        ``config.canary_weight`` is the percentage [0-100] of requests that
        should go to the canary (second target).  If only one target is
        defined, 100% of traffic goes to it.
        """
        if len(config.targets) < 2:
            return config.targets[0]

        roll = random.uniform(0, 100)
        if roll < config.canary_weight:
            return config.targets[1]  # canary
        return config.targets[0]  # primary


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

routing_engine = RoutingEngine()
