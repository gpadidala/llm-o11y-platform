"""Gateway middleware -- the full request pipeline.

Pipeline order:
    Auth -> RateLimit -> Cache(check) -> CircuitBreaker -> Route -> Retry -> Provider -> Cache(store) -> Log

This module wires together every gateway component into a single
``gateway_pipeline`` coroutine that the router (or any other caller) can
invoke for a fully-managed LLM request.

All steps are optional and degrade gracefully.  If no virtual key is
supplied, auth/rate-limit/budget steps are skipped.  If no routing config
is provided, the request goes directly to the provider specified in the
request.
"""

from __future__ import annotations

import time
import uuid
from typing import Optional

import structlog

from src.gateway.cache import CacheMode, cache_engine
from src.gateway.circuit_breaker import CircuitState, circuit_breaker
from src.gateway.rate_limiter import rate_limiter
from src.gateway.retry import RetryConfig, retry_with_backoff
from src.gateway.routing import RouteTarget, RoutingConfig, RoutingStrategy, routing_engine
from src.gateway.virtual_keys import VirtualKey, key_manager
from src.models.telemetry import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Provider,
)
from src.providers import get_provider
from src.providers.base import ProviderError
import src.otel.setup as otel_setup

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class GatewayAuthError(Exception):
    """Raised when virtual key validation fails."""


class GatewayRateLimitError(Exception):
    """Raised when a rate limit is exceeded."""


class GatewayBudgetExceededError(Exception):
    """Raised when a virtual key's budget is exhausted."""


class GatewayCircuitOpenError(Exception):
    """Raised when the circuit breaker is open for the target provider."""


class GatewayPermissionError(Exception):
    """Raised when the virtual key lacks permission for the requested provider/model."""


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


