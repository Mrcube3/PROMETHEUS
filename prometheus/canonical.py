"""Deterministic canonical serialisation and hashing.

Every hash PROMETHEUS publishes is produced here so that an independent party can
recompute it. The rules are intentionally boring and fully specified:

  * UTF-8, no BOM
  * object keys sorted lexicographically by code point
  * no insignificant whitespace (``,`` and ``:`` separators)
  * non-ASCII characters emitted literally, not escaped
  * floats are rejected -- see ``_freeze`` -- because float repr is not a stable
    cross-language contract. Numbers that need decimals travel as strings.

The float ban is the important one. A signal hash that a buyer cannot reproduce in
another language is not a proof of anything.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

CANONICAL_VERSION = "canonical-json-1.0.0"


class NonCanonicalValue(TypeError):
    """Raised when a value cannot be canonically serialised."""


def _freeze(value: Any) -> Any:
    """Recursively convert ``value`` into canonical-safe primitives."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise NonCanonicalValue(
            "float values are not canonically serialisable; carry decimals as str "
            f"(offending value: {value!r})"
        )
    if isinstance(value, Decimal):
        # Decimal has an exact, stable textual form. Normalise away trailing zeros
        # so 1.50 and 1.5 cannot produce two different hashes for one price.
        return format(value.normalize(), "f")
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise NonCanonicalValue(f"object keys must be str, got {type(key).__name__}")
            out[key] = _freeze(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_freeze(item) for item in value]
    raise NonCanonicalValue(f"type {type(value).__name__} is not canonically serialisable")


def canonical_json(value: Any) -> str:
    """Return the canonical JSON text for ``value``."""
    return json.dumps(
        _freeze(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_hex(value: Any) -> str:
    """SHA-256 of the canonical serialisation, lowercase hex, ``sha256:`` prefixed."""
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def verify_hash(value: Any, expected: str) -> bool:
    """Constant-time-ish comparison of a recomputed hash against ``expected``."""
    import hmac

    return hmac.compare_digest(sha256_hex(value), expected)
