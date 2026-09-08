"""Provider selection.

The configured provider is the provider. There is no silent fallback: if
``PROM_MODEL_PROVIDER=anthropic`` and the key is missing, generation fails loudly
rather than quietly producing heuristic output under an Anthropic label.
"""

from __future__ import annotations

from typing import Any

from ..config import Config, get_config
from .base import ModelProvider, ProviderError
from .heuristic import HeuristicProvider
from .llm import AnthropicProvider, OllamaProvider


def build_provider(cfg: Config | None = None) -> ModelProvider:
    cfg = cfg or get_config()
    kind = cfg.model_provider.strip().lower()
    if kind == "heuristic":
        return HeuristicProvider()
    if kind == "anthropic":
        return AnthropicProvider(cfg.anthropic_api_key, cfg.model_name, cfg.anthropic_base_url)
    if kind == "ollama":
        return OllamaProvider(cfg.ollama_host, cfg.model_name)
    raise ProviderError(
        f"unknown PROM_MODEL_PROVIDER {cfg.model_provider!r}; expected heuristic, anthropic or ollama"
    )


def all_provider_status(cfg: Config | None = None) -> list[dict[str, Any]]:
    """Status of every adapter, so the dashboard can show what is and is not live."""
    cfg = cfg or get_config()
    active = cfg.model_provider.strip().lower()
    out = []
    for provider in (
        HeuristicProvider(),
        AnthropicProvider(cfg.anthropic_api_key, cfg.model_name, cfg.anthropic_base_url),
        OllamaProvider(cfg.ollama_host, cfg.model_name),
    ):
        info = provider.status()
        info["active"] = provider.name == active
        out.append(info)
    return out
