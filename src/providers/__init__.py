"""Provider registry -- maps Provider enum values to adapter instances."""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from src.models.telemetry import Provider
from src.providers.base import BaseProvider, ProviderError

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Lazy registry
# ---------------------------------------------------------------------------
# Concrete adapters are imported on first use to avoid pulling in heavy SDKs
# (openai, anthropic, google-cloud-aiplatform, ...) at import time.

_REGISTRY: dict[Provider, type[BaseProvider]] = {}


def _ensure_registry() -> None:
    """Populate ``_REGISTRY`` with concrete adapter classes on first call.

    Adapters are imported lazily so the gateway can start even if an
    optional provider SDK is not installed -- the import error surfaces
    only when the provider is actually requested.
    """
    if _REGISTRY:
        return

    # Each entry maps a Provider enum member to a *module path* and
    # *class name*.  Add new providers here as they are implemented.
    _ADAPTER_MAP: dict[Provider, tuple[str, str]] = {
        Provider.OPENAI: ("src.providers.openai_provider", "OpenAIProvider"),
        Provider.AZURE_OPENAI: ("src.providers.openai_provider", "OpenAIProvider"),
        Provider.ANTHROPIC: ("src.providers.anthropic_provider", "AnthropicProvider"),
        Provider.VERTEX_AI: ("src.providers.vertex_provider", "VertexAIProvider"),
        Provider.BEDROCK: ("src.providers.bedrock_provider", "BedrockProvider"),
        Provider.COHERE: ("src.providers.cohere_provider", "CohereProvider"),
    }

    import importlib

    for provider, (module_path, class_name) in _ADAPTER_MAP.items():
        try:
            module = importlib.import_module(module_path)
            adapter_cls = getattr(module, class_name)
            _REGISTRY[provider] = adapter_cls
        except (ImportError, AttributeError):
            # Provider SDK not installed or adapter not yet implemented --
            # this is fine; the error is raised later in get_provider().
            pass


@lru_cache(maxsize=16)
def _instantiate(provider: Provider) -> BaseProvider:
    """Create (and cache) a singleton adapter instance for the given provider."""
    _ensure_registry()

    adapter_cls = _REGISTRY.get(provider)
    if adapter_cls is None:
        raise ProviderError(
            provider=provider.value,
            message=(
                f"Provider '{provider.value}' is not available. "
                "Either the adapter is not implemented or the required SDK "
                "is not installed."
            ),
        )
    return adapter_cls()


def get_provider(provider: Provider) -> BaseProvider:
    """Return a cached provider adapter instance.

    Raises:
        ProviderError: If the requested provider is not registered or its
            SDK is not installed.
    """
    return _instantiate(provider)


__all__ = ["get_provider", "BaseProvider", "ProviderError"]
