"""Emit OpenTelemetry spans for LLM requests following GenAI semantic conventions."""

import logging

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from src.models.telemetry import LLMRequestRecord
import src.otel.setup as otel_setup

logger = logging.getLogger(__name__)

# Maximum length for message content stored as span events to prevent oversized
# spans when prompts or completions are very large.
_MAX_EVENT_CONTENT_LEN = 4096


def _truncate(text: str, max_len: int = _MAX_EVENT_CONTENT_LEN) -> str:
    """Truncate text and append an ellipsis marker when it exceeds *max_len*."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "...[truncated]"


def emit_llm_span(record: LLMRequestRecord) -> None:
    """Create a span for an LLM request with GenAI semantic attributes.

    The span follows the OpenTelemetry GenAI semantic conventions and records
    token usage, cost, latency, and (optionally) message events.

    Corresponding OTel metrics are also updated so that dashboards and
    alerting rules can consume the data without querying traces.
    """
    tracer = trace.get_tracer("llm-o11y-gateway")

    with tracer.start_as_current_span(
        name=f"chat {record.model}",
        kind=trace.SpanKind.CLIENT,
    ) as span:
        # -- GenAI semantic convention attributes ---------------------------
        span.set_attribute("gen_ai.system", record.provider.value)
        span.set_attribute("gen_ai.request.model", record.model)
        span.set_attribute(
            "gen_ai.response.model", record.response_model or record.model
        )
        span.set_attribute("gen_ai.usage.prompt_tokens", record.prompt_tokens)
        span.set_attribute("gen_ai.usage.completion_tokens", record.completion_tokens)
        span.set_attribute("gen_ai.usage.total_tokens", record.total_tokens)
        span.set_attribute("gen_ai.response.finish_reasons", ["stop"])

        # -- Custom gateway attributes -------------------------------------
        span.set_attribute("llm.cost_usd", record.cost_usd)
        span.set_attribute("llm.latency_ms", record.latency_ms)

        if record.ttft_ms is not None:
            span.set_attribute("llm.ttft_ms", record.ttft_ms)

        if record.user_id:
            span.set_attribute("llm.user_id", record.user_id)

        if record.session_id:
            span.set_attribute("llm.session_id", record.session_id)

        span.set_attribute("llm.request_id", record.request_id)

        if record.tags:
            for key, value in record.tags.items():
                span.set_attribute(f"llm.tag.{key}", value)

        # -- Span status ----------------------------------------------------
        if record.status == "success":
            span.set_status(StatusCode.OK)
        else:
            span.set_status(StatusCode.ERROR, description=record.error or "unknown")
            if record.error:
                span.record_exception(
                    Exception(record.error),
                    attributes={"exception.type": "LLMRequestError"},
                )

        # -- Message events (input / output) --------------------------------
        try:
            for idx, msg in enumerate(record.messages):
                span.add_event(
                    name=f"gen_ai.{msg.role}.message",
                    attributes={
                        "gen_ai.message.index": idx,
                        "gen_ai.message.role": msg.role,
                        "gen_ai.message.content": _truncate(msg.content),
                    },
                )
        except Exception:
            logger.debug("Failed to add message events to span", exc_info=True)

    # -- Metrics ------------------------------------------------------------
    _emit_metrics(record)


def _emit_metrics(record: LLMRequestRecord) -> None:
    """Update OTel counters and histograms for the given LLM request record."""
    provider = record.provider.value
    model = record.model
    status = record.status

    common_attrs = {"provider": provider, "model": model}

    # Request count
    if otel_setup.llm_request_counter is not None:
        otel_setup.llm_request_counter.add(1, {**common_attrs, "status": status})

    # Token usage
    if otel_setup.llm_token_counter is not None:
        if record.prompt_tokens > 0:
            otel_setup.llm_token_counter.add(
                record.prompt_tokens,
                {**common_attrs, "token_type": "prompt"},
            )
        if record.completion_tokens > 0:
            otel_setup.llm_token_counter.add(
                record.completion_tokens,
                {**common_attrs, "token_type": "completion"},
            )

    # Cost
    if otel_setup.llm_cost_counter is not None and record.cost_usd > 0:
        otel_setup.llm_cost_counter.add(record.cost_usd, common_attrs)

    # Latency
    if otel_setup.llm_request_duration is not None:
        otel_setup.llm_request_duration.record(record.latency_ms, common_attrs)

    # Time to first token
    if otel_setup.llm_ttft_histogram is not None and record.ttft_ms is not None:
        otel_setup.llm_ttft_histogram.record(record.ttft_ms, common_attrs)
