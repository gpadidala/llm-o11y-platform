"""Data models for LLM and MCP telemetry records.

These Pydantic models define the request/response shapes for the gateway API
and the internal records used to emit OpenTelemetry signals.
"""

import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Provider(str, Enum):
    """Supported LLM provider backends."""

    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"
    ANTHROPIC = "anthropic"
    VERTEX_AI = "vertex_ai"
    BEDROCK = "bedrock"
    COHERE = "cohere"


# ---------------------------------------------------------------------------
# Chat completion request / response
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    """Unified chat completion request accepted by the gateway."""

    model: str
    messages: List[ChatMessage]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    stream: Optional[bool] = False
    # Layer-6 passthroughs (OpenAI-compatible)
    stop: Optional[List[str]] = None  # stop sequences
    logprobs: Optional[bool] = None  # return log-probs
    top_logprobs: Optional[int] = None  # top-N logprobs when logprobs=True
    seed: Optional[int] = None  # deterministic sampling (OpenAI/Anthropic)
    response_format: Optional[Dict[str, str]] = None  # {"type": "json_object"}

    # Gateway extensions
    provider: Provider = Provider.OPENAI
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    tags: Optional[Dict[str, str]] = None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # Prompt cache token breakdown (OpenAI + Anthropic feature — billed at discount)
    # - cache_creation_input_tokens: tokens written to cache (25% markup on Anthropic)
    # - cache_read_input_tokens:     tokens served from cache (~10% of base cost)
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class ChatChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: Optional[str] = "stop"


class ChatCompletionResponse(BaseModel):
    """Unified chat completion response returned by the gateway."""

    id: str
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatChoice]
    usage: Usage

    # Gateway extensions
    provider: Optional[str] = None
    cost_usd: Optional[float] = None


# ---------------------------------------------------------------------------
# Internal telemetry records
# ---------------------------------------------------------------------------


class LLMRequestRecord(BaseModel):
    """Internal record for telemetry emission after every LLM call."""

    request_id: str
    provider: Provider
    model: str
    messages: List[ChatMessage]
    response_model: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    # Cost breakdown by token tier (Layer 7)
    input_cost_usd: float = 0.0
    output_cost_usd: float = 0.0
    cache_cost_usd: float = 0.0
    # Prompt caching (OpenAI/Anthropic)
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    latency_ms: float = 0.0
    ttft_ms: Optional[float] = None  # Time to first token (streaming)
    status: str = "success"
    error: Optional[str] = None
    # finish_reason from provider: stop / length / content_filter / tool_calls / function_call
    finish_reason: Optional[str] = None
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    tags: Optional[Dict[str, str]] = None
    # Virtual key attribution for audit + per-key metrics
    key_id: Optional[str] = None
    team: Optional[str] = None
    ip_address: Optional[str] = None


# ---------------------------------------------------------------------------
# MCP (Model Context Protocol) telemetry records
# ---------------------------------------------------------------------------


class MCPToolCallRecord(BaseModel):
    """Record for MCP tool call telemetry."""

    server_name: str
    tool_name: str
    input_params: Optional[Dict[str, Any]] = None
    output_data: Optional[Dict[str, Any]] = None
    latency_ms: float = 0.0
    status: str = "success"
    error: Optional[str] = None
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    attributed_input_tokens: int = 0
    attributed_output_tokens: int = 0
    attributed_cost_usd: float = 0.0


class MCPSessionStart(BaseModel):
    session_id: str
    agent_name: Optional[str] = None
    user_id: Optional[str] = None


class MCPSessionEnd(BaseModel):
    session_id: str
