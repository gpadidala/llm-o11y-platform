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

# Log export via OTLP
try:
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    _LOGS_AVAILABLE = True
except ImportError:
    _LOGS_AVAILABLE = False

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

# ---------------------------------------------------------------------------
# Gateway operational metrics
# ---------------------------------------------------------------------------
gateway_cache_hits: metrics.Counter = None  # type: ignore[assignment]
gateway_cache_misses: metrics.Counter = None  # type: ignore[assignment]
gateway_cache_tokens_saved: metrics.Counter = None  # type: ignore[assignment]
gateway_cache_cost_saved: metrics.Counter = None  # type: ignore[assignment]
gateway_rate_limit_rejections: metrics.Counter = None  # type: ignore[assignment]
gateway_circuit_breaker_trips: metrics.Counter = None  # type: ignore[assignment]
gateway_circuit_breaker_state: metrics.UpDownCounter = None  # type: ignore[assignment]
gateway_auth_failures: metrics.Counter = None  # type: ignore[assignment]
gateway_budget_exceeded: metrics.Counter = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Guardrail metrics
# ---------------------------------------------------------------------------
guardrail_checks: metrics.Counter = None  # type: ignore[assignment]
guardrail_violations: metrics.Counter = None  # type: ignore[assignment]
guardrail_pii_detected: metrics.Counter = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Evaluation metrics
# ---------------------------------------------------------------------------
eval_runs: metrics.Counter = None  # type: ignore[assignment]
eval_scores: metrics.Histogram = None  # type: ignore[assignment]
eval_latency: metrics.Histogram = None  # type: ignore[assignment]


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
    global gateway_cache_hits, gateway_cache_misses, gateway_cache_tokens_saved
    global gateway_cache_cost_saved, gateway_rate_limit_rejections
    global gateway_circuit_breaker_trips, gateway_circuit_breaker_state
    global gateway_auth_failures, gateway_budget_exceeded
    global guardrail_checks, guardrail_violations, guardrail_pii_detected
    global eval_runs, eval_scores, eval_latency

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

    # ---- Logs (OTLP export) ------------------------------------------
    if _LOGS_AVAILABLE:
        try:
            log_exporter = OTLPLogExporter(endpoint=endpoint, insecure=True)
            logger_provider = LoggerProvider(resource=resource)
            logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
            set_logger_provider(logger_provider)

            # Attach OTel handler to Python root logger so all structlog/logging
            # output is also forwarded to the collector → Loki
            otel_handler = LoggingHandler(
                level=logging.INFO,
                logger_provider=logger_provider,
            )
            logging.getLogger().addHandler(otel_handler)
            logger.info("OTLP log exporter initialized — logs will be sent to collector")
        except Exception:
            logger.warning("Failed to initialize OTLP log exporter", exc_info=True)
    else:
        logger.info("OTLP log export not available (opentelemetry-sdk-logs not installed)")

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

    # ---- Gateway operational metrics ---------------------------------
    gateway_cache_hits = meter.create_counter("gateway.cache.hits", description="Cache hits", unit="1")
    gateway_cache_misses = meter.create_counter("gateway.cache.misses", description="Cache misses", unit="1")
    gateway_cache_tokens_saved = meter.create_counter("gateway.cache.tokens.saved", description="Tokens saved by cache", unit="token")
    gateway_cache_cost_saved = meter.create_counter("gateway.cache.cost.saved", description="Cost saved by cache", unit="usd")
    gateway_rate_limit_rejections = meter.create_counter("gateway.rate.limit.rejections", description="Rate limit rejections", unit="1")
    gateway_circuit_breaker_trips = meter.create_counter("gateway.circuit.breaker.trips", description="Circuit breaker state changes", unit="1")
    gateway_circuit_breaker_state = meter.create_up_down_counter("gateway.circuit.breaker.state", description="Circuit breaker current state (0=closed,1=open,2=half_open)", unit="1")
    gateway_auth_failures = meter.create_counter("gateway.auth.failures", description="Auth failures", unit="1")
    gateway_budget_exceeded = meter.create_counter("gateway.budget.exceeded", description="Budget exceeded events", unit="1")

    # ---- Guardrail metrics -------------------------------------------
    guardrail_checks = meter.create_counter("guardrail.checks", description="Guardrail check executions", unit="1")
    guardrail_violations = meter.create_counter("guardrail.violations", description="Guardrail violations detected", unit="1")
    guardrail_pii_detected = meter.create_counter("guardrail.pii.detected", description="PII instances detected", unit="1")

    # ---- Evaluation metrics ------------------------------------------
    eval_runs = meter.create_counter("eval.runs", description="Evaluation runs", unit="1")
    eval_scores = meter.create_histogram("eval.scores", description="Evaluation scores distribution", unit="1")
    eval_latency = meter.create_histogram("eval.latency", description="Evaluation judge latency", unit="ms")

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
