"""Emit OpenTelemetry spans for MCP tool calls and sessions."""

import json
import logging
import time
import threading
from typing import Any, Dict, List, Optional

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from src.models.telemetry import MCPToolCallRecord
import src.otel.setup as otel_setup

logger = logging.getLogger(__name__)

# Maximum length for serialised input/output stored as span events.
_MAX_EVENT_CONTENT_LEN = 4096


def _truncate_json(data: Optional[Dict[str, Any]], max_len: int = _MAX_EVENT_CONTENT_LEN) -> str:
    """Serialize *data* to JSON and truncate if the result exceeds *max_len*."""
    if data is None:
        return "{}"
    try:
        text = json.dumps(data, default=str)
    except (TypeError, ValueError):
        text = str(data)
    if len(text) <= max_len:
        return text
    return text[:max_len] + "...[truncated]"


def emit_mcp_tool_span(record: MCPToolCallRecord) -> None:
    """Create a span for an MCP tool call.

    Attributes follow a ``mcp.*`` namespace convention.  Corresponding
    counters and histograms are updated for dashboard consumption.
    """
    tracer = trace.get_tracer("llm-o11y-gateway")

    with tracer.start_as_current_span(
        name=f"mcp.tool/{record.server_name}/{record.tool_name}",
        kind=trace.SpanKind.CLIENT,
    ) as span:
        # -- Core MCP attributes -------------------------------------------
        span.set_attribute("mcp.server.name", record.server_name)
        span.set_attribute("mcp.tool.name", record.tool_name)
        span.set_attribute("mcp.tool.status", record.status)
        span.set_attribute("mcp.tool.latency_ms", record.latency_ms)

        # -- Token attribution ---------------------------------------------
        span.set_attribute("mcp.attributed.input_tokens", record.attributed_input_tokens)
        span.set_attribute("mcp.attributed.output_tokens", record.attributed_output_tokens)
        span.set_attribute("mcp.attributed.cost_usd", record.attributed_cost_usd)

        # -- Optional context identifiers ----------------------------------
        if record.session_id:
            span.set_attribute("mcp.session.id", record.session_id)
        if record.user_id:
            span.set_attribute("mcp.user.id", record.user_id)

        # -- Events for input / output -------------------------------------
        span.add_event(
            name="mcp.tool.input",
            attributes={"mcp.tool.input_params": _truncate_json(record.input_params)},
        )
        span.add_event(
            name="mcp.tool.output",
            attributes={"mcp.tool.output_data": _truncate_json(record.output_data)},
        )

        # -- Status --------------------------------------------------------
        if record.status == "success":
            span.set_status(StatusCode.OK)
        else:
            span.set_status(StatusCode.ERROR, description=record.error or "unknown")
            if record.error:
                span.record_exception(
                    Exception(record.error),
                    attributes={"exception.type": "MCPToolCallError"},
                )

    # -- Metrics -----------------------------------------------------------
    _emit_tool_metrics(record)


def _emit_tool_metrics(record: MCPToolCallRecord) -> None:
    """Update OTel counters and histograms for the given MCP tool call."""
    common_attrs = {
        "server_name": record.server_name,
        "tool_name": record.tool_name,
    }

    if otel_setup.mcp_tool_call_counter is not None:
        otel_setup.mcp_tool_call_counter.add(
            1, {**common_attrs, "status": record.status}
        )

    if otel_setup.mcp_tool_call_duration is not None:
        otel_setup.mcp_tool_call_duration.record(record.latency_ms, common_attrs)


# ---------------------------------------------------------------------------
# Session tracker
# ---------------------------------------------------------------------------


