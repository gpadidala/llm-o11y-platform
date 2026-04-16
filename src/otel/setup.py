"""OpenTelemetry bootstrap -- initializes traces, metrics, and logs exporters.

Call ``init_telemetry()`` once at application startup.  It configures:
- OTLP gRPC exporters for traces and metrics
- A TracerProvider with a service-name resource
- A MeterProvider for Prometheus-compatible metrics
- FastAPI and HTTPX auto-instrumentation
- GenAI semantic-convention metrics for LLM requests
- MCP tool-call and session metrics
"""

import logging

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_NAMESPACE
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from src.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level globals -- populated by init_telemetry()
# ---------------------------------------------------------------------------
tracer: trace.Tracer = trace.get_tracer("llm-o11y-gateway")
meter: metrics.Meter = metrics.get_meter("llm-o11y-gateway")

# ---------------------------------------------------------------------------
# LLM metrics (GenAI semantic conventions where applicable)
# ---------------------------------------------------------------------------
llm_request_counter: metrics.Counter = None  # type: ignore[assignment]
llm_token_counter: metrics.Counter = None  # type: ignore[assignment]
llm_cost_counter: metrics.Counter = None  # type: ignore[assignment]
llm_request_duration: metrics.Histogram = None  # type: ignore[assignment]
llm_ttft_histogram: metrics.Histogram = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# MCP metrics
# ---------------------------------------------------------------------------
mcp_tool_call_counter: metrics.Counter = None  # type: ignore[assignment]
mcp_tool_call_duration: metrics.Histogram = None  # type: ignore[assignment]
mcp_session_cost_counter: metrics.Counter = None  # type: ignore[assignment]


def init_telemetry(app=None):
    """Initialize OpenTelemetry and optionally instrument a FastAPI app.

    Configuration is read from ``src.config.settings`` which in turn reads
    from environment variables / ``.env``.

    Returns:
        tuple: (tracer, meter) for convenience.
    """
    global tracer, meter
    global llm_request_counter, llm_token_counter, llm_cost_counter
    global llm_request_duration, llm_ttft_histogram
    global mcp_tool_call_counter, mcp_tool_call_duration, mcp_session_cost_counter

    endpoint = settings.otel_exporter_otlp_endpoint
    service_name = settings.otel_service_name

    # ---- Resource --------------------------------------------------------
    resource = Resource.create(
        {
            SERVICE_NAME: service_name,
            SERVICE_NAMESPACE: "llm-o11y",
            "service.version": "0.1.0",
            "deployment.environment": "production",
        }
    )

    # ---- Traces ----------------------------------------------------------
    span_exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    tracer = trace.get_tracer("llm-o11y-gateway", tracer_provider=tracer_provider)

    # ---- Metrics ---------------------------------------------------------
    metric_exporter = OTLPMetricExporter(endpoint=endpoint, insecure=True)
    metric_reader = PeriodicExportingMetricReader(
        metric_exporter, export_interval_millis=15_000
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    meter = metrics.get_meter("llm-o11y-gateway", meter_provider=meter_provider)

    # ---- LLM metrics ------------------------------------------------
    # Names use dots (OTel convention); the Prometheus remote-write
    # exporter converts dots to underscores and appends _total / _bucket.
    # Dashboard queries use the Prometheus-converted names with simple
    # label keys (provider, model, status, token_type).
    llm_request_counter = meter.create_counter(
        name="llm.requests",
        description="Number of LLM requests by provider, model, and status",
        unit="1",
    )

    llm_token_counter = meter.create_counter(
        name="llm.tokens",
        description="Token usage by provider, model, and token type (prompt/completion)",
        unit="token",
    )

    llm_cost_counter = meter.create_counter(
        name="llm.cost.usd",
        description="Estimated cost in USD by provider and model",
        unit="usd",
    )

    llm_request_duration = meter.create_histogram(
        name="llm.request.duration",
        description="LLM request latency in milliseconds by provider and model",
        unit="ms",
    )

    llm_ttft_histogram = meter.create_histogram(
        name="llm.ttft",
        description="Time to first token in milliseconds (streaming requests)",
        unit="ms",
    )

    # ---- MCP metrics -------------------------------------------------
    mcp_tool_call_counter = meter.create_counter(
        name="mcp.tool.calls",
        description="Number of MCP tool calls by server, tool name, and status",
        unit="1",
    )

    mcp_tool_call_duration = meter.create_histogram(
        name="mcp.tool.duration",
        description="MCP tool call latency in milliseconds by server and tool name",
        unit="ms",
    )

    mcp_session_cost_counter = meter.create_counter(
        name="mcp.session.cost.usd",
        description="Accumulated cost in USD by session and agent",
        unit="usd",
    )

    # ---- Auto-instrumentation --------------------------------------------
    if app is not None:
        try:
            FastAPIInstrumentor.instrument_app(app)
            logger.info("FastAPI instrumented with OpenTelemetry")
        except Exception:
            logger.exception("Failed to instrument FastAPI app")

    try:
        HTTPXClientInstrumentor().instrument()
        logger.info("HTTPX client instrumented with OpenTelemetry")
    except Exception:
        logger.exception("Failed to instrument HTTPX client")

    logger.info(
        "OpenTelemetry initialized: service=%s endpoint=%s", service_name, endpoint
    )

    return tracer, meter


def shutdown_telemetry() -> None:
    """Flush and shut down all OTel providers gracefully."""
    tracer_provider = trace.get_tracer_provider()
    if hasattr(tracer_provider, "shutdown"):
        tracer_provider.shutdown()

    meter_provider = metrics.get_meter_provider()
    if hasattr(meter_provider, "shutdown"):
        meter_provider.shutdown()

    logger.info("OpenTelemetry providers shut down")
