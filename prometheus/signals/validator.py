"""Evidence validator.

Every factual claim a model makes must cite evidence keys that actually exist in
the quant packet *and* that actually carry a value. A claim resting on a feature
that could not be computed is unsupported, and an unsupported claim kills the
signal. Nothing is repaired: a rejected draft is recorded as rejected, never
patched into something sellable.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..quant.features import QuantPacket
from .schema import Direction, ModelSignal

VALIDATOR_VERSION = "evidence-validator-1.0.0"


class RejectionCode(str, enum.Enum):
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"
    UNAVAILABLE_EVIDENCE = "UNAVAILABLE_EVIDENCE"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    UNSUPPORTED_ASSET = "UNSUPPORTED_ASSET"
    UNSUPPORTED_HORIZON = "UNSUPPORTED_HORIZON"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    CORROBORATION_FAILED = "CORROBORATION_FAILED"
    INCOHERENT_INVALIDATION = "INCOHERENT_INVALIDATION"
    PROVIDER_ERROR = "PROVIDER_ERROR"


@dataclass
class ValidationResult:
    ok: bool
    code: RejectionCode | None = None
    detail: str = ""
    cited_keys: list[str] = field(default_factory=list)
    unsupported_keys: list[str] = field(default_factory=list)
    unavailable_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "validator_version": VALIDATOR_VERSION,
            "code": None if self.code is None else self.code.value,
            "detail": self.detail,
            "cited_keys": sorted(self.cited_keys),
            "unsupported_keys": sorted(self.unsupported_keys),
            "unavailable_keys": sorted(self.unavailable_keys),
        }


def validate_evidence(
    signal: ModelSignal,
    packet: QuantPacket,
    *,
    entry_reference: Decimal,
) -> ValidationResult:
    """Validate every claim in ``signal`` against ``packet``."""
    cited = sorted(signal.evidence_keys())

    known = packet.all_keys()
    computed = packet.available_keys()

    # A key the quant engine has never heard of. This is the fabrication case.
    unsupported = sorted(k for k in cited if k not in known)
    if unsupported:
        return ValidationResult(
            ok=False,
            code=RejectionCode.UNSUPPORTED_CLAIM,
            detail=(
                "model cited evidence keys that do not exist in the quant packet: "
                + ", ".join(unsupported)
            ),
            cited_keys=cited,
            unsupported_keys=unsupported,
        )

    # A key that exists as a concept but could not be computed from this snapshot.
    unavailable = sorted(k for k in cited if k not in computed)
    if unavailable:
        return ValidationResult(
            ok=False,
            code=RejectionCode.UNAVAILABLE_EVIDENCE,
            detail=(
                "model cited evidence that is UNAVAILABLE in this snapshot: "
                + ", ".join(unavailable)
            ),
            cited_keys=cited,
            unavailable_keys=unavailable,
        )

    # Each individual claim must stand on its own citations.
    for idx, claim in enumerate(signal.claims):
        if not claim.evidence_keys:
            return ValidationResult(
                ok=False,
                code=RejectionCode.UNSUPPORTED_CLAIM,
                detail=f"claim {idx} cites no evidence",
                cited_keys=cited,
            )

    # The invalidation must be a real, reachable price on the correct side of the
    # entry. A LONG invalidated above entry is not a stop, it is nonsense.
    inv = signal.invalidation.reference_price
    if signal.direction == Direction.LONG and inv >= entry_reference:
        return ValidationResult(
            ok=False,
            code=RejectionCode.INCOHERENT_INVALIDATION,
            detail=f"LONG invalidation {inv} must sit below entry_reference {entry_reference}",
            cited_keys=cited,
        )
    if signal.direction == Direction.SHORT and inv <= entry_reference:
        return ValidationResult(
            ok=False,
            code=RejectionCode.INCOHERENT_INVALIDATION,
            detail=f"SHORT invalidation {inv} must sit above entry_reference {entry_reference}",
            cited_keys=cited,
        )
    # Guard against an invalidation so far away it is decorative, or so close it
    # is guaranteed to trigger on noise.
    if entry_reference > 0:
        distance_bps = abs(inv - entry_reference) / entry_reference * Decimal(10_000)
        if signal.direction in (Direction.LONG, Direction.SHORT):
            if distance_bps < Decimal("1"):
                return ValidationResult(
                    ok=False,
                    code=RejectionCode.INCOHERENT_INVALIDATION,
                    detail=f"invalidation is only {distance_bps:.2f} bps from entry",
                    cited_keys=cited,
                )
            if distance_bps > Decimal("2000"):
                return ValidationResult(
                    ok=False,
                    code=RejectionCode.INCOHERENT_INVALIDATION,
                    detail=f"invalidation is {distance_bps:.2f} bps from entry, beyond the 2000 bps limit",
                    cited_keys=cited,
                )

    return ValidationResult(ok=True, cited_keys=cited)
