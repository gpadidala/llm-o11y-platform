"""MCP Tracer -- tool call ingestion API and instrumentation decorator.

Public API
----------
- ``trace_mcp_tool``: Decorator for auto-tracing MCP tool functions.
- ``mcp_router``:     FastAPI router that exposes ``/tool-call``,
                      ``/session/start``, and ``/session/end`` endpoints.
"""

from src.mcp_tracer.interceptor import trace_mcp_tool
from src.mcp_tracer.router import router as mcp_router

__all__ = ["trace_mcp_tool", "mcp_router"]
