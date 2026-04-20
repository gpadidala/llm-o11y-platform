"""OpenAI provider adapter.

Uses the official ``openai`` Python SDK to execute chat completion requests
and returns a normalized ``ChatCompletionResponse``.
"""

from __future__ import annotations

import uuid

from openai import AsyncOpenAI, APIError, APIConnectionError, RateLimitError

from src.config import settings
from src.models.telemetry import (
    ChatChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)
from src.providers.base import BaseProvider, ProviderError


class OpenAIProvider(BaseProvider):
    """Adapter for the OpenAI Chat Completions API."""

    def __init__(self) -> None:
        if not settings.openai_api_key:
            raise ProviderError("openai", "OPENAI_API_KEY is not configured")
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def chat_completion(
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse:
        request_id = str(uuid.uuid4())

        kwargs: dict = {
            "model": request.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except RateLimitError as exc:
            raise ProviderError("openai", f"Rate limit exceeded: {exc}", exc) from exc
        except APIConnectionError as exc:
            raise ProviderError("openai", f"Connection error: {exc}", exc) from exc
        except APIError as exc:
            raise ProviderError("openai", f"API error: {exc}", exc) from exc
        except Exception as exc:
            raise ProviderError("openai", f"Unexpected error: {exc}", exc) from exc

        # Extract usage --------------------------------------------------
        usage_data = response.usage
        prompt_tokens = usage_data.prompt_tokens if usage_data else 0
        completion_tokens = usage_data.completion_tokens if usage_data else 0
        total_tokens = usage_data.total_tokens if usage_data else 0

        # Prompt cache tokens — OpenAI returns these under prompt_tokens_details
        cache_read_tokens = 0
        cache_creation_tokens = 0
        if usage_data and hasattr(usage_data, "prompt_tokens_details"):
            details = getattr(usage_data, "prompt_tokens_details", None) or {}
            if hasattr(details, "cached_tokens"):
                cache_read_tokens = int(getattr(details, "cached_tokens", 0) or 0)
            elif isinstance(details, dict):
                cache_read_tokens = int(details.get("cached_tokens", 0) or 0)

        # Tiered cost breakdown (Layer 7)
        breakdown = self.estimate_cost_breakdown(
            request.model,
            prompt_tokens - cache_read_tokens,  # uncached portion only
            completion_tokens,
            cache_creation_input_tokens=cache_creation_tokens,
            cache_read_input_tokens=cache_read_tokens,
        )

        # Build choices ---------------------------------------------------
        choices = [
            ChatChoice(
                index=c.index,
                message=ChatMessage(
                    role=c.message.role,
                    content=c.message.content or "",
                ),
                finish_reason=c.finish_reason,
            )
            for c in response.choices
        ]

        return ChatCompletionResponse(
            id=request_id,
            created=response.created,
            model=response.model,
            choices=choices,
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cache_creation_input_tokens=cache_creation_tokens,
                cache_read_input_tokens=cache_read_tokens,
            ),
            provider="openai",
            cost_usd=breakdown["total_usd"],
        )
