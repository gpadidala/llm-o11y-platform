"""FastAPI application for the LLM O11y Gateway.

Startup lifecycle:
  1. Configure structured JSON logging via structlog.
  2. Initialize OpenTelemetry (traces, metrics, auto-instrumentation).
  3. Create .data/ directory for persistent storage.
  4. Initialize all subsystem engines (routing, cache, guardrails, eval, prompts).

Routes:
  /                    -- web UI dashboard
  /settings            -- web UI settings page
  /providers           -- web UI provider status page
  /playground          -- web UI playground page
  /prompts             -- web UI prompts management page
  /logs                -- web UI request logs page
  /keys                -- web UI virtual keys page
  /eval                -- web UI evaluation page
  /guardrails          -- web UI guardrails page
  /routing             -- web UI routing config page
  /health              -- liveness probe
  /metrics             -- Prometheus scrape endpoint
  /api/settings        -- settings CRUD API
  /api/status          -- backend service health API
  /api/dashboard/stats -- aggregated dashboard statistics
  /api/keys/*          -- virtual key management API
  /api/routing/*       -- routing configuration API
  /api/cache/*         -- cache management API
  /api/logs/*          -- request log API
  /api/prompts/*       -- prompt template management API
  /api/guardrails/*    -- guardrails management API
  /api/eval/*          -- evaluation API
  /v1/chat/completions -- unified chat completion proxy
  /v1/mcp/*            -- MCP telemetry receiver
"""

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx
import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from opentelemetry import trace

from src.config import settings
from src.gateway.router import router as gateway_router
from src.mcp_tracer.router import router as mcp_router
from src.otel.setup import init_telemetry, shutdown_telemetry

# ---------------------------------------------------------------------------
# Conditional imports for new subsystem modules
# ---------------------------------------------------------------------------
# Each import is wrapped in try/except so the app still starts even if
# a module has not been created yet.

try:
    from src.gateway.middleware import gateway_pipeline
except ImportError:
    gateway_pipeline = None  # type: ignore[assignment]

try:
    from src.gateway.virtual_keys import key_manager  # VirtualKeyManager instance
except ImportError:
    key_manager = None  # type: ignore[assignment]

try:
    from src.gateway.routing import routing_engine  # RoutingEngine instance
except ImportError:
    routing_engine = None  # type: ignore[assignment]

try:
    from src.gateway.cache import cache_engine  # CacheEngine instance
except ImportError:
    cache_engine = None  # type: ignore[assignment]

try:
    from src.gateway.rate_limiter import rate_limiter  # RateLimiter instance
except ImportError:
    rate_limiter = None  # type: ignore[assignment]

try:
    from src.gateway.circuit_breaker import circuit_breaker  # CircuitBreaker instance
except ImportError:
    circuit_breaker = None  # type: ignore[assignment]

try:
    from src.prompts.router import router as prompts_router
except ImportError:
    prompts_router = None  # type: ignore[assignment]

try:
    from src.prompts.templates import prompt_store  # PromptStore instance
except ImportError:
    prompt_store = None  # type: ignore[assignment]

try:
    from src.guardrails.router import router as guardrails_router
except ImportError:
    guardrails_router = None  # type: ignore[assignment]

try:
    from src.guardrails.engine import guardrails_engine  # GuardrailsEngine instance
except ImportError:
    guardrails_engine = None  # type: ignore[assignment]

try:
    from src.eval.router import router as eval_router
except ImportError:
    eval_router = None  # type: ignore[assignment]

try:
    from src.eval.judge import eval_judge  # EvalJudge instance
except ImportError:
    eval_judge = None  # type: ignore[assignment]

try:
    from src.eval.datasets import dataset_store  # DatasetStore instance
except ImportError:
    dataset_store = None  # type: ignore[assignment]

try:
    from src.auth.manager import auth_manager  # AuthManager instance
    from src.auth.models import Role, User
    from src.auth.middleware import get_current_user, require_auth, require_role
except ImportError:
    auth_manager = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    User = None  # type: ignore[assignment]
    get_current_user = None  # type: ignore[assignment]
    require_auth = None  # type: ignore[assignment]
    require_role = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Request Log Store (in-memory ring buffer)
# ---------------------------------------------------------------------------


class RequestLog(BaseModel):
    """A single gateway request log entry."""

    request_id: str
    timestamp: float
    provider: str
    model: str
    status: str  # "success" or "error"
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    cache_hit: bool = False
    error: Optional[str] = None
    # Store first 500 chars of input/output for log display
    input_preview: str = ""
    output_preview: str = ""


