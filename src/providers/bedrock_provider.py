"""AWS Bedrock provider adapter.

Uses the Bedrock Runtime ``Converse`` API via ``boto3`` to execute chat
completion requests and returns a normalized ``ChatCompletionResponse``.
"""

from __future__ import annotations

import time
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from src.config import settings
from src.models.telemetry import (
    ChatChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)
from src.providers.base import BaseProvider, ProviderError


# ---------------------------------------------------------------------------
# Friendly model name -> Bedrock model ID mapping
# ---------------------------------------------------------------------------

BEDROCK_MODEL_MAP: dict[str, str] = {
    "claude-opus-4-6":       "anthropic.claude-opus-4-6-20250918-v1:0",
    "claude-sonnet-4-6":     "anthropic.claude-sonnet-4-6-20250514-v1:0",
    "claude-haiku-4-5":      "anthropic.claude-haiku-4-5-20250401-v1:0",
    "claude-3-opus":         "anthropic.claude-3-opus-20240229-v1:0",
    "claude-3-sonnet":       "anthropic.claude-3-sonnet-20240229-v1:0",
    "claude-3-haiku":        "anthropic.claude-3-haiku-20240307-v1:0",
    "command-r-plus":        "cohere.command-r-plus-v1:0",
    "command-r":             "cohere.command-r-v1:0",
}


class BedrockProvider(BaseProvider):
    """Adapter for AWS Bedrock using the Converse API."""

    def __init__(self) -> None:
        session_kwargs: dict = {}
        if settings.aws_access_key_id:
            session_kwargs["aws_access_key_id"] = settings.aws_access_key_id
        if settings.aws_secret_access_key:
            session_kwargs["aws_secret_access_key"] = settings.aws_secret_access_key

        session = boto3.Session(**session_kwargs)
        self._client = session.client(
            "bedrock-runtime",
            region_name=settings.aws_region,
        )

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _resolve_model_id(model: str) -> str:
        """Map a friendly model name to the full Bedrock model ID."""
        return BEDROCK_MODEL_MAP.get(model, model)

    @staticmethod
    def _convert_messages(
        messages: list[ChatMessage],
    ) -> tuple[list[dict] | None, list[dict]]:
        """Convert OpenAI messages to Bedrock Converse format.

        System messages become a separate ``system`` parameter.  User and
        assistant messages are mapped directly.
        """
        system_prompts: list[dict] = []
        bedrock_messages: list[dict] = []

        for msg in messages:
            if msg.role == "system":
                system_prompts.append({"text": msg.content})
            else:
                bedrock_messages.append({
                    "role": msg.role,
                    "content": [{"text": msg.content}],
                })

        return (system_prompts if system_prompts else None), bedrock_messages

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    async def chat_completion(
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse:
        request_id = str(uuid.uuid4())
        model_id = self._resolve_model_id(request.model)
        system_prompts, messages = self._convert_messages(request.messages)

        # Build inference config ------------------------------------------
        inference_config: dict = {}
        if request.temperature is not None:
            inference_config["temperature"] = request.temperature
        if request.max_tokens is not None:
            inference_config["maxTokens"] = request.max_tokens
        if request.top_p is not None:
            inference_config["topP"] = request.top_p

        converse_kwargs: dict = {
            "modelId": model_id,
            "messages": messages,
        }
        if system_prompts is not None:
            converse_kwargs["system"] = system_prompts
        if inference_config:
            converse_kwargs["inferenceConfig"] = inference_config

        try:
            # boto3 is synchronous; we call it directly (works fine in
            # asyncio when wrapped with run_in_executor if needed, but
            # for simplicity and compatibility we call synchronously here).
            response = self._client.converse(**converse_kwargs)
        except ClientError as exc:
            raise ProviderError(
                "bedrock", f"AWS API error: {exc}", exc
            ) from exc
        except BotoCoreError as exc:
            raise ProviderError(
                "bedrock", f"Boto core error: {exc}", exc
            ) from exc
        except Exception as exc:
            raise ProviderError(
                "bedrock", f"Unexpected error: {exc}", exc
            ) from exc

        # Extract content -------------------------------------------------
        output = response.get("output", {})
        message_body = output.get("message", {})
        content_blocks = message_body.get("content", [])
        content_text = ""
        for block in content_blocks:
            if "text" in block:
                content_text += block["text"]

        # Extract usage ---------------------------------------------------
        usage_data = response.get("usage", {})
        prompt_tokens = usage_data.get("inputTokens", 0)
        completion_tokens = usage_data.get("outputTokens", 0)
        total_tokens = usage_data.get("totalTokens", prompt_tokens + completion_tokens)

        cost = self.estimate_cost(request.model, prompt_tokens, completion_tokens)

        # Map stop reason -------------------------------------------------
        stop_reason = response.get("stopReason", "end_turn")
        finish_reason_map = {
            "end_turn": "stop",
            "max_tokens": "length",
            "stop_sequence": "stop",
            "content_filtered": "content_filter",
        }
        finish_reason = finish_reason_map.get(stop_reason, "stop")

        return ChatCompletionResponse(
            id=request_id,
            created=int(time.time()),
            model=model_id,
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
            provider="bedrock",
            cost_usd=cost,
        )
