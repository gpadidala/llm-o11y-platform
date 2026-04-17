"""Pydantic models for Prompt Management API responses.

Defines the request/response schemas used by the prompt management
router for template CRUD, rendering, and version history.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Template response models
# ---------------------------------------------------------------------------


class PromptTemplateResponse(BaseModel):
    """Full prompt template response."""

    template_id: str
    name: str
    description: str = ""
    content: str
    model: Optional[str] = None
    provider: Optional[str] = None
    variables: List[str] = []
    tags: List[str] = []
    version: int
    created_at: float
    updated_at: float
    created_by: str
    variants: Dict[str, str] = {}
    active_variant: Optional[str] = None


class PromptTemplateSummary(BaseModel):
    """Abbreviated template info for list views."""

    template_id: str
    name: str
    description: str = ""
    model: Optional[str] = None
    provider: Optional[str] = None
    tags: List[str] = []
    version: int
    variable_count: int
    created_at: float
    updated_at: float


class PromptVersionResponse(BaseModel):
    """A single version entry in a template's history."""

    version: int
    content: str
    created_at: float
    created_by: str
    change_note: str = ""


# ---------------------------------------------------------------------------
# Operation response models
# ---------------------------------------------------------------------------


class PromptCreatedResponse(BaseModel):
    """Response after creating a prompt template."""

    status: str = "created"
    template: PromptTemplateResponse


class PromptUpdatedResponse(BaseModel):
    """Response after updating a prompt template."""

    status: str = "updated"
    template: PromptTemplateResponse


class PromptDeletedResponse(BaseModel):
    """Response after deleting a prompt template."""

    status: str = "deleted"
    template_id: str


class PromptListResponse(BaseModel):
    """Response for listing prompt templates."""

    count: int
    templates: List[PromptTemplateResponse]


class PromptVersionListResponse(BaseModel):
    """Response for listing template versions."""

    template_id: str
    count: int
    versions: List[PromptVersionResponse]


# ---------------------------------------------------------------------------
# Render response models
# ---------------------------------------------------------------------------


class PromptRenderResponse(BaseModel):
    """Response after rendering a template with variables."""

    rendered: str
    template_id: str


class PromptTestResponse(BaseModel):
    """Response after testing a template against a live model."""

    rendered_prompt: str
    model: str
    provider: str
    response: Dict[str, Any]