class RequestLogStore:
    """In-memory request log store with ring buffer.

    Maintains a fixed-size deque of ``RequestLog`` entries and provides
    querying, filtering, and aggregate statistics.
    """

    def __init__(self, max_entries: int = 10000):
        self._max_entries = max_entries
        self._buffer: deque[RequestLog] = deque(maxlen=max_entries)
        self._index: dict[str, RequestLog] = {}  # request_id -> log for O(1) lookup

    def add(self, log: RequestLog) -> None:
        """Add a log entry to the ring buffer."""
        # If we are at capacity, evict the oldest from the index
        if len(self._buffer) >= self._max_entries:
            oldest = self._buffer[0]
            self._index.pop(oldest.request_id, None)

        self._buffer.append(log)
        self._index[log.request_id] = log

    def query(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        status: Optional[str] = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[RequestLog], int]:
        """Query logs with optional filters and pagination.

        Returns:
            A tuple of (matching_logs, total_matching_count).
        """
        # Filter from newest to oldest
        results: list[RequestLog] = []
        for log in reversed(self._buffer):
            if provider and log.provider != provider:
                continue
            if model and log.model != model:
                continue
            if status and log.status != status:
                continue
            results.append(log)

        total = len(results)
        page = results[offset : offset + limit]
        return page, total

    def get(self, request_id: str) -> Optional[RequestLog]:
        """Get a specific log entry by request_id."""
        return self._index.get(request_id)

    def get_stats(self) -> dict:
        """Compute aggregate statistics from all buffered logs."""
        if not self._buffer:
            return {
                "total_requests": 0,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "avg_latency_ms": 0.0,
                "cache_hit_rate": 0.0,
                "error_rate": 0.0,
                "requests_by_provider": {},
                "requests_by_model": {},
            }

        total_requests = len(self._buffer)
        total_tokens = 0
        total_cost = 0.0
        total_latency = 0.0
        cache_hits = 0
        errors = 0
        by_provider: dict[str, int] = {}
        by_model: dict[str, int] = {}

        for log in self._buffer:
            total_tokens += log.total_tokens
            total_cost += log.cost_usd
            total_latency += log.latency_ms
            if log.cache_hit:
                cache_hits += 1
            if log.status == "error":
                errors += 1
            by_provider[log.provider] = by_provider.get(log.provider, 0) + 1
            by_model[log.model] = by_model.get(log.model, 0) + 1

        return {
            "total_requests": total_requests,
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 6),
            "avg_latency_ms": round(total_latency / total_requests, 2),
            "cache_hit_rate": round(cache_hits / total_requests, 4) if total_requests > 0 else 0.0,
            "error_rate": round(errors / total_requests, 4) if total_requests > 0 else 0.0,
            "requests_by_provider": by_provider,
            "requests_by_model": by_model,
        }

    def recent(self, n: int = 10) -> list[RequestLog]:
        """Return the *n* most recent log entries."""
        items = list(self._buffer)
        return list(reversed(items[-n:]))


# Module-level singleton
request_log_store = RequestLogStore()


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


def _inject_trace_context(logger, method_name, event_dict):
    """Structlog processor: inject trace_id and span_id from the active OTel span.

    This enables deep linking between logs (Loki) and traces (Tempo) in Grafana.
    Loki's derivedFields regex matches ``trace_id=<hex>`` to create clickable links.
    """
    span = trace.get_current_span()
    if span and span.is_recording():
        ctx = span.get_span_context()
        if ctx and ctx.trace_id:
            event_dict["trace_id"] = format(ctx.trace_id, "032x")
            event_dict["span_id"] = format(ctx.span_id, "016x")
            event_dict["trace_flags"] = format(ctx.trace_flags, "02x")
    return event_dict


