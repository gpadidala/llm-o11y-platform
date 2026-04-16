"""MCP tool instrumentation -- decorator for auto-tracing MCP tool calls.

Provides :func:`trace_mcp_tool`, a decorator that wraps async MCP tool
functions to automatically:

1. Measure wall-clock latency.
2. Build an :class:`~src.models.telemetry.MCPToolCallRecord`.
3. Emit an OpenTelemetry span via :func:`~src.otel.mcp_spans.emit_mcp_tool_span`.
4. Optionally forward the record to a remote gateway over HTTP.

Usage::

    @trace_mcp_tool("my-server", "1.0.0")
    async def search_docs(query: str) -> dict:
        results = await do_search(query)
        return {"results": results}

    # With remote gateway forwarding:
    @trace_mcp_tool("my-server", "1.0.0", gateway_url="http://gateway:8080")
    async def search_docs(query: str) -> dict:
        ...
"""

from __future__ import annotations

import functools
import time
from typing import Any, Callable, Optional

import httpx
import structlog

from src.models.telemetry import MCPToolCallRecord
from src.otel.mcp_spans import emit_mcp_tool_span

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Public decorator
# ---------------------------------------------------------------------------


def trace_mcp_tool(
    server_name: str,
    server_version: str = "unknown",
    gateway_url: Optional[str] = None,
):
    """Decorator to auto-trace MCP tool calls.

    Parameters
    ----------
    server_name:
        Logical name of the MCP server (e.g. ``"filesystem-server"``).
    server_version:
        Version string of the MCP server for attribution.
    gateway_url:
        If provided, the telemetry record is also POSTed to
        ``{gateway_url}/v1/mcp/tool-call`` so a central gateway can
        aggregate metrics.  Failures are logged but never propagated.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tool_name = func.__name__
            start_time = time.perf_counter()
            status = "success"
            error: Optional[str] = None
            result: Any = None

            try:
                result = await func(*args, **kwargs)
                return result
            except Exception as exc:
                status = "error"
                error = str(exc)
                raise
            finally:
                latency_ms = (time.perf_counter() - start_time) * 1000

                # Build the record
                record = MCPToolCallRecord(
                    server_name=server_name,
                    tool_name=tool_name,
                    input_params=_safe_serialize(
                        kwargs or (args[0] if args else None)
                    ),
                    output_data=_safe_serialize(result) if result else None,
                    latency_ms=latency_ms,
                    status=status,
                    error=error,
                )

                # Emit span locally
                emit_mcp_tool_span(record)

                # Optionally forward to the central gateway
                if gateway_url:
                    await _send_to_gateway(gateway_url, record)

                logger.info(
                    "mcp_tool_traced",
                    server=server_name,
                    version=server_version,
                    tool=tool_name,
                    latency_ms=round(latency_ms, 2),
                    status=status,
                )

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _send_to_gateway(
    gateway_url: str, record: MCPToolCallRecord
) -> None:
    """POST the telemetry record to the central gateway.

    Uses a short timeout and swallows all errors so that tool execution
    is never blocked by telemetry forwarding.
    """
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{gateway_url.rstrip('/')}/v1/mcp/tool-call",
                json=record.model_dump(mode="json"),
                timeout=5.0,
            )
    except Exception as exc:
        logger.warning(
            "failed_to_send_mcp_telemetry",
            gateway_url=gateway_url,
            error=str(exc),
        )


def _safe_serialize(data: Any) -> dict:
    """Safely convert *data* to a dict, truncating large values.

    Returns an empty dict for ``None`` so that the Pydantic model never
    receives a non-dict value for ``input_params`` / ``output_data``.
    """
    if data is None:
        return {}
    if isinstance(data, dict):
        return {k: _truncate(v) for k, v in data.items()}
    try:
        return {"value": _truncate(data)}
    except Exception:
        return {"value": str(data)[:500]}


def _truncate(value: Any, max_len: int = 500) -> Any:
    """Truncate large string representations to *max_len* characters."""
    s = str(value)
    if len(s) > max_len:
        return s[:max_len] + "...[truncated]"
    return value
