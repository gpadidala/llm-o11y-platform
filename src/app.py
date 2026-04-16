"""FastAPI application for the LLM O11y Gateway.

Startup lifecycle:
  1. Configure structured JSON logging via structlog.
  2. Initialize OpenTelemetry (traces, metrics, auto-instrumentation).

Routes:
  /                    -- web UI dashboard
  /settings            -- web UI settings page
  /providers           -- web UI provider status page
  /health              -- liveness probe
  /metrics             -- Prometheus scrape endpoint
  /api/settings        -- settings CRUD API
  /api/status          -- backend service health API
  /v1/chat/completions -- unified chat completion proxy
  /v1/mcp/*            -- MCP telemetry receiver
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
import structlog
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from src.config import settings
from src.gateway.router import router as gateway_router
from src.mcp_tracer.router import router as mcp_router
from src.otel.setup import init_telemetry, shutdown_telemetry


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    """Set up structlog for JSON-formatted, context-rich log output."""
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
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

    logger.info(
        "starting_gateway",
        port=settings.gateway_port,
        otel_endpoint=settings.otel_exporter_otlp_endpoint,
    )
    # OTel providers are initialised at module level (below) so that
    # FastAPI middleware can be added before the app starts.

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
        "providers and emits OpenTelemetry traces, metrics, and logs."
    ),
    version="0.1.0",
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

# ---- Routes ---------------------------------------------------------------

app.include_router(gateway_router, prefix="/v1")
app.include_router(mcp_router, prefix="/v1/mcp")


# ---------------------------------------------------------------------------
# Web UI pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def ui_home(request: Request):
    """Dashboard home page."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/settings", response_class=HTMLResponse)
async def ui_settings(request: Request):
    """Settings configuration page."""
    return templates.TemplateResponse("settings.html", {"request": request})


@app.get("/providers", response_class=HTMLResponse)
async def ui_providers(request: Request):
    """Provider status page."""
    return templates.TemplateResponse("providers.html", {"request": request})


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
