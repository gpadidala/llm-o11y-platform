"""FastAPI router for the Prompt Management API.

Provides CRUD operations for prompt templates, version history,
template rendering with variable interpolation, and live testing
against the gateway.

Endpoints:
  POST   /prompts              -- Create a new template
  GET    /prompts              -- List templates (with optional tag filter)
  GET    /prompts/{id}         -- Get a template by ID
  PUT    /prompts/{id}         -- Update a template (creates a new version)
  DELETE /prompts/{id}         -- Delete a template
  POST   /prompts/{id}/render  -- Render a template with variables
  GET    /prompts/{id}/versions -- Get version history
  POST   /prompts/{id}/test    -- Test a template against a live model
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.prompts.templates import prompt_store

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Prompt Management"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class CreatePromptRequest(BaseModel):
    """Request body for creating a new prompt template."""

    name: str
    content: str
    description: str = ""
    model: Optional[str] = None
    provider: Optional[str] = None
    variables: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    created_by: str = "system"
    variants: Optional[Dict[str, str]] = None


class UpdatePromptRequest(BaseModel):
    """Request body for updating a prompt template."""

    content: str
    change_note: str = ""
    created_by: str = "system"


class RenderPromptRequest(BaseModel):
    """Request body for rendering a template with variables."""

    variables: Dict[str, Any]
    variant: Optional[str] = None


class TestPromptRequest(BaseModel):
    """Request body for testing a template against a live model."""

    variables: Dict[str, Any]
    variant: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_prompt(request: CreatePromptRequest) -> Dict[str, Any]:
    """Create a new prompt template.

    Variables are auto-detected from ``{{var}}`` patterns in the content
    unless explicitly provided.
    """
    template = prompt_store.create(
        name=request.name,
        content=request.content,
        description=request.description,
        model=request.model,
        provider=request.provider,
        variables=request.variables,
        tags=request.tags,
        created_by=request.created_by,
        variants=request.variants,
    )
    logger.info("prompt_created", template_id=template.template_id, name=template.name)
    return {"status": "created", "template": template.model_dump()}


@router.get("")
async def list_prompts(
    tags: Optional[str] = Query(None, description="Comma-separated tag filter"),
) -> Dict[str, Any]:
    """List all prompt templates, optionally filtered by tags."""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    templates = prompt_store.list_all(tags=tag_list)
    return {
        "count": len(templates),
        "templates": [t.model_dump() for t in templates],
    }


@router.get("/{template_id}")
async def get_prompt(template_id: str) -> Dict[str, Any]:
    """Get a single prompt template by ID."""
    template = prompt_store.get(template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template not found: {template_id}")
    return {"template": template.model_dump()}


@router.put("/{template_id}")
async def update_prompt(template_id: str, request: UpdatePromptRequest) -> Dict[str, Any]:
    """Update a prompt template, creating a new version.

    The previous version is preserved in the version history.
    """
    try:
        template = prompt_store.update(
            template_id=template_id,
            content=request.content,
            change_note=request.change_note,
            created_by=request.created_by,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Template not found: {template_id}")

    logger.info(
        "prompt_updated",
        template_id=template_id,
        version=template.version,
    )
    return {"status": "updated", "template": template.model_dump()}


@router.delete("/{template_id}")
async def delete_prompt(template_id: str) -> Dict[str, Any]:
    """Delete a prompt template and its version history."""
    deleted = prompt_store.delete(template_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Template not found: {template_id}")
    logger.info("prompt_deleted", template_id=template_id)
    return {"status": "deleted", "template_id": template_id}


@router.post("/{template_id}/render")
async def render_prompt(template_id: str, request: RenderPromptRequest) -> Dict[str, Any]:
    """Render a prompt template by substituting variables.

    Returns the fully rendered text ready for use in an LLM call.
    """
    try:
        rendered = prompt_store.render(
            template_id=template_id,
            variables=request.variables,
            variant=request.variant,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Template not found: {template_id}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"rendered": rendered, "template_id": template_id}


@router.get("/{template_id}/versions")
async def get_prompt_versions(template_id: str) -> Dict[str, Any]:
    """Get the full version history for a prompt template."""
    try:
        versions = prompt_store.get_versions(template_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Template not found: {template_id}")

    return {
        "template_id": template_id,
        "count": len(versions),
        "versions": [v.model_dump() for v in versions],
    }


@router.post("/{template_id}/test")
async def test_prompt(template_id: str, request: TestPromptRequest) -> Dict[str, Any]:
    """Test a prompt template by rendering it and sending it to the gateway.

    This renders the template with the given variables and then makes a
    live call to the LLM gateway to see the actual model output.
    """
    # Step 1: Render the template
    try:
        rendered = prompt_store.render(
            template_id=template_id,
            variables=request.variables,
            variant=request.variant,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Template not found: {template_id}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Step 2: Determine model/provider
    template = prompt_store.get(template_id)
    model = request.model or (template.model if template else None) or "gpt-4o-mini"
    provider = request.provider or (template.provider if template else None) or "openai"

    # Step 3: Call the gateway
    payload: Dict[str, Any] = {
        "model": model,
        "provider": provider,
        "messages": [{"role": "user", "content": rendered}],
    }
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "http://localhost:8080/v1/chat/completions",
                json=payload,
            )
            resp.raise_for_status()
            gateway_response = resp.json()
    except httpx.HTTPError as exc:
        logger.error("prompt_test_gateway_error", error=str(exc))
        raise HTTPException(
            status_code=502,
            detail=f"Gateway call failed: {exc}",
        )

    return {
        "rendered_prompt": rendered,
        "model": model,
        "provider": provider,
        "response": gateway_response,
    }