def _configure_logging() -> None:
    """Set up structlog for JSON-formatted, context-rich log output with trace correlation."""
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _inject_trace_context,  # <-- injects trace_id/span_id into every log
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application startup / shutdown lifecycle."""
    _configure_logging()
    logger = structlog.get_logger("llm-o11y-gateway")

    # Create .data/ directory for persistent storage
    data_dir = Path(__file__).resolve().parent.parent / ".data"
    data_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "starting_gateway",
        port=settings.gateway_port,
        otel_endpoint=settings.otel_exporter_otlp_endpoint,
    )

    # Log subsystem availability
    subsystems = {
        "gateway_pipeline": gateway_pipeline is not None,
        "key_manager": key_manager is not None,
        "routing_engine": routing_engine is not None,
        "cache_engine": cache_engine is not None,
        "rate_limiter": rate_limiter is not None,
        "circuit_breaker": circuit_breaker is not None,
        "prompt_store": prompt_store is not None,
        "guardrails_engine": guardrails_engine is not None,
        "eval_judge": eval_judge is not None,
        "dataset_store": dataset_store is not None,
        "auth_manager": auth_manager is not None,
    }
    logger.info("subsystem_status", **subsystems)

    yield

    logger.info("shutting_down_gateway")
    shutdown_telemetry()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="LLM O11y Gateway",
    description=(
        "OpenAI-compatible gateway that proxies LLM requests to multiple "
        "providers and emits OpenTelemetry traces, metrics, and logs. "
        "Includes prompt management, guardrails, evaluation, caching, "
        "routing, and virtual key management."
    ),
    version="0.2.0",
    lifespan=lifespan,
)

# Instrument at module level so middleware is registered before startup.
init_telemetry(app)

# ---------------------------------------------------------------------------
# Static files & templates
# ---------------------------------------------------------------------------

_src_dir = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(_src_dir / "static")), name="static")
templates = Jinja2Templates(directory=str(_src_dir / "templates"))

# ---------------------------------------------------------------------------
# Router mounts
# ---------------------------------------------------------------------------

app.include_router(gateway_router, prefix="/v1")
app.include_router(mcp_router, prefix="/v1/mcp")

# New subsystem API routers (only mount if available)
if prompts_router is not None:
    app.include_router(prompts_router, prefix="/api/prompts", tags=["prompts"])
if guardrails_router is not None:
    app.include_router(guardrails_router, prefix="/api/guardrails", tags=["guardrails"])
if eval_router is not None:
    app.include_router(eval_router, prefix="/api/eval", tags=["eval"])


# ---------------------------------------------------------------------------
# Auth helper -- inject current user into template context
# ---------------------------------------------------------------------------

async def _user_context(request: Request) -> dict:
    """Build template context dict with current_user for RBAC-aware rendering."""
    ctx: dict = {"request": request, "current_user": None}
    if auth_manager is not None and get_current_user is not None:
        user = await get_current_user(request)
        ctx["current_user"] = user
    return ctx


# ---------------------------------------------------------------------------
# Web UI pages
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def ui_landing(request: Request):
    """Landing page with top nav, hero, flow, and features."""
    return templates.TemplateResponse("landing.html", {"request": request})


@app.get("/dashboard", response_class=HTMLResponse)
async def ui_dashboard(request: Request):
    """Analytics dashboard page."""
    ctx = await _user_context(request)
    ctx["active_page"] = "dashboard"
    return templates.TemplateResponse("index.html", ctx)


@app.get("/settings", response_class=HTMLResponse)
async def ui_settings(request: Request):
    """Settings configuration page."""
    ctx = await _user_context(request)
    ctx["active_page"] = "settings"
    return templates.TemplateResponse("settings.html", ctx)


@app.get("/providers", response_class=HTMLResponse)
async def ui_providers(request: Request):
    """Provider status page."""
    ctx = await _user_context(request)
    ctx["active_page"] = "providers"
    return templates.TemplateResponse("providers.html", ctx)


@app.get("/playground", response_class=HTMLResponse)
async def ui_playground(request: Request):
    """Interactive LLM playground page."""
    ctx = await _user_context(request)
    ctx["active_page"] = "playground"
    return templates.TemplateResponse("playground.html", ctx)


@app.get("/prompts", response_class=HTMLResponse)
async def ui_prompts(request: Request):
    """Prompt template management page."""
    ctx = await _user_context(request)
    ctx["active_page"] = "prompts"
    return templates.TemplateResponse("prompts.html", ctx)


@app.get("/logs", response_class=HTMLResponse)
async def ui_logs(request: Request):
    """Request logs viewer page."""
    ctx = await _user_context(request)
    ctx["active_page"] = "logs"
    return templates.TemplateResponse("logs.html", ctx)


@app.get("/keys", response_class=HTMLResponse)
async def ui_keys(request: Request):
    """Virtual key management page."""
    ctx = await _user_context(request)
    ctx["active_page"] = "keys"
    return templates.TemplateResponse("keys.html", ctx)


@app.get("/eval", response_class=HTMLResponse)
async def ui_eval(request: Request):
    """Evaluation dashboard page."""
    ctx = await _user_context(request)
    ctx["active_page"] = "eval"
    return templates.TemplateResponse("eval.html", ctx)


@app.get("/guardrails", response_class=HTMLResponse)
async def ui_guardrails(request: Request):
    """Guardrails configuration page."""
    ctx = await _user_context(request)
    ctx["active_page"] = "guardrails"
    return templates.TemplateResponse("guardrails.html", ctx)


@app.get("/routing", response_class=HTMLResponse)
async def ui_routing(request: Request):
    """Routing configuration page."""
    ctx = await _user_context(request)
    ctx["active_page"] = "routing"
    return templates.TemplateResponse("routing.html", ctx)


# ---------------------------------------------------------------------------
# Health & Metrics
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    """Liveness / readiness probe."""
    return {"status": "healthy", "service": "llm-o11y-gateway"}


@app.get("/metrics")
async def metrics_endpoint():
    """Prometheus scrape endpoint.

    ``prometheus_client`` auto-collects default process metrics.  Custom
    OTel metrics are exported via OTLP; this endpoint is a convenience
    for environments that scrape ``/metrics`` directly.
    """
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# Keys that contain secrets and should be redacted in GET responses
_SECRET_KEYS = {
    "openai_api_key", "azure_openai_api_key", "anthropic_api_key",
    "cohere_api_key", "aws_access_key_id", "aws_secret_access_key",
    "google_application_credentials",
}

# Mapping from settings field names to .env variable names
_SETTINGS_FIELDS = [
    "openai_api_key", "azure_openai_api_key", "azure_openai_endpoint",
    "azure_openai_api_version", "anthropic_api_key",
    "google_application_credentials", "vertex_project_id", "vertex_location",
    "aws_access_key_id", "aws_secret_access_key", "aws_region",
    "cohere_api_key", "gateway_port", "log_level",
    "otel_exporter_otlp_endpoint",
]


def _read_env_file() -> dict[str, str]:
    """Parse the .env file into a dict (simple KEY=VALUE parsing)."""
    result: dict[str, str] = {}
    if not _ENV_FILE.exists():
        return result
    for line in _ENV_FILE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key, _, value = stripped.partition("=")
            result[key.strip()] = value.strip()
    return result


def _write_env_file(env_vars: dict[str, str]) -> None:
    """Write settings back to the .env file, preserving comments."""
    lines: list[str] = []
    written_keys: set[str] = set()

    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.partition("=")[0].strip()
                if key in env_vars:
                    lines.append(f"{key}={env_vars[key]}")
                    written_keys.add(key)
                else:
                    lines.append(line)
            else:
                lines.append(line)

    # Append any new keys not already in the file
    for key, value in env_vars.items():
        if key not in written_keys:
            lines.append(f"{key}={value}")

    _ENV_FILE.write_text("\n".join(lines) + "\n")


@app.get("/api/settings")
async def get_settings():
    """Return current settings (secrets redacted)."""
    env = _read_env_file()
    result: dict[str, str] = {}

    for field in _SETTINGS_FIELDS:
        env_key = field.upper()
        value = env.get(env_key, "")
        if field in _SECRET_KEYS and value and not value.startswith("your-"):
            result[field] = "***"
        else:
            result[field] = value

    # Read MCP servers from env (stored as JSON in MCP_SERVERS var)
    mcp_raw = env.get("MCP_SERVERS", "")
    mcp_servers = []
    if mcp_raw:
        try:
            mcp_servers = json.loads(mcp_raw)
        except json.JSONDecodeError:
            pass

    return {"settings": result, "mcp_servers": mcp_servers}


@app.post("/api/settings")
async def save_settings(data: dict):
    """Save settings to the .env file."""
    env = _read_env_file()

    user_settings = data.get("settings", {})
    for field in _SETTINGS_FIELDS:
        if field in user_settings:
            value = user_settings[field]
            if value and value != "***":
                env[field.upper()] = str(value)

    # Save MCP servers as JSON
    mcp_servers = data.get("mcp_servers", [])
    if mcp_servers:
        env["MCP_SERVERS"] = json.dumps(mcp_servers)
    elif "MCP_SERVERS" in env:
        del env["MCP_SERVERS"]

    _write_env_file(env)
    return {"status": "saved"}


# ---------------------------------------------------------------------------
# Service Status API
# ---------------------------------------------------------------------------


async def _check_service(url: str, timeout: float = 2.0) -> dict:
    """Probe a service URL and return health info."""
    try:
        start = time.monotonic()
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
        elapsed = round((time.monotonic() - start) * 1000)
        return {"healthy": resp.status_code < 400, "latency_ms": elapsed}
    except Exception as exc:
        return {"healthy": False, "error": str(type(exc).__name__)}


@app.get("/api/status")
async def get_service_status():
    """Check health of all backend services.

    When running inside Docker, services are reachable via container names.
    The OTEL_EXPORTER_OTLP_ENDPOINT env var tells us we are in Docker.
    """
    in_docker = "otel-collector" in settings.otel_exporter_otlp_endpoint
    if in_docker:
        checks = {
            "gateway": _check_service(f"http://localhost:{settings.gateway_port}/health"),
            "otel_collector": _check_service("http://otel-collector:8888/metrics"),
            "tempo": _check_service("http://tempo:3200/ready"),
            "prometheus": _check_service("http://prometheus:9090/-/ready"),
            "loki": _check_service("http://loki:3100/ready"),
            "grafana": _check_service("http://grafana:3000/api/health"),
        }
    else:
        checks = {
            "gateway": _check_service(f"http://localhost:{settings.gateway_port}/health"),
            "otel_collector": _check_service("http://localhost:8888/metrics"),
            "tempo": _check_service("http://localhost:3200/ready"),
            "prometheus": _check_service("http://localhost:9090/-/ready"),
            "loki": _check_service("http://localhost:3100/ready"),
            "grafana": _check_service("http://localhost:3000/api/health"),
        }
    results = await asyncio.gather(*checks.values(), return_exceptions=True)
    services = {}
    for name, result in zip(checks.keys(), results):
        if isinstance(result, Exception):
            services[name] = {"healthy": False, "error": str(result)}
        else:
            services[name] = result
    return {"services": services}


# ---------------------------------------------------------------------------
# Dashboard Stats API
# ---------------------------------------------------------------------------


@app.get("/api/dashboard/stats")
async def get_dashboard_stats():
    """Aggregated statistics for the dashboard.

    Combines request log statistics with live subsystem data (cache stats,
    provider health via circuit breaker) to give a single-call overview.
    """
    stats = request_log_store.get_stats()

    # Add recent requests
    stats["recent_requests"] = [
        log.model_dump() for log in request_log_store.recent(10)
    ]

    # Provider health from circuit breaker (if available)
    provider_health: dict = {}
    if circuit_breaker is not None:
        try:
            provider_health = circuit_breaker.get_states()
        except Exception:
            pass
    stats["provider_health"] = provider_health

    return stats


# ---------------------------------------------------------------------------
# Virtual Keys API (/api/keys/*)
# ---------------------------------------------------------------------------


class CreateKeyRequest(BaseModel):
    """Request body for creating a virtual key."""
    name: str
    owner: Optional[str] = None
    team: Optional[str] = None
    allowed_providers: Optional[list[str]] = None
    allowed_models: Optional[list[str]] = None
    budget_usd: Optional[float] = None
    tags: Optional[dict[str, str]] = None


@app.post("/api/keys")
async def create_virtual_key(body: CreateKeyRequest):
    """Create a new virtual API key."""
    if key_manager is None:
        raise HTTPException(status_code=501, detail="Virtual key management not available")
    try:
        raw_key, key_obj = key_manager.create_key(
            name=body.name,
            owner=body.owner,
            team=body.team,
            allowed_providers=body.allowed_providers,
            allowed_models=body.allowed_models,
            budget_usd=body.budget_usd,
            tags=body.tags or {},
        )
        return {"key": raw_key, "key_id": key_obj.key_id, "name": key_obj.name}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/keys")
async def list_virtual_keys():
    """List all virtual API keys."""
    if key_manager is None:
        raise HTTPException(status_code=501, detail="Virtual key management not available")
    try:
        keys = key_manager.list_keys()
        return {"keys": keys}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/keys/{key_id}")
async def get_virtual_key(key_id: str):
    """Get details for a specific virtual key."""
    if key_manager is None:
        raise HTTPException(status_code=501, detail="Virtual key management not available")
    try:
        key = key_manager.get_key(key_id)
        if key is None:
            raise HTTPException(status_code=404, detail=f"Key not found: {key_id}")
        return key
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/keys/{key_id}")
async def revoke_virtual_key(key_id: str):
    """Revoke (delete) a virtual key."""
    if key_manager is None:
        raise HTTPException(status_code=501, detail="Virtual key management not available")
    try:
        success = key_manager.revoke_key(key_id)
        if not success:
            raise HTTPException(status_code=404, detail=f"Key not found: {key_id}")
        return {"status": "revoked", "key_id": key_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/keys/{key_id}/usage")
async def get_key_usage(key_id: str):
    """Get usage statistics for a virtual key."""
    if key_manager is None:
        raise HTTPException(status_code=501, detail="Virtual key management not available")
    try:
        usage = key_manager.get_budget_status(key_id)
        if usage is None:
            raise HTTPException(status_code=404, detail=f"Key not found: {key_id}")
        return usage
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/keys/{key_id}/rotate")
async def rotate_virtual_key(key_id: str, grace_period_seconds: int = 3600):
    """Rotate a virtual key — creates a new key with identical config.

    The old key remains valid for ``grace_period_seconds`` (default 1 hour)
    so callers can roll forward without downtime. After the grace period,
    only the new key is accepted.

    Returns the NEW raw key (shown once — cannot be recovered).
    """
    if key_manager is None:
        raise HTTPException(status_code=501, detail="Virtual key management not available")
    try:
        result = key_manager.rotate_key(key_id, grace_period_seconds=grace_period_seconds)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Key not found: {key_id}")
        raw_key, new_vk = result
        return {
            "old_key_id": key_id,
            "new_key_id": new_vk.key_id,
            "new_key": raw_key,
            "grace_period_seconds": grace_period_seconds,
            "grace_expires_at": time.time() + grace_period_seconds,
            "message": "Old key accepted until grace expires. Update clients now.",
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/keys/{key_id}/audit")
async def get_key_audit(key_id: str, limit: int = 50):
    """Return the recent usage audit trail for a virtual key.

    Includes timestamp, IP address, endpoint, status, model, tokens, cost
    for each request served by the key. Plus the unique IP set.
    """
    if key_manager is None:
        raise HTTPException(status_code=501, detail="Virtual key management not available")
    try:
        events = key_manager.get_usage_events(key_id, limit=limit)
        vk = key_manager.get_key(key_id)
        if vk is None:
            raise HTTPException(status_code=404, detail=f"Key not found: {key_id}")
        return {
            "key_id": key_id,
            "total_events": len(vk.recent_usage),
            "unique_ips": vk.recent_ips,
            "unique_ip_count": len(vk.recent_ips),
            "last_used_at": vk.last_used_at,
            "rotation_ttl_seconds": vk.rotation_ttl_seconds,
            "last_rotated_at": vk.last_rotated_at,
            "rotation_grace_expires_at": vk.rotation_grace_expires_at,
            "events": [e.model_dump() for e in events],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Routing API (/api/routing/*)
# ---------------------------------------------------------------------------


@app.get("/api/routing/config")
async def get_routing_config():
    """Get the current routing configuration."""
    if routing_engine is None:
        raise HTTPException(status_code=501, detail="Routing engine not available")
    try:
        config = routing_engine.get_stats()
        return {"routing_stats": config}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.put("/api/routing/config")
async def update_routing_config(body: dict):
    """Update the routing configuration.

    Accepts a JSON body describing the new routing strategy and targets.
    The exact schema depends on the RoutingEngine implementation.
    """
    if routing_engine is None:
        raise HTTPException(status_code=501, detail="Routing engine not available")
    try:
        # Delegate to engine -- it may support a configure() or similar method
        if hasattr(routing_engine, "configure"):
            routing_engine.configure(body)
        return {"status": "updated", "config": body}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/routing/stats")
async def get_routing_stats():
    """Get routing performance statistics."""
    if routing_engine is None:
        raise HTTPException(status_code=501, detail="Routing engine not available")
    try:
        return routing_engine.get_stats()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/routing/circuit-breaker")
async def get_circuit_breaker_states():
    """Get the current state of all circuit breakers."""
    if circuit_breaker is None:
        raise HTTPException(status_code=501, detail="Circuit breaker not available")
    try:
        states = circuit_breaker.get_all_states()
        return {"circuit_breakers": states}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Cache API (/api/cache/*)
# ---------------------------------------------------------------------------


@app.get("/api/cache/stats")
async def get_cache_stats():
    """Get cache statistics (hit rate, entries, savings)."""
    if cache_engine is None:
        raise HTTPException(status_code=501, detail="Cache engine not available")
    try:
        return cache_engine.get_stats()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/cache/clear")
async def clear_cache():
    """Clear all cache entries and reset statistics."""
    if cache_engine is None:
        raise HTTPException(status_code=501, detail="Cache engine not available")
    try:
        cache_engine.clear()
        return {"status": "cleared"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Logs API (/api/logs/*)
# ---------------------------------------------------------------------------


@app.get("/api/logs")
async def get_request_logs(
    provider: Optional[str] = Query(None, description="Filter by provider"),
    model: Optional[str] = Query(None, description="Filter by model"),
    status: Optional[str] = Query(None, description="Filter by status (success/error)"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    limit: int = Query(50, ge=1, le=200, description="Page size"),
):
    """Get recent request logs with optional filtering and pagination."""
    logs, total = request_log_store.query(
        provider=provider,
        model=model,
        status=status,
        offset=offset,
        limit=limit,
    )
    return {
        "logs": [log.model_dump() for log in logs],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@app.get("/api/logs/{request_id}")
async def get_request_log_detail(request_id: str):
    """Get detailed information for a specific request log entry."""
    log = request_log_store.get(request_id)
    if log is None:
        raise HTTPException(status_code=404, detail=f"Request log not found: {request_id}")
    return log.model_dump()


# ---------------------------------------------------------------------------
# Authentication routes (login / logout / admin page)
# ---------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Render the login page."""
    if auth_manager is None:
        return RedirectResponse("/dashboard", status_code=302)
    # If already logged in, redirect to dashboard
    if get_current_user is not None:
        user = await get_current_user(request)
        if user is not None:
            return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def login_submit(request: Request):
    """Handle login form submission."""
    if auth_manager is None:
        return RedirectResponse("/dashboard", status_code=302)
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    session_id = auth_manager.authenticate(str(username), str(password))
    if not session_id:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid username or password"},
        )
    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie("session_id", session_id, httponly=True, max_age=86400, samesite="lax")
    return response


