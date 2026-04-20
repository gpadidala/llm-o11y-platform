"""Anthropic provider adapter.

Translates OpenAI-compatible chat completion requests into the Anthropic
Messages API format and normalizes the response back.
"""

from __future__ import annotations

import time
import uuid

import anthropic

from src.config import settings
from src.models.telemetry import (
    ChatChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)
from src.providers.base import BaseProvider, ProviderError


class AnthropicProvider(BaseProvider):
    """Adapter for the Anthropic Messages API."""

    def __init__(self) -> None:
        if not settings.anthropic_api_key:
            raise ProviderError("anthropic", "ANTHROPIC_API_KEY is not configured")
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _convert_messages(
        messages: list[ChatMessage],
    ) -> tuple[str | None, list[dict]]:
        """Separate the system prompt and convert the rest to Anthropic format.

        OpenAI allows ``role='system'`` inline.  Anthropic expects the system
        prompt as a dedicated top-level parameter.
        """
        system_prompt: str | None = None
        anthropic_messages: list[dict] = []

        for msg in messages:
            if msg.role == "system":
                # Concatenate multiple system messages if present
                if system_prompt is None:
                    system_prompt = msg.content
                else:
                    system_prompt += "\n" + msg.content
            else:
                anthropic_messages.append({
                    "role": msg.role,
                    "content": msg.content,
                })

        return system_prompt, anthropic_messages

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    async def chat_completion(
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse:
        request_id = str(uuid.uuid4())
        system_prompt, messages = self._convert_messages(request.messages)

        kwargs: dict = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens or 4096,
        }
        if system_prompt is not None:
            kwargs["system"] = system_prompt
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p

        try:
            response = await self._client.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise ProviderError("anthropic", f"Authentication failed: {exc}", exc) from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("anthropic", f"Rate limit exceeded: {exc}", exc) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("anthropic", f"Connection error: {exc}", exc) from exc
        except anthropic.APIError as exc:
            raise ProviderError("anthropic", f"API error: {exc}", exc) from exc
        except Exception as exc:
            raise ProviderError("anthropic", f"Unexpected error: {exc}", exc) from exc

        # Extract usage --------------------------------------------------
        prompt_tokens = response.usage.input_tokens
        completion_tokens = response.usage.output_tokens
        # Prompt cache tokens — Anthropic returns these directly on usage
        cache_creation_tokens = int(
            getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        )
        cache_read_tokens = int(
            getattr(response.usage, "cache_read_input_tokens", 0) or 0
        )
        total_tokens = prompt_tokens + completion_tokens + cache_creation_tokens + cache_read_tokens

        # Tiered cost breakdown (Layer 7) — Anthropic charges 1.25x for cache
        # writes and 0.10x for cache reads. prompt_tokens is already the
        # UNCACHED portion in Anthropic's API response.
        breakdown = self.estimate_cost_breakdown(
            request.model,
            prompt_tokens,
            completion_tokens,
            cache_creation_input_tokens=cache_creation_tokens,
            cache_read_input_tokens=cache_read_tokens,
        )

        # Assemble response text -----------------------------------------
        content_text = ""
        for block in response.content:
            if block.type == "text":
                content_text += block.text

        # Map Anthropic stop_reason to OpenAI finish_reason ---------------
        finish_reason_map = {
            "end_turn": "stop",
            "max_tokens": "length",
            "stop_sequence": "stop",
            "tool_use": "tool_calls",
        }
        finish_reason = finish_reason_map.get(response.stop_reason, response.stop_reason)

        return ChatCompletionResponse(
            id=request_id,
            created=int(time.time()),
            model=response.model,
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content=content_text),
                    finish_reason=finish_reason,
                ),
            ],
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cache_creation_input_tokens=cache_creation_tokens,
                cache_read_input_tokens=cache_read_tokens,
            ),
            provider="anthropic",
            cost_usd=breakdown["total_usd"],
        )
