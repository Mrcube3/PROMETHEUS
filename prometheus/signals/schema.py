"""Strict signal schemas.

A model response that does not satisfy ``ModelSignal`` never becomes a product.
Validation is deliberately unforgiving: there is no coercion of a bad confidence
into a good one and no repair of a missing invalidation.
"""

from __future__ import annotations

import enum
import re
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "signal-schema-1.0.0"

# Horizon label -> seconds. Short labels exist so a full lifecycle can be observed;
# the longer ones are the normal operating configuration.
HORIZONS: dict[str, int] = {
    "10M": 600,
    "15M": 900,
    "30M": 1_800,
    "1H": 3_600,
    "4H": 14_400,
    "24H": 86_400,
}

_SIGNAL_ID = re.compile(r"^sig_[0-9a-f]{24}$")


class Direction(str, enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"
    STAND_DOWN = "STAND_DOWN"


class SignalState(str, enum.Enum):
    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    REJECTED = "REJECTED"
    FROZEN = "FROZEN"
    LISTED = "LISTED"
    PURCHASED = "PURCHASED"
    DELIVERED = "DELIVERED"
    AWAITING_OUTCOME = "AWAITING_OUTCOME"
    MATURED = "MATURED"
    SCORED = "SCORED"
    VERIFIED = "VERIFIED"
    EXPIRED = "EXPIRED"
    UNRESOLVED = "UNRESOLVED"


# Explicit transition table. Anything absent is illegal.
ALLOWED_TRANSITIONS: dict[SignalState, set[SignalState]] = {
    SignalState.DRAFT: {SignalState.VALIDATING, SignalState.REJECTED},
    SignalState.VALIDATING: {SignalState.FROZEN, SignalState.REJECTED},
    SignalState.REJECTED: set(),
    # A signal may mature without ever being bought -- reputation must not depend
    # on whether somebody paid.
    SignalState.FROZEN: {SignalState.LISTED, SignalState.EXPIRED},
    SignalState.LISTED: {SignalState.PURCHASED, SignalState.AWAITING_OUTCOME, SignalState.EXPIRED},
    SignalState.PURCHASED: {SignalState.DELIVERED, SignalState.AWAITING_OUTCOME},
    SignalState.DELIVERED: {SignalState.AWAITING_OUTCOME},
    SignalState.AWAITING_OUTCOME: {SignalState.MATURED, SignalState.UNRESOLVED},
    SignalState.MATURED: {SignalState.SCORED, SignalState.UNRESOLVED},
    SignalState.SCORED: {SignalState.VERIFIED},
    SignalState.VERIFIED: set(),
    SignalState.EXPIRED: set(),
    SignalState.UNRESOLVED: set(),
}


class IllegalTransition(ValueError):
    pass


def assert_transition(current: SignalState, target: SignalState) -> None:
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise IllegalTransition(f"{current.value} -> {target.value} is not a permitted transition")


class Claim(BaseModel):
    """One factual assertion plus the evidence keys that support it."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=8, max_length=400)
    evidence_keys: list[str] = Field(min_length=1, max_length=8)

    @field_validator("evidence_keys")
    @classmethod
    def _keys_clean(cls, v: list[str]) -> list[str]:
        for k in v:
            if not k or not re.fullmatch(r"[a-z0-9_]{2,48}", k):
                raise ValueError(f"malformed evidence key: {k!r}")
        return v


class Invalidation(BaseModel):
    """The condition under which the prediction is considered wrong early."""

    model_config = ConfigDict(extra="forbid")

    condition: str = Field(min_length=8, max_length=300)
    reference_price: Decimal
    evidence_keys: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("reference_price")
    @classmethod
    def _positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("invalidation reference_price must be positive")
        return v


class ModelSignal(BaseModel):
    """The structured object a model provider must return.

    Note what is absent: no price, no reputation, no outcome. A model cannot
    express those, so it cannot influence them.
    """

    model_config = ConfigDict(extra="forbid")

    direction: Direction
    confidence: Decimal
    thesis: str = Field(min_length=24, max_length=1200)
    claims: list[Claim] = Field(min_length=1, max_length=8)
    risk_factors: list[str] = Field(min_length=1, max_length=6)
    invalidation: Invalidation

    @field_validator("confidence")
    @classmethod
    def _conf_range(cls, v: Decimal) -> Decimal:
        if not (Decimal(0) <= v <= Decimal(1)):
            raise ValueError(f"confidence must be within [0, 1], got {v}")
        return v.quantize(Decimal("0.0001"))

    @field_validator("risk_factors")
    @classmethod
    def _risks_nonempty(cls, v: list[str]) -> list[str]:
        for r in v:
            if len(r.strip()) < 8:
                raise ValueError("each risk factor must be a substantive statement")
        return v

    @model_validator(mode="after")
    def _coherent(self) -> "ModelSignal":
        # A directional call with no conviction is not a product; a stand-down with
        # high conviction is a contradiction. Both are rejected outright.
        if self.direction in (Direction.LONG, Direction.SHORT) and self.confidence < Decimal("0.05"):
            raise ValueError("directional signal requires confidence >= 0.05")
        if self.direction == Direction.STAND_DOWN and self.confidence > Decimal("0.5"):
            raise ValueError("STAND_DOWN cannot carry confidence above 0.5")
        return self

    def evidence_keys(self) -> set[str]:
        keys: set[str] = set()
        for c in self.claims:
            keys.update(c.evidence_keys)
        keys.update(self.invalidation.evidence_keys)
        return keys


class SignalMeta(BaseModel):
    """Code-authored fields. The model never supplies any of these."""

    model_config = ConfigDict(extra="forbid")

    signal_id: str
    created_at: str
    asset: str
    venue: str
    horizon: str
    horizon_seconds: int
    matures_at: str
    entry_reference: Decimal
    model_provider: str
    model_version: str
    prompt_version: str
    schema_version: str = SCHEMA_VERSION
    snapshot_hash: str
    quant_hash: str
    environment: str

    @field_validator("signal_id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not _SIGNAL_ID.match(v):
            raise ValueError(f"malformed signal_id: {v!r}")
        return v

    @field_validator("horizon")
    @classmethod
    def _known_horizon(cls, v: str) -> str:
        if v not in HORIZONS:
            raise ValueError(f"unsupported horizon: {v!r}")
        return v

    @field_validator("entry_reference")
    @classmethod
    def _entry_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("entry_reference must be positive")
        return v
