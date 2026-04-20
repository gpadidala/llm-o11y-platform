"""OpenAI-compatible LLM Gateway router with full observability.

This module defines the FastAPI APIRouter that serves as the primary entry
point for chat completion requests.  Incoming requests follow the OpenAI
chat completions schema and are routed to the appropriate provider adapter
(OpenAI, Anthropic, Vertex AI, etc.).

Every request -- successful or failed -- emits:
  * An OpenTelemetry span with GenAI semantic convention attributes
  * Counter / histogram metric updates (token usage, cost, latency)
  * A structured log line via ``structlog``
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List

import structlog
from fastapi import APIRouter, HTTPException

from src.models.telemetry import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    LLMRequestRecord,
)
from src.otel.llm_spans import emit_llm_span
from src.providers import get_provider
from src.providers.base import ProviderError

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["LLM Gateway"])

# ---------------------------------------------------------------------------
# Known models (used by the /models listing endpoint)
# ---------------------------------------------------------------------------

_KNOWN_MODELS: List[Dict[str, str]] = [
    # OpenAI
    {"id": "gpt-4o", "provider": "openai"},
    {"id": "gpt-4o-mini", "provider": "openai"},
    {"id": "o1", "provider": "openai"},
    {"id": "o1-mini", "provider": "openai"},
    # Anthropic
    {"id": "claude-opus-4-6", "provider": "anthropic"},
    {"id": "claude-sonnet-4-6", "provider": "anthropic"},
    {"id": "claude-haiku-4-5", "provider": "anthropic"},
    # Google Gemini / Vertex AI
    {"id": "gemini-1.5-pro", "provider": "vertex_ai"},
    {"id": "gemini-1.5-flash", "provider": "vertex_ai"},
    {"id": "gemini-2.0-flash", "provider": "vertex_ai"},
    # Cohere
    {"id": "command-r-plus", "provider": "cohere"},
    {"id": "command-r", "provider": "cohere"},
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_telemetry_record(
    *,
    request_id: str,
    request: ChatCompletionRequest,
    latency_ms: float,
    status: str,
    response: ChatCompletionResponse | None = None,
    error: str | None = None,
) -> LLMRequestRecord:
    """Construct an ``LLMRequestRecord`` from request/response data.

    Centralises record construction so both the success and error paths
    produce consistent telemetry.
    """
    # Extract finish_reason from the first choice (if present)
    finish_reason = None
    if response and response.choices:
        finish_reason = response.choices[0].finish_reason

    # Compute per-component cost breakdown for metrics
    from src.providers.base import BaseProvider
    input_cost = output_cost = cache_cost = 0.0
    if response and response.cost_usd:
        breakdown = BaseProvider.estimate_cost_breakdown(
            request.model,
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
            cache_creation_input_tokens=response.usage.cache_creation_input_tokens,
            cache_read_input_tokens=response.usage.cache_read_input_tokens,
        )
        input_cost = breakdown["input_cost_usd"]
        output_cost = breakdown["output_cost_usd"]
        cache_cost = breakdown["cache_cost_usd"]

    return LLMRequestRecord(
        request_id=request_id,
        provider=request.provider,
        model=request.model,
        messages=request.messages,
        response_model=response.model if response else None,
        prompt_tokens=response.usage.prompt_tokens if response else 0,
        completion_tokens=response.usage.completion_tokens if response else 0,
        total_tokens=response.usage.total_tokens if response else 0,
        cost_usd=response.cost_usd if response and response.cost_usd else 0.0,
        input_cost_usd=input_cost,
        output_cost_usd=output_cost,
        cache_cost_usd=cache_cost,
        cache_creation_input_tokens=response.usage.cache_creation_input_tokens if response else 0,
        cache_read_input_tokens=response.usage.cache_read_input_tokens if response else 0,
        latency_ms=latency_ms,
        status=status,
        error=error,
        finish_reason=finish_reason,
        user_id=request.user_id,
        session_id=request.session_id,
        tags=request.tags,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(request: ChatCompletionRequest) -> ChatCompletionResponse:
    """OpenAI-compatible chat completions endpoint.

    Routes to the appropriate provider based on the ``request.provider``
    field.  Emits full OTel telemetry (span + metrics) for every request,
    regardless of outcome.

    Returns:
        ChatCompletionResponse in the standard OpenAI response format.

    Raises:
        HTTPException 502: When the upstream provider returns an error.
        HTTPException 500: For unexpected internal failures.
    """
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()

    log = logger.bind(
        request_id=request_id,
        provider=request.provider.value,
        model=request.model,
    )

    try:
        # 0. Layer 3 — pre-flight context window validation
        #    Reject requests that will overflow the model's context window
        #    BEFORE we forward them to the upstream provider (saves cost + latency).
        try:
            from src.gateway.context_window import (
                ContextWindowExceededError,
                validate_context_window,
            )
            validate_context_window(
                model=request.model,
                messages=request.messages,
                max_tokens=request.max_tokens,
            )
        except ContextWindowExceededError as ctx_exc:
            log.warning(
                "context_window_exceeded",
                estimated_tokens=ctx_exc.estimated_tokens,
                max_tokens=ctx_exc.max_tokens,
                limit=ctx_exc.limit,
            )
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "context_length_exceeded",
                    "message": str(ctx_exc),
                    "model": request.model,
                    "estimated_input_tokens": ctx_exc.estimated_tokens,
                    "max_tokens": ctx_exc.max_tokens,
                    "context_window": ctx_exc.limit,
                },
            )
        except ImportError:
            pass  # context_window module optional

        # 1. Resolve provider adapter
        provider = get_provider(request.provider)

        # 2. Execute the chat completion against the upstream provider
        response = await provider.chat_completion(request)

        # 3. Measure end-to-end latency
        latency_ms = (time.perf_counter() - start_time) * 1000

        # 4. Build and emit telemetry
        #    emit_llm_span creates the OTel span AND updates all metrics
        #    (counters + histograms) internally, so no separate metric calls.
        record = _build_telemetry_record(
            request_id=request_id,
            request=request,
            latency_ms=latency_ms,
            status="success",
            response=response,
        )
        emit_llm_span(record)

        # 5. Structured log
        log.info(
            "llm_request_completed",
            latency_ms=round(latency_ms, 2),
            tokens=response.usage.total_tokens,
            cost_usd=response.cost_usd,
        )

        return response

    except HTTPException:
        # Let intentionally-raised HTTPExceptions (e.g. 400 context overflow)
        # propagate unchanged. The outer Exception handler would otherwise
        # re-wrap them as 500.
        raise

    except ProviderError as exc:
        latency_ms = (time.perf_counter() - start_time) * 1000

        record = _build_telemetry_record(
            request_id=request_id,
            request=request,
            latency_ms=latency_ms,
            status="error",
            error=str(exc),
        )
        emit_llm_span(record)

        log.error(
            "llm_request_failed",
            error=str(exc),
            latency_ms=round(latency_ms, 2),
        )

        raise HTTPException(
            status_code=502,
            detail=f"Provider error: {exc}",
        ) from exc

    except Exception as exc:
        latency_ms = (time.perf_counter() - start_time) * 1000

        record = _build_telemetry_record(
            request_id=request_id,
            request=request,
            latency_ms=latency_ms,
            status="error",
            error=str(exc),
        )
        emit_llm_span(record)

        log.error(
            "llm_request_unexpected_error",
            error=str(exc),
            error_type=type(exc).__name__,
            latency_ms=round(latency_ms, 2),
        )

        raise HTTPException(
            status_code=500,
            detail=f"Internal gateway error: {type(exc).__name__}",
        ) from exc


@router.get("/models")
async def list_models() -> Dict[str, Any]:
    """List available models across all providers.

    Returns an OpenAI-compatible ``/v1/models`` response shape so that
    client libraries (e.g. ``openai.Client``) can consume this endpoint
    directly.
    """
    return {
        "object": "list",
        "data": [
            {
                "id": m["id"],
                "object": "model",
                "owned_by": m["provider"],
            }
            for m in _KNOWN_MODELS
        ],
    }


@router.get("/health")
async def health_check() -> Dict[str, str]:
    """Lightweight health-check endpoint for load balancers and k8s probes."""
    return {"status": "ok"}
