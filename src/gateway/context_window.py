"""Pre-flight context window validation (Layer 3 of the LLM API stack).

Rejects requests that will exceed the model's context window BEFORE we forward
them to the upstream provider.  This saves cost (no provider round-trip for a
doomed request) and latency, and gives users a clear local error.

The token count is estimated — we don't pull in ``tiktoken`` to keep the
container small.  A 4-chars-per-token heuristic matches most BPE tokenizers
within ~10%, which is enough for a boundary check.
"""

from __future__ import annotations

from typing import List, Optional

from src.models.telemetry import ChatMessage


# ---------------------------------------------------------------------------
# Model context windows (total = input + output)
# ---------------------------------------------------------------------------
# Conservative upper bounds for each supported model.  Unknown models default
# to a safe 8K limit so we always have *some* guardrail.
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "o1": 200_000,
    "o1-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-3.5-turbo": 16_385,
    # Anthropic
    "claude-opus-4-6": 200_000,
    "claude-opus-4-20250918": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-sonnet-4-20250514": 200_000,
    "claude-haiku-4-5": 200_000,
    # Google Gemini
    "gemini-1.5-pro": 2_000_000,
    "gemini-1.5-flash": 1_000_000,
    "gemini-2.0-flash": 1_000_000,
    # Cohere
    "command-r-plus": 128_000,
    "command-r": 128_000,
}

_DEFAULT_CONTEXT_WINDOW = 8_192
# Reserve tokens for the model's own response tokens if max_tokens not set
_DEFAULT_RESERVED_OUTPUT = 1_024
# Safety margin — we add a small buffer because our char-heuristic underestimates
# by ~5-10% on non-English text and JSON-heavy prompts
_SAFETY_MARGIN_FRAC = 0.10


class ContextWindowExceededError(ValueError):
    """Raised when a request will not fit in the target model's context window."""

    def __init__(self, model: str, estimated_tokens: int, max_tokens: int, limit: int):
        self.model = model
        self.estimated_tokens = estimated_tokens
        self.max_tokens = max_tokens
        self.limit = limit
        super().__init__(
            f"Request exceeds context window for {model!r}: "
            f"~{estimated_tokens} input + {max_tokens} output = "
            f"{estimated_tokens + max_tokens} tokens, but model supports only {limit}."
        )


def estimate_tokens(text: str) -> int:
    """Estimate the token count for *text* using a 4-chars-per-token heuristic.

    This is deliberately simple — accurate enough for a boundary check, fast
    enough to run on every request without adding a tokenizer dependency.
    """
    if not text:
        return 0
    # 4 chars/token is the rule-of-thumb for English BPE.  Round up so we never
    # UNDERestimate (false negatives are worse than false positives here).
    return (len(text) + 3) // 4


def estimate_messages_tokens(messages: List[ChatMessage]) -> int:
    """Estimate total input tokens for a list of chat messages.

    Adds a small per-message overhead for role tags / formatting — OpenAI's
    cookbook uses 3-4 tokens per message for this.
    """
    total = 0
    for msg in messages:
        total += estimate_tokens(msg.content) + 4  # 4 tokens for role + separators
    total += 2  # priming for assistant reply
    return total


def get_context_window(model: str) -> int:
    """Return the context window (in tokens) for *model*, or the default."""
    return MODEL_CONTEXT_WINDOWS.get(model, _DEFAULT_CONTEXT_WINDOW)


def validate_context_window(
    model: str,
    messages: List[ChatMessage],
    max_tokens: Optional[int] = None,
) -> dict:
    """Pre-flight check that the request will fit in the target model's window.

    Raises:
        ContextWindowExceededError: if the estimate plus max_tokens exceeds the
        model's context window (after a small safety margin).

    Returns a dict with ``{estimated_input_tokens, max_tokens, limit,
    remaining_tokens}`` — useful for telemetry / response headers.
    """
    limit = get_context_window(model)
    estimated_input = estimate_messages_tokens(messages)
    reserved_output = max_tokens if max_tokens is not None else _DEFAULT_RESERVED_OUTPUT

    # Apply safety margin — a buffer so our heuristic doesn't miss edge cases
    budget = int(limit * (1.0 - _SAFETY_MARGIN_FRAC))

    total = estimated_input + reserved_output
    if total > budget:
        raise ContextWindowExceededError(
            model=model,
            estimated_tokens=estimated_input,
            max_tokens=reserved_output,
            limit=limit,
        )

    return {
        "estimated_input_tokens": estimated_input,
        "max_tokens": reserved_output,
        "limit": limit,
        "safety_budget": budget,
        "remaining_tokens": budget - total,
    }