@app.get("/logout")
async def logout(request: Request):
    """Log out the current user."""
    session_id = request.cookies.get("session_id")
    if session_id and auth_manager is not None:
        auth_manager.logout(session_id)
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session_id")
    return response


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Admin panel page (admin role required)."""
    if auth_manager is None:
        raise HTTPException(status_code=501, detail="Auth module not available")
    ctx = await _user_context(request)
    user = ctx.get("current_user")
    if user is None:
        return RedirectResponse("/login", status_code=302)
    if require_role is not None:
        require_role(user, Role.ADMIN)
    ctx["active_page"] = "admin"
    return templates.TemplateResponse("admin.html", ctx)


# ---------------------------------------------------------------------------
# User Management API (/api/users/*)
# ---------------------------------------------------------------------------


@app.get("/api/users/me")
async def get_current_user_info(request: Request):
    """Returns current logged-in user info."""
    if auth_manager is None:
        return {"user": None, "auth_enabled": False}
    if get_current_user is None:
        return {"user": None, "auth_enabled": False}
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    data = user.model_dump()
    data["password_hash"] = "***"
    return {"user": data}


@app.post("/api/users")
async def create_user_api(request: Request):
    """Create a new user (admin only)."""
    if auth_manager is None:
        raise HTTPException(status_code=501, detail="Auth module not available")
    # Check admin permission
    if get_current_user is not None:
        caller = await get_current_user(request)
        if caller is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if require_role is not None:
            require_role(caller, Role.ADMIN)

    body = await request.json()
    try:
        role_val = body.get("role", "viewer")
        role_enum = Role(role_val) if Role is not None else role_val
        user = auth_manager.create_user(
            username=body.get("username", ""),
            email=body.get("email", ""),
            password=body.get("password", "changeme"),
            role=role_enum,
            display_name=body.get("display_name", ""),
            teams=body.get("teams", []),
        )
        data = user.model_dump()
        data["password_hash"] = "***"
        return {"user": data}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/users")
async def list_users_api(request: Request):
    """List all users (admin only)."""
    if auth_manager is None:
        raise HTTPException(status_code=501, detail="Auth module not available")
    if get_current_user is not None:
        caller = await get_current_user(request)
        if caller is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if require_role is not None:
            require_role(caller, Role.ADMIN)
    return {"users": auth_manager.list_users()}


@app.get("/api/users/{user_id}")
async def get_user_api(request: Request, user_id: str):
    """Get user by ID."""
    if auth_manager is None:
        raise HTTPException(status_code=501, detail="Auth module not available")
    if get_current_user is not None:
        caller = await get_current_user(request)
        if caller is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
    user = auth_manager.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"User not found: {user_id}")
    data = user.model_dump()
    data["password_hash"] = "***"
    return {"user": data}


@app.put("/api/users/{user_id}")
async def update_user_api(request: Request, user_id: str):
    """Update user properties (admin only)."""
    if auth_manager is None:
        raise HTTPException(status_code=501, detail="Auth module not available")
    if get_current_user is not None:
        caller = await get_current_user(request)
        if caller is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if require_role is not None:
            require_role(caller, Role.ADMIN)

    body = await request.json()
    kwargs = {}
    if "role" in body and Role is not None:
        kwargs["role"] = Role(body["role"])
    if "enabled" in body:
        kwargs["enabled"] = bool(body["enabled"])
    if "display_name" in body:
        kwargs["display_name"] = body["display_name"]
    if "teams" in body:
        kwargs["teams"] = body["teams"]
    if "email" in body:
        kwargs["email"] = body["email"]

    user = auth_manager.update_user(user_id, **kwargs)
    if user is None:
        raise HTTPException(status_code=404, detail=f"User not found: {user_id}")
    data = user.model_dump()
    data["password_hash"] = "***"
    return {"user": data}


@app.delete("/api/users/{user_id}")
async def delete_user_api(request: Request, user_id: str):
    """Delete a user (admin only)."""
    if auth_manager is None:
        raise HTTPException(status_code=501, detail="Auth module not available")
    if get_current_user is not None:
        caller = await get_current_user(request)
        if caller is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if require_role is not None:
            require_role(caller, Role.ADMIN)

    success = auth_manager.delete_user(user_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"User not found: {user_id}")
    return {"status": "deleted", "user_id": user_id}


@app.put("/api/users/{user_id}/password")
async def change_password_api(request: Request, user_id: str):
    """Change user password (admin only)."""
    if auth_manager is None:
        raise HTTPException(status_code=501, detail="Auth module not available")
    if get_current_user is not None:
        caller = await get_current_user(request)
        if caller is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if require_role is not None:
            require_role(caller, Role.ADMIN)

    body = await request.json()
    new_password = body.get("password", "")
    if not new_password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")

    success = auth_manager.change_password(user_id, new_password)
    if not success:
        raise HTTPException(status_code=404, detail=f"User not found: {user_id}")
    return {"status": "password_changed", "user_id": user_id}


# ---------------------------------------------------------------------------
# Sessions API (/api/sessions/*)
# ---------------------------------------------------------------------------


@app.get("/api/sessions")
async def list_sessions_api(request: Request):
    """List active sessions (admin only)."""
    if auth_manager is None:
        raise HTTPException(status_code=501, detail="Auth module not available")
    if get_current_user is not None:
        caller = await get_current_user(request)
        if caller is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if require_role is not None:
            require_role(caller, Role.ADMIN)
    return {"sessions": auth_manager.list_sessions()}


@app.delete("/api/sessions/{session_id}")
async def revoke_session_api(request: Request, session_id: str):
    """Revoke a specific session (admin only)."""
    if auth_manager is None:
        raise HTTPException(status_code=501, detail="Auth module not available")
    if get_current_user is not None:
        caller = await get_current_user(request)
        if caller is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if require_role is not None:
            require_role(caller, Role.ADMIN)

    success = auth_manager.revoke_session(session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "revoked", "session_id": session_id}


# ---------------------------------------------------------------------------
# Admin System Info API
# ---------------------------------------------------------------------------


@app.get("/api/admin/system-info")
async def admin_system_info(request: Request):
    """Get system info for admin page (admin only)."""
    if auth_manager is None:
        raise HTTPException(status_code=501, detail="Auth module not available")
    if get_current_user is not None:
        caller = await get_current_user(request)
        if caller is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if require_role is not None:
            require_role(caller, Role.ADMIN)
    return {"system_info": auth_manager.get_system_info()}


# ---------------------------------------------------------------------------
# Enhanced Gateway -- /v1/chat/completions override
# ---------------------------------------------------------------------------
# The enhanced endpoint wraps the original gateway router logic with:
#   - Virtual key extraction from Authorization header
#   - Gateway pipeline (middleware, cache, rate limit, guardrails)
#   - Request logging to RequestLogStore
#   - Gateway-specific response headers
#
# If gateway_pipeline is not available, requests fall through to the
# original router mounted at /v1 (src.gateway.router).
# ---------------------------------------------------------------------------


@app.post("/v1/chat/completions")
async def enhanced_chat_completions(request: Request):
    """Enhanced OpenAI-compatible chat completions with full gateway features.

    This endpoint intercepts /v1/chat/completions before the gateway_router
    handles it. It adds virtual key auth, caching, guardrails, and logging.
    If the advanced gateway_pipeline is not available, falls back to the
    standard provider-direct path via the mounted gateway_router.
    """
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    logger = structlog.get_logger("llm-o11y-gateway")

    # Parse request body
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON request body")

    # Extract virtual key from Authorization header
    auth_header = request.headers.get("authorization", "")
    virtual_key: Optional[str] = None
    if auth_header.startswith("Bearer sk-llmo-"):
        virtual_key = auth_header.replace("Bearer ", "").strip()

    # Capture client IP + user agent for audit trail
    client_ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                 or (request.client.host if request.client else ""))
    client_ua = request.headers.get("user-agent", "")[:200]

    # Validate virtual key if key_manager is available and key is present
    key_info = None
    if virtual_key and key_manager is not None:
        try:
            key_info = key_manager.validate_key(virtual_key)
            if key_info is None:
                raise HTTPException(status_code=401, detail="Invalid virtual key")
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("virtual_key_validation_error", error=str(exc))

    provider = body.get("provider", "openai")
    model = body.get("model", "gpt-4o")

    # Extract gateway extension fields from extra_body or top-level
    routing_strategy = body.pop("routing_strategy", None)
    cache_mode = body.pop("cache_mode", None)
    retry_config = body.pop("retry_config", None)
    guardrails_config = body.pop("guardrails", None)

    # --- Layer 3: Pre-flight context window validation ---
    # Reject requests that will overflow the target model's context window
    # BEFORE forwarding to the provider (saves cost + latency).
    try:
        from src.gateway.context_window import (
            ContextWindowExceededError,
            validate_context_window,
        )
        from src.models.telemetry import ChatMessage as _CtxMsg
        ctx_messages = [
            _CtxMsg(role=m.get("role", "user"), content=m.get("content", ""))
            for m in body.get("messages", [])
            if isinstance(m, dict)
        ]
        ctx_info = validate_context_window(
            model=model,
            messages=ctx_messages,
            max_tokens=body.get("max_tokens"),
        )
        logger.debug("context_window_check_passed", **ctx_info)
    except ContextWindowExceededError as ctx_exc:
        logger.warning(
            "context_window_exceeded",
            model=model,
            estimated_tokens=ctx_exc.estimated_tokens,
            max_tokens=ctx_exc.max_tokens,
            limit=ctx_exc.limit,
        )
        raise HTTPException(
            status_code=400,
            detail={
                "error": "context_length_exceeded",
                "message": str(ctx_exc),
                "model": model,
                "estimated_input_tokens": ctx_exc.estimated_tokens,
                "max_tokens": ctx_exc.max_tokens,
                "context_window": ctx_exc.limit,
            },
        )
    except Exception as ctx_exc:
        # Never block a request because of a bug in the estimator
        logger.debug("context_window_check_skipped", error=str(ctx_exc))

    cache_hit = False
    response_data: Optional[dict] = None
    error_msg: Optional[str] = None
    status_str = "success"

    try:
        # --- Cache lookup ---
        if cache_engine is not None and cache_mode and cache_mode != "none":
            from src.gateway.cache import CacheMode
            mode = CacheMode(cache_mode) if cache_mode in ("simple", "semantic") else CacheMode.SIMPLE
            cached = cache_engine.get(
                messages=body.get("messages", []),
                model=model,
                mode=mode,
            )
            if cached is not None:
                cache_hit = True
                response_data = cached

        # --- Gateway pipeline or direct provider call ---
        if response_data is None:
            if gateway_pipeline is not None:
                # Use the advanced gateway pipeline
                response_data = await gateway_pipeline(
                    body=body,
                    routing_strategy=routing_strategy,
                    cache_mode=cache_mode,
                    retry_config=retry_config,
                    guardrails=guardrails_config,
                    virtual_key=virtual_key,
                    request_id=request_id,
                )
            else:
                # Fall back to the standard provider path
                from src.models.telemetry import ChatCompletionRequest
                from src.providers import get_provider
                from src.otel.llm_spans import emit_llm_span

                chat_request = ChatCompletionRequest(**body)
                provider_adapter = get_provider(chat_request.provider)
                response_obj = await provider_adapter.chat_completion(chat_request)
                response_data = response_obj.model_dump()

                # Emit OTel telemetry via the existing path
                from src.models.telemetry import LLMRequestRecord
                latency_ms = (time.perf_counter() - start_time) * 1000
                record = LLMRequestRecord(
                    request_id=request_id,
                    provider=chat_request.provider,
                    model=chat_request.model,
                    messages=chat_request.messages,
                    response_model=response_obj.model if response_obj else None,
                    prompt_tokens=response_obj.usage.prompt_tokens if response_obj else 0,
                    completion_tokens=response_obj.usage.completion_tokens if response_obj else 0,
                    total_tokens=response_obj.usage.total_tokens if response_obj else 0,
                    cost_usd=response_obj.cost_usd if response_obj and response_obj.cost_usd else 0.0,
                    latency_ms=latency_ms,
                    status="success",
                    user_id=chat_request.user_id,
                    session_id=chat_request.session_id,
                    tags=chat_request.tags,
                )
                emit_llm_span(record)

            # --- Cache store ---
            if cache_engine is not None and cache_mode and cache_mode != "none" and not cache_hit:
                from src.gateway.cache import CacheMode
                cache_engine.put(
                    messages=body.get("messages", []),
                    model=model,
                    response=response_data,
                )

    except HTTPException:
        raise
    except Exception as exc:
        status_str = "error"
        error_msg = str(exc)
        logger.error(
            "enhanced_chat_completion_error",
            request_id=request_id,
            error=error_msg,
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Gateway error: {type(exc).__name__}: {exc}",
        ) from exc
    finally:
        # --- Log every request to RequestLogStore ---
        latency_ms = (time.perf_counter() - start_time) * 1000

        # Extract token/cost info from response
        usage = {}
        cost_usd = 0.0
        if response_data and isinstance(response_data, dict):
            usage = response_data.get("usage", {})
            if isinstance(usage, dict):
                pass
            else:
                usage = {}
            cost_usd = response_data.get("cost_usd", 0.0) or 0.0

        prompt_tokens = usage.get("prompt_tokens", 0) if isinstance(usage, dict) else 0
        completion_tokens = usage.get("completion_tokens", 0) if isinstance(usage, dict) else 0
        total_tokens = usage.get("total_tokens", 0) if isinstance(usage, dict) else 0

        # Build input/output previews
        input_preview = ""
        messages = body.get("messages", [])
        if messages:
            last_msg = messages[-1]
            content = last_msg.get("content", "") if isinstance(last_msg, dict) else ""
            input_preview = content[:500]

        output_preview = ""
        if response_data and isinstance(response_data, dict):
            choices = response_data.get("choices", [])
            if choices:
                first_choice = choices[0]
                if isinstance(first_choice, dict):
                    msg = first_choice.get("message", {})
                    if isinstance(msg, dict):
                        output_preview = msg.get("content", "")[:500]

        log_entry = RequestLog(
            request_id=request_id,
            timestamp=time.time(),
            provider=provider if isinstance(provider, str) else str(provider),
            model=model,
            status=status_str,
            latency_ms=round(latency_ms, 2),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
            user_id=body.get("user_id"),
            session_id=body.get("session_id"),
            cache_hit=cache_hit,
            error=error_msg,
            input_preview=input_preview,
            output_preview=output_preview,
        )
        request_log_store.add(log_entry)

        # --- Audit trail: record usage event on the virtual key ---
        if key_info is not None and key_manager is not None:
            try:
                from src.gateway.virtual_keys import KeyUsageEvent
                event = KeyUsageEvent(
                    timestamp=time.time(),
                    ip_address=client_ip,
                    user_agent=client_ua,
                    endpoint="/v1/chat/completions",
                    status=status_str,
                    model=model,
                    tokens=total_tokens,
                    cost_usd=cost_usd,
                )
                key_manager.record_usage_event(key_info.key_id, event)
            except Exception as audit_exc:
                logger.debug("audit_event_failed", error=str(audit_exc))

    # --- Build response with gateway headers ---
    headers = {
        "X-Request-ID": request_id,
        "X-Cache-Status": "HIT" if cache_hit else "MISS",
        "X-Provider": provider if isinstance(provider, str) else str(provider),
        "X-Latency-Ms": str(round(latency_ms, 2)),
    }

    return JSONResponse(content=response_data, headers=headers)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the gateway with Uvicorn."""
    uvicorn.run(
        "src.app:app",
        host="0.0.0.0",
        port=settings.gateway_port,
        log_level=settings.log_level,
        reload=False,
    )


if __name__ == "__main__":
    main()
