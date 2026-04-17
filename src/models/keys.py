"""Pydantic models for API responses related to virtual API keys.

Virtual keys provide a layer of abstraction over provider API keys,
enabling per-user rate limiting, cost tracking, and key rotation
without exposing raw provider credentials.
"""

import time
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class KeyStatus(str, Enum):
    """Status of a virtual API key."""

    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    RATE_LIMITED = "rate_limited"


class KeyScope(str, Enum):
    """Permission scope for a virtual key."""

    FULL = "full"  # All endpoints
    CHAT_ONLY = "chat_only"  # Only chat completions
    READ_ONLY = "read_only"  # Only read endpoints (models, health)
    EVAL_ONLY = "eval_only"  # Only evaluation endpoints


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CreateKeyRequest(BaseModel):
    """Request to create a new virtual API key."""

    name: str
    description: str = ""
    owner: str = "system"
    scopes: list[KeyScope] = [KeyScope.FULL]
    allowed_models: Optional[list[str]] = None  # None = all models allowed
    allowed_providers: Optional[list[str]] = None  # None = all providers
    rate_limit_rpm: Optional[int] = None  # Requests per minute
    rate_limit_tpm: Optional[int] = None  # Tokens per minute
    budget_usd: Optional[float] = None  # Max spend in USD
    expires_at: Optional[float] = None  # Unix timestamp


class UpdateKeyRequest(BaseModel):
    """Request to update a virtual API key."""

    name: Optional[str] = None
    description: Optional[str] = None
    scopes: Optional[list[KeyScope]] = None
    allowed_models: Optional[list[str]] = None
    allowed_providers: Optional[list[str]] = None
    rate_limit_rpm: Optional[int] = None
    rate_limit_tpm: Optional[int] = None
    budget_usd: Optional[float] = None
    expires_at: Optional[float] = None
    status: Optional[KeyStatus] = None


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class VirtualKeyResponse(BaseModel):
    """Response model for a virtual API key."""

    key_id: str
    key_prefix: str  # First 8 chars of the key for display (e.g., "vk-a1b2...")
    name: str
    description: str = ""
    owner: str
    status: KeyStatus = KeyStatus.ACTIVE
    scopes: list[KeyScope] = [KeyScope.FULL]
    allowed_models: Optional[list[str]] = None
    allowed_providers: Optional[list[str]] = None
    rate_limit_rpm: Optional[int] = None
    rate_limit_tpm: Optional[int] = None
    budget_usd: Optional[float] = None
    spent_usd: float = 0.0
    request_count: int = 0
    token_count: int = 0
    last_used_at: Optional[float] = None
    created_at: float = Field(default_factory=time.time)
    expires_at: Optional[float] = None


class KeyCreatedResponse(BaseModel):
    """Response when a new key is created (includes the full key once)."""

    key_id: str
    key: str  # Full key -- shown only at creation time
    key_prefix: str
    name: str
    status: str = "created"
    message: str = "Store this key securely. It will not be shown again."


class KeyListResponse(BaseModel):
    """Response for listing virtual keys."""

    count: int
    keys: list[VirtualKeyResponse]


class KeyUsageResponse(BaseModel):
    """Usage statistics for a virtual key."""

    key_id: str
    key_prefix: str
    name: str
    request_count: int
    token_count: int
    spent_usd: float
    budget_usd: Optional[float] = None
    budget_remaining_usd: Optional[float] = None
    rate_limit_rpm: Optional[int] = None
    current_rpm: int = 0
    rate_limit_tpm: Optional[int] = None
    current_tpm: int = 0
    last_used_at: Optional[float] = None
    usage_by_model: dict[str, int] = {}  # model -> request count
    usage_by_provider: dict[str, int] = {}  # provider -> request count


class KeyValidationResponse(BaseModel):
    """Response for key validation check."""

    valid: bool
    key_id: Optional[str] = None
    status: Optional[KeyStatus] = None
    reason: str = ""  # Why validation failed, if applicable
    scopes: list[KeyScope] = []
