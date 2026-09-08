"""Provenance envelope for every externally sourced value.

The rule this module enforces is the one that makes the rest of the product
honest: a number that came off Binance and a number PROMETHEUS worked out itself
must never look the same downstream. Missing data stays missing -- there is no
code path here that turns ``None`` into ``0``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: datetime) -> str:
    """RFC3339 with millisecond precision and a literal Z."""
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def now_iso() -> str:
    return iso(utcnow())


def ms_to_iso(ms: int) -> str:
    return iso(datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc))


class Freshness(str, enum.Enum):
    FRESH = "FRESH"
    AGING = "AGING"
    STALE = "STALE"
    EXPIRED = "EXPIRED"
    UNAVAILABLE = "UNAVAILABLE"


class Classification(str, enum.Enum):
    BINANCE_REPORTED = "BINANCE_REPORTED"
    PROMETHEUS_ESTIMATE = "PROMETHEUS_ESTIMATE"
    SIMULATION = "SIMULATION"
    UNAVAILABLE = "UNAVAILABLE"


class Status(str, enum.Enum):
    """Integration / subsystem status vocabulary used across the product."""

    VERIFIED_LIVE = "VERIFIED_LIVE"
    VERIFIED_TESTNET = "VERIFIED_TESTNET"
    VERIFIED_LOCAL = "VERIFIED_LOCAL"
    SIMULATION = "SIMULATION"
    MOCK = "MOCK"
    ADAPTER_ONLY = "ADAPTER_ONLY"
    UNVERIFIED = "UNVERIFIED"
    UNAVAILABLE = "UNAVAILABLE"
    BROKEN = "BROKEN"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


# Freshness thresholds in milliseconds. A quote older than EXPIRED_MS must never be
# used to originate a tradeable prediction.
FRESH_MS = 3_000
AGING_MS = 15_000
STALE_MS = 60_000
EXPIRED_MS = 300_000


def classify_freshness(age_ms: int | None) -> Freshness:
    if age_ms is None:
        return Freshness.UNAVAILABLE
    if age_ms < 0:
        # Clock skew between us and the venue. Treat as fresh but do not pretend
        # to negative age -- callers clamp before storing.
        return Freshness.FRESH
    if age_ms <= FRESH_MS:
        return Freshness.FRESH
    if age_ms <= AGING_MS:
        return Freshness.AGING
    if age_ms <= STALE_MS:
        return Freshness.STALE
    return Freshness.EXPIRED


@dataclass(frozen=True)
class Field:
    """A single provenance-tagged value.

    ``value`` is ``None`` exactly when the datum is genuinely unavailable.
    """

    value: Any
    source: str
    timestamp: str | None
    retrieved_at: str
    age_ms: int | None
    freshness: Freshness
    status: Status
    classification: Classification

    def to_dict(self) -> dict[str, Any]:
        value = self.value
        if isinstance(value, Decimal):
            value = format(value.normalize(), "f")
        return {
            "value": value,
            "source": self.source,
            "timestamp": self.timestamp,
            "retrieved_at": self.retrieved_at,
            "age_ms": self.age_ms,
            "freshness": self.freshness.value,
            "status": self.status.value,
            "classification": self.classification.value,
        }

    @property
    def usable(self) -> bool:
        """True when the value exists and is not past its shelf life."""
        return self.value is not None and self.freshness not in (
            Freshness.EXPIRED,
            Freshness.UNAVAILABLE,
        )


def binance_field(
    value: Any,
    *,
    source: str,
    event_ms: int | None,
    retrieved_ms: int,
) -> Field:
    """Wrap a value Binance actually reported."""
    if value is None:
        return unavailable_field(source=source)
    age = None if event_ms is None else max(0, retrieved_ms - event_ms)
    return Field(
        value=value,
        source=source,
        timestamp=None if event_ms is None else ms_to_iso(event_ms),
        retrieved_at=ms_to_iso(retrieved_ms),
        age_ms=age,
        freshness=classify_freshness(age),
        status=Status.VERIFIED_LIVE,
        classification=Classification.BINANCE_REPORTED,
    )


def estimate_field(
    value: Any,
    *,
    source: str,
    basis_ms: int | None,
    retrieved_ms: int,
) -> Field:
    """Wrap a value PROMETHEUS derived itself."""
    if value is None:
        return unavailable_field(source=source, classification=Classification.PROMETHEUS_ESTIMATE)
    age = None if basis_ms is None else max(0, retrieved_ms - basis_ms)
    return Field(
        value=value,
        source=source,
        timestamp=None if basis_ms is None else ms_to_iso(basis_ms),
        retrieved_at=ms_to_iso(retrieved_ms),
        age_ms=age,
        freshness=classify_freshness(age),
        status=Status.VERIFIED_LOCAL,
        classification=Classification.PROMETHEUS_ESTIMATE,
    )


def unavailable_field(
    *,
    source: str,
    classification: Classification = Classification.UNAVAILABLE,
    reason: str | None = None,
) -> Field:
    return Field(
        value=None,
        source=source if reason is None else f"{source} ({reason})",
        timestamp=None,
        retrieved_at=now_iso(),
        age_ms=None,
        freshness=Freshness.UNAVAILABLE,
        status=Status.UNAVAILABLE,
        classification=classification,
    )
