"""Abstract base class for LLM provider adapters.

Every concrete provider inherits from BaseProvider and implements
``chat_completion``.  The shared ``estimate_cost`` helper uses the
MODEL_PRICING table so cost calculation stays consistent across providers.
"""

from abc import ABC, abstractmethod

from src.models.telemetry import ChatCompletionRequest, ChatCompletionResponse


# ---------------------------------------------------------------------------
# Per-1M-token pricing  (input, output) in USD
# ---------------------------------------------------------------------------

MODEL_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o":                   (2.50,  10.00),
    "gpt-4o-mini":              (0.15,   0.60),
    "o1":                       (15.00, 60.00),
    "o1-mini":                  (3.00,  12.00),
    # Anthropic
    "claude-opus-4-6":          (15.00, 75.00),
    "claude-opus-4-20250918":   (15.00, 75.00),
    "claude-sonnet-4-6":        (3.00,  15.00),
    "claude-sonnet-4-20250514": (3.00,  15.00),
    "claude-haiku-4-5":         (0.80,   4.00),
    # Google Gemini
    "gemini-1.5-pro":           (1.25,   5.00),
    "gemini-1.5-flash":         (0.075,  0.30),
    "gemini-2.0-flash":         (0.10,   0.40),
    # Cohere
    "command-r-plus":           (2.50,  10.00),
    "command-r":                (0.15,   0.60),
}


class ProviderError(Exception):
    """Common error raised by all provider adapters.

    Provider-specific exceptions are caught internally and re-raised as
    ``ProviderError`` so callers only need to handle one type.
    """

    def __init__(self, provider: str, message: str, original: Exception | None = None):
        self.provider = provider
        self.original = original
        super().__init__(f"[{provider}] {message}")


class BaseProvider(ABC):
    """Base class for LLM provider adapters."""

    @abstractmethod
    async def chat_completion(
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse:
        """Execute a chat completion request and return a normalized response."""
        ...

    @staticmethod
    def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """Return estimated cost in USD using MODEL_PRICING.

        Unknown models return 0.0 rather than raising so telemetry can still
        be emitted even when pricing data is missing.
        """
        pricing = MODEL_PRICING.get(model)
        if pricing is None:
            return 0.0
        input_price, output_price = pricing
        return (prompt_tokens * input_price / 1_000_000) + (
            completion_tokens * output_price / 1_000_000
        )

    @staticmethod
    def estimate_cost_breakdown(
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> dict:
        """Return a per-component cost breakdown in USD.

        Applies the industry-standard prompt-cache pricing discounts:
          - cache_creation_input_tokens: billed at 1.25x input_price (Anthropic)
                                          — OpenAI charges full input rate, so the
                                            1.25x multiplier is a conservative default
          - cache_read_input_tokens:     billed at 0.10x input_price (90% discount)
          - prompt_tokens (uncached):    billed at input_price
          - completion_tokens:           billed at output_price (typically 3-5x input)

        Returns a dict with: input_cost_usd, output_cost_usd, cache_cost_usd, total_usd
        """
        pricing = MODEL_PRICING.get(model)
        if pricing is None:
            return {"input_cost_usd": 0.0, "output_cost_usd": 0.0, "cache_cost_usd": 0.0, "total_usd": 0.0}

        input_price, output_price = pricing
        # Uncached input tokens — prompt_tokens already EXCLUDES cached tokens in
        # OpenAI/Anthropic responses (they're in cache_read_input_tokens separately)
        input_cost = prompt_tokens * input_price / 1_000_000
        output_cost = completion_tokens * output_price / 1_000_000
        cache_write_cost = cache_creation_input_tokens * input_price * 1.25 / 1_000_000
        cache_read_cost = cache_read_input_tokens * input_price * 0.10 / 1_000_000
        cache_cost = cache_write_cost + cache_read_cost

        return {
            "input_cost_usd": round(input_cost, 8),
            "output_cost_usd": round(output_cost, 8),
            "cache_cost_usd": round(cache_cost, 8),
            "total_usd": round(input_cost + output_cost + cache_cost, 8),
        }
