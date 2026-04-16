"""MCP telemetry ingestion API -- receive tool call and session data.

Agents and MCP clients POST telemetry events here so the gateway can emit
OpenTelemetry spans and metrics on their behalf.  Session tracking is
delegated to :class:`~src.otel.mcp_spans.MCPSessionTracker` which
aggregates per-session tool calls and emits a summary span on session end.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter

from src.models.telemetry import MCPSessionEnd, MCPSessionStart, MCPToolCallRecord
from src.otel.mcp_spans import emit_mcp_tool_span, MCPSessionTracker
import src.otel.setup as otel_setup

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["mcp-tracer"])

# Module-level session tracker shared across requests in this process.
# For multi-process deployments, swap for a Redis-backed implementation.
session_tracker = MCPSessionTracker()


# ---------------------------------------------------------------------------
# Tool call ingestion
# ---------------------------------------------------------------------------


@router.post("/tool-call", status_code=201)
async def ingest_tool_call(record: MCPToolCallRecord):
    """Ingest a single MCP tool call and emit telemetry.

    1. Emit an OTel span via ``emit_mcp_tool_span``.
    2. Update tool-call counter and latency histogram.
    3. If the call belongs to a tracked session, record it there.
    4. Structured log for operational visibility.
    """
    if record.session_id:
        # The session tracker's record_tool_call already emits the span
        # and updates metrics internally, so we delegate entirely to it.
        session_tracker.record_tool_call(record.session_id, record)
    else:
        # No session -- emit the span and metrics directly.
        emit_mcp_tool_span(record)

    # Structured log
    logger.info(
        "mcp_tool_call_ingested",
        server=record.server_name,
        tool=record.tool_name,
        latency_ms=round(record.latency_ms, 2),
        status=record.status,
    )

    return {"status": "ok", "server": record.server_name, "tool": record.tool_name}


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


@router.post("/session/start", status_code=201)
async def start_session(req: MCPSessionStart):
    """Start tracking an MCP session.

    Subsequent ``/tool-call`` payloads carrying the same ``session_id``
    will be aggregated until ``/session/end`` is called.
    """
    session_tracker.start_session(
        session_id=req.session_id,
        agent_name=req.agent_name,
        user_id=req.user_id,
    )

    logger.info(
        "mcp_session_started",
        session_id=req.session_id,
        agent=req.agent_name,
    )

    return {"status": "ok", "session_id": req.session_id}


@router.post("/session/end", status_code=201)
async def end_session(req: MCPSessionEnd):
    """End an MCP session and emit aggregated telemetry.

    Returns a summary containing tool call count, total latency,
    accumulated cost, and per-tool breakdowns.
    """
    summary = session_tracker.end_session(req.session_id)

    if summary is not None:
        # Update session cost metric (the tracker already emits its own
        # metric, but the router also records it with a session-scoped label
        # so that per-session cost queries work from either source).
        total_cost = summary.get("total_cost_usd", 0)
        if otel_setup.mcp_session_cost_counter is not None and total_cost > 0:
            otel_setup.mcp_session_cost_counter.add(
                total_cost,
                {
                    "session_id": req.session_id,
                    "agent_name": summary.get("agent_name") or "unknown",
                },
            )

        logger.info(
            "mcp_session_ended",
            session_id=req.session_id,
            duration_ms=summary.get("duration_ms"),
            tool_calls=summary.get("tool_call_count"),
            total_cost=summary.get("total_cost_usd"),
        )

        return {
            "status": "ok",
            "session_id": req.session_id,
            "summary": summary,
        }

    logger.warning("mcp_session_not_found", session_id=req.session_id)
    return {"status": "not_found", "session_id": req.session_id}
