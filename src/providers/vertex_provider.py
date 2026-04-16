"""Google Vertex AI provider adapter.

Translates OpenAI-compatible chat completion requests to Vertex AI's
GenerativeModel API and normalizes the response.
"""

from __future__ import annotations

import time
import uuid

import vertexai
from vertexai.generative_models import (
    Content,
    GenerationConfig,
    GenerativeModel,
    Part,
)

from src.config import settings
from src.models.telemetry import (
    ChatChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)
from src.providers.base import BaseProvider, ProviderError


class VertexAIProvider(BaseProvider):
    """Adapter for Google Vertex AI generative models."""

    def __init__(self) -> None:
        if not settings.vertex_project_id:
            raise ProviderError("vertex_ai", "VERTEX_PROJECT_ID is not configured")

        vertexai.init(
            project=settings.vertex_project_id,
            location=settings.vertex_location,
        )

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _convert_messages(
        messages: list[ChatMessage],
    ) -> tuple[str | None, list[Content]]:
        """Convert OpenAI-format messages to Vertex AI Content objects.

        * ``system`` messages are extracted and returned separately for use
          as ``system_instruction``.
        * ``assistant`` is mapped to the Vertex ``model`` role.
        """
        system_parts: list[str] = []
        contents: list[Content] = []

        role_map = {
            "user": "user",
            "assistant": "model",
        }

        for msg in messages:
            if msg.role == "system":
                system_parts.append(msg.content)
                continue
            vertex_role = role_map.get(msg.role, "user")
            contents.append(
                Content(role=vertex_role, parts=[Part.from_text(msg.content)])
            )

        system_instruction = "\n".join(system_parts) if system_parts else None
        return system_instruction, contents

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    async def chat_completion(
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse:
        request_id = str(uuid.uuid4())
        system_instruction, contents = self._convert_messages(request.messages)

        # Build generation config -----------------------------------------
        gen_config_kwargs: dict = {}
        if request.temperature is not None:
            gen_config_kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            gen_config_kwargs["max_output_tokens"] = request.max_tokens
        if request.top_p is not None:
            gen_config_kwargs["top_p"] = request.top_p
        generation_config = GenerationConfig(**gen_config_kwargs) if gen_config_kwargs else None

        # Initialise model ------------------------------------------------
        model_kwargs: dict = {"model_name": request.model}
        if system_instruction is not None:
            model_kwargs["system_instruction"] = system_instruction
        if generation_config is not None:
            model_kwargs["generation_config"] = generation_config

        try:
            model = GenerativeModel(**model_kwargs)
            response = await model.generate_content_async(contents)
        except Exception as exc:
            raise ProviderError(
                "vertex_ai", f"Vertex AI request failed: {exc}", exc
            ) from exc

        # Extract text ----------------------------------------------------
        content_text = response.text if response.text else ""

        # Extract usage ---------------------------------------------------
        prompt_tokens = 0
        completion_tokens = 0
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            prompt_tokens = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
            completion_tokens = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
        total_tokens = prompt_tokens + completion_tokens

        cost = self.estimate_cost(request.model, prompt_tokens, completion_tokens)

        # Map finish reason -----------------------------------------------
        finish_reason = "stop"
        if response.candidates:
            candidate = response.candidates[0]
            fr = getattr(candidate, "finish_reason", None)
            if fr is not None:
                vertex_reason_map = {
                    1: "stop",       # STOP
                    2: "length",     # MAX_TOKENS
                    3: "stop",       # SAFETY
                    4: "stop",       # RECITATION
                    5: "stop",       # OTHER
                }
                finish_reason = vertex_reason_map.get(int(fr), "stop")

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
            provider="vertex_ai",
            cost_usd=cost,
        )