class MCPSessionTracker:
    """Track MCP sessions in-memory and emit aggregated session spans.

    Thread-safe: all mutations are guarded by a lock so the tracker can be
    used from concurrent request handlers without external synchronisation.
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # -- public API --------------------------------------------------------

    def start_session(
        self,
        session_id: str,
        agent_name: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        """Start tracking a new MCP session."""
        with self._lock:
            if session_id in self._sessions:
                logger.warning(
                    "Session %s already tracked -- overwriting", session_id
                )
            self._sessions[session_id] = {
                "start_time": time.time(),
                "agent_name": agent_name,
                "user_id": user_id,
                "tool_calls": [],
                "total_cost_usd": 0.0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
            }
            logger.debug("MCP session started: %s", session_id)

    def record_tool_call(
        self, session_id: str, record: MCPToolCallRecord
    ) -> None:
        """Record a tool call within an existing session.

        Also emits the individual tool-call span via :func:`emit_mcp_tool_span`.
        """
        emit_mcp_tool_span(record)

        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                logger.warning(
                    "Tool call for unknown session %s -- ignoring session tracking",
                    session_id,
                )
                return
            session["tool_calls"].append(
                {
                    "server_name": record.server_name,
                    "tool_name": record.tool_name,
                    "latency_ms": record.latency_ms,
                    "status": record.status,
                    "cost_usd": record.attributed_cost_usd,
                    "input_tokens": record.attributed_input_tokens,
                    "output_tokens": record.attributed_output_tokens,
                }
            )
            session["total_cost_usd"] += record.attributed_cost_usd
            session["total_input_tokens"] += record.attributed_input_tokens
            session["total_output_tokens"] += record.attributed_output_tokens

    def end_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """End a session, emit an aggregated span, and return a summary.

        Returns ``None`` if the session was not found.
        """
        with self._lock:
            session = self._sessions.pop(session_id, None)

        if session is None:
            logger.warning("Cannot end unknown session %s", session_id)
            return None

        duration_ms = (time.time() - session["start_time"]) * 1000.0
        tool_count = len(session["tool_calls"])
        success_count = sum(
            1 for tc in session["tool_calls"] if tc["status"] == "success"
        )
        error_count = tool_count - success_count

        summary: Dict[str, Any] = {
            "session_id": session_id,
            "agent_name": session.get("agent_name"),
            "user_id": session.get("user_id"),
            "duration_ms": round(duration_ms, 2),
            "tool_call_count": tool_count,
            "success_count": success_count,
            "error_count": error_count,
            "total_cost_usd": round(session["total_cost_usd"], 6),
            "total_input_tokens": session["total_input_tokens"],
            "total_output_tokens": session["total_output_tokens"],
            "tool_calls": session["tool_calls"],
        }

        # Emit aggregated session span
        self._emit_session_span(summary)

        # Emit session cost metric
        if otel_setup.mcp_session_cost_counter is not None and session["total_cost_usd"] > 0:
            attrs: Dict[str, str] = {"session_id": session_id}
            if session.get("agent_name"):
                attrs["agent_name"] = session["agent_name"]
            otel_setup.mcp_session_cost_counter.add(session["total_cost_usd"], attrs)

        logger.info(
            "MCP session ended: id=%s tools=%d cost=%.6f duration=%.0fms",
            session_id,
            tool_count,
            session["total_cost_usd"],
            duration_ms,
        )

        return summary

    # -- internal ----------------------------------------------------------

    @staticmethod
    def _emit_session_span(summary: Dict[str, Any]) -> None:
        """Emit a single parent span that summarises the entire session."""
        tracer = trace.get_tracer("llm-o11y-gateway")

        with tracer.start_as_current_span(
            name=f"mcp.session/{summary['session_id']}",
            kind=trace.SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("mcp.session.id", summary["session_id"])

            if summary.get("agent_name"):
                span.set_attribute("mcp.agent.name", summary["agent_name"])
            if summary.get("user_id"):
                span.set_attribute("mcp.user.id", summary["user_id"])

            span.set_attribute("mcp.session.duration_ms", summary["duration_ms"])
            span.set_attribute("mcp.session.tool_call_count", summary["tool_call_count"])
            span.set_attribute("mcp.session.success_count", summary["success_count"])
            span.set_attribute("mcp.session.error_count", summary["error_count"])
            span.set_attribute("mcp.session.total_cost_usd", summary["total_cost_usd"])
            span.set_attribute(
                "mcp.session.total_input_tokens", summary["total_input_tokens"]
            )
            span.set_attribute(
                "mcp.session.total_output_tokens", summary["total_output_tokens"]
            )

            # Add per-tool-call events for drill-down in trace viewers
            for idx, tc in enumerate(summary.get("tool_calls", [])):
                span.add_event(
                    name=f"mcp.tool_call.{idx}",
                    attributes={
                        "mcp.server.name": tc["server_name"],
                        "mcp.tool.name": tc["tool_name"],
                        "mcp.tool.latency_ms": tc["latency_ms"],
                        "mcp.tool.status": tc["status"],
                        "mcp.tool.cost_usd": tc["cost_usd"],
                    },
                )

            span.set_status(
                StatusCode.OK
                if summary["error_count"] == 0
                else StatusCode.ERROR
            )
