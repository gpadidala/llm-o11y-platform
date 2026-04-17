"""FastAPI router for the Guardrails API.

Provides endpoints for input/output safety checks, PII redaction,
and guardrail configuration management.

Endpoints:
  POST /guardrails/check-input  -- Check input messages against guardrails
  POST /guardrails/check-output -- Check output content against guardrails
  POST /guardrails/redact       -- Redact PII from text
  GET  /guardrails/config       -- Get current guardrail configuration
  PUT  /guardrails/config       -- Update guardrail configuration
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.guardrails.engine import (
    GuardrailAction,
    GuardrailConfig,
    GuardrailResult,
    guardrails_engine,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Guardrails"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class CheckInputRequest(BaseModel):
    """Request body for checking input messages."""

    messages: List[Dict[str, str]]
    config: Optional[GuardrailConfig] = None


class CheckOutputRequest(BaseModel):
    """Request body for checking output content."""

    content: str
    config: Optional[GuardrailConfig] = None


class RedactRequest(BaseModel):
    """Request body for redacting PII from text."""

    text: str


class CheckResponse(BaseModel):
    """Response body for guardrail checks."""

    passed: bool
    results: List[Dict[str, Any]]
    blocked: bool
    summary: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/check-input", response_model=CheckResponse)
async def check_input(request: CheckInputRequest) -> CheckResponse:
    """Check input messages against all enabled guardrails.

    Returns a summary of all guardrail results, including whether any
    check blocked the content.
    """
    results = await guardrails_engine.check_input(
        messages=request.messages,
        config=request.config,
    )

    all_passed = all(r.passed for r in results)
    any_blocked = any(r.action == GuardrailAction.BLOCK for r in results)

    # Build summary
    if all_passed:
        summary = "All input guardrails passed"
    elif any_blocked:
        blocked_rules = [r.rule_name for r in results if r.action == GuardrailAction.BLOCK]
        summary = f"Input blocked by: {', '.join(blocked_rules)}"
    else:
        warn_rules = [r.rule_name for r in results if not r.passed]
        summary = f"Input warnings from: {', '.join(warn_rules)}"

    logger.info(
        "guardrails_input_checked",
        passed=all_passed,
        blocked=any_blocked,
        check_count=len(results),
    )

    return CheckResponse(
        passed=all_passed,
        results=[r.model_dump() for r in results],
        blocked=any_blocked,
        summary=summary,
    )


@router.post("/check-output", response_model=CheckResponse)
async def check_output(request: CheckOutputRequest) -> CheckResponse:
    """Check LLM output content against all enabled guardrails.

    Returns a summary of all guardrail results, including whether any
    check blocked or flagged the content.
    """
    results = await guardrails_engine.check_output(
        content=request.content,
        config=request.config,
    )

    all_passed = all(r.passed for r in results)
    any_blocked = any(r.action == GuardrailAction.BLOCK for r in results)

    if all_passed:
        summary = "All output guardrails passed"
    elif any_blocked:
        blocked_rules = [r.rule_name for r in results if r.action == GuardrailAction.BLOCK]
        summary = f"Output blocked by: {', '.join(blocked_rules)}"
    else:
        warn_rules = [r.rule_name for r in results if not r.passed]
        summary = f"Output warnings from: {', '.join(warn_rules)}"

    logger.info(
        "guardrails_output_checked",
        passed=all_passed,
        blocked=any_blocked,
        check_count=len(results),
    )

    return CheckResponse(
        passed=all_passed,
        results=[r.model_dump() for r in results],
        blocked=any_blocked,
        summary=summary,
    )


@router.post("/redact")
async def redact_pii(request: RedactRequest) -> Dict[str, Any]:
    """Redact all detected PII from the given text.

    Returns both the redacted text and details about what was found.
    """
    from src.guardrails.pii import pii_detector

    detection = pii_detector.scan(request.text)

    logger.info(
        "pii_redacted",
        total_found=detection.total_found,
        pii_types=[m.pii_type for m in detection.matches],
    )

    return {
        "original_length": len(request.text),
        "redacted_text": detection.redacted_text,
        "pii_found": detection.total_found,
        "matches": [m.model_dump() for m in detection.matches],
    }


@router.get("/config")
async def get_config() -> Dict[str, Any]:
    """Get the current default guardrail configuration."""
    return {"config": guardrails_engine.config.model_dump()}


@router.put("/config")
async def update_config(config: GuardrailConfig) -> Dict[str, Any]:
    """Update the default guardrail configuration.

    This sets the engine-wide defaults. Individual requests can still
    override the config by passing it in the request body.
    """
    guardrails_engine.config = config

    logger.info(
        "guardrails_config_updated",
        pii_detection=config.enable_pii_detection,
        content_safety=config.enable_content_safety,
        topic_restriction=config.enable_topic_restriction,
        output_validation=config.enable_output_validation,
    )

    return {"status": "updated", "config": config.model_dump()}