async def gateway_pipeline(
    request: ChatCompletionRequest,
    api_key: str | None = None,
    routing_config: RoutingConfig | None = None,
    cache_mode: CacheMode = CacheMode.NONE,
    retry_config: RetryConfig | None = None,
) -> ChatCompletionResponse:
    """Execute the full gateway middleware pipeline for a request.

    Args:
        request: The incoming chat completion request.
        api_key: Optional raw virtual API key for auth/budget/rate-limit.
        routing_config: Optional routing strategy.  If ``None``, the request
            is sent directly to ``request.provider`` / ``request.model``.
        cache_mode: Cache lookup strategy (default: no caching).
        retry_config: Retry behaviour.  If ``None``, sensible defaults apply.

    Returns:
        A ``ChatCompletionResponse`` from the upstream provider (or cache).

    Raises:
        GatewayAuthError: Invalid or expired virtual key.
        GatewayRateLimitError: Rate limit exceeded.
        GatewayBudgetExceededError: Budget exhausted.
        GatewayPermissionError: Key not permitted for the provider/model.
        GatewayCircuitOpenError: Circuit breaker is open.
        ProviderError: Upstream provider failure (after retries).
    """
    pipeline_start = time.perf_counter()
    request_id = str(uuid.uuid4())
    vk: Optional[VirtualKey] = None

    log = logger.bind(
        request_id=request_id,
        model=request.model,
        provider=request.provider.value,
    )

    # -----------------------------------------------------------------
    # 1. Validate virtual key (if provided)
    # -----------------------------------------------------------------
    if api_key is not None:
        vk = key_manager.validate_key(api_key)
        if vk is None:
            # Try to determine if this is specifically a budget exhaustion
            budget_status = key_manager.get_budget_status_by_raw_key(api_key) if hasattr(key_manager, "get_budget_status_by_raw_key") else None
            if budget_status and budget_status.get("budget_exceeded"):
                log.warning("gateway_auth_failed", reason="budget_exceeded")
                if otel_setup.gateway_budget_exceeded is not None:
                    otel_setup.gateway_budget_exceeded.add(1, {"key_id": budget_status.get("key_id", "unknown")})
                raise GatewayBudgetExceededError("Budget exceeded for API key")
            log.warning("gateway_auth_failed", reason="invalid_or_expired_key")
            if otel_setup.gateway_auth_failures is not None:
                otel_setup.gateway_auth_failures.add(1, {"reason": "invalid_or_expired_key"})
            raise GatewayAuthError("Invalid, disabled, expired, or over-budget API key")
        log = log.bind(key_id=vk.key_id, team=vk.team, owner=vk.owner)

    # -----------------------------------------------------------------
    # 2. Check permissions
    # -----------------------------------------------------------------
    if vk is not None:
        if not key_manager.check_permissions(
            vk.key_id,
            provider=request.provider.value,
            model=request.model,
        ):
            log.warning("gateway_permission_denied")
            if otel_setup.gateway_auth_failures is not None:
                otel_setup.gateway_auth_failures.add(1, {"reason": "permission_denied"})
            raise GatewayPermissionError(
                f"Key '{vk.key_id}' is not permitted to access "
                f"{request.provider.value}/{request.model}"
            )

    # -----------------------------------------------------------------
    # 3. Check rate limits
    # -----------------------------------------------------------------
    if vk is not None and vk.rate_limit is not None:
        # Ensure the rate limiter has the key's config
        rate_limiter.set_config(vk.key_id, vk.rate_limit)
        result = rate_limiter.consume(vk.key_id)
        if not result.allowed:
            log.warning(
                "gateway_rate_limited",
                denied_reason=result.denied_reason,
                retry_after=result.retry_after,
            )
            if otel_setup.gateway_rate_limit_rejections is not None:
                otel_setup.gateway_rate_limit_rejections.add(
                    1, {"dimension": result.denied_reason or "unknown", "key_id": vk.key_id}
                )
            raise GatewayRateLimitError(
                f"Rate limit exceeded for key '{vk.key_id}': {result.denied_reason}. "
                f"Retry after {result.retry_after:.1f}s"
                if result.retry_after
                else f"Rate limit exceeded for key '{vk.key_id}': {result.denied_reason}"
            )

    # -----------------------------------------------------------------
    # 4. Check cache
    # -----------------------------------------------------------------
    if cache_mode != CacheMode.NONE:
        # Convert messages to list of dicts for cache key
        messages_dicts = [{"role": m.role, "content": m.content} for m in request.messages]
        cached = cache_engine.get(messages_dicts, request.model, mode=cache_mode)
        if cached is not None:
            log.info("gateway_cache_hit", cache_mode=cache_mode.value)
            # Reconstruct ChatCompletionResponse from cached dict
            response = ChatCompletionResponse(**cached)
            _pipeline_latency = (time.perf_counter() - pipeline_start) * 1000

            # Emit cache-hit metrics
            if otel_setup.gateway_cache_hits is not None:
                otel_setup.gateway_cache_hits.add(1)
            cached_tokens = response.usage.total_tokens if response.usage else 0
            if otel_setup.gateway_cache_tokens_saved is not None and cached_tokens > 0:
                otel_setup.gateway_cache_tokens_saved.add(cached_tokens)
            cached_cost = response.cost_usd or 0.0
            if otel_setup.gateway_cache_cost_saved is not None and cached_cost > 0:
                otel_setup.gateway_cache_cost_saved.add(cached_cost)

            log.info(
                "gateway_request_completed",
                source="cache",
                latency_ms=round(_pipeline_latency, 2),
            )
            return response
        else:
            # Cache miss
            if otel_setup.gateway_cache_misses is not None:
                otel_setup.gateway_cache_misses.add(1)

    # -----------------------------------------------------------------
    # 5. Determine target (routing)
    # -----------------------------------------------------------------
    if routing_config is not None and routing_config.targets:
        target = routing_engine.select_target(routing_config)
        target_provider = target.provider
        target_model = target.model
    else:
        target_provider = request.provider.value
        target_model = request.model

    # -----------------------------------------------------------------
    # 6. Check circuit breaker
    # -----------------------------------------------------------------
    circuit_key = f"{target_provider}:{target_model}"
    if not circuit_breaker.can_execute(circuit_key):
        state = circuit_breaker.get_state(circuit_key)
        log.warning("gateway_circuit_open", circuit_key=circuit_key, state=state.value)
        if otel_setup.gateway_circuit_breaker_trips is not None:
            otel_setup.gateway_circuit_breaker_trips.add(
                1, {"provider": target_provider, "new_state": "open"}
            )

        # If we have a routing config with fallback, try the next target
        if routing_config and routing_config.strategy == RoutingStrategy.FALLBACK:
            fallback_found = False
            for fallback_target in routing_config.targets:
                fk = f"{fallback_target.provider}:{fallback_target.model}"
                if fk != circuit_key and circuit_breaker.can_execute(fk):
                    target_provider = fallback_target.provider
                    target_model = fallback_target.model
                    circuit_key = fk
                    fallback_found = True
                    log.info("gateway_circuit_fallback", new_target=fk)
                    break
            if not fallback_found:
                raise GatewayCircuitOpenError(
                    f"Circuit breaker is {state.value} for all targets"
                )
        else:
            raise GatewayCircuitOpenError(
                f"Circuit breaker is {state.value} for {circuit_key}"
            )

    # -----------------------------------------------------------------
    # 7. Build the provider call
    # -----------------------------------------------------------------
    # Update request with routed target
    routed_request = request.model_copy(
        update={
            "provider": Provider(target_provider),
            "model": target_model,
        }
    )

    async def _call_provider() -> ChatCompletionResponse:
        """Execute the provider call (used as the retry target)."""
        provider_adapter = get_provider(routed_request.provider)
        return await provider_adapter.chat_completion(routed_request)

    # -----------------------------------------------------------------
    # 8. Execute with retry
    # -----------------------------------------------------------------
    call_start = time.perf_counter()
    try:
        response = await retry_with_backoff(
            func=_call_provider,
            config=retry_config,
        )
        call_latency_ms = (time.perf_counter() - call_start) * 1000

        # Record success in circuit breaker and routing engine
        circuit_breaker.record_success(circuit_key)
        routing_engine.record_result(
            provider=target_provider,
            model=target_model,
            latency_ms=call_latency_ms,
            success=True,
        )

    except Exception as exc:
        call_latency_ms = (time.perf_counter() - call_start) * 1000

        # Record failure
        circuit_breaker.record_failure(circuit_key)
        routing_engine.record_result(
            provider=target_provider,
            model=target_model,
            latency_ms=call_latency_ms,
            success=False,
        )

        # Release concurrent slot if we acquired one
        if vk is not None and vk.rate_limit is not None:
            rate_limiter.release_concurrent(vk.key_id)

        log.error(
            "gateway_provider_error",
            target=circuit_key,
            error=str(exc),
            latency_ms=round(call_latency_ms, 2),
        )
        raise

    # -----------------------------------------------------------------
    # 9. Store in cache
    # -----------------------------------------------------------------
    if cache_mode != CacheMode.NONE:
        messages_dicts = [{"role": m.role, "content": m.content} for m in request.messages]
        cache_engine.put(messages_dicts, request.model, response.model_dump())

    # -----------------------------------------------------------------
    # 10. Record usage on virtual key
    # -----------------------------------------------------------------
    if vk is not None:
        total_tokens = response.usage.total_tokens if response.usage else 0
        cost = response.cost_usd or 0.0
        key_manager.record_usage(vk.key_id, tokens=total_tokens, cost_usd=cost)

        # Release concurrent slot
        if vk.rate_limit is not None:
            rate_limiter.release_concurrent(vk.key_id)

    # -----------------------------------------------------------------
    # 11. Final logging
    # -----------------------------------------------------------------
    pipeline_latency_ms = (time.perf_counter() - pipeline_start) * 1000
    log.info(
        "gateway_request_completed",
        source="provider",
        target=circuit_key,
        latency_ms=round(pipeline_latency_ms, 2),
        tokens=response.usage.total_tokens if response.usage else 0,
        cost_usd=response.cost_usd,
        cache_mode=cache_mode.value,
    )

    return response


# ---------------------------------------------------------------------------
# Convenience: get full gateway status for monitoring
# ---------------------------------------------------------------------------


def get_gateway_status() -> dict:
    """Aggregate status from all gateway components for a monitoring endpoint."""
    return {
        "routing": routing_engine.get_stats(),
        "cache": cache_engine.get_stats(),
        "circuit_breakers": circuit_breaker.get_all_states(),
    }
