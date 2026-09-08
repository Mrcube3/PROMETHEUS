"""Model provider interface.

A provider receives a read-only view of deterministic features and must return a
``ModelSignal``. It is given no ability to compute, price, settle or score.

Every call is recorded with the provider, model, prompt version and the hash of the
exact quant packet that was shown to it, so any published signal can be traced back
to the precise inputs that produced it.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any

from ..signals.schema import ModelSignal

PROMPT_VERSION = "prompt-1.0.0"


@dataclass
class ModelRequest:
    asset: str
    venue: str
    horizon: str
    horizon_seconds: int
    entry_reference: str
    features: dict[str, str | None]
    available_keys: list[str]
    quant_hash: str
    snapshot_hash: str


@dataclass
class ModelResponse:
    signal: ModelSignal
    provider: str
    model_version: str
    prompt_version: str
    raw: dict[str, Any]
    latency_ms: int


class ProviderError(RuntimeError):
    """The provider failed to produce a usable structured response."""


class ModelProvider(abc.ABC):
    name: str = "abstract"

    @abc.abstractmethod
    def model_version(self) -> str:
        ...

    @abc.abstractmethod
    def status(self) -> dict[str, Any]:
        """Provider health, using the project status vocabulary."""

    @abc.abstractmethod
    def generate(self, request: ModelRequest) -> ModelResponse:
        ...


def build_request(
    *,
    asset: str,
    venue: str,
    horizon: str,
    horizon_seconds: int,
    entry_reference: str,
    packet,
) -> ModelRequest:
    return ModelRequest(
        asset=asset,
        venue=venue,
        horizon=horizon,
        horizon_seconds=horizon_seconds,
        entry_reference=entry_reference,
        features=packet.summary(),
        available_keys=sorted(packet.available_keys()),
        quant_hash="",  # filled by the caller once the packet is hashed
        snapshot_hash=packet.snapshot_hash,
    )
