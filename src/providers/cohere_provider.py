"""Cohere provider adapter.

Translates OpenAI-compatible chat completion requests to the Cohere Chat API
and normalizes the response.
"""

from __future__ import annotations

import time
import uuid

import cohere

from src.config import settings
from src.models.telemetry import (
    ChatChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)
from src.providers.base import BaseProvider, ProviderError


class CohereProvider(BaseProvider):
    """Adapter for the Cohere Chat API."""

    def __init__(self) -> None:
        if not settings.cohere_api_key:
            raise ProviderError("cohere", "COHERE_API_KEY is not configured")
        self._client = cohere.AsyncClientV2(api_key=settings.cohere_api_key)

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _convert_messages(
        messages: list[ChatMessage],
    ) -> list[dict]:
        """Convert OpenAI messages to Cohere V2 chat format.

        Cohere V2 accepts a list of messages with roles: system, user,
        assistant -- same as OpenAI format.
        """
        cohere_messages: list[dict] = []
        for msg in messages:
            cohere_messages.append({
                "role": msg.role,
                "content": msg.content,
            })
        return cohere_messages

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    async def chat_completion(
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse:
        request_id = str(uuid.uuid4())
        cohere_messages = self._convert_messages(request.messages)

        kwargs: dict = {
            "model": request.model,
            "messages": cohere_messages,
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.top_p is not None:
            kwargs["p"] = request.top_p

        try:
            response = await self._client.chat(**kwargs)
        except cohere.errors.BadRequestError as exc:
            raise ProviderError("cohere", f"Bad request: {exc}", exc) from exc
        except cohere.errors.UnauthorizedError as exc:
            raise ProviderError("cohere", f"Authentication failed: {exc}", exc) from exc
        except cohere.errors.TooManyRequestsError as exc:
            raise ProviderError("cohere", f"Rate limit exceeded: {exc}", exc) from exc
        except Exception as exc:
            raise ProviderError("cohere", f"Unexpected error: {exc}", exc) from exc

        # Extract text from response --------------------------------------
        content_text = ""
        if response.message and response.message.content:
            for block in response.message.content:
                if hasattr(block, "text"):
                    content_text += block.text

        # Extract usage ---------------------------------------------------
        prompt_tokens = 0
        completion_tokens = 0
        if response.usage and response.usage.tokens:
            prompt_tokens = getattr(response.usage.tokens, "input_tokens", 0) or 0
            completion_tokens = getattr(response.usage.tokens, "output_tokens", 0) or 0
        total_tokens = prompt_tokens + completion_tokens

        cost = self.estimate_cost(request.model, prompt_tokens, completion_tokens)

        # Map finish reason -----------------------------------------------
        finish_reason_raw = getattr(response, "finish_reason", "COMPLETE")
        finish_reason_map = {
            "COMPLETE": "stop",
            "MAX_TOKENS": "length",
            "ERROR": "stop",
            "ERROR_TOXIC": "content_filter",
        }
        finish_reason = finish_reason_map.get(str(finish_reason_raw), "stop")

        return ChatCompletionResponse(
            id=request_id,
            created=int(time.time()),
            model=request.model,
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
            ),
            provider="cohere",
            cost_usd=cost,
        )
